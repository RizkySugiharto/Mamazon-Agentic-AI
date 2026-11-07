"""Build the local knowledge corpus from Markdown rules and index it into zvec.

All rules, task definitions, check specifications, fine schedules, Angel Card terms,
and agent behavioral guidelines are read from Markdown files in `config.RULES_DIR`.

The rulebase.json file and RulebaseEngine are no longer used.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

from .. import config
from ..models import TaskCatalogEntry
from ..utils import format_rupiah, normalize_sheet_name
from .embeddings import LocalEmbeddings, build_embeddings
from .vectorstore import ZvecVectorStore

logger = logging.getLogger(__name__)


# ── document helpers ───────────────────────────────────────────────────────
def _doc(text: str, kind: str, doc_id: str, source: str, **extra: Any) -> dict[str, Any]:
    """One corpus document. `text` is what gets embedded and retrieved."""
    metadata = {"id": doc_id, "kind": kind, "source": source}
    metadata.update(extra)
    return {"page_content": text, "metadata": metadata}


# ── Task Catalog & Markdown Parsers ─────────────────────────────────────────
_TASK_CATALOG_CACHE: dict[str, TaskCatalogEntry] | None = None
_TASK_CATALOG_MTIME: float = 0.0


def _parse_task_markdown(file_path: Path, task_type: str) -> dict[str, TaskCatalogEntry]:
    """Parse a task markdown file (pp_tasks.md or buzzer_tasks.md) into catalog entries."""
    if not file_path.exists():
        return {}
    content = file_path.read_text(encoding="utf-8")
    sections = re.split(r"\n(?=##\s+)", content)
    entries: dict[str, TaskCatalogEntry] = {}
    for sec in sections[1:]:
        header = sec.splitlines()[0].replace("##", "").strip()
        kv: dict[str, str] = dict(re.findall(r"- \*\*([^*]+)\*\*:\s*(.*)", sec))
        task_id = kv.get("ID", header)
        sheet_val = kv.get("Sheet", "")
        sheet = None if sheet_val.strip().lower() in ("none", "") else sheet_val.strip()
        platforms = [p.strip() for p in kv.get("Platform", "").split(",") if p.strip()]

        tags: list[str] = []
        for tag_str in (kv.get("Story Tags", ""), kv.get("Feed Tags", "")):
            for t in tag_str.split(","):
                if t.strip() and t.strip() not in tags:
                    tags.append(t.strip())

        hashtags: list[str] = []
        for h_str in (kv.get("Story Hashtags", ""), kv.get("Feed Hashtags", "")):
            for h in h_str.split(","):
                if h.strip() and h.strip() not in hashtags:
                    hashtags.append(h.strip())

        caption_val = kv.get("Feed Caption Reference", "")
        caption = "" if caption_val.strip().lower() in ("none", "") else caption_val.strip()
        jadwal = kv.get("Jadwal Post", "")
        deadline = kv.get("Deadline", "")
        drive = kv.get("Drive Link", "")
        form = kv.get("Form Link", "")

        story_info: dict[str, Any] = {}
        if kv.get("Story Media"):
            story_info["media"] = kv.get("Story Media")
        if kv.get("Story Tags"):
            story_info["tags"] = [t.strip() for t in kv["Story Tags"].split(",") if t.strip()]
        if kv.get("Story Keep Hours"):
            try:
                story_info["keep_hours"] = int(kv["Story Keep Hours"])
            except ValueError:
                story_info["keep_hours"] = 24

        feed_info: dict[str, Any] = {}
        if kv.get("Feed Media"):
            feed_info["media"] = kv.get("Feed Media")
        if kv.get("Feed Tags"):
            feed_info["tags"] = [t.strip() for t in kv["Feed Tags"].split(",") if t.strip()]
        if caption:
            feed_info["caption"] = caption

        raw = dict(kv)
        raw["id"] = task_id
        raw["task_type"] = task_type
        raw["sheet"] = sheet
        raw["jadwal_post"] = jadwal
        raw["deadline"] = deadline
        raw["platforms"] = platforms
        raw["drive_link"] = drive
        raw["form_link"] = form
        raw["tags"] = tags
        raw["hashtags"] = hashtags
        raw["caption"] = caption
        raw["story"] = story_info
        raw["feed"] = feed_info

        links_match = re.search(r"- \*\*Links\*\*:\s*\n((?:\s+-\s+[^\n]+\n?)+)", sec)
        links_dict: dict[str, str] = {}
        if links_match:
            for line in links_match.group(1).splitlines():
                m = re.match(r"\s+-\s+([a-zA-Z0-9_-]+):\s*(.*)", line)
                if m:
                    links_dict[m.group(1).lower().strip()] = m.group(2).strip()
        raw["links"] = links_dict

        tipe_val = [t.strip() for t in kv.get("Tipe Validasi", "").split(",") if t.strip()]
        raw["tipe_validasi"] = tipe_val

        custom_match = re.search(r"- \*\*Custom Requirements\*\*:\s*\n((?:\s+-\s+[^\n]+\n?)+)", sec)
        custom_dict: dict[str, Any] = {}
        if custom_match:
            for line in custom_match.group(1).splitlines():
                m = re.match(r"\s+-\s+([a-zA-Z0-9_-]+):\s*(.*)", line)
                if m:
                    custom_dict[m.group(1).lower().strip()] = m.group(2).strip()
        raw["custom_requirements"] = custom_dict

        entries[task_id] = TaskCatalogEntry(
            id=task_id,
            task_type=task_type,
            sheet=sheet,
            jadwal_post=jadwal,
            deadline=deadline,
            platforms=platforms,
            drive_link=drive,
            form_link=form,
            tags=tags,
            hashtags=hashtags,
            caption=caption,
            story=story_info,
            feed=feed_info,
            raw=raw,
        )
    return entries


def load_task_catalog(force: bool = False) -> dict[str, TaskCatalogEntry]:
    """Load or return cached task definitions from markdown files."""
    global _TASK_CATALOG_CACHE, _TASK_CATALOG_MTIME
    pp_file = config.RULES_DIR / "pp_tasks.md"
    bz_file = config.RULES_DIR / "buzzer_tasks.md"
    current_mtime = max(
        pp_file.stat().st_mtime if pp_file.exists() else 0,
        bz_file.stat().st_mtime if bz_file.exists() else 0,
    )
    if not force and _TASK_CATALOG_CACHE is not None and current_mtime <= _TASK_CATALOG_MTIME:
        return _TASK_CATALOG_CACHE

    catalog: dict[str, TaskCatalogEntry] = {}
    catalog.update(_parse_task_markdown(pp_file, "PP"))
    catalog.update(_parse_task_markdown(bz_file, "BUZZER"))
    _TASK_CATALOG_CACHE = catalog
    _TASK_CATALOG_MTIME = current_mtime
    return catalog


def get_task(task_id: str) -> TaskCatalogEntry | None:
    return load_task_catalog().get(task_id.upper().strip())


def all_tasks() -> list[TaskCatalogEntry]:
    return list(load_task_catalog().values())


def all_task_ids(task_type: str | None = None) -> list[str]:
    tasks = all_tasks()
    if task_type:
        tasks = [t for t in tasks if t.task_type.upper() == task_type.upper()]
    return [t.id for t in tasks]


def task_sheet_map() -> dict[str, str]:
    """Mapping of normalized(sheet_name) -> task_id."""
    index: dict[str, str] = {}
    for task in all_tasks():
        if task.sheet:
            index[normalize_sheet_name(task.sheet)] = task.id
    return index


def get_check_meta(check_id: str) -> dict[str, Any]:
    """Retrieve metadata for a check code (C1-C10, B1-B7) from checks.md."""
    checks_file = config.RULES_DIR / "checks.md"
    if not checks_file.exists():
        return {}
    content = checks_file.read_text(encoding="utf-8")
    pattern = rf"###\s+{re.escape(check_id.upper())}\b.*?(?=(?:\n###|\Z))"
    match = re.search(pattern, content, re.DOTALL | re.IGNORECASE)
    if not match:
        return {}
    block = match.group(0)
    kv = dict(re.findall(r"- \*\*([^*]+)\*\*:\s*(.*)", block))
    header = block.splitlines()[0].replace("###", "").strip()
    name = kv.get("Nama", header.split("—")[-1].strip() if "—" in header else header)
    denda = 0
    try:
        denda = int(kv.get("Denda", 0))
    except ValueError:
        pass
    return {
        "id": check_id.upper(),
        "nama": name,
        "syarat": kv.get("Syarat", ""),
        "denda": denda,
        "tipe_error": kv.get("Tipe Error", ""),
        "cara_validasi": kv.get("Cara Validasi", ""),
        "catatan": kv.get("Catatan", ""),
        "sumber": kv.get("Sumber", "checks.md"),
    }


def load_fines_policy() -> dict[str, Any]:
    """Parse fines.md for tariff amounts and bank account details."""
    path = config.RULES_DIR / "fines.md"
    if not path.exists():
        return {
            "tidak_post": 20000, "per_kesalahan_post": 5000,
            "bank": "BCA", "nomor": "4731763318", "atas_nama": "Bertrand Immanuel Wijaya",
            "deadline": "Setiap hari Sabtu pukul 23.59 WIB",
        }
    text = path.read_text(encoding="utf-8")
    def _amount(name: str, default: int) -> int:
        m = re.search(rf"\*\*({name})\*\*:\s*Rp\s*([\d.,]+)", text, re.IGNORECASE)
        if m:
            clean = re.sub(r"[\s.,]", "", m.group(2))
            return int(clean) if clean.isdigit() else default
        return default

    bank_m = re.search(r"\*\*Bank\*\*:\s*([^\n]+)", text)
    no_m = re.search(r"\*\*Nomor Rekening\*\*:\s*([^\n]+)", text)
    an_m = re.search(r"\*\*Atas Nama\*\*:\s*([^\n]+)", text)
    dl_m = re.search(r"\*\*Deadline Pembayaran\*\*:\s*([^\n]+)", text)

    return {
        "tidak_post": _amount("Tidak Post", 20000),
        "terlambat_post": _amount("Terlambat Post", 20000),
        "terlalu_cepat_post": _amount("Terlalu Cepat Post", 20000),
        "tidak_kirim_bukti": _amount("Tidak Kirim Bukti", 20000),
        "per_kesalahan_post": _amount("Per Kesalahan Post", 5000),
        "terlambat_bayar_denda_per_hari": _amount("Terlambat Bayar Denda Per Hari", 5000),
        "tidak_mengerjakan_buzzer": _amount("Tidak Mengerjakan Buzzer", 20000),
        "buzzer_tidak_sesuai_ketentuan": _amount("Buzzer Tidak Sesuai Ketentuan", 20000),
        "bank": bank_m.group(1).strip() if bank_m else "BCA",
        "nomor": no_m.group(1).strip() if no_m else "4731763318",
        "atas_nama": an_m.group(1).strip() if an_m else "Bertrand Immanuel Wijaya",
        "deadline": dl_m.group(1).strip() if dl_m else "Setiap hari Sabtu pukul 23.59 WIB",
    }


def load_angel_policy() -> dict[str, Any]:
    """Parse angel_card.md for price and conditions."""
    path = config.RULES_DIR / "angel_card.md"
    if not path.exists():
        return {"harga": 100000, "masa_berlaku": "Sampai kepanitiaan Horizon selesai"}
    text = path.read_text(encoding="utf-8")
    harga_m = re.search(r"\*\*Harga\*\*:\s*Rp\s*([\d.,]+)", text, re.IGNORECASE)
    harga = 100000
    if harga_m:
        clean = re.sub(r"[\s.,]", "", harga_m.group(1))
        harga = int(clean) if clean.isdigit() else 100000
    return {"harga": harga, "masa_berlaku": "Sampai kepanitiaan Horizon selesai"}


# ── Markdown Document Ingestion for Vector Store ───────────────────────────
def _rules_file_docs(file_path: Path) -> list[dict[str, Any]]:
    if not file_path.exists():
        return []
    content = file_path.read_text(encoding="utf-8")
    # Split by level-2 or level-3 headers
    sections = re.split(r"\n(?=#{2,3}\s+)", content)
    docs: list[dict[str, Any]] = []
    base_name = file_path.name
    for idx, sec in enumerate(sections):
        sec_text = sec.strip()
        if not sec_text:
            continue
        first_line = sec_text.splitlines()[0]
        header = re.sub(r"^#{1,3}\s*", "", first_line).strip()
        doc_id = f"rule:{file_path.stem}:{idx}"
        extra: dict[str, Any] = {"title": header}
        m_task = re.search(r"\b(PP-\d{1,2}|BZ-\d{1,2})\b", header, re.IGNORECASE)
        if m_task:
            extra["task_id"] = m_task.group(1).upper()
            doc_id = f"task:{m_task.group(1).upper()}"
        m_check = re.search(r"\b(C(?:10|[1-9])|B[1-7])\b", header, re.IGNORECASE)
        if m_check:
            check_id = m_check.group(1).upper()
            extra["check_id"] = check_id
            doc_id = f"check:{check_id}"
            m_amt = re.search(r"- \*\*Denda\*\*:\s*(\d+)", sec_text)
            if m_amt:
                amt = int(m_amt.group(1))
                sec_text = f"{sec_text}\nDenda nominal: {format_rupiah(amt)}."

        docs.append(
            _doc(
                sec_text,
                "rule",
                doc_id,
                base_name,
                **extra,
            )
        )
    return docs


def _info_docs() -> list[dict[str, Any]]:
    return [
        _doc(
            "Chatbot Mamazon tidak bisa memvalidasi, mengirim blast denda, atau mengubah aturan secara mandiri. "
            "Semua tindakan mutasi membutuhkan konfirmasi tombol eksplisit di Discord dan approval koor untuk blast denda.",
            "info",
            "info:chat_boundaries",
            "agent_rules.md",
        )
    ]


def _command_docs() -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    docs.append(
        _doc(
            "Command /validate. Menjalankan validasi terhadap satu sheet tugas. "
            "Role yang boleh menjalankan: validator SPADE, koor, atau BPH. "
            "Satu tugas hanya boleh berjalan satu proses pada satu waktu.",
            "command",
            "command:validate",
            "discord_bot",
        )
    )
    docs.append(
        _doc(
            "Command /blast. Menghasilkan draft pengumuman denda untuk sheet yang sudah divalidasi. "
            "Draft masuk ke channel #blast-drafts untuk di-review oleh koor sebelum dikirim.",
            "command",
            "command:blast",
            "discord_bot",
        )
    )
    docs.append(
        _doc(
            "Command /denda-list. Menampilkan daftar denda panitia. "
            "Panitia biasa hanya dapat melihat denda miliknya sendiri.",
            "command",
            "command:denda-list",
            "discord_bot",
        )
    )
    docs.append(
        _doc(
            "Command /help. Menampilkan panduan Mamazon dan daftar command sesuai role pengguna.",
            "command",
            "command:help",
            "discord_bot",
        )
    )
    return docs


def _blast_context_docs() -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    path = config.BLAST_CONTEXT_PATH
    if not path.exists():
        return docs
    try:
        from ..utils import truncate
        data = json.loads(path.read_text(encoding="utf-8"))
        contexts = data.get("contexts", {}) if isinstance(data, dict) else {}
        for task_id, item in contexts.items():
            if not isinstance(item, dict):
                continue
            tags = item.get("tags") or []
            hashtags = item.get("hashtags") or []
            search_tags = item.get("search_tags") or []
            deadline = item.get("deadline") or "-"
            short_desc = item.get("short_description") or ""
            notes = item.get("notes") or []
            caption = truncate(item.get("caption_referensi") or "", 200)

            text_parts = [
                f"Tugas {task_id}.",
                f"Kode Buzzer/Paid Promote: {task_id}.",
                f"Deskripsi: {short_desc}.",
                f"Deadline: {deadline}.",
            ]
            if tags:
                text_parts.append(f"Tag wajib: {', '.join(tags)}.")
            if hashtags:
                text_parts.append(f"Hashtag: {', '.join(hashtags)}.")
            if search_tags:
                text_parts.append(f"Tags pencarian: {', '.join(search_tags)}.")
            if item.get("drive_folder"):
                text_parts.append(f"Drive: {item['drive_folder']}.")
            if item.get("form_link"):
                text_parts.append(f"Form: {item['form_link']}.")
            if notes:
                text_parts.append(f"Catatan: {'; '.join(notes)}.")
            if caption:
                text_parts.append(f"Caption referensi: {caption}.")

            docs.append(
                _doc(
                    " ".join(text_parts),
                    "rule",
                    f"blast:{task_id}",
                    "blast_context.json",
                    task_id=task_id,
                    search_tags=search_tags,
                )
            )
    except Exception as exc:
        logger.warning("Could not read blast contexts for knowledge corpus: %s", exc)
    return docs


def index_blast_task(parsed: Any, store: ZvecVectorStore | None = None) -> str | None:
    """Index or update a single blast task document into the vector store."""
    try:
        from ..utils import fmt_dt, truncate

        target_store = store or ensure_store()
        if target_store is None:
            logger.warning(
                "Vector store unavailable for indexing blast task %s",
                getattr(parsed, "task_id", ""),
            )
            return None

        task_id = str(getattr(parsed, "task_id", "") or "UNKNOWN")
        task_type = str(getattr(parsed, "task_type", "") or "PP")
        short_desc = str(getattr(parsed, "short_description", "") or "")
        deadline_val = getattr(parsed, "deadline", None)
        deadline_str = fmt_dt(deadline_val) if deadline_val else "Belum ditentukan"
        tags = list(getattr(parsed, "tags", []) or [])
        hashtags = list(getattr(parsed, "hashtags", []) or [])
        search_tags = list(getattr(parsed, "search_tags", []) or [])
        drive = str(getattr(parsed, "drive_folder", "") or "")
        form = str(getattr(parsed, "form_link", "") or "")
        notes = list(getattr(parsed, "notes", []) or [])
        caption = truncate(str(getattr(parsed, "caption_referensi", "") or ""), 200)

        text_parts = [
            f"Tugas {task_id} ({task_type}).",
            f"Kode Buzzer/Paid Promote: {task_id}.",
            f"Deskripsi: {short_desc}.",
            f"Deadline: {deadline_str}.",
        ]
        if tags:
            text_parts.append(f"Tag wajib: {', '.join(tags)}.")
        if hashtags:
            text_parts.append(f"Hashtag: {', '.join(hashtags)}.")
        if search_tags:
            text_parts.append(f"Tags pencarian: {', '.join(search_tags)}.")
        if drive:
            text_parts.append(f"Drive: {drive}.")
        if form:
            text_parts.append(f"Form: {form}.")
        if notes:
            text_parts.append(f"Catatan: {'; '.join(notes)}.")
        if caption:
            text_parts.append(f"Caption referensi: {caption}.")

        doc_id = f"blast:{task_id}"
        meta = {
            "id": doc_id,
            "kind": "rule",
            "source": "blast_reader",
            "task_id": task_id,
            "task_type": task_type,
            "search_tags": search_tags,
            "short_description": short_desc,
        }
        target_store.add_texts([" ".join(text_parts)], metadatas=[meta], ids=[doc_id])
        logger.info("Indexed blast task %s into vector store", task_id)
        return doc_id
    except Exception as exc:
        logger.warning("Failed to index blast task into vector store: %s", exc)
        return None


def build_corpus() -> list[dict[str, Any]]:
    """Assemble all corpus documents from Markdown rule files."""
    docs: list[dict[str, Any]] = []
    rules_dir = config.RULES_DIR
    if rules_dir.exists():
        for md_file in sorted(rules_dir.glob("*.md")):
            docs.extend(_rules_file_docs(md_file))
    elif config.AGENT_RULES_PATH.exists():
        docs.extend(_rules_file_docs(config.AGENT_RULES_PATH))

    docs.extend(_command_docs())
    docs.extend(_info_docs())
    docs.extend(_blast_context_docs())
    return docs


# ── fingerprint & rebuild ──────────────────────────────────────────────────
def source_fingerprint() -> str:
    """Hash of all markdown files and configs to detect corpus staleness."""
    hasher = hashlib.sha256()
    rules_dir = config.RULES_DIR
    if rules_dir.exists():
        for md_file in sorted(rules_dir.glob("*.md")):
            try:
                hasher.update(md_file.name.encode("utf-8"))
                hasher.update(md_file.read_bytes())
            except OSError:
                pass
    elif config.AGENT_RULES_PATH.exists():
        try:
            hasher.update(config.AGENT_RULES_PATH.read_bytes())
        except OSError:
            pass

    blast_mtime = ""
    if config.BLAST_CONTEXT_PATH.exists():
        try:
            blast_mtime = str(config.BLAST_CONTEXT_PATH.stat().st_mtime)
        except OSError:
            pass

    payload = {
        "permissions": {key: list(value) for key, value in config.PERMISSIONS.items()},
        "divisions": config.DIVISI_LIST,
        "embedding_model": config.EMBEDDING_MODEL,
        "blast_mtime": blast_mtime,
        "version": 4,
    }
    hasher.update(json.dumps(payload, sort_keys=True).encode("utf-8"))
    return hasher.hexdigest()


def _fingerprint_path() -> Path:
    return config.VECTOR_DB_DIR / f"{config.VECTOR_DB_COLLECTION}.fingerprint"


def is_fresh() -> bool:
    if not _store_path().exists():
        return False
    path = _fingerprint_path()
    if not path.exists():
        return False
    try:
        return path.read_text(encoding="utf-8").strip() == source_fingerprint()
    except OSError:
        return False


def _store_path() -> Path:
    return config.VECTOR_DB_DIR / config.VECTOR_DB_COLLECTION


def build_store(
    embeddings: LocalEmbeddings | None = None,
    force: bool = False,
) -> ZvecVectorStore:
    """Create or refresh the zvec collection and index the corpus."""
    provider = embeddings or build_embeddings()
    path = _store_path()
    fingerprint = source_fingerprint()
    if not force and is_fresh() and path.exists():
        store = ZvecVectorStore(
            path, config.VECTOR_DB_COLLECTION, provider, config.EMBEDDING_DIM
        )
        if store.count() > 0:
            logger.info("Knowledge base is up to date (%d docs)", store.count())
            return store
    docs = build_corpus()
    logger.info("Indexing %d knowledge documents into %s", len(docs), path)
    store = ZvecVectorStore.create(
        path, config.VECTOR_DB_COLLECTION, provider, config.EMBEDDING_DIM
    )
    store.add_documents(docs)
    try:
        config.VECTOR_DB_DIR.mkdir(parents=True, exist_ok=True)
        _fingerprint_path().write_text(fingerprint, encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write knowledge fingerprint: %s", exc)
    return store


def open_store(embeddings: LocalEmbeddings | None = None) -> ZvecVectorStore:
    provider = embeddings or build_embeddings()
    return ZvecVectorStore(
        _store_path(), config.VECTOR_DB_COLLECTION, provider, config.EMBEDDING_DIM
    )


def ensure_store(embeddings: LocalEmbeddings | None = None) -> ZvecVectorStore | None:
    provider = embeddings or build_embeddings()
    try:
        if not _store_path().exists() or (config.KB_REBUILD_ON_CHANGE and not is_fresh()):
            return build_store(provider)
        return open_store(provider)
    except Exception as exc:
        logger.warning("Knowledge base unavailable: %s", exc)
        return None


def search(
    query: str,
    store: ZvecVectorStore | None = None,
    top_k: int | None = None,
    min_score: float | None = None,
    kinds: Sequence[str] | None = None,
) -> list[tuple[Any, float]]:
    if store is None:
        store = ensure_store()
    if store is None:
        return []
    limit = top_k or config.CHAT_AGENT_TOP_K
    floor = config.CHAT_AGENT_MIN_SCORE if min_score is None else min_score
    filter_expr = None
    if kinds:
        joined = ", ".join(f"'{kind}'" for kind in kinds)
        filter_expr = f"kind in ({joined})"
    try:
        results = store.similarity_search_with_score(query, k=limit, filter=filter_expr)
    except Exception as exc:
        logger.warning("Knowledge search failed: %s", exc)
        return []
    return [(doc, score) for doc, score in results if score >= floor]
