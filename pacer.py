"""SMS pacer (Phase 3) — sends due sequence SMS through the existing Twilio engine.

Metered by operator dials and gated wireless-only:
  - sms_paused         : hard stop
  - sms_dry_run        : simulate (default ON) — marks touches sent with a
                         synthetic SID, touches no Twilio and no number counters
  - sms_rate_per_hour  : overall throughput dial (per-call drain ceiling)
  - sms_texts_per_day  : 1 or 2 — when 1, the day's second text is held

Reuses sender.py's per-number token buckets, daily-cap claims, warmup, rotation,
and OptOut handling. Last-second re-checks DNC + wireless before every send.
"""
import os
import logging
from datetime import datetime

import database as db
import sequence as sq
import sender

logger = logging.getLogger(__name__)


def is_dry_run():
    return str(db.get_setting('sms_dry_run', '1')).strip().lower() not in ('0', 'false', 'off', 'no')


def is_paused():
    return str(db.get_setting('sms_paused', '0')).strip().lower() in ('1', 'true', 'on', 'yes')


def texts_per_day():
    try:
        return 1 if int(db.get_setting('sms_texts_per_day', '2')) <= 1 else 2
    except (ValueError, TypeError):
        return 2


def rate_per_hour():
    try:
        return max(1, int(db.get_setting('sms_rate_per_hour', '12000')))
    except (ValueError, TypeError):
        return 12000


_DUE_SQL = (
    "SELECT t.id, t.lead_id, t.cohort_id, t.step_in_day, t.template_slot_id, "
    "t.agent_slot_id, t.callback_slot_id, "
    "l.phone, l.first_name, l.last_name, l.state, l.amount, l.custom_fields, l.line_type, "
    "co.brand AS brand "
    "FROM touches t JOIN leads l ON l.id = t.lead_id JOIN cohorts co ON co.id = t.cohort_id "
    "WHERE t.touch_type='sms' AND t.status IN ('planned','eligible') "
    "AND t.eligible_at <= ? AND l.status IN ('enrolled','in_progress') "
    "ORDER BY t.eligible_at LIMIT ?"
)


def dispatch_due_sms(limit=None):
    """Send due SMS touches, FIFO, up to `limit` (default = one hour's worth of the
    operator rate). Returns a summary dict."""
    if is_paused():
        return {'paused': True, 'sent': 0, 'due': 0}
    if not sq.within_send_window():
        return {'outside_window': True, 'sent': 0, 'due': 0}

    dry = is_dry_run()
    tpd = texts_per_day()
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    if limit is None:
        limit = rate_per_hour()

    conn = db.get_db()
    rows = conn.execute(_DUE_SQL, (now, limit * 3)).fetchall()
    conn.close()

    tmpl      = {t['id']: t for t in sq.get_templates()}
    agents    = {a['id']: a['name'] for a in sq.get_agents()}
    callbacks = {c['id']: c['number'] for c in sq.get_callbacks()}

    summary = {'due': len(rows), 'sent': 0, 'skipped_wireless': 0, 'skipped_dnc': 0,
               'skipped_tpd': 0, 'no_capacity': 0, 'errors': 0, 'dry_run': dry, 'paused': False}

    rate = sender._current_rate_mps()
    base = (db.get_setting('base_url', '') or os.environ.get('APP_URL', '') or '').strip().rstrip('/')
    status_cb = f"{base}/webhook/status" if base else None
    display_numbers = db.get_active_numbers() if dry else None
    numbers_cache = {}   # brand -> active numbers, fetched once per dispatch (not per touch)

    sent = 0
    for r in rows:
        if sent >= limit:
            break
        # Last-second gates (order matters: never send non-wireless / DNC)
        if db.is_dnc(r['phone']):
            _cancel(r['id'], 'dnc'); summary['skipped_dnc'] += 1; continue
        if r['line_type'] != 'wireless':
            summary['skipped_wireless'] += 1; continue          # unknown -> hold
        if r['step_in_day'] == 2 and tpd < 2:
            summary['skipped_tpd'] += 1; continue

        body = _render(r, tmpl, agents, callbacks)

        if dry:
            fn = _display_number(display_numbers, r)
            _mark_sms_sent(r['id'], fn, f"DRYRUN-SMS-{r['id']}", now, dry=True)
            sent += 1; summary['sent'] += 1
            continue

        # ── Live: claim a sending number (rate token + daily cap), then send ──
        brand = r['brand'] or None
        if brand not in numbers_cache:
            numbers_cache[brand] = db.get_active_numbers(brand=brand)
        numbers = numbers_cache[brand]
        if not numbers:
            summary['no_capacity'] += 1
            break
        numbers = sorted(numbers, key=lambda n: n.get('total_sent', 0))
        last_used = db.get_last_sending_number(r['phone'])
        ordered = ([n for n in numbers if n['phone_number'] != last_used] +
                   [n for n in numbers if n['phone_number'] == last_used])
        chosen = None
        for n in ordered:
            cap = sender.effective_daily_cap(n)
            if not sender.get_bucket(n['phone_number'], rate).try_consume():
                continue
            if db.claim_number_capacity(n['phone_number'], cap):
                chosen = n
                break
        if chosen is None:
            summary['no_capacity'] += 1
            continue

        fn = chosen['phone_number']
        try:
            sid = sender.send_with_retry(
                to_phone=r['phone'], from_number=fn, body=body, message_type='sms',
                media_url=None, account_sid=chosen['account_sid'],
                auth_token=chosen['auth_token'], status_callback=status_cb)
            _mark_sms_sent(r['id'], fn, sid, now, dry=False)
            db.update_number_history(r['phone'], fn)
            sent += 1; summary['sent'] += 1
        except sender.OptOutError:
            db.release_number_capacity(fn)
            sq.suppress_and_cancel(r['phone'], 'twilio_opt_out', 'opted_out_sms', source='twilio')
            summary['skipped_dnc'] += 1
        except Exception as e:
            db.release_number_capacity(fn)
            _mark_failed(r['id'], str(e))
            summary['errors'] += 1
            logger.warning("[SMS] send failed for lead %s: %s", r['lead_id'], e)

    logger.info("[SMS] pacer %s: %s", 'DRY-RUN' if dry else 'LIVE', summary)
    return summary


def _render(r, tmpl, agents, callbacks):
    body = tmpl.get(r['template_slot_id'], {}).get('body', '')
    lead = {'first_name': r['first_name'], 'last_name': r['last_name'],
            'amount': r['amount'], 'state': r['state'], 'custom_fields': r['custom_fields']}
    return sq.render_body(body, lead, agents.get(r['agent_slot_id'], ''), callbacks.get(r['callback_slot_id'], ''))


def _display_number(numbers, r):
    return numbers[r['id'] % len(numbers)]['phone_number'] if numbers else ''


def _mark_sms_sent(touch_id, from_number, sid, now, dry):
    conn = db.get_db()
    conn.execute("UPDATE touches SET status='sent', sending_number=?, sent_at=?, message_sid=? WHERE id=?",
                 (from_number, now, sid, touch_id))
    if not dry and from_number:
        conn.execute("UPDATE sending_numbers SET total_sent=total_sent+1 WHERE phone_number=?", (from_number,))
    conn.commit()
    conn.close()


def _cancel(touch_id, reason):
    conn = db.get_db()
    conn.execute("UPDATE touches SET status='cancelled', skipped_reason=? WHERE id=?", (reason, touch_id))
    conn.commit()
    conn.close()


def _mark_failed(touch_id, msg):
    conn = db.get_db()
    conn.execute("UPDATE touches SET status='failed', skipped_reason=? WHERE id=?", (msg[:200], touch_id))
    conn.commit()
    conn.close()
