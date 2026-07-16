"""RVM dispatch + Drop.co status handling (Phase 2).

Dispatches due RVM touches to Drop and applies Drop's status webhooks back onto
the sequence: line-type gating, SMS time-stamping, and suppression.

Safety: honors a dry-run switch (``rvm_dry_run`` setting, default ON). In dry-run
nothing is posted to Drop — touches are marked sent with a synthetic activity
token so the rest of the pipeline (SMS time-stamping, webhook gating) stays fully
testable. Flip ``rvm_dry_run`` to 0 only when you intend to leave real voicemails.
"""
import logging
from datetime import datetime

import database as db
import sequence as sq
import drop

logger = logging.getLogger(__name__)


def is_dry_run():
    return str(db.get_setting('rvm_dry_run', '1')).strip().lower() not in ('0', 'false', 'off', 'no')


def campaign_token():
    return (db.get_setting('drop_campaign_token', '') or '').strip()


# Drop DropStatusCode -> classification. PLACEHOLDER: Drop's docs don't enumerate
# these codes, so fill this map with the real ones from your Drop dashboard/webhook.
# Until then we fall back to keyword-matching DropStatusMessage.
#   e.g. DROP_STATUS_MAP = {'1000': 'wireless', '2001': 'landline', '3001': 'dead'}
DROP_STATUS_MAP = {}

# Classifications we act on:
#   wireless  -> SMS proceeds
#   landline  -> RVM-only (SMS cancelled)
#   dead      -> permanently suppressed
#   blacklist -> permanently suppressed
#   callback  -> they called back / IVR opt-out -> removed (called_in)
#   unknown   -> leave as-is, decide later


def classify_drop_status(code, message=''):
    c = str(code).strip()
    if c in DROP_STATUS_MAP:
        return DROP_STATUS_MAP[c]
    m = (message or '').lower()
    if any(k in m for k in ('landline', 'fixed line', 'fixed-line', 'fixed voip')):
        return 'landline'
    if any(k in m for k in ('blacklist', 'litigator', 'blocked', 'dnc')):
        return 'blacklist'
    if any(k in m for k in ('dead', 'invalid', 'disconnect', 'unreachable', 'no longer')):
        return 'dead'
    if any(k in m for k in ('callback', 'called back', 'missed call', 'transfer', 'inbound')):
        return 'callback'
    if any(k in m for k in ('wireless', 'mobile', 'cell', 'delivered', 'success', 'complete')):
        return 'wireless'
    return 'unknown'


def dispatch_due_rvms(limit=2000):
    """Fire every due RVM touch (eligible now, lead active). Honors dry-run.

    On send: mark the touch sent, stamp that day's SMS times from the real send
    moment, and move the lead to in_progress. Returns a summary dict.
    """
    dry   = is_dry_run()
    token = campaign_token()
    now   = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')

    conn = db.get_db()
    rows = conn.execute(
        "SELECT t.id, t.lead_id, t.cohort_id, t.run_number, t.audio_slot_id, l.phone AS phone "
        "FROM touches t JOIN leads l ON l.id = t.lead_id "
        "WHERE t.touch_type='rvm' AND t.status IN ('planned','eligible') "
        "AND t.eligible_at <= ? AND l.status IN ('enrolled','in_progress') "
        "ORDER BY t.eligible_at LIMIT ?",
        (now, limit)
    ).fetchall()
    conn.close()

    audio   = {a['id']: a for a in sq.get_audio()}
    summary = {'due': len(rows), 'sent': 0, 'skipped_dnc': 0, 'errors': 0, 'dry_run': dry}

    if not dry and not token:
        summary['error'] = 'No Drop campaign token set (Engine settings) — nothing sent.'
        return summary

    for r in rows:
        phone = r['phone']
        if db.is_dnc(phone):
            _cancel_touch(r['id'], 'dnc')
            summary['skipped_dnc'] += 1
            continue
        a = audio.get(r['audio_slot_id'])
        audio_url = a['url'] if a else ''
        try:
            if dry:
                activity_token = f"DRYRUN-{r['id']}"
            else:
                resp = drop.post_record(token, phone, audio_url=audio_url or None,
                                        custom={'C1': r['lead_id'], 'C2': r['cohort_id']})
                activity_token = resp.get('ActivityToken', '')
            _mark_rvm_sent(r['id'], r['lead_id'], r['run_number'], activity_token, now)
            summary['sent'] += 1
        except Exception as e:
            _mark_rvm_error(r['id'])
            summary['errors'] += 1
            logger.warning("[RVM] dispatch failed for lead %s: %s", r['lead_id'], e)

    logger.info("[RVM] dispatch %s: %s", 'DRY-RUN' if dry else 'LIVE', summary)
    return summary


def _mark_rvm_sent(touch_id, lead_id, run_number, activity_token, now):
    conn = db.get_db()
    conn.execute("UPDATE touches SET status='sent', sent_at=?, drop_activity_token=? WHERE id=?",
                 (now, activity_token, touch_id))
    conn.execute("UPDATE leads SET status='in_progress' WHERE id=? AND status='enrolled'", (lead_id,))
    conn.commit()
    conn.close()
    sq.stamp_rvm_day_sms(lead_id, run_number, now)


def _cancel_touch(touch_id, reason):
    conn = db.get_db()
    conn.execute("UPDATE touches SET status='cancelled', skipped_reason=? WHERE id=?", (reason, touch_id))
    conn.commit()
    conn.close()


def _mark_rvm_error(touch_id):
    conn = db.get_db()
    conn.execute("UPDATE touches SET status='failed', skipped_reason='drop_error' WHERE id=?", (touch_id,))
    conn.commit()
    conn.close()


def handle_drop_status(data):
    """Apply one Drop status webhook payload to the sequence. Returns the
    classification string (for logging)."""
    code    = data.get('DropStatusCode')
    msg     = data.get('DropStatusMessage') or ''
    phone   = data.get('PhoneTo') or ''
    lead_id = data.get('C1')
    try:
        lead_id = int(lead_id) if lead_id not in (None, '') else None
    except (ValueError, TypeError):
        lead_id = None

    cls = classify_drop_status(code, msg)

    if cls == 'callback':
        # Called back / IVR opt-out -> remove from the machine (called_in).
        target = None
        if lead_id:
            lead = sq.get_lead(lead_id)
            target = lead['phone'] if lead else None
        target = target or phone
        if target:
            sq.suppress_and_cancel(target, 'called_in', 'called_in', source='drop')
        return cls

    if lead_id:
        gated = cls if cls in ('wireless', 'landline', 'dead', 'blacklist') else 'unknown'
        sq.apply_line_type(lead_id, gated)
    return cls
