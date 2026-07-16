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
from datetime import datetime, date, timedelta

import pytz

import database as db

logger = logging.getLogger(__name__)

PACIFIC = pytz.timezone('America/Los_Angeles')

# ── Sequence shape ────────────────────────────────────────────────────────────
RVM_RUNS = (1, 3, 5)          # RVM days
SMS_ONLY_RUNS = (2, 4)        # SMS-only days
TOTAL_RUNS = 5

# RVM morning anchor (Pacific), by timezone bucket
RVM_ANCHOR = {'ET': (7, 30), 'CT': (7, 30), 'MT': (8, 30), 'PT': (8, 30)}
# On RVM days, SMS is planned relative to the RVM anchor (re-stamped to the
# lead's ACTUAL RVM send time once it fires — see Phase 2).
RVM_DAY_SMS_OFFSETS_MIN = {1: 90, 2: 180}       # SMS#1 +1.5h, SMS#2 +3h
# SMS-only day start (Pacific), by bucket; SMS#2 is +3.5h after SMS#1
SMS_ONLY_START = {'ET': (8, 0), 'CT': (9, 0), 'MT': (10, 0), 'PT': (10, 0)}
SMS_ONLY_SMS2_OFFSET_MIN = 210                  # +3.5h

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


# ── Schema ────────────────────────────────────────────────────────────────────

def init_sequence_db():
    conn = db.get_db()
    conn.executescript('''
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
            line_type TEXT DEFAULT 'unknown',
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
            eligible_at TIMESTAMP,
            eligible_estimated INTEGER DEFAULT 0,
            status TEXT DEFAULT 'planned',
            sending_number TEXT DEFAULT '',
            message_sid TEXT DEFAULT '',
            drop_activity_token TEXT DEFAULT '',
            sent_at TIMESTAMP,
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
    ''')
    conn.commit()
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


def default_start_date(today=None):
    """Enrollment start: today if a weekday, else the next Monday."""
    d = today or date.today()
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

_STD_KEYS = {'phone', 'phone_number', 'phonenumber', 'telephone', 'mobile', 'cell',
             'first_name', 'firstname', 'first', 'last_name', 'lastname', 'last',
             'name', 'state', 'amount'}


def _extract(row):
    """Pull standard fields (flexible headers) + custom fields from a CSV row dict."""
    low = {k.strip().lower(): (v or '').strip() for k, v in row.items() if k}
    phone = ''
    for k in ('phone', 'phone_number', 'phonenumber', 'telephone', 'mobile', 'cell'):
        if low.get(k):
            phone = low[k]; break
    first = low.get('first_name') or low.get('firstname') or low.get('first') or low.get('name') or ''
    last = low.get('last_name') or low.get('lastname') or low.get('last') or ''
    state = low.get('state') or ''
    amount = low.get('amount') or ''
    custom = {k.strip(): (v or '').strip()
              for k, v in row.items()
              if k and k.strip().lower() not in _STD_KEYS and (v or '').strip()}
    return phone, first, last, state, amount, custom


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
    counts = {'loaded': 0, 'invalid': 0, 'dupes': 0, 'suppressed': 0, 'already_active': 0}
    lead_i = 0

    for row in rows:
        phone_raw, first, last, state, amount, custom = _extract(row)
        if not phone_raw:
            continue
        phone = db.normalize_phone(phone_raw)
        if len(phone) != 10:
            counts['invalid'] += 1
            continue
        if phone in seen:
            counts['dupes'] += 1
            continue
        seen.add(phone)
        if db.is_dnc(phone):
            counts['suppressed'] += 1
            continue
        if _phone_active_elsewhere(phone):
            counts['already_active'] += 1
            continue

        bucket = timezone_bucket_for_state(state)
        callback_slot = callbacks[lead_i % len(callbacks)] if callbacks else None
        _create_lead_and_plan(cohort_id, first, last, phone, state, amount, custom,
                              bucket, callback_slot, run_dates, agents, audio_pool,
                              stage_templates, lead_i)
        counts['loaded'] += 1
        lead_i += 1

    conn = db.get_db()
    conn.execute('UPDATE cohorts SET size=? WHERE id=?', (counts['loaded'], cohort_id))
    conn.commit()
    conn.close()

    counts['cohort_id'] = cohort_id
    logger.info("[Cohort %s '%s'] enrolled=%s dupes=%s invalid=%s suppressed=%s active_elsewhere=%s",
                cohort_id, name, counts['loaded'], counts['dupes'], counts['invalid'],
                counts['suppressed'], counts['already_active'])
    return counts


def _phone_active_elsewhere(phone):
    conn = db.get_db()
    row = conn.execute(
        "SELECT 1 FROM leads WHERE phone=? AND status IN ('enrolled','in_progress') LIMIT 1",
        (phone,)
    ).fetchone()
    conn.close()
    return row is not None


def _create_lead_and_plan(cohort_id, first, last, phone, state, amount, custom,
                          bucket, callback_slot, run_dates, agents, audio_pool,
                          stage_templates, lead_i):
    conn = db.get_db()
    cur = conn.execute(
        'INSERT INTO leads (cohort_id, first_name, last_name, phone, state, amount, '
        'custom_fields, timezone_bucket, callback_slot_id) VALUES (?,?,?,?,?,?,?,?,?)',
        (cohort_id, first, last, phone, state, amount, json.dumps(custom), bucket, callback_slot)
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

    touch_rows = []
    seq = 0
    for run in range(1, TOTAL_RUNS + 1):
        run_date = run_dates[run - 1]
        for (tt, step, eligible, estimated) in plan_touch_times(bucket, run_date, run):
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
                touch_rows.append((
                    lead_id, cohort_id, run, 'sms', step, seq, stg,
                    tmpl, agent_by_run.get(run), callback_slot,
                    None, eligible, 1 if estimated else 0, 'planned'
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
    dt = datetime.strptime(utc_str, '%Y-%m-%d %H:%M:%S')
    pac = pytz.utc.localize(dt).astimezone(PACIFIC)
    return pac.strftime('%a %Y-%m-%d %I:%M %p %Z')


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
        'line_type, status, outcome, current_run FROM leads WHERE cohort_id=? '
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
