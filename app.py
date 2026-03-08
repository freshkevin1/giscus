import anthropic
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urljoin, urlparse

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

# Load .env file if it exists (local development)
_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)
from flask import Flask, flash, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from flask_wtf.csrf import CSRFProtect, generate_csrf

from config import Config
import json

from models import AnkiCard, AnkiDeck, Article, ChatMessage, Compliment, ContactChatMessage, InsightKeyword, LoginLog, MyBook, MyScreen, NewsInsight, NotificationPreference, PushSubscription, ReadArticle, Recommendation, SavedBook, SavedScreen, ScreenChatMessage, User, db, init_default_user
from pywebpush import webpush, WebPushException
from recommender import chat_recommendation, chat_screen_recommendation, generate_recommendations
import requests as http_requests
from scraper import scrape_acdeeptech, scrape_ai_robotics_companies, scrape_aitimes, scrape_amazon_charts, scrape_deeplearning_batch, scrape_fieldai_news, scrape_geek_news_weekly, scrape_ifr_press_releases, scrape_irobotnews, scrape_mk_today, scrape_nyt_tech, scrape_robotreport, scrape_the_decoder, scrape_vention_press, scrape_wsj_ai, scrape_yes24_bestseller

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

NEWS_SOURCE_MAP = {
    'mk': '매일경제', 'irobot': '로봇신문', 'robotreport': 'Robot Report',
    'wsj_ai': 'WSJ', 'nyt_tech': 'NYT', 'ai_robotics': 'AI Companies',
    'geek_weekly': 'GeekNews Weekly', 'dl_batch': 'The Batch', 'the_decoder': 'The Decoder',
    'acdeeptech': 'Deep Tech',
    'aitimes': 'AI타임스',
    'fieldai': 'Field AI',
    'vention': 'Vention',
    'ifr_press': 'IFR Press',
}
NEWS_SOURCES = list(NEWS_SOURCE_MAP.keys())

app = Flask(__name__)
app.config.from_object(Config)
csrf = CSRFProtect(app)

app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 86400  # 1 day for static files
db.init_app(app)


def _client_ip_for_rate_limit():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return get_remote_address()


def _login_rate_limit_key():
    username = request.form.get("username", "").strip().lower()
    return f"{_client_ip_for_rate_limit()}:{username}"


limiter = Limiter(
    key_func=_client_ip_for_rate_limit,
    default_limits=[],
    storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"),
)
limiter.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = "로그인이 필요합니다."


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": generate_csrf}


@app.context_processor
def inject_push_config():
    return {'vapid_public_key': Config.VAPID_PUBLIC_KEY}


PASSWORD_EXPIRY_DAYS = 30
ADMIN_USERNAME = "tornadogrowth"

HABITS = ["아침 조깅/테니스/골프 + 스트레칭/명상"]
FAMILY_HABITS = ["아이와 놀기", "와이프 데이트"]

# --- Dashboard Cache (60s TTL, date-keyed) ---
_dashboard_cache = {"data": None, "ts": 0, "date_key": ""}
DASHBOARD_CACHE_TTL = 60

def _invalidate_dashboard_cache():
    _dashboard_cache["data"] = None
    _dashboard_cache["ts"] = 0
    _dashboard_cache["date_key"] = ""
_WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]


def admin_required(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if current_user.username != ADMIN_USERNAME:
            if request.path.startswith("/api/"):
                return jsonify({"error": "관리자 권한이 필요합니다."}), 403
            flash("관리자 권한이 필요합니다.", "danger")
            return redirect(url_for("index"))
        return func(*args, **kwargs)

    return wrapped


def _build_habit_date_sets(all_rows=None):
    """Build {habit_name: set(date_str)} in one O(n) pass."""
    if all_rows is None:
        from sheets import _get_all_habit_rows
        all_rows = _get_all_habit_rows()
    result = {}
    for row in all_rows:
        hname = row.get("habit_name", "")
        if hname:
            result.setdefault(hname, set()).add(row.get("logged_date", ""))
    return result


def _habit_stats(habit_name, logged_dates=None):
    today = date.today()
    if logged_dates is None:
        from sheets import _get_all_habit_rows
        all_rows = _get_all_habit_rows()
        logged_dates = {
            row["logged_date"]
            for row in all_rows
            if row.get("habit_name") == habit_name
        }
    days = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        done = d.isoformat() in logged_dates
        days.append({
            "date": d.strftime("%m/%d"),
            "weekday": _WEEKDAY_KO[d.weekday()],
            "done": done,
            "is_today": d == today,
        })
    streak = 0
    d = today
    while d.isoformat() in logged_dates:
        streak += 1
        d -= timedelta(days=1)
    total = len(logged_dates)
    today_done = days[-1]["done"]
    weekly_count = sum(1 for d in days if d["done"])
    one_month_ago = (today - timedelta(days=28)).isoformat()
    one_year_ago  = (today - timedelta(days=365)).isoformat()
    monthly_count = sum(1 for d in logged_dates if d >= one_month_ago)
    yearly_count  = sum(1 for d in logged_dates if d >= one_year_ago)
    monthly_avg = round(monthly_count / 4, 1)
    yearly_avg  = round(yearly_count / 52, 1)
    if logged_dates:
        last = max(logged_dates)
        days_since_last = (today - date.fromisoformat(last)).days
    else:
        last = None
        days_since_last = None
    return {"name": habit_name, "today_done": today_done, "streak": streak, "total": total, "days": days,
            "weekly_count": weekly_count, "monthly_avg": monthly_avg, "yearly_avg": yearly_avg,
            "last_date": last, "days_since_last": days_since_last,
            "yearly_count": yearly_count, "monthly_count": monthly_count}


@app.before_request
def enforce_password_change():
    if not current_user.is_authenticated:
        return
    session.permanent = True
    if request.endpoint in ("change_password", "logout", "static"):
        return

    changed_at = current_user.password_changed_at
    if changed_at is None or (datetime.utcnow() - changed_at).days >= PASSWORD_EXPIRY_DAYS:
        flash("보안을 위해 비밀번호를 변경해 주세요 (30일 경과).", "warning")
        return redirect(url_for("change_password"))


@app.after_request
def set_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    return response


@app.errorhandler(429)
def handle_rate_limit(_error):
    if request.path == "/login":
        flash("로그인 시도가 너무 많습니다. 잠시 후 다시 시도해 주세요.", "danger")
        return render_template("login.html"), 429
    if request.path.startswith("/api/"):
        return jsonify({"error": "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요."}), 429
    return "Too many requests", 429


# --- Background Recommendation Regeneration ---

def _regenerate_recommendations_background():
    """Regenerate recommendations in a background thread."""
    with app.app_context():
        try:
            books = MyBook.query.all()
            if not books:
                return
            recs = generate_recommendations(books)
            Recommendation.query.delete()
            for r in recs:
                db.session.add(Recommendation(
                    title=r["title"],
                    author=r["author"],
                    reason=r["reason"],
                    category=r["category"],
                ))
            db.session.commit()
            logger.info("Recommendations auto-regenerated (%d)", len(recs))
        except Exception as e:
            logger.error("Background recommendation regeneration failed: %s", e)


def auto_regenerate_recommendations():
    """Trigger recommendation regeneration in a background thread (non-blocking)."""
    thread = threading.Thread(target=_regenerate_recommendations_background, daemon=True)
    thread.start()


# --- Background Scraping ---

def _scrape_background(source):
    with app.app_context():
        try:
            count = run_scrape(source)
            logger.info("Background scrape for %s: %d new articles", source, count)
        except Exception as e:
            logger.error("Background scrape failed for %s: %s", source, e)


_auto_scrape_ts = {}
_AUTO_SCRAPE_INTERVAL = 1800  # 30 minutes


def auto_scrape(source):
    """Trigger scraping in a background thread, throttled to once per 30 min per source."""
    import time as _time
    now = _time.time()
    if now - _auto_scrape_ts.get(source, 0) < _AUTO_SCRAPE_INTERVAL:
        return
    _auto_scrape_ts[source] = now
    thread = threading.Thread(target=_scrape_background, args=(source,), daemon=True)
    thread.start()


# --- Scheduler ---

def scheduled_scrape():
    """Run scraping job within app context."""
    with app.app_context():
        run_scrape("mk")
        run_scrape("irobot")
        run_scrape("robotreport")
        run_scrape("ai_robotics")
        run_scrape("geek_weekly")
        run_scrape("dl_batch")
        run_scrape("the_decoder")
        run_scrape("wsj_ai")
        run_scrape("nyt_tech")
        run_scrape("acdeeptech")
        run_scrape("aitimes")
        run_scrape("fieldai")
        run_scrape("vention")
        run_scrape("ifr_press")
        run_scrape("bestseller")
        run_scrape("bestseller_kr")


def run_scrape(source="mk"):
    """Scrape articles for a given source and save to DB, enforcing per-source limit."""
    if source == "mk":
        articles = scrape_mk_today()
    elif source == "irobot":
        articles = scrape_irobotnews()
    elif source == "robotreport":
        articles = scrape_robotreport()
    elif source == "ai_robotics":
        articles = scrape_ai_robotics_companies()
    elif source == "geek_weekly":
        articles = scrape_geek_news_weekly()
    elif source == "dl_batch":
        articles = scrape_deeplearning_batch()
    elif source == "the_decoder":
        articles = scrape_the_decoder()
    elif source == "acdeeptech":
        articles = scrape_acdeeptech()
    elif source == "aitimes":
        articles = scrape_aitimes()
    elif source == "fieldai":
        articles = scrape_fieldai_news()
    elif source == "vention":
        articles = scrape_vention_press()
    elif source == "ifr_press":
        articles = scrape_ifr_press_releases()
    elif source == "wsj_ai":
        articles = scrape_wsj_ai()
    elif source == "nyt_tech":
        articles = scrape_nyt_tech()
    elif source == "bestseller":
        articles = scrape_amazon_charts()
    elif source == "bestseller_kr":
        articles = scrape_yes24_bestseller()
    else:
        return 0

    if not articles:
        logger.warning("No articles scraped for %s", source)
        return 0

    # Bestseller: replace all existing entries (weekly/monthly rotation)
    if source in ("bestseller", "bestseller_kr"):
        Article.query.filter_by(source=source).delete()
        db.session.commit()
        for a in articles:
            article = Article(
                title=a["title"],
                url=a["url"],
                source=source,
                section=str(a["rank"]),
                image_url=a.get("image_url", ""),
            )
            db.session.add(article)
        db.session.commit()
        logger.info("Replaced %s list with %d books", source, len(articles))
        return len(articles)

    # Batch-load existing URLs to avoid N+1 queries
    existing_read = {r.url for r in ReadArticle.query.with_entities(ReadArticle.url).all()}
    existing_art = {r.url for r in Article.query.filter_by(source=source).with_entities(Article.url).all()}

    count = 0
    for a in articles:
        if a["url"] in existing_read or a["url"] in existing_art:
            continue
        article = Article(
            title=a["title"],
            url=a["url"],
            source=source,
            section=a["section"],
        )
        db.session.add(article)
        count += 1

    db.session.commit()
    logger.info("Added %d new articles for %s", count, source)

    # Enforce retention policy: time-based (60 days) + count (2000)
    cutoff = datetime.now(timezone.utc) - timedelta(days=Config.MAX_ARTICLE_AGE_DAYS)
    expired = Article.query.filter(
        Article.source == source,
        Article.scraped_at < cutoff
    ).all()
    if expired:
        for old in expired:
            db.session.delete(old)
        db.session.commit()
        logger.info("Removed %d expired %s articles (older than %d days)",
                    len(expired), source, Config.MAX_ARTICLE_AGE_DAYS)

    total = Article.query.filter_by(source=source).count()
    if total > Config.MAX_ARTICLES:
        excess = total - Config.MAX_ARTICLES
        old_articles = (
            Article.query.filter_by(source=source)
            .order_by(Article.scraped_at.asc())
            .limit(excess)
            .all()
        )
        for old in old_articles:
            db.session.delete(old)
        db.session.commit()
        logger.info("Removed %d old %s articles (limit: %d)", excess, source, Config.MAX_ARTICLES)

    return count


def _generate_insight(keyword):
    """Generate a structured insight for a keyword using Claude API with web search."""
    try:
        client = anthropic.Anthropic()
        is_korean = bool(re.search(r'[가-힣]', keyword))
        search_lang = "Search in Korean." if is_korean else "Search in English."
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            system=(
                "You are a business intelligence analyst for a CEO. "
                "Use web search to find recent news about the given keyword. "
                f"{search_lang} "
                "Write your analysis in Korean."
            ),
            messages=[{"role": "user", "content": (
                f"키워드: {keyword}\n\n"
                "이 키워드에 대한 최근 24시간의 주요 뉴스를 웹에서 검색하고 분석해주세요.\n\n"
                "다음 형식으로 작성:\n"
                "## 한줄 요약\n(이 키워드의 현재 상황을 한 문장으로)\n\n"
                "## 주요 동향\n"
                "- **[이슈명]**: 핵심 팩트 + 수치 (1-2문장)\n"
                "- **[이슈명]**: 핵심 팩트 + 수치 (1-2문장)\n"
                "- **[이슈명]**: 핵심 팩트 + 수치 (1-2문장)\n\n"
                "## 액션 포인트\n"
                "- 구체적 대응 방안 또는 모니터링 포인트 (1-2개)"
            )}],
        )

        if response.stop_reason != "end_turn":
            logger.warning(
                "Insight for '%s' stopped with reason: %s",
                keyword, response.stop_reason,
            )

        insight_text = ""
        source_articles = []
        seen_urls = set()

        for block in response.content:
            if block.type == "text":
                insight_text += block.text
                if hasattr(block, 'citations') and block.citations:
                    for cite in block.citations:
                        if hasattr(cite, 'url') and cite.url not in seen_urls:
                            seen_urls.add(cite.url)
                            source_articles.append({
                                "title": getattr(cite, 'title', cite.url),
                                "url": cite.url,
                            })

        insight_text = insight_text.strip()
        logger.info(
            "Insight tokens for '%s': input=%d, output=%d",
            keyword, response.usage.input_tokens, response.usage.output_tokens,
        )

        if not insight_text:
            logger.warning(
                "Empty insight for '%s' (stop_reason=%s, blocks=%d)",
                keyword, response.stop_reason, len(response.content),
            )
            return None, []
        return insight_text, source_articles
    except Exception as e:
        logger.error("Failed to generate insight for '%s': %s", keyword, e)
        return None, []


_insight_status = {"running": False, "completed": [], "total": 0}


def generate_all_insights():
    """Generate insights for all tracked keywords (parallel)."""
    global _insight_status
    keywords = InsightKeyword.query.all()
    if not keywords:
        return
    _insight_status = {"running": True, "completed": [], "total": len(keywords)}

    def _process_keyword(kw_id, keyword_text):
        """Worker: API call only, no DB access."""
        insight_text, source_articles = _generate_insight(keyword_text)
        return kw_id, keyword_text, insight_text, source_articles

    max_workers = min(3, len(keywords))
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(_process_keyword, kw.id, kw.keyword)
                for kw in keywords
            ]
            for future in as_completed(futures):
                try:
                    kw_id, keyword_text, insight_text, source_articles = future.result()
                except Exception as e:
                    logger.error("Insight worker failed: %s", e)
                    continue
                if insight_text:
                    insight = NewsInsight(
                        keyword_id=kw_id,
                        insight_text=insight_text,
                        source_articles_json=json.dumps(source_articles, ensure_ascii=False),
                    )
                    db.session.add(insight)
                    logger.info("Generated insight for '%s'", keyword_text)
                else:
                    logger.info("No insight generated for keyword '%s'", keyword_text)
                _insight_status["completed"].append(kw_id)

        db.session.commit()
    finally:
        _insight_status["running"] = False

    # Cleanup: keep only last 30 days of insights
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    NewsInsight.query.filter(NewsInsight.generated_at < cutoff).delete()
    db.session.commit()


def scheduled_generate_insights():
    """Run insight generation within app context."""
    with app.app_context():
        generate_all_insights()


scheduler = BackgroundScheduler()
scheduler.add_job(
    scheduled_scrape,
    "cron",
    hour=Config.SCRAPE_HOUR_UTC,
    minute=Config.SCRAPE_MINUTE,
    id="daily_scrape",
)


def send_daily_push_notifications():
    """매일 06:00 KST (21:00 UTC): 연락처/비즈니스 follow-up 알림 발송."""
    if not Config.VAPID_PRIVATE_KEY or not Config.VAPID_PUBLIC_KEY:
        logger.warning("push_notify: VAPID keys not configured, skipping")
        return
    try:
        from sheets import get_all_contacts
        from sheets_entities import get_all_entities
    except Exception as e:
        logger.error("push_notify: import error: %s", e)
        return

    today = date.today()
    today_iso = today.isoformat()
    try:
        contacts = get_all_contacts()
        entities = get_all_entities()
    except Exception as e:
        logger.error("push_notify: failed to load data: %s", e)
        return

    c_today = [c for c in contacts if c.get("follow_up_date") == today_iso]
    c_over  = [c for c in contacts if c.get("follow_up_date") and c.get("follow_up_date") < today_iso]
    b_today = [b for b in entities if b.get("follow_up_date") == today_iso]
    b_over  = [b for b in entities if b.get("follow_up_date") and b.get("follow_up_date") < today_iso]

    def _names(items, max_names=4):
        parts = []
        for item in items[:max_names]:
            name = item.get("name", "").strip() or "이름없음"
            fdate = item.get("follow_up_date", "")
            if fdate and fdate < today_iso:
                days = (today - date.fromisoformat(fdate)).days
                parts.append(f"{name}({days}일 초과)")
            else:
                parts.append(name)
        if len(items) > max_names:
            parts.append(f"외 {len(items) - max_names}명")
        return ", ".join(parts)

    def _send(sub, title, body_lines, tag, url):
        payload = json.dumps({
            "title": title,
            "body": "\n".join(body_lines),
            "tag": tag,
            "url": url,
        })
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth}
                },
                data=payload,
                vapid_private_key=Config.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": f"mailto:{Config.VAPID_CLAIM_EMAIL}"}
            )
            logger.info("push_notify[%s]: sent to ...%s", tag, sub.endpoint[-20:])
        except WebPushException as e:
            if e.response and e.response.status_code in (404, 410):
                db.session.delete(sub)
                db.session.commit()
                logger.info("push_notify: removed expired subscription")
            else:
                logger.error("push_notify: error: %s", e)

    with app.app_context():
        subscriptions = PushSubscription.query.all()
        if not subscriptions:
            logger.info("push_notify: no subscriptions, nothing sent")
            return

        for sub in subscriptions:
            pref = NotificationPreference.query.filter_by(user_id=sub.user_id).first()

            # ── 연락처 알림 ──
            c_lines = []
            if (not pref or pref.contacts_today) and c_today:
                c_lines.append(f"오늘: {_names(c_today)}")
            if (not pref or pref.contacts_overdue) and c_over:
                c_lines.append(f"⚠️ 초과: {_names(c_over)}")
            if c_lines:
                _send(sub, "연락처 팔로업", c_lines, "contacts-digest", "/contacts")

            # ── 비즈니스 알림 ──
            b_lines = []
            if (not pref or pref.business_today) and b_today:
                b_lines.append(f"오늘: {_names(b_today)}")
            if (not pref or pref.business_overdue) and b_over:
                b_lines.append(f"⚠️ 초과: {_names(b_over)}")
            if b_lines:
                _send(sub, "비즈니스 팔로업", b_lines, "business-digest", "/business")


scheduler.add_job(
    send_daily_push_notifications,
    "cron",
    hour=21,
    minute=0,
    id="daily_push_notify",
)

scheduler.add_job(
    scheduled_generate_insights,
    "cron",
    hour="21,4",
    minute=0,
    id="daily_insights",
)


def _prewarm_sheets_cache():
    """Pre-warm Google Sheets caches so page loads never block on API calls."""
    try:
        from sheets import get_all_contacts
        get_all_contacts()
    except Exception as e:
        logger.warning("Sheets prewarm (contacts) failed: %s", e)
    try:
        from sheets_entities import get_all_entities
        get_all_entities()
    except Exception as e:
        logger.warning("Sheets prewarm (entities) failed: %s", e)


scheduler.add_job(
    _prewarm_sheets_cache,
    "interval",
    minutes=4,
    id="sheets_prewarm",
)


# --- Auth Routes ---

@app.route("/sw.js")
def service_worker():
    return send_from_directory("static", "sw.js", mimetype="application/javascript")


def _get_client_ip():
    """Get client IP, preferring X-Forwarded-For for reverse proxies."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _is_safe_next_url(target):
    """Allow only same-host relative redirects."""
    if not target:
        return False
    host_url = urlparse(request.host_url)
    redirect_url = urlparse(urljoin(request.host_url, target))
    return (
        redirect_url.scheme in ("http", "https")
        and host_url.netloc == redirect_url.netloc
    )


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", key_func=_login_rate_limit_key, methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        ip = _get_client_ip()
        ua = request.headers.get("User-Agent", "")[:500]

        if user and user.check_password(password):
            db.session.add(LoginLog(username=username, ip_address=ip, user_agent=ua, success=True))
            db.session.commit()
            login_user(user, remember=True)
            session.permanent = True
            next_page = request.args.get("next", "")
            if not _is_safe_next_url(next_page):
                next_page = url_for("index", fresh=1)
            return redirect(next_page)

        failure_reason = "unknown_user" if not user else "invalid_password"
        db.session.add(LoginLog(username=username, ip_address=ip, user_agent=ua, success=False, failure_reason=failure_reason))
        db.session.commit()
        flash("아이디 또는 비밀번호가 올바르지 않습니다.", "danger")

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_pw = request.form.get("current_password", "")
        new_pw = request.form.get("new_password", "")
        confirm_pw = request.form.get("confirm_password", "")

        if not current_user.check_password(current_pw):
            flash("현재 비밀번호가 올바르지 않습니다.", "danger")
        elif len(new_pw) < 8:
            flash("새 비밀번호는 8자 이상이어야 합니다.", "danger")
        elif new_pw != confirm_pw:
            flash("새 비밀번호가 일치하지 않습니다.", "danger")
        else:
            current_user.set_password(new_pw)
            current_user.password_changed_at = datetime.utcnow()
            db.session.commit()
            flash("비밀번호가 변경되었습니다.", "success")
            return redirect(url_for("index"))

    days_since = None
    if current_user.password_changed_at:
        days_since = (datetime.utcnow() - current_user.password_changed_at).days

    return render_template("change_password.html", days_since=days_since)


# --- Menu Routes ---

@app.route("/")
@login_required
def index():
    import time as _time
    fresh = request.args.get("fresh") == "1"
    if fresh:
        from sheets import invalidate_contacts_cache
        invalidate_contacts_cache()
        _invalidate_dashboard_cache()

    # Dashboard cache check
    today_key = date.today().isoformat()
    now = _time.time()
    if (not fresh
        and _dashboard_cache["data"] is not None
        and _dashboard_cache["date_key"] == today_key
        and (now - _dashboard_cache["ts"]) < DASHBOARD_CACHE_TTL):
        return render_template("landing.html", **_dashboard_cache["data"])

    ctx = _build_dashboard_context()
    _dashboard_cache["data"] = ctx
    _dashboard_cache["ts"] = _time.time()
    _dashboard_cache["date_key"] = today_key
    return render_template("landing.html", **ctx)


def _build_dashboard_context():
    """Build all template context for the dashboard landing page."""
    from sqlalchemy import func as sa_func

    try:
        from sheets import get_all_contacts
        from scoring import sort_contacts_by_score
        contacts = sort_contacts_by_score(get_all_contacts())
    except Exception:
        contacts = []

    try:
        from sheets_entities import get_all_entities
        from scoring import sort_entities_by_score
        entities = sort_entities_by_score(get_all_entities())
    except Exception:
        entities = []

    today_str = date.today().isoformat()

    # 최근 7일 일별 last_contact 집계
    last_contact_counts = {}
    for c in contacts:
        lc = c.get("last_contact", "")
        if lc:
            last_contact_counts[lc] = last_contact_counts.get(lc, 0) + 1

    weekly_stats = []
    for i in range(6, -1, -1):
        d = date.today() - timedelta(days=i)
        d_str = d.isoformat()
        weekly_stats.append({
            "date": d_str,
            "label": d.strftime("%-m/%-d"),
            "weekday": ["월", "화", "수", "목", "금", "토", "일"][d.weekday()],
            "count": last_contact_counts.get(d_str, 0),
            "is_today": i == 0,
        })

    weekly_total = sum(s["count"] for s in weekly_stats)
    max_daily = max((s["count"] for s in weekly_stats), default=1) or 1
    one_month_ago_str = (date.today() - timedelta(days=28)).isoformat()
    one_year_ago_str  = (date.today() - timedelta(days=365)).isoformat()
    contact_monthly_count = sum(v for k, v in last_contact_counts.items() if k >= one_month_ago_str)
    contact_yearly_count  = sum(v for k, v in last_contact_counts.items() if k >= one_year_ago_str)
    contact_monthly_avg = round(contact_monthly_count / 4, 1)
    contact_yearly_avg  = round(contact_yearly_count / 52, 1)

    # ReadArticle: SQL GROUP BY instead of .all() + Python loop
    one_year_ago_dt = datetime.combine(
        date.today() - timedelta(days=365), datetime.min.time()
    )
    article_daily_counts = {}
    try:
        rows = db.session.query(
            sa_func.date(ReadArticle.read_at),
            sa_func.count(ReadArticle.id)
        ).filter(
            ReadArticle.read_at >= one_year_ago_dt
        ).group_by(
            sa_func.date(ReadArticle.read_at)
        ).all()
        for d_str, cnt in rows:
            if d_str:
                article_daily_counts[str(d_str)] = cnt
    except Exception:
        article_daily_counts = {}

    article_weekly_stats = []
    for i in range(6, -1, -1):
        d = date.today() - timedelta(days=i)
        d_str = d.isoformat()
        article_weekly_stats.append({
            "date": d_str,
            "label": d.strftime("%-m/%-d"),
            "weekday": ["월", "화", "수", "목", "금", "토", "일"][d.weekday()],
            "count": article_daily_counts.get(d_str, 0),
            "is_today": i == 0,
        })

    article_weekly_total = sum(s["count"] for s in article_weekly_stats)
    article_max_daily = max((s["count"] for s in article_weekly_stats), default=1) or 1
    article_monthly_count = sum(v for k, v in article_daily_counts.items() if k >= one_month_ago_str)
    article_yearly_count  = sum(v for k, v in article_daily_counts.items() if k >= one_year_ago_str)
    article_monthly_avg = round(article_monthly_count / 4, 1)
    article_yearly_avg  = round(article_yearly_count / 52, 1)
    article_today_count = article_weekly_stats[-1]["count"] if article_weekly_stats else 0

    # Compliment: SQL GROUP BY instead of .all() + Python loop
    compliment_daily_counts = {}
    one_year_ago_date = date.today() - timedelta(days=365)
    try:
        rows = db.session.query(
            Compliment.given_at,
            sa_func.count(Compliment.id)
        ).filter(
            Compliment.given_at >= one_year_ago_date
        ).group_by(
            Compliment.given_at
        ).all()
        for given_at, cnt in rows:
            if given_at:
                compliment_daily_counts[given_at.isoformat()] = cnt
    except Exception:
        compliment_daily_counts = {}

    compliment_weekly_stats = []
    for i in range(6, -1, -1):
        d = date.today() - timedelta(days=i)
        d_str = d.isoformat()
        compliment_weekly_stats.append({
            "date": d_str,
            "label": d.strftime("%-m/%-d"),
            "weekday": ["월", "화", "수", "목", "금", "토", "일"][d.weekday()],
            "count": compliment_daily_counts.get(d_str, 0),
            "is_today": i == 0,
        })

    compliment_weekly_total = sum(s["count"] for s in compliment_weekly_stats)
    compliment_max_daily = max((s["count"] for s in compliment_weekly_stats), default=1) or 1
    compliment_monthly_count = sum(v for k, v in compliment_daily_counts.items() if k >= one_month_ago_str)
    compliment_yearly_count  = sum(v for k, v in compliment_daily_counts.items() if k >= one_year_ago_str)
    compliment_monthly_avg = round(compliment_monthly_count / 4, 1)
    compliment_yearly_avg  = round(compliment_yearly_count / 52, 1)
    compliment_today_count = compliment_weekly_stats[-1]["count"] if compliment_weekly_stats else 0
    total_compliments = sum(compliment_daily_counts.values())

    total_contacts = len(contacts)
    fu0_count = sum(1 for c in contacts if c.get("follow_up_priority") == "FU0")
    overdue_count = sum(1 for c in contacts if c.get("follow_up_date", "") and c.get("follow_up_date", "") < today_str)

    eligible = [
        c for c in contacts
        if c.get("follow_up_date") and c.get("follow_up_priority") != "FU9"
    ]
    overdue = [c for c in eligible if c.get("follow_up_date", "") < today_str]
    not_overdue = [c for c in eligible if c.get("follow_up_date", "") >= today_str]
    for c in overdue:
        try:
            delta = date.today() - date.fromisoformat(c["follow_up_date"])
            c["days_overdue"] = delta.days
        except Exception:
            c["days_overdue"] = 0
    _sort_key = lambda c: (-c.get("score", 0), c.get("follow_up_date", "9999-99-99"))
    top5 = sorted(overdue, key=_sort_key) + sorted(not_overdue, key=_sort_key)[:max(0, 5 - len(overdue))]

    eligible_e = [e for e in entities if e.get("follow_up_priority") != "FU9"]
    overdue_e = [e for e in eligible_e if e.get("follow_up_date") and e["follow_up_date"] < today_str]
    not_overdue_e = [e for e in eligible_e if not (e.get("follow_up_date") and e["follow_up_date"] < today_str)]
    for e in overdue_e:
        try:
            delta = date.today() - date.fromisoformat(e["follow_up_date"])
            e["days_overdue"] = delta.days
        except Exception:
            e["days_overdue"] = 0
    _esort = lambda e: (-e.get("score", 0), e.get("follow_up_date", "9999-99-99"))
    entity_top5 = sorted(overdue_e, key=_esort) + sorted(not_overdue_e, key=_esort)[:max(0, 5 - len(overdue_e))]

    incoming = [
        c for c in contacts
        if "입사 후보자" in (c.get("key_value_interest") or "")
        or "입사 후보자" in (c.get("tag") or "")
    ]

    reading_books = MyBook.query.filter_by(shelf="reading").order_by(MyBook.added_at.desc()).all()

    # Habit stats: single pass over all habit rows
    habit_date_sets = _build_habit_date_sets()
    habits_data = [_habit_stats(h, logged_dates=habit_date_sets.get(h, set())) for h in HABITS]
    family_stats = [_habit_stats(h, logged_dates=habit_date_sets.get(h, set())) for h in FAMILY_HABITS]

    # Anki due widget: single query for both count and first card
    anki_due_query = AnkiCard.query.filter(
        AnkiCard.status == 'active',
        AnkiCard.next_review <= date.today()
    ).order_by(AnkiCard.next_review.asc())
    anki_due_cards = anki_due_query.all()
    anki_due_count = len(anki_due_cards)
    anki_first_card = anki_due_cards[0] if anki_due_cards else None

    try:
        from sheets import get_valid_tags
        valid_tags = get_valid_tags()
    except Exception:
        valid_tags = []

    return dict(
        top5=top5,
        entity_top5=entity_top5,
        incoming=incoming,
        reading_books=reading_books,
        weekly_stats=weekly_stats,
        weekly_total=weekly_total,
        max_daily=max_daily,
        article_weekly_stats=article_weekly_stats,
        article_weekly_total=article_weekly_total,
        article_max_daily=article_max_daily,
        article_today_count=article_today_count,
        total_contacts=total_contacts,
        fu0_count=fu0_count,
        overdue_count=overdue_count,
        habits_data=habits_data,
        family_stats=family_stats,
        today_str=date.today().strftime("%Y년 %m월 %d일"),
        today_str_iso=date.today().isoformat(),
        contact_monthly_avg=contact_monthly_avg,
        contact_yearly_avg=contact_yearly_avg,
        article_monthly_avg=article_monthly_avg,
        article_yearly_avg=article_yearly_avg,
        compliment_weekly_stats=compliment_weekly_stats,
        compliment_weekly_total=compliment_weekly_total,
        compliment_max_daily=compliment_max_daily,
        compliment_today_count=compliment_today_count,
        total_compliments=total_compliments,
        compliment_monthly_avg=compliment_monthly_avg,
        compliment_yearly_avg=compliment_yearly_avg,
        anki_due_count=anki_due_count,
        anki_first_card=anki_first_card,
        valid_tags=valid_tags,
    )


@app.route("/contacts")
@login_required
def contact_list():
    return render_template("contacts.html")


@app.route("/business-opportunities")
@login_required
def business_opportunities_page():
    return render_template("business_opportunities.html")


@app.route("/contacts/chat")
@login_required
def contact_chat():
    return render_template("contact_chat.html")


@app.route("/news")
@login_required
def daily_news():
    for src in NEWS_SOURCES:
        auto_scrape(src)

    selected = request.args.get('source', '')

    # Source counts via SQL GROUP BY
    from sqlalchemy import func as sa_func
    count_rows = (db.session.query(Article.source, sa_func.count(Article.id))
                  .filter(Article.source.in_(NEWS_SOURCES))
                  .group_by(Article.source)
                  .all())
    source_counts = {src: cnt for src, cnt in count_rows if cnt > 0}

    # Load only the articles needed for display
    if selected and selected in NEWS_SOURCES:
        articles = (Article.query.filter_by(source=selected)
                    .order_by(Article.scraped_at.desc()).all())
    else:
        articles = (Article.query.filter(Article.source.in_(NEWS_SOURCES))
                    .order_by(Article.scraped_at.desc()).all())
        selected = ''

    return render_template("news.html", articles=articles,
                           source_counts=source_counts,
                           source_map=NEWS_SOURCE_MAP,
                           news_sources=NEWS_SOURCES,
                           selected_source=selected)


@app.route("/news/mk")
@login_required
def mk_news():
    return redirect(url_for("daily_news", source="mk"))


@app.route("/news/irobot")
@login_required
def irobot_news():
    return redirect(url_for("daily_news", source="irobot"))


@app.route("/news/ai")
@login_required
def ai_news():
    return redirect(url_for("daily_news"))


@app.route("/news/ai-robotics/companies")
@login_required
def ai_robotics_companies_news():
    return redirect(url_for("daily_news", source="ai_robotics"))


@app.route("/news/trends")
@login_required
def trends_news():
    return redirect(url_for("daily_news", source="geek_weekly"))


@app.route("/news/deeplearning")
@login_required
def deeplearning_news():
    return redirect(url_for("daily_news", source="dl_batch"))


@app.route("/bestsellers")
@login_required
def bestsellers():
    return render_template("bestsellers.html")


@app.route("/bestsellers/global")
@login_required
def bestsellers_global():
    articles = (
        Article.query.filter_by(source="bestseller")
        .order_by(db.cast(Article.section, db.Integer))
        .all()
    )
    return render_template("bestsellers_global.html", articles=articles)


@app.route("/bestsellers/kr")
@login_required
def bestsellers_kr():
    articles = (
        Article.query.filter_by(source="bestseller_kr")
        .order_by(db.cast(Article.section, db.Integer))
        .all()
    )
    return render_template("bestsellers_kr.html", articles=articles)


# --- My Books Routes ---

@app.route("/books")
@login_required
def my_books():
    all_read = MyBook.query.filter_by(shelf="read").all()
    total_read = len(all_read)
    rated_books = [b for b in all_read if b.my_rating > 0]
    avg_rating = sum(b.my_rating for b in rated_books) / len(rated_books) if rated_books else 0
    # 연도별 통계
    yearly = {}
    for b in all_read:
        if b.date_read and '/' in b.date_read:
            year = b.date_read.split('/')[0]
            if year.isdigit():
                yearly[int(year)] = yearly.get(int(year), 0) + 1
    yearly_stats = sorted(yearly.items(), reverse=True)
    hall_of_fame_count = MyBook.query.filter_by(hall_of_fame=True).count()
    return render_template("books.html",
                           total_read=total_read,
                           avg_rating=round(avg_rating, 1),
                           yearly_stats=yearly_stats,
                           hall_of_fame_count=hall_of_fame_count)


@app.route("/books/library")
@login_required
def book_library():
    books = MyBook.query.filter_by(shelf="read").order_by(MyBook.date_read.desc()).all()
    return render_template("book_library.html", books=books)


@app.route("/books/reading")
@login_required
def book_reading():
    books = MyBook.query.filter_by(shelf="reading").order_by(MyBook.added_at.desc()).all()
    return render_template("book_reading.html", books=books)


@app.route("/books/hall-of-fame")
@login_required
def book_hall_of_fame():
    books = MyBook.query.filter_by(hall_of_fame=True).order_by(MyBook.my_rating.desc()).all()
    return render_template("book_hall_of_fame.html", books=books)


@app.route("/api/books/search")
@login_required
def api_book_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": []})

    params = {"q": q, "maxResults": 20, "printType": "books"}
    api_key = os.environ.get("GOOGLE_BOOKS_API_KEY")
    if api_key:
        params["key"] = api_key
    try:
        r = http_requests.get("https://www.googleapis.com/books/v1/volumes",
                              params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        items = data.get("items", [])
    except Exception as e:
        logger.error("Google Books API error: %s", e)
        return jsonify({"results": [], "error": "처리 중 오류가 발생했습니다."})

    results = []
    for item in items:
        info = item.get("volumeInfo", {})
        isbns = {i["type"]: i["identifier"] for i in info.get("industryIdentifiers", [])}
        pub_date = info.get("publishedDate", "")
        year = int(pub_date[:4]) if pub_date and pub_date[:4].isdigit() else 0
        results.append({
            "title": info.get("title", ""),
            "author": ", ".join(info.get("authors", [])),
            "publisher": info.get("publisher", ""),
            "year_published": year,
            "isbn": isbns.get("ISBN_10", ""),
            "isbn13": isbns.get("ISBN_13", ""),
            "average_rating": info.get("averageRating", 0.0),
            "thumbnail": info.get("imageLinks", {}).get("thumbnail", ""),
        })
    return jsonify({"results": results})


@app.route("/books/library/add", methods=["POST"])
@login_required
def book_add():
    title = request.form.get("title", "").strip()
    author = request.form.get("author", "").strip()
    if not title or not author:
        flash("제목과 저자를 모두 입력해 주세요.", "danger")
        return redirect(url_for("book_library"))

    # Convert date_read from YYYY-MM-DD to YYYY/MM/DD
    date_read_raw = request.form.get("date_read", "").strip()
    date_read = date_read_raw.replace("-", "/") if date_read_raw else ""

    my_rating = int(request.form.get("my_rating", 0) or 0)
    if my_rating < 0 or my_rating > 5:
        my_rating = 0

    book = MyBook(
        title=title,
        author=author,
        isbn=request.form.get("isbn", "").strip(),
        isbn13=request.form.get("isbn13", "").strip(),
        publisher=request.form.get("publisher", "").strip(),
        year_published=int(request.form.get("year_published", 0) or 0),
        average_rating=float(request.form.get("average_rating", 0) or 0),
        my_rating=my_rating,
        date_read=date_read,
        shelf=request.form.get("shelf", "read"),
    )
    db.session.add(book)
    db.session.commit()
    auto_regenerate_recommendations()
    flash(f'"{title}" 추가 완료', "success")
    if request.form.get("shelf", "read") == "reading":
        return redirect(url_for("book_reading"))
    return redirect(url_for("book_library"))


@app.route("/books/recommendations")
@login_required
def book_recommendations():
    return render_template("book_recommendations.html")


@app.route("/books/saved")
@login_required
def book_saved():
    books = SavedBook.query.order_by(SavedBook.saved_at.desc()).all()
    return render_template("book_saved.html", books=books)


# --- Keyword Insights ---

@app.route("/insights")
@login_required
def keyword_insights():
    keywords = InsightKeyword.query.order_by(InsightKeyword.position.asc(), InsightKeyword.created_at.asc()).all()

    # Batch: latest insight per keyword via subquery (avoids N+1)
    from sqlalchemy import func as sa_func
    latest_subq = (db.session.query(
        NewsInsight.keyword_id,
        sa_func.max(NewsInsight.id).label('max_id')
    ).group_by(NewsInsight.keyword_id).subquery())
    latest_insights = (db.session.query(NewsInsight)
        .join(latest_subq, NewsInsight.id == latest_subq.c.max_id)
        .all())
    insight_map = {ni.keyword_id: ni for ni in latest_insights}

    keyword_data = []
    for kw in keywords:
        latest = insight_map.get(kw.id)
        source_articles = []
        if latest and latest.source_articles_json:
            try:
                source_articles = json.loads(latest.source_articles_json)
            except (json.JSONDecodeError, TypeError):
                pass
        keyword_data.append({
            "keyword": kw,
            "insight": latest,
            "source_articles": source_articles,
        })
    return render_template("keyword_insights.html", keyword_data=keyword_data)


@app.route("/api/insights/keywords", methods=["POST"])
@login_required
def api_add_keyword():
    data = request.get_json(silent=True) or {}
    keyword = (data.get("keyword") or "").strip()
    if not keyword:
        return jsonify({"status": "error", "message": "키워드를 입력해주세요."}), 400
    if InsightKeyword.query.count() >= 10:
        return jsonify({"status": "error", "message": "키워드는 최대 10개까지 추가할 수 있습니다."}), 400
    if InsightKeyword.query.filter_by(keyword=keyword).first():
        return jsonify({"status": "error", "message": "이미 등록된 키워드입니다."}), 400
    max_pos = db.session.query(db.func.max(InsightKeyword.position)).scalar() or 0
    kw = InsightKeyword(keyword=keyword, position=max_pos + 1)
    db.session.add(kw)
    db.session.commit()
    return jsonify({"status": "ok", "id": kw.id, "keyword": kw.keyword})


@app.route("/api/insights/keywords/reorder", methods=["PATCH"])
@login_required
def api_reorder_keywords():
    data = request.get_json(silent=True) or {}
    order = data.get("order", [])
    if not isinstance(order, list):
        return jsonify({"status": "error", "message": "Invalid order"}), 400
    for i, keyword_id in enumerate(order):
        kw = db.session.get(InsightKeyword, keyword_id)
        if kw:
            kw.position = i
    db.session.commit()
    return jsonify({"status": "ok"})


@app.route("/api/insights/keywords/<int:keyword_id>", methods=["DELETE"])
@login_required
def api_delete_keyword(keyword_id):
    kw = db.session.get(InsightKeyword, keyword_id)
    if not kw:
        return jsonify({"status": "error", "message": "Not found"}), 404
    db.session.delete(kw)
    db.session.commit()
    return jsonify({"status": "ok"})


@app.route("/api/insights/generate", methods=["POST"])
@login_required
def api_generate_insights():
    def _bg():
        with app.app_context():
            generate_all_insights()
    thread = threading.Thread(target=_bg, daemon=True)
    thread.start()
    return jsonify({"status": "ok", "message": "인사이트 생성을 시작했습니다."})


@app.route("/api/insights/status")
@login_required
def api_insight_status():
    return jsonify(_insight_status)


@app.route("/api/insights/keywords/<int:keyword_id>/history")
@login_required
def api_keyword_history(keyword_id):
    insights = (NewsInsight.query
                .filter_by(keyword_id=keyword_id)
                .order_by(NewsInsight.generated_at.desc())
                .limit(14)
                .all())
    result = []
    for ins in insights:
        articles = []
        if ins.source_articles_json:
            try:
                articles = json.loads(ins.source_articles_json)
            except (json.JSONDecodeError, TypeError):
                pass
        result.append({
            "id": ins.id,
            "insight_text": ins.insight_text,
            "source_articles": articles,
            "generated_at": ins.generated_at.strftime("%Y-%m-%d %H:%M"),
        })
    return jsonify({"status": "ok", "insights": result})


# --- API Routes ---

@app.route("/api/scrape/<source>", methods=["POST"])
@login_required
def api_scrape(source):
    if source not in ("mk", "irobot", "robotreport", "ai_robotics", "geek_weekly", "dl_batch", "the_decoder", "acdeeptech", "aitimes", "wsj_ai", "nyt_tech", "bestseller", "bestseller_kr", "fieldai", "vention", "ifr_press"):
        return jsonify({"status": "error", "message": "Unknown source"}), 400
    count = run_scrape(source)
    return jsonify({"status": "ok", "new_articles": count})


@app.route("/api/articles/<int:article_id>/read", methods=["POST"])
@login_required
def mark_read(article_id):
    article = db.session.get(Article, article_id)
    if article:
        # Record URL as read before deleting
        if not ReadArticle.query.filter_by(url=article.url).first():
            db.session.add(ReadArticle(url=article.url))
        db.session.delete(article)
        db.session.commit()
        _invalidate_dashboard_cache()
        return jsonify({"status": "ok"})
    return jsonify({"status": "not_found"}), 404


@app.route("/api/articles/read-all/<source>", methods=["POST"])
@login_required
def mark_all_read(source):
    articles = Article.query.filter_by(source=source).all()
    # Batch-load existing read URLs to avoid N+1
    article_urls = [a.url for a in articles]
    existing_read = set()
    if article_urls:
        existing_read = {r.url for r in ReadArticle.query.filter(
            ReadArticle.url.in_(article_urls)
        ).with_entities(ReadArticle.url).all()}
    count = 0
    for article in articles:
        if article.url not in existing_read:
            db.session.add(ReadArticle(url=article.url))
        db.session.delete(article)
        count += 1
    db.session.commit()
    _invalidate_dashboard_cache()
    return jsonify({"status": "ok", "cleared": count})


@app.route("/api/books/<int:book_id>/rate", methods=["POST"])
@login_required
def api_rate_book(book_id):
    book = db.session.get(MyBook, book_id)
    if not book:
        return jsonify({"status": "not_found"}), 404
    data = request.get_json()
    rating = data.get("rating", 0)
    if not isinstance(rating, int) or rating < 0 or rating > 5:
        return jsonify({"status": "error", "message": "Rating must be 0-5"}), 400
    book.my_rating = rating
    db.session.commit()
    auto_regenerate_recommendations()
    return jsonify({"status": "ok", "rating": rating})


@app.route("/api/books/<int:book_id>/delete", methods=["POST"])
@login_required
def api_delete_book(book_id):
    book = db.session.get(MyBook, book_id)
    if not book:
        return jsonify({"status": "not_found"}), 404
    db.session.delete(book)
    db.session.commit()
    auto_regenerate_recommendations()
    return jsonify({"status": "ok"})


@app.route("/api/books/<int:book_id>/complete", methods=["POST"])
@login_required
def api_complete_book(book_id):
    book = db.session.get(MyBook, book_id)
    if not book:
        return jsonify({"status": "not_found"}), 404
    data = request.get_json()
    rating = data.get("my_rating", 0)
    if not isinstance(rating, int) or rating < 1 or rating > 5:
        return jsonify({"status": "error", "message": "별점을 선택해 주세요."}), 400
    date_raw = data.get("date_read", "").strip()
    book.shelf = "read"
    book.my_rating = rating
    book.date_read = date_raw.replace("-", "/") if date_raw else ""
    book.hall_of_fame = bool(data.get("hall_of_fame", False))
    db.session.commit()
    auto_regenerate_recommendations()
    return jsonify({"status": "ok"})


@app.route("/api/books/<int:book_id>/hall-of-fame", methods=["POST"])
@login_required
def api_toggle_hall_of_fame(book_id):
    book = db.session.get(MyBook, book_id)
    if not book:
        return jsonify({"status": "not_found"}), 404
    data = request.get_json()
    book.hall_of_fame = bool(data.get("hall_of_fame", not book.hall_of_fame))
    db.session.commit()
    auto_regenerate_recommendations()
    return jsonify({"status": "ok", "hall_of_fame": book.hall_of_fame})


@app.route("/api/books/recommendations/generate", methods=["POST"])
@login_required
def api_generate_recommendations():
    books = MyBook.query.all()
    if not books:
        return jsonify({"status": "error", "message": "책이 없습니다. 먼저 라이브러리에 책을 추가해 주세요."}), 400
    try:
        recs = generate_recommendations(books)
    except Exception as e:
        logger.error("Recommendation generation failed: %s", e)
        return jsonify({"status": "error", "message": "처리 중 오류가 발생했습니다."}), 500

    # Replace all existing recommendations
    Recommendation.query.delete()
    for r in recs:
        db.session.add(Recommendation(
            title=r["title"],
            author=r["author"],
            reason=r["reason"],
            category=r["category"],
        ))
    db.session.commit()
    return jsonify({"status": "ok", "count": len(recs)})


@app.route("/api/books/chat", methods=["POST"])
@login_required
def api_books_chat():
    data = request.get_json()
    if not data or not data.get("message", "").strip():
        return jsonify({"status": "error", "message": "메시지를 입력해 주세요."}), 400

    user_message = data["message"].strip()
    books = MyBook.query.all()
    saved_books = SavedBook.query.all()

    # Load conversation history from DB (last 50 messages for context window management)
    db_messages = (
        ChatMessage.query
        .order_by(ChatMessage.created_at.desc())
        .limit(50).all()
    )
    history = [{"role": m.role, "content": m.content} for m in reversed(db_messages)]

    try:
        result = chat_recommendation(user_message, history, books, saved_books=saved_books)
    except Exception as e:
        logger.error("Chat recommendation failed: %s", e)
        return jsonify({"status": "error", "message": "처리 중 오류가 발생했습니다."}), 500

    # Persist user message
    db.session.add(ChatMessage(role="user", content=user_message))
    # Persist assistant message with recommendations JSON
    recs_json = json.dumps(result.get("recommendations", []), ensure_ascii=False) if result.get("recommendations") else ""
    db.session.add(ChatMessage(role="assistant", content=result["message"], recommendations_json=recs_json))
    db.session.commit()

    return jsonify(result)


@app.route("/api/books/chat/history", methods=["GET"])
@login_required
def api_chat_history():
    """Return full chat history for page reload restoration."""
    messages = (ChatMessage.query
                .order_by(ChatMessage.created_at.desc())
                .limit(50).all())
    result = []
    for m in reversed(messages):
        entry = {"role": m.role, "content": m.content}
        if m.recommendations_json:
            try:
                entry["recommendations"] = json.loads(m.recommendations_json)
            except (json.JSONDecodeError, TypeError):
                entry["recommendations"] = []
        result.append(entry)
    return jsonify({"messages": result})


@app.route("/api/books/chat/clear", methods=["POST"])
@login_required
def api_chat_clear():
    """Clear all chat history for a fresh conversation."""
    count = ChatMessage.query.delete()
    db.session.commit()
    return jsonify({"status": "ok", "cleared": count})


@app.route("/api/books/saved", methods=["POST"])
@login_required
def api_save_book():
    data = request.get_json()
    if not data or not data.get("title", "").strip():
        return jsonify({"status": "error", "message": "제목이 필요합니다."}), 400

    book = SavedBook(
        title=data["title"].strip(),
        author=data.get("author", "").strip(),
        reason=data.get("reason", "").strip(),
        category=data.get("category", "").strip(),
    )
    db.session.add(book)
    db.session.commit()
    return jsonify({"status": "ok", "id": book.id})


@app.route("/api/books/saved/<int:book_id>", methods=["DELETE"])
@login_required
def api_delete_saved_book(book_id):
    book = db.session.get(SavedBook, book_id)
    if not book:
        return jsonify({"status": "not_found"}), 404
    db.session.delete(book)
    db.session.commit()
    return jsonify({"status": "ok"})


@app.route("/api/books/saved/<int:book_id>/to-reading", methods=["POST"])
@login_required
def api_book_saved_to_reading(book_id):
    saved = SavedBook.query.get_or_404(book_id)

    extra = {}
    try:
        params = {"q": f"{saved.title} {saved.author}", "maxResults": 1, "printType": "books"}
        api_key = os.environ.get("GOOGLE_BOOKS_API_KEY")
        if api_key:
            params["key"] = api_key
        r = http_requests.get("https://www.googleapis.com/books/v1/volumes", params=params, timeout=5)
        if r.ok:
            items = r.json().get("items", [])
            if items:
                info = items[0].get("volumeInfo", {})
                isbns = {i["type"]: i["identifier"] for i in info.get("industryIdentifiers", [])}
                pub_date = info.get("publishedDate", "")
                extra = {
                    "isbn": isbns.get("ISBN_10", ""),
                    "isbn13": isbns.get("ISBN_13", ""),
                    "publisher": info.get("publisher", ""),
                    "year_published": int(pub_date[:4]) if pub_date and pub_date[:4].isdigit() else 0,
                    "average_rating": info.get("averageRating", 0.0),
                }
    except Exception:
        pass

    book = MyBook(title=saved.title, author=saved.author, shelf="reading", **extra)
    db.session.add(book)
    db.session.delete(saved)
    db.session.commit()
    return jsonify({"ok": True, "book_id": book.id})


# --- My Screens Routes ---

TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"


def _tmdb_params(extra=None):
    params = {"api_key": os.environ.get("TMDB_API_KEY", "")}
    if extra:
        params.update(extra)
    return params


def _tmdb_poster_url(poster_path):
    if poster_path:
        return f"{TMDB_IMAGE_BASE}{poster_path}"
    return ""


def _tmdb_enrich(title, media_type="movie"):
    """Search TMDB for title and return {tmdb_id, tmdb_title, year, poster_url}. Returns {} on failure."""
    api_key = os.environ.get("TMDB_API_KEY", "")
    if not api_key or not title:
        return {}
    try:
        endpoint = f"{TMDB_BASE_URL}/search/{'movie' if media_type == 'movie' else 'tv'}"
        params = {"query": title, "language": "ko-KR", "include_adult": False, "api_key": api_key}
        r = http_requests.get(endpoint, params=params, headers={"accept": "application/json"}, timeout=5)
        if not r.ok:
            return {}
        items = r.json().get("results", [])
        if not items:
            return {}
        item = items[0]
        if media_type == "movie":
            tmdb_title = item.get("title") or item.get("original_title", "")
            year_str = item.get("release_date", "")
        else:
            tmdb_title = item.get("name") or item.get("original_name", "")
            year_str = item.get("first_air_date", "")
        return {
            "tmdb_id": item.get("id"),
            "tmdb_title": tmdb_title,
            "year": int(year_str[:4]) if year_str and year_str[:4].isdigit() else None,
            "poster_url": _tmdb_poster_url(item.get("poster_path", "")),
        }
    except Exception:
        return {}


@app.route("/screens")
@login_required
def my_screens():
    watched = MyScreen.query.filter_by(shelf="watched").all()
    total_watched = len(watched)
    rated = [s for s in watched if s.my_rating > 0]
    avg_rating = sum(s.my_rating for s in rated) / len(rated) if rated else 0
    hall_of_fame_count = MyScreen.query.filter_by(hall_of_fame=True).count()
    watching_count = MyScreen.query.filter_by(shelf="watching").count()
    want_count = MyScreen.query.filter_by(shelf="want-to-watch").count()
    return render_template(
        "screens.html",
        total_watched=total_watched,
        avg_rating=round(avg_rating, 1),
        hall_of_fame_count=hall_of_fame_count,
        watching_count=watching_count,
        want_count=want_count,
    )


@app.route("/screens/library")
@login_required
def screen_library():
    screens = MyScreen.query.filter_by(shelf="watched").order_by(MyScreen.date_watched.desc()).all()
    return render_template("screen_library.html", screens=screens)


@app.route("/screens/watching")
@login_required
def screen_watching():
    screens = MyScreen.query.filter_by(shelf="watching").order_by(MyScreen.added_at.desc()).all()
    return render_template("screen_watching.html", screens=screens)


@app.route("/screens/hall-of-fame")
@login_required
def screen_hall_of_fame():
    screens = MyScreen.query.filter_by(hall_of_fame=True).order_by(MyScreen.my_rating.desc()).all()
    return render_template("screen_hall_of_fame.html", screens=screens)


@app.route("/screens/recommendations")
@login_required
def screen_recommendations():
    return render_template("screen_recommendations.html")


@app.route("/screens/saved")
@login_required
def screen_saved():
    screens = SavedScreen.query.order_by(SavedScreen.saved_at.desc()).all()
    return render_template("screen_saved.html", screens=screens)


@app.route("/api/screens/search")
@login_required
def api_screen_search():
    q = request.args.get("q", "").strip()
    media_type = request.args.get("type", "all")  # movie | tv | all
    if not q:
        return jsonify({"results": []})

    api_key = os.environ.get("TMDB_API_KEY", "")
    if not api_key:
        return jsonify({"results": [], "error": "TMDB_API_KEY가 설정되지 않았습니다."})

    params = {"query": q, "language": "ko-KR", "include_adult": False, "api_key": api_key}

    if media_type == "movie":
        endpoint = f"{TMDB_BASE_URL}/search/movie"
    elif media_type == "tv":
        endpoint = f"{TMDB_BASE_URL}/search/tv"
    else:
        endpoint = f"{TMDB_BASE_URL}/search/multi"

    try:
        r = http_requests.get(endpoint, params=params, headers={"accept": "application/json"}, timeout=10)
        r.raise_for_status()
        data = r.json()
        items = data.get("results", [])
    except Exception as e:
        logger.error("TMDB API error: %s", e)
        return jsonify({"results": [], "error": "처리 중 오류가 발생했습니다."})

    results = []
    for item in items:
        mtype = item.get("media_type", media_type)
        if mtype not in ("movie", "tv"):
            continue
        if mtype == "movie":
            title = item.get("title") or item.get("original_title", "")
            original_title = item.get("original_title", "")
            year_str = item.get("release_date", "")
        else:
            title = item.get("name") or item.get("original_name", "")
            original_title = item.get("original_name", "")
            year_str = item.get("first_air_date", "")
        year = int(year_str[:4]) if year_str and year_str[:4].isdigit() else 0
        results.append({
            "tmdb_id": item.get("id"),
            "title": title,
            "original_title": original_title,
            "media_type": mtype,
            "year": year,
            "overview": item.get("overview", ""),
            "tmdb_rating": item.get("vote_average", 0.0),
            "poster_url": _tmdb_poster_url(item.get("poster_path", "")),
        })

    return jsonify({"results": results[:20]})


@app.route("/screens/add", methods=["POST"])
@login_required
def screen_add():
    title = request.form.get("title", "").strip()
    if not title:
        flash("제목을 입력해 주세요.", "danger")
        return redirect(url_for("screen_library"))

    date_watched_raw = request.form.get("date_watched", "").strip()
    date_watched = date_watched_raw.replace("-", "/") if date_watched_raw else ""

    my_rating = int(request.form.get("my_rating", 0) or 0)
    if my_rating < 0 or my_rating > 5:
        my_rating = 0

    screen = MyScreen(
        tmdb_id=int(request.form.get("tmdb_id") or 0) or None,
        title=title,
        original_title=request.form.get("original_title", "").strip(),
        media_type=request.form.get("media_type", "movie"),
        genres=request.form.get("genres", "").strip(),
        director=request.form.get("director", "").strip(),
        year=int(request.form.get("year", 0) or 0),
        poster_url=request.form.get("poster_url", "").strip(),
        overview=request.form.get("overview", "").strip(),
        tmdb_rating=float(request.form.get("tmdb_rating", 0) or 0),
        my_rating=my_rating,
        date_watched=date_watched,
        shelf=request.form.get("shelf", "watched"),
    )
    db.session.add(screen)
    db.session.commit()
    flash(f'"{title}" 추가 완료', "success")
    shelf = request.form.get("shelf", "watched")
    if shelf == "watching":
        return redirect(url_for("screen_watching"))
    return redirect(url_for("screen_library"))


@app.route("/api/screens/<int:screen_id>/rate", methods=["POST"])
@login_required
def api_rate_screen(screen_id):
    screen = db.session.get(MyScreen, screen_id)
    if not screen:
        return jsonify({"status": "not_found"}), 404
    data = request.get_json()
    rating = data.get("rating", 0)
    if not isinstance(rating, int) or rating < 0 or rating > 5:
        return jsonify({"status": "error", "message": "Rating must be 0-5"}), 400
    screen.my_rating = rating
    db.session.commit()
    return jsonify({"status": "ok", "rating": rating})


@app.route("/api/screens/<int:screen_id>/delete", methods=["POST"])
@login_required
def api_delete_screen(screen_id):
    screen = db.session.get(MyScreen, screen_id)
    if not screen:
        return jsonify({"status": "not_found"}), 404
    db.session.delete(screen)
    db.session.commit()
    return jsonify({"status": "ok"})


@app.route("/api/screens/<int:screen_id>/complete", methods=["POST"])
@login_required
def api_complete_screen(screen_id):
    screen = db.session.get(MyScreen, screen_id)
    if not screen:
        return jsonify({"status": "not_found"}), 404
    data = request.get_json()
    rating = data.get("my_rating", 0)
    if not isinstance(rating, int) or rating < 1 or rating > 5:
        return jsonify({"status": "error", "message": "별점을 선택해 주세요."}), 400
    date_raw = data.get("date_watched", "").strip()
    screen.shelf = "watched"
    screen.my_rating = rating
    screen.date_watched = date_raw.replace("-", "/") if date_raw else ""
    screen.hall_of_fame = bool(data.get("hall_of_fame", False))
    db.session.commit()
    return jsonify({"status": "ok"})


@app.route("/api/screens/<int:screen_id>/hall-of-fame", methods=["POST"])
@login_required
def api_toggle_screen_hall_of_fame(screen_id):
    screen = db.session.get(MyScreen, screen_id)
    if not screen:
        return jsonify({"status": "not_found"}), 404
    data = request.get_json()
    screen.hall_of_fame = bool(data.get("hall_of_fame", not screen.hall_of_fame))
    db.session.commit()
    return jsonify({"status": "ok", "hall_of_fame": screen.hall_of_fame})


@app.route("/api/screens/chat", methods=["POST"])
@login_required
def api_screens_chat():
    data = request.get_json()
    if not data or not data.get("message", "").strip():
        return jsonify({"status": "error", "message": "메시지를 입력해 주세요."}), 400

    user_message = data["message"].strip()
    screens = MyScreen.query.all()
    saved_screens = SavedScreen.query.all()

    db_messages = (
        ScreenChatMessage.query
        .order_by(ScreenChatMessage.created_at.desc())
        .limit(50).all()
    )
    history = [{"role": m.role, "content": m.content} for m in reversed(db_messages)]

    try:
        result = chat_screen_recommendation(user_message, history, screens, saved_screens=saved_screens)
    except Exception as e:
        logger.error("Screen chat recommendation failed: %s", e)
        return jsonify({"status": "error", "message": "처리 중 오류가 발생했습니다."}), 500

    for rec in result.get("recommendations", []):
        rec.update(_tmdb_enrich(rec["title"], rec.get("media_type", "movie")))

    db.session.add(ScreenChatMessage(role="user", content=user_message))
    recs_json = json.dumps(result.get("recommendations", []), ensure_ascii=False) if result.get("recommendations") else ""
    db.session.add(ScreenChatMessage(role="assistant", content=result["message"], recommendations_json=recs_json))
    db.session.commit()

    return jsonify(result)


@app.route("/api/screens/chat/history", methods=["GET"])
@login_required
def api_screen_chat_history():
    messages = (ScreenChatMessage.query
                .order_by(ScreenChatMessage.created_at.desc())
                .limit(50).all())
    result = []
    for m in reversed(messages):
        entry = {"role": m.role, "content": m.content}
        if m.recommendations_json:
            try:
                entry["recommendations"] = json.loads(m.recommendations_json)
            except (json.JSONDecodeError, TypeError):
                entry["recommendations"] = []
        result.append(entry)
    return jsonify({"messages": result})


@app.route("/api/screens/chat/clear", methods=["POST"])
@login_required
def api_screen_chat_clear():
    count = ScreenChatMessage.query.delete()
    db.session.commit()
    return jsonify({"status": "ok", "cleared": count})


@app.route("/api/screens/saved", methods=["POST"])
@login_required
def api_save_screen():
    data = request.get_json()
    if not data or not data.get("title", "").strip():
        return jsonify({"status": "error", "message": "제목이 필요합니다."}), 400

    screen = SavedScreen(
        title=data["title"].strip(),
        media_type=data.get("media_type", "movie"),
        reason=data.get("reason", "").strip(),
        category=data.get("category", "").strip(),
        tmdb_id=data.get("tmdb_id") or None,
        tmdb_title=data.get("tmdb_title", "").strip() or None,
        year=data.get("year") or None,
        poster_url=data.get("poster_url", "").strip() or None,
    )
    db.session.add(screen)
    db.session.commit()
    return jsonify({"status": "ok", "id": screen.id})


@app.route("/api/screens/saved/<int:screen_id>", methods=["DELETE"])
@login_required
def api_delete_saved_screen(screen_id):
    screen = db.session.get(SavedScreen, screen_id)
    if not screen:
        return jsonify({"status": "not_found"}), 404
    db.session.delete(screen)
    db.session.commit()
    return jsonify({"status": "ok"})


@app.route("/api/screens/saved/<int:screen_id>/to-watching", methods=["POST"])
@login_required
def api_screen_saved_to_watching(screen_id):
    saved = SavedScreen.query.get_or_404(screen_id)
    media_type = saved.media_type or "movie"
    display_title = saved.tmdb_title or saved.title

    extra = {}
    api_key = os.environ.get("TMDB_API_KEY", "")
    if api_key:
        try:
            if saved.tmdb_id:
                # Direct fetch by ID — exact match, no ambiguity
                mtype = "movie" if media_type == "movie" else "tv"
                endpoint = f"{TMDB_BASE_URL}/{mtype}/{saved.tmdb_id}"
                params = _tmdb_params({"language": "ko-KR"})
                r = http_requests.get(endpoint, params=params, headers={"accept": "application/json"}, timeout=5)
                if r.ok:
                    item = r.json()
                    if media_type == "movie":
                        fetched_title = item.get("title") or item.get("original_title", "")
                        year_str = item.get("release_date", "")
                    else:
                        fetched_title = item.get("name") or item.get("original_name", "")
                        year_str = item.get("first_air_date", "")
                    if fetched_title:
                        display_title = fetched_title
                    extra = {
                        "tmdb_id": item.get("id"),
                        "original_title": item.get("original_title") or item.get("original_name", ""),
                        "year": int(year_str[:4]) if year_str and year_str[:4].isdigit() else 0,
                        "poster_url": _tmdb_poster_url(item.get("poster_path", "")),
                        "overview": item.get("overview", ""),
                        "tmdb_rating": item.get("vote_average", 0.0),
                    }
            else:
                # Fallback: search by title (legacy — no tmdb_id stored)
                endpoint = f"{TMDB_BASE_URL}/search/{'movie' if media_type == 'movie' else 'tv'}"
                params = _tmdb_params({"query": saved.title, "language": "ko-KR", "include_adult": False})
                r = http_requests.get(endpoint, params=params, headers={"accept": "application/json"}, timeout=5)
                if r.ok:
                    items = r.json().get("results", [])
                    if items:
                        item = items[0]
                        year_str = item.get("release_date" if media_type == "movie" else "first_air_date", "")
                        extra = {
                            "tmdb_id": item.get("id"),
                            "original_title": item.get("original_title") or item.get("original_name", ""),
                            "year": int(year_str[:4]) if year_str and year_str[:4].isdigit() else 0,
                            "poster_url": _tmdb_poster_url(item.get("poster_path", "")),
                            "overview": item.get("overview", ""),
                            "tmdb_rating": item.get("vote_average", 0.0),
                        }
        except Exception:
            pass

    screen = MyScreen(title=display_title, media_type=media_type, shelf="watching", **extra)
    db.session.add(screen)
    db.session.delete(saved)
    db.session.commit()
    return jsonify({"ok": True, "screen_id": screen.id})


# --- Compliment API ---

@app.route("/api/compliments", methods=["GET"])
@login_required
def api_get_compliments():
    thirty_days_ago = date.today() - timedelta(days=30)
    rows = Compliment.query.filter(Compliment.given_at >= thirty_days_ago)\
        .order_by(Compliment.given_at.desc(), Compliment.created_at.desc()).all()
    return jsonify([{
        "id": r.id, "recipient": r.recipient,
        "content": r.content, "given_at": r.given_at.isoformat(),
    } for r in rows])


@app.route("/api/compliments", methods=["POST"])
@login_required
def api_add_compliment():
    data = request.get_json()
    recipient = (data.get("recipient") or "").strip()
    content = (data.get("content") or "").strip()
    given_at_str = (data.get("given_at") or "").strip()
    if not recipient or not content:
        return jsonify({"error": "recipient and content are required"}), 400
    try:
        given_at = date.fromisoformat(given_at_str) if given_at_str else date.today()
    except ValueError:
        return jsonify({"error": "invalid given_at date"}), 400
    c = Compliment(recipient=recipient, content=content, given_at=given_at)
    db.session.add(c)
    db.session.commit()
    _invalidate_dashboard_cache()
    return jsonify({"id": c.id, "recipient": c.recipient, "content": c.content, "given_at": c.given_at.isoformat()}), 201


@app.route("/api/compliments/<int:compliment_id>", methods=["DELETE"])
@login_required
def api_delete_compliment(compliment_id):
    c = db.session.get(Compliment, compliment_id)
    if not c:
        return jsonify({"error": "not found"}), 404
    db.session.delete(c)
    db.session.commit()
    _invalidate_dashboard_cache()
    return jsonify({"ok": True})


# --- Habit Log API ---

@app.route("/api/habits/toggle", methods=["POST"])
@login_required
def api_toggle_habit():
    from sheets import is_habit_logged, add_habit_log, delete_habit_log
    habit_name = request.json.get("habit_name", "")
    if not habit_name:
        return jsonify({"error": "habit_name required"}), 400
    date_str = request.json.get("date")
    if date_str:
        try:
            target_date = date.fromisoformat(date_str)
        except ValueError:
            return jsonify({"error": "invalid date format, use YYYY-MM-DD"}), 400
    else:
        target_date = date.today()
    if is_habit_logged(habit_name, target_date):
        delete_habit_log(habit_name, target_date)
        action = "undone"
    else:
        add_habit_log(habit_name, target_date)
        action = "done"
    _invalidate_dashboard_cache()
    return jsonify({"action": action, **_habit_stats(habit_name)})


# --- Entity API ---

@app.route("/api/entities", methods=["GET"])
@login_required
def api_get_entities():
    try:
        from sheets_entities import get_all_entities
        from scoring import sort_entities_by_score
        entities = get_all_entities()
        scored = sort_entities_by_score(entities)
        return jsonify({"entities": scored})
    except Exception as e:
        logger.error("Failed to get entities: %s", e)
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


@app.route("/api/entities", methods=["POST"])
@login_required
def api_add_entity():
    try:
        from sheets_entities import add_entity
        data = request.get_json()
        if not data or not data.get("name"):
            return jsonify({"error": "Name is required"}), 400
        entity_hmac = add_entity(data)
        return jsonify({"success": True, "entity_hmac": entity_hmac})
    except Exception as e:
        logger.error("Failed to add entity: %s", e)
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


@app.route("/api/entities/deleted", methods=["GET"])
@login_required
def api_get_deleted_entities():
    try:
        from sheets_entities import get_deleted_entities
        entities = get_deleted_entities()
        return jsonify({"entities": entities})
    except Exception as e:
        logger.error("Failed to get deleted entities: %s", e)
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


@app.route("/api/entities/<entity_hmac>", methods=["PUT"])
@login_required
def api_update_entity(entity_hmac):
    try:
        from sheets_entities import update_entity
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
        success = update_entity(entity_hmac, data)
        if not success:
            return jsonify({"error": "Entity not found"}), 404
        return jsonify({"success": True})
    except Exception as e:
        logger.error("Failed to update entity: %s", e)
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


@app.route("/api/entities/<entity_hmac>", methods=["DELETE"])
@login_required
def api_delete_entity(entity_hmac):
    try:
        from sheets_entities import delete_entity
        success = delete_entity(entity_hmac, deleted_by="User")
        if not success:
            return jsonify({"error": "Entity not found"}), 404
        return jsonify({"success": True})
    except Exception as e:
        logger.error("Failed to delete entity: %s", e)
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


@app.route("/api/entities/<entity_hmac>/restore", methods=["POST"])
@login_required
def api_restore_entity(entity_hmac):
    try:
        from sheets_entities import restore_entity
        success = restore_entity(entity_hmac)
        if not success:
            return jsonify({"error": "Entity not found in trash"}), 404
        return jsonify({"success": True})
    except Exception as e:
        logger.error("Failed to restore entity: %s", e)
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


@app.route("/api/entities/<entity_hmac>/permanent", methods=["DELETE"])
@login_required
def api_permanent_delete_entity(entity_hmac):
    try:
        from sheets_entities import permanent_delete_entity
        success = permanent_delete_entity(entity_hmac)
        if not success:
            return jsonify({"error": "Entity not found in trash"}), 404
        return jsonify({"success": True})
    except Exception as e:
        logger.error("Failed to permanently delete entity: %s", e)
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


@app.route("/api/entities/<entity_hmac>/logs", methods=["GET"])
@login_required
def api_get_entity_logs(entity_hmac):
    try:
        from sheets_entities import get_entity_logs
        logs = get_entity_logs(entity_hmac)
        return jsonify({"logs": logs})
    except Exception as e:
        logger.error("Failed to get entity logs: %s", e)
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


@app.route("/api/entities/<entity_hmac>/opportunities", methods=["GET"])
@login_required
def api_get_opportunities(entity_hmac):
    try:
        from sheets_entities import get_entity_opportunities
        opps = get_entity_opportunities(entity_hmac)
        return jsonify({"opportunities": opps})
    except Exception as e:
        logger.error("Failed to get opportunities: %s", e)
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


@app.route("/api/entities/<entity_hmac>/opportunities", methods=["POST"])
@login_required
def api_add_opportunity(entity_hmac):
    try:
        from sheets_entities import add_opportunity
        data = request.get_json()
        if not data or not data.get("title"):
            return jsonify({"error": "Title is required"}), 400
        opp_id, created = add_opportunity(entity_hmac, data["title"], data.get("details", ""))
        if not created:
            return jsonify({"error": "같은 제목의 Opportunity가 이미 존재합니다."}), 409
        return jsonify({"success": True, "opp_id": opp_id})
    except Exception as e:
        logger.error("Failed to add opportunity: %s", e)
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


@app.route("/api/entities/<entity_hmac>/opportunities/<opp_id>", methods=["PUT"])
@login_required
def api_update_opportunity(entity_hmac, opp_id):
    try:
        from sheets_entities import update_opportunity
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
        success = update_opportunity(entity_hmac, opp_id,
                                     title=data.get("title"),
                                     details=data.get("details"))
        if not success:
            return jsonify({"error": "Opportunity not found"}), 404
        return jsonify({"success": True})
    except Exception as e:
        logger.error("Failed to update opportunity: %s", e)
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


@app.route("/api/entities/<entity_hmac>/opportunities/<opp_id>", methods=["DELETE"])
@login_required
def api_delete_opportunity(entity_hmac, opp_id):
    try:
        from sheets_entities import delete_opportunity
        success = delete_opportunity(entity_hmac, opp_id)
        if not success:
            return jsonify({"error": "Opportunity not found"}), 404
        return jsonify({"success": True})
    except Exception as e:
        logger.error("Failed to delete opportunity: %s", e)
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


@app.route("/api/entities/<entity_hmac>/suggested-contacts", methods=["GET"])
@login_required
def api_get_suggested_contacts(entity_hmac):
    try:
        from sheets_entities import get_suggested_contacts
        result = get_suggested_contacts(entity_hmac)
        return jsonify(result)
    except Exception as e:
        logger.error("Failed to get suggested contacts: %s", e)
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


# --- Contact API ---

@app.route("/api/contacts", methods=["GET"])
@login_required
def api_get_contacts():
    """Get all contacts with scores."""
    try:
        from sheets import get_all_contacts
        from scoring import sort_contacts_by_score
        contacts = get_all_contacts()
        scored = sort_contacts_by_score(contacts)
        return jsonify({"contacts": scored})
    except Exception as e:
        logger.error("Failed to get contacts: %s", e)
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


@app.route("/api/contacts", methods=["POST"])
@login_required
def api_add_contact():
    """Add a new contact."""
    try:
        from sheets import add_contact, get_valid_tags
        from validation import validate_contact

        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        valid_tags = get_valid_tags()
        is_valid, errors = validate_contact(data, valid_tags)
        if not is_valid:
            return jsonify({"error": "Validation failed", "errors": errors}), 400

        name_hmac = add_contact(data)
        return jsonify({"success": True, "name_hmac": name_hmac})
    except Exception as e:
        logger.error("Failed to add contact: %s", e)
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


@app.route("/api/contacts/<name_hmac>", methods=["PUT"])
@login_required
def api_update_contact(name_hmac):
    """Update a contact's fields."""
    try:
        from sheets import update_contact, get_valid_tags
        from validation import validate_update_fields

        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        valid_tags = get_valid_tags()
        is_valid, errors = validate_update_fields(data, valid_tags)
        if not is_valid:
            return jsonify({"error": "Validation failed", "errors": errors}), 400

        success, _ = update_contact(name_hmac, data)
        if not success:
            return jsonify({"error": "Contact not found"}), 404
        return jsonify({"success": True})
    except Exception as e:
        logger.error("Failed to update contact: %s", e)
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


@app.route("/api/contacts/deleted", methods=["GET"])
@login_required
def api_get_deleted_contacts():
    """Get all soft-deleted contacts."""
    try:
        from sheets import get_deleted_contacts
        contacts = get_deleted_contacts()
        return jsonify({"contacts": contacts})
    except Exception as e:
        logger.error("Failed to get deleted contacts: %s", e)
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


@app.route("/api/contacts/scan-card", methods=["POST"])
@login_required
@limiter.limit("5 per minute")
def api_scan_card():
    import base64, json as _json

    if "image" not in request.files:
        return jsonify({"error": "이미지 파일이 필요합니다."}), 400

    file = request.files["image"]
    mime_type = file.mimetype or "image/jpeg"
    if mime_type not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
        return jsonify({"error": "JPG, PNG, WEBP만 지원됩니다."}), 400

    file_bytes = file.read()
    if len(file_bytes) > 5 * 1024 * 1024:
        return jsonify({"error": "이미지 크기가 5MB를 초과합니다."}), 400

    try:
        image_b64 = base64.standard_b64encode(file_bytes).decode("utf-8")
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": image_b64}},
                    {"type": "text", "text": (
                        "이 명함 이미지에서 다음 정보를 추출하세요. 한국어·영어 명함 모두 지원합니다.\n"
                        "반드시 아래 JSON 형식만 반환하세요. 다른 텍스트는 일절 포함하지 마세요:\n"
                        '{"name":"이름","employer":"회사명","title":"직책","email":"이메일","phone":"전화번호(숫자·하이픈만)"}\n'
                        "추출 불가 필드는 빈 문자열로 설정. 전화번호가 여러 개면 휴대폰 우선.\n"
                        "명함이 아니거나 읽을 수 없으면 모든 필드를 빈 문자열로 반환."
                    )}
                ]
            }]
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = "\n".join(l for l in raw.split("\n") if not l.startswith("```")).strip()

        extracted = _json.loads(raw)
        result = {k: str(extracted.get(k, "")).strip()
                  for k in ("name", "employer", "title", "email", "phone")}

        if not any(result.values()):
            return jsonify({"success": True, "extracted": result,
                            "warning": "명함에서 텍스트를 찾을 수 없었습니다. 이미지가 명확한지 확인해 주세요."})
        return jsonify({"success": True, "extracted": result})

    except _json.JSONDecodeError:
        logger.error("OCR JSON parse failed: %s", raw)
        return jsonify({"error": "AI 응답 파싱에 실패했습니다. 다시 시도해 주세요."}), 500
    except anthropic.APIStatusError as e:
        logger.error("Anthropic API error: %s", e)
        return jsonify({"error": "AI 서비스 오류입니다. 잠시 후 다시 시도해 주세요."}), 502
    except Exception as e:
        logger.error("Card scan failed: %s", e)
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


@app.route("/api/contacts/<name_hmac>/restore", methods=["POST"])
@login_required
def api_restore_contact(name_hmac):
    """Restore a contact from Deleted tab back to Master."""
    try:
        from sheets import restore_contact
        success = restore_contact(name_hmac)
        if not success:
            return jsonify({"error": "Contact not found in trash"}), 404
        return jsonify({"success": True})
    except Exception as e:
        logger.error("Failed to restore contact: %s", e)
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


@app.route("/api/contacts/<name_hmac>/permanent", methods=["DELETE"])
@login_required
def api_permanent_delete(name_hmac):
    """Permanently delete a contact from Deleted tab."""
    try:
        from sheets import permanent_delete
        success = permanent_delete(name_hmac)
        if not success:
            return jsonify({"error": "Contact not found in trash"}), 404
        return jsonify({"success": True})
    except Exception as e:
        logger.error("Failed to permanently delete contact: %s", e)
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


@app.route("/api/contacts/<name_hmac>", methods=["DELETE"])
@login_required
def api_delete_contact(name_hmac):
    """Soft-delete a contact (move to trash)."""
    try:
        from sheets import delete_contact
        success = delete_contact(name_hmac, deleted_by="User")
        if not success:
            return jsonify({"error": "Contact not found"}), 404
        return jsonify({"success": True})
    except Exception as e:
        logger.error("Failed to delete contact: %s", e)
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


# --- Interaction Log API ---

@app.route("/api/contacts/<name_hmac>/logs", methods=["GET"])
@login_required
def api_get_contact_logs(name_hmac):
    """Get interaction logs for a contact."""
    try:
        from sheets import get_interaction_logs
        logs = get_interaction_logs(name_hmac)
        return jsonify({"logs": logs})
    except Exception as e:
        logger.error("Failed to get logs: %s", e)
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


# --- Tag API ---

@app.route("/api/tags", methods=["GET"])
@login_required
def api_get_tags():
    try:
        from sheets import get_valid_tags
        tags = get_valid_tags()
        return jsonify({"tags": tags})
    except Exception as e:
        logger.error("Failed to get tags: %s", e)
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


@app.route("/api/tags", methods=["POST"])
@login_required
def api_add_tag():
    try:
        from sheets import add_tag
        data = request.get_json()
        tag_name = data.get("tag_name", "").strip()
        if not tag_name:
            return jsonify({"error": "Tag name required"}), 400
        add_tag(tag_name)
        return jsonify({"success": True})
    except Exception as e:
        logger.error("Failed to add tag: %s", e)
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


# --- Contact Chat API ---

@app.route("/api/chat", methods=["POST"])
@login_required
def api_contact_chat():
    """Process a chat message with the contact AI agent."""
    try:
        from ai_agent import chat_contact
        from sheets import (
            add_contact, add_interaction_log, find_contact_by_name,
            update_contact, get_valid_tags,
        )
        from validation import validate_update_fields

        data = request.get_json()
        user_message = data.get("message", "").strip()
        if not user_message:
            return jsonify({"error": "Message required"}), 400
        if len(user_message) < 2 or len(user_message) > 2000:
            return jsonify({"error": "메시지는 2자 이상 2000자 이하로 입력해 주세요."}), 400

        # Get conversation history from DB
        history_msgs = (
            ContactChatMessage.query
            .order_by(ContactChatMessage.created_at.desc())
            .limit(50).all()
        )
        conversation_history = [
            {"role": m.role, "content": m.content} for m in reversed(history_msgs)
        ]

        # Call AI agent
        result = chat_contact(user_message, conversation_history)

        # Process actions
        executed_actions = []
        pending_actions = []

        for action in result.get("actions", []):
            action_type = action.get("action", "")
            entity_type = action.get("entity_type", "contact")
            name = action.get("name", "")

            # --- Business Entity & Opportunity actions ---
            if entity_type == "business_entity":
                # search_entity는 읽기 전용 — 항상 실행
                if action_type == "search_entity":
                    from sheets_entities import find_entity_by_name
                    matches = find_entity_by_name(name) if name else []
                    executed_actions.append({
                        "type": "search_entity",
                        "name": name,
                        "results": matches,
                    })

                # add_entity — 항상 사용자 확인 필요
                elif action_type == "add_entity":
                    pending_actions.append({**action, "reason": "새 비즈니스 엔티티 추가 — 확인 후 실행"})

                # Entity CRUD — 모두 pending
                elif action_type in ("update_entity", "delete_entity"):
                    from sheets_entities import find_entity_by_name
                    matches = find_entity_by_name(name)
                    if len(matches) == 1:
                        pending_actions.append({**action, "reason": "확인이 필요합니다"})
                    else:
                        pending_actions.append({**action, "reason": "엔티티를 찾을 수 없음"})

                # Opportunity CRUD — 모두 pending
                elif action_type == "add_opp_to_entity":
                    from sheets_entities import find_entity_by_name
                    matches = find_entity_by_name(name)
                    opp_title = action.get("opp_title", "")
                    if len(matches) == 1 and opp_title:
                        pending_actions.append({**action, "reason": "확인이 필요합니다"})
                    else:
                        reason = "기회 제목(opp_title) 누락" if not opp_title else "엔티티를 찾을 수 없음"
                        pending_actions.append({**action, "reason": reason})

                elif action_type in ("update_opp", "delete_opp"):
                    from sheets_entities import find_entity_by_name
                    matches = find_entity_by_name(name)
                    opp_id = action.get("opp_id", "")
                    if len(matches) == 1 and opp_id:
                        pending_actions.append({**action, "reason": "확인이 필요합니다"})
                    else:
                        pending_actions.append({**action, "reason": "엔티티 또는 opp_id를 찾을 수 없음"})

                else:
                    logger.warning("Unknown business_entity action: %s", action_type)
                    pending_actions.append({**action, "reason": "알 수 없는 액션"})

                continue  # business_entity 처리 완료 → contact 블록 skip

            # --- Contact actions (READ-ONLY: search) ---
            if action_type == "search":
                matches = find_contact_by_name(name) if name else []
                executed_actions.append({
                    "type": "search",
                    "name": name,
                    "results": matches,
                })
                continue

            # --- Contact write actions (always pending) ---
            if action_type == "update_contact":
                matches = find_contact_by_name(name)
                if len(matches) == 1:
                    contact = matches[0]
                    current_values = {
                        k: contact[k] for k in action.get("fields", {}) if contact.get(k)
                    }
                    pending_actions.append({
                        **action,
                        "reason": "확인이 필요합니다",
                        "current_values": current_values,
                    })
                elif len(matches) > 1:
                    pending_actions.append({
                        **action,
                        "reason": "동명이인 발견",
                        "candidates": [
                            {"name": m["name"], "employer": m.get("employer", ""), "name_hmac": m["name_hmac"]}
                            for m in matches
                        ],
                    })
                else:
                    executed_actions.append({"type": "not_found", "name": name, "reason": "연락처를 찾을 수 없음"})

            elif action_type == "add_contact":
                pending_actions.append({**action, "reason": "새 연락처 추가 — 확인 후 실행"})

            elif action_type == "delete_contact":
                matches = find_contact_by_name(name)
                if len(matches) == 0:
                    executed_actions.append({"type": "not_found", "name": name, "reason": "연락처를 찾을 수 없음"})
                elif len(matches) == 1:
                    pending_actions.append({**action, "reason": "삭제를 확인해 주세요"})
                else:
                    pending_actions.append({
                        **action,
                        "reason": "동명이인 발견",
                        "candidates": [
                            {"name": m["name"], "employer": m.get("employer", ""), "name_hmac": m["name_hmac"]}
                            for m in matches
                        ],
                    })

            else:
                logger.warning("Unknown contact action: %s", action_type)
                pending_actions.append({**action, "reason": "알 수 없는 액션"})

        # Save messages to DB
        user_msg = ContactChatMessage(role="user", content=user_message)
        assistant_msg = ContactChatMessage(
            role="assistant",
            content=result["message"],
            actions_json=json.dumps(result.get("actions", []), ensure_ascii=False),
        )
        db.session.add(user_msg)
        db.session.add(assistant_msg)
        db.session.commit()

        return jsonify({
            "message": result["message"],
            "executed_actions": executed_actions,
            "pending_actions": pending_actions,
        })

    except Exception as e:
        logger.error("Contact chat error: %s", e, exc_info=True)
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


@app.route("/api/chat/confirm", methods=["POST"])
@login_required
def api_contact_chat_confirm():
    """Confirm and execute a pending contact chat action."""
    try:
        from sheets import (
            add_contact, add_interaction_log, update_contact, get_valid_tags,
        )
        from validation import validate_update_fields

        data = request.get_json()
        action = data.get("action", {})
        selected_hmac = data.get("selected_hmac", "")

        action_type = action.get("action", "")
        name = action.get("name", "")

        if action_type == "update_contact":
            name_hmac = selected_hmac
            if not name_hmac:
                from sheets import find_contact_by_name
                matches = find_contact_by_name(name)
                if len(matches) == 1:
                    name_hmac = matches[0]["name_hmac"]
                else:
                    return jsonify({"error": "연락처를 특정할 수 없습니다."}), 400

            fields = action.get("fields", {})
            # YYYY-MM → YYYY-MM-01 정규화
            for _df in ('last_contact', 'follow_up_date'):
                if _df in fields and re.match(r'^\d{4}-\d{2}$', str(fields[_df])):
                    fields[_df] = fields[_df] + '-01'
            valid_tags = get_valid_tags()
            is_valid, errors = validate_update_fields(fields, valid_tags)
            if not is_valid:
                return jsonify({"error": "Validation failed", "errors": errors}), 400

            update_contact(name_hmac, fields, changed_by="AI")

            interaction_log = action.get("interaction_log", "")
            if interaction_log:
                from sheets import find_contact_by_hmac
                contact = find_contact_by_hmac(name_hmac)
                display = f"{contact['name']}({contact['employer']})" if contact and contact.get("employer") else name
                key_extract = action.get("key_value_extract", "")
                updated_fields_str = ", ".join(fields.keys())
                add_interaction_log(name_hmac, display, interaction_log,
                                    key_extract, updated_fields_str)

            return jsonify({"success": True, "type": "update"})

        elif action_type == "add_contact":
            from sheets import find_contact_by_name
            existing = find_contact_by_name(name)
            if existing:
                names_str = ", ".join(
                    f"{c['name']}({c.get('employer', '')})" for c in existing
                )
                return jsonify({
                    "error": f"이미 존재하는 연락처입니다: {names_str}",
                    "duplicate": True,
                }), 409
            fields = action.get("fields", {})
            new_contact = {"name": name, **fields}
            name_hmac = add_contact(new_contact)
            return jsonify({"success": True, "type": "add", "name_hmac": name_hmac})

        elif action_type == "delete_contact":
            from sheets import find_contact_by_name, delete_contact
            name_hmac = selected_hmac
            if not name_hmac:
                matches = find_contact_by_name(name)
                if len(matches) == 1:
                    name_hmac = matches[0]["name_hmac"]
                else:
                    return jsonify({"error": "연락처를 특정할 수 없습니다."}), 400
            success = delete_contact(name_hmac, deleted_by="AI")
            if not success:
                return jsonify({"error": "연락처를 찾을 수 없습니다."}), 404
            return jsonify({"success": True, "type": "delete"})

        elif action_type == "add_entity":
            from sheets_entities import find_entity_by_name, add_entity
            existing = find_entity_by_name(name)
            if existing:
                names_str = ", ".join(e["name"] for e in existing)
                return jsonify({
                    "error": f"이미 존재하는 엔티티입니다: {names_str}",
                    "duplicate": True,
                }), 409
            fields = action.get("fields", {})
            new_entity = {"name": name, **fields}
            entity_hmac = add_entity(new_entity)
            return jsonify({"success": True, "type": "add_entity", "entity_hmac": entity_hmac})

        elif action_type == "update_entity":
            from sheets_entities import find_entity_by_name, update_entity, add_entity_log
            entity_hmac = selected_hmac
            if not entity_hmac:
                matches = find_entity_by_name(name)
                if len(matches) == 1:
                    entity_hmac = matches[0]["entity_hmac"]
                else:
                    return jsonify({"error": "엔티티를 특정할 수 없습니다."}), 400

            fields = action.get("fields", {})
            interaction_log = action.get("interaction_log", "")
            key_extract = action.get("key_value_extract", "")
            
            entity_name = name
            if interaction_log:
                add_entity_log(entity_hmac, entity_name, interaction_log, key_extract, ", ".join(fields.keys()))
            if fields:
                update_entity(entity_hmac, fields, changed_by="AI")
            return jsonify({"success": True, "type": "update_entity"})

        elif action_type == "delete_entity":
            from sheets_entities import find_entity_by_name, delete_entity
            entity_hmac = selected_hmac
            if not entity_hmac:
                matches = find_entity_by_name(name)
                if len(matches) == 1:
                    entity_hmac = matches[0]["entity_hmac"]
                else:
                    return jsonify({"error": "엔티티를 특정할 수 없습니다."}), 400
            delete_entity(entity_hmac, deleted_by="AI")
            return jsonify({"success": True, "type": "delete_entity"})

        elif action_type == "add_opp_to_entity":
            from sheets_entities import find_entity_by_name, add_opportunity
            entity_hmac = selected_hmac
            if not entity_hmac:
                matches = find_entity_by_name(name)
                if len(matches) == 1:
                    entity_hmac = matches[0]["entity_hmac"]
                else:
                    return jsonify({"error": "엔티티를 특정할 수 없습니다."}), 400
            opp_title = action.get("opp_title", "")
            opp_details = action.get("opp_details", "")
            add_opportunity(entity_hmac, opp_title, opp_details)
            return jsonify({"success": True, "type": "add_opp_to_entity"})

        elif action_type == "update_opp":
            from sheets_entities import find_entity_by_name, update_opportunity
            entity_hmac = selected_hmac
            if not entity_hmac:
                matches = find_entity_by_name(name)
                if len(matches) == 1:
                    entity_hmac = matches[0]["entity_hmac"]
                else:
                    return jsonify({"error": "엔티티를 특정할 수 없습니다."}), 400
            opp_title = action.get("opp_title") or None
            opp_details = action.get("opp_details") or None
            opp_id = action.get("opp_id", "")
            update_opportunity(entity_hmac, opp_id, title=opp_title, details=opp_details)
            return jsonify({"success": True, "type": "update_opp"})

        elif action_type == "delete_opp":
            from sheets_entities import find_entity_by_name, delete_opportunity
            entity_hmac = selected_hmac
            if not entity_hmac:
                matches = find_entity_by_name(name)
                if len(matches) == 1:
                    entity_hmac = matches[0]["entity_hmac"]
                else:
                    return jsonify({"error": "엔티티를 특정할 수 없습니다."}), 400
            opp_id = action.get("opp_id", "")
            delete_opportunity(entity_hmac, opp_id)
            return jsonify({"success": True, "type": "delete_opp"})

        return jsonify({"error": "Unknown action type"}), 400

    except Exception as e:
        logger.error("Contact chat confirm error: %s", e, exc_info=True)
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


@app.route("/api/chat/history", methods=["GET"])
@login_required
def api_contact_chat_history():
    """Get contact chat history."""
    messages = (ContactChatMessage.query
                .order_by(ContactChatMessage.created_at.desc())
                .limit(50).all())
    return jsonify({
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                "actions_json": m.actions_json,
                "created_at": m.created_at.isoformat() if m.created_at else "",
            }
            for m in reversed(messages)
        ]
    })


@app.route("/api/chat/clear", methods=["POST"])
@login_required
def api_contact_chat_clear():
    """Clear contact chat history."""
    ContactChatMessage.query.delete()
    db.session.commit()
    return jsonify({"success": True})


@app.route("/admin/security")
@login_required
@admin_required
def admin_security():
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())
    month_start = today_start.replace(day=1)

    # Recent 50 login logs
    recent_logs = LoginLog.query.order_by(LoginLog.created_at.desc()).limit(50).all()

    # Today stats
    today_success = LoginLog.query.filter(LoginLog.created_at >= today_start, LoginLog.success == True).count()
    today_fail = LoginLog.query.filter(LoginLog.created_at >= today_start, LoginLog.success == False).count()

    # This week stats
    week_success = LoginLog.query.filter(LoginLog.created_at >= week_start, LoginLog.success == True).count()
    week_fail = LoginLog.query.filter(LoginLog.created_at >= week_start, LoginLog.success == False).count()

    # This month stats
    month_success = LoginLog.query.filter(LoginLog.created_at >= month_start, LoginLog.success == True).count()
    month_fail = LoginLog.query.filter(LoginLog.created_at >= month_start, LoginLog.success == False).count()

    # Last 7 days chart data
    chart_labels = []
    chart_success = []
    chart_fail = []
    for i in range(6, -1, -1):
        day = today_start - timedelta(days=i)
        next_day = day + timedelta(days=1)
        chart_labels.append(day.strftime("%m/%d"))
        chart_success.append(LoginLog.query.filter(
            LoginLog.created_at >= day, LoginLog.created_at < next_day, LoginLog.success == True
        ).count())
        chart_fail.append(LoginLog.query.filter(
            LoginLog.created_at >= day, LoginLog.created_at < next_day, LoginLog.success == False
        ).count())

    # Unique IPs
    unique_ips = db.session.query(db.func.count(db.distinct(LoginLog.ip_address))).scalar() or 0

    # Total logs
    total_logs = LoginLog.query.count()

    # Recent failures (last 10)
    recent_failures = LoginLog.query.filter_by(success=False).order_by(LoginLog.created_at.desc()).limit(10).all()

    # Security checklist
    secret_key_set = os.environ.get("SECRET_KEY", "") != ""
    db_url_set = os.environ.get("DATABASE_URL", "") != ""
    dashboard_user_set = os.environ.get("DASHBOARD_USER", "") != ""
    csrf_enabled = "csrf" in app.extensions
    login_ratelimit_set = True

    push_sub_count = PushSubscription.query.count()
    push_pref = NotificationPreference.query.filter_by(user_id=current_user.id).first()

    return render_template("admin_security.html",
        recent_logs=recent_logs,
        today_success=today_success, today_fail=today_fail,
        week_success=week_success, week_fail=week_fail,
        month_success=month_success, month_fail=month_fail,
        chart_labels=chart_labels, chart_success=chart_success, chart_fail=chart_fail,
        unique_ips=unique_ips, total_logs=total_logs,
        recent_failures=recent_failures,
        secret_key_set=secret_key_set, db_url_set=db_url_set,
        dashboard_user_set=dashboard_user_set,
        csrf_enabled=csrf_enabled,
        login_ratelimit_set=login_ratelimit_set,
        push_sub_count=push_sub_count,
        push_pref=push_pref,
    )


@app.route("/api/push/subscribe", methods=["POST"])
@login_required
def push_subscribe():
    data = request.get_json()
    endpoint = data.get("endpoint")
    p256dh = data.get("keys", {}).get("p256dh")
    auth = data.get("keys", {}).get("auth")
    if not all([endpoint, p256dh, auth]):
        return jsonify({"error": "Invalid subscription data"}), 400
    existing = PushSubscription.query.filter_by(endpoint=endpoint).first()
    if existing:
        existing.p256dh = p256dh
        existing.auth = auth
    else:
        db.session.add(PushSubscription(
            user_id=current_user.id, endpoint=endpoint, p256dh=p256dh, auth=auth
        ))
    db.session.commit()
    return jsonify({"status": "subscribed"}), 201


@app.route("/api/push/unsubscribe", methods=["DELETE"])
@login_required
def push_unsubscribe():
    data = request.get_json()
    endpoint = data.get("endpoint")
    PushSubscription.query.filter_by(
        endpoint=endpoint, user_id=current_user.id
    ).delete()
    db.session.commit()
    return jsonify({"status": "unsubscribed"}), 200


@app.route("/api/push/preferences", methods=["GET"])
@login_required
def push_get_prefs():
    pref = NotificationPreference.query.filter_by(user_id=current_user.id).first()
    if not pref:
        pref = NotificationPreference(user_id=current_user.id)
        db.session.add(pref)
        db.session.commit()
    return jsonify({
        "contacts_today": pref.contacts_today,
        "contacts_overdue": pref.contacts_overdue,
        "business_today": pref.business_today,
        "business_overdue": pref.business_overdue,
    })


@app.route("/api/push/preferences", methods=["PUT"])
@login_required
def push_update_prefs():
    data = request.get_json()
    pref = NotificationPreference.query.filter_by(user_id=current_user.id).first()
    if not pref:
        pref = NotificationPreference(user_id=current_user.id)
        db.session.add(pref)
    pref.contacts_today = bool(data.get("contacts_today", True))
    pref.contacts_overdue = bool(data.get("contacts_overdue", True))
    pref.business_today = bool(data.get("business_today", True))
    pref.business_overdue = bool(data.get("business_overdue", True))
    db.session.commit()
    return jsonify({"status": "saved"})


@app.route("/api/push/test", methods=["POST"])
@login_required
def push_test():
    if not Config.VAPID_PRIVATE_KEY or not Config.VAPID_PUBLIC_KEY:
        return jsonify({"message": "VAPID 키 미설정/형식오류 — Railway 환경변수를 확인하세요"})
    subs = PushSubscription.query.all()
    if not subs:
        return jsonify({"message": "구독된 기기가 없습니다. 먼저 🔔를 눌러 알림을 구독하세요"})
    sent, failed = 0, 0
    for sub in subs:
        payload = json.dumps({
            "title": "테스트 알림",
            "body": "Web Push 알림이 정상 작동합니다 ✓",
            "url": "/contacts"
        })
        try:
            webpush(
                subscription_info={"endpoint": sub.endpoint,
                                   "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
                data=payload,
                vapid_private_key=Config.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": f"mailto:{Config.VAPID_CLAIM_EMAIL}"}
            )
            sent += 1
        except WebPushException as e:
            if e.response and e.response.status_code in (404, 410):
                db.session.delete(sub)
                db.session.commit()
            logger.error("push_test: WebPushException: %s", e)
            failed += 1
        except Exception as e:
            logger.error("push_test: unexpected error: %s", e)
            failed += 1
    return jsonify({"message": f"발송 {sent}개 / 실패 {failed}개 (전체 구독 {len(subs)}개)"})


@app.route("/api/admin/clear-logs", methods=["POST"])
@login_required
@admin_required
def clear_old_logs():
    """Delete login logs older than 90 days."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    count = LoginLog.query.filter(LoginLog.created_at < cutoff).delete()
    db.session.commit()
    return jsonify({"status": "ok", "cleared": count})


@app.route("/api/admin/clear-read/<keyword>", methods=["POST"])
@login_required
@admin_required
def clear_read_history(keyword):
    """Remove read-history entries whose URL contains the given keyword."""
    entries = ReadArticle.query.filter(ReadArticle.url.contains(keyword)).all()
    count = len(entries)
    for e in entries:
        db.session.delete(e)
    db.session.commit()
    return jsonify({"status": "ok", "cleared": count, "keyword": keyword})


# --- Anki SRS ---

def _apply_srs_review(card, rating):
    """Apply SM-2 algorithm. rating: 1=Again, 2=Hard, 3=Good, 4=Easy."""
    if rating == 1:
        card.repetitions = 0
        card.interval = 1
        card.ease_factor = max(1.3, card.ease_factor - 0.2)
    elif rating == 2:
        card.interval = max(2, round(card.interval * 1.2))
        card.ease_factor = max(1.3, card.ease_factor - 0.15)
        card.repetitions += 1
    elif rating == 3:
        if card.repetitions == 0:
            card.interval = 1
        elif card.repetitions == 1:
            card.interval = 6
        else:
            card.interval = round(card.interval * card.ease_factor)
        card.repetitions += 1
    elif rating == 4:
        if card.repetitions == 0:
            card.interval = 4
        else:
            card.interval = round(card.interval * card.ease_factor * 1.3)
        card.ease_factor = min(2.5, card.ease_factor + 0.15)
        card.repetitions += 1
    card.next_review = date.today() + timedelta(days=card.interval)
    card.last_reviewed = datetime.now(timezone.utc)


@app.route("/anki")
@login_required
def anki_hub():
    today = date.today()
    due_count = AnkiCard.query.filter(
        AnkiCard.status == 'active',
        AnkiCard.next_review <= today
    ).count()
    total_active = AnkiCard.query.filter_by(status='active').count()
    total_archived = AnkiCard.query.filter_by(status='archived').count()
    decks = AnkiDeck.query.order_by(AnkiDeck.created_at.desc()).all()

    from sqlalchemy import func as sa_func
    active_counts = dict(
        db.session.query(AnkiCard.deck_id, sa_func.count(AnkiCard.id))
        .filter_by(status='active')
        .group_by(AnkiCard.deck_id)
        .all()
    )
    due_counts = dict(
        db.session.query(AnkiCard.deck_id, sa_func.count(AnkiCard.id))
        .filter(AnkiCard.status == 'active', AnkiCard.next_review <= today)
        .group_by(AnkiCard.deck_id)
        .all()
    )
    deck_stats = [
        {'deck': deck, 'active_count': active_counts.get(deck.id, 0),
         'due_count': due_counts.get(deck.id, 0)}
        for deck in decks
    ]

    return render_template(
        "anki.html",
        due_count=due_count,
        total_active=total_active,
        total_archived=total_archived,
        deck_stats=deck_stats,
    )


@app.route("/anki/review")
@login_required
def anki_review():
    return render_template("anki_review.html")


@app.route("/anki/deck/<int:deck_id>")
@login_required
def anki_deck(deck_id):
    deck = db.session.get(AnkiDeck, deck_id)
    if not deck:
        return redirect(url_for('anki_hub'))
    cards = AnkiCard.query.filter_by(deck_id=deck_id).order_by(AnkiCard.next_review.asc()).all()
    return render_template("anki_deck.html", deck=deck, cards=cards, today=date.today())


@app.route("/api/anki/due")
@login_required
def api_anki_due():
    today = date.today()
    cards = AnkiCard.query.filter(
        AnkiCard.status == 'active',
        AnkiCard.next_review <= today
    ).order_by(AnkiCard.next_review.asc()).all()
    return jsonify([{
        'id': c.id,
        'front': c.front,
        'back': c.back,
        'card_type': c.card_type,
        'source_ref': c.source_ref,
        'interval': c.interval,
        'repetitions': c.repetitions,
    } for c in cards])


@app.route("/api/anki/upload/preview", methods=["POST"])
@login_required
def api_anki_upload_preview():
    if 'file' not in request.files:
        return jsonify({"error": "파일이 없습니다."}), 400
    MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5MB

    f = request.files['file']
    filename = f.filename or ''
    content_bytes = f.read(MAX_UPLOAD_BYTES + 1)
    if len(content_bytes) > MAX_UPLOAD_BYTES:
        return jsonify({"error": "파일 크기는 5MB 이하여야 합니다."}), 400
    if filename.lower().endswith('.pdf'):
        content = content_bytes
    else:
        content = content_bytes.decode('utf-8', errors='replace')

    from anki_parser import parse_auto
    cards = parse_auto(content, filename)
    if not cards:
        return jsonify({"error": "카드를 파싱할 수 없습니다. 파일 형식을 확인하세요."}), 422

    # Check for duplicate deck by source_file
    duplicate = AnkiDeck.query.filter_by(source_file=filename).first()
    dup_info = None
    if duplicate:
        dup_info = {'id': duplicate.id, 'name': duplicate.name}

    # Auto-detect deck name and author from first card
    deck_name = cards[0].get('deck_name', '') if cards else ''
    author = cards[0].get('author', '') if cards else ''

    return jsonify({
        'total': len(cards),
        'preview': cards[:5],
        'all_cards': cards,
        'deck_name': deck_name,
        'author': author,
        'source_file': filename,
        'duplicate_deck': dup_info,
    })


@app.route("/api/anki/upload/confirm", methods=["POST"])
@login_required
def api_anki_upload_confirm():
    data = request.get_json()
    if not data:
        return jsonify({"error": "데이터가 없습니다."}), 400

    cards_data = data.get('cards', [])
    deck_name = data.get('deck_name', '').strip()
    author = data.get('author', '').strip()
    source_file = data.get('source_file', '').strip()
    overwrite = data.get('overwrite', False)

    if not deck_name:
        return jsonify({"error": "덱 이름이 필요합니다."}), 400
    if not cards_data:
        return jsonify({"error": "카드 데이터가 없습니다."}), 400

    if overwrite and source_file:
        existing = AnkiDeck.query.filter_by(source_file=source_file).first()
        if existing:
            db.session.delete(existing)
            db.session.commit()

    deck = AnkiDeck(
        name=deck_name,
        author=author,
        source_file=source_file,
        source_type='highlight',
    )
    db.session.add(deck)
    db.session.flush()  # get deck.id

    for card_data in cards_data:
        card = AnkiCard(
            deck_id=deck.id,
            card_type='highlight',
            front=card_data.get('front', ''),
            back=card_data.get('back', ''),
            source_ref=card_data.get('source_ref', ''),
        )
        db.session.add(card)

    db.session.commit()
    return jsonify({"deck_id": deck.id, "card_count": len(cards_data)})


@app.route("/api/anki/cards", methods=["POST"])
@login_required
def api_anki_create_card():
    data = request.get_json()
    if not data:
        return jsonify({"error": "데이터가 없습니다."}), 400

    front = data.get('front', '').strip()
    back = data.get('back', '').strip()
    if not front or not back:
        return jsonify({"error": "Front와 Back 내용이 필요합니다."}), 400

    deck_id = data.get('deck_id')
    new_deck_name = data.get('new_deck_name', '').strip()

    if deck_id:
        deck = db.session.get(AnkiDeck, int(deck_id))
        if not deck:
            return jsonify({"error": "덱을 찾을 수 없습니다."}), 404
    elif new_deck_name:
        deck = AnkiDeck(name=new_deck_name, source_type='manual')
        db.session.add(deck)
        db.session.flush()
    else:
        return jsonify({"error": "deck_id 또는 new_deck_name이 필요합니다."}), 400

    card = AnkiCard(
        deck_id=deck.id,
        card_type='qa',
        front=front,
        back=back,
        source_ref=data.get('source_ref', ''),
    )
    db.session.add(card)
    db.session.commit()
    return jsonify({"card_id": card.id, "deck_id": deck.id})


@app.route("/api/anki/cards/<int:card_id>/review", methods=["POST"])
@login_required
def api_anki_review_card(card_id):
    card = db.session.get(AnkiCard, card_id)
    if not card:
        return jsonify({"error": "카드를 찾을 수 없습니다."}), 404
    data = request.get_json()
    rating = data.get('rating')
    if rating not in (1, 2, 3, 4):
        return jsonify({"error": "rating은 1-4 사이여야 합니다."}), 400
    _apply_srs_review(card, rating)
    db.session.commit()
    return jsonify({
        "interval": card.interval,
        "next_review": card.next_review.isoformat(),
        "ease_factor": round(card.ease_factor, 2),
    })


@app.route("/api/anki/cards/<int:card_id>/archive", methods=["POST"])
@login_required
def api_anki_archive_card(card_id):
    card = db.session.get(AnkiCard, card_id)
    if not card:
        return jsonify({"error": "카드를 찾을 수 없습니다."}), 404
    card.status = 'active' if card.status == 'archived' else 'archived'
    db.session.commit()
    return jsonify({"status": card.status})


@app.route("/api/anki/cards/<int:card_id>", methods=["DELETE"])
@login_required
def api_anki_delete_card(card_id):
    card = db.session.get(AnkiCard, card_id)
    if not card:
        return jsonify({"error": "카드를 찾을 수 없습니다."}), 404
    db.session.delete(card)
    db.session.commit()
    return jsonify({"deleted": True})


@app.route("/api/anki/decks/<int:deck_id>", methods=["DELETE"])
@login_required
def api_anki_delete_deck(deck_id):
    deck = db.session.get(AnkiDeck, deck_id)
    if not deck:
        return jsonify({"error": "덱을 찾을 수 없습니다."}), 404
    db.session.delete(deck)
    db.session.commit()
    return jsonify({"status": "ok"})


# --- Init & Run ---

with app.app_context():
    db.create_all()
    # Migrate: add image_url column if missing (SQLite)
    import sqlalchemy
    with db.engine.connect() as conn:
        columns = [r[1] for r in conn.execute(sqlalchemy.text("PRAGMA table_info(article)"))]
        if "image_url" not in columns:
            conn.execute(sqlalchemy.text("ALTER TABLE article ADD COLUMN image_url VARCHAR(1000) DEFAULT ''"))
            conn.commit()
        # Migrate: add hall_of_fame column to my_book if missing
        mb_columns = [r[1] for r in conn.execute(sqlalchemy.text("PRAGMA table_info(my_book)"))]
        if "hall_of_fame" not in mb_columns:
            conn.execute(sqlalchemy.text("ALTER TABLE my_book ADD COLUMN hall_of_fame BOOLEAN DEFAULT 0"))
            conn.commit()
        # Migrate: add password_changed_at column to user if missing
        user_columns = [r[1] for r in conn.execute(sqlalchemy.text("PRAGMA table_info(user)"))]
        if "password_changed_at" not in user_columns:
            conn.execute(sqlalchemy.text("ALTER TABLE user ADD COLUMN password_changed_at DATETIME"))
            conn.commit()
        # Migrate: add TMDB columns to saved_screen if missing
        ss_columns = [r[1] for r in conn.execute(sqlalchemy.text("PRAGMA table_info(saved_screen)"))]
        for col, col_type in [("tmdb_id", "INTEGER"), ("tmdb_title", "VARCHAR(500)"), ("year", "INTEGER"), ("poster_url", "VARCHAR(500)")]:
            if col not in ss_columns:
                conn.execute(sqlalchemy.text(f"ALTER TABLE saved_screen ADD COLUMN {col} {col_type}"))
                conn.commit()
        # Migrate: add position column to insight_keyword if missing
        ik_columns = [r[1] for r in conn.execute(sqlalchemy.text("PRAGMA table_info(insight_keyword)"))]
        if "position" not in ik_columns:
            conn.execute(sqlalchemy.text("ALTER TABLE insight_keyword ADD COLUMN position INTEGER DEFAULT 0"))
            conn.commit()
    # Migrate: create login_log table if missing
    from sqlalchemy import inspect as sa_inspect
    inspector = sa_inspect(db.engine)
    if "login_log" not in inspector.get_table_names():
        LoginLog.__table__.create(db.engine)
    # Migrate: create anki tables if missing
    if "anki_deck" not in inspector.get_table_names():
        AnkiDeck.__table__.create(db.engine)
    if "anki_card" not in inspector.get_table_names():
        AnkiCard.__table__.create(db.engine)
    init_default_user()

    # Ensure indexes exist on pre-existing tables
    with db.engine.connect() as conn:
        conn.execute(sqlalchemy.text("CREATE INDEX IF NOT EXISTS ix_article_url ON article(url)"))
        conn.execute(sqlalchemy.text("CREATE INDEX IF NOT EXISTS ix_article_source_scraped ON article(source, scraped_at)"))
        conn.execute(sqlalchemy.text("CREATE INDEX IF NOT EXISTS ix_anki_card_deck_id ON anki_card(deck_id)"))
        conn.execute(sqlalchemy.text("CREATE INDEX IF NOT EXISTS ix_news_insight_keyword_id ON news_insight(keyword_id)"))
        conn.commit()

    # Migrate: initialize position for existing InsightKeywords (position=0 means unset)
    unset_kws = InsightKeyword.query.filter_by(position=0).order_by(InsightKeyword.created_at.asc()).all()
    if len(unset_kws) > 1:
        for i, kw in enumerate(unset_kws):
            kw.position = i
        db.session.commit()

    # One-time: migrate aicompanies/robotics_companies → ai_robotics
    migrated = Article.query.filter(Article.source.in_(["aicompanies", "robotics_companies"])).count()
    if migrated:
        Article.query.filter(Article.source.in_(["aicompanies", "robotics_companies"])).update(
            {Article.source: "ai_robotics"}, synchronize_session="fetch"
        )
        db.session.commit()
        app.logger.info("Migrated %d articles to ai_robotics source", migrated)

    # One-time: fill missing date_read from year_published
    from models import MyBook
    books_no_date = MyBook.query.filter(
        MyBook.shelf == "read",
        (MyBook.date_read == None) | (MyBook.date_read == "")
    ).all()
    if books_no_date:
        for b in books_no_date:
            if b.year_published and b.year_published > 0:
                b.date_read = f"{b.year_published}/01/01"
        db.session.commit()
        app.logger.info(f"Backfilled date_read for {len(books_no_date)} books")

scheduler.start()

# --- Contact List Startup Tasks ---
def _run_contact_startup_tasks():
    """Run contact list startup tasks (sheet headers, auto-upgrade)."""
    try:
        from sheets import ensure_sheet_headers, get_all_contacts
        from sheets_entities import ensure_entity_sheet_headers, get_all_entities
        from scoring import auto_upgrade_followup, auto_upgrade_entity_followup
        ensure_sheet_headers()
        ensure_entity_sheet_headers()

        contacts = get_all_contacts()
        upgraded = auto_upgrade_followup(contacts)
        if upgraded:
            from sheets import update_contact
            for contact, old_fu, new_fu in upgraded:
                update_contact(
                    contact["name_hmac"],
                    {"follow_up_priority": new_fu},
                    changed_by="AI",
                )
            logger.info("Auto-upgraded %d contacts' follow-up priority", len(upgraded))

        entities = get_all_entities()
        upgraded_entities = auto_upgrade_entity_followup(entities)
        if upgraded_entities:
            from sheets_entities import update_entity
            for entity, old_fu, new_fu in upgraded_entities:
                update_entity(
                    entity["entity_hmac"],
                    {"follow_up_priority": new_fu},
                    changed_by="AI",
                )
            logger.info("Auto-upgraded %d entities' follow-up priority", len(upgraded_entities))
    except Exception as e:
        logger.warning("Contact startup tasks failed (sheets may not be configured): %s", e)

with app.app_context():
    _run_contact_startup_tasks()

if __name__ == "__main__":
    try:
        app.run(debug=True, use_reloader=False, port=5000)
    finally:
        scheduler.shutdown()
