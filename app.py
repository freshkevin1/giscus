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
from scraper import scrape_mk_today

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
        run_scrape()


def run_scrape():
    """Scrape articles and save to DB, enforcing MAX_ARTICLES limit."""
    articles = scrape_mk_today()
    if not articles:
        logger.warning("No articles scraped")
        return 0

    count = 0
    for a in articles:
        # Skip already-read articles
        if ReadArticle.query.filter_by(url=a["url"]).first():
            continue
        exists = Article.query.filter_by(url=a["url"]).first()
        if not exists:
            article = Article(
                title=a["title"],
                url=a["url"],
                section=a["section"],
            )
            db.session.add(article)
            count += 1

    db.session.commit()
    logger.info("Added %d new articles", count)

    # Enforce article limit
    total = Article.query.count()
    if total > Config.MAX_ARTICLES:
        excess = total - Config.MAX_ARTICLES
        old_articles = Article.query.order_by(Article.scraped_at.asc()).limit(excess).all()
        for old in old_articles:
            db.session.delete(old)
        db.session.commit()
        logger.info("Removed %d old articles (limit: %d)", excess, Config.MAX_ARTICLES)

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
    articles = Article.query.order_by(Article.scraped_at.desc()).all()
    return render_template("mk_news.html", articles=articles)


@app.route("/events")
@login_required
def daily_event():
    return render_template("daily_event.html")


@app.route("/bestsellers")
@login_required
def bestsellers():
    return render_template("bestsellers.html")


# --- API Routes ---

@app.route("/api/scrape", methods=["POST"])
@login_required
def api_scrape():
    count = run_scrape()
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


# --- Init & Run ---

with app.app_context():
    db.create_all()
    init_default_user()

scheduler.start()

if __name__ == "__main__":
    try:
        app.run(debug=True, use_reloader=False, port=5000)
    finally:
        scheduler.shutdown()
