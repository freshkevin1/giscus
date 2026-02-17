"""Data validation for contact fields."""

import re
from datetime import datetime

CONTACT_PRIORITIES = [
    "1A-인생관계",
    "1M-Mentor, 은인",
    "1F-Family",
    "2A-비즈니스 우선순위",
    "2C-비즈니스",
    "3A-인적 우선순위",
    "3C-인적 네트워킹",
    "4A-Passive",
    "5A-Inactive",
]

FOLLOWUP_PRIORITIES = ["FU0", "FU1", "FU3", "FU5", "FU9"]


def validate_date(value):
    """Validate YYYY-MM-DD format. Returns (True, parsed_date) or (False, error_msg)."""
    if not value:
        return True, None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
        return True, parsed
    except ValueError:
        return False, f"날짜 형식이 올바르지 않습니다: '{value}' (YYYY-MM-DD)"


def validate_email(value):
    """Basic email format check."""
    if not value:
        return True, None
    pattern = r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
    if re.match(pattern, value):
        return True, None
    return False, f"이메일 형식이 올바르지 않습니다: '{value}'"


def validate_contact_priority(value):
    if not value:
        return True, None
    if value in CONTACT_PRIORITIES:
        return True, None
    return False, f"유효하지 않은 Contact Priority: '{value}'. 유효값: {', '.join(CONTACT_PRIORITIES)}"


def validate_followup_priority(value):
    if not value:
        return True, None
    if value in FOLLOWUP_PRIORITIES:
        return True, None
    return False, f"유효하지 않은 Follow-up Priority: '{value}'. 유효값: {', '.join(FOLLOWUP_PRIORITIES)}"


def validate_tag(value, valid_tags):
    """Check tag against the pre-defined list."""
    if not value:
        return True, None
    tags = [t.strip() for t in value.split(",")]
    invalid = [t for t in tags if t and t not in valid_tags]
    if invalid:
        return False, f"미정의 태그: {', '.join(invalid)}. 유효 태그: {', '.join(valid_tags)}"
    return True, None


def validate_contact(data, valid_tags=None):
    """Validate a full contact dict. Returns (is_valid, errors_list)."""
    errors = []

    if not data.get("name"):
        errors.append("이름은 필수 항목입니다.")

    ok, err = validate_contact_priority(data.get("contact_priority", ""))
    if not ok:
        errors.append(err)

    ok, err = validate_followup_priority(data.get("follow_up_priority", ""))
    if not ok:
        errors.append(err)

    ok, err = validate_date(data.get("follow_up_date", ""))
    if not ok:
        errors.append(err)

    ok, err = validate_date(data.get("last_contact", ""))
    if not ok:
        errors.append(err)

    ok, err = validate_email(data.get("email", ""))
    if not ok:
        errors.append(err)

    if valid_tags is not None:
        ok, err = validate_tag(data.get("tag", ""), valid_tags)
        if not ok:
            errors.append(err)

    return len(errors) == 0, errors


def validate_update_fields(fields, valid_tags=None):
    """Validate a partial update dict. Returns (is_valid, errors_list)."""
    errors = []

    if "contact_priority" in fields:
        ok, err = validate_contact_priority(fields["contact_priority"])
        if not ok:
            errors.append(err)

    if "follow_up_priority" in fields:
        ok, err = validate_followup_priority(fields["follow_up_priority"])
        if not ok:
            errors.append(err)

    if "follow_up_date" in fields:
        ok, err = validate_date(fields["follow_up_date"])
        if not ok:
            errors.append(err)

    if "last_contact" in fields:
        ok, err = validate_date(fields["last_contact"])
        if not ok:
            errors.append(err)

    if "email" in fields:
        ok, err = validate_email(fields["email"])
        if not ok:
            errors.append(err)

    if "tag" in fields and valid_tags is not None:
        ok, err = validate_tag(fields["tag"], valid_tags)
        if not ok:
            errors.append(err)

    return len(errors) == 0, errors
