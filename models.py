import os
from datetime import datetime, timezone

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    password_changed_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Article(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(500), nullable=False)
    url = db.Column(db.String(1000), nullable=False)
    source = db.Column(db.String(50), default="mk")
    section = db.Column(db.String(100), default="")
    image_url = db.Column(db.String(1000), default="")
    scraped_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<Article {self.title[:30]}>"


class MyBook(db.Model):
    """Tracks books the user has read or wants to read, with ratings."""
    id = db.Column(db.Integer, primary_key=True)
    goodreads_id = db.Column(db.String(20), unique=True, nullable=True)
    title = db.Column(db.String(500), nullable=False)
    author = db.Column(db.String(300), nullable=False)
    isbn = db.Column(db.String(20), default="")
    isbn13 = db.Column(db.String(20), default="")
    my_rating = db.Column(db.Integer, default=0)  # 0-5
    average_rating = db.Column(db.Float, default=0.0)
    publisher = db.Column(db.String(200), default="")
    year_published = db.Column(db.Integer, default=0)
    date_read = db.Column(db.String(20), default="")
    shelf = db.Column(db.String(20), default="read")
    hall_of_fame = db.Column(db.Boolean, default=False)
    added_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<MyBook {self.title[:30]}>"


class Recommendation(db.Model):
    """Cached book recommendations from Claude API."""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(500), nullable=False)
    author = db.Column(db.String(300), nullable=False)
    reason = db.Column(db.Text, default="")
    category = db.Column(db.String(100), default="")
    generated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class SavedBook(db.Model):
    """찜한 책 — AI 추천에서 저장한 책."""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(500), nullable=False)
    author = db.Column(db.String(300), nullable=False)
    reason = db.Column(db.Text, default="")
    category = db.Column(db.String(100), default="")
    saved_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class ChatMessage(db.Model):
    """Persisted chat messages for the book recommendation conversation."""
    id = db.Column(db.Integer, primary_key=True)
    role = db.Column(db.String(20), nullable=False)  # "user" or "assistant"
    content = db.Column(db.Text, nullable=False)
    recommendations_json = db.Column(db.Text, default="")  # 추천 카드 재렌더링용
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class LoginLog(db.Model):
    """Tracks login attempts with IP, user agent, and success/failure."""
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), nullable=False)
    ip_address = db.Column(db.String(45), nullable=False)  # IPv6 support
    user_agent = db.Column(db.String(500), default="")
    success = db.Column(db.Boolean, nullable=False)
    failure_reason = db.Column(db.String(100), default="")  # "invalid_password", "unknown_user"
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class ReadArticle(db.Model):
    """Tracks URLs of articles marked as read, so they are not re-imported."""
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String(1000), unique=True, nullable=False)
    read_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class ContactChatMessage(db.Model):
    """Chat messages for the contact AI assistant."""
    id = db.Column(db.Integer, primary_key=True)
    role = db.Column(db.String(20), nullable=False)  # "user" or "assistant"
    content = db.Column(db.Text, nullable=False)
    actions_json = db.Column(db.Text, default="")  # Parsed [ACTION] data for re-rendering
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class HabitLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    habit_name = db.Column(db.String(200), nullable=False)
    logged_date = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('habit_name', 'logged_date', name='uq_habit_date'),
    )


def init_default_user():
    """Create default user if not exists. Reads credentials from environment variables."""
    username = os.environ.get("DASHBOARD_USER")
    password = os.environ.get("DASHBOARD_PASS")
    if not username or not password:
        return
    if not User.query.filter_by(username=username).first():
        user = User(username=username, password_changed_at=datetime.utcnow())
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
