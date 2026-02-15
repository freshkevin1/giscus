import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "exec-dashboard-secret-key-change-in-prod")
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{os.path.join(BASE_DIR, 'instance', 'dashboard.db')}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Scraping schedule (KST = UTC+9, so 6:00 KST = 21:00 UTC previous day)
    SCRAPE_HOUR_UTC = 21
    SCRAPE_MINUTE = 0

    # Article retention limit
    MAX_ARTICLES = 500

    # Target URL
    MK_URL = "https://www.mk.co.kr/today-paper"
