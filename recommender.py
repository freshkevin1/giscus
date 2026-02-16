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

    user_prompt = (
        f"Here is a reader's book library:\n\n"
        f"{profile}\n"
        f"{sections}\n\n"
        f"Based on this reader's taste, recommend exactly {num_recommendations} books "
        "they would love. Do NOT recommend any book already in their library.\n"
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
        "- Provide diverse recommendations across different categories while staying aligned with the reader's demonstrated preferences."
    )

    all_titles = {b.title.lower() for b in books}

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

    # Filter out any books already in library
    filtered = []
    for rec in recommendations:
        if rec.get("title", "").lower() not in all_titles:
            filtered.append({
                "title": rec.get("title", ""),
                "author": rec.get("author", ""),
                "reason": rec.get("reason", ""),
                "category": rec.get("category", ""),
            })

    return filtered[:num_recommendations]
