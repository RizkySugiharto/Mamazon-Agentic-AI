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
import re
import shutil
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

# Data & Rules files
DATA_DIR = Path(os.environ.get("DATA_DIR", PROJECT_ROOT / "data"))
RULES_DIR = Path(os.environ.get("RULES_DIR", DATA_DIR / "rules"))
AGENT_RULES_PATH = Path(
    os.environ.get("AGENT_RULES_PATH", RULES_DIR / "agent_rules.md")
)
# Compatibility alias
RULEBASE_PATH = RULES_DIR
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

# Logs (daily date-divided)
LOGS_DIR = Path(os.environ.get("LOGS_DIR", PROJECT_ROOT / "logs"))

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


# ─── 9Router (the one and only AI backend) ─────────────────────────────────
# 9Router is a self-hosted OpenAI-compatible gateway. It fronts several
# upstream providers and routes between them, either directly by model id or
# through a named "combo" (an ordered fallback chain).
#
#   base url : the gateway's OpenAI-compatible endpoint, usually
#              http://localhost:20128/v1
#   api key  : the gateway key (Bearer), created in the 9Router dashboard
#   model    : either an exact model id ("ag/gemini-3.8-flash-low") or the
#              name of a combo ("mamazon") that 9Router resolves itself
#
# Nothing is guessed: the model is sent exactly as configured.
NINEROUTER_BASE_URL = _env(
    "NINEROUTER_BASE_URL", "http://localhost:20128/v1"
).rstrip("/")
NINEROUTER_API_KEY = _env("NINEROUTER_API_KEY")
NINEROUTER_MODEL = _env("NINEROUTER_MODEL", "mamazon")
NINEROUTER_TIMEOUT = max(1, _env_int("NINEROUTER_TIMEOUT", 120))
NINEROUTER_MAX_IMAGES = max(1, _env_int("NINEROUTER_MAX_IMAGES", 8))
NINEROUTER_MAX_TOKENS = max(256, _env_int("NINEROUTER_MAX_TOKENS", 3000))
# Ask for a JSON object; retried without it when a model rejects the parameter.
NINEROUTER_JSON_MODE = _env_bool("NINEROUTER_JSON_MODE", True)
# low|medium|high. Set for reasoning models so they do not spend the whole
# token budget thinking and return empty content.
NINEROUTER_REASONING_EFFORT = _env("NINEROUTER_REASONING_EFFORT", "").lower()
# Optional attribution headers (only affect gateway-side leaderboards).
NINEROUTER_SITE_URL = _env("NINEROUTER_SITE_URL", "https://github.com/mamazon/spade-agent")
NINEROUTER_APP_NAME = _env("NINEROUTER_APP_NAME", "Mamazon Spade Agent")
# The gateway is usually local; some deployments still sit behind a proxy that
# blocks the default urllib agent.
NINEROUTER_USER_AGENT = _env(
    "NINEROUTER_USER_AGENT", "MamazonSpadeAgent/1.0 (+9Router)"
)
# Audio transcription model routed through 9Router (Groq Whisper)
NINEROUTER_WHISPER_MODEL = _env("NINEROUTER_WHISPER_MODEL", "groq/whisper-large-v3-turbo")
NINEROUTER_WHISPER_TIMEOUT = max(5, _env_int("NINEROUTER_WHISPER_TIMEOUT", 60))
# Feature toggle: accept and parse voice notes / audio files in Discord #ai-chat
VOICE_DETECTION_ENABLED = _env_bool("VOICE_DETECTION_ENABLED", True)

# ─── Bounded agent tools (no arbitrary code execution) ─────────────────────
CHAT_TOOLS_ENABLED = _env_bool("CHAT_TOOLS_ENABLED", True)
CHAT_INCLUDE_TEMPLATES = _env_bool("CHAT_INCLUDE_TEMPLATES", False)
CHAT_MEMORY_ENABLED = _env_bool("CHAT_MEMORY_ENABLED", True)
CHAT_MEMORY_PATH = Path(_env("CHAT_MEMORY_PATH", str(OUTPUT_DIR / "chat_memory.json")))
CHAT_MEMORY_MAX_ITEMS = max(1, _env_int("CHAT_MEMORY_MAX_ITEMS", 20))
WEB_SEARCH_URL = _env("WEB_SEARCH_URL")
WEB_SEARCH_API_KEY = _env("WEB_SEARCH_API_KEY")
WEB_SEARCH_TIMEOUT = max(1, _env_int("WEB_SEARCH_TIMEOUT", 10))
WEB_SEARCH_MAX_RESULTS = max(1, _env_int("WEB_SEARCH_MAX_RESULTS", 3))

# ─── Agentic chat (LangChain + LangGraph + local zvec) ─────────────────────
# The deterministic chat_agent stays the fallback; this is the conversational
# path that retrieves from a local vector store and calls read-only tools.
CHAT_AGENT_ENABLED = _env_bool("CHAT_AGENT_ENABLED", True)
# Max graph steps (retrieval + tool calls) per question.
CHAT_AGENT_MAX_STEPS = max(1, _env_int("CHAT_AGENT_MAX_STEPS", 6))
# Documents pulled from the vector store per retrieval.
CHAT_AGENT_TOP_K = max(1, _env_int("CHAT_AGENT_TOP_K", 5))
# Retrieval score floor; below this the context is treated as irrelevant.
# Measured on the multilingual model: on-topic rules score ~0.70-0.80,
# loosely related ~0.50, off-topic noise ~0.37.
CHAT_AGENT_MIN_SCORE = _env_float("CHAT_AGENT_MIN_SCORE", 0.45)
# Corpus chunks kept in the prompt (chars) so we never blow the context.
CHAT_AGENT_MAX_CONTEXT_CHARS = max(1000, _env_int("CHAT_AGENT_MAX_CONTEXT_CHARS", 6000))

# Local vector store (zvec) — embedded, no server, no Docker.
VECTOR_DB_DIR = Path(_env("VECTOR_DB_DIR", str(PROJECT_ROOT / "data" / "vectorstore")))
VECTOR_DB_COLLECTION = _env("VECTOR_DB_COLLECTION", "mamazon_knowledge")
# Local embedding model. multilingual-e5 handles Indonesian + English.
EMBEDDING_MODEL = _env(
    "EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)
EMBEDDING_DIM = max(1, _env_int("EMBEDDING_DIM", 384))
# Rebuild the corpus when the rulebase or the source fingerprint changes.
KB_REBUILD_ON_CHANGE = _env_bool("KB_REBUILD_ON_CHANGE", True)
KB_COLLECTION_NAME = _env("KB_COLLECTION_NAME", "mamazon_knowledge")

# ─── One provider for both screenshot reading and #ai-chat ─────────────────
CHAT_ENABLED = _env_bool("CHAT_ENABLED", True)
CHAT_TIMEOUT = max(1, _env_int("CHAT_TIMEOUT", 30))
CHAT_MAX_TOKENS = max(256, _env_int("CHAT_MAX_TOKENS", 2000))
CHAT_MODEL = NINEROUTER_MODEL  # read-only compatibility alias

# 9Router is the only backend. "mock" stays available as an offline mode for
# tests and demos, but it is never a real provider and is never auto-selected.
AI_PROVIDER = "9router"
AI_PROVIDERS = {"9router", "mock"}
# Legacy aliases are mirrors, not separate selectors.
VISION_PROVIDER = AI_PROVIDER
AI_AGENT_PROVIDER = AI_PROVIDER
CHAT_PROVIDER = AI_PROVIDER
# Offline mode is opt-in only, so a missing key never silently fakes a verdict.
USE_MOCK_VISION = _env_bool("AI_OFFLINE", False)
# Provider names that used to exist. They are rejected loudly instead of
# silently falling back to another backend.
REMOVED_AI_PROVIDERS = {
    "openrouter", "or", "openai", "opencode", "opencode-cli", "open-code",
    "opencode-api", "opencode_api", "opencodeapi", "opencode-http",
    "opencode-server", "opencode-console", "open-code-api", "local",
    "tencent", "tencent-cloud", "hunyuan", "agy", "agy-cli",
    "antigravity", "antigravity-cli", "local-agy", "off", "none", "disabled",
}


def effective_ninerouter_base_url() -> str:
    """Accept either an API base or a pasted full /chat/completions URL."""
    base = NINEROUTER_BASE_URL.rstrip("/")
    suffix = "/chat/completions"
    return base[: -len(suffix)] if base.endswith(suffix) else base


def ai_provider_warnings() -> list[str]:
    """Non-blocking, secret-free operational warnings."""
    warnings: list[str] = []
    if not NINEROUTER_API_KEY:
        warnings.append(
            "NINEROUTER_API_KEY is empty. The 9Router gateway requires its own "
            "Bearer key (create one in the 9Router dashboard). Requests will 401."
        )
    if not NINEROUTER_BASE_URL.startswith("http"):
        warnings.append(f"NINEROUTER_BASE_URL looks wrong: {NINEROUTER_BASE_URL}")
    if not NINEROUTER_MODEL:
        warnings.append(
            "NINEROUTER_MODEL is empty. Use an exact model id or a 9Router "
            "combo name (default: mamazon)."
        )
    return warnings


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
    "roster-set-discord": (ROLE_BPH, ROLE_KOOR),
    "roster-remove-discord": (ROLE_BPH, ROLE_KOOR),
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

    if not RULES_DIR.exists():
        issues.append(f"Rules directory not found: {RULES_DIR}")
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
    issues.extend(validate_ai_config())
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


def active_ai_model() -> str:
    """Exact model used by both Vision and chat; never substituted."""
    return NINEROUTER_MODEL


def active_chat_model() -> str:
    """Compatibility alias for callers written before unification."""
    return active_ai_model()


def validate_ai_config() -> list[str]:
    """Validate the single provider used by screenshot reading and text chat."""
    issues: list[str] = []
    if USE_MOCK_VISION:
        # Offline mode: no gateway required, and nothing is faked silently
        # because the operator asked for it explicitly.
        return issues
    if not NINEROUTER_API_KEY:
        issues.append(
            "NINEROUTER_API_KEY is empty — set the 9Router gateway key "
            "(dashboard → API keys)"
        )
    if not NINEROUTER_MODEL:
        issues.append(
            "NINEROUTER_MODEL is empty — set an exact model id or a 9Router "
            "combo name (default: mamazon)"
        )
    base = effective_ninerouter_base_url()
    if not base.startswith("http"):
        issues.append(f"NINEROUTER_BASE_URL looks wrong: {base}")
    return issues


def validate_chat_config() -> list[str]:
    """Compatibility alias: chat and Vision now share one validation."""
    return validate_ai_config()


def ai_provider_status() -> dict[str, object]:
    """Safe, secret-free summary for CLI, /help and startup diagnostics."""
    return {
        "provider": AI_PROVIDER,
        "model": active_ai_model(),
        "configured_model": NINEROUTER_MODEL,
        "chat_enabled": CHAT_ENABLED,
        "enabled": bool(CHAT_ENABLED and not USE_MOCK_VISION),
        "tools_enabled": CHAT_TOOLS_ENABLED,
        "include_templates": CHAT_INCLUDE_TEMPLATES,
        "memory_enabled": CHAT_MEMORY_ENABLED,
        "rules_dir": str(RULES_DIR),
        "web_search_configured": bool(WEB_SEARCH_URL),
        "api_base_url": effective_ninerouter_base_url(),
        "api_user_agent": NINEROUTER_USER_AGENT,
        "api_key_set": bool(NINEROUTER_API_KEY),
        "whisper_model": NINEROUTER_WHISPER_MODEL,
        "voice_detection_enabled": VOICE_DETECTION_ENABLED,
        "offline": USE_MOCK_VISION,
        "issues": validate_ai_config(),
        "warnings": ai_provider_warnings(),
    }



def chat_provider_status() -> dict[str, object]:
    """Compatibility alias for code written before Vision/chat unification."""
    return ai_provider_status()


if __name__ == "__main__":
    print("=== Config Check ===")
    print(f"Project root     : {PROJECT_ROOT}")
    print(f"Rulebase         : {RULEBASE_PATH} ({'OK' if RULEBASE_PATH.exists() else 'MISSING'})")
    print(f"Dataset          : {DUMMY_DATA_PATH} ({'OK' if DUMMY_DATA_PATH.exists() else 'MISSING'})")
    print(f"Output dir       : {OUTPUT_DIR} ({'OK' if OUTPUT_DIR.exists() else 'MISSING'})")
    print(f"AI provider      : {AI_PROVIDER} (Vision + chat)")
    print(f"AI model         : {active_ai_model() or '-'}")
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
