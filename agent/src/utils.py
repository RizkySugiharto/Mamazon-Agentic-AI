"""
Shared helpers: Indonesian date parsing, name/sheet normalization, fuzzy
matching, Drive link parsing, hashing and formatting.

Kept dependency-free (stdlib only) so every module can import it cheaply and
the unit tests run without Pillow / discord.py installed.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date, datetime, time, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Iterator, Sequence

try:
    from zoneinfo import ZoneInfo

    WIB = ZoneInfo("Asia/Jakarta")
except Exception:  # pragma: no cover - Windows without tzdata
    WIB = None  # type: ignore[assignment]


# ─── Time ───────────────────────────────────────────────────────────────────
BULAN = {
    "januari": 1, "jan": 1,
    "februari": 2, "feb": 2, "pebruari": 2,
    "maret": 3, "mar": 3,
    "april": 4, "apr": 4,
    "mei": 5,
    "juni": 6, "jun": 6,
    "juli": 7, "jul": 7,
    "agustus": 8, "agu": 8, "agt": 8, "ags": 8,
    "september": 9, "sep": 9, "sept": 9,
    "oktober": 10, "okt": 10, "okto": 10,
    "november": 11, "nov": 11, "nopember": 11, "nop": 11,
    "desember": 12, "des": 12,
}
HARI = {
    "senin": 0, "selasa": 1, "rabu": 2, "kamis": 3,
    "jumat": 4, "jum'at": 4, "sabtu": 5, "minggu": 6, "ahad": 6,
}

_MONTH_ALT = "|".join(sorted(BULAN, key=len, reverse=True))
_DATE_RE = re.compile(
    rf"(\d{{1,2}})\s+({_MONTH_ALT})\s+(\d{{4}})"
    r"(?:\s*(?:pukul|jam)?\s*(\d{1,2})\s*[:.]\s*(\d{2})(?:\s*[:.]\s*(\d{2}))?)?",
    re.IGNORECASE,
)
_ISO_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?"
)


def wib_now() -> datetime:
    """Current time in WIB (UTC+7), timezone-aware when tzdata is available."""
    if WIB is not None:
        return datetime.now(WIB)
    from datetime import timezone

    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=7)


def wib_today() -> date:
    return wib_now().date()


def to_wib(dt: datetime) -> datetime:
    """Attach/convert to WIB. Naive datetimes are assumed to already be WIB."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=WIB) if WIB else dt
    return dt.astimezone(WIB) if WIB else dt


def naive(dt: datetime | None) -> datetime | None:
    """Drop tzinfo for safe comparison between stored and parsed values."""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def parse_dt(
    text: str | None,
    default_time: tuple[int, int] | None = (23, 59),
) -> datetime | None:
    """
    Parse the first Indonesian or ISO datetime in `text`.

    Handles "Kamis, 29 Oktober 2026 15:00 WIB", "29 Okt 2026", "2026-10-29".
    When the string has no clock time, `default_time` is applied (pass None to
    keep midnight).
    """
    if not text:
        return None
    text = str(text).strip()

    match = _ISO_RE.search(text)
    if match:
        year, month, day = (int(match.group(i)) for i in (1, 2, 3))
        hour = int(match.group(4)) if match.group(4) else None
        minute = int(match.group(5)) if match.group(5) else None
        second = int(match.group(6)) if match.group(6) else 0
        try:
            if hour is None:
                hour, minute = default_time if default_time else (0, 0)
                second = 0
            return datetime(year, month, day, hour, minute, second)
        except ValueError:
            return None

    match = _DATE_RE.search(text)
    if match:
        day = int(match.group(1))
        month = BULAN[match.group(2).lower()]
        year = int(match.group(3))
        hour = int(match.group(4)) if match.group(4) else None
        minute = int(match.group(5)) if match.group(5) else None
        second = int(match.group(6)) if match.group(6) else 0
        try:
            if hour is None:
                hour, minute = default_time if default_time else (0, 0)
                second = 0
            return datetime(year, month, day, hour, minute, second)
        except ValueError:
            return None
    return None


def parse_dt_all(text: str | None) -> list[datetime]:
    """Parse every datetime in a string (used for date ranges)."""
    if not text:
        return []
    found: list[datetime] = []
    for match in _DATE_RE.finditer(text):
        day = int(match.group(1))
        month = BULAN[match.group(2).lower()]
        year = int(match.group(3))
        hour = int(match.group(4)) if match.group(4) else 23
        minute = int(match.group(5)) if match.group(5) else 59
        second = int(match.group(6)) if match.group(6) else 0
        try:
            found.append(datetime(year, month, day, hour, minute, second))
        except ValueError:
            continue
    if not found:
        single = parse_dt(text)
        if single:
            found.append(single)
    return found


def parse_date_range(text: str | None) -> tuple[datetime | None, datetime | None]:
    """Return (start, end) for "28 Oktober 2026 - 2 November 2026" style text."""
    stamps = parse_dt_all(text)
    if not stamps:
        return None, None
    if len(stamps) == 1:
        return stamps[0], stamps[0]
    return min(stamps), max(stamps)


def day_name_matches(dt: datetime, text: str) -> bool | None:
    """
    Verify the weekday written in `text` against `dt`.
    Returns None when the text contains no day name.
    """
    lowered = text.lower()
    for name, index in HARI.items():
        if name in lowered:
            return dt.weekday() == index
    return None


def fmt_dt(dt: datetime | None, with_time: bool = True) -> str:
    if dt is None:
        return "-"
    if with_time:
        return dt.strftime("%d %b %Y %H:%M WIB")
    return dt.strftime("%d %b %Y")


def fmt_date_short(dt: datetime | None) -> str:
    if dt is None:
        return "-"
    return dt.strftime("%d/%m/%Y")


def fmt_timestamp(dt: datetime | None = None) -> str:
    """ISO-ish timestamp used in audit logs and file names."""
    return (dt or wib_now()).strftime("%Y-%m-%dT%H:%M:%S")


def blast_id_now(dt: datetime | None = None) -> str:
    return "blast_" + (dt or wib_now()).strftime("%Y%m%d_%H%M%S")


# ─── Numbers & text ─────────────────────────────────────────────────────────
def format_rupiah(value: int | float | None) -> str:
    if not value:
        return "Rp 0"
    return "Rp " + f"{int(round(value)):,}".replace(",", ".")


def safe_int(value: object, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        if isinstance(value, str):
            cleaned = re.sub(r"[^\d.-]", "", value)
            if not cleaned:
                return default
            return int(float(cleaned))
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value: object, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        if isinstance(value, str):
            cleaned = re.sub(r"[^\d.,-]", "", value).replace(",", ".")
            if not cleaned:
                return default
            return float(cleaned)
        return float(value)
    except (TypeError, ValueError):
        return default


def strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )


def normalize_name(name: str | None) -> str:
    """Lowercase, accent-free, alphanumeric-only name key."""
    if not name:
        return ""
    text = strip_accents(str(name)).lower()
    text = re.sub(r"\(.*?\)", " ", text)
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def name_tokens(name: str | None) -> set[str]:
    tokens = {t for t in normalize_name(name).split() if len(t) > 1}
    return tokens


def name_similarity(a: str | None, b: str | None) -> float:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    if ta and tb:
        overlap = len(ta & tb) / max(len(ta), len(tb))
        ratio = max(ratio, overlap * 0.95)
    return round(ratio, 4)


def best_name_match(query: str, candidates: Sequence[str], threshold: float = 0.82):
    """Return (candidate, score) for the closest name, or (None, 0)."""
    scored = [(c, name_similarity(query, c)) for c in candidates]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    if scored and scored[0][1] >= threshold:
        return scored[0]
    return None, 0.0


_MONTH_EXPAND = {
    "okt": "oktober", "jan": "januari", "feb": "februari", "mar": "maret",
    "apr": "april", "jun": "juni", "jul": "juli", "agu": "agustus",
    "agt": "agustus", "ags": "agustus", "sep": "september", "sept": "september",
    "nov": "november", "nop": "november", "des": "desember",
}
_NOISE_WORDS = {"sheet", "form", "submission", "tugas", "mamazon", "horizon"}


def normalize_sheet_name(name: str | None) -> str:
    """
    Normalize sheet names for comparison: "PP Feed 26 Oktober 2026" and
    "pp feed 26 okt" both collapse to "pp feed 26 oktober".
    """
    if not name:
        return ""
    text = strip_accents(str(name)).lower()
    text = re.sub(r"[_\-–—/\\|.,:;()\[\]]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    words = []
    for word in text.split():
        word = _MONTH_EXPAND.get(word, word)
        if word in _NOISE_WORDS:
            continue
        if re.fullmatch(r"20\d{2}", word):
            continue
        words.append(word)
    return " ".join(words)


_SHEET_VARIANTS = {
    "feed", "bundle", "story", "vote", "gmaps", "ig", "reels", "tiktok",
    "sponsor", "survey", "review",
}


def _sheet_parts(name: str) -> tuple[str, set[str], set[int], set[str], set[str]]:
    normalized = normalize_sheet_name(name)
    tokens = normalized.split()
    numbers = {int(token) for token in tokens if token.isdigit()}
    months = {token for token in tokens if token in BULAN}
    variants = {token for token in tokens if token in _SHEET_VARIANTS}
    return normalized, set(tokens), numbers, months, variants


def _sheet_family(tokens: set[str]) -> str | None:
    if "pp" in tokens or "paid" in tokens:
        return "pp"
    if "buzzer" in tokens or "buzz" in tokens:
        return "buzzer"
    return None


def sheet_similarity(query: str, candidate: str) -> float:
    """
    Structure-aware similarity for sheet names.

    Plain string similarity is dangerous here: "PP 27 Oktober" scores ~0.92
    against "PP 07 Oktober" even though the day is different. Day and month
    mismatches are therefore treated as hard conflicts, and the variant word
    (feed / bundle / story / vote / gmaps / ig) decides ties.
    """
    nq, tokens_q, nums_q, months_q, variants_q = _sheet_parts(query)
    nc, tokens_c, nums_c, months_c, variants_c = _sheet_parts(candidate)
    if not nq or not nc:
        return 0.0
    if nq == nc:
        return 1.0

    # Hard conflict: PP vs Buzzer.
    family_q, family_c = _sheet_family(tokens_q), _sheet_family(tokens_c)
    if family_q and family_c and family_q != family_c:
        return 0.15

    # Hard conflict: different month.
    if months_q and months_c and not (months_q & months_c):
        return 0.05

    # Both sides carry a day number.
    if nums_q and nums_c:
        if not (nums_q & nums_c):
            return 0.10  # different day = different task, never guess
        if variants_q and variants_c:
            if variants_q == variants_c:
                return 0.98
            return 0.85  # same day+month, different variant -> genuine ambiguity
        if variants_q or variants_c:
            return 0.90
        return 0.95

    # Query is a strict subset of the candidate ("PP Feed" -> "PP Feed 26 Oktober").
    if tokens_q and tokens_q <= tokens_c:
        return min(0.95, 0.72 + 0.08 * len(tokens_q))
    if tokens_c and tokens_c <= tokens_q:
        return 0.70

    jaccard = len(tokens_q & tokens_c) / max(1, len(tokens_q | tokens_c))
    return round(min(0.60, jaccard * 0.75), 4)


def word_count(text: str | None) -> int:
    return len(re.findall(r"[A-Za-zÀ-ÿ0-9']+", text or ""))


_EMOJI_RE = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF" "\U00002B00-\U00002BFF" "\U0000FE0F" "\U0000200D" "]"
)


def is_emoji_only(text: str | None) -> bool:
    if not text:
        return True
    stripped = _EMOJI_RE.sub("", text)
    return not re.search(r"[A-Za-z0-9]", stripped)


def normalize_caption(text: str | None) -> str:
    """Whitespace-collapsed caption used for verbatim comparison."""
    if not text:
        return ""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def caption_similarity(a: str | None, b: str | None) -> float:
    """
    Compare captions ignoring line-break and spacing differences.

    OCR from a screenshot never reproduces newlines exactly, so treating a
    line break as a caption edit would fine innocent panitia. Wording, emoji and
    punctuation still count.
    """
    na = re.sub(r"\s+", " ", normalize_caption(a)).strip().lower()
    nb = re.sub(r"\s+", " ", normalize_caption(b)).strip().lower()
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return round(SequenceMatcher(None, na, nb).ratio(), 4)


def truncate(text: str | None, limit: int = 1000, suffix: str = "…") -> str:
    if not text:
        return ""
    text = str(text)
    return text if len(text) <= limit else text[: limit - len(suffix)] + suffix


def slugify(text: str, max_len: int = 40) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return cleaned[:max_len] or "x"


def chunked(items: Sequence, size: int) -> Iterator[list]:
    for start in range(0, len(items), max(1, size)):
        yield list(items[start : start + size])


# ─── Links ──────────────────────────────────────────────────────────────────
_URL_RE = re.compile(r"https?://[^\s,;\"'<>]+", re.IGNORECASE)
_DRIVE_PATTERNS = [
    re.compile(r"/file/d/([A-Za-z0-9_-]{10,})"),
    re.compile(r"/document/d/([A-Za-z0-9_-]{10,})"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})"),
    re.compile(r"/d/([A-Za-z0-9_-]{10,})"),
    re.compile(r"^([A-Za-z0-9_-]{20,})$"),
]


def split_links(cell: str | None) -> list[str]:
    """
    Split a Google Form multi-upload cell into individual URLs.

    Google Forms joins multiple file uploads with ", " and sometimes newlines.
    Naive parsing treats the whole cell as one URL, which is the root cause of
    problem #1 in the planning document (74-81% of PP rows are multi-link).
    """
    if not cell:
        return []
    raw = str(cell)
    # Normalise separators that Forms / Sheets may emit.
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    parts = re.split(r"[,\n;]+", raw)
    links: list[str] = []
    for part in parts:
        for match in _URL_RE.finditer(part):
            url = match.group(0).strip().rstrip(".,);")
            if url and url not in links:
                links.append(url)
    if not links:
        # Some sheets store bare file ids separated by commas.
        for part in parts:
            candidate = part.strip()
            if re.fullmatch(r"[A-Za-z0-9_-]{20,}", candidate) and candidate not in links:
                links.append(candidate)
    return links


def extract_drive_file_id(url: str | None) -> str | None:
    """Extract the Drive file id from any common Drive URL shape."""
    if not url:
        return None
    url = str(url).strip()
    for pattern in _DRIVE_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return None


def is_drive_url(url: str | None) -> bool:
    return bool(url) and "drive.google.com" in str(url)


_FOLDER_RE = re.compile(r"/folders/([A-Za-z0-9_-]{10,})")


def extract_drive_folder_id(url: str | None) -> str | None:
    """Extract a folder id from a Drive folder URL or a bare id."""
    if not url:
        return None
    text = str(url).strip()
    match = _FOLDER_RE.search(text)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", text):
        return text
    return extract_drive_file_id(text)


def drive_preview_url(file_id: str | None) -> str:
    if not file_id:
        return ""
    return f"https://drive.google.com/file/d/{file_id}/view"


def drive_open_url(file_id: str | None) -> str:
    if not file_id:
        return ""
    return f"https://drive.google.com/open?id={file_id}"


def extract_hashtags(text: str | None) -> list[str]:
    if not text:
        return []
    seen: list[str] = []
    for match in re.findall(r"#[0-9A-Za-z_]{2,}", str(text)):
        if match.lower() not in {h.lower() for h in seen}:
            seen.append(match)
    return seen


def extract_mentions(text: str | None) -> list[str]:
    if not text:
        return []
    seen: list[str] = []
    for match in re.findall(r"(?<![\w.])@[0-9A-Za-z_.]{2,}", str(text)):
        if match.lower() not in {m.lower() for m in seen}:
            seen.append(match)
    return seen


def normalize_handle(handle: str | None) -> str:
    """'@Horizon_UMN/' -> 'horizon_umn'."""
    if not handle:
        return ""
    return re.sub(r"[^a-z0-9_.]", "", str(handle).lower().lstrip("@"))


def handle_similarity(a: str | None, b: str | None) -> float:
    ha, hb = normalize_handle(a), normalize_handle(b)
    if not ha or not hb:
        return 0.0
    if ha == hb:
        return 1.0
    return round(SequenceMatcher(None, ha, hb).ratio(), 4)


# ─── Hashing & files ────────────────────────────────────────────────────────
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def human_size(num_bytes: int | None) -> str:
    if not num_bytes:
        return "0 B"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def days_between(a: datetime | date, b: datetime | date) -> int:
    if isinstance(a, datetime):
        a = a.date()
    if isinstance(b, datetime):
        b = b.date()
    return (a - b).days


def combine(dt: date, clock: time | None = None) -> datetime:
    return datetime.combine(dt, clock or time(23, 59))


def unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out
