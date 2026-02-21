"""Contact priority scoring and follow-up auto-upgrade."""

from datetime import datetime, date

BP_WEIGHTS = {
    "0-Critical": 100,
    "1-High": 75,
    "2-Medium": 50,
    "3-Low": 25,
}

CP_WEIGHTS = {
    "1A-인생관계": 100,
    "1M-Mentor, 은인": 100,
    "1F-Family": 70,
    "2A-비즈니스 우선순위": 80,
    "2C-비즈니스": 50,
    "3A-인적 우선순위": 70,
    "3C-인적 네트워킹": 40,
    "4A-Passive": 20,
    "5A-Inactive": 0,
}

FU_WEIGHTS = {
    "FU0": 100,
    "FU1": 80,
    "FU3": 50,
    "FU5": 20,
    "FU9": 0,
}

# FU upgrade path (FU9 excluded from auto-upgrade)
FU_UPGRADE = {
    "FU5": "FU3",
    "FU3": "FU1",
    "FU1": "FU0",
}

# Overdue multiplier per day
OVERDUE_MULTIPLIER = 2.0
OVERDUE_CAP = 100


def score_contact(contact):
    """Calculate contact score.

    Args:
        contact: dict with keys contact_priority, follow_up_priority,
                 follow_up_date (str YYYY-MM-DD or empty)

    Returns:
        float score (0-100)
    """
    cp = CP_WEIGHTS.get(contact.get("contact_priority", ""), 0)
    fu = FU_WEIGHTS.get(contact.get("follow_up_priority", ""), 0)

    # Overdue calculation
    overdue = 0
    fu_date_str = contact.get("follow_up_date", "")
    if fu_date_str:
        try:
            fu_date = datetime.strptime(fu_date_str, "%Y-%m-%d").date()
            days_overdue = (date.today() - fu_date).days
            if days_overdue > 0:
                overdue = min(days_overdue * OVERDUE_MULTIPLIER, OVERDUE_CAP)
        except ValueError:
            pass

    # Context weight = 0 for MVP
    context = 0

    score = (cp * 0.30) + (fu * 0.25) + (overdue * 0.30) + (context * 0.15)
    return round(score, 1)


def sort_contacts_by_score(contacts):
    """Sort contacts list by score descending."""
    for c in contacts:
        c["score"] = score_contact(c)
    return sorted(contacts, key=lambda x: x["score"], reverse=True)


def score_entity(entity):
    """Calculate entity score.

    Score = (BP × 0.35) + (FU × 0.30) + (Overdue × 0.35)
    """
    bp = BP_WEIGHTS.get(entity.get("business_priority", ""), 0)
    fu = FU_WEIGHTS.get(entity.get("follow_up_priority", ""), 0)

    overdue = 0
    fu_date_str = entity.get("follow_up_date", "")
    if fu_date_str:
        try:
            fu_date = datetime.strptime(fu_date_str, "%Y-%m-%d").date()
            days_overdue = (date.today() - fu_date).days
            if days_overdue > 0:
                overdue = min(days_overdue * OVERDUE_MULTIPLIER, OVERDUE_CAP)
        except ValueError:
            pass

    score = (bp * 0.35) + (fu * 0.30) + (overdue * 0.35)
    return round(score, 1)


def sort_entities_by_score(entities):
    """Sort entities list by score descending."""
    for e in entities:
        e["score"] = score_entity(e)
    return sorted(entities, key=lambda x: x["score"], reverse=True)


def auto_upgrade_entity_followup(entities):
    """Auto-upgrade entity follow-up priority if overdue > 7 days."""
    today = date.today()
    upgraded = []

    for entity in entities:
        fu = entity.get("follow_up_priority", "")
        fu_date_str = entity.get("follow_up_date", "")

        if fu not in FU_UPGRADE or not fu_date_str:
            continue

        try:
            fu_date = datetime.strptime(fu_date_str, "%Y-%m-%d").date()
        except ValueError:
            continue

        days_overdue = (today - fu_date).days
        if days_overdue > 7:
            new_fu = FU_UPGRADE[fu]
            old_fu = fu
            entity["follow_up_priority"] = new_fu
            upgraded.append((entity, old_fu, new_fu))

    return upgraded


def auto_upgrade_followup(contacts):
    """Auto-upgrade follow-up priority if overdue > 7 days.

    Returns list of (contact, old_fu, new_fu) for contacts that were upgraded.
    """
    today = date.today()
    upgraded = []

    for contact in contacts:
        fu = contact.get("follow_up_priority", "")
        fu_date_str = contact.get("follow_up_date", "")

        # Skip FU9 and contacts without FU date
        if fu not in FU_UPGRADE or not fu_date_str:
            continue

        try:
            fu_date = datetime.strptime(fu_date_str, "%Y-%m-%d").date()
        except ValueError:
            continue

        days_overdue = (today - fu_date).days
        if days_overdue > 7:
            new_fu = FU_UPGRADE[fu]
            old_fu = fu
            contact["follow_up_priority"] = new_fu
            upgraded.append((contact, old_fu, new_fu))

    return upgraded
