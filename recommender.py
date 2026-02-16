"""Utility module for Claude-powered book recommendations."""

import json
import logging
import re

logger = logging.getLogger(__name__)


def generate_recommendations(books, num_recommendations=10):
    """Use Claude API to generate book recommendations based on the user's library.

    Args:
        books: list of MyBook model instances
        num_recommendations: how many books to recommend

    Returns:
        list of dicts with keys: title, author, reason, category
    """
    import anthropic

    # Build the prompt from the user's book data
    rated_books = sorted(
        [b for b in books if b.my_rating > 0 and b.shelf == "read"],
        key=lambda b: b.my_rating,
        reverse=True,
    )
    unrated_books = [b for b in books if b.my_rating == 0 and b.shelf == "read"]
    lines = []
    lines.append("Here is a reader's book library:\n")

    if rated_books:
        lines.append("## Rated books (highest first):")
        for b in rated_books:
            lines.append(f'- "{b.title}" by {b.author} ({b.my_rating}/5)')

    if unrated_books:
        lines.append("\n## Read but unrated:")
        for b in unrated_books:
            lines.append(f'- "{b.title}" by {b.author} (read, unrated)')

    all_titles = {b.title.lower() for b in books}
    lines.append(f"\nBased on this reader's taste, recommend exactly {num_recommendations} books "
                 "they would love. Do NOT recommend any book already in their library. "
                 "Provide diverse recommendations across different categories.\n"
                 "Respond with ONLY a JSON array, no markdown fences, no extra text:\n"
                 '[{"title": "...", "author": "...", "reason": "...", "category": "..."}]')

    prompt_text = "\n".join(lines)

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    message = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt_text}],
    )

    # Parse response
    response_text = message.content[0].text.strip()
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
