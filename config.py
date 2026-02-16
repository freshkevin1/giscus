import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "exec-dashboard-secret-key-change-in-prod")
    _db_dir = os.path.join(BASE_DIR, "instance")
    os.makedirs(_db_dir, exist_ok=True)
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        f"sqlite:///{os.path.join(_db_dir, 'dashboard.db')}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Scraping schedule (KST = UTC+9, so 6:00 KST = 21:00 UTC previous day)
    SCRAPE_HOUR_UTC = 21
    SCRAPE_MINUTE = 0

    # Article retention limit
    MAX_ARTICLES = 500

    # Target URL
    MK_URL = "https://www.mk.co.kr/today-paper"

    # Anthropic API
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
