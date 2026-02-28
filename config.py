import os
import re as _re
import base64 as _base64

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


def _pem_to_vapid_b64url(key):
    """PEM 포맷 VAPID private key → pywebpush용 base64url raw key 변환.
    변환 실패 시 multi-line PEM으로 정규화하여 반환 (pywebpush가 파싱 가능한 포맷).
    """
    if not key or '-----' not in key:
        return key  # 이미 base64url이면 그대로
    # 이스케이프된 \n 복원
    key = key.replace('\\n', '\n')
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
        from cryptography.hazmat.primitives.serialization import (
            load_pem_private_key, Encoding, PrivateFormat, NoEncryption)
        ec_key = load_pem_private_key(key.encode(), password=None)
        raw = ec_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        return _base64.urlsafe_b64encode(raw).decode().rstrip('=')
    except Exception:
        return key  # 정규화된 multi-line PEM 반환 → pywebpush가 직접 파싱


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
