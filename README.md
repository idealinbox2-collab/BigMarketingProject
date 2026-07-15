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

Early build.

- **Phase 0 (done):** the existing SMS platform imported as the foundation, plus a
  Drop.co API client (`drop.py`) and a `/webhook/drop` status skeleton. No sequence
  logic yet; nothing new sends.
- **Next:** Phase 1 — cohort upload, dedupe, line-type scrub, slot assignment, the
  materialized per-lead plan, and a lead-preview screen.

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
| `DASHBOARD_PASSWORD` | Enables HTTP Basic auth on the dashboard (webhooks always exempt) |
| `DROP_API_KEY` | Drop.co Customer API key (server-side only) |
| `DROP_BASE_URL` | Override the Drop API base (default `https://customerapi.drop.co`) |
| `DB_PATH` | Override the SQLite path (default `sms_dashboard.db`) |

Twilio credentials are stored per sub-account inside the app, not in env.
