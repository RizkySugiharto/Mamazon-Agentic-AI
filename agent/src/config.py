"""
Mamazon Spade Office Agent - Configuration
==========================================
Central config for paths, API keys, adapter flags and constants.

Everything that can vary between machines (secrets, channel ids, adapter
selection) is read from the environment. Nothing secret is hardcoded.

Load order for .env files:
    1. agent/.env                (preferred, git-ignored)
    2. <workspace>/.env          (fallback, shared with other tooling)
    3. real environment variables always win over .env values.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

try:  # python-dotenv is optional; the agent must still boot without it
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - exercised only on bare installs
    load_dotenv = None


# ─── Paths ──────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent      # agent/
WORKSPACE_ROOT = PROJECT_ROOT.parent                       # repo root

if load_dotenv is not None:
    load_dotenv(PROJECT_ROOT / ".env")
    load_dotenv(WORKSPACE_ROOT / ".env")


REFS_DIR = Path(os.environ.get("REFS_DIR", WORKSPACE_ROOT / "_refs"))

# Data files
RULEBASE_PATH = Path(os.environ.get("RULEBASE_PATH", REFS_DIR / "rulebase.json"))
DUMMY_DATA_PATH = Path(
    os.environ.get("DUMMY_DATA_PATH", REFS_DIR / "dummy_dataset_anonymized.xlsx")
)

# Output
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", PROJECT_ROOT / "output"))
VERIFICATION_REPORT_PATH = OUTPUT_DIR / "verification_report.csv"
VIOLATIONS_LOG_PATH = OUTPUT_DIR / "violations_log.csv"
BLAST_DRAFT_PATH = OUTPUT_DIR / "blast_draft.md"
AUDIT_LOG_PATH = OUTPUT_DIR / "audit_log.jsonl"
BLAST_CONTEXT_PATH = OUTPUT_DIR / "blast_context.json"
PROCESSED_MESSAGES_PATH = OUTPUT_DIR / "processed_messages.json"
BLAST_STORE_DIR = OUTPUT_DIR / "blasts"

# Temp (images, hashes)
TMP_DIR = Path(os.environ.get("TMP_DIR", PROJECT_ROOT / "tmp"))
IMAGE_TMP_DIR = TMP_DIR / "images"
HASH_CACHE_PATH = TMP_DIR / "hashes.json"

# Config
CONFIG_DIR = PROJECT_ROOT / "config"
SERVICE_ACCOUNT_PATH = Path(
    os.environ.get("GOOGLE_SERVICE_ACCOUNT_PATH", CONFIG_DIR / "service_account.json")
)


# ─── Helpers ────────────────────────────────────────────────────────────────
def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return value.strip() if value is not None else default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


# ─── Adapter selection ──────────────────────────────────────────────────────
# Vision: "auto" picks tencent when keys exist, otherwise mock.
TENCENT_SECRET_ID = _env("TENCENT_SECRET_ID")
TENCENT_SECRET_KEY = _env("TENCENT_SECRET_KEY")
TENCENT_REGION = _env("TENCENT_REGION", "ap-jakarta")
TENCENT_VISION_MODEL = _env("TENCENT_VISION_MODEL", "hunyuan-vision")
TENCENT_VISION_ENDPOINT = _env(
    "TENCENT_VISION_ENDPOINT", "hunyuan.tencentcloudapi.com"
)

# ─── OpenRouter (Vision backend) ────────────────────────────────────────────
# https://openrouter.ai/api/v1 — OpenAI-compatible, Bearer auth.
# Model ids are "provider/model" (openai/gpt-4o-mini,
# google/gemini-2.0-flash-001) and MUST accept image input.
OPENROUTER_BASE_URL = _env(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
).rstrip("/")
OPENROUTER_API_KEY = _env("OPENROUTER_API_KEY")
OPENROUTER_MODEL = _env("OPENROUTER_MODEL")
OPENROUTER_TIMEOUT = _env_int("OPENROUTER_TIMEOUT", 90)
OPENROUTER_MAX_IMAGES = _env_int("OPENROUTER_MAX_IMAGES", 8)
OPENROUTER_MAX_TOKENS = _env_int("OPENROUTER_MAX_TOKENS", 3000)
# Ask for a JSON object; retried without it when a model rejects the parameter.
OPENROUTER_JSON_MODE = _env_bool("OPENROUTER_JSON_MODE", True)
# low|medium|high. Set for reasoning models so they do not spend the whole
# token budget thinking and return empty content.
OPENROUTER_REASONING_EFFORT = _env("OPENROUTER_REASONING_EFFORT", "").lower()
# Optional attribution headers (only affect OpenRouter leaderboards).
OPENROUTER_SITE_URL = _env("OPENROUTER_SITE_URL", "https://github.com/mamazon/spade-agent")
OPENROUTER_APP_NAME = _env("OPENROUTER_APP_NAME", "Mamazon Spade Agent")

# Text chat reuses the configured OpenRouter model/key; Vision remains separate.
CHAT_ENABLED = _env_bool("CHAT_ENABLED", True)
CHAT_MODEL = _env("CHAT_MODEL", OPENROUTER_MODEL)
CHAT_TIMEOUT = max(1, _env_int("CHAT_TIMEOUT", 30))
CHAT_MAX_TOKENS = max(256, _env_int("CHAT_MAX_TOKENS", 2000))

_VISION_PROVIDER = _env("VISION_PROVIDER", "auto").lower()
if _VISION_PROVIDER == "auto":
    # Precedence: OpenRouter (explicitly configured) -> Tencent -> mock.
    if OPENROUTER_API_KEY or OPENROUTER_MODEL:
        VISION_PROVIDER = "openrouter"
    elif TENCENT_SECRET_ID and TENCENT_SECRET_KEY:
        VISION_PROVIDER = "tencent"
    else:
        VISION_PROVIDER = "mock"
else:
    VISION_PROVIDER = {
        "or": "openrouter",
        "openrouter.ai": "openrouter",
        "openai": "openrouter",  # OpenRouter speaks the OpenAI wire format
    }.get(_VISION_PROVIDER, _VISION_PROVIDER)

# Kept for backwards compatibility with the planning documents.
USE_MOCK_VISION = VISION_PROVIDER == "mock"

# Drive: "auto" picks google when a service account file exists, else mock.
_DRIVE_PROVIDER = _env("DRIVE_PROVIDER", "auto").lower()
if _DRIVE_PROVIDER == "auto":
    DRIVE_PROVIDER = "google" if SERVICE_ACCOUNT_PATH.exists() else "mock"
else:
    DRIVE_PROVIDER = _DRIVE_PROVIDER

USE_MOCK_DRIVE = DRIVE_PROVIDER == "mock"

# Spreadsheet source: local xlsx (default) or a live Google Sheet.
SHEET_SOURCE = _env("SHEET_SOURCE", "xlsx").lower()

# Live Google Sheets (SHEET_SOURCE=gsheets). Accepts a bare id or a full URL.
GOOGLE_SPREADSHEET_ID = _env("GOOGLE_SPREADSHEET_ID")
# Service account used for both Sheets and Drive (same JSON key file).
GOOGLE_SHEETS_CREDENTIALS = Path(
    _env("GOOGLE_SHEETS_CREDENTIALS", str(SERVICE_ACCOUNT_PATH))
)


# ─── Discord ────────────────────────────────────────────────────────────────
DISCORD_BOT_TOKEN = _env("DISCORD_BOT_TOKEN")
DISCORD_GUILD_ID = _env("DISCORD_GUILD_ID")
# Coordinator-maintained bindings: immutable Discord user ID -> exact roster name.
# A mutable display name is not safe authorization for private fine records.
try:
    _member_map = json.loads(_env("DISCORD_MEMBER_MAP", "{}"))
    DISCORD_MEMBER_MAP = {str(key): value for key, value in _member_map.items()
                          if str(key).isdigit() and isinstance(value, str)} if isinstance(_member_map, dict) else {}
except (ValueError, TypeError):
    DISCORD_MEMBER_MAP = {}

DISCORD_BLAST_TUGAS_CHANNEL_ID = _env("DISCORD_BLAST_TUGAS_CHANNEL_ID")
DISCORD_DENDA_CHANNEL_ID = _env("DISCORD_DENDA_CHANNEL_ID")
DISCORD_BLAST_DRAFTS_CHANNEL_ID = _env("DISCORD_BLAST_DRAFTS_CHANNEL_ID")
DISCORD_AUDIT_LOG_CHANNEL_ID = _env("DISCORD_AUDIT_LOG_CHANNEL_ID")
DISCORD_PP_VALIDATION_CHANNEL_ID = _env("DISCORD_PP_VALIDATION_CHANNEL_ID")
DISCORD_BUZZER_VALIDATION_CHANNEL_ID = _env(
    "DISCORD_BUZZER_VALIDATION_CHANNEL_ID"
)
DISCORD_EVIDENCE_CHANNEL_ID = _env("DISCORD_EVIDENCE_CHANNEL_ID")
DISCORD_BUKTI_TAMBAHAN_CHANNEL_ID = _env("DISCORD_BUKTI_TAMBAHAN_CHANNEL_ID")
DISCORD_AI_CHAT_CHANNEL_ID = _env("DISCORD_AI_CHAT_CHANNEL_ID")
DISCORD_KETENTUAN_CHANNEL_ID = _env("DISCORD_KETENTUAN_CHANNEL_ID")

CHANNEL_ENV_MAP = {
    "blast_tugas": ("DISCORD_BLAST_TUGAS_CHANNEL_ID", DISCORD_BLAST_TUGAS_CHANNEL_ID),
    "denda": ("DISCORD_DENDA_CHANNEL_ID", DISCORD_DENDA_CHANNEL_ID),
    "blast_drafts": ("DISCORD_BLAST_DRAFTS_CHANNEL_ID", DISCORD_BLAST_DRAFTS_CHANNEL_ID),
    "audit_log": ("DISCORD_AUDIT_LOG_CHANNEL_ID", DISCORD_AUDIT_LOG_CHANNEL_ID),
    "pp_validation": ("DISCORD_PP_VALIDATION_CHANNEL_ID", DISCORD_PP_VALIDATION_CHANNEL_ID),
    "buzzer_validation": (
        "DISCORD_BUZZER_VALIDATION_CHANNEL_ID",
        DISCORD_BUZZER_VALIDATION_CHANNEL_ID,
    ),
    "evidence": ("DISCORD_EVIDENCE_CHANNEL_ID", DISCORD_EVIDENCE_CHANNEL_ID),
    "bukti_tambahan": (
        "DISCORD_BUKTI_TAMBAHAN_CHANNEL_ID",
        DISCORD_BUKTI_TAMBAHAN_CHANNEL_ID,
    ),
    "ai_chat": ("DISCORD_AI_CHAT_CHANNEL_ID", DISCORD_AI_CHAT_CHANNEL_ID),
    "ketentuan": ("DISCORD_KETENTUAN_CHANNEL_ID", DISCORD_KETENTUAN_CHANNEL_ID),
}


# ─── Roles & permissions ────────────────────────────────────────────────────
# Role names are matched case-insensitively. BPH and server admins bypass all
# checks (see discord_bot.has_permission).
ROLE_BPH = _env("ROLE_BPH", "BPH")
ROLE_KOOR = _env("ROLE_KOOR", "koor-spade")
ROLE_SPADE = _env("ROLE_SPADE", "spade")
ROLE_PANITIA = _env("ROLE_PANITIA", "panitia")

ALL_ROLES = (ROLE_BPH, ROLE_KOOR, ROLE_SPADE, ROLE_PANITIA)

# Command -> roles allowed to run it. Mirrors the workflow permission matrix.
PERMISSIONS = {
    "validate": (ROLE_BPH, ROLE_KOOR, ROLE_SPADE),
    "check-angel": (ROLE_BPH, ROLE_KOOR, ROLE_SPADE),
    "add-angel": (ROLE_BPH, ROLE_KOOR),
    "remove-angel": (ROLE_BPH, ROLE_KOOR),
    "blast": (ROLE_BPH, ROLE_KOOR),
    "hold-blast": (ROLE_BPH, ROLE_KOOR),
    "release-blast": (ROLE_BPH, ROLE_KOOR),
    "list-active": (ROLE_BPH, ROLE_KOOR, ROLE_SPADE),
    "resubmit-status": (ROLE_BPH, ROLE_KOOR, ROLE_SPADE),
    "audit-log": (ROLE_BPH, ROLE_KOOR),
    "denda-list": (ROLE_BPH, ROLE_KOOR, ROLE_SPADE, ROLE_PANITIA),
    "ketentuan-pp": (ROLE_BPH, ROLE_KOOR, ROLE_SPADE, ROLE_PANITIA),
    "ketentuan-buzzer": (ROLE_BPH, ROLE_KOOR, ROLE_SPADE, ROLE_PANITIA),
    "ketentuan-angel-card": (ROLE_BPH, ROLE_KOOR, ROLE_SPADE, ROLE_PANITIA),
    "scan-blast": (ROLE_BPH, ROLE_KOOR),
    "list-sheets": (ROLE_BPH, ROLE_KOOR, ROLE_SPADE),
}


# ─── Constants ──────────────────────────────────────────────────────────────
MIN_IG_FOLLOWERS = _env_int("MIN_IG_FOLLOWERS", 300)
STORY_KEEP_HOURS = 24
IMAGE_TTL_HOURS = _env_int("IMAGE_TTL_HOURS", 24)
IMAGE_MAX_BYTES = _env_int("IMAGE_MAX_BYTES", 2 * 1024 * 1024)
IMAGE_TARGET_BYTES = _env_int("IMAGE_TARGET_BYTES", 500 * 1024)
DRIVE_DOWNLOAD_BATCH = _env_int("DRIVE_DOWNLOAD_BATCH", 100)
DRIVE_MAX_RETRIES = _env_int("DRIVE_MAX_RETRIES", 3)
BLAST_SCAN_DAYS = _env_int("BLAST_SCAN_DAYS", 7)
BLAST_SCAN_MAX_MESSAGES = _env_int("BLAST_SCAN_MAX_MESSAGES", 1000)
RECURRING_THRESHOLD = _env_int("RECURRING_THRESHOLD", 3)
VALIDATION_MAX_ROWS = _env_int("VALIDATION_MAX_ROWS", 0)  # 0 = no limit

# Date guardrail: post earlier than jadwal by up to this many minutes is OK.
POST_EARLY_TOLERANCE_MINUTES = _env_int("POST_EARLY_TOLERANCE_MINUTES", 60)

TIMEZONE = _env("TIMEZONE", "Asia/Jakarta")
CURRENCY = "Rp"

DIVISI_LIST = [
    "Fletch (Perlengkapan)",
    "Dupin (Visual)",
    "Columbo (Dokumentasi)",
    "Marple (Dekorasi)",
    "Marlowe (Public Relations)",
    "Kindaichi (Keamanan)",
    "Conan (Acara)",
    "Spade (Fresh Money)",
]

# Excel sheet -> rulebase task id.
# NOTE: this is a fallback only. rulebase.json's per-task "sheet" field wins
# when present, because the rulebase is the single source of truth. Known
# conflicts are reported by `python -m src.main lint` rather than patched.
SHEET_TO_TASK = {
    "PP - 5 Oktober": "PP-06",
    "PP 07 Oktober": None,
    "PP Story 09 Oktober": "PP-07",
    "PP Bundle 9 Oktober": "PP-06",
    "PP Bundle 26 Oktober": "PP-05",
    "PP Feed 26 Oktober": "PP-03",
    "PP 29 Oktober": "PP-01",
    "buzzer-6 Oktober": "BZ-07",
    "Buzzer Gmaps 09 Oktober": "BZ-02",
    "Buzzer IG 09 Oktober": "BZ-06",
    "Buzzer vote 26 Oktober": "BZ-03",
    "Buzzer vote 26 Oktober (joccely": "BZ-03",
}

# Sheets that are never submission sheets.
NON_SUBMISSION_SHEETS = {
    "roster",
    "roster panitia",
    "angel card registry",
    "bukti tambahan",
    "evidence vault",
    "cancel",
    "_info",
    "info",
}

# Tab names vary between workbooks ("Roster" vs "Roster Panitia", "_INFO"),
# so registry detection is keyword-based rather than an exact match.
REGISTRY_KEYWORDS = (
    "roster",
    "angel card",
    "bukti tambahan",
    "evidence",
    "info",
    "cancel",
    "panduan",
    "readme",
)


def is_registry_sheet(name: str | None) -> bool:
    """True for roster/registry/meta tabs that must not be validated as tasks."""
    if not name:
        return True
    lowered = " ".join(str(name).lower().replace("_", " ").split())
    if lowered in NON_SUBMISSION_SHEETS:
        return True
    return any(keyword in lowered for keyword in REGISTRY_KEYWORDS)

ANGEL_CARD_SHEET = "Angel Card Registry"
ROSTER_SHEET = "Roster"
BUKTI_TAMBAHAN_SHEET = "Bukti Tambahan"
EVIDENCE_VAULT_SHEET = "Evidence Vault"


# ─── Self-check ─────────────────────────────────────────────────────────────
def module_available(name: str) -> bool:
    """True when an optional dependency is importable (no import side effects)."""
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def validate_config(require_discord: bool = False) -> list[str]:
    """Return a list of human-readable configuration problems."""
    issues: list[str] = []

    if not RULEBASE_PATH.exists():
        issues.append(f"Rulebase not found: {RULEBASE_PATH}")
    if SHEET_SOURCE in {"gsheets", "google", "google-sheets", "sheets"}:
        if not GOOGLE_SPREADSHEET_ID:
            issues.append(
                "SHEET_SOURCE=gsheets but GOOGLE_SPREADSHEET_ID is empty — paste the "
                "spreadsheet id or URL"
            )
        if not module_available("googleapiclient"):
            issues.append(
                "SHEET_SOURCE=gsheets but google-api-python-client is not installed "
                "(pip install -r agent/requirements.txt)"
            )
        if not GOOGLE_SHEETS_CREDENTIALS.exists():
            issues.append(
                f"Service account key not found: {GOOGLE_SHEETS_CREDENTIALS}"
            )
    elif not DUMMY_DATA_PATH.exists():
        issues.append(
            f"Dataset not found: {DUMMY_DATA_PATH} "
            "(run `python _refs/build_dummy.py` to generate fixtures)"
        )
    if VISION_PROVIDER == "tencent":
        if not (TENCENT_SECRET_ID and TENCENT_SECRET_KEY):
            issues.append("VISION_PROVIDER=tencent but Tencent keys are missing")
        if not module_available("tencentcloud"):
            issues.append(
                "VISION_PROVIDER=tencent but the SDK is not installed "
                "(pip install tencentcloud-sdk-python)"
            )
    if VISION_PROVIDER == "openrouter":
        if not OPENROUTER_API_KEY:
            issues.append(
                "VISION_PROVIDER=openrouter but OPENROUTER_API_KEY is empty — create a "
                "key at https://openrouter.ai/keys"
            )
        if not OPENROUTER_MODEL:
            issues.append(
                "VISION_PROVIDER=openrouter but OPENROUTER_MODEL is empty — pick a "
                "vision model (python -m src.main vision-status --models 20)"
            )
        if not OPENROUTER_BASE_URL.startswith("http"):
            issues.append(
                f"OPENROUTER_BASE_URL looks wrong: {OPENROUTER_BASE_URL} "
                "(expected https://openrouter.ai/api/v1)"
            )
    if DRIVE_PROVIDER == "google":
        if not SERVICE_ACCOUNT_PATH.exists():
            issues.append(
                f"DRIVE_PROVIDER=google but {SERVICE_ACCOUNT_PATH} is missing"
            )
        if not module_available("googleapiclient"):
            issues.append(
                "DRIVE_PROVIDER=google but google-api-python-client is not "
                "installed (pip install google-api-python-client google-auth)"
            )
    if SHEET_SOURCE not in {"xlsx", "gsheets", "google", "google-sheets", "sheets"}:
        issues.append(
            f"SHEET_SOURCE={SHEET_SOURCE} tidak dikenal — pakai xlsx atau gsheets"
        )
    if require_discord:
        if not DISCORD_BOT_TOKEN:
            issues.append("DISCORD_BOT_TOKEN is not set")
        if not DISCORD_GUILD_ID:
            issues.append("DISCORD_GUILD_ID is not set")
        missing_channels = [
            env_name
            for env_name, value in CHANNEL_ENV_MAP.values()
            if not value
        ]
        if missing_channels:
            issues.append(
                "Channel ids not set: " + ", ".join(sorted(missing_channels))
            )
    return issues


if __name__ == "__main__":
    print("=== Config Check ===")
    print(f"Project root     : {PROJECT_ROOT}")
    print(f"Rulebase         : {RULEBASE_PATH} ({'OK' if RULEBASE_PATH.exists() else 'MISSING'})")
    print(f"Dataset          : {DUMMY_DATA_PATH} ({'OK' if DUMMY_DATA_PATH.exists() else 'MISSING'})")
    print(f"Output dir       : {OUTPUT_DIR} ({'OK' if OUTPUT_DIR.exists() else 'MISSING'})")
    print(f"Vision provider  : {VISION_PROVIDER}")
    print(f"Drive provider   : {DRIVE_PROVIDER}")
    print(f"Discord token    : {'SET' if DISCORD_BOT_TOKEN else 'NOT SET'}")
    print(f"Discord guild    : {'SET' if DISCORD_GUILD_ID else 'NOT SET'}")
    print()
    problems = validate_config(require_discord=False)
    if problems:
        print("ISSUES:")
        for problem in problems:
            print(f"  - {problem}")
    else:
        print("All checks passed.")
