import re
from app import app, db
from models import Article, ReadArticle

with app.app_context():
    geek_articles = Article.query.filter_by(source="geek_weekly") \
        .order_by(Article.section.desc(), Article.id.asc()).all()

    dl_articles = Article.query.filter_by(source="dl_batch").all()
    dl_articles.sort(
        key=lambda a: int(m.group(1)) if (m := re.search(r'/issue-(\d+)/', a.url)) else 0,
        reverse=True,
    )

    the_decoder_articles = Article.query.filter_by(source="the_decoder") \
        .order_by(Article.id.desc()).all()

    articles = the_decoder_articles + dl_articles + geek_articles
    bottom_100 = articles[-100:]

    count = 0
    for article in bottom_100:
        if not ReadArticle.query.filter_by(url=article.url).first():
            db.session.add(ReadArticle(url=article.url))
        db.session.delete(article)
        count += 1

    db.session.commit()
    print(f"Marked {count} articles as read.")
