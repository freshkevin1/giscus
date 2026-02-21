"""Google Sheets CRUD for Business Entities and Opportunities."""

import logging
import time
import uuid
from datetime import datetime

import gspread

from encryption import hmac_index
from sheets import _get_spreadsheet

logger = logging.getLogger(__name__)

# --- Headers ---

ENTITY_HEADERS = [
    "Name", "Entity_hmac", "Business Priority",
    "Follow-up Priority", "Follow-up Date", "Follow-up Note",
    "Last Contact", "Key Value & Interest", "Interaction Context",
    "Tag", "Related Individuals", "Referred by",
    "Last Modified", "Created Date",
]

ENTITY_OPP_HEADERS = [
    "Entity_hmac", "Opp_id", "Title", "Details", "Created Date",
]

ENTITY_LOG_HEADERS = [
    "Date", "Entity_hmac", "Display Name", "Context",
    "Key Value Extracted", "Updated Fields",
]

ENTITY_CHANGE_LOG_HEADERS = [
    "Timestamp", "Entity_hmac", "Field", "Old Value", "New Value", "Changed By",
]

DELETED_ENTITY_HEADERS = ENTITY_HEADERS + ["Deleted Date", "Deleted By"]

BP_VALUES = ["0-Critical", "1-High", "2-Medium", "3-Low"]

# In-memory cache
_ecache = {
    "entities": None,
    "entities_time": 0,
    "deleted_entities": None,
    "deleted_entities_time": 0,
}
CACHE_TTL = 300  # 5 minutes


def _invalidate_entity_cache(key=None):
    if key:
        _ecache[key] = None
        _ecache[f"{key}_time"] = 0
    else:
        for k in ["entities", "deleted_entities"]:
            _ecache[k] = None
            _ecache[f"{k}_time"] = 0


def _is_ecached(key):
    return _ecache[key] is not None and (time.time() - _ecache[f"{key}_time"]) < CACHE_TTL


# --- Sheet Setup ---

def ensure_entity_sheet_headers():
    """Create entity-related tabs and headers if they don't exist."""
    sp = _get_spreadsheet()
    existing = [ws.title for ws in sp.worksheets()]

    tabs = {
        "Business Entities": ENTITY_HEADERS,
        "Entity Opportunities": ENTITY_OPP_HEADERS,
        "Entity Log": ENTITY_LOG_HEADERS,
        "Entity Change Log": ENTITY_CHANGE_LOG_HEADERS,
        "Deleted Entities": DELETED_ENTITY_HEADERS,
    }

    for tab_name, headers in tabs.items():
        if tab_name not in existing:
            ws = sp.add_worksheet(title=tab_name, rows=1000, cols=len(headers))
            ws.update("A1", [headers])
            logger.info("Created entity tab: %s", tab_name)
        else:
            ws = sp.worksheet(tab_name)
            current_headers = ws.row_values(1)
            if current_headers != headers:
                ws.update("A1", [headers])
                logger.info("Updated headers for entity tab: %s", tab_name)


# --- Row conversion ---

def _row_to_entity(row, headers):
    data = {}
    for i, header in enumerate(headers):
        data[header] = row[i] if i < len(row) else ""

    return {
        "name": data.get("Name", ""),
        "entity_hmac": data.get("Entity_hmac", ""),
        "business_priority": data.get("Business Priority", ""),
        "follow_up_priority": data.get("Follow-up Priority", ""),
        "follow_up_date": data.get("Follow-up Date", ""),
        "follow_up_note": data.get("Follow-up Note", ""),
        "last_contact": data.get("Last Contact", ""),
        "key_value_interest": data.get("Key Value & Interest", ""),
        "interaction_context": data.get("Interaction Context", ""),
        "tag": data.get("Tag", ""),
        "related_individuals": data.get("Related Individuals", ""),
        "referred_by": data.get("Referred by", ""),
        "last_modified": data.get("Last Modified", ""),
        "created_date": data.get("Created Date", ""),
    }


def _entity_to_row(entity):
    today = datetime.now().strftime("%Y-%m-%d")
    name = entity.get("name", "")
    entity_hmac = entity.get("entity_hmac") or hmac_index(name)

    return [
        name,
        entity_hmac,
        entity.get("business_priority", ""),
        entity.get("follow_up_priority", "FU9"),
        entity.get("follow_up_date", ""),
        entity.get("follow_up_note", ""),
        entity.get("last_contact", ""),
        entity.get("key_value_interest", ""),
        entity.get("interaction_context", ""),
        entity.get("tag", ""),
        entity.get("related_individuals", ""),
        entity.get("referred_by", ""),
        today,
        entity.get("created_date", today),
    ]


def _find_entity_row(ws, entity_hmac):
    """Find 1-based row index for entity by entity_hmac (column B)."""
    col_values = ws.col_values(2)
    for i, val in enumerate(col_values):
        if val == entity_hmac and i > 0:
            return i + 1
    return None


# --- Entity CRUD ---

def get_all_entities():
    """Get all entities from Business Entities tab. Uses cache."""
    if _is_ecached("entities"):
        return _ecache["entities"]

    sp = _get_spreadsheet()
    ws = sp.worksheet("Business Entities")
    all_rows = ws.get_all_values()

    if len(all_rows) <= 1:
        _ecache["entities"] = []
        _ecache["entities_time"] = time.time()
        return []

    headers = all_rows[0]
    entities = []
    for row in all_rows[1:]:
        try:
            entities.append(_row_to_entity(row, headers))
        except Exception as e:
            logger.warning("Failed to parse entity row: %s", e)

    _ecache["entities"] = entities
    _ecache["entities_time"] = time.time()
    return entities


def find_entity_by_name(name):
    """Find entities by name (case-insensitive substring match)."""
    entities = get_all_entities()
    name_lower = name.strip().lower()
    return [e for e in entities if name_lower in e["name"].lower()]


def find_entity_by_hmac(entity_hmac):
    """Find a single entity by entity_hmac."""
    entities = get_all_entities()
    for e in entities:
        if e["entity_hmac"] == entity_hmac:
            return e
    return None


def add_entity(entity):
    """Add a new entity to Business Entities tab. Returns entity_hmac."""
    sp = _get_spreadsheet()
    ws = sp.worksheet("Business Entities")
    row = _entity_to_row(entity)
    ws.append_row(row, value_input_option="USER_ENTERED")
    _invalidate_entity_cache("entities")
    logger.info("Added entity: %s", entity.get("name", ""))
    return row[1]  # entity_hmac


def update_entity(entity_hmac, fields, changed_by="User"):
    """Update specific fields of an entity. Logs changes.

    Returns True if updated, False if not found.
    """
    sp = _get_spreadsheet()
    ws = sp.worksheet("Business Entities")
    row_idx = _find_entity_row(ws, entity_hmac)
    if not row_idx:
        return False

    current_row = ws.row_values(row_idx)
    headers = ws.row_values(1)

    key_to_header = {
        "name": "Name",
        "business_priority": "Business Priority",
        "follow_up_priority": "Follow-up Priority",
        "follow_up_date": "Follow-up Date",
        "follow_up_note": "Follow-up Note",
        "last_contact": "Last Contact",
        "key_value_interest": "Key Value & Interest",
        "interaction_context": "Interaction Context",
        "tag": "Tag",
        "related_individuals": "Related Individuals",
        "referred_by": "Referred by",
    }

    changes = []
    cells_to_update = []

    for key, new_value in fields.items():
        header = key_to_header.get(key)
        if not header or header not in headers:
            continue

        col_idx = headers.index(header)
        old_value = current_row[col_idx] if col_idx < len(current_row) else ""

        if old_value != new_value:
            cells_to_update.append((row_idx, col_idx + 1, new_value))
            changes.append((header, old_value, new_value))

    if not cells_to_update:
        return True

    today = datetime.now().strftime("%Y-%m-%d")
    lm_col = headers.index("Last Modified") + 1 if "Last Modified" in headers else None
    if lm_col:
        cells_to_update.append((row_idx, lm_col, today))

    from gspread.utils import rowcol_to_a1
    ws.batch_update(
        [{"range": rowcol_to_a1(r, c), "values": [[v]]} for r, c, v in cells_to_update],
        value_input_option="RAW",
    )

    for field_name, old_val, new_val in changes:
        _log_entity_change(entity_hmac, field_name, old_val, new_val, changed_by)

    _invalidate_entity_cache("entities")
    return True


def delete_entity(entity_hmac, deleted_by="User"):
    """Soft-delete: move entity from Business Entities to Deleted Entities tab."""
    sp = _get_spreadsheet()
    ws_main = sp.worksheet("Business Entities")
    row_idx = _find_entity_row(ws_main, entity_hmac)
    if not row_idx:
        return False

    row_data = ws_main.row_values(row_idx)
    while len(row_data) < len(ENTITY_HEADERS):
        row_data.append("")

    deleted_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row_data.append(deleted_date)
    row_data.append(deleted_by)

    ws_deleted = sp.worksheet("Deleted Entities")
    ws_deleted.append_row(row_data, value_input_option="USER_ENTERED")

    ws_main.delete_rows(row_idx)

    _invalidate_entity_cache()
    logger.info("Soft-deleted entity %s by %s", entity_hmac, deleted_by)
    return True


def get_deleted_entities():
    """Get all entities from Deleted Entities tab. Uses cache."""
    if _is_ecached("deleted_entities"):
        return _ecache["deleted_entities"]

    sp = _get_spreadsheet()
    ws = sp.worksheet("Deleted Entities")
    all_rows = ws.get_all_values()

    if len(all_rows) <= 1:
        _ecache["deleted_entities"] = []
        _ecache["deleted_entities_time"] = time.time()
        return []

    headers = all_rows[0]
    entities = []
    for row in all_rows[1:]:
        try:
            entity = _row_to_entity(row, headers)
            deleted_date_idx = len(ENTITY_HEADERS)
            deleted_by_idx = len(ENTITY_HEADERS) + 1
            entity["deleted_date"] = row[deleted_date_idx] if deleted_date_idx < len(row) else ""
            entity["deleted_by"] = row[deleted_by_idx] if deleted_by_idx < len(row) else ""
            entities.append(entity)
        except Exception as e:
            logger.warning("Failed to parse deleted entity row: %s", e)

    _ecache["deleted_entities"] = entities
    _ecache["deleted_entities_time"] = time.time()
    return entities


def restore_entity(entity_hmac):
    """Restore an entity from Deleted Entities back to Business Entities."""
    sp = _get_spreadsheet()
    ws_deleted = sp.worksheet("Deleted Entities")

    col_values = ws_deleted.col_values(2)
    row_idx = None
    for i, val in enumerate(col_values):
        if val == entity_hmac and i > 0:
            row_idx = i + 1
            break

    if not row_idx:
        return False

    row_data = ws_deleted.row_values(row_idx)
    master_row = row_data[:len(ENTITY_HEADERS)]
    while len(master_row) < len(ENTITY_HEADERS):
        master_row.append("")

    lm_idx = ENTITY_HEADERS.index("Last Modified")
    master_row[lm_idx] = datetime.now().strftime("%Y-%m-%d")

    ws_main = sp.worksheet("Business Entities")
    ws_main.append_row(master_row, value_input_option="USER_ENTERED")

    ws_deleted.delete_rows(row_idx)

    _invalidate_entity_cache()
    logger.info("Restored entity %s", entity_hmac)
    return True


def permanent_delete_entity(entity_hmac):
    """Permanently delete an entity from Deleted Entities tab."""
    sp = _get_spreadsheet()
    ws = sp.worksheet("Deleted Entities")

    col_values = ws.col_values(2)
    for i, val in enumerate(col_values):
        if val == entity_hmac and i > 0:
            ws.delete_rows(i + 1)
            _invalidate_entity_cache("deleted_entities")
            logger.info("Permanently deleted entity %s", entity_hmac)
            return True
    return False


# --- Entity Log ---

def add_entity_log(entity_hmac, display_name, context, key_value_extracted="", updated_fields=""):
    """Add a new entity interaction log entry."""
    sp = _get_spreadsheet()
    ws = sp.worksheet("Entity Log")
    today = datetime.now().strftime("%Y-%m-%d")
    row = [today, entity_hmac, display_name, context, key_value_extracted, updated_fields]
    ws.append_row(row, value_input_option="USER_ENTERED")


def get_entity_logs(entity_hmac):
    """Get all interaction logs for an entity."""
    sp = _get_spreadsheet()
    ws = sp.worksheet("Entity Log")
    all_rows = ws.get_all_values()

    if len(all_rows) <= 1:
        return []

    headers = all_rows[0]
    logs = []
    for row in all_rows[1:]:
        row_data = {h: (row[i] if i < len(row) else "") for i, h in enumerate(headers)}
        if row_data.get("Entity_hmac") == entity_hmac:
            logs.append({
                "date": row_data.get("Date", ""),
                "display_name": row_data.get("Display Name", ""),
                "context": row_data.get("Context", ""),
                "key_value_extracted": row_data.get("Key Value Extracted", ""),
                "updated_fields": row_data.get("Updated Fields", ""),
            })
    return logs


def _log_entity_change(entity_hmac, field, old_value, new_value, changed_by="User"):
    """Log a field change for an entity."""
    sp = _get_spreadsheet()
    ws = sp.worksheet("Entity Change Log")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = [timestamp, entity_hmac, field, old_value, new_value, changed_by]
    ws.append_row(row, value_input_option="USER_ENTERED")


# --- Opportunity CRUD ---

def _find_opp_row(ws, entity_hmac, opp_id):
    """Find 1-based row index for an opportunity by entity_hmac + opp_id."""
    all_rows = ws.get_all_values()
    for i, row in enumerate(all_rows[1:], start=2):
        if len(row) >= 2 and row[0] == entity_hmac and row[1] == opp_id:
            return i
    return None


def get_entity_opportunities(entity_hmac):
    """Get all opportunities for an entity."""
    sp = _get_spreadsheet()
    ws = sp.worksheet("Entity Opportunities")
    all_rows = ws.get_all_values()

    if len(all_rows) <= 1:
        return []

    headers = all_rows[0]
    opps = []
    for row in all_rows[1:]:
        row_data = {h: (row[i] if i < len(row) else "") for i, h in enumerate(headers)}
        if row_data.get("Entity_hmac") == entity_hmac:
            opps.append({
                "entity_hmac": row_data.get("Entity_hmac", ""),
                "opp_id": row_data.get("Opp_id", ""),
                "title": row_data.get("Title", ""),
                "details": row_data.get("Details", ""),
                "created_date": row_data.get("Created Date", ""),
            })
    return opps


def add_opportunity(entity_hmac, title, details=""):
    """Add a new opportunity for an entity. Returns opp_id."""
    sp = _get_spreadsheet()
    ws = sp.worksheet("Entity Opportunities")
    opp_id = str(uuid.uuid4())[:8]
    today = datetime.now().strftime("%Y-%m-%d")
    row = [entity_hmac, opp_id, title, details, today]
    ws.append_row(row, value_input_option="USER_ENTERED")
    logger.info("Added opportunity '%s' for entity %s", title, entity_hmac)
    return opp_id


def update_opportunity(entity_hmac, opp_id, title=None, details=None):
    """Update an opportunity's title and/or details. Returns True if updated."""
    sp = _get_spreadsheet()
    ws = sp.worksheet("Entity Opportunities")
    row_idx = _find_opp_row(ws, entity_hmac, opp_id)
    if not row_idx:
        return False

    headers = ws.row_values(1)
    cells_to_update = []

    if title is not None:
        col = headers.index("Title") + 1 if "Title" in headers else None
        if col:
            cells_to_update.append((row_idx, col, title))

    if details is not None:
        col = headers.index("Details") + 1 if "Details" in headers else None
        if col:
            cells_to_update.append((row_idx, col, details))

    if cells_to_update:
        from gspread.utils import rowcol_to_a1
        ws.batch_update(
            [{"range": rowcol_to_a1(r, c), "values": [[v]]} for r, c, v in cells_to_update],
            value_input_option="RAW",
        )
    return True


def delete_opportunity(entity_hmac, opp_id):
    """Delete an opportunity row. Returns True if deleted."""
    sp = _get_spreadsheet()
    ws = sp.worksheet("Entity Opportunities")
    row_idx = _find_opp_row(ws, entity_hmac, opp_id)
    if not row_idx:
        return False
    ws.delete_rows(row_idx)
    logger.info("Deleted opportunity %s for entity %s", opp_id, entity_hmac)
    return True


# --- Suggested Contacts ---

def get_suggested_contacts(entity_hmac):
    """Return suggested contacts for an entity using Employer matching.

    Returns dict: {"suggested": [...], "others": [...]}
    Priority:
      1. Contact.Employer contains Entity.Name (case-insensitive) → Employer match
      2. Contact.Tag == Entity.Tag → tag suggestion
      3. Everything else
    """
    from sheets import get_all_contacts

    entity = find_entity_by_hmac(entity_hmac)
    if not entity:
        return {"suggested": [], "others": []}

    entity_name_lower = entity["name"].strip().lower()
    entity_tag = entity.get("tag", "").strip().lower()

    # Parse existing related individuals (stored as comma-sep hmacs or names)
    related_raw = entity.get("related_individuals", "")
    related_set = {r.strip() for r in related_raw.split(",") if r.strip()}

    contacts = get_all_contacts()
    suggested = []
    others = []

    for c in contacts:
        employer = (c.get("employer") or "").strip().lower()
        c_tag = (c.get("tag") or "").strip().lower()
        c_name = (c.get("name") or "").strip().lower()

        match_reason = None
        rank = 3

        # Priority 1: Employer contains entity name OR entity name contains employer
        if entity_name_lower and employer and (
            entity_name_lower in employer or employer in entity_name_lower
        ):
            match_reason = "Employer match"
            rank = 1

        # Priority 2: Tag match
        elif entity_tag and c_tag and entity_tag == c_tag:
            match_reason = "Tag match"
            rank = 2

        # Priority 3: Entity name keyword in contact name
        elif entity_name_lower and entity_name_lower in c_name:
            match_reason = "Name match"
            rank = 3

        is_related = c["name_hmac"] in related_set

        entry = {
            "name_hmac": c["name_hmac"],
            "display_name": f"{c['name']}({c['employer']})" if c.get("employer") else c["name"],
            "match_reason": match_reason,
            "rank": rank,
            "is_related": is_related,
        }

        if match_reason:
            suggested.append(entry)
        else:
            others.append(entry)

    suggested.sort(key=lambda x: x["rank"])
    return {"suggested": suggested, "others": others}
