"""
Blast reader: turn a #blast-tugas message into structured extra requirements.

This is the second layer of ketentuan (see planning §5.1). rulebase.json is
static; blast_context.json is dynamic and extracted from what the koor actually
posted — custom caption, specific tags, hashtags, Drive folder, deadline.

Parsing is deterministic (regex + rulebase matching), no LLM. A wrong guess here
would silently change fines, so ambiguous extractions keep task_id=None and the
validator warns instead of applying them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from . import config
from .models import BlastContext
from .utils import (
    day_name_matches,
    extract_hashtags,
    extract_mentions,
    fmt_dt,
    normalize_sheet_name,
    parse_dt,
    sheet_similarity,
    truncate,
    wib_now,
)

KEYWORDS = ("blast", "tugas", "pp", "buzzer", "deadline", "paid promote")
LABELS = (
    "caption", "media", "tags", "tag", "hashtag", "deadline", "form",
    "link", "drive", "catatan", "note", "platform",
)
_DRIVE_RE = re.compile(r"https?://drive\.google\.com/[^\s,;<>\"']+", re.IGNORECASE)
_FORM_RE = re.compile(
    r"https?://(?:forms\.gle|docs\.google\.com/forms)[^\s,;<>\"']+", re.IGNORECASE
)
_TASK_ID_RE = re.compile(r"\b(PP|BZ)[-\s]?(\d{1,2})\b", re.IGNORECASE)


@dataclass
class ParsedBlast:
    task_id: str | None = None
    task_type: str = "PP"
    blast_date: datetime | None = None
    caption_referensi: str = ""
    tags: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    deadline: datetime | None = None
    drive_folder: str = ""
    form_link: str = ""
    notes: list[str] = field(default_factory=list)
    confidence: float = 0.0
    id_confidence: float = 0.0
    raw_message: str = ""
    short_description: str = ""
    search_tags: list[str] = field(default_factory=list)

    def to_context(self, message_id: str = "") -> BlastContext:
        return BlastContext(
            task_id=self.task_id or "",
            blast_message_id=message_id,
            blast_date=self.blast_date,
            caption_referensi=self.caption_referensi,
            tags=self.tags,
            hashtags=self.hashtags,
            deadline=self.deadline,
            drive_folder=self.drive_folder,
            form_link=self.form_link,
            notes=self.notes,
            extracted_at=wib_now(),
            confidence=self.confidence,
            raw_message=self.raw_message,
            short_description=self.short_description,
            search_tags=self.search_tags,
        )


def looks_like_blast(text: str | None) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(keyword in lowered for keyword in KEYWORDS)


def _extract_caption(lines: list[str]) -> str:
    """Collect the block after a 'Caption:' label until the next label."""
    for index, line in enumerate(lines):
        stripped = line.strip()
        match = re.match(r"^\W*(caption|caption referensi|copy)\W*[:\-]\s*(.*)$", stripped, re.I)
        if not match:
            continue
        collected = [match.group(2).strip()] if match.group(2).strip() else []
        for follow in lines[index + 1 :]:
            probe = follow.strip()
            if not probe:
                if collected:
                    break
                continue
            if re.match(r"^\W*(%s)\W*[:\-]" % "|".join(LABELS), probe, re.I):
                break
            collected.append(probe)
        return "\n".join(collected).strip()
    return ""


def _extract_field(text: str, *names: str) -> str:
    pattern = r"(?:%s)\W*[:\-]\s*(.+)" % "|".join(re.escape(name) for name in names)
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def generate_next_task_id(task_type: str, existing_ids: Iterable[str]) -> str:
    prefix = "BZ" if str(task_type or "").upper() == "BUZZER" else "PP"
    highest = 0
    pattern = re.compile(rf"\b{prefix}[-\s]?(\d{{1,2}})\b", re.IGNORECASE)
    for raw in existing_ids:
        for match in pattern.finditer(str(raw or "")):
            try:
                num = int(match.group(1))
                if num > highest:
                    highest = num
            except ValueError:
                pass
    return f"{prefix}-{highest + 1:02d}"


def extract_short_description(
    text: str,
    task_type: str,
    deadline: datetime | None,
    tags: list[str],
    task_id: str | None = None,
) -> str:
    lowered = (text or "").lower()
    detected_platforms: list[str] = []
    if "story" in lowered:
        detected_platforms.append("Story")
    if "feed" in lowered:
        detected_platforms.append("Feed")
    if "reel" in lowered:
        detected_platforms.append("Reels")
    if "tiktok" in lowered:
        detected_platforms.append("TikTok")
    if "x.com" in lowered or "twitter" in lowered:
        detected_platforms.append("X/Twitter")

    if not detected_platforms:
        detected_platforms = ["Story & Feed"] if task_type == "PP" else ["Instagram & TikTok"]

    platform_str = " / ".join(detected_platforms)

    sponsor = _extract_field(text or "", "sponsor", "target", "klien")
    if not sponsor:
        for tag in tags:
            clean = tag.strip().lower()
            if clean not in ("@horizon_umn", "@horizon.umn"):
                sponsor = tag.strip()
                break
    if not sponsor:
        first_line = (text or "").strip().splitlines()[0] if text else ""
        if len(first_line) > 5 and not first_line.lower().startswith("blast"):
            sponsor = truncate(first_line, 50)

    deadline_str = fmt_dt(deadline) if deadline else "Belum ditentukan"
    label = task_id or task_type
    parts = [f"Tugas {label} ({platform_str})"]
    if sponsor:
        parts.append(f"• Target/Sponsor: {sponsor}")
    parts.append(f"• Deadline: {deadline_str}")
    return " ".join(parts)


def extract_search_tags(
    task_id: str | None,
    task_type: str,
    tags: list[str],
    hashtags: list[str],
    text: str,
) -> list[str]:
    results: list[str] = []
    seen: set[str] = set()

    def add(item: str) -> None:
        val = item.strip()
        if not val:
            return
        tag = val if val.startswith("#") else f"#{val}"
        lower = tag.lower()
        if lower not in seen:
            seen.add(lower)
            results.append(tag)

    if task_id:
        for code in re.findall(r"\b(PP|BZ)[-\s]?(\d{1,2})\b", task_id, re.IGNORECASE):
            p, n = code[0].upper(), int(code[1])
            add(f"{p}-{n:02d}")
            add(f"{p}{n:02d}")

    if task_type == "PP":
        add("PP")
        add("PaidPromote")
    else:
        add("Buzzer")
        add("BZ")

    lowered = (text or "").lower()
    if "story" in lowered:
        add("Story")
    if "feed" in lowered:
        add("Feed")
    if "reel" in lowered:
        add("Reels")
    if "tiktok" in lowered:
        add("TikTok")

    for mention in tags:
        clean = re.sub(r"[^\w]", "", mention)
        if clean:
            add(clean)

    for h in hashtags:
        clean = re.sub(r"^[#]+", "", h).strip()
        if clean:
            add(clean)

    return results


def _guess_task_id(
    text: str,
    parsed: ParsedBlast,
    rulebase: dict[str, Any] | None,
    message_date: datetime | None,
) -> tuple[str | None, float]:
    # 1. Explicit task id(s) in the message.
    matches = _TASK_ID_RE.findall(text)
    if matches:
        seen = set()
        codes = []
        for prefix, number in matches:
            code = f"{prefix.upper()}-{int(number):02d}"
            if code not in seen:
                seen.add(code)
                codes.append(code)
        return " / ".join(codes), 0.95

    rulebase = rulebase or {}
    tasks = [
        task
        for key in ("tugas_pp", "tugas_buzzer")
        for task in rulebase.get(key, []) or []
    ]
    if not tasks:
        try:
            from .agent.knowledge import all_tasks
            tasks = [t.raw for t in all_tasks()]
        except Exception:
            tasks = []
    if not tasks:
        return None, 0.0

    # 2. Form link equality — a form belongs to exactly one task.
    if parsed.form_link:
        normalized_form = parsed.form_link.split("?")[0].rstrip("/")
        for task in tasks:
            task_form = str(task.get("form_link") or "").split("?")[0].rstrip("/")
            if task_form and task_form == normalized_form:
                return task["id"], 0.95

    # 3. Sheet name spelled out in the message ("PP Feed 26 Oktober 2026").
    sheet_best: tuple[str | None, float] = (None, 0.0)
    for task in tasks:
        sheet = task.get("sheet")
        if not sheet:
            continue
        score = sheet_similarity(text[:400], sheet)
        if score > sheet_best[1]:
            sheet_best = (task["id"], score)
    if sheet_best[0] and sheet_best[1] >= 0.9:
        return sheet_best[0], 0.9

    # 4. Drive folder equality.
    if parsed.drive_folder:
        for task in tasks:
            if task.get("drive_link") and task["drive_link"] == parsed.drive_folder:
                return task["id"], 0.85

    # 5. Tag / hashtag overlap.
    best: tuple[str | None, float] = (None, 0.0)
    message_tags = {tag.lower() for tag in parsed.tags}
    message_hashtags = {tag.lower() for tag in parsed.hashtags}
    for task in tasks:
        block_tags: set[str] = set()
        block_hashtags: set[str] = set()
        for key in ("story", "feed"):
            block = task.get(key) or {}
            block_tags |= {str(tag).lower() for tag in block.get("tags") or []}
            block_hashtags |= {str(tag).lower() for tag in block.get("hashtags") or []}
        score = 0.0
        if message_tags and block_tags:
            score += 0.45 * len(message_tags & block_tags) / len(message_tags | block_tags)
        if message_hashtags and block_hashtags:
            score += 0.35 * len(message_hashtags & block_hashtags) / len(
                message_hashtags | block_hashtags
            )
        if score > best[1]:
            best = (task["id"], score)

    if best[0] and best[1] >= 0.35:
        return best[0], min(0.8, 0.4 + best[1])

    return None, 0.0


def check_date_guardrail(parsed: ParsedBlast) -> list[str]:
    """
    G1 from the rulebase: validate the dates a koor typed into the blast.

    Checks that the deadline exists, is after the blast date, and that any
    weekday name matches the date. Returns human-readable problems; the caller
    decides whether to refuse or just warn.
    """
    problems: list[str] = []
    raw = parsed.raw_message or ""

    if parsed.deadline is None:
        problems.append("deadline tidak terbaca dari blast")
    if parsed.blast_date and parsed.deadline and parsed.deadline < parsed.blast_date:
        problems.append(
            f"deadline {parsed.deadline:%d %b %H:%M} mendahului tanggal blast "
            f"{parsed.blast_date:%d %b %H:%M}"
        )

    if parsed.deadline:
        deadline_line = next(
            (line for line in raw.splitlines() if "deadline" in line.lower()),
            "",
        )
        if deadline_line:
            matches = day_name_matches(parsed.deadline, deadline_line)
            if matches is False:
                problems.append(
                    f"nama hari di '{deadline_line.strip()}' tidak cocok dengan "
                    f"tanggal ({parsed.deadline:%A})"
                )
    return problems


def parse_blast_message(
    text: str,
    rulebase: dict[str, Any] | None = None,
    message_date: datetime | None = None,
    context_store: BlastContextStore | None = None,
) -> ParsedBlast:
    rulebase = rulebase or {}
    lines = str(text or "").splitlines()
    parsed = ParsedBlast(raw_message=text or "")

    parsed.task_type = "BUZZER" if "buzzer" in (text or "").lower() else "PP"
    parsed.tags = extract_mentions(text)
    parsed.hashtags = extract_hashtags(text)
    parsed.caption_referensi = _extract_caption(lines)

    drive_links = _DRIVE_RE.findall(text or "")
    if drive_links:
        parsed.drive_folder = drive_links[0]

    form_links = _FORM_RE.findall(text or "")
    if form_links:
        parsed.form_link = form_links[0]

    deadline_text = _extract_field(text or "", "deadline", "batas waktu", "tenggat")
    parsed.deadline = parse_dt(deadline_text) if deadline_text else None

    blast_date_text = _extract_field(text or "", "blast", "tanggal blast")
    parsed.blast_date = parse_dt(blast_date_text) if blast_date_text else None
    if parsed.blast_date is None:
        header = parse_dt(str(text or "")[:300], default_time=(0, 0))
        parsed.blast_date = header or message_date

    note_text = _extract_field(text or "", "catatan", "note", "khusus")
    if note_text:
        parsed.notes.append(note_text)

    task_id, id_confidence = _guess_task_id(text or "", parsed, rulebase, message_date)
    if not task_id and context_store is not None:
        task_id = generate_next_task_id(parsed.task_type, context_store.all().keys())
        id_confidence = 0.80
    parsed.task_id = task_id

    parsed.short_description = extract_short_description(
        text or "", parsed.task_type, parsed.deadline, parsed.tags, parsed.task_id
    )
    parsed.search_tags = extract_search_tags(
        parsed.task_id, parsed.task_type, parsed.tags, parsed.hashtags, text or ""
    )

    # Confidence: how much of the context we actually recovered.
    fields = [
        bool(parsed.caption_referensi),
        bool(parsed.tags),
        bool(parsed.hashtags),
        bool(parsed.deadline),
        bool(parsed.drive_folder),
        bool(parsed.form_link),
        bool(parsed.blast_date),
    ]
    parsed.confidence = round(
        min(0.95, 0.35 + 0.09 * sum(fields) + (0.1 if task_id else 0.0)), 2
    )

    # G1 guardrail — a blast with impossible dates must not silently shape fines.
    for problem in check_date_guardrail(parsed):
        parsed.notes.append(f"⚠️ {problem}")
        parsed.confidence = round(max(0.2, parsed.confidence - 0.15), 2)

    parsed.id_confidence = id_confidence
    return parsed


# ─── Storage ────────────────────────────────────────────────────────────────
class BlastContextStore:
    """Read/write output/blast_context.json."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or config.BLAST_CONTEXT_PATH)
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "updated_at": "", "contexts": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "updated_at": "", "contexts": {}}
        if "contexts" not in data:
            data = {"version": 1, "updated_at": "", "contexts": data}
        return data

    def reload(self) -> None:
        self._data = self._load()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data["updated_at"] = wib_now().isoformat(timespec="seconds")
        self.path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def upsert(self, context: BlastContext) -> None:
        if not context.task_id:
            return
        self._data["contexts"][context.task_id] = context.to_dict()
        self.save()

    def get(self, task_id: str | None) -> BlastContext | None:
        if not task_id:
            return None
        raw = self._data.get("contexts", {}).get(task_id)
        return BlastContext.from_dict(raw) if raw else None

    def all(self) -> dict[str, BlastContext]:
        return {
            task_id: BlastContext.from_dict(raw)
            for task_id, raw in self._data.get("contexts", {}).items()
        }

    def remove(self, task_id: str) -> bool:
        contexts = self._data.get("contexts", {})
        if task_id in contexts:
            del contexts[task_id]
            self.save()
            return True
        return False

    def summary(self) -> dict[str, Any]:
        contexts = self._data.get("contexts", {})
        return {
            "count": len(contexts),
            "task_ids": sorted(contexts.keys()),
            "updated_at": self._data.get("updated_at", ""),
        }


def load_contexts(path: Path | None = None) -> dict[str, BlastContext]:
    return BlastContextStore(path).all()


# ─── Processed message cache ────────────────────────────────────────────────
class ProcessedMessageCache:
    """Remembers which #blast-tugas messages were already extracted."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or config.PROCESSED_MESSAGES_PATH)
        self._ids: set[str] = set()
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._ids = set(data.get("message_ids", []))
            except (OSError, json.JSONDecodeError):
                self._ids = set()

    def __contains__(self, message_id: str) -> bool:
        return str(message_id) in self._ids

    def add(self, message_id: str) -> None:
        self._ids.add(str(message_id))
        self._flush()

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"message_ids": sorted(self._ids)}, indent=2), encoding="utf-8"
        )

    def __len__(self) -> int:
        return len(self._ids)
