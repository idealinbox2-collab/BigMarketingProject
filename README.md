# BigMarketingProject — Marketing Automation Platform

A multi-touch **RVM + SMS sequence engine** that turns a weekly lead list into a
paced, per-lead drip designed to generate inbound calls at a rate a small agent
team can actually work.

- **SMS** runs on the internal Twilio engine — number rotation, warmup, per-number
  daily caps, and opt-out / DNC handling.
- **RVM** (ringless voicemail) runs through the external **Drop.co** VMDrop API,
  which also doubles as line-type cleaning (wireless / landline / dead).

See **[docs/DESIGN.md](docs/DESIGN.md)** for the full design — the 5-run cycle,
timezone scheduling, manual pacing, line-type gating, slot-based resource pools,
suppression/ledger, the data model, and the build phases.

## Status

Core build complete (Phases 0–4), running in **dry-run** — nothing reaches Drop or
Twilio until the dry-run switches are flipped off.

- **Phase 0** — existing SMS platform imported as the foundation + Drop.co client.
- **Phase 1** — cohorts, leads (ledger), the materialized per-lead `touches` plan,
  editable resource pools, CSV upload/dedupe/slot-assignment, and a lead preview.
- **Phase 2** — RVM dispatch, the Drop status webhook, line-type gating, and the
  single suppression gate (SMS opt-out / IVR DNC / called-in upload / dead).
- **Phase 3** — the SMS pacer (rate, 1-or-2 texts, pause) through the Twilio engine.
- **Phase 4** — daily cleanup + completion, the cohort ledger + A/B, non-responder
  export, and the background scheduler (auto-run, gated, honors every switch).

Modules: `sequence.py` (engine/data), `rvm.py` (Drop dispatch), `pacer.py` (SMS),
`scheduler.py` (automation), `drop.py` (Drop API client).

**Next:** Phase 5 — hardening, an in-repo test suite, rebrand, and go-live prep.

## Layout

| Path | What |
|---|---|
| `app.py` | Flask web app: dashboard, JSON API, Twilio + Drop webhooks |
| `database.py` | SQLite data access + schema |
| `sender.py` | Concurrent SMS sending engine (rate limits, warmup, rotation) |
| `drop.py` | Drop.co (VMDrop) ringless-voicemail API client |
| `templates/dashboard.html` | Operator dashboard (being reworked for the sequence workflow) |
| `docs/DESIGN.md` | Design spec — source of truth |

## Running (local)

```bash
pip install -r requirements.txt
python app.py            # serves http://localhost:5000
```

Run as a **single process** in production (e.g. `gunicorn --workers 1 --threads 8
app:app`) — the sending engine keeps rate-limit state in-process, so multiple
workers would multiply the real send rate.

## Environment

| Var | Purpose |
|---|---|
| `DATABASE_URL` | Postgres connection string (`postgresql://…`). **If set, the app uses Postgres; otherwise SQLite.** |
| `DASHBOARD_PASSWORD` | Enables HTTP Basic auth on the dashboard (webhooks always exempt) |
| `DROP_API_KEY` | Drop.co Customer API key (server-side only) |
| `DROP_BASE_URL` | Override the Drop API base (default `https://customerapi.drop.co`) |
| `DROP_WEBHOOK_TOKEN` | Secret placed in Drop's webhook URL (`/webhook/drop?token=…`); when set, the app rejects Drop posts without it |
| `WEBHOOK_AUTH_ENFORCE` | `1` = reject invalid Twilio signatures / Drop tokens; `0` (default) = log-only so a misconfig can't drop callbacks |
| `APP_URL` | The app's own public URL (fallback for Twilio status callbacks + webhook-signature URL) |
| `DB_PATH` | Override the SQLite path (default `sms_dashboard.db`); ignored when `DATABASE_URL` is set |

Twilio credentials are stored per sub-account inside the app, not in env.

## Database

Runs on **SQLite by default** (zero-setup local/dev) and **Postgres in production**
via `DATABASE_URL` — the same code path, selected at startup. The full test suite
passes on both. Schema and migrations are created automatically on first run.

## Deployment (DigitalOcean App Platform)

Deploy-ready: `Procfile`, `.do/app.yaml` (App spec + managed Postgres), and a
`/health` probe are included.

**Deploy:**
1. `doctl apps create --spec .do/app.yaml` (or import the spec in the DO console).
   This provisions Managed Postgres and injects `DATABASE_URL` automatically, so
   the app runs on Postgres.
2. Set the real `DASHBOARD_PASSWORD` and `DROP_API_KEY` secrets on the app.
3. Keep **instance count = 1** — the scheduler and per-number rate limiters are
   in-process; a second instance would double-send.

**After the first deploy (required for full function):**
- In **Settings**, set **Base URL** to the app's public URL — Twilio only sends
  delivery callbacks (which drive dead-number cleaning) if this is set. (`APP_URL`
  covers it automatically when the DO binding resolves.)
- Point webhooks at the app:
  - Twilio inbound → `https://<app>/webhook/inbound`
  - Twilio status callback → `https://<app>/webhook/status`
  - Drop customer webhook → `https://<app>/webhook/drop`

**Go live (only when you mean it):** everything ships in **dry-run**. In
Sequences → Engine set the SMS rate, turn off RVM/SMS dry-run, and enable the
scheduler. Real sending also requires **10DLC registration** on your Twilio
numbers, a **Drop campaign token** + audio, and your **pool content**.

A Droplet + SQLite (on a persistent volume) also works but is higher-ops.
