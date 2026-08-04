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


# Drop DropStatusCode -> classification. Drop's docs don't publish these, so we
# map them from live responses (webhook + VMDropStatus) as we observe them;
# unmapped codes fall back to keyword-matching DropStatusMessage.
#
# Observed live (2026-07), from the delivery webhook / VMDropStatus:
#   18  "Failed-VM Unreachable"          -> RVM couldn't drop this attempt
#   23  "Failed-RVM Vmail Not Detected"  -> no mailbox reached
#   -1  (DropStatusMessage null)         -> never queued / not yet processed
#
# IMPORTANT: 18/23 are RVM *delivery* failures, NOT dead-number signals — the
# number may be a perfectly good wireless line that just didn't take a VM this
# run. So we map them to 'unknown' (keep the lead; RVM retries next run; SMS
# gating comes from Twilio Lookup, not this). Drop reports the DROP OUTCOME, not
# a carrier or a clean mobile/landline flag — confirmed no Carrier field in the
# real webhook payload either. Use the Twilio Lookup scrubber for line type.
DROP_STATUS_MAP = {
    '18': 'unknown',
    '23': 'unknown',
}

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
    # DNC flavors are NOT interchangeable — see classify_rejection. A national or
    # state DNC must never be treated as a blacklist kill.
    kind = classify_rejection(m)
    if kind in ('blacklist', 'already_client', 'registry_dnc'):
        return kind
    # NB: 'unreachable' is deliberately NOT here — for RVM it means the voicemail
    # couldn't be dropped this attempt, not that the number is dead.
    if any(k in m for k in ('dead', 'invalid', 'disconnect', 'no longer')):
        return 'dead'
    if any(k in m for k in ('callback', 'called back', 'missed call', 'transfer', 'inbound')):
        return 'callback'
    if any(k in m for k in ('wireless', 'mobile', 'cell', 'delivered', 'success', 'complete')):
        return 'wireless'
    return 'unknown'


def classify_rejection(message):
    """Classify a Drop /Delivery rejection. The distinction is load-bearing:

      already_client — Drop's CUSTOMER DNC. These people called in and signed up
                       already; they're clients, so all marketing stops.
      registry_dnc   — national / state DNC. They opted in through our form, so
                       SMS continues; only RVM stops (lead demotes to Lane B).
      blacklist      — litigator / blacklist. Killed and reported separately.
      other          — anything else: cancel the touch, keep the lead.

    Only an explicit CUSTOMER dnc kills a lead. Every other DNC flavor demotes,
    so a national-DNC bounce can never silently destroy a textable opted-in lead.
    """
    m = (message or '').lower()
    if any(k in m for k in ('litigator', 'blacklist')):
        return 'blacklist'
    if 'dnc' in m or 'do not call' in m or 'do-not-call' in m:
        if 'customer' in m:
            return 'already_client'
        return 'registry_dnc'
    return 'other'


def dispatch_due_rvms(limit=2000):
    """Fire every due RVM touch (eligible now, lead active). Honors dry-run.

    On send: mark the touch sent, stamp that day's SMS times from the real send
    moment, and move the lead to in_progress. Returns a summary dict.
    """
    if not sq.within_send_window():
        return {'due': 0, 'sent': 0, 'skipped_dnc': 0, 'errors': 0,
                'dry_run': is_dry_run(), 'outside_window': True}
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
    summary = {'due': len(rows), 'sent': 0, 'skipped_dnc': 0, 'rejected': 0,
               'already_client': 0, 'demoted_sms_only': 0, 'blacklisted': 0,
               'errors': 0, 'dry_run': dry}

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
                if not resp.get('accepted'):
                    # Drop refused the record at post time. Not sent, not a
                    # transport error — classify it and move on.
                    msg = str(resp.get('ApiStatusMessage') or 'rejected')
                    _cancel_touch(r['id'], msg[:60])
                    summary['rejected'] += 1
                    kind = classify_rejection(msg)
                    if kind == 'already_client':
                        # They already called in and signed up — stop all marketing.
                        sq.suppress_and_cancel(phone, 'already_client', 'already_client',
                                               source='drop')
                        summary['already_client'] += 1
                    elif kind == 'registry_dnc':
                        # National/state DNC. They opted in through our form, so we
                        # keep texting — we just never voicemail them again.
                        sq.demote_to_sms_only(r['lead_id'], 'dnc_registry')
                        summary['demoted_sms_only'] += 1
                    elif kind == 'blacklist':
                        sq.suppress_and_cancel(phone, 'blacklist', 'blacklist', source='drop')
                        summary['blacklisted'] += 1
                    logger.info("[RVM] Drop rejected lead %s (%s): %s", r['lead_id'], kind, msg)
                    continue
                activity_token = resp.get('ActivityToken', '')
            _mark_rvm_sent(r['id'], r['lead_id'], r['run_number'], activity_token, now)
            if dry:
                # No real Drop webhook in dry-run — simulate a wireless drop so the
                # SMS flow can be exercised end-to-end.
                sq.apply_line_type(r['lead_id'], 'wireless')
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

    def _phone_for_lead():
        if lead_id:
            lead = sq.get_lead(lead_id)
            if lead:
                return lead['phone']
        return phone

    if cls == 'callback':
        # Called back / IVR opt-out -> remove from the machine (called_in).
        target = _phone_for_lead()
        if target:
            sq.suppress_and_cancel(target, 'called_in', 'called_in', source='drop')
        return cls

    if cls == 'already_client':
        # Customer DNC — they already called in and signed up. Stop everything.
        target = _phone_for_lead()
        if target:
            sq.suppress_and_cancel(target, 'already_client', 'already_client', source='drop')
        return cls

    if cls == 'registry_dnc':
        # National/state DNC — keep texting (they opted in), stop voicemailing.
        if lead_id:
            sq.demote_to_sms_only(lead_id, 'dnc_registry')
        return cls

    if lead_id:
        gated = cls if cls in ('wireless', 'landline', 'dead', 'blacklist') else 'unknown'
        sq.apply_line_type(lead_id, gated)
    return cls
