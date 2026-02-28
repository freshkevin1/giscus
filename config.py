import os
import re as _re
import base64 as _base64

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


def _pem_to_vapid_b64url(key):
    """PEM 포맷 VAPID private key → pywebpush용 base64url raw key 변환."""
    if not key or '-----' not in key:
        return key  # 이미 base64url이면 그대로
    # 이스케이프된 \n 복원
    key = key.replace('\\n', '\n')
    # 줄바꿈이 없으면 PEM 구조 재조립 (한 줄로 저장된 경우)
    if '\n' not in key:
        m = _re.search(r'-----BEGIN [^-]+----- *([A-Za-z0-9+/=]+) *-----END', key)
        if m:
            b64 = m.group(1)
            lines = '\n'.join(b64[i:i+64] for i in range(0, len(b64), 64))
            key = f"-----BEGIN EC PRIVATE KEY-----\n{lines}\n-----END EC PRIVATE KEY-----\n"
    try:
        from cryptography.hazmat.primitives.serialization import (
            load_pem_private_key, Encoding, PrivateFormat, NoEncryption)
        ec_key = load_pem_private_key(key.encode(), password=None)
        raw = ec_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        return _base64.urlsafe_b64encode(raw).decode().rstrip('=')
    except Exception:
        return key  # 변환 실패 시 원본 반환 (기존 동작 유지)


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

    VAPID_PRIVATE_KEY = _pem_to_vapid_b64url(os.environ.get("VAPID_PRIVATE_KEY", ""))
    VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
    VAPID_CLAIM_EMAIL = os.environ.get("VAPID_CLAIM_EMAIL", "")
