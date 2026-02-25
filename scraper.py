import html as html_module
import json
import logging
import re
from datetime import date, datetime, timedelta

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
    base_url = "https://www.irobotnews.com"
    list_url = base_url + "/news/articleList.html?view_type=sm"
    articles = []
    seen_urls = set()

    irobot_headers = {
        **HEADERS,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": base_url + "/",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }

    session = requests.Session()
    try:
        session.get(base_url, headers=irobot_headers, timeout=15)
        resp = session.get(list_url, headers=irobot_headers, timeout=30)
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


def scrape_openai():
    """Scrape articles from OpenAI news via RSS feed.

    Returns a list of dicts with keys: title, url, section.
    """
    url = "https://openai.com/blog/rss.xml"
    articles = []

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("Failed to fetch openai rss: %s", e)
        return articles

    # Strip CDATA wrappers before parsing so html.parser returns clean text
    clean_xml = re.sub(r"<!\[CDATA\[(.*?)]]>", r"\1", resp.text, flags=re.DOTALL)
    soup = BeautifulSoup(clean_xml, "html.parser")

    for item in soup.find_all("item")[:30]:
        title_el = item.find("title")
        link_el = item.find("guid")
        if not title_el or not link_el:
            continue
        title = title_el.get_text(strip=True)
        href = link_el.get_text(strip=True)
        if not title or not href:
            continue
        articles.append({"title": title, "url": href, "section": "OpenAI"})

    logger.info("Scraped %d articles from openai", len(articles))
    return articles


def scrape_wsj_ai():
    """Scrape articles from WSJ Tech RSS feed.

    Returns a list of dicts with keys: title, url, section.
    """
    url = "https://feeds.content.dowjones.io/public/rss/RSSWSJD"
    articles = []

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("Failed to fetch wsj rss: %s", e)
        return articles

    # Use regex instead of BeautifulSoup — html.parser treats <link> as
    # self-closing (HTML5 void element), losing the URL text content.
    items = re.findall(r'<item>(.*?)</item>', resp.text, re.DOTALL)
    for item_xml in items:
        title_m = re.search(r'<title>(.*?)</title>', item_xml, re.DOTALL)
        link_m = re.search(r'<link\s*/?\s*>(.*?)(?:<|$)', item_xml)
        if not title_m or not link_m:
            continue
        title = html_module.unescape(title_m.group(1).strip())
        href = link_m.group(1).strip()
        if not title or not href:
            continue
        articles.append({"title": title, "url": href, "section": "WSJ"})
        if len(articles) >= 30:
            break

    logger.info("Scraped %d articles from wsj ai", len(articles))
    return articles


def scrape_nyt_tech():
    """Scrape articles from NYT Technology RSS feed.

    Returns a list of dicts with keys: title, url, section.
    """
    url = "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml"
    articles = []

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("Failed to fetch nyt rss: %s", e)
        return articles

    # Use regex instead of BeautifulSoup — html.parser treats <link> as
    # self-closing (HTML5 void element), losing the URL text content.
    items = re.findall(r'<item>(.*?)</item>', resp.text, re.DOTALL)
    for item_xml in items[:30]:
        title_m = re.search(r'<title>(.*?)</title>', item_xml, re.DOTALL)
        link_m = re.search(r'<link\s*/?\s*>(.*?)(?:<|$)', item_xml)
        if not title_m or not link_m:
            continue
        title = html_module.unescape(title_m.group(1).strip())
        href = link_m.group(1).strip()
        if not title or not href:
            continue
        articles.append({"title": title, "url": href, "section": "NYT"})

    logger.info("Scraped %d articles from nyt tech", len(articles))
    return articles


def _get_recent_sunday():
    """Return the most recent Sunday as YYYY-MM-DD string.

    Amazon Charts uses Sundays as week anchors. If the current Sunday's
    chart is not yet available (404), the caller should try the previous week.
    """
    today = date.today()
    days_since_sunday = (today.weekday() + 1) % 7
    return today - timedelta(days=days_since_sunday)


def scrape_amazon_charts():
    """Scrape Amazon Charts Most Read Nonfiction top 20.

    Returns a list of dicts with keys: rank, title, author, url, image_url.
    """
    articles = []

    amazon_headers = {
        **HEADERS,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }

    # Try this week's Sunday, then fall back to previous week
    sunday = _get_recent_sunday()
    resp = None
    for attempt in range(3):
        chart_date = (sunday - timedelta(weeks=attempt)).isoformat()
        url = f"https://www.amazon.com/charts/{chart_date}/mostread/nonfiction"
        try:
            resp = requests.get(url, headers=amazon_headers, timeout=30)
            if resp.status_code == 200:
                logger.info("Amazon Charts date: %s", chart_date)
                break
        except requests.RequestException as e:
            logger.error("Failed to fetch amazon charts (%s): %s", chart_date, e)
            continue
    else:
        logger.error("All Amazon Charts date attempts failed")
        return articles

    if not resp or resp.status_code != 200:
        return articles

    soup = BeautifulSoup(resp.text, "html.parser")

    seen_urls = set()
    for img in soup.select('img[alt^="Cover image of"]'):
        alt = img.get("alt", "")
        # alt format: "Cover image of Title by Author"
        match = re.match(r"Cover image of (.+?) by (.+)", alt)
        if not match:
            continue

        title = match.group(1).strip()
        author = match.group(2).strip()
        image_url = img.get("src", "")

        # Find parent <a> with /dp/ in href
        link_el = None
        for parent in img.parents:
            if parent.name == "a":
                href = parent.get("href", "")
                if "/dp/" in href:
                    link_el = parent
                    break

        if not link_el:
            continue

        href = link_el.get("href", "")
        if href.startswith("/"):
            href = "https://www.amazon.com" + href

        if href in seen_urls:
            continue
        seen_urls.add(href)

        # Extract rank from ref parameter: chrt_bk_rd_XX_N (fc=fiction, nf=nonfiction)
        rank_match = re.search(r"chrt_bk_rd_\w+_(\d+)", href)
        rank = int(rank_match.group(1)) if rank_match else len(articles) + 1

        articles.append({
            "rank": rank,
            "title": title,
            "author": author,
            "url": href,
            "image_url": image_url,
        })

    articles.sort(key=lambda x: x["rank"])
    logger.info("Scraped %d books from amazon charts", len(articles))
    return articles


def scrape_yes24_bestseller():
    """Scrape YES24 monthly bestseller top 30.

    Returns a list of dicts with keys: rank, title, author, url, image_url.
    """
    url = (
        "https://www.yes24.com/product/category/monthbestseller"
        "?categoryNumber=001&pageNumber=1&pageSize=30"
    )
    articles = []

    yes24_headers = {
        **HEADERS,
        "Accept-Language": "ko-KR,ko;q=0.9",
    }

    try:
        resp = requests.get(url, headers=yes24_headers, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("Failed to fetch yes24 bestseller: %s", e)
        return articles

    soup = BeautifulSoup(resp.text, "html.parser")

    for item in soup.select(".itemUnit")[:30]:
        rank_el = item.select_one("em.ico.rank")
        rank = int(rank_el.get_text(strip=True)) if rank_el else len(articles) + 1

        title_el = item.select_one("a.gd_name")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        href = title_el.get("href", "")
        if href.startswith("/"):
            href = "https://www.yes24.com" + href

        author_el = item.select_one(".info_auth a")
        author = author_el.get_text(strip=True) if author_el else ""

        img_el = item.select_one("img.lazy")
        image_url = ""
        if img_el:
            image_url = img_el.get("data-original", "") or img_el.get("src", "")

        articles.append({
            "rank": rank,
            "title": title,
            "author": author,
            "url": href,
            "image_url": image_url,
        })

    logger.info("Scraped %d books from yes24 bestseller", len(articles))
    return articles


WEEKLY_EDITION_RE = re.compile(r"^/weekly/(202[6-9]\d{2}|20[3-9]\d{3})$")


def scrape_geek_news_weekly():
    """Scrape articles from GeekNews Weekly (news.hada.io/weekly).

    Fetches editions from 2026 onwards and extracts all topic links.
    Returns a list of dicts with keys: title, url, section.
    """
    base_url = "https://news.hada.io"
    list_url = f"{base_url}/weekly"
    all_articles = []
    seen_urls = set()

    try:
        resp = requests.get(list_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("Failed to fetch geek news weekly list: %s", e)
        return all_articles

    soup = BeautifulSoup(resp.text, "html.parser")

    # Find edition links matching 2025+
    edition_links = []
    for a_tag in soup.find_all("a", href=WEEKLY_EDITION_RE):
        href = a_tag.get("href", "")
        if href not in [e[0] for e in edition_links]:
            edition_links.append((href, a_tag.get_text(strip=True)))

    logger.info("Found %d GeekNews Weekly editions (2026+)", len(edition_links))

    for edition_path, edition_title in edition_links:
        edition_url = base_url + edition_path
        try:
            resp = requests.get(edition_url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error("Failed to fetch edition %s: %s", edition_path, e)
            continue

        edition_soup = BeautifulSoup(resp.text, "html.parser")
        edition_id = edition_path.split("/")[-1]
        section = f"{edition_id} | {edition_title[:30]}" if edition_title else edition_id

        # Add edition page itself as an article
        if edition_url not in seen_urls:
            seen_urls.add(edition_url)
            all_articles.append({
                "title": f"\U0001f4cb {edition_title}" if edition_title else f"\U0001f4cb Weekly {edition_id}",
                "url": edition_url,
                "section": section,
            })

        for topic_a in edition_soup.select('a[href*="/topic?id="]'):
            title = topic_a.get_text(strip=True)
            href = topic_a.get("href", "")
            if not title or not href:
                continue
            full_url = href if href.startswith("http") else base_url + href
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)
            all_articles.append({"title": title, "url": full_url, "section": section})

    logger.info("Scraped %d articles from GeekNews Weekly", len(all_articles))
    return all_articles




BD_CUTOFF_DATE = date(2026, 2, 22)


def _bd_article_pub_date(url: str):
    """BD 기사/비디오/화이트페이퍼 페이지에서 JSON-LD datePublished(또는 uploadDate) 추출.

    Returns:
        date object if found, None otherwise.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("BD date fetch failed %s: %s", url, e)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            # Unwrap @graph arrays if present
            items = data.get("@graph", [data]) if isinstance(data, dict) else [data]
            for item in items:
                if isinstance(item, dict):
                    dp = item.get("datePublished") or item.get("uploadDate")
                    if dp:
                        return date.fromisoformat(str(dp)[:10])
        except (json.JSONDecodeError, ValueError, AttributeError, TypeError):
            continue
    return None


def _scrape_bd_listing_page(listing_url: str, slug_pattern: str, base_url: str, section: str) -> list:
    """범용 Boston Dynamics 리스팅 페이지 스크래퍼.

    날짜 필터 (BD_CUTOFF_DATE = 2026-02-22 기준):
    - pub_date > 컷오프 → 신규 기사: 전부 포함
    - pub_date <= 컷오프 → 구 기사: 최초 1개만 포함 후 STOP
    - pub_date None → 날짜 불명: 포함하고 계속 탐색

    리스팅 페이지는 newest-first 정렬을 가정.
    첫 번째 old 기사 발견 시 STOP → HTTP 요청 최소화.
    """
    articles = []
    seen_hrefs = set()

    try:
        resp = requests.get(listing_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("Failed to fetch BD listing %s: %s", listing_url, e)
        return articles

    soup = BeautifulSoup(resp.text, "html.parser")
    slug_re = re.compile(slug_pattern)

    # 기사 링크 추출 (순서 유지 — 리스팅 페이지는 newest-first)
    ordered_links = []
    for a_tag in soup.find_all("a", href=True):
        href = a_tag.get("href", "")
        if href.startswith("/"):
            href = base_url.rstrip("/") + href
        # 리스팅 페이지 자체, 쿼리스트링, 앵커 등 제외
        if not slug_re.search(href):
            continue
        if href in seen_hrefs:
            continue
        # 제목 추출: 내부 heading 우선, 없으면 링크 텍스트
        heading = a_tag.find(["h1", "h2", "h3", "h4"])
        title = heading.get_text(strip=True) if heading else a_tag.get_text(strip=True)
        if not title or len(title) < 5:
            continue
        seen_hrefs.add(href)
        ordered_links.append((href, title))

    # 날짜 필터 적용
    old_article_added = False
    for href, title in ordered_links:
        pub_date = _bd_article_pub_date(href)

        if pub_date is None:
            # 날짜 불명: 포함하고 계속
            articles.append({"title": title, "url": href, "section": section})
        elif pub_date > BD_CUTOFF_DATE:
            # 신규 기사: 포함
            articles.append({"title": title, "url": href, "section": section})
        else:
            # 구 기사: 첫 번째 1개만 포함 후 중단
            if not old_article_added:
                articles.append({"title": title, "url": href, "section": section})
                old_article_added = True
            break  # 이후 기사는 더 오래됐으므로 중단

    logger.info("BD %s: scraped %d articles from %s", section, len(articles), listing_url)
    return articles


def scrape_bostondynamics_blog() -> list:
    """Boston Dynamics 블로그 기사 스크래핑."""
    return _scrape_bd_listing_page(
        listing_url="https://bostondynamics.com/blog/",
        slug_pattern=r"bostondynamics\.com/blog/[^/?#]+",
        base_url="https://bostondynamics.com",
        section="Boston Dynamics Blog",
    )


def scrape_bostondynamics_videos() -> list:
    """Boston Dynamics 비디오 스크래핑."""
    return _scrape_bd_listing_page(
        listing_url="https://bostondynamics.com/videos/",
        slug_pattern=r"bostondynamics\.com/video/[^/?#]+",
        base_url="https://bostondynamics.com",
        section="Boston Dynamics Videos",
    )


def scrape_bostondynamics_whitepapers() -> list:
    """Boston Dynamics 화이트페이퍼 스크래핑."""
    return _scrape_bd_listing_page(
        listing_url="https://bostondynamics.com/resources/whitepaper/",
        slug_pattern=r"bostondynamics\.com/whitepaper/[^/?#]+",
        base_url="https://bostondynamics.com",
        section="Boston Dynamics Whitepaper",
    )


def scrape_figure_ai():
    """Scrape news articles from figure.ai/news via __NEXT_DATA__ JSON."""
    url = "https://www.figure.ai/news"
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except Exception as e:
        logger.warning("figure.ai fetch failed: %s", e)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    tag = soup.find("script", {"id": "__NEXT_DATA__"})
    if not tag:
        logger.warning("figure.ai: __NEXT_DATA__ not found")
        return []

    try:
        data = json.loads(tag.string)
        items = (
            data["props"]["pageProps"]["page"]
                ["sectionsCollection"]["items"][0]
                ["articlePageCollection"]["items"]
        )
    except (KeyError, IndexError, TypeError) as e:
        logger.warning("figure.ai: JSON path error: %s", e)
        return []

    articles = []
    for item in items:
        title = item.get("articleTitle", "").strip()
        slug  = item.get("slug", "").strip()
        pub_date = item.get("publicationDate", "")

        if not title or not slug:
            continue

        # 2026년 이후 기사만 포함
        if not pub_date.startswith("2026"):
            continue

        # 외부 링크가 있으면 우선 사용, 없으면 figure.ai URL 구성
        external_url = item.get("externalArticleUrl", "") or ""
        url = external_url.strip() if external_url.strip() else f"https://www.figure.ai/news/{slug}"

        articles.append({
            "title":   title,
            "url":     url,
            "section": "Figure AI",
        })

    logger.info("figure.ai: scraped %d articles", len(articles))
    return articles


def scrape_wirobotics():
    """Scrape news from WI Robotics via JSON API.

    Returns a list of dicts with keys: title, url, section.
    """
    articles = []
    seen_idx = set()

    old_article_added = False
    page = 1
    while True:
        api_url = f"https://www.wirobotics.com/media/newsList?pageType=01&page={page}"
        try:
            resp = requests.get(api_url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            logger.error("Failed to fetch wirobotics page %d: %s", page, e)
            break

        items = data.get("list", [])
        if not items:
            break

        for item in items:
            idx = item.get("idx")
            title = item.get("title", "").strip()
            reg_date_str = item.get("regDate", "")

            if not idx or not title or idx in seen_idx:
                continue
            seen_idx.add(idx)

            url = f"https://www.wirobotics.com/media/newsDetail?pageType=01&idx={idx}"

            # Parse date (YYYY.MM.DD)
            pub_date = None
            try:
                pub_date = date(*[int(x) for x in reg_date_str.split(".")])
            except (ValueError, TypeError):
                pass

            if pub_date is None:
                articles.append({"title": title, "url": url, "section": "WI Robotics"})
            elif pub_date > BD_CUTOFF_DATE:
                articles.append({"title": title, "url": url, "section": "WI Robotics"})
            else:
                if not old_article_added:
                    articles.append({"title": title, "url": url, "section": "WI Robotics"})
                    old_article_added = True
                break  # oldest-first within page, stop completely

        if old_article_added:
            break

        max_page = items[0].get("maxPage", 1) if items else 1
        if page >= max_page:
            break
        page += 1

    logger.info("Scraped %d articles from WI Robotics", len(articles))
    return articles


def scrape_agility_robotics():
    """Scrape press articles from Agility Robotics.

    Returns a list of dicts with keys: title, url, section.
    """
    url = "https://www.agilityrobotics.com/about/press"
    articles = []
    seen_urls = set()

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("Failed to fetch agility robotics: %s", e)
        return articles

    soup = BeautifulSoup(resp.text, "html.parser")

    old_article_added = False
    for block in soup.find_all("div", class_="div-block-47"):
        # Extract title
        title_el = block.find("div", class_="text-block-74")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        if not title:
            continue

        # Extract URL from first /content/ link
        link_el = block.find("a", href=lambda h: h and "/content/" in h)
        if not link_el:
            continue
        href = link_el.get("href", "")
        if href.startswith("/"):
            href = "https://www.agilityrobotics.com" + href
        if href in seen_urls:
            continue
        seen_urls.add(href)

        # Extract date
        date_el = block.find("div", class_="text-block-72")
        pub_date = None
        if date_el:
            try:
                pub_date = datetime.strptime(date_el.get_text(strip=True), "%B %d, %Y").date()
            except (ValueError, AttributeError):
                pass

        if pub_date is None:
            articles.append({"title": title, "url": href, "section": "Agility Robotics"})
        elif pub_date > BD_CUTOFF_DATE:
            articles.append({"title": title, "url": href, "section": "Agility Robotics"})
        else:
            if not old_article_added:
                articles.append({"title": title, "url": href, "section": "Agility Robotics"})
                old_article_added = True
            break

    logger.info("Scraped %d articles from Agility Robotics", len(articles))
    return articles


def scrape_ai_robotics_companies():
    """Aggregate news from AI and robotics companies."""
    all_articles = []
    # AI companies
    all_articles.extend(scrape_anthropic())
    all_articles.extend(scrape_deepmind())
    all_articles.extend(scrape_meta_ai())
    all_articles.extend(scrape_openai())
    # Robotics companies
    all_articles.extend(scrape_figure_ai())
    all_articles.extend(scrape_bostondynamics_blog())
    all_articles.extend(scrape_bostondynamics_videos())
    all_articles.extend(scrape_bostondynamics_whitepapers())
    # New
    all_articles.extend(scrape_wirobotics())
    all_articles.extend(scrape_agility_robotics())
    logger.info("Total AI & robotics companies articles: %d", len(all_articles))
    return all_articles


def scrape_deeplearning_batch():
    """Scrape weekly newsletter issues from DeepLearning.AI — The Batch.

    Returns a list of dicts with keys: title, url, section.
    """
    url = "https://www.deeplearning.ai/the-batch/"
    articles = []

    try:
        resp = requests.get(url, timeout=15, headers=HEADERS)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("Failed to fetch deeplearning batch: %s", e)
        return articles

    soup = BeautifulSoup(resp.text, "html.parser")

    for article in soup.find_all("article"):
        h2 = article.find("h2")
        a = article.find("a", href=lambda h: h and h.startswith("/the-batch/issue-"))
        if not h2 or not a:
            continue
        title = h2.get_text(strip=True)
        href = "https://www.deeplearning.ai" + a["href"]
        articles.append({"title": title, "url": href, "section": "The Batch"})

    logger.info("Scraped %d issues from DeepLearning.AI The Batch", len(articles))
    return articles


ACDEEPTECH_CUTOFF = date(2026, 2, 22)


def scrape_acdeeptech():
    """Scrape articles from ACDeepTech Substack archive.

    Date filter (cutoff = 2026-02-22):
    - After cutoff: include all
    - Before/on cutoff: max 10

    Returns a list of dicts with keys: title, url, section.
    """
    url = "https://acdeeptech.substack.com/archive"
    articles = []
    seen_urls = set()

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("Failed to fetch acdeeptech: %s", e)
        return articles

    soup = BeautifulSoup(resp.text, "html.parser")

    old_count = 0
    for time_el in soup.find_all("time", datetime=True):
        dt_str = time_el["datetime"][:10]
        try:
            pub_date = date.fromisoformat(dt_str)
        except ValueError:
            continue

        # Walk up to find container with /p/ link
        parent = time_el
        title = ""
        href = ""
        for _ in range(10):
            parent = parent.parent
            if parent is None:
                break
            for a_tag in parent.find_all("a", href=lambda h: h and "/p/" in h):
                txt = a_tag.get_text(strip=True)
                if len(txt) > 15:
                    title = txt
                    href = a_tag.get("href", "")
                    break
            if title:
                break

        if not title or not href or href in seen_urls:
            continue
        seen_urls.add(href)

        if pub_date > ACDEEPTECH_CUTOFF:
            articles.append({"title": title, "url": href, "section": "Deep Tech"})
        else:
            if old_count < 10:
                articles.append({"title": title, "url": href, "section": "Deep Tech"})
                old_count += 1

    logger.info("Scraped %d articles from ACDeepTech", len(articles))
    return articles


AITIMES_CUTOFF = date(2026, 2, 22)


def scrape_aitimes():
    """Scrape articles from AI타임스 (aitimes.com).

    Date filter (cutoff = 2026-02-22):
    - After cutoff: include all
    - Before/on cutoff: max 10

    Returns a list of dicts with keys: title, url, section.
    """
    url = "https://www.aitimes.com/news/articleList.html?view_type=sm"
    articles = []
    seen_urls = set()

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("Failed to fetch aitimes: %s", e)
        return articles

    soup = BeautifulSoup(resp.text, "html.parser")

    today = date.today()
    old_count = 0

    for item in soup.select("#section-list li.altlist-webzine-item"):
        link_el = item.select_one("h2.altlist-subject > a")
        if not link_el:
            continue

        title = link_el.get_text(strip=True)
        href = link_el.get("href", "")

        if not title or not href:
            continue

        if href.startswith("/"):
            href = "https://www.aitimes.com" + href

        if href in seen_urls:
            continue
        seen_urls.add(href)

        # Extract category from 1st info-item
        section = ""
        info_items = item.select("div.altlist-info-item")
        if info_items:
            section = info_items[0].get_text(strip=True)

        # Extract date from 3rd info-item (MM-DD HH:MM format, no year)
        pub_date = None
        if len(info_items) >= 3:
            date_text = info_items[2].get_text(strip=True)
            match = re.match(r"(\d{2})-(\d{2})", date_text)
            if match:
                month = int(match.group(1))
                day = int(match.group(2))
                try:
                    pub_date = date(today.year, month, day)
                    # If parsed date is in the future, assume previous year
                    if pub_date > today:
                        pub_date = date(today.year - 1, month, day)
                except ValueError:
                    pub_date = None

        if pub_date is None or pub_date > AITIMES_CUTOFF:
            articles.append({"title": title, "url": href, "section": section})
        else:
            if old_count < 10:
                articles.append({"title": title, "url": href, "section": section})
                old_count += 1

    logger.info("Scraped %d articles from aitimes", len(articles))
    return articles


def scrape_the_decoder():
    """Scrape latest articles from The Decoder via RSS feed.

    Returns up to 10 articles (most recent first) as a list of dicts
    with keys: title, url, section.
    """
    import xml.etree.ElementTree as ET

    feed_url = "https://the-decoder.com/feed/"
    articles = []

    try:
        resp = requests.get(feed_url, timeout=15, headers=HEADERS)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("Failed to fetch The Decoder RSS: %s", e)
        return articles

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        logger.error("Failed to parse The Decoder RSS XML: %s", e)
        return articles

    channel = root.find("channel")
    if channel is None:
        logger.error("The Decoder RSS: no <channel> element found")
        return articles

    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        articles.append({"title": title, "url": link, "section": "The Decoder"})
        if len(articles) >= 10:
            break

    logger.info("Scraped %d articles from The Decoder RSS", len(articles))
    return articles
