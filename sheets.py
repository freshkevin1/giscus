"""Google Sheets CRUD operations with in-memory caching."""

import json
import logging
import os
import time
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

from encryption import decrypt, encrypt, hmac_index

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

MASTER_HEADERS = [
    "Name", "Name_hmac", "Contact Priority", "Employer", "Title",
    "Follow-up Priority", "Follow-up Date", "Follow-up Note",
    "Last Contact", "Key Value & Interest", "Tag", "Referred by",
    "Email", "Email_hmac", "Phone Number", "Phone_hmac",
    "Last Modified", "Created Date",
]

INTERACTION_LOG_HEADERS = [
    "Date", "Name_hmac", "Display Name", "Context",
    "Key Value Extracted", "Updated Fields",
]

CHANGE_LOG_HEADERS = [
    "Timestamp", "Name_hmac", "Field", "Old Value", "New Value", "Changed By",
]

TAGS_HEADERS = ["Tag Name"]

DEFAULT_TAGS = ["Tuck", "McKinsey", "Toss", "Doosan"]

# In-memory cache
_cache = {
    "contacts": None,
    "contacts_time": 0,
    "tags": None,
    "tags_time": 0,
}
CACHE_TTL = 300  # 5 minutes


def _get_client():
    """Get authenticated gspread client."""
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not creds_json:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON not set")
    creds_data = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_data, scopes=SCOPES)
    return gspread.authorize(creds)


def _get_spreadsheet():
    """Get the Contact List spreadsheet."""
    client = _get_client()
    sheet_name = os.environ.get("GOOGLE_SHEET_NAME", "Contact List")
    return client.open(sheet_name)


def _invalidate_cache(key=None):
    """Invalidate cache. If key is None, invalidate all."""
    if key:
        _cache[key] = None
        _cache[f"{key}_time"] = 0
    else:
        _cache["contacts"] = None
        _cache["contacts_time"] = 0
        _cache["tags"] = None
        _cache["tags_time"] = 0


def _is_cached(key):
    return _cache[key] is not None and (time.time() - _cache[f"{key}_time"]) < CACHE_TTL


# --- Sheet Setup ---

def ensure_sheet_headers():
    """Create tabs and headers if they don't exist."""
    sp = _get_spreadsheet()
    existing = [ws.title for ws in sp.worksheets()]

    tabs = {
        "Master": MASTER_HEADERS,
        "Interaction Log": INTERACTION_LOG_HEADERS,
        "Change Log": CHANGE_LOG_HEADERS,
        "Tags": TAGS_HEADERS,
    }

    for tab_name, headers in tabs.items():
        if tab_name not in existing:
            ws = sp.add_worksheet(title=tab_name, rows=1000, cols=len(headers))
            ws.update("A1", [headers])
            logger.info("Created tab: %s", tab_name)
            if tab_name == "Tags" and DEFAULT_TAGS:
                rows = [[tag] for tag in DEFAULT_TAGS]
                ws.update(f"A2:A{1 + len(rows)}", rows)
                logger.info("Added default tags: %s", DEFAULT_TAGS)
        else:
            ws = sp.worksheet(tab_name)
            current_headers = ws.row_values(1)
            if current_headers != headers:
                ws.update("A1", [headers])
                logger.info("Updated headers for tab: %s", tab_name)

    # Remove default Sheet1 if other tabs exist
    if "Sheet1" in existing and len(existing) > 1:
        try:
            sp.del_worksheet(sp.worksheet("Sheet1"))
        except Exception:
            pass


# --- Master Tab CRUD ---

def _row_to_contact(row, headers):
    """Convert a sheet row to a contact dict with decrypted PII."""
    data = {}
    for i, header in enumerate(headers):
        data[header] = row[i] if i < len(row) else ""

    # Decrypt PII fields
    contact = {
        "name": decrypt(data.get("Name", "")),
        "name_hmac": data.get("Name_hmac", ""),
        "contact_priority": data.get("Contact Priority", ""),
        "employer": data.get("Employer", ""),
        "title": data.get("Title", ""),
        "follow_up_priority": data.get("Follow-up Priority", ""),
        "follow_up_date": data.get("Follow-up Date", ""),
        "follow_up_note": data.get("Follow-up Note", ""),
        "last_contact": data.get("Last Contact", ""),
        "key_value_interest": data.get("Key Value & Interest", ""),
        "tag": data.get("Tag", ""),
        "referred_by": data.get("Referred by", ""),
        "email": decrypt(data.get("Email", "")),
        "email_hmac": data.get("Email_hmac", ""),
        "phone": decrypt(data.get("Phone Number", "")),
        "phone_hmac": data.get("Phone_hmac", ""),
        "last_modified": data.get("Last Modified", ""),
        "created_date": data.get("Created Date", ""),
    }
    return contact


def _contact_to_row(contact):
    """Convert a contact dict to a sheet row with encrypted PII."""
    today = datetime.now().strftime("%Y-%m-%d")
    name = contact.get("name", "")
    email = contact.get("email", "")
    phone = contact.get("phone", "")

    # Generate HMAC — use name_employer combo for uniqueness
    employer = contact.get("employer", "")
    hmac_value = hmac_index(f"{name}_{employer}" if employer else name)

    return [
        encrypt(name),
        hmac_value,
        contact.get("contact_priority", ""),
        employer,
        contact.get("title", ""),
        contact.get("follow_up_priority", "FU9"),
        contact.get("follow_up_date", ""),
        contact.get("follow_up_note", ""),
        contact.get("last_contact", ""),
        contact.get("key_value_interest", ""),
        contact.get("tag", ""),
        contact.get("referred_by", ""),
        encrypt(email),
        hmac_index(email) if email else "",
        encrypt(phone),
        hmac_index(phone) if phone else "",
        today,
        contact.get("created_date", today),
    ]


def get_all_contacts():
    """Get all contacts from Master tab. Uses cache."""
    if _is_cached("contacts"):
        return _cache["contacts"]

    sp = _get_spreadsheet()
    ws = sp.worksheet("Master")
    all_rows = ws.get_all_values()

    if len(all_rows) <= 1:
        _cache["contacts"] = []
        _cache["contacts_time"] = time.time()
        return []

    headers = all_rows[0]
    contacts = []
    for row in all_rows[1:]:
        try:
            contacts.append(_row_to_contact(row, headers))
        except Exception as e:
            logger.warning("Failed to parse row: %s", e)
            continue

    _cache["contacts"] = contacts
    _cache["contacts_time"] = time.time()
    return contacts


def find_contact_by_name(name):
    """Find contact(s) by name using HMAC index. Returns list (may have duplicates for 동명이인)."""
    contacts = get_all_contacts()
    target_hmac = hmac_index(name)

    # First try exact name match
    results = [c for c in contacts if c["name_hmac"] == target_hmac]

    if not results:
        # Try name_employer combo HMACs
        results = [c for c in contacts
                   if hmac_index(f"{name}_{c['employer']}") == c["name_hmac"]]

    if not results:
        # Fallback: substring match on decrypted names
        name_lower = name.strip().lower()
        results = [c for c in contacts if name_lower in c["name"].lower()]

    return results


def find_contact_by_hmac(name_hmac):
    """Find a single contact by its name_hmac value."""
    contacts = get_all_contacts()
    for c in contacts:
        if c["name_hmac"] == name_hmac:
            return c
    return None


def _find_row_index(ws, name_hmac):
    """Find the row index (1-based) for a contact by name_hmac."""
    col_values = ws.col_values(2)  # Name_hmac is column B
    for i, val in enumerate(col_values):
        if val == name_hmac and i > 0:  # Skip header
            return i + 1
    return None


def add_contact(contact):
    """Add a new contact to Master tab."""
    sp = _get_spreadsheet()
    ws = sp.worksheet("Master")
    row = _contact_to_row(contact)
    ws.append_row(row, value_input_option="USER_ENTERED")
    _invalidate_cache("contacts")
    logger.info("Added contact: %s", contact.get("name", ""))
    return row[1]  # Return name_hmac


def update_contact(name_hmac, fields, changed_by="User"):
    """Update specific fields of a contact. Logs changes.

    Args:
        name_hmac: HMAC identifier of the contact
        fields: dict of field_name -> new_value (using internal keys like 'employer', 'title')
        changed_by: "User" or "AI"

    Returns:
        True if updated, False if not found
    """
    sp = _get_spreadsheet()
    ws = sp.worksheet("Master")
    row_idx = _find_row_index(ws, name_hmac)
    if not row_idx:
        return False

    # Get current row data
    current_row = ws.row_values(row_idx)
    headers = ws.row_values(1)

    # Map internal keys to sheet column names
    key_to_header = {
        "name": "Name",
        "contact_priority": "Contact Priority",
        "employer": "Employer",
        "title": "Title",
        "follow_up_priority": "Follow-up Priority",
        "follow_up_date": "Follow-up Date",
        "follow_up_note": "Follow-up Note",
        "last_contact": "Last Contact",
        "key_value_interest": "Key Value & Interest",
        "tag": "Tag",
        "referred_by": "Referred by",
        "email": "Email",
        "phone": "Phone Number",
    }

    # PII fields that need encryption
    pii_fields = {"name", "email", "phone"}
    # Fields that need HMAC update
    hmac_fields = {"name": "Name_hmac", "email": "Email_hmac", "phone": "Phone_hmac"}

    changes = []
    cells_to_update = []

    for key, new_value in fields.items():
        header = key_to_header.get(key)
        if not header or header not in headers:
            continue

        col_idx = headers.index(header)
        old_value = current_row[col_idx] if col_idx < len(current_row) else ""

        # For PII fields, decrypt old value for change log
        if key in pii_fields:
            old_display = decrypt(old_value)
            new_encrypted = encrypt(new_value)
            if old_value != new_encrypted:
                cells_to_update.append((row_idx, col_idx + 1, new_encrypted))
                changes.append((header, old_display, new_value))
                # Update HMAC
                if key in hmac_fields:
                    hmac_header = hmac_fields[key]
                    hmac_col = headers.index(hmac_header) + 1
                    if key == "name":
                        employer = fields.get("employer") or (
                            current_row[headers.index("Employer")]
                            if "Employer" in headers and headers.index("Employer") < len(current_row)
                            else ""
                        )
                        new_hmac = hmac_index(f"{new_value}_{employer}" if employer else new_value)
                    else:
                        new_hmac = hmac_index(new_value) if new_value else ""
                    cells_to_update.append((row_idx, hmac_col, new_hmac))
        else:
            if old_value != new_value:
                cells_to_update.append((row_idx, col_idx + 1, new_value))
                changes.append((header, old_value, new_value))

    if not cells_to_update:
        return True  # No changes needed

    # Update Last Modified
    today = datetime.now().strftime("%Y-%m-%d")
    lm_col = headers.index("Last Modified") + 1 if "Last Modified" in headers else None
    if lm_col:
        cells_to_update.append((row_idx, lm_col, today))

    # Apply all cell updates
    for r, c, v in cells_to_update:
        ws.update_cell(r, c, v)

    # Log changes
    for field_name, old_val, new_val in changes:
        log_change(name_hmac, field_name, old_val, new_val, changed_by)

    _invalidate_cache("contacts")
    return True


def delete_contact(name_hmac):
    """Delete a contact from Master tab."""
    sp = _get_spreadsheet()
    ws = sp.worksheet("Master")
    row_idx = _find_row_index(ws, name_hmac)
    if not row_idx:
        return False
    ws.delete_rows(row_idx)
    _invalidate_cache("contacts")
    return True


# --- Interaction Log Tab ---

def add_interaction_log(name_hmac, display_name, context, key_value_extracted="", updated_fields=""):
    """Add a new interaction log entry."""
    sp = _get_spreadsheet()
    ws = sp.worksheet("Interaction Log")
    today = datetime.now().strftime("%Y-%m-%d")
    row = [today, name_hmac, display_name, context, key_value_extracted, updated_fields]
    ws.append_row(row, value_input_option="USER_ENTERED")
    logger.info("Added interaction log for: %s", display_name)


def get_interaction_logs(name_hmac):
    """Get all interaction logs for a contact."""
    sp = _get_spreadsheet()
    ws = sp.worksheet("Interaction Log")
    all_rows = ws.get_all_values()

    if len(all_rows) <= 1:
        return []

    headers = all_rows[0]
    logs = []
    for row in all_rows[1:]:
        row_data = {}
        for i, h in enumerate(headers):
            row_data[h] = row[i] if i < len(row) else ""
        if row_data.get("Name_hmac") == name_hmac:
            logs.append({
                "date": row_data.get("Date", ""),
                "display_name": row_data.get("Display Name", ""),
                "context": row_data.get("Context", ""),
                "key_value_extracted": row_data.get("Key Value Extracted", ""),
                "updated_fields": row_data.get("Updated Fields", ""),
            })

    return logs


# --- Change Log Tab ---

def log_change(name_hmac, field, old_value, new_value, changed_by="User"):
    """Log a field change for rollback."""
    sp = _get_spreadsheet()
    ws = sp.worksheet("Change Log")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = [timestamp, name_hmac, field, old_value, new_value, changed_by]
    ws.append_row(row, value_input_option="USER_ENTERED")


# --- Tags Tab ---

def get_valid_tags():
    """Get list of valid tags from Tags tab. Uses cache."""
    if _is_cached("tags"):
        return _cache["tags"]

    sp = _get_spreadsheet()
    ws = sp.worksheet("Tags")
    all_rows = ws.get_all_values()

    tags = []
    for row in all_rows[1:]:  # Skip header
        if row and row[0].strip():
            tags.append(row[0].strip())

    _cache["tags"] = tags
    _cache["tags_time"] = time.time()
    return tags


def add_tag(tag_name):
    """Add a new tag to the Tags tab."""
    sp = _get_spreadsheet()
    ws = sp.worksheet("Tags")
    ws.append_row([tag_name], value_input_option="USER_ENTERED")
    _invalidate_cache("tags")
    logger.info("Added new tag: %s", tag_name)
