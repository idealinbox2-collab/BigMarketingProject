"""Sequence engine — the per-lead RVM + SMS drip.

This module owns everything the campaign-blast platform did not: cohorts, leads
(the ledger), the materialized per-lead plan (`touches`), and the editable
resource pools (SMS templates by stage, callback numbers, agent names, RVM audio).

Phase 1 scope: schema, resource-pool data access, cohort enrollment, plan
materialization, and lead preview. No sending happens here — later phases add the
RVM dispatcher, the SMS pacer, suppression, and the daily cleanup.

Design source of truth: docs/DESIGN.md. SQL is kept portable (no SQLite-only
syntax) so a later Postgres move is mechanical.
"""
import json
import logging
import re
import threading
from datetime import datetime, date, timedelta

import pytz

import database as db

logger = logging.getLogger(__name__)

PACIFIC = pytz.timezone('America/Los_Angeles')

# One process-wide lock serializes all dispatch (RVM + SMS) so a scheduler tick
# and a manual "Run now" can never double-send the same touch.
dispatch_lock = threading.Lock()


def within_send_window(nowpac=None):
    """Hard quiet-hours backstop: no sequence RVM/SMS outside these Pacific hours,
    regardless of manual triggers or scheduler timing. Configurable via settings
    (default 7:00 AM – 9:00 PM PT, which brackets every scheduled anchor)."""
    try:
        start = int(db.get_setting('seq_window_start', '7'))
        end = int(db.get_setting('seq_window_end', '21'))
    except (ValueError, TypeError):
        start, end = 7, 21
    now = nowpac or datetime.now(PACIFIC)
    return start <= now.hour < end

# ── Sequence shape ────────────────────────────────────────────────────────────
RVM_RUNS = (1, 3, 5)          # RVM days
SMS_ONLY_RUNS = (2, 4)        # SMS-only days
TOTAL_RUNS = 5

# RVM morning anchor (Pacific), by timezone bucket. East -> west stagger so every
# lead is hit mid-morning in their OWN local time (v2 spec).
#   ET 8:30 PT = 11:30 local · CT 9:30 PT = 11:30 local · MT/PT 10:30 PT
RVM_ANCHOR = {'ET': (8, 30), 'CT': (9, 30), 'MT': (10, 30), 'PT': (10, 30)}
# On RVM days, SMS is planned relative to the RVM anchor (re-stamped to the
# lead's ACTUAL RVM send time once it fires — see Phase 2).
RVM_DAY_SMS_OFFSETS_MIN = {1: 90, 2: 180}       # SMS#1 +1.5h, SMS#2 +3h
# SMS-only days use the SAME anchors as RVM days — one clock for the whole week.
SMS_ONLY_START = dict(RVM_ANCHOR)
SMS_ONLY_SMS2_OFFSET_MIN = 210                  # +3.5h

# A cohort uploaded after this Pacific hour starts on the NEXT business day —
# otherwise run 1 would compress a whole day of touches into the afternoon.
UPLOAD_CUTOFF_HOUR_PT = 8

# Lanes (v2). Which channels a lead is allowed to receive.
LANE_FULL = 'A'        # RVM + SMS
LANE_SMS_ONLY = 'B'    # national/state DNC — opted in, textable, never RVM'd
LANE_RVM_ONLY = 'C'    # landline — dropped, never texted

# Template stages, mapped by run/day (editable design default)
STAGES = ('checking_in', 'following_up', 'last_day')


def stage_for_run(run_number):
    if run_number <= 2:
        return 'checking_in'
    if run_number <= 4:
        return 'following_up'
    return 'last_day'


# ── State -> timezone bucket ──────────────────────────────────────────────────
# Predominant zone per state (a few states straddle; we pick the majority zone).
_STATE_BUCKET = {}
for _abbrs, _bucket in [
    ('CT DE DC FL GA IN KY MA MD ME MI NC NH NJ NY OH PA RI SC VA VT WV', 'ET'),
    ('AL AR IA IL KS LA MN MO MS ND NE OK SD TN TX WI', 'CT'),
    ('AZ CO ID MT NM UT WY', 'MT'),
    ('CA NV OR WA AK HI', 'PT'),
]:
    for _a in _abbrs.split():
        _STATE_BUCKET[_a] = _bucket

_STATE_NAMES = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR',
    'california': 'CA', 'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE',
    'district of columbia': 'DC', 'florida': 'FL', 'georgia': 'GA', 'hawaii': 'HI',
    'idaho': 'ID', 'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA', 'kansas': 'KS',
    'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME', 'maryland': 'MD',
    'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN', 'mississippi': 'MS',
    'missouri': 'MO', 'montana': 'MT', 'nebraska': 'NE', 'nevada': 'NV',
    'new hampshire': 'NH', 'new jersey': 'NJ', 'new mexico': 'NM', 'new york': 'NY',
    'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH', 'oklahoma': 'OK',
    'oregon': 'OR', 'pennsylvania': 'PA', 'rhode island': 'RI',
    'south carolina': 'SC', 'south dakota': 'SD', 'tennessee': 'TN', 'texas': 'TX',
    'utah': 'UT', 'vermont': 'VT', 'virginia': 'VA', 'washington': 'WA',
    'west virginia': 'WV', 'wisconsin': 'WI', 'wyoming': 'WY',
}

# Unknown state defaults to the LATEST bucket so we never risk texting/dropping
# too early in someone's real local morning (TCPA-safe default).
DEFAULT_BUCKET = 'PT'


def timezone_bucket_for_state(state):
    if not state:
        return DEFAULT_BUCKET
    s = str(state).strip()
    if len(s) == 2 and s.upper() in _STATE_BUCKET:
        return _STATE_BUCKET[s.upper()]
    abbr = _STATE_NAMES.get(s.lower())
    if abbr:
        return _STATE_BUCKET.get(abbr, DEFAULT_BUCKET)
    return DEFAULT_BUCKET


# IANA timezone -> bucket. The list's per-phone `timezone` column is authoritative
# because the phone's real zone can differ from the mailing address (a Texas
# address carrying a Los Angeles cell would otherwise be dropped 3 hours early).
_TZ_BUCKET = {
    'america/new_york': 'ET', 'america/detroit': 'ET', 'america/toronto': 'ET',
    'america/indiana/indianapolis': 'ET', 'america/kentucky/louisville': 'ET',
    'us/eastern': 'ET', 'est': 'ET', 'edt': 'ET', 'et': 'ET', 'eastern': 'ET',
    'america/chicago': 'CT', 'america/winnipeg': 'CT', 'america/menominee': 'CT',
    'america/indiana/knox': 'CT', 'us/central': 'CT', 'cst': 'CT', 'cdt': 'CT',
    'ct': 'CT', 'central': 'CT',
    'america/denver': 'MT', 'america/phoenix': 'MT', 'america/boise': 'MT',
    'america/edmonton': 'MT', 'us/mountain': 'MT', 'mst': 'MT', 'mdt': 'MT',
    'mt': 'MT', 'mountain': 'MT',
    'america/los_angeles': 'PT', 'america/vancouver': 'PT', 'america/anchorage': 'PT',
    'america/juneau': 'PT', 'pacific/honolulu': 'PT', 'us/pacific': 'PT',
    'pst': 'PT', 'pdt': 'PT', 'pt': 'PT', 'pacific': 'PT',
}


def timezone_bucket_for(timezone_name='', state=''):
    """Send-clock bucket for a lead. The per-phone IANA timezone wins; the state
    is only a fallback when the column is missing or unrecognized."""
    tz = (timezone_name or '').strip().lower()
    if tz:
        bucket = _TZ_BUCKET.get(tz)
        if bucket:
            return bucket
        # tolerate "America/Los_Angeles (PDT)" style values
        for key, val in _TZ_BUCKET.items():
            if '/' in key and key in tz:
                return val
    return timezone_bucket_for_state(state)


# ── Schema ────────────────────────────────────────────────────────────────────

def init_sequence_db():
    conn = db.get_db()
    conn.executescript(db.portable_schema('''
        CREATE TABLE IF NOT EXISTS cohorts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            brand TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            default_texts_per_day INTEGER DEFAULT 2,
            default_sms_rate INTEGER DEFAULT 12000,
            size INTEGER DEFAULT 0,
            notes TEXT DEFAULT '',
            start_date TEXT DEFAULT '',
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cohort_id INTEGER NOT NULL,
            first_name TEXT DEFAULT '',
            last_name TEXT DEFAULT '',
            phone TEXT NOT NULL,
            state TEXT DEFAULT '',
            amount TEXT DEFAULT '',
            custom_fields TEXT DEFAULT '{}',
            timezone_bucket TEXT DEFAULT '',
            timezone_name TEXT DEFAULT '',
            line_type TEXT DEFAULT 'unknown',
            carrier TEXT DEFAULT '',
            lane TEXT DEFAULT 'A',
            callback_slot_id INTEGER,
            status TEXT DEFAULT 'enrolled',
            outcome TEXT DEFAULT '',
            current_run INTEGER DEFAULT 1,
            enrolled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            removed_at TIMESTAMP,
            removed_reason TEXT DEFAULT ''
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_cohort_phone ON leads(cohort_id, phone);
        CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone);
        CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);

        CREATE TABLE IF NOT EXISTS touches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            cohort_id INTEGER NOT NULL,
            run_number INTEGER NOT NULL,
            touch_type TEXT NOT NULL,
            step_in_day INTEGER NOT NULL,
            seq_index INTEGER NOT NULL,
            stage TEXT DEFAULT '',
            template_slot_id INTEGER,
            agent_slot_id INTEGER,
            callback_slot_id INTEGER,
            audio_slot_id INTEGER,
            eligible_at TEXT,
            eligible_estimated INTEGER DEFAULT 0,
            status TEXT DEFAULT 'planned',
            sending_number TEXT DEFAULT '',
            message_sid TEXT DEFAULT '',
            drop_activity_token TEXT DEFAULT '',
            sent_at TEXT,
            delivery_status TEXT DEFAULT '',
            error_code TEXT DEFAULT '',
            skipped_reason TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_touches_pacer ON touches(status, touch_type, eligible_at);
        CREATE INDEX IF NOT EXISTS idx_touches_lead ON touches(lead_id, seq_index);

        CREATE TABLE IF NOT EXISTS sms_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stage TEXT NOT NULL,
            body TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            weight INTEGER DEFAULT 1,
            version INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS callback_numbers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            number TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            notes TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS agent_names (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS rvm_audio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            url TEXT NOT NULL,
            run_mapping TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS called_in_uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT DEFAULT 'manual',
            row_count INTEGER DEFAULT 0,
            matched_count INTEGER DEFAULT 0,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    '''))
    conn.commit()

    # v2 migrations — columns added after the first release. Each is attempted
    # independently so a database at any prior version lands on the full schema.
    for ddl in (
        "ALTER TABLE leads ADD COLUMN lane TEXT DEFAULT 'A'",
        "ALTER TABLE leads ADD COLUMN carrier TEXT DEFAULT ''",
        "ALTER TABLE leads ADD COLUMN timezone_name TEXT DEFAULT ''",
    ):
        try:
            conn.execute(ddl)
            conn.commit()
        except Exception:
            conn.rollback()          # column already present
    conn.close()


# ── Resource pools ────────────────────────────────────────────────────────────

def add_template(stage, body, weight=1):
    if stage not in STAGES:
        raise ValueError(f'unknown stage {stage!r}')
    conn = db.get_db()
    cur = conn.execute(
        'INSERT INTO sms_templates (stage, body, weight) VALUES (?,?,?)',
        (stage, body, int(weight))
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def get_templates(stage=None, active_only=False):
    conn = db.get_db()
    q = 'SELECT * FROM sms_templates'
    where, params = [], []
    if stage:
        where.append('stage=?'); params.append(stage)
    if active_only:
        where.append('active=1')
    if where:
        q += ' WHERE ' + ' AND '.join(where)
    q += ' ORDER BY stage, id'
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_template(template_id, body=None, active=None, weight=None):
    sets, params = [], []
    if body is not None:
        sets += ['body=?', 'version=version+1']; params.append(body)
    if active is not None:
        sets.append('active=?'); params.append(1 if active else 0)
    if weight is not None:
        sets.append('weight=?'); params.append(int(weight))
    if not sets:
        return
    sets.append("updated_at=?"); params.append(datetime.utcnow())
    params.append(template_id)
    conn = db.get_db()
    conn.execute(f'UPDATE sms_templates SET {", ".join(sets)} WHERE id=?', params)
    conn.commit()
    conn.close()


def add_callback(number, notes=''):
    number = db.normalize_phone(number)
    conn = db.get_db()
    cur = conn.execute('INSERT INTO callback_numbers (number, notes) VALUES (?,?)', (number, notes))
    cid = cur.lastrowid
    conn.commit()
    conn.close()
    return cid


def get_callbacks(active_only=False):
    conn = db.get_db()
    q = 'SELECT * FROM callback_numbers'
    if active_only:
        q += ' WHERE active=1'
    q += ' ORDER BY id'
    rows = conn.execute(q).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_callback(callback_id, number=None, active=None, notes=None):
    """Replace/toggle a callback slot. Replacing the number heals every lead
    stuck to this slot — their next send carries the new value automatically."""
    sets, params = [], []
    if number is not None:
        sets.append('number=?'); params.append(db.normalize_phone(number))
    if active is not None:
        sets.append('active=?'); params.append(1 if active else 0)
    if notes is not None:
        sets.append('notes=?'); params.append(notes)
    if not sets:
        return
    sets.append('updated_at=?'); params.append(datetime.utcnow())
    params.append(callback_id)
    conn = db.get_db()
    conn.execute(f'UPDATE callback_numbers SET {", ".join(sets)} WHERE id=?', params)
    conn.commit()
    conn.close()


def add_agent(name):
    conn = db.get_db()
    cur = conn.execute('INSERT INTO agent_names (name) VALUES (?)', (name,))
    aid = cur.lastrowid
    conn.commit()
    conn.close()
    return aid


def get_agents(active_only=False):
    conn = db.get_db()
    q = 'SELECT * FROM agent_names'
    if active_only:
        q += ' WHERE active=1'
    q += ' ORDER BY id'
    rows = conn.execute(q).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_agent(agent_id, name=None, active=None):
    sets, params = [], []
    if name is not None:
        sets.append('name=?'); params.append(name)
    if active is not None:
        sets.append('active=?'); params.append(1 if active else 0)
    if not sets:
        return
    params.append(agent_id)
    conn = db.get_db()
    conn.execute(f'UPDATE agent_names SET {", ".join(sets)} WHERE id=?', params)
    conn.commit()
    conn.close()


def add_audio(label, url, run_mapping=''):
    conn = db.get_db()
    cur = conn.execute(
        'INSERT INTO rvm_audio (label, url, run_mapping) VALUES (?,?,?)',
        (label, url, run_mapping)
    )
    aid = cur.lastrowid
    conn.commit()
    conn.close()
    return aid


def get_audio(active_only=False):
    conn = db.get_db()
    q = 'SELECT * FROM rvm_audio'
    if active_only:
        q += ' WHERE active=1'
    q += ' ORDER BY id'
    rows = conn.execute(q).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_audio(audio_id, label=None, url=None, run_mapping=None, active=None):
    sets, params = [], []
    if label is not None:
        sets.append('label=?'); params.append(label)
    if url is not None:
        sets.append('url=?'); params.append(url)
    if run_mapping is not None:
        sets.append('run_mapping=?'); params.append(run_mapping)
    if active is not None:
        sets.append('active=?'); params.append(1 if active else 0)
    if not sets:
        return
    params.append(audio_id)
    conn = db.get_db()
    conn.execute(f'UPDATE rvm_audio SET {", ".join(sets)} WHERE id=?', params)
    conn.commit()
    conn.close()


def audio_for_run(audio_pool, run_number):
    """Pick the audio slot for a run: prefer one whose run_mapping includes the
    run; else the first active audio. Returns an id or None."""
    if not audio_pool:
        return None
    for a in audio_pool:
        mapping = (a.get('run_mapping') or '').strip()
        if mapping:
            runs = {int(x) for x in mapping.replace(' ', '').split(',') if x.isdigit()}
            if run_number in runs:
                return a['id']
    # no explicit mapping matched -> first active
    return audio_pool[0]['id']


# ── Scheduling helpers ────────────────────────────────────────────────────────

def business_days(start, count):
    """List of `count` weekday dates starting at `start` (rolled forward off a
    weekend). Skips Sat/Sun."""
    d = start
    while d.weekday() >= 5:            # 5=Sat, 6=Sun
        d += timedelta(days=1)
    out = []
    while len(out) < count:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def default_start_date(today=None, now_pac=None):
    """Enrollment start: today if it's a weekday AND we're still before the first
    anchor; otherwise the next business day. Uploading at 2 PM would otherwise
    make every run-1 touch instantly due and compress the day into one burst."""
    now_pac = now_pac or datetime.now(PACIFIC)
    d = today or now_pac.date()
    if today is None and now_pac.hour >= UPLOAD_CUTOFF_HOUR_PT:
        d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _pacific_to_utc_str(d, hour, minute):
    """Pacific wall-clock (DST-aware) -> naive-UTC 'YYYY-MM-DD HH:MM:SS' string,
    matching the DB's existing UTC timestamp convention."""
    naive = datetime(d.year, d.month, d.day, hour, minute)
    pac = PACIFIC.localize(naive)
    utc = pac.astimezone(pytz.utc).replace(tzinfo=None)
    return utc.strftime('%Y-%m-%d %H:%M:%S')


def _plus_minutes_utc_str(utc_str, minutes):
    dt = datetime.strptime(utc_str, '%Y-%m-%d %H:%M:%S') + timedelta(minutes=minutes)
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def plan_touch_times(bucket, run_date, run_number):
    """Return the planned (touch_type, step_in_day, eligible_at_utc, estimated)
    tuples for one run/day. RVM-day SMS times are estimates off the RVM anchor —
    they get re-stamped to the actual RVM send time in Phase 2."""
    out = []
    if run_number in RVM_RUNS:
        ah, am = RVM_ANCHOR[bucket]
        rvm_utc = _pacific_to_utc_str(run_date, ah, am)
        out.append(('rvm', 0, rvm_utc, False))
        out.append(('sms', 1, _plus_minutes_utc_str(rvm_utc, RVM_DAY_SMS_OFFSETS_MIN[1]), True))
        out.append(('sms', 2, _plus_minutes_utc_str(rvm_utc, RVM_DAY_SMS_OFFSETS_MIN[2]), True))
    else:
        sh, sm = SMS_ONLY_START[bucket]
        sms1_utc = _pacific_to_utc_str(run_date, sh, sm)
        out.append(('sms', 1, sms1_utc, False))
        out.append(('sms', 2, _plus_minutes_utc_str(sms1_utc, SMS_ONLY_SMS2_OFFSET_MIN), False))
    return out


# ── Slot assignment ───────────────────────────────────────────────────────────

def _pick_no_repeat(pool_ids, count, offset):
    """Pick `count` ids from pool_ids starting at `offset`, distinct while the
    pool is large enough; wraps (allowing repeats) only if count > pool size."""
    if not pool_ids:
        return [None] * count
    n = len(pool_ids)
    return [pool_ids[(offset + i) % n] for i in range(count)]


# ── Enrollment + materialization ──────────────────────────────────────────────

# Header synonyms the machine understands. EVERY other column rides along
# untouched as a merge tag and comes back in the run report — the uploaded list
# is never mutated, only annotated.
_COL = {
    'phone':    ('phone_primary', 'phone', 'phone_number', 'phonenumber', 'telephone',
                 'mobile', 'cell'),
    'first':    ('first_name', 'firstname', 'first', 'name'),
    'last':     ('last_name', 'lastname', 'last'),
    'state':    ('state', 'st'),
    'amount':   ('total_unsecured', 'amount', 'total_debt', 'debt', 'balance',
                 'unsec_installment_bal'),
    'timezone': ('timezone', 'time_zone', 'tz'),
    'carrier':  ('carrier_name', 'carrier'),
    'linetype': ('line_type', 'linetype', 'landline'),
    'sms_ok':   ('sms_ok', 'smsok'),
    'rvm_ok':   ('rvm_ok', 'rvmok'),
    'dead':     ('dead_number', 'deadnumber', 'dead'),
    'validation': ('validation_status', 'validationstatus'),
    'contact':  ('contact_status', 'contactstatus'),
}
_STD_KEYS = {k for keys in _COL.values() for k in keys}

_TRUE_WORDS = {'1', 'true', 'yes', 'y', 't', 'ok'}
_FALSE_WORDS = {'0', 'false', 'no', 'n', 'f'}


def _flag(value, default=None):
    """Read a boolean-ish column. Blank/unrecognized -> default (usually None =
    'not stated', which callers treat as permissive)."""
    v = (value or '').strip().lower()
    if v in _TRUE_WORDS:
        return True
    if v in _FALSE_WORDS:
        return False
    return default


def _extract(row):
    """Map one CSV row onto the fields the machine acts on, plus the pass-through
    custom fields. Returns a dict — originals are never modified."""
    low = {k.strip().lower(): (v or '').strip() for k, v in row.items() if k}

    def pick(field):
        for key in _COL[field]:
            if low.get(key):
                return low[key]
        return ''

    return {
        'phone':      pick('phone'),
        'first':      pick('first'),
        'last':       pick('last'),
        'state':      pick('state'),
        'amount':     pick('amount'),
        'timezone':   pick('timezone'),
        'carrier':    pick('carrier'),
        'line_type':  pick('linetype'),
        'sms_ok':     _flag(pick('sms_ok')),
        'rvm_ok':     _flag(pick('rvm_ok')),
        'dead':       _flag(pick('dead'), False),
        'validation': pick('validation').lower(),
        'contact':    pick('contact').lower(),
        'custom': {k.strip(): (v or '').strip()
                   for k, v in row.items()
                   if k and k.strip().lower() not in _STD_KEYS and (v or '').strip()},
    }


def normalize_carrier(name):
    """Carrier -> a stable key the no-text switches match on ('T-Mobile USA, Inc.'
    and 'TMOBILE' both become 'tmobile'). Blank stays blank = unknown = textable."""
    n = (name or '').strip().lower()
    if not n:
        return ''
    squashed = re.sub(r'[^a-z0-9]', '', n)
    for key, needles in (
        ('tmobile',  ('tmobile', 'tmo', 'metropcs', 'metro')),
        ('verizon',  ('verizon', 'vzw')),
        ('att',      ('att', 'atandt', 'cingular', 'cricket')),
        ('sprint',   ('sprint', 'boost')),
        ('uscellular', ('uscellular', 'uscc')),
    ):
        if any(x in squashed for x in needles):
            return key
    return squashed[:32]


# Line-type values that mean "cannot receive SMS" (RVM-only lane).
_NON_SMS_LINE_TYPES = {'landline', 'fixed', 'fixedline', 'fixed_line', 'voip',
                       'fixedvoip', 'nonfixedvoip', 'true', 'yes'}
# validation_status / contact_status values that kill a lead before enrollment.
_KILL_VALIDATION = ('blacklist', 'litigator', 'invalid', 'dead', 'disconnect',
                    'unassigned', 'do_not_call', 'dnc')


def lane_for(rec):
    """Which channels this lead may receive, from the registry's own permissions.

    sms_ok / rvm_ok are authoritative when present (the upstream registry already
    resolved DNC + line type); line_type is the fallback signal. Returns a lane
    or None when the lead has no usable channel at all."""
    sms_ok, rvm_ok = rec['sms_ok'], rec['rvm_ok']
    lt = re.sub(r'[^a-z]', '', (rec['line_type'] or '').lower())
    if sms_ok is None:
        sms_ok = lt not in _NON_SMS_LINE_TYPES
    if rvm_ok is None:
        rvm_ok = True
    if sms_ok and rvm_ok:
        return LANE_FULL
    if sms_ok:
        return LANE_SMS_ONLY
    if rvm_ok:
        return LANE_RVM_ONLY
    return None


def enroll_cohort(name, rows, brand='', start_date=None, texts_per_day=2,
                  sms_rate=12000, notes=''):
    """Create a cohort, enroll its leads, and materialize every lead's plan.

    rows: iterable of dict-like CSV rows.
    Returns a summary dict (counts + cohort_id).
    """
    start = start_date or default_start_date()
    if isinstance(start, str):
        start = date.fromisoformat(start)
    run_dates = business_days(start, TOTAL_RUNS)

    # Pools (snapshotted once for even round-robin across this cohort)
    callbacks = [c['id'] for c in get_callbacks(active_only=True)]
    agents = [a['id'] for a in get_agents(active_only=True)]
    audio_pool = get_audio(active_only=True)
    stage_templates = {s: [t['id'] for t in get_templates(stage=s, active_only=True)] for s in STAGES}

    conn = db.get_db()
    cur = conn.execute(
        'INSERT INTO cohorts (name, brand, default_texts_per_day, default_sms_rate, notes, start_date) '
        'VALUES (?,?,?,?,?,?)',
        (name, brand.strip(), int(texts_per_day), int(sms_rate), notes, start.isoformat())
    )
    cohort_id = cur.lastrowid
    conn.commit()
    conn.close()

    seen = set()
    counts = {'loaded': 0, 'invalid': 0, 'dupes': 0, 'suppressed': 0, 'already_active': 0,
              'dead': 0, 'blacklisted': 0, 'no_channel': 0, 'non_us': 0,
              'lane_a': 0, 'lane_b': 0, 'lane_c': 0}
    lead_i = 0

    for row in rows:
        rec = _extract(row)
        if not rec['phone']:
            continue
        phone = db.normalize_phone(rec['phone'])
        if len(phone) != 10:
            counts['invalid'] += 1
            continue
        if not _is_us_number(phone):
            counts['non_us'] += 1
            continue
        if phone in seen:
            counts['dupes'] += 1
            continue
        seen.add(phone)

        # Registry verdicts — the list already knows these; don't re-litigate.
        if rec['dead']:
            counts['dead'] += 1
            continue
        verdict = f"{rec['validation']} {rec['contact']}"
        if any(k in verdict for k in _KILL_VALIDATION):
            counts['blacklisted'] += 1
            continue

        if db.is_dnc(phone):
            counts['suppressed'] += 1
            continue
        if _phone_active_elsewhere(phone):
            counts['already_active'] += 1
            continue

        lane = lane_for(rec)
        if lane is None:                      # neither channel allowed
            counts['no_channel'] += 1
            continue

        bucket = timezone_bucket_for(rec['timezone'], rec['state'])
        callback_slot = callbacks[lead_i % len(callbacks)] if callbacks else None
        _create_lead_and_plan(cohort_id, rec, phone, bucket, lane, callback_slot,
                              run_dates, agents, audio_pool, stage_templates, lead_i)
        counts['loaded'] += 1
        counts[{'A': 'lane_a', 'B': 'lane_b', 'C': 'lane_c'}[lane]] += 1
        lead_i += 1

    conn = db.get_db()
    conn.execute('UPDATE cohorts SET size=? WHERE id=?', (counts['loaded'], cohort_id))
    conn.commit()
    conn.close()

    counts['cohort_id'] = cohort_id
    logger.info("[Cohort %s '%s'] enrolled=%s (A=%s B=%s C=%s) dupes=%s invalid=%s "
                "non_us=%s dead=%s blacklisted=%s no_channel=%s dnc=%s active_elsewhere=%s",
                cohort_id, name, counts['loaded'], counts['lane_a'], counts['lane_b'],
                counts['lane_c'], counts['dupes'], counts['invalid'], counts['non_us'],
                counts['dead'], counts['blacklisted'], counts['no_channel'],
                counts['suppressed'], counts['already_active'])
    return counts


def demote_to_sms_only(lead_id, reason='dnc_registry'):
    """Drop bounced this number as national/state DNC. They opted in through our
    form, so texting continues — we just stop trying to voicemail them. Cancels
    every remaining RVM and moves the lead to Lane B.

    If the lead had no SMS to begin with (a landline), there's no channel left
    at all, so it exits instead of idling for the rest of the week."""
    conn = db.get_db()
    row = conn.execute('SELECT lane FROM leads WHERE id=?', (lead_id,)).fetchone()
    if not row:
        conn.close()
        return None
    if row['lane'] == LANE_RVM_ONLY:
        conn.close()
        return exit_no_channel(lead_id, reason)

    cur = conn.execute(
        "UPDATE touches SET status='cancelled', skipped_reason=? "
        "WHERE lead_id=? AND touch_type='rvm' AND status IN ('planned','eligible')",
        (reason, lead_id)
    )
    cancelled = cur.rowcount
    conn.execute("UPDATE leads SET lane=? WHERE id=?", (LANE_SMS_ONLY, lead_id))
    conn.commit()
    conn.close()
    logger.info("[Lane] lead %s demoted to SMS-only (%s), %s RVM(s) cancelled",
                lead_id, reason, cancelled)
    return {'lane': LANE_SMS_ONLY, 'cancelled_rvms': cancelled}


def exit_no_channel(lead_id, reason='no_channel'):
    """No usable channel remains (e.g. a landline that's also on a DNC registry).
    Close the lead out now rather than leaving it 'active' with nothing to send."""
    conn = db.get_db()
    conn.execute(
        "UPDATE touches SET status='cancelled', skipped_reason=? "
        "WHERE lead_id=? AND status IN ('planned','eligible')", (reason, lead_id)
    )
    conn.execute(
        "UPDATE leads SET status='removed', outcome='no_channel', removed_at=?, "
        "removed_reason=? WHERE id=?", (datetime.utcnow(), reason, lead_id)
    )
    conn.commit()
    conn.close()
    logger.info("[Lane] lead %s exited — no channel left (%s)", lead_id, reason)
    return {'lane': None, 'outcome': 'no_channel'}


# Non-US area codes inside the NANP — a 10-digit number alone doesn't prove US.
# Canada + Caribbean members share the +1 country code; US territories (PR 787/939,
# Guam 671, USVI 340, N. Mariana 670, Am. Samoa 684) are US and stay in.
_NON_US_AREA_CODES = frozenset("""
204 226 236 249 250 263 289 306 343 354 365 367 368 382 387 403 416 418 428 431
437 438 450 468 474 506 514 519 548 579 581 584 587 600 604 613 639 647 672 683
705 709 742 753 778 780 782 807 819 825 867 873 879 902 905
242 246 264 268 284 345 441 473 649 664 721 758 767 784 809 829 849 868 869 876
""".split())


def _is_us_number(phone10):
    """True when a normalized 10-digit NANP number is US (or a US territory)."""
    if len(phone10) != 10:
        return False
    return phone10[:3] not in _NON_US_AREA_CODES


def _phone_active_elsewhere(phone):
    conn = db.get_db()
    row = conn.execute(
        "SELECT 1 FROM leads WHERE phone=? AND status IN ('enrolled','in_progress') LIMIT 1",
        (phone,)
    ).fetchone()
    conn.close()
    return row is not None


def _create_lead_and_plan(cohort_id, rec, phone, bucket, lane, callback_slot,
                          run_dates, agents, audio_pool, stage_templates, lead_i):
    # Lanes A and B are textable by definition (the registry's paid lookup already
    # settled it), so SMS doesn't wait on Drop to confirm the line.
    line_type = 'landline' if lane == LANE_RVM_ONLY else 'wireless'
    conn = db.get_db()
    cur = conn.execute(
        'INSERT INTO leads (cohort_id, first_name, last_name, phone, state, amount, '
        'custom_fields, timezone_bucket, timezone_name, carrier, lane, line_type, '
        'callback_slot_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (cohort_id, rec['first'], rec['last'], phone, rec['state'], rec['amount'],
         json.dumps(rec['custom']), bucket, rec['timezone'],
         normalize_carrier(rec['carrier']), lane, line_type, callback_slot)
    )
    lead_id = cur.lastrowid
    conn.commit()
    conn.close()

    # Agent sticky per day: one agent per run for this lead.
    agent_by_run = {}
    for ridx, run in enumerate(range(1, TOTAL_RUNS + 1)):
        agent_by_run[run] = agents[(lead_i + ridx) % len(agents)] if agents else None

    # Template no-repeat per stage: pick distinct templates within each stage,
    # offset per lead for even A/B spread across the cohort.
    stage_counts = {'checking_in': 0, 'following_up': 0, 'last_day': 0}
    for run in range(1, TOTAL_RUNS + 1):
        for tt, step in [('rvm', 0), ('sms', 1), ('sms', 2)] if run in RVM_RUNS else [('sms', 1), ('sms', 2)]:
            if tt == 'sms':
                stage_counts[stage_for_run(run)] += 1
    stage_picks = {}
    for s in STAGES:
        pool = stage_templates.get(s, [])
        stage_picks[s] = _pick_no_repeat(pool, stage_counts[s], lead_i) if pool else [None] * stage_counts[s]
    stage_cursor = {'checking_in': 0, 'following_up': 0, 'last_day': 0}

    # Lane decides which channels get materialized at all: B never plans an RVM,
    # C never plans an SMS. A touch that was never created can't be fired by
    # anything downstream — the safest possible gate.
    allow_rvm = lane in (LANE_FULL, LANE_RVM_ONLY)
    allow_sms = lane in (LANE_FULL, LANE_SMS_ONLY)

    touch_rows = []
    seq = 0
    for run in range(1, TOTAL_RUNS + 1):
        run_date = run_dates[run - 1]
        for (tt, step, eligible, estimated) in plan_touch_times(bucket, run_date, run):
            if tt == 'rvm' and not allow_rvm:
                continue
            if tt == 'sms' and not allow_sms:
                continue
            seq += 1
            if tt == 'rvm':
                touch_rows.append((
                    lead_id, cohort_id, run, 'rvm', step, seq, '',
                    None, agent_by_run.get(run), callback_slot,
                    audio_for_run(audio_pool, run), eligible, 1 if estimated else 0, 'planned'
                ))
            else:
                stg = stage_for_run(run)
                tmpl = stage_picks[stg][stage_cursor[stg]]
                stage_cursor[stg] += 1
                # "Estimated" means the time gets re-stamped when the day's RVM
                # actually fires. With no RVM in this lane it never will, so the
                # anchor-derived time is already final.
                est = 1 if (estimated and allow_rvm) else 0
                touch_rows.append((
                    lead_id, cohort_id, run, 'sms', step, seq, stg,
                    tmpl, agent_by_run.get(run), callback_slot,
                    None, eligible, est, 'planned'
                ))

    conn = db.get_db()
    conn.executemany(
        'INSERT INTO touches (lead_id, cohort_id, run_number, touch_type, step_in_day, '
        'seq_index, stage, template_slot_id, agent_slot_id, callback_slot_id, audio_slot_id, '
        'eligible_at, eligible_estimated, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        touch_rows
    )
    conn.commit()
    conn.close()
    return lead_id


# ── Merge / preview ───────────────────────────────────────────────────────────

def render_body(body, lead, agent_name, callback_number):
    """Fill merge tags for preview / send. Supports [first_name], [last_name],
    [agent], [callback], [amount], [state], and any custom field, case-tolerant."""
    if not body:
        return ''
    ctx = {
        'first_name': lead.get('first_name', ''),
        'last_name': lead.get('last_name', ''),
        'agent': agent_name or '',
        'callback': db.format_e164(callback_number) if callback_number else '',
        'amount': lead.get('amount', ''),
        'state': lead.get('state', ''),
    }
    try:
        ctx.update({k: v for k, v in json.loads(lead.get('custom_fields') or '{}').items()})
    except (ValueError, TypeError):
        pass
    result = body
    for key, val in ctx.items():
        val = '' if val is None else str(val)
        for variant in (f'[{key}]', f'[{key.lower()}]', f'[{key.upper()}]',
                        f'[{key.capitalize()}]'):
            result = result.replace(variant, val)
    return result


def _utc_str_to_pacific(utc_str):
    if not utc_str:
        return ''
    try:
        dt = datetime.strptime(str(utc_str)[:19], '%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        return str(utc_str)
    pac = pytz.utc.localize(dt).astimezone(PACIFIC)
    return pac.strftime('%a %Y-%m-%d %I:%M %p %Z')


def _day_start_utc(now_pac=None):
    """UTC timestamp string for Pacific midnight of the current day — the
    boundary for 'today' counters (sent_at is stored UTC)."""
    now_pac = now_pac or datetime.now(PACIFIC)
    start = PACIFIC.localize(datetime(now_pac.year, now_pac.month, now_pac.day))
    return start.astimezone(pytz.utc).strftime('%Y-%m-%d %H:%M:%S')


def get_lead_plan(lead_id):
    """Full rendered plan for one lead — the preview payload."""
    conn = db.get_db()
    lead = conn.execute('SELECT * FROM leads WHERE id=?', (lead_id,)).fetchone()
    if not lead:
        conn.close()
        return None
    lead = dict(lead)
    touches = [dict(r) for r in conn.execute(
        'SELECT * FROM touches WHERE lead_id=? ORDER BY seq_index', (lead_id,)
    ).fetchall()]
    conn.close()

    # Resolve slot values live (that's the whole point of slots)
    tmpl = {t['id']: t for t in get_templates()}
    agents = {a['id']: a['name'] for a in get_agents()}
    callbacks = {c['id']: c['number'] for c in get_callbacks()}
    audio = {a['id']: a for a in get_audio()}

    steps = []
    for t in touches:
        agent_name = agents.get(t['agent_slot_id'], '')
        callback_num = callbacks.get(t['callback_slot_id'], '')
        row = {
            'seq': t['seq_index'], 'run': t['run_number'], 'type': t['touch_type'],
            'step_in_day': t['step_in_day'], 'stage': t['stage'],
            'eligible_at_pacific': _utc_str_to_pacific(t['eligible_at']),
            'eligible_estimated': bool(t['eligible_estimated']),
            'status': t['status'], 'agent': agent_name,
            'callback': db.format_e164(callback_num) if callback_num else '',
        }
        if t['touch_type'] == 'sms':
            body = tmpl.get(t['template_slot_id'], {}).get('body', '')
            row['template_id'] = t['template_slot_id']
            row['message'] = render_body(body, lead, agent_name, callback_num)
        else:
            a = audio.get(t['audio_slot_id'])
            row['audio'] = a['label'] if a else '(no audio configured)'
            row['audio_url'] = a['url'] if a else ''
        steps.append(row)

    return {'lead': lead, 'steps': steps}


def get_cohort_leads(cohort_id, limit=100, offset=0):
    conn = db.get_db()
    rows = conn.execute(
        'SELECT id, first_name, last_name, phone, state, amount, timezone_bucket, '
        'line_type, carrier, lane, status, outcome, current_run FROM leads WHERE cohort_id=? '
        'ORDER BY id LIMIT ? OFFSET ?', (cohort_id, limit, offset)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_cohorts():
    conn = db.get_db()
    rows = conn.execute('SELECT * FROM cohorts ORDER BY uploaded_at DESC').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_lead(lead_id):
    conn = db.get_db()
    row = conn.execute('SELECT * FROM leads WHERE id=?', (lead_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ── Line-type gating & suppression (Phase 2) ──────────────────────────────────

def stamp_rvm_day_sms(lead_id, run_number, rvm_sent_at):
    """After a lead's RVM actually fires, set that day's SMS eligible_at from the
    real send time (+1.5h / +3h) and clear the estimate flag."""
    conn = db.get_db()
    for step, off in RVM_DAY_SMS_OFFSETS_MIN.items():
        new = _plus_minutes_utc_str(rvm_sent_at, off)
        conn.execute(
            "UPDATE touches SET eligible_at=?, eligible_estimated=0 "
            "WHERE lead_id=? AND run_number=? AND touch_type='sms' AND step_in_day=? "
            "AND status IN ('planned','eligible')",
            (new, lead_id, run_number, step)
        )
    conn.commit()
    conn.close()


def apply_line_type(lead_id, line_type):
    """Record a lead's line type and gate SMS: wireless -> SMS proceed;
    landline/voip -> cancel SMS (RVM-only); dead/blacklist -> full suppress."""
    if line_type not in ('wireless', 'landline', 'voip', 'dead', 'blacklist', 'unknown'):
        line_type = 'unknown'
    conn = db.get_db()
    conn.execute('UPDATE leads SET line_type=? WHERE id=?', (line_type, lead_id))
    conn.commit()
    conn.close()

    if line_type in ('dead', 'blacklist'):
        lead = get_lead(lead_id)
        if lead:
            reason = 'dead_number' if line_type == 'dead' else 'blacklist'
            suppress_and_cancel(lead['phone'], reason, line_type, source='drop')
    elif line_type in ('landline', 'voip'):
        conn = db.get_db()
        conn.execute(
            "UPDATE touches SET status='cancelled', skipped_reason='landline' "
            "WHERE lead_id=? AND touch_type='sms' AND status IN ('planned','eligible')",
            (lead_id,)
        )
        conn.commit()
        conn.close()
    return line_type


def suppress_and_cancel(phone, reason, outcome, source='manual', their_message=''):
    """The single exit gate. Add a phone to the global DNC and cancel every pending
    touch for any ACTIVE lead with that number, marking those leads removed with the
    given outcome. Used by: SMS opt-out, Drop IVR DNC, called-in uploads, dead/blacklist.
    Returns {'leads': n, 'cancelled_touches': n}."""
    phone = db.normalize_phone(phone)
    if len(phone) != 10:
        return {'leads': 0, 'cancelled_touches': 0}
    db.add_to_dnc(phone, reason, source, their_message)

    conn = db.get_db()
    lead_ids = [r['id'] for r in conn.execute(
        "SELECT id FROM leads WHERE phone=? AND status IN ('enrolled','in_progress')", (phone,)
    ).fetchall()]
    cancelled = 0
    if lead_ids:
        ph = ','.join('?' for _ in lead_ids)
        cur = conn.execute(
            f"UPDATE touches SET status='cancelled', skipped_reason=? "
            f"WHERE lead_id IN ({ph}) AND status IN ('planned','eligible')",
            [reason] + lead_ids
        )
        cancelled = cur.rowcount
        conn.execute(
            f"UPDATE leads SET status='removed', outcome=?, removed_at=?, removed_reason=? "
            f"WHERE id IN ({ph})",
            [outcome, datetime.utcnow(), reason] + lead_ids
        )
    conn.commit()
    conn.close()
    return {'leads': len(lead_ids), 'cancelled_touches': cancelled}


def apply_delivery_status(message_sid, delivery_status, error_code=''):
    """Apply a Twilio status callback to a sequence SMS touch: record the delivery
    status/error on the touch, and if the number is permanently dead, suppress and
    cancel the lead (so it's never texted again). Returns the lead_id if matched."""
    if not message_sid:
        return None
    conn = db.get_db()
    row = conn.execute("SELECT lead_id FROM touches WHERE message_sid=?", (message_sid,)).fetchone()
    if not row:
        conn.close()
        return None
    lead_id = row['lead_id']
    conn.execute("UPDATE touches SET delivery_status=?, error_code=? WHERE message_sid=?",
                 (delivery_status, error_code, message_sid))
    conn.commit()
    conn.close()
    if str(error_code) in db.DEAD_NUMBER_CODES:
        apply_line_type(lead_id, 'dead')   # marks line dead + suppresses + cancels
    return lead_id


def mark_replied_and_stop(phone):
    """A non-opt-out inbound reply means the lead engaged — stop the drip and flag
    them for the reps. Does NOT add to DNC (they're a live lead, not an opt-out)."""
    phone = db.normalize_phone(phone)
    if len(phone) != 10:
        return {'leads': 0, 'cancelled_touches': 0}
    conn = db.get_db()
    lead_ids = [r['id'] for r in conn.execute(
        "SELECT id FROM leads WHERE phone=? AND status IN ('enrolled','in_progress')", (phone,)
    ).fetchall()]
    cancelled = 0
    if lead_ids:
        ph = ','.join('?' for _ in lead_ids)
        cur = conn.execute(
            f"UPDATE touches SET status='cancelled', skipped_reason='replied' "
            f"WHERE lead_id IN ({ph}) AND status IN ('planned','eligible')",
            lead_ids
        )
        cancelled = cur.rowcount
        conn.execute(
            f"UPDATE leads SET status='removed', outcome='replied', removed_at=?, removed_reason='replied' "
            f"WHERE id IN ({ph})",
            [datetime.utcnow()] + lead_ids
        )
    conn.commit()
    conn.close()
    return {'leads': len(lead_ids), 'cancelled_touches': cancelled}


# ── Daily cleanup + ledger (Phase 4) ──────────────────────────────────────────

def run_daily_cleanup(now=None):
    """End-of-day sweep (fires at 5:01 PM PT on weekdays):
      1. Cancel any un-fired touch whose time has passed (missed its window) so it
         never fires late — this is the 'advance regardless' rule.
      2. Complete any lead with no remaining touches (finalizing no_response for the
         unengaged); otherwise point current_run at the next pending run.
    Idempotent. Returns a summary."""
    now = now or datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    conn = db.get_db()

    cur = conn.execute(
        "UPDATE touches SET status='cancelled', skipped_reason='missed_window' "
        "WHERE status IN ('planned','eligible') AND eligible_at < ? "
        "AND lead_id IN (SELECT id FROM leads WHERE status IN ('enrolled','in_progress'))",
        (now,)
    )
    missed = cur.rowcount
    conn.commit()

    active = conn.execute("SELECT id FROM leads WHERE status IN ('enrolled','in_progress')").fetchall()
    completed = 0
    for row in active:
        lid = row['id']
        nxt = conn.execute(
            "SELECT MIN(run_number) m FROM touches WHERE lead_id=? AND status IN ('planned','eligible')",
            (lid,)
        ).fetchone()['m']
        if nxt is None:
            conn.execute(
                "UPDATE leads SET status='complete', completed_at=?, "
                "outcome=CASE WHEN outcome IS NULL OR outcome='' THEN 'no_response' ELSE outcome END "
                "WHERE id=?", (now, lid)
            )
            completed += 1
        else:
            conn.execute("UPDATE leads SET current_run=? WHERE id=?", (nxt, lid))
    conn.commit()
    conn.close()
    logger.info("[Cleanup] missed_cancelled=%s completed=%s", missed, completed)
    return {'missed_cancelled': missed, 'completed': completed}


def get_cohort_ledger(cohort_id):
    """Outcome breakdown, touch breakdown, and per-template A/B for a cohort."""
    conn = db.get_db()
    leads = conn.execute(
        "SELECT status, outcome, COUNT(*) c FROM leads WHERE cohort_id=? GROUP BY status, outcome",
        (cohort_id,)
    ).fetchall()
    touches = conn.execute(
        "SELECT touch_type, status, COUNT(*) c FROM touches WHERE cohort_id=? GROUP BY touch_type, status",
        (cohort_id,)
    ).fetchall()
    ab = conn.execute(
        "SELECT t.template_slot_id tid, "
        "  SUM(CASE WHEN t.status='sent' THEN 1 ELSE 0 END) sent, "
        "  SUM(CASE WHEN t.status='sent' AND l.outcome='opted_out_sms' THEN 1 ELSE 0 END) opted_out "
        "FROM touches t JOIN leads l ON l.id=t.lead_id "
        "WHERE t.cohort_id=? AND t.touch_type='sms' AND t.template_slot_id IS NOT NULL "
        "GROUP BY t.template_slot_id ORDER BY sent DESC",
        (cohort_id,)
    ).fetchall()
    conn.close()

    tmpl = {x['id']: x for x in get_templates()}
    ab_rows = []
    for r in ab:
        t = tmpl.get(r['tid'], {})
        ab_rows.append({
            'template_id': r['tid'], 'stage': t.get('stage', ''),
            'body': (t.get('body', '') or '')[:90],
            'sent': r['sent'] or 0, 'opted_out': r['opted_out'] or 0,
        })
    return {
        'leads':   [dict(r) for r in leads],
        'touches': [dict(r) for r in touches],
        'ab':      ab_rows,
    }


def get_cohort_nonresponders(cohort_id):
    """Leads who ran the full cycle without engaging — for the 2–3-week re-touch."""
    conn = db.get_db()
    rows = conn.execute(
        "SELECT first_name, last_name, phone, state, amount FROM leads "
        "WHERE cohort_id=? AND outcome='no_response' ORDER BY id", (cohort_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


_OUTCOME_LABEL = {
    'no_response':   'No response',
    'opted_out_sms': 'Opted out (text STOP)',
    'called_in':     'Called in',
    'dnc_ivr':       'Opted out (IVR)',
    'already_client': 'Already a client (called in)',
    'no_channel':    'No usable channel',
    'dead':          'Dead number',
    'blacklist':     'Blacklisted',
    'blocked':       'Manually blocked',
    'replied':       'Replied / engaged',
}


def outcome_label(status, outcome):
    """Human-readable bucket for the end-of-week report."""
    if outcome and outcome in _OUTCOME_LABEL:
        return _OUTCOME_LABEL[outcome]
    if status == 'complete':
        return 'No response'
    if status in ('enrolled', 'in_progress'):
        return 'In progress'
    return outcome or status


def get_cohort_outcomes_rows(cohort_id):
    """Every lead from the uploaded list with its final result — the annotated
    'original sheet' for the end-of-week hand-off."""
    conn = db.get_db()
    rows = conn.execute(
        "SELECT first_name, last_name, phone, state, amount, line_type, carrier, lane, "
        "timezone_bucket, status, outcome, enrolled_at, completed_at, removed_at "
        "FROM leads WHERE cohort_id=? ORDER BY id",
        (cohort_id,)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d['result'] = outcome_label(d['status'], d['outcome'])
        out.append(d)
    return out


# ── Activity tracking + command home (dashboard) ──────────────────────────────

# Friendly status buckets -> the WHERE predicate that selects them. `queued` are
# still-pending touches; everything else describes a touch that already fired.
_ACTIVITY_STATUS_SQL = {
    'sent':        "t.status='sent'",
    'delivered':   "t.delivery_status='delivered'",
    'undelivered': "t.delivery_status IN ('undelivered','failed')",
    'cancelled':   "t.status='cancelled'",
    'queued':      "t.status IN ('planned','eligible')",
}


def activity_row_label(row):
    """Short human status for one activity row (RVM has no carrier delivery)."""
    if row['status'] == 'cancelled':
        return ('skipped', row.get('skipped_reason') or 'cancelled')
    if row['status'] in ('planned', 'eligible'):
        return ('queued', 'waiting for its window')
    if row['touch_type'] == 'rvm':
        return ('dropped', 'voicemail delivered to carrier')
    ds = (row.get('delivery_status') or '').lower()
    if ds == 'delivered':
        return ('delivered', 'carrier confirmed delivery')
    if ds in ('undelivered', 'failed'):
        return ('undelivered', f"error {row.get('error_code') or '—'}")
    if row['status'] == 'sent':
        return ('sent', 'handed to carrier, awaiting receipt')
    return (row['status'], '')


def get_activity(touch_type=None, status=None, cohort_id=None, phone=None,
                 limit=100, offset=0):
    """Unified activity feed of every RVM + SMS touch — the tracking view.

    Newest actioned touches first. Resolves slot values so each row is
    self-describing (message body / audio label, agent, callback, delivery).
    Returns {'rows': [...], 'has_more': bool, 'counts': {...}}.
    """
    limit = max(1, min(500, int(limit)))
    offset = max(0, int(offset))

    filt, params = [], []
    if touch_type in ('rvm', 'sms'):
        filt.append('t.touch_type=?'); params.append(touch_type)
    if cohort_id:
        filt.append('t.cohort_id=?'); params.append(int(cohort_id))
    p = db.normalize_phone(phone) if phone else ''
    if p:
        filt.append('l.phone LIKE ?'); params.append('%' + p + '%')

    where = list(filt)
    where_params = list(params)
    clause = _ACTIVITY_STATUS_SQL.get(status)
    if clause:
        where.append(clause)
    else:
        # default: things that actually happened — a real drop or a cancellation,
        # not the thousands of still-planned future touches.
        where.append("t.status IN ('sent','cancelled')")
    wsql = (' WHERE ' + ' AND '.join(where)) if where else ''

    conn = db.get_db()
    rows = conn.execute(
        "SELECT t.*, l.first_name, l.last_name, l.phone, l.state, l.amount, "
        "l.custom_fields, l.line_type, c.name AS cohort_name "
        "FROM touches t JOIN leads l ON l.id=t.lead_id "
        "JOIN cohorts c ON c.id=t.cohort_id" + wsql +
        # Real sends (sent_at populated) lead, newest first; un-sent
        # (cancelled/queued) fall below, ordered by their planned time — so a
        # skip dated next week never sits above an actual drop from today.
        " ORDER BY COALESCE(t.sent_at, '') DESC, t.eligible_at DESC, t.id DESC "
        "LIMIT ? OFFSET ?", where_params + [limit + 1, offset]
    ).fetchall()

    # Filter-chip counts respect the type/cohort/phone filter but span every
    # status bucket, so the chips always add up to what's selectable.
    cwsql = (' WHERE ' + ' AND '.join(filt)) if filt else ''
    c = conn.execute(
        "SELECT "
        " COUNT(*) total, "
        " SUM(CASE WHEN t.status='sent' THEN 1 ELSE 0 END) sent, "
        " SUM(CASE WHEN t.delivery_status='delivered' THEN 1 ELSE 0 END) delivered, "
        " SUM(CASE WHEN t.delivery_status IN ('undelivered','failed') THEN 1 ELSE 0 END) undelivered, "
        " SUM(CASE WHEN t.status='cancelled' THEN 1 ELSE 0 END) cancelled, "
        " SUM(CASE WHEN t.status IN ('planned','eligible') THEN 1 ELSE 0 END) queued "
        "FROM touches t JOIN leads l ON l.id=t.lead_id" + cwsql, params
    ).fetchone()
    conn.close()

    has_more = len(rows) > limit
    rows = rows[:limit]

    tmpl = {t['id']: t for t in get_templates()}
    agents = {a['id']: a['name'] for a in get_agents()}
    callbacks = {cb['id']: cb['number'] for cb in get_callbacks()}
    audio = {a['id']: a for a in get_audio()}

    out = []
    for r in rows:
        r = dict(r)
        agent_name = agents.get(r['agent_slot_id'], '')
        callback_num = callbacks.get(r['callback_slot_id'], '')
        label, detail = activity_row_label(r)
        item = {
            'id': r['id'], 'lead_id': r['lead_id'], 'cohort_id': r['cohort_id'],
            'cohort_name': r['cohort_name'], 'type': r['touch_type'],
            'run': r['run_number'], 'step_in_day': r['step_in_day'], 'stage': r['stage'] or '',
            'status': r['status'], 'label': label, 'detail': detail,
            'delivery_status': r['delivery_status'] or '',
            'error_code': r['error_code'] or '',
            'skipped_reason': r['skipped_reason'] or '',
            'sending_number': r['sending_number'] or '',
            'first_name': r['first_name'], 'last_name': r['last_name'] or '',
            'phone': r['phone'], 'state': r['state'] or '', 'amount': r['amount'] or '',
            'line_type': r['line_type'] or 'unknown',
            'agent': agent_name,
            'callback': db.format_e164(callback_num) if callback_num else '',
            'when': _utc_str_to_pacific(r['sent_at'] or r['eligible_at']),
            'is_sent': bool(r['sent_at']),
        }
        if r['touch_type'] == 'sms':
            body = tmpl.get(r['template_slot_id'], {}).get('body', '')
            item['message'] = render_body(body, r, agent_name, callback_num)
        else:
            a = audio.get(r['audio_slot_id'])
            item['audio'] = a['label'] if a else ''
        out.append(item)

    counts = {k: (c[k] or 0) for k in ('total', 'sent', 'delivered', 'undelivered', 'cancelled', 'queued')}
    return {'rows': out, 'has_more': has_more, 'counts': counts}


def get_command_summary():
    """Live headline numbers for the Command home — leads, what's due, today's
    drops by channel, delivery, and the removed-lead outcome funnel."""
    conn = db.get_db()
    now_utc = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    day0 = _day_start_utc()

    leads = dict(conn.execute(
        "SELECT "
        " SUM(CASE WHEN status IN ('enrolled','in_progress') THEN 1 ELSE 0 END) active, "
        " SUM(CASE WHEN status='complete' THEN 1 ELSE 0 END) complete, "
        " SUM(CASE WHEN status='removed' THEN 1 ELSE 0 END) removed, "
        " COUNT(*) total FROM leads").fetchone())

    due = conn.execute(
        "SELECT touch_type, COUNT(*) c FROM touches "
        "WHERE status IN ('planned','eligible') AND eligible_at <= ? "
        "AND lead_id IN (SELECT id FROM leads WHERE status IN ('enrolled','in_progress')) "
        "GROUP BY touch_type", (now_utc,)).fetchall()
    due_map = {r['touch_type']: r['c'] for r in due}

    today = conn.execute(
        "SELECT touch_type, "
        " SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) sent, "
        " SUM(CASE WHEN delivery_status='delivered' THEN 1 ELSE 0 END) delivered, "
        " SUM(CASE WHEN delivery_status IN ('undelivered','failed') THEN 1 ELSE 0 END) undelivered "
        "FROM touches WHERE sent_at >= ? GROUP BY touch_type", (day0,)).fetchall()
    today_map = {r['touch_type']: dict(r) for r in today}

    outcomes = conn.execute(
        "SELECT outcome, COUNT(*) c FROM leads WHERE status='removed' AND outcome<>'' "
        "GROUP BY outcome ORDER BY c DESC").fetchall()
    cohorts_active = conn.execute(
        "SELECT COUNT(*) c FROM cohorts WHERE status='active'").fetchone()['c']
    conn.close()

    def _today(tt, key):
        return int((today_map.get(tt) or {}).get(key) or 0)

    return {
        'leads': {
            'active':   int(leads.get('active') or 0),
            'complete': int(leads.get('complete') or 0),
            'removed':  int(leads.get('removed') or 0),
            'total':    int(leads.get('total') or 0),
        },
        'due': {'rvm': int(due_map.get('rvm', 0)), 'sms': int(due_map.get('sms', 0))},
        'today': {
            'rvm_sent':     _today('rvm', 'sent'),
            'sms_sent':     _today('sms', 'sent'),
            'delivered':    _today('rvm', 'delivered') + _today('sms', 'delivered'),
            'undelivered':  _today('rvm', 'undelivered') + _today('sms', 'undelivered'),
        },
        'outcomes': [
            {'outcome': r['outcome'], 'label': outcome_label('removed', r['outcome']), 'count': r['c']}
            for r in outcomes
        ],
        'cohorts_active': cohorts_active,
    }
