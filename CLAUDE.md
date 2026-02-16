# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Executive dashboard web app (Korean/English) that scrapes and displays news articles and bestseller book lists. Built with Flask, SQLAlchemy (SQLite), and BeautifulSoup. Deployed via Gunicorn on Railway (CLI deploy).

## Commands

- **Run locally:** `python app.py` (starts on port 5000 with debug mode, no reloader)
- **Run production:** `gunicorn app:app`
- **Install dependencies:** `pip install -r requirements.txt`

No test suite or linter is configured.

## Deployment

- **Platform:** Railway (CLI deploy via `railway up`, GitHub auto-deploy is NOT connected)
- **Production URL:** `executive-dashboard-production.up.railway.app`
- **Deploy command:** `railway up --detach` (uploads current directory and builds on Railway)
- **Logs:** `railway logs -n 20` to check deployment status and runtime errors

### Commit & Deploy Policy

When the user asks to deploy (배포), follow this sequence without asking for confirmation at each step:

1. `git add` the changed files (specific files, not `-A`)
2. `git commit` with a descriptive message
3. `git push origin main`
4. `railway up --detach` — triggers the actual Railway build & deploy
5. **Validate deployment** (see below)

All commands (`git add`, `git commit`, `git push`, `railway up`) are pre-authorized in `.claude/settings.local.json`.

### Deployment Validation

After every `railway up`, verify the deployment succeeded:

1. Wait ~30 seconds for the build to complete
2. Run `railway deployment list` — confirm the latest deployment shows `SUCCESS`
3. Run `railway logs -n 10` — confirm Gunicorn started without errors (look for `Listening at:`)
4. If the deployment shows `FAILED` or logs contain errors, report the issue to the user immediately

## Architecture

**Four Python files make up the entire backend:**

- `app.py` — Flask app, routes, scheduler setup, and DB initialization. All routes require login (`@login_required`). Contains both page routes and JSON API routes (`/api/...`).
- `scraper.py` — All web scraping functions. Each source has its own `scrape_*()` function returning `list[dict]` with keys `title`, `url`, `section` (news) or `rank`, `title`, `author`, `url`, `image_url` (bestsellers).
- `models.py` — SQLAlchemy models: `User`, `Article`, `ReadArticle`. `ReadArticle` tracks URLs that have been dismissed so they aren't re-imported.
- `config.py` — `Config` class with env-based settings (`SECRET_KEY`, `DATABASE_URL`, scrape schedule, `MAX_ARTICLES`).

**Data sources** (the `source` field on `Article`):
| source key | scraper function | content |
|---|---|---|
| `mk` | `scrape_mk_today()` | 매일경제 newspaper |
| `irobot` | `scrape_irobotnews()` | 로봇신문 |
| `robotreport` | `scrape_robotreport()` | The Robot Report |
| `aicompanies` | `scrape_ai_companies()` | Anthropic, DeepMind, Meta AI, OpenAI |
| `bestseller` | `scrape_amazon_charts()` | Amazon Charts Most Read Nonfiction |
| `bestseller_kr` | `scrape_yes24_bestseller()` | YES24 monthly bestseller |

**Key behavior differences:** Bestseller sources (`bestseller`, `bestseller_kr`) do a full replace on each scrape (delete all existing, insert fresh). News sources deduplicate against both existing `Article` rows and `ReadArticle` history, and enforce `MAX_ARTICLES` per source.

**Scheduling:** APScheduler runs `scheduled_scrape()` daily at 21:00 UTC (06:00 KST).

**Templates:** Jinja2 templates in `templates/` extend `base.html`. Each news source and bestseller list has its own template.

**Environment variables:**
- `SECRET_KEY`, `DATABASE_URL` — standard Flask/SQLAlchemy config
- `DASHBOARD_USER`, `DASHBOARD_PASS` — auto-creates a default login user on startup

## Security

**Current protections:**
- `.env`, `instance/` (SQLite DB) are in `.gitignore`
- Passwords hashed via `werkzeug.security.generate_password_hash`
- SQLAlchemy ORM prevents SQL injection
- All routes gated by `@login_required`

**Known gaps:**
- `config.py` has a hardcoded `SECRET_KEY` fallback — production must set the env var, otherwise sessions are predictable
- No CSRF protection on POST routes (Flask-WTF not installed)
- No rate limiting on `/login` or API endpoints
- `/api/admin/clear-read/<keyword>` has no admin role check — any logged-in user can call it

**Guidelines for future changes:**
- New POST/PUT/DELETE routes must use `@login_required`; admin-only routes should add role verification when a role system is introduced
- Never commit `.env` or `instance/` — verify `.gitignore` covers new secret files
- Scraper functions receive external HTML — avoid passing raw scraped content into `db.session.execute()` or f-string SQL; always use the ORM
- If adding user-generated content to templates, ensure Jinja2 autoescaping is on (default) and do not use `| safe` on untrusted data

## Adding a New Scraping Source

1. Add a `scrape_*()` function in `scraper.py` returning `list[dict]` with the standard keys
2. Import it in `app.py` and add a branch in `run_scrape()`
3. Add the source key to the allowed list in `api_scrape()`
4. Add it to `scheduled_scrape()` if it should run daily
5. Create a template and route for the new source page
