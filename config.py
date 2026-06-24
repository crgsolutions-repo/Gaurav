import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def load_env_file(path=BASE_DIR / ".env"):
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip().lstrip("\ufeff")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_env_file()


class Config:
    FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY")
    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY")
    SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
    GEMINI_EMBEDDING_MODEL = os.getenv("GEMINI_EMBEDDING_MODEL", "models/gemini-embedding-001")
    GEMINI_EMBEDDING_DIMENSION = int(os.getenv("GEMINI_EMBEDDING_DIMENSION", "768"))
    GEMINI_TIMEOUT_SECONDS = float(os.getenv("GEMINI_TIMEOUT_SECONDS", "30"))
    GEMINI_PLANNER_ENABLED = os.getenv("GEMINI_PLANNER_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    RAG_ENABLED = os.getenv("RAG_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    POLICY_SOURCE_DIR = os.getenv("POLICY_SOURCE_DIR", str(BASE_DIR / "policies" / "source"))
    HR_CONTACT_EMAIL = os.getenv("HR_CONTACT_EMAIL")
    HR_CONTACT_CHANNEL = os.getenv("HR_CONTACT_CHANNEL")
    TESSERACT_EXE_PATH = os.getenv(
        "TESSERACT_EXE_PATH",
        str(BASE_DIR / "Tesseract-OCR" / "tesseract.exe"),
    )
    TESSDATA_PREFIX = os.getenv(
        "TESSDATA_PREFIX",
        str(BASE_DIR / "Tesseract-OCR" / "tessdata"),
    )
    EXPENSE_UPLOAD_DIR = os.getenv(
        "EXPENSE_UPLOAD_DIR",
        str(BASE_DIR / "uploads" / "expenses"),
    )


def require_config(*names):
    missing = [name for name in names if not getattr(Config, name, None)]
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(
            f"Missing required environment configuration: {joined}. "
            "Create a .env file from .env.example or set these variables."
        )
