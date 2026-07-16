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
| `DB_PATH` | Override the SQLite path (default `sms_dashboard.db`); ignored when `DATABASE_URL` is set |

Twilio credentials are stored per sub-account inside the app, not in env.

## Database

Runs on **SQLite by default** (zero-setup local/dev) and **Postgres in production**
via `DATABASE_URL` — the same code path, selected at startup. The full test suite
passes on both. Schema and migrations are created automatically on first run.

## Deployment (DigitalOcean)

- **App Platform + Managed Postgres** (recommended): create a managed Postgres DB,
  set `DATABASE_URL` from it, and set the other env vars above. Run as a **single
  instance / one process** (the sending engine and scheduler keep in-process state).
  Point Twilio (inbound + status) and Drop webhooks at the app's public URL.
- **Droplet + SQLite**: also works, but the SQLite file must live on a persistent
  volume, and you manage the VM yourself. Postgres is the lower-ops path.

Everything ships in **dry-run** — flip the RVM/SMS switches off dry-run (Sequences →
Engine) only when you intend to send.
