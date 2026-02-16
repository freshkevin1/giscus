import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

# Load .env file if it exists (local development)
_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)
from flask import Flask, flash, jsonify, redirect, render_template, request, url_for
from flask_login import LoginManager, current_user, login_required, login_user, logout_user

from config import Config
from models import Article, ReadArticle, User, db, init_default_user
from scraper import scrape_ai_companies, scrape_amazon_charts, scrape_irobotnews, scrape_mk_today, scrape_robotreport, scrape_yes24_bestseller

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = "로그인이 필요합니다."


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# --- Scheduler ---

def scheduled_scrape():
    """Run scraping job within app context."""
    with app.app_context():
        run_scrape("mk")
        run_scrape("irobot")
        run_scrape("robotreport")
        run_scrape("aicompanies")
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

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("daily_news"))

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            login_user(user)
            next_page = request.args.get("next")
            return redirect(next_page or url_for("daily_news"))

        flash("아이디 또는 비밀번호가 올바르지 않습니다.", "danger")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# --- Menu Routes ---

@app.route("/")
@login_required
def index():
    return redirect(url_for("daily_news"))


@app.route("/contacts")
@login_required
def contact_list():
    return render_template("contact_list.html")


@app.route("/news")
@login_required
def daily_news():
    return render_template("daily_news.html")


@app.route("/news/mk")
@login_required
def mk_news():
    articles = Article.query.filter_by(source="mk").order_by(Article.scraped_at.desc()).all()
    return render_template("mk_news.html", articles=articles)


@app.route("/news/irobot")
@login_required
def irobot_news():
    articles = Article.query.filter_by(source="irobot").order_by(Article.scraped_at.desc()).all()
    return render_template("irobot_news.html", articles=articles)


@app.route("/news/robotreport")
@login_required
def robotreport_news():
    articles = Article.query.filter_by(source="robotreport").order_by(Article.scraped_at.desc()).all()
    return render_template("robotreport_news.html", articles=articles)


@app.route("/news/ai")
@login_required
def ai_news():
    return render_template("ai_news.html")


@app.route("/news/ai/companies")
@login_required
def ai_companies_news():
    articles = Article.query.filter_by(source="aicompanies").order_by(Article.scraped_at.desc()).all()
    return render_template("ai_companies_news.html", articles=articles)


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


# --- API Routes ---

@app.route("/api/scrape/<source>", methods=["POST"])
@login_required
def api_scrape(source):
    if source not in ("mk", "irobot", "robotreport", "aicompanies", "bestseller", "bestseller_kr"):
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


@app.route("/api/admin/clear-read/<keyword>", methods=["POST"])
@login_required
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
    init_default_user()

scheduler.start()

if __name__ == "__main__":
    try:
        app.run(debug=True, use_reloader=False, port=5000)
    finally:
        scheduler.shutdown()
