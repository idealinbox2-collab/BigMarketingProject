import time
import json
import logging
import re
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, date
import pytz
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
import database as db

logger = logging.getLogger(__name__)

# ── Tunable defaults (all overridable live via settings) ──────────────────────
DEFAULT_RATE_MPS   = 0.2   # messages/sec PER NUMBER  (0.2 = 1 message / 5s)
DEFAULT_MAX_WORKERS = 16   # concurrent in-flight sends across this campaign
RETRY_ATTEMPTS      = 3    # attempts per message on transient Twilio errors

# HTTP statuses / Twilio codes that are worth retrying (transient)
_TRANSIENT_HTTP  = {429, 500, 502, 503, 504}
_TRANSIENT_CODES = {20429, 20500, 20503}


# ── Per-number rate limiter (token bucket) ────────────────────────────────────
# ONE bucket per phone number, shared PROCESS-WIDE. Every campaign thread draws
# from the same buckets, so a number can never exceed its own per-second rate no
# matter how many campaigns are running at once. The bucket IS the anti-herd
# mechanism: if many workers want the same "lowest-total-sent" number, only as
# many as have tokens get it; the rest fall through to the next number.

class TokenBucket:
    def __init__(self, rate_per_sec, capacity=None):
        self.rate = max(0.0001, float(rate_per_sec))
        self.capacity = capacity if capacity is not None else max(1.0, self.rate)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def set_rate(self, rate_per_sec):
        """Live-adjust the rate without dropping accumulated tokens."""
        with self.lock:
            self.rate = max(0.0001, float(rate_per_sec))
            self.capacity = max(1.0, self.rate)
            if self.tokens > self.capacity:
                self.tokens = self.capacity

    def try_consume(self, amount=1):
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens >= amount:
                self.tokens -= amount
                return True
            return False


_buckets = {}
_buckets_lock = threading.Lock()


def get_bucket(phone_number, rate_per_sec):
    """Fetch (or create) the shared bucket for a number and keep its rate current."""
    with _buckets_lock:
        b = _buckets.get(phone_number)
        if b is None:
            b = TokenBucket(rate_per_sec)
            _buckets[phone_number] = b
        else:
            b.set_rate(rate_per_sec)
        return b


def _current_rate_mps():
    try:
        return max(0.0001, float(db.get_setting('send_rate_mps', str(DEFAULT_RATE_MPS))))
    except (ValueError, TypeError):
        return DEFAULT_RATE_MPS


def _global_paused():
    return str(db.get_setting('global_pause', '0')).strip() in ('1', 'true', 'True')


def effective_daily_cap(num):
    """
    The daily ceiling for a number, enforced for ALL numbers (this is the cap
    the user sets per number).
      • warmup numbers: the warmup schedule limit (but never above daily_limit)
      • normal numbers: daily_limit
      • daily_limit <= 0  → uncapped (None)
    """
    daily_limit = num.get('daily_limit')
    daily_limit = int(daily_limit) if daily_limit not in (None, '') else 0
    hard = daily_limit if daily_limit > 0 else None

    if num.get('warmup_mode'):
        wl = get_warmup_limit(num.get('warmup_start_date', ''))
        if wl is not None:
            return wl if hard is None else min(wl, hard)
    return hard

# Hard opt-out keywords (exact match)
OPT_OUT_KEYWORDS = {
    # canonical
    'stop', 'quit', 'cancel', 'unsubscribe', 'end', 'stopall',
    'optout', 'revoke', 'delete', 'remove',
    # bare "no" and variants — client wants these treated as opt-out
    'no', 'no.', 'no!', 'nope', 'nah', 'naw', 'noo', 'nooo', 'noooo',
    'no thanks', 'no thank you', 'nothanks',
    # misspelled / spaced / styled "stop"
    'stopp', 'stoppp', 'sto', 'stp', 'stap', 'stahp', 'stip', 'stop.', 'stop!',
    's t o p', 'st0p', 'sopt', 'stopit', 'stopitt', 'stfu',
    # misspelled unsubscribe / cancel / quit
    'unsub', 'unsubcribe', 'unsubscibe', 'unsubscrbe', 'cancle', 'cancel', 'qui',
}
OPT_IN_KEYWORDS  = {'start', 'unstop', 'yes', 'yeah', 'yep', 'interested'}

# Fuzzy opt-out phrases (substring match — order doesn't matter)
OPT_OUT_PHRASES = [
    # do-not variants
    'do not text', 'dont text', "don't text", 'do not text me',
    'do not contact', 'dont contact', "don't contact", 'do not contact me',
    'do not send', 'dont send', "don't send",
    'do not call', 'dont call', "don't call",
    'do not message', 'dont message', "don't message",
    'do not reach', 'stop reaching',
    # stop variants
    'stop texting', 'stop messaging', 'stop sending', 'stop contacting',
    'stop calling', 'stop reaching out', 'please stop', 'stop now',
    # remove / off variants
    'remove me', 'take me off', 'take me out', 'count me out', 'leave me out',
    'opt out', 'opt-out', 'opt me out', 'wish to opt', 'like to opt',
    'unsubscribe me', 'remove my number', 'remove this number', 'delete my number',
    # no-more variants
    'no more texts', 'no more messages', 'no more', 'no further',
    'stop the texts', 'stop the messages',
    # not-interested / wrong-recipient (common lead-gen opt-outs)
    'not interested', 'no thank you', 'no thanks', 'no interest',
    'did not request', 'didnt request', "didn't request", 'never requested',
    'did not sign up', 'didnt sign up', "didn't sign up", 'never signed up',
    'did not apply', 'didnt apply', "didn't apply", 'never applied',
    'did not ask', 'didnt ask', "didn't ask",
    'wrong number', 'wrong person', 'who is this', 'who are you',
    'never contacted you', 'lose my number', 'lose this number',
    # hostile / firm
    'leave me alone', 'stop harassing', 'quit texting', 'quit messaging',
    'report you', 'reporting you', 'i will report', 'file a complaint',
    'unsubscribe', 'revoke consent', 'withdraw consent',
]

# First-word triggers: if the reply STARTS with one of these, treat as opt-out.
# 'no' and 'out' live here (not as exact keywords) so "no stop" / "out, remove me"
# count, but a mid-sentence 'no' in an interested reply is less likely to trip.
OPT_OUT_STARTERS = ['stop', 'quit', 'cancel', 'unsubscribe', 'end', 'no', 'nope',
                    'remove', 'delete', 'optout']
TWILIO_OPT_OUT_CODES = {21610}

# Warmup schedule: day_number → max messages that day
WARMUP_SCHEDULE = {
    1: 50,
    2: 100,
    3: 200,
    4: 300,
    5: 500,
    6: 750,
    7: 1000,
}
WARMUP_DAYS = 7  # After this many days warmup auto-completes


# ── Warmup Helpers ────────────────────────────────────────────────────────────

def get_warmup_limit(warmup_start_date_str):
    """
    Given a warmup start date string (YYYY-MM-DD), return today's send limit.
    Returns None if warmup is complete (day > WARMUP_DAYS).
    """
    if not warmup_start_date_str:
        return None
    try:
        start = date.fromisoformat(warmup_start_date_str)
    except (ValueError, TypeError):
        return None
    day = (date.today() - start).days + 1  # Day 1 = start date
    if day > WARMUP_DAYS:
        return None  # Warmup complete
    return WARMUP_SCHEDULE.get(day, 50)


def get_warmup_day(warmup_start_date_str):
    """Returns current warmup day number, or None if complete."""
    if not warmup_start_date_str:
        return None
    try:
        start = date.fromisoformat(warmup_start_date_str)
    except (ValueError, TypeError):
        return None
    day = (date.today() - start).days + 1
    return day if day <= WARMUP_DAYS else None


def is_number_available_today(num):
    """
    Check if a number can still send today based on warmup/daily limits.
    Returns (available: bool, limit: int|None, sent_today: int)
    """
    today = date.today().isoformat()
    daily_sent = num.get('daily_sent', 0) if num.get('daily_sent_date') == today else 0

    if num.get('warmup_mode'):
        warmup_start = num.get('warmup_start_date', '')
        limit = get_warmup_limit(warmup_start)
        if limit is None:
            # Warmup complete — auto-graduate (will be handled in campaign loop)
            return True, None, daily_sent
        return daily_sent < limit, limit, daily_sent
    else:
        return True, None, daily_sent


# ── Send Window ───────────────────────────────────────────────────────────────

def get_timezone():
    tz_name = db.get_setting('send_timezone', 'America/Los_Angeles')
    try:
        return pytz.timezone(tz_name)
    except Exception:
        return pytz.timezone('America/Los_Angeles')


def is_within_send_window():
    tz = get_timezone()
    now = datetime.now(tz)
    schedule = db.get_send_schedule()
    if now.weekday() not in schedule['send_days']:
        return False
    return schedule['start_hour'] <= now.hour < schedule['end_hour']


def seconds_until_next_window():
    tz = get_timezone()
    now = datetime.now(tz)
    schedule = db.get_send_schedule()
    start_hour = schedule['start_hour']
    send_days  = schedule['send_days']

    for days_ahead in range(8):
        candidate = now + timedelta(days=days_ahead)
        if candidate.weekday() not in send_days:
            continue
        if days_ahead == 0 and now.hour >= schedule['end_hour']:
            continue
        if days_ahead == 0 and now.hour < start_hour:
            target = candidate.replace(hour=start_hour, minute=0, second=0, microsecond=0)
            return max(0, (target - now).total_seconds())
        if days_ahead > 0:
            target = candidate.replace(hour=start_hour, minute=0, second=0, microsecond=0)
            return max(0, (target - now).total_seconds())

    return 86400


# ── Opt-out Detection ─────────────────────────────────────────────────────────

def _edit_distance(a, b):
    """Levenshtein distance — small helper for catching misspelled opt-outs."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        cur = [i + 1]
        for j, cb in enumerate(b):
            cur.append(min(prev[j + 1] + 1, cur[j] + 1, prev[j] + (ca != cb)))
        prev = cur
    return prev[-1]


def _is_fuzzy_optout_word(word):
    """True if a single word is a near-miss of a core opt-out word (typos)."""
    if not word or len(word) > 12:
        return False
    # collapse repeated letters (stoppp -> stop, noooo -> no)
    collapsed = re.sub(r'(.)\1{2,}', r'\1', word)
    for target in ('stop', 'quit', 'cancel', 'unsubscribe', 'optout'):
        # allow 1 edit for short words, 2 for longer ones
        tol = 1 if len(target) <= 5 else 2
        if _edit_distance(collapsed, target) <= tol:
            return True
    return False


def classify_inbound(body):
    clean = body.strip().lower()
    clean_nopunct = re.sub(r'[^\w\s]', ' ', clean).strip()
    words = clean_nopunct.split()
    first_word = words[0] if words else ''

    # Opt-in must be checked before fuzzy opt-out so "start"/"yes" aren't misread
    if clean in OPT_IN_KEYWORDS or clean_nopunct in OPT_IN_KEYWORDS:
        return 'opted_in'

    if clean in OPT_OUT_KEYWORDS or clean_nopunct in OPT_OUT_KEYWORDS:
        return 'opted_out'
    if first_word in OPT_OUT_STARTERS:
        return 'opted_out'
    for phrase in OPT_OUT_PHRASES:
        if phrase in clean or phrase in clean_nopunct:
            return 'opted_out'
    # Fuzzy: a short reply that's a misspelled stop/quit/etc, or starts with one
    if _is_fuzzy_optout_word(clean_nopunct.replace(' ', '')):
        return 'opted_out'
    if first_word and _is_fuzzy_optout_word(first_word):
        return 'opted_out'
    return 'other'


# ── Number Rotation ───────────────────────────────────────────────────────────

def pick_sending_number(contact_phone, available_numbers):
    """
    Pick the best number for this contact:
    1. Avoid last-used number (rotation)
    2. Among candidates, prefer number with fewest total sends
    3. Respect warmup daily limits
    """
    if not available_numbers:
        return None
    if len(available_numbers) == 1:
        return available_numbers[0]

    last_used  = db.get_last_sending_number(contact_phone)
    candidates = [n for n in available_numbers if n['phone_number'] != last_used]

    if not candidates:
        candidates = available_numbers

    return min(candidates, key=lambda n: n['total_sent'])


# ── Message Merge ─────────────────────────────────────────────────────────────

def merge_message(template, first_name, custom_fields=None, agent=None, callback=None):
    """
    Replace merge tags in template.
    Supports: [first_name], [agent], [callback], [loan_amount], [monthly_payment],
              [state], [email], and any custom field from the CSV.
    """
    result = template
    first_name = (first_name or '').strip()

    # Standard first_name tags
    for tag in ('[first_name]', '[First_Name]', '[FirstName]', '[name]', '[Name]'):
        result = result.replace(tag, first_name)

    # Agent and callback rotation tags
    if agent is not None:
        result = result.replace('[agent]', agent)
        result = result.replace('[AGENT]', agent)
    if callback is not None:
        result = result.replace('[callback]', callback)
        result = result.replace('[CALLBACK]', callback)

    # Custom fields from CSV
    if custom_fields:
        if isinstance(custom_fields, str):
            try:
                custom_fields = json.loads(custom_fields)
            except (json.JSONDecodeError, TypeError):
                custom_fields = {}
        if isinstance(custom_fields, dict):
            for key, value in custom_fields.items():
                result = result.replace(f'[{key}]', str(value) if value else '')
                result = result.replace(f'[{key.lower()}]', str(value) if value else '')
                result = result.replace(f'[{key.upper()}]', str(value) if value else '')

    return result


def _parse_list(json_str):
    """Safely parse a JSON array string. Returns empty list on failure."""
    if not json_str:
        return []
    try:
        result = json.loads(json_str)
        return [str(x).strip() for x in result if str(x).strip()]
    except (json.JSONDecodeError, TypeError):
        return []


def _build_rotation_state(campaign):
    """
    Build shuffled rotation lists for agents, callbacks, and message variants.
    Called once at campaign start. Returns a dict with shuffled lists and index.
    If any list is empty, that slot is None (use whatever is in the template).
    """
    agents    = _parse_list(campaign.get('agent_names', ''))
    callbacks = _parse_list(campaign.get('callback_numbers', ''))
    variants  = _parse_list(campaign.get('message_variants', ''))

    # Shuffle each list independently for maximum variation
    if agents:    random.shuffle(agents)
    if callbacks: random.shuffle(callbacks)
    if variants:  random.shuffle(variants)

    return {
        'agents':    agents,
        'callbacks': callbacks,
        'variants':  variants,
        'idx':       0,  # increments each send
    }


def pick_rotation_values(rotation_state):
    """
    Pick next agent, callback, and message variant from shuffled rotation.
    Re-shuffles each list when exhausted.
    Returns (agent, callback, variant_body) — any can be None if list is empty.
    """
    idx = rotation_state['idx']

    def pick(lst):
        if not lst:
            return None
        pos = idx % len(lst)
        # Reshuffle when we've gone through the whole list
        if pos == 0 and idx > 0:
            random.shuffle(lst)
        return lst[pos]

    agent    = pick(rotation_state['agents'])
    callback = pick(rotation_state['callbacks'])
    variant  = pick(rotation_state['variants'])

    rotation_state['idx'] += 1
    return agent, callback, variant


# ── Single Send ───────────────────────────────────────────────────────────────

def send_single_message(to_phone, from_number, message_body, message_type,
                        media_url, account_sid, auth_token, status_callback=None):
    from twilio.http.http_client import TwilioHttpClient
    http_client = TwilioHttpClient(timeout=30)
    client = Client(account_sid, auth_token, http_client=http_client)
    params = {
        'body':  message_body,
        'from_': db.format_e164(from_number),
        'to':    db.format_e164(to_phone),
    }
    if message_type == 'mms' and media_url:
        params['media_url'] = [media_url]
    if status_callback:
        params['status_callback'] = status_callback
    msg = client.messages.create(**params)
    return msg.sid


# ── Send with retry ───────────────────────────────────────────────────────────

class OptOutError(Exception):
    """Raised when Twilio rejects because the recipient has opted out (21610)."""


def _is_transient(exc):
    if isinstance(exc, TwilioRestException):
        status = getattr(exc, 'status', None)
        code   = getattr(exc, 'code', None)
        return status in _TRANSIENT_HTTP or code in _TRANSIENT_CODES
    # Network-level errors (timeouts, connection resets) have no Twilio code
    return True


def send_with_retry(to_phone, from_number, body, message_type, media_url,
                    account_sid, auth_token, status_callback):
    """
    Send one message, retrying transient failures with exponential backoff.
    Raises OptOutError on carrier opt-out, or the last exception on permanent
    failure / exhausted retries.
    """
    last_exc = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return send_single_message(
                to_phone=to_phone, from_number=from_number, message_body=body,
                message_type=message_type, media_url=media_url,
                account_sid=account_sid, auth_token=auth_token,
                status_callback=status_callback,
            )
        except TwilioRestException as e:
            code = getattr(e, 'code', None)
            if code in TWILIO_OPT_OUT_CODES or 'unsubscribed' in str(e).lower():
                raise OptOutError(str(e))
            last_exc = e
            if not _is_transient(e) or attempt == RETRY_ATTEMPTS - 1:
                raise
        except Exception as e:  # network/timeout
            last_exc = e
            if attempt == RETRY_ATTEMPTS - 1:
                raise
        time.sleep(0.5 * (2 ** attempt))  # 0.5s, 1s, 2s
    if last_exc:
        raise last_exc


# ── Error tracking (concurrency-safe consecutive-failure auto-pause) ──────────

class _ErrorTracker:
    """Auto-pause a campaign after N consecutive failures, across all workers."""
    def __init__(self, threshold=10):
        self.threshold = threshold
        self.streak = 0
        self.lock = threading.Lock()

    def success(self):
        with self.lock:
            self.streak = 0

    def failure(self):
        with self.lock:
            self.streak += 1
            return self.streak >= self.threshold


# ── Campaign Loop ─────────────────────────────────────────────────────────────

def _wait_for_window(campaign_id):
    wait = seconds_until_next_window()
    logger.info(f"[Campaign {campaign_id}] Outside send window — sleeping {int(wait)}s")
    slept = 0
    while slept < wait:
        c = db.get_campaign(campaign_id)
        if not c or c['status'] == 'cancelled':
            return False
        if c['status'] == 'paused':
            time.sleep(5)
            wait  = seconds_until_next_window()
            slept = 0
            continue
        chunk = min(30, wait - slept)
        time.sleep(chunk)
        slept += chunk
    return True


def _auto_graduate_warmup(num):
    """If warmup is complete, flip warmup_mode off automatically."""
    if not num.get('warmup_mode'):
        return
    warmup_start = num.get('warmup_start_date', '')
    if get_warmup_limit(warmup_start) is None:
        # Day > WARMUP_DAYS — graduate
        db.update_number_warmup(num['id'], warmup_mode=0, daily_limit=num.get('daily_limit', 500))
        logger.info(f"Number {num['phone_number']} graduated from warmup to full send ✅")


def _worker_send(contact, num, campaign, media_url, status_callback,
                 rotation_state, brand, err_tracker):
    """Runs in the thread pool: does one send + all bookkeeping for one contact.
    The number\'s capacity was ALREADY reserved by the dispatcher before we got
    here, so on any non-send outcome we must release it."""
    contact_phone = contact['phone']
    from_number   = num['phone_number']
    try:
        agent, callback, variant_body = pick_rotation_values(rotation_state)
        template = variant_body if variant_body else campaign['message_body']
        body = merge_message(template, contact.get('first_name') or '',
                             contact.get('custom_fields', '{}'),
                             agent=agent, callback=callback)
        try:
            sid = send_with_retry(
                to_phone=contact_phone, from_number=from_number, body=body,
                message_type=campaign['message_type'], media_url=media_url,
                account_sid=num['account_sid'], auth_token=num['auth_token'],
                status_callback=status_callback,
            )
        except OptOutError as e:
            db.release_number_capacity(from_number)      # never actually sent
            db.add_to_dnc(contact_phone, 'twilio_opt_out', 'twilio')
            db.mark_contact_dnc(contact['id'], campaign['id'])
            err_tracker.success()                        # opt-out isn't a health problem
            logger.info(f"[Campaign {campaign['id']}] Opt-out {contact_phone}: {e}")
            return

        # Success
        db.mark_contact_sent(contact['id'], campaign['id'], from_number,
                             message_sid=sid, variant_sent=template)
        db.update_number_history(contact_phone, from_number)
        if brand:
            db.record_brand_contact(contact_phone, brand)
        err_tracker.success()
        logger.info(f"[Campaign {campaign['id']}] Sent -> {contact_phone} from {from_number} SID={sid}")

    except TwilioRestException as e:
        db.release_number_capacity(from_number)
        code = getattr(e, 'code', None)
        # Permanently-dead destination (invalid/landline) → suppress for future campaigns
        if str(code) in db.DEAD_NUMBER_CODES:
            db.add_to_dnc(contact_phone, 'dead_number', 'auto')
        db.mark_contact_failed(contact['id'], campaign['id'], str(e)[:500])
        if err_tracker.failure():
            db.update_campaign_status(campaign['id'], 'paused')
            logger.error(f"[Campaign {campaign['id']}] Too many consecutive errors - auto-paused")
        logger.warning(f"[Campaign {campaign['id']}] Twilio error {code} for {contact_phone}: {e}")

    except Exception as e:
        db.release_number_capacity(from_number)
        db.mark_contact_failed(contact['id'], campaign['id'], str(e)[:500])
        if err_tracker.failure():
            db.update_campaign_status(campaign['id'], 'paused')
            logger.error(f"[Campaign {campaign['id']}] Too many consecutive errors - auto-paused")
        logger.error(f"[Campaign {campaign['id']}] Unexpected send error for {contact_phone}: {e}")


def run_campaign(campaign_id, base_url):
    """
    Concurrent campaign engine.

    Model: one dispatcher (this thread) hands ready (contact, number) pairs to a
    pool of worker threads. A message only goes out when BOTH gates pass, per
    number, in one place:
      1. the number has a per-second token in its SHARED bucket (rate limit), and
      2. claim_number_capacity() reserves a daily-cap slot atomically.
    Because the buckets and the daily-cap reservation are process-wide, running
    two campaigns at once can never push a single number past its own limits -
    they transparently share each number\'s capacity.
    """
    logger.info(f"[Campaign {campaign_id}] Engine starting")

    try:
        campaign = db.get_campaign(campaign_id)
        if not campaign:
            logger.error(f"[Campaign {campaign_id}] Not found - aborting")
            return

        brand = campaign.get('brand', '') or ''

        media_url = None
        if campaign['message_type'] == 'mms' and campaign['media_filename']:
            media_url = f"{base_url.rstrip('/')}/static/uploads/{campaign['media_filename']}"

        base = db.get_setting('base_url', '').strip().rstrip('/')
        status_callback = f"{base}/webhook/status" if base else None

        # Recover any contacts stranded 'sending' by a prior crash/redeploy
        recovered = db.reset_inflight_contacts(campaign_id)
        if recovered:
            logger.info(f"[Campaign {campaign_id}] Recovered {recovered} in-flight contact(s) from a previous run")

        rotation_state = _build_rotation_state(campaign)
        err_tracker = _ErrorTracker(threshold=10)

        try:
            max_workers = max(1, int(db.get_setting('max_workers', str(DEFAULT_MAX_WORKERS))))
        except (ValueError, TypeError):
            max_workers = DEFAULT_MAX_WORKERS

        pool = ThreadPoolExecutor(max_workers=max_workers)
        futures = set()

        numbers = []
        numbers_refreshed = 0.0

        try:
            while True:
                current = db.get_campaign(campaign_id)
                if not current:
                    break
                status = current['status']

                if status == 'cancelled':
                    logger.info(f"[Campaign {campaign_id}] Cancelled - stopping")
                    break
                if status == 'paused' or _global_paused():
                    time.sleep(3)
                    continue
                if not is_within_send_window():
                    if not _wait_for_window(campaign_id):
                        break
                    continue

                # Refresh the number snapshot every ~5s (ordering only; capacity
                # correctness comes from the atomic claim, not this snapshot).
                now = time.monotonic()
                if now - numbers_refreshed > 5 or not numbers:
                    all_numbers = db.get_active_numbers(brand=brand if brand else None)
                    for n in all_numbers:
                        _auto_graduate_warmup(n)
                    # lowest-total-sent first (your chosen strategy)
                    numbers = sorted(all_numbers, key=lambda n: n.get('total_sent', 0))
                    numbers_refreshed = now

                if not numbers:
                    logger.warning(f"[Campaign {campaign_id}] No active numbers - waiting")
                    time.sleep(5)
                    continue

                # Backpressure: don't claim work faster than the pool can drain
                if len(futures) >= max_workers:
                    _reap(futures, block=True)
                    continue

                # Claim one contact atomically (idempotency guard)
                contact = db.claim_next_pending_contact(campaign_id)
                if contact is None:
                    if futures:
                        _reap(futures, block=True)   # let stragglers finish
                        continue
                    db.update_campaign_status(campaign_id, 'completed')
                    logger.info(f"[Campaign {campaign_id}] Completed!")
                    break

                # Cheap DNC check before spending a number reservation
                if db.is_dnc(contact['phone']):
                    db.mark_contact_dnc(contact['id'], campaign_id)
                    continue

                # Find a number: prefer not-last-used, lowest-total-sent, that has
                # a free rate token AND a free daily-cap slot right now.
                rate = _current_rate_mps()
                last_used = db.get_last_sending_number(contact['phone'])
                ordered = ([n for n in numbers if n['phone_number'] != last_used] +
                           [n for n in numbers if n['phone_number'] == last_used])

                chosen = None
                for n in ordered:
                    cap = effective_daily_cap(n)
                    if not get_bucket(n['phone_number'], rate).try_consume():
                        continue                                  # at per-sec rate
                    if db.claim_number_capacity(n['phone_number'], cap):
                        chosen = n
                        break                                     # reserved!
                    # lost the cap race (or hit daily cap) -> try next number

                if chosen is None:
                    # Every number is momentarily saturated (rate) or capped (daily).
                    db.requeue_contact(contact['id'])
                    _reap(futures, block=False)
                    time.sleep(0.25)
                    continue

                futures.add(pool.submit(
                    _worker_send, contact, chosen, current, media_url,
                    status_callback, rotation_state, brand, err_tracker
                ))
                _reap(futures, block=False)

        finally:
            # Drain outstanding sends before the thread exits
            for f in list(futures):
                try:
                    f.result(timeout=60)
                except Exception:
                    pass
            pool.shutdown(wait=True)

    except Exception as e:
        logger.exception(f"[Campaign {campaign_id}] Engine crashed: {e}")
        try:
            db.update_campaign_status(campaign_id, 'paused')
        except Exception:
            pass

    logger.info(f"[Campaign {campaign_id}] Engine exiting")


def _reap(futures, block=False):
    """Remove finished futures from the set. If block, wait for at least one."""
    if not futures:
        return
    done = {f for f in futures if f.done()}
    if not done and block:
        # Wait for the earliest to finish
        from concurrent.futures import wait, FIRST_COMPLETED
        finished, _ = wait(futures, timeout=30, return_when=FIRST_COMPLETED)
        done |= set(finished)
    for f in done:
        futures.discard(f)


# ── Inbound Webhook ───────────────────────────────────────────────────────────

def handle_inbound(from_phone, body, to_number=''):
    reply_type = classify_inbound(body)
    db.log_inbound_reply(from_phone, to_number, body, reply_type)

    if reply_type == 'opted_out':
        db.add_to_dnc(from_phone, 'stop_reply', 'inbound', their_message=body)
        logger.info(f"Opt-out recorded: {from_phone} — '{body}'")
        return 'opted_out'

    if reply_type == 'opted_in':
        db.remove_from_dnc(from_phone)
        logger.info(f"Opt-in recorded: {from_phone}")
        return 'opted_in'

    logger.info(f"Inbound reply (other): {from_phone} — '{body[:50]}'")
    return 'other'
