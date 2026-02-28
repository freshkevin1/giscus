import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY")
    if not SECRET_KEY:
        raise RuntimeError("SECRET_KEY environment variable must be set")
    _db_dir = os.path.join(BASE_DIR, "instance")
    os.makedirs(_db_dir, exist_ok=True)
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        f"sqlite:///{os.path.join(_db_dir, 'dashboard.db')}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Scraping schedule (KST = UTC+9): 06:00, 12:00, 18:00, 24:00 KST = 21:00, 03:00, 09:00, 15:00 UTC
    SCRAPE_HOUR_UTC = "21,3,9,15"
    SCRAPE_MINUTE = 0

    # Article retention limit
    MAX_ARTICLES = 500

    # Target URL
    MK_URL = "https://www.mk.co.kr/today-paper"

    # Anthropic API
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

    # Google Sheets (Contact List)
    GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    GOOGLE_SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME", "Contact List")

    # Encryption keys (Contact List PII)
    FERNET_KEY = os.environ.get("FERNET_KEY", "")
    HMAC_KEY = os.environ.get("HMAC_KEY", "")

    VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
    VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
    VAPID_CLAIM_EMAIL = os.environ.get("VAPID_CLAIM_EMAIL", "")
