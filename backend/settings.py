"""
settings.py — all configuration comes from environment variables.

NO secret is ever hard-coded or sent to the browser. In Azure, the app settings
below are populated from Key Vault via Key Vault references, so the running app
reads them as ordinary env vars while the actual secret stays in Key Vault.
"""
import os
from pathlib import Path

# Bump this on every deploy so you can confirm the live app is the new code:
# open  https://<your-app>/api/version  in a browser and check it matches.
VERSION = "2026.07.22"
VERSION_NOTE = "Soft Butter state + full PROCEDURE capture + typo tolerance"

BASE_DIR = Path(__file__).resolve().parent
CORE_DIR = BASE_DIR / "core"
FRONTEND_DIR = BASE_DIR.parent / "frontend"
MASTER_TEMPLATE = BASE_DIR / "templates_store" / "master.xlsx"

# Writable data area (jobs, generated files, shared DB, audit). On Azure use a
# mounted share or /home (persistent) — set DATA_DIR accordingly.
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR.parent / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"          # temporary — deleted after extraction
OUTPUT_DIR = DATA_DIR / "generated"        # generated .xlsx — TTL cleanup
SHARED_DB = DATA_DIR / "ingredient_db.json"
AUDIT_LOG = DATA_DIR / "audit.jsonl"
DB_HISTORY = DATA_DIR / "db_history.jsonl"

for d in (DATA_DIR, UPLOAD_DIR, OUTPUT_DIR):
    d.mkdir(parents=True, exist_ok=True)

# --- privacy / retention ---
DELETE_SOURCE_AFTER_PROCESSING = os.getenv("DELETE_SOURCE_AFTER_PROCESSING", "true").lower() == "true"
GENERATED_RETENTION_MINUTES = int(os.getenv("GENERATED_RETENTION_MINUTES", "240"))
MAX_BATCH_FILES = int(os.getenv("MAX_BATCH_FILES", "20"))
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "25"))

# --- extraction backend ---
# If Azure OpenAI is configured, the LLM extractor is used (handles messy layouts,
# OCR text, and novel-term translation). Otherwise the local heuristic extractor
# runs (good for clean table-based docx/xlsx) so the app is demonstrable with no key.
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_KEY = os.getenv("AZURE_OPENAI_KEY", "")            # secret → Key Vault
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
AZURE_OCR_ENDPOINT = os.getenv("AZURE_OCR_ENDPOINT", "")        # Document Intelligence
AZURE_OCR_KEY = os.getenv("AZURE_OCR_KEY", "")                  # secret → Key Vault

def llm_enabled() -> bool:
    return bool(AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_KEY)

# --- Google Gemini (FREE tier) — reads photos/scans, any Excel layout, translates.
# Get a free key at https://aistudio.google.com  →  set GEMINI_API_KEY on Render.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

def gemini_enabled() -> bool:
    return bool(GEMINI_API_KEY)

# Run an AI validation pass (categorize, translate, flag errors/duplicates) on
# EVERY recipe when Gemini is configured. Failures never block the conversion.
AI_REVIEW_ENABLED = os.getenv("AI_REVIEW_ENABLED", "true").lower() == "true"

# --- auth / roles (self-managed — NO corporate directory / Entra required) ---
# AUTH_MODE: 'code' = one shared access code (default) | 'users' = per-user accounts
#            | 'dev' = open, local testing only.
AUTH_MODE = os.getenv("AUTH_MODE", "code")
# 'users' mode: APP_USERS = "email|pbkdf2hash|admin, email2|hash2"  (generate hashes
# with make_user.py; the optional 3rd field 'admin' marks a reviewer).
APP_USERS = os.getenv("APP_USERS", "")
# 'code' mode: one shared access code, read from a private env var. The value is
# never hard-coded and never logged. On Render set SHARED_ACCESS_CODE as a secret.
ACCESS_CODE = os.getenv("SHARED_ACCESS_CODE", "") or os.getenv("ACCESS_CODE", "")
# Session signing + lifetime + cookie flags.
SESSION_SECRET = os.getenv("SESSION_SECRET", "")               # set a long random value in the host
SESSION_HOURS = int(os.getenv("SESSION_HOURS", "12"))
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() == "true"   # false only for local http
# 'dev' mode identity.
DEV_USER = os.getenv("DEV_USER", "dev@local")
ADMIN_USERS = {u.strip().lower() for u in os.getenv("ADMIN_USERS", "dev@local").split(",") if u.strip()}
