import os
import re as _re
import base64 as _base64

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


def _pem_to_vapid_b64url(key):
    """PEM 포맷 VAPID private key를 py_vapid 호환 base64url(raw 32-byte)로 변환."""
    if not key:
        return ""

    key = key.strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in {"'", '"'}:
        key = key[1:-1].strip()

    if '-----' not in key:
        return key  # 이미 base64url 또는 DER(base64url) 문자열이면 그대로

    # 이스케이프된 \n 복원
    key = key.replace('\\r', '').replace('\\n', '\n').strip()
    # 줄바꿈 없으면 PEM 구조 재조립 (한 줄로 저장된 경우) — 원본 헤더/푸터 타입 보존
    if '\n' not in key:
        stripped = key.replace(' ', '')
        m = _re.search(
            r'(-----BEGIN [^-]+-----)'
            r'([A-Za-z0-9+/=]+)'
            r'(-----END [^-]+-----)',
            stripped
        )
        if m:
            header, body, footer = m.group(1), m.group(2), m.group(3)
            lines = '\n'.join(body[i:i+64] for i in range(0, len(body), 64))
            key = f"{header}\n{lines}\n{footer}\n"
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        ec_key = load_pem_private_key(key.encode(), password=None)
        # py_vapid는 P-256 개인키를 32-byte raw(base64url) 형태로 기대한다.
        private_value = ec_key.private_numbers().private_value
        raw = private_value.to_bytes(32, "big")
        return _base64.urlsafe_b64encode(raw).decode().rstrip('=')
    except Exception:
        return ""


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
    MAX_ARTICLES = 2000
    MAX_ARTICLE_AGE_DAYS = 60  # 2개월

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
