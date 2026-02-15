from datetime import datetime, timezone

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Article(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(500), nullable=False)
    url = db.Column(db.String(1000), nullable=False)
    section = db.Column(db.String(100), default="")
    scraped_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<Article {self.title[:30]}>"


class ReadArticle(db.Model):
    """Tracks URLs of articles marked as read, so they are not re-imported."""
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String(1000), unique=True, nullable=False)
    read_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


def init_default_user():
    """Create default user if not exists."""
    if not User.query.filter_by(username="tornadogrowth").first():
        user = User(username="tornadogrowth")
        user.set_password("tornadogrowth128504")
        db.session.add(user)
        db.session.commit()
