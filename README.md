# NZ Booking System

A multi-tenant appointment-booking backend for small service businesses in
New Zealand (hairdressers, beauty therapists, clinics, trades). Built with
FastAPI + SQLite (local dev) or PostgreSQL (production, via Supabase).

## Quick start (local)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python main.py          # http://localhost:8000
```

Local dev uses SQLite automatically (no setup). A demo shop is seeded on
first run — open `/api/book/demo/shop` to see it working.

## Production (free deploy)

The app is backend-agnostic. Set these environment variables on your host
(Koyeb / Render / Docker):

| Variable                 | Purpose                                   |
|--------------------------|-------------------------------------------|
| `DATABASE_URL`           | Postgres connection string (`postgresql://...?sslmode=require`). If unset, falls back to SQLite. |
| `SECRET_KEY`             | Session/JWT signing secret (generate a long random string). |
| `SUPER_ADMIN_PASSWORD`   | Password for the super-admin login.       |
| `SHOP_EMAIL`             | From-address for customer confirmation emails. |
| `SHOP_TZ`                | Shop timezone, e.g. `Pacific/Auckland`.   |
| `PORT`                   | Port the host assigns (e.g. `8000`); read automatically. |

`init_db()` runs on startup (`lifespan`), so tables are created automatically
on first deploy — no manual migration step.

### One-click deploy

- **Koyeb:** import this repo → set the env vars above → Deploy.
- **Render:** use the included `render.yaml` → set `DATABASE_URL` and the other
  env vars → Deploy. Health check: `/api/book/demo/shop`.

## Architecture notes

- `database.py` — dual-mode DB layer. App code uses a small connection API
  (`conn.execute`, `cur.lastrowid`); on Postgres a thin adapter translates
  `?` placeholders to `%s` and appends `RETURNING id`.
- Double-booking is prevented at the database level with a **partial unique
  index** on `(shop_id, date_str, start_min)` where `status IN ('pending','confirmed')`.
- Auth tokens use constant-time comparison; `SECRET_KEY` is required at startup.

## Compliance (NZ)

- Privacy Act 2020: collect only what you need, secure it, and have a breach
  plan (breaches expected to be notified within 72h).
- GST: register once turnover reaches the $60k rolling threshold (15%).
- Payments: integrate Stripe NZ or Windcave; never store raw card numbers.

## Files

```
main.py          FastAPI app + API routes
database.py      dual-mode SQLite/Postgres layer + schema
auth_utils.py    token signing / verification
emailer.py       SMTP confirmation emails
requirements.txt pinned dependencies
Dockerfile       container build
render.yaml      Render one-click config
```
