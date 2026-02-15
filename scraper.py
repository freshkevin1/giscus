import logging
import re
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

ARTICLE_URL_RE = re.compile(r"/news/[^/]+/\d{5,}")


def _get_recent_weekday():
    """Return the most recent weekday (Mon-Fri) as YYYYMMDD string."""
    d = date.today()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


def _extract_from_page(url, default_section=""):
    """Extract articles from a single MK page.

    Tries two patterns:
    1. li.news_node containing h3.news_ttl and a.link (today-paper, headline)
    2. a[href] wrapping h3.news_ttl (ranking page)
    """
    articles = []
    seen_urls = set()

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("Failed to fetch %s: %s", url, e)
        return articles

    soup = BeautifulSoup(resp.text, "html.parser")

    # Pattern 1: li.news_node > a.link + h3.news_ttl
    for node in soup.select("li.news_node"):
        title_el = node.select_one("h3.news_ttl")
        link_el = node.select_one("a.link") or node.select_one("a")

        if not title_el or not link_el:
            continue

        # Remove reporter name span before extracting title
        writing_span = title_el.select_one("span.writing")
        if writing_span:
            writing_span.decompose()
        title = title_el.get_text(strip=True)
        href = link_el.get("href", "")

        if not title or not ARTICLE_URL_RE.search(href):
            continue

        if href.startswith("/"):
            href = "https://www.mk.co.kr" + href

        if href in seen_urls:
            continue
        seen_urls.add(href)

        section = default_section
        cate_parent = node.find_parent("li", class_="cate_page_node")
        if cate_parent:
            cate_el = cate_parent.select_one("em.cate")
            if cate_el:
                section = cate_el.get_text(strip=True)

        articles.append({"title": title, "url": href, "section": section})

    # Pattern 2: a[href] > h3.news_ttl (ranking page style)
    if not articles:
        for a_tag in soup.find_all("a", href=ARTICLE_URL_RE):
            title_el = a_tag.select_one("h3.news_ttl")
            if not title_el:
                continue

            title = title_el.get_text(strip=True)
            href = a_tag.get("href", "")

            if not title or href in seen_urls:
                continue

            if href.startswith("/"):
                href = "https://www.mk.co.kr" + href

            seen_urls.add(href)
            articles.append({"title": title, "url": href, "section": default_section})

    logger.info("Extracted %d articles from %s", len(articles), url)
    return articles


def scrape_mk_today():
    """Scrape today's articles from 매일경제.

    Sources:
    1. today-paper (uses most recent weekday if today is weekend)
    2. ranking page (인기뉴스)

    Returns a list of dicts with keys: title, url, section.
    """
    all_articles = []
    seen_urls = set()

    weekday_date = _get_recent_weekday()
    sources = [
        (f"https://www.mk.co.kr/today-paper?date={weekday_date}", "오늘의 매경"),
    ]

    for url, section in sources:
        articles = _extract_from_page(url, section)
        for a in articles:
            if a["url"] not in seen_urls:
                seen_urls.add(a["url"])
                all_articles.append(a)

    logger.info("Total scraped: %d unique articles", len(all_articles))
    return all_articles


def scrape_irobotnews():
    """Scrape articles from 로봇신문 (irobotnews.com).

    Returns a list of dicts with keys: title, url, section.
    """
    url = "https://www.irobotnews.com/news/articleList.html?view_type=sm"
    articles = []
    seen_urls = set()

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("Failed to fetch irobotnews: %s", e)
        return articles

    soup = BeautifulSoup(resp.text, "html.parser")

    for item in soup.select("#section-list li.altlist-webzine-item"):
        link_el = item.select_one("h2.altlist-subject > a")
        if not link_el:
            continue

        title = link_el.get_text(strip=True)
        href = link_el.get("href", "")

        if not title or not href:
            continue

        if href.startswith("/"):
            href = "https://www.irobotnews.com" + href

        if href in seen_urls:
            continue
        seen_urls.add(href)

        # Extract category from metadata
        section = ""
        info_items = item.select("div.altlist-info-item")
        if info_items:
            section = info_items[0].get_text(strip=True)

        articles.append({"title": title, "url": href, "section": section})

    logger.info("Scraped %d articles from irobotnews", len(articles))
    return articles


def scrape_robotreport():
    """Scrape articles from The Robot Report.

    Returns a list of dicts with keys: title, url, section.
    """
    url = "https://www.therobotreport.com/category/news/"
    articles = []
    seen_urls = set()

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("Failed to fetch robotreport: %s", e)
        return articles

    soup = BeautifulSoup(resp.text, "html.parser")

    for article_tag in soup.select("article"):
        link_el = article_tag.select_one("h2 a.entry-title-link")
        if not link_el:
            link_el = article_tag.select_one("h2 a") or article_tag.select_one("h3 a")
        if not link_el:
            continue

        title = link_el.get_text(strip=True)
        href = link_el.get("href", "")

        if not title or not href:
            continue

        if href in seen_urls:
            continue
        seen_urls.add(href)

        articles.append({"title": title, "url": href, "section": ""})

    logger.info("Scraped %d articles from robotreport", len(articles))
    return articles


def scrape_anthropic():
    """Scrape articles from Anthropic research blog.

    Returns a list of dicts with keys: title, url, section.
    """
    url = "https://www.anthropic.com/research"
    articles = []
    seen_urls = set()

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("Failed to fetch anthropic: %s", e)
        return articles

    soup = BeautifulSoup(resp.text, "html.parser")

    # Featured articles
    for a_tag in soup.select("a[class*='FeaturedGrid']"):
        title_el = a_tag.select_one("h2") or a_tag.select_one("h4")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        href = a_tag.get("href", "")
        if not title or not href:
            continue
        if href.startswith("/"):
            href = "https://www.anthropic.com" + href
        if href not in seen_urls:
            seen_urls.add(href)
            articles.append({"title": title, "url": href, "section": "Anthropic"})

    # Publication list articles
    for a_tag in soup.select("a[class*='PublicationList']"):
        title_el = a_tag.select_one("span[class*='title']")
        if not title_el:
            title_el = a_tag.select_one("h3") or a_tag.select_one("h4")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        href = a_tag.get("href", "")
        if not title or not href:
            continue
        if href.startswith("/"):
            href = "https://www.anthropic.com" + href
        if href not in seen_urls:
            seen_urls.add(href)
            articles.append({"title": title, "url": href, "section": "Anthropic"})

    logger.info("Scraped %d articles from anthropic", len(articles))
    return articles


def scrape_deepmind():
    """Scrape articles from Google DeepMind blog.

    Returns a list of dicts with keys: title, url, section.
    """
    url = "https://deepmind.google/discover/blog/"
    articles = []
    seen_urls = set()

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("Failed to fetch deepmind: %s", e)
        return articles

    soup = BeautifulSoup(resp.text, "html.parser")

    for card in soup.select("article.card-blog"):
        title_el = card.select_one("h3")
        link_el = card.select_one("a[href]")
        if not title_el or not link_el:
            continue
        title = title_el.get_text(strip=True)
        href = link_el.get("href", "")
        if not title or not href:
            continue
        if href.startswith("/"):
            href = "https://deepmind.google" + href
        if href not in seen_urls:
            seen_urls.add(href)
            articles.append({"title": title, "url": href, "section": "DeepMind"})

    logger.info("Scraped %d articles from deepmind", len(articles))
    return articles


def scrape_meta_ai():
    """Scrape articles from Meta AI blog.

    Returns a list of dicts with keys: title, url, section.
    """
    url = "https://ai.meta.com/blog/"
    articles = []
    seen_urls = set()

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("Failed to fetch meta ai: %s", e)
        return articles

    soup = BeautifulSoup(resp.text, "html.parser")

    for a_tag in soup.select("a[href*='/blog/']"):
        text = a_tag.get_text(strip=True)
        href = a_tag.get("href", "")
        if len(text) <= 10 or not href:
            continue
        # Ensure absolute URL
        if href.startswith("/"):
            href = "https://ai.meta.com" + href
        if href in seen_urls:
            continue
        seen_urls.add(href)
        articles.append({"title": text, "url": href, "section": "Meta AI"})

    logger.info("Scraped %d articles from meta ai", len(articles))
    return articles


def scrape_ai_companies():
    """Scrape articles from Anthropic, DeepMind, and Meta AI.

    Returns a combined list of dicts with keys: title, url, section.
    """
    all_articles = []
    all_articles.extend(scrape_anthropic())
    all_articles.extend(scrape_deepmind())
    all_articles.extend(scrape_meta_ai())
    logger.info("Total AI companies articles: %d", len(all_articles))
    return all_articles
