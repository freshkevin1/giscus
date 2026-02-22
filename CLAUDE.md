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

## Architecture Principles

1. After every code change, validate the build succeeds.
2. Security Guardrails:
   - 2A. Secret Management: API keys, passwords — never hardcode (.env reference only)
   - 2B. Sandbox Execution: Tests in `tests/sandbox` only

## Behavioral Guidelines

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

### 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

### 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

### 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

### 5. Post-Implementation Validation (Required)

**Every change must survive a hostile review before being presented as complete.**

After implementing, switch your role to an Engineering Manager reviewing a PR from a junior developer. Perform ALL of the following checks:

#### 5A. Intent Alignment Check
- Re-read the original request. Compare it word-by-word against what you built.
- State explicitly: "The request was X. My implementation does Y. These align because Z."
- If there is ANY gap, flag it before presenting. Watch for scope drift.

#### 5B. Behavioral Verification
- Trace the primary user flow end-to-end mentally.
- Identify the top 3 most likely failure scenarios.
- API changes: verify request/response shapes, status codes, error paths.
- UI changes: verify empty state, normal state, error state.
- Data changes: verify edge cases (null, empty, malformed, boundary values).

#### 5C. Regression Check
- List every file you touched. For each: "What existing behavior depends on this?"
- If you changed a function signature, check all callers.
- If you changed a data structure, check all consumers.
- **Ordered schema change (column lists, header arrays):** New fields must be appended at the end. Inserting in the middle shifts column offsets and corrupts existing row reads without a data migration. Always ask: "What does an EXISTING row look like when read through the NEW schema?" If the answer differs from the old read → migration required, not just a header update.

#### 5D. Integration Boundary Check
- Check imports: are all new dependencies available in the project?
- Check types: do your changes maintain type compatibility at every boundary?
- Check side effects: does your change alter global state, env vars, or shared resources?

#### 5E. Validation Output (Required Format)

After every non-trivial change, append:

```
## Validation
- Intent: [one sentence - what was requested]
- Implementation: [one sentence - what was built]
- Alignment: [match / partial - explain gap]
- Build: [pass / fail]
- Tests: [pass / fail / not applicable - reason]
- Regression risk: [none / low / medium - explain]
- Edge cases checked: [list 2-3 specific cases]
- Confidence: [high / medium / low - if low, explain what's uncertain]
```

#### 5F. When to Escalate

STOP and surface the concern if:
- Confidence is "low" on any dimension
- You made an assumption that wasn't validated
- The change works but you don't fully understand WHY
- You had to work around something that felt wrong

**Escalation format:** "This works, but I want to flag: [concern]. My recommendation is [action]."
