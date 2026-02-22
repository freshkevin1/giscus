import logging
import os
import re
import threading
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
from flask import Flask, flash, jsonify, redirect, render_template, request, send_from_directory, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from flask_wtf.csrf import CSRFProtect, generate_csrf

from config import Config
import json

from models import Article, ChatMessage, ContactChatMessage, LoginLog, MyBook, ReadArticle, Recommendation, SavedBook, User, db, init_default_user
from recommender import chat_recommendation, generate_recommendations
import requests as http_requests
from scraper import scrape_ai_companies, scrape_amazon_charts, scrape_deeplearning_batch, scrape_geek_news_weekly, scrape_irobotnews, scrape_mk_today, scrape_robotics_companies, scrape_robotreport, scrape_yes24_bestseller

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config.from_object(Config)
csrf = CSRFProtect(app)

db.init_app(app)


def _client_ip_for_rate_limit():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
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


PASSWORD_EXPIRY_DAYS = 30
ADMIN_USERNAME = "tornadogrowth"

HABITS = ["아침 조깅/테니스/골프 + 스트레칭/명상"]
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


def _habit_stats(habit_name):
    from sheets import _get_all_habit_rows, is_habit_logged
    today = date.today()
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
    return {"name": habit_name, "today_done": today_done, "streak": streak, "total": total, "days": days}


@app.before_request
def enforce_password_change():
    if not current_user.is_authenticated:
        return
    if request.endpoint in ("change_password", "logout", "static"):
        return

    changed_at = current_user.password_changed_at
    if changed_at is None or (datetime.utcnow() - changed_at).days >= PASSWORD_EXPIRY_DAYS:
        flash("보안을 위해 비밀번호를 변경해 주세요 (30일 경과).", "warning")
        return redirect(url_for("change_password"))


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


def auto_scrape(source):
    """Trigger scraping in a background thread (non-blocking)."""
    thread = threading.Thread(target=_scrape_background, args=(source,), daemon=True)
    thread.start()


# --- Scheduler ---

def scheduled_scrape():
    """Run scraping job within app context."""
    with app.app_context():
        run_scrape("mk")
        run_scrape("irobot")
        run_scrape("robotreport")
        run_scrape("aicompanies")
        run_scrape("robotics_companies")
        run_scrape("geek_weekly")
        run_scrape("dl_batch")
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
    elif source == "aicompanies":
        articles = scrape_ai_companies()
    elif source == "robotics_companies":
        articles = scrape_robotics_companies()
    elif source == "geek_weekly":
        articles = scrape_geek_news_weekly()
    elif source == "dl_batch":
        articles = scrape_deeplearning_batch()
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

    count = 0
    for a in articles:
        if ReadArticle.query.filter_by(url=a["url"]).first():
            continue
        if Article.query.filter_by(url=a["url"]).first():
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

    # Enforce per-source article limit
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


scheduler = BackgroundScheduler()
scheduler.add_job(
    scheduled_scrape,
    "cron",
    hour=Config.SCRAPE_HOUR_UTC,
    minute=Config.SCRAPE_MINUTE,
    id="daily_scrape",
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
            login_user(user)
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
    fresh = request.args.get("fresh") == "1"
    if fresh:
        from sheets import invalidate_contacts_cache
        invalidate_contacts_cache()

    try:
        from sheets import get_all_contacts
        from scoring import sort_contacts_by_score
        contacts = sort_contacts_by_score(get_all_contacts())
    except Exception:
        contacts = []

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

    total_contacts = len(contacts)
    fu0_count = sum(1 for c in contacts if c.get("follow_up_priority") == "FU0")
    overdue_count = sum(1 for c in contacts if c.get("follow_up_date", "") and c.get("follow_up_date", "") < today_str)

    eligible = [
        c for c in contacts
        if c.get("follow_up_date") and c.get("follow_up_priority") != "FU9"
    ]
    overdue = [c for c in eligible if c.get("follow_up_date", "") < today_str]
    not_overdue = [c for c in eligible if c.get("follow_up_date", "") >= today_str]
    # overdue는 무조건 포함, 나머지 슬롯은 스코어 순 non-overdue로 채움
    for c in overdue:
        try:
            delta = date.today() - date.fromisoformat(c["follow_up_date"])
            c["days_overdue"] = delta.days
        except Exception:
            c["days_overdue"] = 0
    # 각 그룹 내: 1차 score 내림차순, 2차 follow_up_date 오름차순
    _sort_key = lambda c: (-c.get("score", 0), c.get("follow_up_date", "9999-99-99"))
    top5 = sorted(overdue, key=_sort_key) + sorted(not_overdue, key=_sort_key)[:max(0, 5 - len(overdue))]

    incoming = [
        c for c in contacts
        if "입사 후보자" in (c.get("key_value_interest") or "")
        or "입사 후보자" in (c.get("tag") or "")
    ]

    reading_books = MyBook.query.filter_by(shelf="reading").order_by(MyBook.added_at.desc()).all()
    habits_data = [_habit_stats(h) for h in HABITS]

    return render_template(
        "landing.html",
        top5=top5,
        incoming=incoming,
        reading_books=reading_books,
        weekly_stats=weekly_stats,
        weekly_total=weekly_total,
        max_daily=max_daily,
        total_contacts=total_contacts,
        fu0_count=fu0_count,
        overdue_count=overdue_count,
        habits_data=habits_data,
        today_str=date.today().strftime("%Y년 %m월 %d일"),
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
    source_keys = ["mk", "irobot", "robotreport", "aicompanies", "robotics_companies", "geek_weekly"]
    hub = {}
    for key in source_keys:
        count = Article.query.filter_by(source=key).count()
        latest = Article.query.filter_by(source=key).order_by(Article.scraped_at.desc()).first()
        hub[key] = {
            "count": count,
            "latest_title": latest.title if latest else None,
            "latest_url": latest.url if latest else None,
        }
    return render_template("daily_news.html", hub=hub)


@app.route("/news/mk")
@login_required
def mk_news():
    auto_scrape("mk")
    articles = Article.query.filter_by(source="mk").order_by(Article.scraped_at.desc()).all()
    return render_template("mk_news.html", articles=articles)


@app.route("/news/irobot")
@login_required
def irobot_news():
    auto_scrape("irobot")
    articles = Article.query.filter_by(source="irobot").order_by(Article.scraped_at.desc()).all()
    return render_template("irobot_news.html", articles=articles)


@app.route("/news/robotreport")
@login_required
def robotreport_news():
    auto_scrape("robotreport")
    articles = Article.query.filter_by(source="robotreport").order_by(Article.scraped_at.desc()).all()
    return render_template("robotreport_news.html", articles=articles)


@app.route("/news/ai")
@login_required
def ai_news():
    return redirect(url_for("daily_news"))


@app.route("/news/ai/companies")
@login_required
def ai_companies_news():
    auto_scrape("aicompanies")
    articles = Article.query.filter_by(source="aicompanies").order_by(Article.scraped_at.desc()).all()
    return render_template("ai_companies_news.html", articles=articles)


@app.route("/news/robotics/companies")
@login_required
def robotics_companies_news():
    auto_scrape("robotics_companies")
    articles = Article.query.filter_by(source="robotics_companies").order_by(Article.scraped_at.desc()).all()
    return render_template("robotics_companies_news.html", articles=articles)


@app.route("/news/trends")
@login_required
def trends_news():
    # geek_weekly
    if Article.query.filter_by(source="geek_weekly").count() == 0:
        run_scrape("geek_weekly")
    else:
        auto_scrape("geek_weekly")
    # dl_batch
    if Article.query.filter_by(source="dl_batch").count() == 0:
        run_scrape("dl_batch")
    else:
        auto_scrape("dl_batch")

    geek_articles = Article.query.filter_by(source="geek_weekly").order_by(Article.section.desc(), Article.id.asc()).all()

    dl_articles = Article.query.filter_by(source="dl_batch").all()
    dl_articles.sort(
        key=lambda a: int(m.group(1)) if (m := re.search(r'/issue-(\d+)/', a.url)) else 0,
        reverse=True,
    )

    articles = dl_articles + geek_articles
    return render_template("trends_news.html", articles=articles)


@app.route("/news/deeplearning")
@login_required
def deeplearning_news():
    if Article.query.filter_by(source="dl_batch").count() == 0:
        run_scrape("dl_batch")
    else:
        auto_scrape("dl_batch")
    articles = Article.query.filter_by(source="dl_batch").all()
    articles.sort(
        key=lambda a: int(m.group(1)) if (m := re.search(r'/issue-(\d+)/', a.url)) else 0,
        reverse=True,
    )
    return render_template("deeplearning_news.html", articles=articles)


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


# --- API Routes ---

@app.route("/api/scrape/<source>", methods=["POST"])
@login_required
def api_scrape(source):
    if source not in ("mk", "irobot", "robotreport", "aicompanies", "robotics_companies", "geek_weekly", "dl_batch", "bestseller", "bestseller_kr"):
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
        return jsonify({"status": "ok"})
    return jsonify({"status": "not_found"}), 404


@app.route("/api/articles/read-all/<source>", methods=["POST"])
@login_required
def mark_all_read(source):
    articles = Article.query.filter_by(source=source).all()
    count = 0
    for article in articles:
        if not ReadArticle.query.filter_by(url=article.url).first():
            db.session.add(ReadArticle(url=article.url))
        db.session.delete(article)
        count += 1
    db.session.commit()
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
        .order_by(ChatMessage.created_at.asc())
        .all()
    )
    history = [{"role": m.role, "content": m.content} for m in db_messages[-50:]]

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
    messages = ChatMessage.query.order_by(ChatMessage.created_at.asc()).all()
    result = []
    for m in messages:
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

        # Get conversation history from DB
        history_msgs = ContactChatMessage.query.order_by(
            ContactChatMessage.created_at.asc()
        ).all()
        conversation_history = [
            {"role": m.role, "content": m.content} for m in history_msgs
        ]

        # Call AI agent
        result = chat_contact(user_message, conversation_history)

        # Process actions
        executed_actions = []
        pending_actions = []

        for action in result.get("actions", []):
            action_type = action.get("action", "")
            entity_type = action.get("entity_type", "contact")
            confidence = action.get("confidence", "low")
            name = action.get("name", "")

            # --- Business Entity & Opportunity actions ---
            if entity_type == "business_entity":
                # search_entity는 읽기 전용 — confidence 무관하게 항상 실행
                if action_type == "search_entity":
                    from sheets_entities import find_entity_by_name
                    matches = find_entity_by_name(name) if name else []
                    executed_actions.append({
                        "type": "search_entity",
                        "name": name,
                        "results": matches,
                    })
                    continue

                # add_entity는 항상 사용자 확인 필요 (중복 방지)
                if action_type == "add_entity":
                    pending_actions.append({
                        **action,
                        "confidence": "low",
                        "reason": "새 비즈니스 엔티티 추가 — 확인 후 실행",
                    })
                    continue

                if confidence != "high":
                    pending_actions.append(action)
                    continue

                # Entity CRUD — add_entity는 위에서 처리됨
                if action_type == "update_entity":
                    from sheets_entities import find_entity_by_name, update_entity, add_entity_log
                    matches = find_entity_by_name(name)
                    if len(matches) == 1:
                        entity = matches[0]
                        fields = action.get("fields", {})
                        interaction_log = action.get("interaction_log", "")
                        key_extract = action.get("key_value_extract", "")
                        if interaction_log:
                            add_entity_log(entity["entity_hmac"], entity["name"],
                                           interaction_log, key_extract, ", ".join(fields.keys()))
                        if fields:
                            update_entity(entity["entity_hmac"], fields, changed_by="AI")
                        executed_actions.append({
                            "type": "update_entity",
                            "name": entity["name"],
                            "fields": fields,
                        })
                    else:
                        pending_actions.append({**action, "confidence": "low",
                                                "reason": "엔티티를 찾을 수 없음"})

                elif action_type == "delete_entity":
                    from sheets_entities import find_entity_by_name, delete_entity
                    matches = find_entity_by_name(name)
                    if len(matches) == 1:
                        delete_entity(matches[0]["entity_hmac"], deleted_by="AI")
                        executed_actions.append({
                            "type": "delete_entity",
                            "name": name,
                        })
                    else:
                        pending_actions.append({**action, "confidence": "low",
                                                "reason": "엔티티를 찾을 수 없음"})

                # Opportunity CRUD
                elif action_type == "add_opp_to_entity":
                    from sheets_entities import find_entity_by_name, add_opportunity
                    matches = find_entity_by_name(name)
                    if len(matches) == 1:
                        entity = matches[0]
                        opp_title = action.get("opp_title", "")
                        opp_details = action.get("opp_details", "")
                        if opp_title:
                            opp_id, created = add_opportunity(entity["entity_hmac"], opp_title, opp_details)
                            executed_actions.append({
                                "type": "add_opp_to_entity",
                                "name": entity["name"],
                                "opp_id": opp_id,
                                "opp_title": opp_title,
                                "duplicate": not created,
                            })
                        else:
                            pending_actions.append({**action, "confidence": "low",
                                                    "reason": "기회 제목(opp_title) 누락"})
                    else:
                        pending_actions.append({**action, "confidence": "low",
                                                "reason": "엔티티를 찾을 수 없음"})

                elif action_type == "update_opp":
                    from sheets_entities import find_entity_by_name, update_opportunity
                    matches = find_entity_by_name(name)
                    opp_id = action.get("opp_id", "")
                    if len(matches) == 1 and opp_id:
                        entity = matches[0]
                        opp_title = action.get("opp_title") or None
                        opp_details = action.get("opp_details") or None
                        update_opportunity(entity["entity_hmac"], opp_id,
                                           title=opp_title, details=opp_details)
                        executed_actions.append({
                            "type": "update_opp",
                            "name": entity["name"],
                            "opp_id": opp_id,
                        })
                    else:
                        pending_actions.append({**action, "confidence": "low",
                                                "reason": "엔티티 또는 opp_id를 찾을 수 없음"})

                elif action_type == "delete_opp":
                    from sheets_entities import find_entity_by_name, delete_opportunity
                    matches = find_entity_by_name(name)
                    opp_id = action.get("opp_id", "")
                    if len(matches) == 1 and opp_id:
                        entity = matches[0]
                        delete_opportunity(entity["entity_hmac"], opp_id)
                        executed_actions.append({
                            "type": "delete_opp",
                            "name": entity["name"],
                            "opp_id": opp_id,
                        })
                    else:
                        pending_actions.append({**action, "confidence": "low",
                                                "reason": "엔티티 또는 opp_id를 찾을 수 없음"})

                continue  # business_entity 처리 완료 → contact 블록 skip

            # --- Contact actions ---
            if action_type == "search":
                matches = find_contact_by_name(name) if name else []
                executed_actions.append({
                    "type": "search",
                    "name": name,
                    "results": matches,
                })
                continue

            if confidence != "high":
                pending_actions.append(action)
                continue

            if action_type == "update_contact":
                matches = find_contact_by_name(name)
                if len(matches) == 1:
                    contact = matches[0]
                    fields = action.get("fields", {})
                    interaction_log = action.get("interaction_log", "")
                    key_extract = action.get("key_value_extract", "")

                    # 1) Interaction Log는 validation과 무관하게 항상 먼저 기록
                    if interaction_log:
                        display = f"{contact['name']}({contact['employer']})" if contact.get("employer") else contact["name"]
                        updated_fields_str = ", ".join(fields.keys()) if fields else ""
                        add_interaction_log(
                            contact["name_hmac"], display, interaction_log,
                            key_extract, updated_fields_str,
                        )

                    # 2) fields가 있으면 validation 후 업데이트
                    if fields:
                        # YYYY-MM → YYYY-MM-01 정규화 (일(day) 불명확할 때 1일로)
                        for _df in ('last_contact', 'follow_up_date'):
                            if _df in fields and re.match(r'^\d{4}-\d{2}$', str(fields[_df])):
                                fields[_df] = fields[_df] + '-01'
                        valid_tags = get_valid_tags()
                        is_valid, errors = validate_update_fields(fields, valid_tags)
                        if not is_valid:
                            # validation 실패는 fields 업데이트만 skip (로그는 이미 기록됨)
                            executed_actions.append({
                                "type": "update_skipped",
                                "name": contact["name"],
                                "reason": errors,
                            })
                        else:
                            success, any_changes = update_contact(contact["name_hmac"], fields, changed_by="AI")

                            # key_value_interest 병합 (fields에 없는 경우만)
                            if key_extract and "key_value_interest" not in fields:
                                existing = contact.get("key_value_interest", "")
                                merged = f"{existing}, {key_extract}" if existing else key_extract
                                update_contact(contact["name_hmac"],
                                               {"key_value_interest": merged}, changed_by="AI")

                            executed_actions.append({
                                "type": "update",
                                "name": contact["name"],
                                "fields": fields,
                                "no_changes": not any_changes,
                            })
                    elif key_extract:
                        # fields 없이 key_extract만 있는 경우 관심사 병합
                        existing = contact.get("key_value_interest", "")
                        merged = f"{existing}, {key_extract}" if existing else key_extract
                        update_contact(contact["name_hmac"],
                                       {"key_value_interest": merged}, changed_by="AI")
                elif len(matches) > 1:
                    pending_actions.append({
                        **action,
                        "confidence": "low",
                        "reason": "동명이인 발견",
                        "candidates": [
                            {"name": m["name"], "employer": m.get("employer", ""), "name_hmac": m["name_hmac"]}
                            for m in matches
                        ],
                    })
                else:
                    pending_actions.append({
                        **action,
                        "confidence": "low",
                        "reason": "연락처를 찾을 수 없음",
                    })

            elif action_type == "add_contact":
                # 새 연락처 추가는 항상 사용자 확인 필요 (중복 방지)
                pending_actions.append({
                    **action,
                    "confidence": "low",
                    "reason": "새 연락처 추가 — 확인 후 실행",
                })

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

        return jsonify({"error": "Unknown action type"}), 400

    except Exception as e:
        logger.error("Contact chat confirm error: %s", e, exc_info=True)
        return jsonify({"error": "처리 중 오류가 발생했습니다."}), 500


@app.route("/api/chat/history", methods=["GET"])
@login_required
def api_contact_chat_history():
    """Get contact chat history."""
    messages = ContactChatMessage.query.order_by(
        ContactChatMessage.created_at.asc()
    ).all()
    return jsonify({
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                "actions_json": m.actions_json,
                "created_at": m.created_at.isoformat() if m.created_at else "",
            }
            for m in messages
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
    )


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
    # Migrate: create login_log table if missing
    from sqlalchemy import inspect as sa_inspect
    inspector = sa_inspect(db.engine)
    if "login_log" not in inspector.get_table_names():
        LoginLog.__table__.create(db.engine)
    init_default_user()

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
