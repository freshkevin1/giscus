"""Utility module for Claude-powered book recommendations."""

import json
import logging
import re
from collections import Counter

logger = logging.getLogger(__name__)


def _extract_year(date_read):
    """Extract year from date_read string (e.g. '2024/01/15' or '2024-01-15')."""
    if not date_read:
        return None
    match = re.search(r"((?:19|20)\d{2})", date_read)
    return int(match.group(1)) if match else None


def build_reader_profile(books):
    """Build a statistical summary of the reader's library."""
    read_books = [b for b in books if b.shelf == "read"]
    total = len(read_books)
    if total == 0:
        return ""

    # Rating distribution
    rating_counts = Counter(b.my_rating for b in read_books if b.my_rating > 0)
    rating_dist = " ".join(
        f"\u2605{r}({rating_counts[r]})" for r in sorted(rating_counts, reverse=True)
    )

    # Favorite authors (3+ books read)
    author_counts = Counter(b.author for b in read_books if b.author)
    fav_authors = sorted(
        [(a, c) for a, c in author_counts.items() if c >= 3],
        key=lambda x: x[1],
        reverse=True,
    )
    fav_str = ", ".join(f"{a}({c})" for a, c in fav_authors) if fav_authors else "None"

    # Reading period
    years = [y for b in read_books if (y := _extract_year(b.date_read))]
    period = f"{min(years)}\u2013{max(years)}" if years else "Unknown"

    lines = [
        "## Reader Profile Analysis",
        f"- Total books read: {total}",
    ]
    if rating_dist:
        lines.append(f"- Rating distribution: {rating_dist}")
    lines.append(f"- Favorite authors (3+ books): {fav_str}")
    lines.append(f"- Reading period: {period}")
    return "\n".join(lines)


def build_book_sections(books):
    """Classify books into sections: Hall of Fame, Highly Rated, Other."""
    read_books = [b for b in books if b.shelf == "read"]

    hof = [b for b in read_books if b.hall_of_fame]
    highly_rated = [b for b in read_books if not b.hall_of_fame and b.my_rating >= 4]
    others = [b for b in read_books if not b.hall_of_fame and b.my_rating < 4]

    # Sort each group by rating desc, then title
    highly_rated.sort(key=lambda b: (-b.my_rating, b.title))
    others.sort(key=lambda b: (-b.my_rating, b.title))

    lines = []

    if hof:
        lines.append("\n## Hall of Fame (All-time Favorites)")
        for b in hof:
            year = _extract_year(b.date_read)
            year_str = f", {year}" if year else ""
            rating_str = f"{b.my_rating}/5" if b.my_rating > 0 else "unrated"
            lines.append(f'- "{b.title}" by {b.author} ({rating_str}{year_str})')

    if highly_rated:
        lines.append("\n## Highly Rated (\u26054-5)")
        for b in highly_rated:
            year = _extract_year(b.date_read)
            year_str = f", {year}" if year else ""
            lines.append(f'- "{b.title}" by {b.author} ({b.my_rating}/5{year_str})')

    if others:
        lines.append("\n## Other Books Read")
        for b in others:
            year = _extract_year(b.date_read)
            year_str = f", {year}" if year else ""
            if b.my_rating > 0:
                lines.append(f'- "{b.title}" by {b.author} ({b.my_rating}/5{year_str})')
            else:
                lines.append(f'- "{b.title}" by {b.author} (unrated{year_str})')

    return "\n".join(lines)


def _normalize_title(title):
    """Normalize a book title for fuzzy comparison.

    Lowercases, removes punctuation, and strips common articles.
    """
    t = title.lower()
    # Remove punctuation
    t = re.sub(r"[^\w\s]", "", t)
    # Remove leading articles
    t = re.sub(r"^(the|a|an)\s+", "", t)
    # Collapse whitespace
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _extract_last_name(author):
    """Extract the last name from an author string."""
    if not author:
        return ""
    # Handle "Last, First" format
    if "," in author:
        return author.split(",")[0].strip().lower()
    # Handle "First Last" format
    parts = author.strip().split()
    return parts[-1].lower() if parts else ""


def _core_title(title):
    """Extract core title before subtitle separators (colon, dash)."""
    # Split on ": " or " - " and take the first part
    part = re.split(r"\s*[:]\s*|\s+[-—–]\s+", title)[0]
    return _normalize_title(part)


def _is_duplicate(rec_title, rec_author, existing_books):
    """Check if a recommended book is a duplicate of any existing book.

    Uses normalized title comparison and author last-name matching.
    """
    norm_rec = _normalize_title(rec_title)
    core_rec = _core_title(rec_title)
    rec_last = _extract_last_name(rec_author)

    for book in existing_books:
        norm_existing = _normalize_title(book.title)

        # Exact normalized title match → duplicate
        if norm_rec == norm_existing:
            return True

        # Same author last name → check title similarity
        if rec_last and _extract_last_name(book.author) == rec_last:
            # One full title contains the other
            if norm_rec in norm_existing or norm_existing in norm_rec:
                return True
            # Core titles (before subtitle) match
            core_existing = _core_title(book.title)
            if core_rec and core_existing and core_rec == core_existing:
                return True

    return False


def generate_recommendations(books, num_recommendations=10):
    """Use Claude API to generate book recommendations based on the user's library.

    Args:
        books: list of MyBook model instances
        num_recommendations: how many books to recommend

    Returns:
        list of dicts with keys: title, author, reason, category
    """
    from openai import OpenAI

    # Build structured prompt
    profile = build_reader_profile(books)
    sections = build_book_sections(books)

    # Build exclusion list for prompt
    exclusion_lines = []
    for b in books:
        author_str = b.author if b.author else "Unknown"
        exclusion_lines.append(f"- {b.title} — {author_str}")
    exclusion_section = "\n".join(exclusion_lines)

    # Request extra books to compensate for filtering
    request_count = num_recommendations + 5

    user_prompt = (
        f"Here is a reader's book library:\n\n"
        f"{profile}\n"
        f"{sections}\n\n"
        f"## EXCLUSION LIST (절대 추천 금지)\n"
        f"The following books are already in the reader's library. "
        f"Do NOT recommend any of these books or different editions/translations of them:\n"
        f"{exclusion_section}\n\n"
        f"Based on this reader's taste, recommend exactly {request_count} books "
        "they would love. Do NOT recommend any book already in their library, "
        "including variant titles or different editions.\n"
        "Respond with ONLY a JSON array, no markdown fences, no extra text:\n"
        '[{"title": "...", "author": "...", "reason": "...", "category": "..."}]'
    )

    system_prompt = (
        "You are a book recommendation specialist. Analyze the reader's profile carefully before making recommendations.\n\n"
        "Guidelines:\n"
        "- Hall of Fame books represent this reader's all-time favorites. Prioritize recommending books with similar depth, style, and themes.\n"
        "- Consider other works by the reader's favorite authors (authors with 3+ books read).\n"
        '- Use specific, granular categories (e.g. "behavioral economics", "leadership", "evolutionary biology", "Korean modern literature") '
        'instead of broad ones (e.g. "business", "self-help", "science", "fiction").\n'
        "- Write the reason field in Korean (한국어).\n"
        "- Provide diverse recommendations across different categories while staying aligned with the reader's demonstrated preferences.\n"
        "- CRITICAL: Never recommend any book from the EXCLUSION LIST. This includes variant titles, subtitles, or different editions of the same work."
    )

    client = OpenAI()  # reads OPENAI_API_KEY from env
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=2048,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )

    # Parse response
    response_text = response.choices[0].message.content.strip()
    # Remove markdown fences if present
    response_text = re.sub(r"^```(?:json)?\s*", "", response_text)
    response_text = re.sub(r"\s*```$", "", response_text)

    recommendations = json.loads(response_text)

    # Filter out any books already in library using fuzzy matching
    filtered = []
    for rec in recommendations:
        title = rec.get("title", "")
        author = rec.get("author", "")
        if not _is_duplicate(title, author, books):
            filtered.append({
                "title": title,
                "author": author,
                "reason": rec.get("reason", ""),
                "category": rec.get("category", ""),
            })

    return filtered[:num_recommendations]
