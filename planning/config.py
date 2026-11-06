"""
Mamazon Spade Office Agent - Configuration
===========================================
Central config for paths, API keys, and constants.
"""

import os
from pathlib import Path

# ─── Paths ──────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent  # agent/
WORKSPACE_ROOT = PROJECT_ROOT.parent                     # Planning & Buat...

# Data files
RULEBASE_PATH = WORKSPACE_ROOT / "_refs" / "rulebase.json"
DUMMY_DATA_PATH = WORKSPACE_ROOT / "_refs" / "dummy_dataset_anonymized.xlsx"
ORIGINAL_DATA_PATH = WORKSPACE_ROOT / "_refs" / "zip_extracted" / "My dummy data without phone number njir.xlsx"

# Output
OUTPUT_DIR = PROJECT_ROOT / "output"
VERIFICATION_REPORT_PATH = OUTPUT_DIR / "verification_report.csv"
VIOLATIONS_LOG_PATH = OUTPUT_DIR / "violations_log.csv"
BLAST_DRAFT_PATH = OUTPUT_DIR / "blast_draft.md"
AUDIT_LOG_PATH = OUTPUT_DIR / "audit_log.jsonl"
BLAST_CONTEXT_PATH = OUTPUT_DIR / "blast_context.json"
PROCESSED_MESSAGES_PATH = OUTPUT_DIR / "processed_messages.json"

# Temp (images, hashes)
TMP_DIR = PROJECT_ROOT / "tmp"
IMAGE_TMP_DIR = TMP_DIR / "images"
HASH_CACHE_PATH = TMP_DIR / "hashes.json"
IMAGE_TTL_HOURS = 24  # Auto-delete images after 24 hours

# Config
CONFIG_DIR = PROJECT_ROOT / "config"
SERVICE_ACCOUNT_PATH = CONFIG_DIR / "service_account.json"  # Google service account

# ─── API Keys (from environment variables) ─────────────────────────────────
# Tencent Cloud Hunyuan
TENCENT_SECRET_ID = os.environ.get("TENCENT_SECRET_ID", "")
TENCENT_SECRET_KEY = os.environ.get("TENCENT_SECRET_KEY", "")
TENCENT_REGION = os.environ.get("TENCENT_REGION", "ap-jakarta")

# Discord Bot
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "")  # Server ID
DISCORD_BLAST_TUGAS_CHANNEL_ID = os.environ.get("DISCORD_BLAST_TUGAS_CHANNEL_ID", "")
DISCORD_DENDA_CHANNEL_ID = os.environ.get("DISCORD_DENDA_CHANNEL_ID", "")
DISCORD_BLAST_DRAFTS_CHANNEL_ID = os.environ.get("DISCORD_BLAST_DRAFTS_CHANNEL_ID", "")
DISCORD_AUDIT_LOG_CHANNEL_ID = os.environ.get("DISCORD_AUDIT_LOG_CHANNEL_ID", "")
DISCORD_PP_VALIDATION_CHANNEL_ID = os.environ.get("DISCORD_PP_VALIDATION_CHANNEL_ID", "")
DISCORD_BUZZER_VALIDATION_CHANNEL_ID = os.environ.get("DISCORD_BUZZER_VALIDATION_CHANNEL_ID", "")

# Google (service account file handles auth, no env var needed)

# ─── Constants ──────────────────────────────────────────────────────────────
MIN_IG_FOLLOWERS = 300
STORY_KEEP_HOURS = 24
DEFAULT_DENDA_TIDAK_POST = 20000
DEFAULT_DENDA_PER_KESALAHAN = 5000
DEFAULT_DENDA_TIDAK_KIRIM_BUKTI = 20000
ANGEL_CARD_HARGA = 100000

# Divisi canonical (8 divisi Mamazon)
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

# Sheet name mapping (Excel sheet -> rulebase task ID)
SHEET_TO_TASK = {
    "PP - 5 Oktober": "PP-06",          # Jumat 9 Okt blast
    "PP 07 Oktober": None,               # no matching blast in rulebase
    "PP Story 09 Oktober": "PP-07",      # Story only, Jumat 9 Okt
    "PP Bundle 9 Oktober": "PP-06",      # Story + Feed, Jumat 9 Okt
    "PP Bundle 26 Oktober": "PP-05",     # Feed only, Senin 26 Okt
    "PP Feed 26 Oktober": "PP-03",       # Feed with custom caption, Rabu 28 Okt
    "PP 29 Oktober": "PP-01",            # Story + Feed, Kamis 29 Okt
    "buzzer-6 Oktober": "BZ-07",         # Form buzzer, 6-7 Okt
    "Buzzer Gmaps 09 Oktober": "BZ-02",  # Gmaps ulasan, multi-day
    "Buzzer IG 09 Oktober": "BZ-06",     # IG like+komen, 9-10 Okt
    "Buzzer vote 26 Oktober": "BZ-03",   # Vote campaign, 26-27 Okt
    "Buzzer vote 26 Oktober (joccely": "BZ-03",  # duplicate sheet
    "CANCEL": None,
}

# ─── Validation ─────────────────────────────────────────────────────────────
def validate_config():
    """Check that all required files and keys are present."""
    issues = []

    if not RULEBASE_PATH.exists():
        issues.append(f"Rulebase not found: {RULEBASE_PATH}")
    if not DUMMY_DATA_PATH.exists():
        issues.append(f"Dummy dataset not found: {DUMMY_DATA_PATH}")
    if not TENCENT_SECRET_ID:
        issues.append("TENCENT_SECRET_ID env var not set (use mock vision for dev)")
    if not TENCENT_SECRET_KEY:
        issues.append("TENCENT_SECRET_KEY env var not set (use mock vision for dev)")
    if not DISCORD_BOT_TOKEN:
        issues.append("DISCORD_BOT_TOKEN env var not set (required for Discord bot)")
    if not DISCORD_GUILD_ID:
        issues.append("DISCORD_GUILD_ID env var not set (required for Discord bot)")

    return issues


if __name__ == "__main__":
    print("=== Config Check ===")
    print(f"Project root: {PROJECT_ROOT}")
    print(f"Rulebase: {RULEBASE_PATH} ({'OK' if RULEBASE_PATH.exists() else 'MISSING'})")
    print(f"Dummy data: {DUMMY_DATA_PATH} ({'OK' if DUMMY_DATA_PATH.exists() else 'MISSING'})")
    print(f"Output dir: {OUTPUT_DIR} ({'OK' if OUTPUT_DIR.exists() else 'MISSING'})")
    print(f"Tencent Secret ID: {'SET' if TENCENT_SECRET_ID else 'NOT SET'}")
    print(f"Tencent Secret Key: {'SET' if TENCENT_SECRET_KEY else 'NOT SET'}")
    print(f"Discord Token: {'SET' if DISCORD_BOT_TOKEN else 'NOT SET'}")
    print(f"Discord Guild ID: {'SET' if DISCORD_GUILD_ID else 'NOT SET'}")
    print()
    issues = validate_config()
    if issues:
        print("ISSUES:")
        for i in issues:
            print(f"  - {i}")
    else:
        print("All checks passed!")
