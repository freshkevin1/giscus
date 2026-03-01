"""Utility module for Claude-powered book recommendations."""

import json
import logging
import re
from collections import Counter

import anthropic

logger = logging.getLogger(__name__)

_REC_MARKER = "[REC]"


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

    # Want-to-read section
    want_to_read = [b for b in books if b.shelf == "want-to-read"]
    if want_to_read:
        lines.append("\n## Want to Read (읽고 싶은 책)")
        for b in want_to_read:
            lines.append(f'- "{b.title}" by {b.author}')

    return "\n".join(lines)


def build_saved_books_section(saved_books):
    """Format saved/wishlisted books as a context section for the AI."""
    if not saved_books:
        return ""
    lines = ["\n## Wishlist / Saved Books (찜한 책)"]
    for b in saved_books:
        cat = f" [{b.category}]" if b.category else ""
        lines.append(f'- "{b.title}" by {b.author}{cat}')
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
    part = re.split(r"\s*[:]\s*|\s+[-\u2014\u2013]\s+", title)[0]
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

        # Exact normalized title match -> duplicate
        if norm_rec == norm_existing:
            return True

        # Same author last name -> check title similarity
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
    # Build structured prompt
    profile = build_reader_profile(books)
    sections = build_book_sections(books)

    # Build exclusion list for prompt
    exclusion_lines = []
    for b in books:
        author_str = b.author if b.author else "Unknown"
        exclusion_lines.append(f"- {b.title} \u2014 {author_str}")
    exclusion_section = "\n".join(exclusion_lines)

    # Request extra books to compensate for filtering
    request_count = num_recommendations + 5

    user_prompt = (
        f"Here is a reader's book library:\n\n"
        f"{profile}\n"
        f"{sections}\n\n"
        f"## EXCLUSION LIST (\uc808\ub300 \ucd94\ucc9c \uae08\uc9c0)\n"
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
        "- Write the reason field in Korean (\ud55c\uad6d\uc5b4).\n"
        "- Provide diverse recommendations across different categories while staying aligned with the reader's demonstrated preferences.\n"
        "- CRITICAL: Never recommend any book from the EXCLUSION LIST. This includes variant titles, subtitles, or different editions of the same work."
    )

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        system=system_prompt,
        messages=[
            {"role": "user", "content": user_prompt},
        ],
    )

    # Parse response
    raw = response.content[0].text
    if not raw:
        raise ValueError(f"Claude returned empty content. stop_reason={response.stop_reason}")
    response_text = raw.strip()
    # Remove markdown fences if present
    response_text = re.sub(r"^```(?:json)?\s*", "", response_text)
    response_text = re.sub(r"\s*```$", "", response_text)

    parsed = json.loads(response_text)
    recommendations = parsed if isinstance(parsed, list) else parsed.get("recommendations", parsed.get("books", []))

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


def chat_recommendation(user_message, conversation_history, books, saved_books=None):
    """Interactive chat-based book recommendation using Claude API.

    Args:
        user_message: the user's current message
        conversation_history: list of {"role": "user"|"assistant", "content": "..."}
        books: list of MyBook model instances
        saved_books: list of SavedBook model instances (optional)

    Returns:
        dict with keys: message (str), recommendations (list of dicts)
    """
    profile = build_reader_profile(books)
    sections = build_book_sections(books)
    saved_section = build_saved_books_section(saved_books) if saved_books else ""

    system_prompt = (
        "You are a world-class book recommendation assistant — friendly, deeply knowledgeable, "
        "and passionate about connecting readers with transformative books.\n\n"
        "You have access to the reader's complete book library, reading history, and wishlist.\n\n"
        f"{profile}\n"
        f"{sections}\n"
        f"{saved_section}\n\n"
        "## Recommendation Philosophy\n"
        "- **Prioritize truly great books**: award winners (Pulitzer, Nobel, Booker, National Book Award), "
        "timeless classics, and modern masterpieces that have stood the test of time.\n"
        "- **Go beyond obvious bestsellers** — surface hidden gems, international literature, "
        "and critically acclaimed works the reader may not have encountered.\n"
        "- Recommend books with real depth, substance, and lasting impact.\n"
        "- Balance between the reader's demonstrated taste and expanding their horizons.\n\n"
        "## Context Rules\n"
        "- Books in the reader's library (read/want-to-read): Do NOT recommend these.\n"
        "- Wishlist/Saved books (찜한 책): Do NOT re-recommend these. Use them as taste signals.\n"
        "- Base your recommendations on the reader's demonstrated preferences from their library.\n\n"
        "## Your Role\n"
        "- Answer questions about books, reading, and provide personalized recommendations.\n"
        "- Always respond in Korean (한국어).\n"
        "- Be conversational, insightful, and helpful.\n\n"
        "## Response Format\n"
        "When you recommend specific books, include them at the END of your response "
        f"after the marker '{_REC_MARKER}' as a JSON array. Example:\n\n"
        "말씀하신 주제에 딱 맞는 책들을 추천드립니다!\n\n"
        f"{_REC_MARKER}\n"
        '[{"title": "Book Title", "author": "Author Name", "reason": "추천 이유", "category": "category"}]\n\n'
        "If your response is just conversational (no book recommendations), do NOT include the marker.\n"
        '- Use specific categories (e.g. "behavioral economics", "leadership", "Korean modern literature") '
        'instead of broad ones (e.g. "business", "fiction").'
    )

    # Build messages list from history + current message
    messages = []
    for msg in conversation_history:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_message})

    # Prompt Caching: mark last assistant message as cache breakpoint
    # so the conversation prefix is cached across consecutive turns
    last_assistant_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i]["role"] == "assistant":
            last_assistant_idx = i
            break

    if last_assistant_idx is not None:
        prior_content = messages[last_assistant_idx]["content"]
        messages[last_assistant_idx]["content"] = [
            {
                "type": "text",
                "text": prior_content,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=messages,
    )

    raw = response.content[0].text or ""

    # Parse: split on [REC] marker
    if _REC_MARKER in raw:
        parts = raw.split(_REC_MARKER, 1)
        message_text = parts[0].strip()
        rec_text = parts[1].strip()
        # Remove markdown fences if present
        rec_text = re.sub(r"^```(?:json)?\s*", "", rec_text)
        rec_text = re.sub(r"\s*```$", "", rec_text)
        try:
            recommendations = json.loads(rec_text)
            if not isinstance(recommendations, list):
                recommendations = recommendations.get("recommendations", [])
        except (json.JSONDecodeError, AttributeError):
            logger.warning("Failed to parse recommendations JSON: %s", rec_text[:200])
            recommendations = []
    else:
        message_text = raw.strip()
        recommendations = []

    # Sanitize recommendations
    clean_recs = []
    for rec in recommendations:
        clean_recs.append({
            "title": rec.get("title", ""),
            "author": rec.get("author", ""),
            "reason": rec.get("reason", ""),
            "category": rec.get("category", ""),
        })

    return {
        "message": message_text,
        "recommendations": clean_recs,
    }


# ─────────────────────────────────────────────
# My Screens recommendation functions
# ─────────────────────────────────────────────

def build_viewer_profile(screens):
    """Build a statistical summary of the viewer's watch history."""
    watched = [s for s in screens if s.shelf == "watched"]
    total = len(watched)
    if total == 0:
        return ""

    rating_counts = Counter(s.my_rating for s in watched if s.my_rating > 0)
    rating_dist = " ".join(
        f"\u2605{r}({rating_counts[r]})" for r in sorted(rating_counts, reverse=True)
    )

    movie_count = sum(1 for s in watched if s.media_type == "movie")
    tv_count = sum(1 for s in watched if s.media_type == "tv")

    genre_tokens = []
    for s in watched:
        if s.genres:
            genre_tokens.extend(g.strip() for g in s.genres.split(",") if g.strip())
    top_genres = [g for g, _ in Counter(genre_tokens).most_common(5)]

    lines = [
        "## Viewer Profile Analysis",
        f"- Total watched: {total} (Movies: {movie_count}, TV: {tv_count})",
    ]
    if rating_dist:
        lines.append(f"- Rating distribution: {rating_dist}")
    if top_genres:
        lines.append(f"- Top genres: {', '.join(top_genres)}")
    return "\n".join(lines)


def build_screen_sections(screens):
    """Classify screens into sections: Hall of Fame, Highly Rated, Other."""
    watched = [s for s in screens if s.shelf == "watched"]

    hof = [s for s in watched if s.hall_of_fame]
    highly_rated = [s for s in watched if not s.hall_of_fame and s.my_rating >= 4]
    others = [s for s in watched if not s.hall_of_fame and s.my_rating < 4]

    highly_rated.sort(key=lambda s: (-s.my_rating, s.title))
    others.sort(key=lambda s: (-s.my_rating, s.title))

    def _fmt(s):
        type_tag = "영화" if s.media_type == "movie" else "드라마"
        year_str = f", {s.year}" if s.year else ""
        rating_str = f"{s.my_rating}/5" if s.my_rating > 0 else "unrated"
        genre_str = f" [{s.genres}]" if s.genres else ""
        return f'- "{s.title}" [{type_tag}{year_str}] ({rating_str}){genre_str}'

    lines = []
    if hof:
        lines.append("\n## Hall of Fame (All-time Favorites)")
        for s in hof:
            lines.append(_fmt(s))

    if highly_rated:
        lines.append("\n## Highly Rated (\u26054-5)")
        for s in highly_rated:
            lines.append(_fmt(s))

    if others:
        lines.append("\n## Other Watched")
        for s in others:
            lines.append(_fmt(s))

    want = [s for s in screens if s.shelf == "want-to-watch"]
    if want:
        lines.append("\n## Want to Watch (보고 싶은 콘텐츠)")
        for s in want:
            type_tag = "영화" if s.media_type == "movie" else "드라마"
            lines.append(f'- "{s.title}" [{type_tag}]')

    return "\n".join(lines)


def build_saved_screens_section(saved_screens):
    """Format saved/wishlisted screens as context for the AI."""
    if not saved_screens:
        return ""
    lines = ["\n## Wishlist / Saved Screens (찜한 콘텐츠)"]
    for s in saved_screens:
        cat = f" [{s.category}]" if s.category else ""
        type_tag = "영화" if s.media_type == "movie" else "드라마"
        lines.append(f'- "{s.title}" [{type_tag}]{cat}')
    return "\n".join(lines)


def generate_screen_recommendations(screens, num_recommendations=10):
    """Use Claude API to generate movie/TV recommendations based on the user's watch history.

    Args:
        screens: list of MyScreen model instances
        num_recommendations: how many items to recommend

    Returns:
        list of dicts with keys: title, media_type, reason, category
    """
    profile = build_viewer_profile(screens)
    sections = build_screen_sections(screens)

    exclusion_lines = []
    for s in screens:
        type_tag = "영화" if s.media_type == "movie" else "드라마"
        exclusion_lines.append(f"- {s.title} [{type_tag}]")
    exclusion_section = "\n".join(exclusion_lines)

    request_count = num_recommendations + 5

    user_prompt = (
        f"Here is a viewer's watch history:\n\n"
        f"{profile}\n"
        f"{sections}\n\n"
        f"## EXCLUSION LIST (절대 추천 금지)\n"
        f"Do NOT recommend any of these (including remakes/sequels/prequels of the same franchise):\n"
        f"{exclusion_section}\n\n"
        f"Based on this viewer's taste, recommend exactly {request_count} movies or TV shows "
        "they would love. Mix movies and TV dramas as appropriate.\n"
        "Respond with ONLY a JSON array, no markdown fences, no extra text:\n"
        '[{"title": "...", "media_type": "movie|tv", "reason": "...", "category": "..."}]'
    )

    system_prompt = (
        "You are a world-class movie and TV drama recommendation specialist. "
        "Analyze the viewer's watch history carefully before making recommendations.\n\n"
        "Guidelines:\n"
        "- Hall of Fame titles represent all-time favorites. Prioritize works with similar themes, style, and depth.\n"
        "- Consider K-dramas, international cinema, and acclaimed global content.\n"
        "- Use specific categories (e.g. '느와르 범죄', '로맨틱 코미디', 'K-드라마', '사이언스 픽션', "
        "'역사극', '다큐멘터리') instead of broad ones.\n"
        "- Write the reason field in Korean (한국어).\n"
        "- Provide diverse recommendations across genres while matching the viewer's taste.\n"
        "- media_type must be exactly 'movie' or 'tv'.\n"
        "- CRITICAL: Never recommend anything from the EXCLUSION LIST."
    )

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = response.content[0].text
    if not raw:
        raise ValueError(f"Claude returned empty content. stop_reason={response.stop_reason}")
    response_text = raw.strip()
    response_text = re.sub(r"^```(?:json)?\s*", "", response_text)
    response_text = re.sub(r"\s*```$", "", response_text)

    parsed = json.loads(response_text)
    recommendations = parsed if isinstance(parsed, list) else parsed.get("recommendations", [])

    # Filter duplicates by normalized title
    existing_titles = {_normalize_title(s.title) for s in screens}
    filtered = []
    for rec in recommendations:
        title = rec.get("title", "")
        if _normalize_title(title) not in existing_titles:
            filtered.append({
                "title": title,
                "media_type": rec.get("media_type", "movie"),
                "reason": rec.get("reason", ""),
                "category": rec.get("category", ""),
            })

    return filtered[:num_recommendations]


def chat_screen_recommendation(user_message, conversation_history, screens, saved_screens=None):
    """Interactive chat-based screen recommendation using Claude API.

    Args:
        user_message: the user's current message
        conversation_history: list of {"role": "user"|"assistant", "content": "..."}
        screens: list of MyScreen model instances
        saved_screens: list of SavedScreen model instances (optional)

    Returns:
        dict with keys: message (str), recommendations (list of dicts)
    """
    profile = build_viewer_profile(screens)
    sections = build_screen_sections(screens)
    saved_section = build_saved_screens_section(saved_screens) if saved_screens else ""

    system_prompt = (
        "You are a world-class movie and TV drama recommendation assistant — friendly, deeply knowledgeable, "
        "and passionate about connecting viewers with great content.\n\n"
        "You have access to the viewer's complete watch history and wishlist.\n\n"
        f"{profile}\n"
        f"{sections}\n"
        f"{saved_section}\n\n"
        "## Recommendation Philosophy\n"
        "- **Prioritize truly great content**: award winners (Oscar, Cannes, Baeksang, Emmy), "
        "critically acclaimed masterpieces, and modern classics.\n"
        "- **Include K-dramas and Korean cinema** — especially if the viewer shows interest.\n"
        "- Go beyond obvious mainstream picks — surface hidden gems and international cinema.\n"
        "- Recommend content with real depth, substance, and lasting impact.\n\n"
        "## Context Rules\n"
        "- Content in the viewer's history (watched/want-to-watch): Do NOT recommend these.\n"
        "- Wishlist/Saved content (찜한 콘텐츠): Do NOT re-recommend. Use as taste signals.\n\n"
        "## Your Role\n"
        "- Answer questions about movies/dramas and provide personalized recommendations.\n"
        "- Always respond in Korean (한국어).\n"
        "- Be conversational, insightful, and enthusiastic.\n\n"
        "## Response Format\n"
        "When you recommend specific titles, include them at the END of your response "
        f"after the marker '{_REC_MARKER}' as a JSON array. Example:\n\n"
        "말씀하신 취향에 딱 맞는 작품들을 추천드립니다!\n\n"
        f"{_REC_MARKER}\n"
        '[{"title": "Title", "media_type": "movie", "reason": "추천 이유", "category": "category"}]\n\n'
        "If your response is just conversational (no recommendations), do NOT include the marker.\n"
        '- media_type must be exactly "movie" or "tv".\n'
        '- Use specific categories (e.g. "느와르 범죄", "K-드라마", "SF 스릴러") instead of broad ones.'
    )

    messages = []
    for msg in conversation_history:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_message})

    last_assistant_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i]["role"] == "assistant":
            last_assistant_idx = i
            break

    if last_assistant_idx is not None:
        prior_content = messages[last_assistant_idx]["content"]
        messages[last_assistant_idx]["content"] = [
            {
                "type": "text",
                "text": prior_content,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=messages,
    )

    raw = response.content[0].text or ""

    if _REC_MARKER in raw:
        parts = raw.split(_REC_MARKER, 1)
        message_text = parts[0].strip()
        rec_text = parts[1].strip()
        rec_text = re.sub(r"^```(?:json)?\s*", "", rec_text)
        rec_text = re.sub(r"\s*```$", "", rec_text)
        try:
            recommendations = json.loads(rec_text)
            if not isinstance(recommendations, list):
                recommendations = recommendations.get("recommendations", [])
        except (json.JSONDecodeError, AttributeError):
            logger.warning("Failed to parse screen recommendations JSON: %s", rec_text[:200])
            recommendations = []
    else:
        message_text = raw.strip()
        recommendations = []

    clean_recs = []
    for rec in recommendations:
        clean_recs.append({
            "title": rec.get("title", ""),
            "media_type": rec.get("media_type", "movie"),
            "reason": rec.get("reason", ""),
            "category": rec.get("category", ""),
        })

    return {
        "message": message_text,
        "recommendations": clean_recs,
    }
