"""
anki_parser.py - Parse highlight sources into Anki card dicts.

Supported formats:
  - Kindle My Clippings.txt  (========== separator)
  - Readwise MD export       (# heading + > blockquote)
  - 밀리의서재 share text     (p.N pattern or plain paragraphs)
  - PDF                      (.pdf extension, PyPDF2 text extraction)

Each parser returns: list[dict] with keys:
  deck_name, author, front, back, source_ref
"""

import os
import re


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_auto(content, filename: str) -> list:
    """Detect format and dispatch to the correct parser."""
    if filename.lower().endswith('.pdf'):
        return parse_pdf_bytes(content, filename=filename)
    if '==========' in content:
        return parse_kindle_clippings(content)
    if re.search(r'^> .+', content, re.MULTILINE):
        return parse_readwise_md(content)
    return parse_millie_text(content)


# ---------------------------------------------------------------------------
# Kindle My Clippings
# ---------------------------------------------------------------------------

def parse_kindle_clippings(content: str) -> list:
    """Parse Kindle 'My Clippings.txt' format."""
    cards = []
    sections = content.split('==========')
    for section in sections:
        section = section.strip()
        if not section:
            continue
        lines = [l for l in section.splitlines() if l.strip()]
        if len(lines) < 2:
            continue

        # Line 0: "Book Title (Author Name)"
        title_line = lines[0].strip()
        author = ''
        match = re.search(r'\(([^)]+)\)\s*$', title_line)
        if match:
            author = match.group(1).strip()
            deck_name = title_line[:match.start()].strip()
        else:
            deck_name = title_line

        # Line 1: metadata — location or page
        meta_line = lines[1].strip() if len(lines) > 1 else ''
        loc_match = re.search(r'location\s+([\d\-]+)', meta_line, re.IGNORECASE)
        page_match = re.search(r'page\s+([\d\-]+)', meta_line, re.IGNORECASE)
        if loc_match:
            source_ref = f"Location {loc_match.group(1)}"
        elif page_match:
            source_ref = f"p.{page_match.group(1)}"
        else:
            source_ref = ''

        # Skip non-highlight entries (notes, bookmarks)
        if 'highlight' not in meta_line.lower() and 'passage' not in meta_line.lower():
            continue

        # Remaining lines: highlight text
        text = ' '.join(lines[2:]).strip()
        if len(text) < 5:
            continue

        front = f'"{deck_name}"'
        if source_ref:
            front += f' | {source_ref}'

        cards.append({
            'deck_name': deck_name,
            'author': author,
            'front': front,
            'back': text,
            'source_ref': source_ref,
        })

    return cards


# ---------------------------------------------------------------------------
# Readwise Markdown export
# ---------------------------------------------------------------------------

def parse_readwise_md(content: str) -> list:
    """Parse Readwise markdown export (# headings + > blockquotes)."""
    cards = []
    deck_name = ''
    author = ''
    current_chapter = ''

    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]

        # Top-level heading = book title
        if line.startswith('# ') and not line.startswith('## '):
            deck_name = line[2:].strip()
            # Look for author on next line
            if i + 1 < len(lines) and lines[i + 1].strip().startswith('By '):
                author = lines[i + 1].strip()[3:].strip()
                i += 1

        # Sub-heading = chapter
        elif line.startswith('## ') or line.startswith('### '):
            current_chapter = re.sub(r'^#+\s*', '', line).strip()

        # Blockquote = highlight
        elif line.startswith('> '):
            highlight_lines = []
            while i < len(lines) and lines[i].startswith('> '):
                highlight_lines.append(lines[i][2:].strip())
                i += 1
            text = ' '.join(highlight_lines).strip()
            if len(text) < 5:
                continue

            front_parts = [f'"{deck_name}"'] if deck_name else []
            if current_chapter:
                front_parts.append(current_chapter)
            front = ' | '.join(front_parts) if front_parts else text[:60]

            cards.append({
                'deck_name': deck_name or 'Unknown',
                'author': author,
                'front': front,
                'back': text,
                'source_ref': current_chapter,
            })
            continue  # already incremented i

        i += 1

    return cards


# ---------------------------------------------------------------------------
# 밀리의서재 share text
# ---------------------------------------------------------------------------

def parse_millie_text(content: str) -> list:
    """Parse 밀리의서재 shared highlight text (variable format)."""
    cards = []

    # Try to detect book title from first non-empty line
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    deck_name = lines[0] if lines else '알 수 없는 책'
    author = ''

    # Split into paragraphs
    paragraphs = re.split(r'\n{2,}', content.strip())

    for para in paragraphs:
        para = para.strip()
        if not para or len(para) < 15:
            continue

        # Extract page reference
        page_match = re.search(r'p\.?\s*(\d+)', para, re.IGNORECASE)
        source_ref = f"p.{page_match.group(1)}" if page_match else ''

        # Remove the page reference from the text for back
        back_text = re.sub(r'p\.?\s*\d+', '', para).strip(' \t\n.,')
        if len(back_text) < 10:
            continue

        front = f'"{deck_name}"'
        if source_ref:
            front += f' | {source_ref}'

        cards.append({
            'deck_name': deck_name,
            'author': author,
            'front': front,
            'back': back_text,
            'source_ref': source_ref,
        })

    return cards


# ---------------------------------------------------------------------------
# PDF (requires PyPDF2)
# ---------------------------------------------------------------------------

def _normalize_whitespace(text: str) -> str:
    return re.sub(r'\s+', ' ', (text or '')).strip()


def _clean_pdf_title(title: str) -> str:
    title = _normalize_whitespace(title or '')
    title = title.strip('"\'')
    if title.lower().endswith('.pdf'):
        title = title[:-4].strip()
    if len(title) < 2:
        return ''

    generic_titles = {
        'untitled',
        'document',
        'pdf document',
        'microsoft word',
        'microsoft powerpoint',
        'powerpoint presentation',
        'untitled document',
    }
    if title.lower() in generic_titles:
        return ''
    return title


def _title_from_filename(filename: str) -> str:
    stem = os.path.splitext(os.path.basename(filename or ''))[0]
    stem = _normalize_whitespace(re.sub(r'[_\-]+', ' ', stem))
    return _clean_pdf_title(stem)


def _title_from_first_page(first_page_text: str) -> str:
    lines = [_normalize_whitespace(line) for line in (first_page_text or '').splitlines()]
    lines = [line for line in lines if line]
    for line in lines[:12]:
        low = line.lower()
        if len(line) < 4 or len(line) > 120:
            continue
        if re.fullmatch(r'\d+', line):
            continue
        if low.startswith(('http://', 'https://', 'www.', 'isbn', 'copyright')):
            continue
        if sum(ch.isalpha() for ch in line) < 3:
            continue
        if line.endswith(('.', '?', '!')):
            continue
        return line
    return ''


def _extract_pdf_title(reader, first_page_text: str, filename: str) -> str:
    metadata = getattr(reader, 'metadata', None) or {}
    metadata_title = (
        metadata.get('/Title')
        or metadata.get('Title')
        or metadata.get('/title')
        or metadata.get('title')
    )
    title = _clean_pdf_title(str(metadata_title)) if metadata_title else ''
    if title:
        return title

    title = _clean_pdf_title(_title_from_first_page(first_page_text))
    if title:
        return title

    return _title_from_filename(filename) or 'PDF Document'


def _extract_pdf_author(reader) -> str:
    metadata = getattr(reader, 'metadata', None) or {}
    author = (
        metadata.get('/Author')
        or metadata.get('Author')
        or metadata.get('/author')
        or metadata.get('author')
        or ''
    )
    return _normalize_whitespace(str(author))


def parse_pdf_bytes(content, filename: str = '') -> list:
    """Extract text from all PDF pages and split into paragraph cards."""
    try:
        import io
        import PyPDF2  # noqa: F401 — optional dependency
        reader = PyPDF2.PdfReader(io.BytesIO(content))
        page_texts = []
        for page_num, page in enumerate(reader.pages, start=1):
            try:
                text = (page.extract_text() or '').strip()
            except Exception:
                text = ''
            page_texts.append((page_num, text))
    except Exception:
        return []

    first_page_text = next((text for _, text in page_texts if text), '')
    deck_name = _extract_pdf_title(reader, first_page_text, filename)
    author = _extract_pdf_author(reader)
    cards = []

    for page_num, page_text in page_texts:
        if not page_text:
            continue
        paragraphs = [p.strip() for p in re.split(r'\n{2,}', page_text) if p.strip()]
        for para in paragraphs:
            back_text = _normalize_whitespace(para)
            if len(back_text) < 30:
                continue
            source_ref = f'p.{page_num}'
            cards.append({
                'deck_name': deck_name,
                'author': author,
                'front': f'"{deck_name}" | {source_ref} | {back_text[:60]}…',
                'back': back_text,
                'source_ref': source_ref,
            })

    # Fallback for PDFs where paragraph boundaries are noisy but page text exists.
    if not cards:
        for page_num, page_text in page_texts:
            back_text = _normalize_whitespace(page_text)
            if len(back_text) < 30:
                continue
            source_ref = f'p.{page_num}'
            cards.append({
                'deck_name': deck_name,
                'author': author,
                'front': f'"{deck_name}" | {source_ref} | {back_text[:60]}…',
                'back': back_text,
                'source_ref': source_ref,
            })

    return cards
