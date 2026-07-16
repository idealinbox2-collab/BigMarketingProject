import sqlite3
import os
import time
from datetime import datetime, date

DB_PATH = os.environ.get('DB_PATH', os.path.abspath(os.path.join(os.path.dirname(os.path.realpath(__file__)), 'sms_dashboard.db')))

# ── Backend: SQLite (default, local/dev) or Postgres via DATABASE_URL (prod) ──
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
IS_PG = DATABASE_URL.lower().startswith(('postgres://', 'postgresql://'))

if IS_PG:
    import psycopg


def portable_schema(sql):
    """Translate a CREATE-TABLE schema across backends (the id primary-key type)."""
    return sql.replace('INTEGER PRIMARY KEY AUTOINCREMENT', 'SERIAL PRIMARY KEY') if IS_PG else sql


def sql_date(expr):
    """Dialect-aware expression yielding a 'YYYY-MM-DD' text date part."""
    return f"to_char({expr}, 'YYYY-MM-DD')" if IS_PG else f"DATE({expr})"


def sql_now_minus_days(days):
    """Dialect-aware 'N days ago' literal expression."""
    days = int(days)
    return f"(now() - interval '{days} days')" if IS_PG else f"datetime('now', '-{days} days')"


# ── Postgres adapter: a thin wrapper giving sqlite3-compatible call semantics ──
if IS_PG:
    def _translate(sql):
        # our SQL uses '?' placeholders and contains no literal '%'; psycopg wants '%s'
        return sql.replace('?', '%s')

    class _Row:
        """Row supporting int and str indexing, plus dict(row) via the mapping protocol."""
        __slots__ = ('_cols', '_vals', '_map')

        def __init__(self, cols, vals):
            self._cols = cols
            self._vals = vals
            self._map = dict(zip(cols, vals))

        def __getitem__(self, k):
            return self._vals[k] if isinstance(k, int) else self._map[k]

        def keys(self):
            return list(self._cols)

        def get(self, k, default=None):
            return self._map.get(k, default)

    def _row_factory(cursor):
        cols = [c.name for c in cursor.description] if cursor.description else []

        def make(values):
            return _Row(cols, values)
        return make

    class _PgCur:
        def __init__(self, cur, conn):
            self._cur = cur
            self._conn = conn

        def fetchone(self):
            return self._cur.fetchone()

        def fetchall(self):
            return self._cur.fetchall()

        @property
        def rowcount(self):
            return self._cur.rowcount

        @property
        def lastrowid(self):
            c = self._conn.cursor()
            c.execute('SELECT lastval()')
            return c.fetchone()[0]

    class _PgConn:
        """Mimics the subset of sqlite3.Connection this app uses."""

        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=()):
            if sql.strip().upper().startswith('BEGIN'):
                return _PgCur(self._conn.cursor(), self._conn)   # psycopg manages the txn
            cur = self._conn.cursor(row_factory=_row_factory)
            cur.execute(_translate(sql), tuple(params) if params else ())
            return _PgCur(cur, self._conn)

        def executemany(self, sql, seq):
            cur = self._conn.cursor()
            cur.executemany(_translate(sql), [tuple(x) for x in seq])
            return _PgCur(cur, self._conn)

        def executescript(self, script):
            cur = self._conn.cursor()
            for stmt in script.split(';'):
                if stmt.strip():
                    cur.execute(stmt)
            self._conn.commit()   # DDL is self-contained, like sqlite3.executescript

        def commit(self):
            self._conn.commit()

        def rollback(self):
            self._conn.rollback()

        def close(self):
            self._conn.close()


def get_db():
    if IS_PG:
        return _PgConn(psycopg.connect(DATABASE_URL))
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA busy_timeout=10000')  # wait up to 10s on lock
    return conn


def _exec_with_retry(fn, retries=5, base_delay=0.2):
    """Execute a DB operation with exponential backoff on lock errors."""
    for attempt in range(retries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if 'database is locked' in str(e) and attempt < retries - 1:
                time.sleep(base_delay * (2 ** attempt))
                continue
            raise


def init_db():
    conn = get_db()
    conn.executescript(portable_schema('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS twilio_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            account_sid TEXT NOT NULL,
            auth_token TEXT NOT NULL,
            brand TEXT DEFAULT '',
            daily_limit INTEGER DEFAULT 10000,
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS sending_numbers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            phone_number TEXT NOT NULL UNIQUE,
            friendly_name TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            total_sent INTEGER DEFAULT 0,
            delivered_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            warmup_mode INTEGER DEFAULT 0,
            daily_limit INTEGER DEFAULT 500,
            daily_sent INTEGER DEFAULT 0,
            daily_sent_date TEXT DEFAULT '',
            warmup_start_date TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (account_id) REFERENCES twilio_accounts(id)
        );

        CREATE TABLE IF NOT EXISTS dnc_list (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL UNIQUE,
            reason TEXT DEFAULT 'manual',
            source TEXT DEFAULT 'manual',
            their_message TEXT DEFAULT '',
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS inbound_replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_phone TEXT NOT NULL,
            to_number TEXT DEFAULT '',
            body TEXT NOT NULL,
            reply_type TEXT DEFAULT 'other',
            received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            message_body TEXT NOT NULL,
            message_type TEXT DEFAULT 'sms',
            media_filename TEXT,
            brand TEXT DEFAULT '',
            agent_names TEXT DEFAULT '',
            callback_numbers TEXT DEFAULT '',
            message_variants TEXT DEFAULT '',
            status TEXT DEFAULT 'draft',
            total_contacts INTEGER DEFAULT 0,
            sent_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            dnc_skipped_count INTEGER DEFAULT 0,
            delivered_count INTEGER DEFAULT 0,
            undelivered_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            completed_at TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            phone TEXT NOT NULL,
            first_name TEXT DEFAULT '',
            custom_fields TEXT DEFAULT '{}',
            status TEXT DEFAULT 'pending',
            delivery_status TEXT DEFAULT '',
            error_code TEXT DEFAULT '',
            message_sid TEXT DEFAULT '',
            sending_number TEXT,
            sent_at TIMESTAMP,
            error_message TEXT,
            variant_sent TEXT DEFAULT '',
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
        );

        CREATE TABLE IF NOT EXISTS contact_brands (
            phone TEXT NOT NULL,
            brand TEXT NOT NULL,
            first_sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (phone, brand)
        );

        CREATE TABLE IF NOT EXISTS number_history (
            phone TEXT PRIMARY KEY,
            last_sending_number TEXT NOT NULL,
            last_used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS delivery_stats (
            message_sid TEXT PRIMARY KEY,
            campaign_id INTEGER NOT NULL,
            contact_phone TEXT NOT NULL,
            sending_number TEXT NOT NULL,
            delivery_status TEXT DEFAULT 'sent',
            error_code TEXT DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS scrub_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            status TEXT DEFAULT 'running',
            total INTEGER DEFAULT 0,
            processed INTEGER DEFAULT 0,
            mobile_count INTEGER DEFAULT 0,
            landline_count INTEGER DEFAULT 0,
            dead_count INTEGER DEFAULT 0,
            error_count INTEGER DEFAULT 0,
            estimated_cost REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS campaign_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            event TEXT NOT NULL,
            sent_snapshot INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
        );
    '''))

    # ── Safe migrations for existing DBs ─────────────────────────────────────
    migrations = [
        # Previous migrations
        'ALTER TABLE dnc_list ADD COLUMN their_message TEXT DEFAULT ""',
        'ALTER TABLE contacts ADD COLUMN delivery_status TEXT DEFAULT ""',
        'ALTER TABLE contacts ADD COLUMN error_code TEXT DEFAULT ""',
        'ALTER TABLE contacts ADD COLUMN message_sid TEXT DEFAULT ""',
        'ALTER TABLE campaigns ADD COLUMN delivered_count INTEGER DEFAULT 0',
        'ALTER TABLE campaigns ADD COLUMN undelivered_count INTEGER DEFAULT 0',
        'ALTER TABLE sending_numbers ADD COLUMN delivered_count INTEGER DEFAULT 0',
        'ALTER TABLE sending_numbers ADD COLUMN failed_count INTEGER DEFAULT 0',
        # New migrations
        'ALTER TABLE twilio_accounts ADD COLUMN brand TEXT DEFAULT ""',
        'ALTER TABLE twilio_accounts ADD COLUMN daily_limit INTEGER DEFAULT 10000',
        'ALTER TABLE campaigns ADD COLUMN brand TEXT DEFAULT ""',
        'ALTER TABLE sending_numbers ADD COLUMN warmup_mode INTEGER DEFAULT 0',
        'ALTER TABLE sending_numbers ADD COLUMN daily_limit INTEGER DEFAULT 500',
        'ALTER TABLE sending_numbers ADD COLUMN daily_sent INTEGER DEFAULT 0',
        'ALTER TABLE sending_numbers ADD COLUMN daily_sent_date TEXT DEFAULT ""',
        'ALTER TABLE contacts ADD COLUMN custom_fields TEXT DEFAULT "{}"',
        'ALTER TABLE sending_numbers ADD COLUMN warmup_start_date TEXT DEFAULT ""',
        # Rotation columns
        'ALTER TABLE campaigns ADD COLUMN agent_names TEXT DEFAULT ""',
        'ALTER TABLE campaigns ADD COLUMN callback_numbers TEXT DEFAULT ""',
        'ALTER TABLE campaigns ADD COLUMN message_variants TEXT DEFAULT ""',
        # Per-contact variant tracking
        'ALTER TABLE contacts ADD COLUMN variant_sent TEXT DEFAULT ""',
        # Per-subaccount auto-provisioning config (Studio Flow SID + inbound reply webhook)
        'ALTER TABLE twilio_accounts ADD COLUMN voice_flow_sid TEXT DEFAULT ""',
        'ALTER TABLE twilio_accounts ADD COLUMN inbound_webhook_url TEXT DEFAULT ""',
    ]
    for m in migrations:
        try:
            conn.execute(m)
            conn.commit()
        except Exception:
            conn.rollback()   # column already exists — Postgres aborts the txn, so reset

    conn.commit()
    conn.close()


# ── Helpers ──────────────────────────────────────────────────────────────────

def normalize_phone(phone):
    if not phone:
        return ''
    digits = ''.join(filter(str.isdigit, str(phone)))
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    return digits


def format_e164(phone):
    return f'+1{normalize_phone(phone)}'


# ── Settings ─────────────────────────────────────────────────────────────────

def get_setting(key, default=None):
    conn = get_db()
    row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    conn.close()
    return row['value'] if row else default


def set_setting(key, value):
    conn = get_db()
    conn.execute('INSERT INTO settings (key,value) VALUES (?,?) ON CONFLICT (key) DO UPDATE SET value=excluded.value', (key, value))
    conn.commit()
    conn.close()


def get_send_schedule():
    return {
        'start_hour': int(get_setting('send_start_hour', '8')),
        'end_hour':   int(get_setting('send_end_hour', '20')),
        'send_days':  [int(d) for d in get_setting('send_days', '0,1,2,3,4,5,6').split(',') if d.strip()],
        'timezone':   get_setting('send_timezone', 'America/Los_Angeles'),
    }


# ── DNC ──────────────────────────────────────────────────────────────────────

def is_dnc(phone):
    phone = normalize_phone(phone)
    conn = get_db()
    row = conn.execute('SELECT id FROM dnc_list WHERE phone=?', (phone,)).fetchone()
    conn.close()
    return row is not None


def add_to_dnc(phone, reason='manual', source='manual', their_message=''):
    phone = normalize_phone(phone)
    if not phone:
        return
    conn = get_db()
    conn.execute(
        'INSERT INTO dnc_list (phone,reason,source,their_message) VALUES (?,?,?,?) ON CONFLICT (phone) DO NOTHING',
        (phone, reason, source, their_message)
    )
    conn.commit()
    conn.close()


def bulk_add_to_dnc(phones, reason='manual', source='manual'):
    valid = []
    seen  = set()
    for p in phones:
        n = normalize_phone(p)
        if len(n) == 10 and n not in seen:
            seen.add(n)
            valid.append((n, reason, source, ''))
    if not valid:
        return 0
    conn = get_db()
    before = conn.execute('SELECT COUNT(*) FROM dnc_list').fetchone()[0]
    conn.executemany(
        'INSERT INTO dnc_list (phone,reason,source,their_message) VALUES (?,?,?,?) ON CONFLICT (phone) DO NOTHING',
        valid
    )
    conn.commit()
    after = conn.execute('SELECT COUNT(*) FROM dnc_list').fetchone()[0]
    conn.close()
    return after - before


def remove_from_dnc(phone):
    phone = normalize_phone(phone)
    conn = get_db()
    conn.execute('DELETE FROM dnc_list WHERE phone=?', (phone,))
    conn.commit()
    conn.close()


def get_dnc_list(search='', limit=None, offset=0):
    conn = get_db()
    params = []
    where = ''
    if search:
        where = 'WHERE phone LIKE ? OR reason LIKE ?'
        params += [f'%{search}%', f'%{search}%']
    sql = f'SELECT * FROM dnc_list {where} ORDER BY added_at DESC'
    if limit is not None:
        sql += ' LIMIT ? OFFSET ?'
        params += [int(limit), int(offset)]
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def backfill_dead_numbers_to_dnc():
    """
    One-time sweep: add every phone that previously got a permanently-dead
    delivery status into the DNC list. Lets re-uploaded old lists skip numbers
    that were already known dead before auto-suppression existed. Idempotent —
    safe to run repeatedly. Returns how many were newly added.
    """
    conn = get_db()
    placeholders = ','.join('?' for _ in DEAD_NUMBER_CODES)
    rows = conn.execute(
        f"SELECT DISTINCT contact_phone FROM delivery_stats "
        f"WHERE error_code IN ({placeholders})",
        tuple(DEAD_NUMBER_CODES)
    ).fetchall()
    before = conn.execute('SELECT COUNT(*) FROM dnc_list').fetchone()[0]
    for r in rows:
        phone = normalize_phone(r['contact_phone'])
        if phone:
            conn.execute(
                'INSERT INTO dnc_list (phone,reason,source) VALUES (?,?,?) ON CONFLICT (phone) DO NOTHING',
                (phone, 'dead_number', 'auto')
            )
    conn.commit()
    after = conn.execute('SELECT COUNT(*) FROM dnc_list').fetchone()[0]
    conn.close()
    return after - before


def get_dnc_count(search=''):
    """Fast COUNT(*) for the DNC list — never loads rows into memory."""
    conn = get_db()
    if search:
        n = conn.execute(
            'SELECT COUNT(*) FROM dnc_list WHERE phone LIKE ? OR reason LIKE ?',
            (f'%{search}%', f'%{search}%')
        ).fetchone()[0]
    else:
        n = conn.execute('SELECT COUNT(*) FROM dnc_list').fetchone()[0]
    conn.close()
    return n


# ── Brand Tracking ────────────────────────────────────────────────────────────

def has_brand_conflict(phone, brand):
    """
    Returns True if this phone number has already been contacted by a DIFFERENT brand.
    A blank brand never conflicts.
    """
    if not brand:
        return False
    phone = normalize_phone(phone)
    conn = get_db()
    row = conn.execute(
        'SELECT brand FROM contact_brands WHERE phone=? AND brand != ?',
        (phone, brand)
    ).fetchone()
    conn.close()
    return row is not None


def record_brand_contact(phone, brand):
    """Record that this phone was contacted by this brand."""
    if not brand:
        return
    phone = normalize_phone(phone)
    conn = get_db()
    conn.execute(
        'INSERT INTO contact_brands (phone,brand) VALUES (?,?) ON CONFLICT (phone,brand) DO NOTHING',
        (phone, brand)
    )
    conn.commit()
    conn.close()


# ── Inbound Replies ───────────────────────────────────────────────────────────

def log_inbound_reply(from_phone, to_number, body, reply_type):
    from_phone = normalize_phone(from_phone)
    def _do():
        conn = get_db()
        conn.execute(
            'INSERT INTO inbound_replies (from_phone,to_number,body,reply_type) VALUES (?,?,?,?)',
            (from_phone, to_number, body, reply_type)
        )
        conn.commit()
        conn.close()
    _exec_with_retry(_do)


def get_inbound_replies(reply_type=None, days=None):
    conn = get_db()
    query = 'SELECT * FROM inbound_replies'
    params = []
    where = []

    if reply_type == 'non_optout':
        where.append("reply_type != 'opted_out'")
    elif reply_type:
        where.append('reply_type=?')
        params.append(reply_type)

    if days:
        where.append(f"received_at >= {sql_now_minus_days(days)}")
    if where:
        query += ' WHERE ' + ' AND '.join(where)
    query += ' ORDER BY received_at DESC'
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Twilio Accounts ───────────────────────────────────────────────────────────

def get_accounts():
    conn = get_db()
    rows = conn.execute('SELECT * FROM twilio_accounts WHERE active=1 ORDER BY id').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_account_by_id(account_id):
    """Full account row (incl. credentials + provisioning config) for one sub."""
    conn = get_db()
    row = conn.execute('SELECT * FROM twilio_accounts WHERE id=?', (account_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_account_provisioning(account_id, voice_flow_sid, inbound_webhook_url):
    """Set the per-sub auto-buy config: Studio Flow SID + inbound reply webhook."""
    conn = get_db()
    conn.execute(
        'UPDATE twilio_accounts SET voice_flow_sid=?, inbound_webhook_url=? WHERE id=?',
        ((voice_flow_sid or '').strip(), (inbound_webhook_url or '').strip(), account_id)
    )
    conn.commit()
    conn.close()


def get_brands():
    """Returns list of distinct non-empty brands across accounts."""
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT brand FROM twilio_accounts WHERE active=1 AND brand != '' ORDER BY brand"
    ).fetchall()
    conn.close()
    return [r['brand'] for r in rows]


def add_account(name, account_sid, auth_token, brand='', daily_limit=10000):
    conn = get_db()
    conn.execute(
        'INSERT INTO twilio_accounts (name,account_sid,auth_token,brand,daily_limit) VALUES (?,?,?,?,?)',
        (name, account_sid, auth_token, brand.strip(), daily_limit)
    )
    conn.commit()
    conn.close()


def update_account_brand(account_id, brand, daily_limit=10000):
    conn = get_db()
    conn.execute(
        'UPDATE twilio_accounts SET brand=?, daily_limit=? WHERE id=?',
        (brand.strip(), daily_limit, account_id)
    )
    conn.commit()
    conn.close()


def delete_account(account_id):
    conn = get_db()
    conn.execute('UPDATE twilio_accounts SET active=0 WHERE id=?', (account_id,))
    conn.commit()
    conn.close()


# ── Sending Numbers ───────────────────────────────────────────────────────────

def _reset_daily_if_needed(conn, number_id, today_str):
    """Reset daily_sent if the stored date is not today. Called within existing conn."""
    conn.execute(
        "UPDATE sending_numbers SET daily_sent=0, daily_sent_date=? WHERE id=? AND daily_sent_date != ?",
        (today_str, number_id, today_str)
    )


def get_active_numbers(brand=None):
    """Get active numbers. If brand given, only return numbers from accounts with that brand."""
    conn = get_db()
    today = date.today().isoformat()

    if brand:
        rows = conn.execute('''
            SELECT sn.*, ta.account_sid, ta.auth_token, ta.brand, ta.daily_limit as account_daily_limit
            FROM sending_numbers sn
            JOIN twilio_accounts ta ON sn.account_id = ta.id
            WHERE sn.active=1 AND ta.active=1 AND ta.brand=?
            ORDER BY sn.total_sent ASC
        ''', (brand,)).fetchall()
    else:
        rows = conn.execute('''
            SELECT sn.*, ta.account_sid, ta.auth_token, ta.brand, ta.daily_limit as account_daily_limit
            FROM sending_numbers sn
            JOIN twilio_accounts ta ON sn.account_id = ta.id
            WHERE sn.active=1 AND ta.active=1
            ORDER BY sn.total_sent ASC
        ''').fetchall()

    # Reset daily counts for any number not updated today
    for row in rows:
        _reset_daily_if_needed(conn, row['id'], today)
    conn.commit()
    conn.close()

    # Re-fetch after reset
    conn = get_db()
    if brand:
        rows = conn.execute('''
            SELECT sn.*, ta.account_sid, ta.auth_token, ta.brand, ta.daily_limit as account_daily_limit
            FROM sending_numbers sn
            JOIN twilio_accounts ta ON sn.account_id = ta.id
            WHERE sn.active=1 AND ta.active=1 AND ta.brand=?
            ORDER BY sn.total_sent ASC
        ''', (brand,)).fetchall()
    else:
        rows = conn.execute('''
            SELECT sn.*, ta.account_sid, ta.auth_token, ta.brand, ta.daily_limit as account_daily_limit
            FROM sending_numbers sn
            JOIN twilio_accounts ta ON sn.account_id = ta.id
            WHERE sn.active=1 AND ta.active=1
            ORDER BY sn.total_sent ASC
        ''').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_available_numbers_for_send(brand=None):
    """
    Returns active numbers that haven't hit their daily limit.
    For warmup numbers: only if daily_sent < daily_limit.
    For non-warmup numbers: always available.
    """
    all_numbers = get_active_numbers(brand=brand)
    available = []
    for n in all_numbers:
        if n.get('warmup_mode'):
            limit = n.get('daily_limit') or 500
            sent  = n.get('daily_sent') or 0
            if sent < limit:
                available.append(n)
        else:
            available.append(n)
    return available


def get_sending_numbers_list():
    conn = get_db()
    rows = conn.execute('''
        SELECT sn.*, ta.name as account_name, ta.brand as account_brand
        FROM sending_numbers sn
        JOIN twilio_accounts ta ON sn.account_id = ta.id
        ORDER BY sn.active DESC, sn.total_sent ASC
    ''').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_sending_number(account_id, phone_number, friendly_name='', warmup_mode=0, daily_limit=500):
    phone_number = normalize_phone(phone_number)
    # Set warmup_start_date to today if warmup_mode is on
    warmup_start = date.today().isoformat() if warmup_mode else ''
    conn = get_db()
    conn.execute(
        'INSERT INTO sending_numbers (account_id,phone_number,friendly_name,warmup_mode,daily_limit,warmup_start_date) VALUES (?,?,?,?,?,?) ON CONFLICT (phone_number) DO NOTHING',
        (account_id, phone_number, friendly_name, int(warmup_mode), daily_limit, warmup_start)
    )
    conn.commit()
    conn.close()


def update_number_warmup(number_id, warmup_mode, daily_limit):
    conn = get_db()
    if warmup_mode:
        # Only set start date if switching warmup ON and no start date yet
        row = conn.execute('SELECT warmup_start_date, warmup_mode FROM sending_numbers WHERE id=?', (number_id,)).fetchone()
        if row and not row['warmup_mode']:
            # Was off, now turning on — set start date to today
            conn.execute(
                'UPDATE sending_numbers SET warmup_mode=?, daily_limit=?, warmup_start_date=? WHERE id=?',
                (1, int(daily_limit), date.today().isoformat(), number_id)
            )
        else:
            conn.execute(
                'UPDATE sending_numbers SET warmup_mode=?, daily_limit=? WHERE id=?',
                (1, int(daily_limit), number_id)
            )
    else:
        conn.execute(
            'UPDATE sending_numbers SET warmup_mode=0, daily_limit=? WHERE id=?',
            (int(daily_limit), number_id)
        )
    conn.commit()
    conn.close()


def deactivate_number(number_id):
    conn = get_db()
    conn.execute('UPDATE sending_numbers SET active=0 WHERE id=?', (number_id,))
    conn.commit()
    conn.close()


def reactivate_number(number_id):
    conn = get_db()
    conn.execute('UPDATE sending_numbers SET active=1 WHERE id=?', (number_id,))
    conn.commit()
    conn.close()


def update_number_delivery_stats(phone_number, delivered=False):
    conn = get_db()
    if delivered:
        conn.execute(
            'UPDATE sending_numbers SET delivered_count=delivered_count+1 WHERE phone_number=?',
            (phone_number,)
        )
    else:
        conn.execute(
            'UPDATE sending_numbers SET failed_count=failed_count+1 WHERE phone_number=?',
            (phone_number,)
        )
    conn.commit()
    conn.close()


# ── Account Daily Usage ───────────────────────────────────────────────────────

def get_account_daily_usage(account_id):
    """Returns (daily_sent_today, daily_limit) for a sub-account."""
    today = date.today().isoformat()
    conn = get_db()
    # Sum daily_sent across all numbers in this account (reset if not today)
    rows = conn.execute(
        "SELECT daily_sent, daily_sent_date FROM sending_numbers WHERE account_id=? AND active=1",
        (account_id,)
    ).fetchall()
    conn.close()
    total = sum(r['daily_sent'] for r in rows if r['daily_sent_date'] == today)
    acc = get_accounts()
    limit = next((a['daily_limit'] for a in acc if a['id'] == account_id), 10000)
    return total, limit


# ── Number Rotation History ───────────────────────────────────────────────────

def get_last_sending_number(phone):
    phone = normalize_phone(phone)
    conn = get_db()
    row = conn.execute('SELECT last_sending_number FROM number_history WHERE phone=?', (phone,)).fetchone()
    conn.close()
    return row['last_sending_number'] if row else None


def update_number_history(contact_phone, sending_number):
    contact_phone = normalize_phone(contact_phone)
    conn = get_db()
    conn.execute(
        'INSERT INTO number_history (phone,last_sending_number,last_used_at) VALUES (?,?,?) ON CONFLICT (phone) DO UPDATE SET last_sending_number=excluded.last_sending_number, last_used_at=excluded.last_used_at',
        (contact_phone, sending_number, datetime.utcnow())
    )
    conn.commit()
    conn.close()


# ── Tracking / Reporting (READ-ONLY — never touches the send path) ─────────────

def backfill_contact_delivery_status():
    """
    One-time repair: sync contacts.delivery_status/error_code from delivery_stats,
    which holds the CORRECT terminal status for each message. Fixes historical
    reports that were frozen at 'sent' due to the update_delivery_status bug.
    Idempotent. Returns how many contact rows were corrected.
    """
    conn = get_db()
    cur = conn.execute('''
        UPDATE contacts
        SET delivery_status = (
                SELECT ds.delivery_status FROM delivery_stats ds
                WHERE ds.message_sid = contacts.message_sid
            ),
            error_code = (
                SELECT ds.error_code FROM delivery_stats ds
                WHERE ds.message_sid = contacts.message_sid
            )
        WHERE message_sid IS NOT NULL AND message_sid != ''
          AND EXISTS (
                SELECT 1 FROM delivery_stats ds
                WHERE ds.message_sid = contacts.message_sid
                  AND ds.delivery_status IS NOT NULL
                  AND ds.delivery_status != contacts.delivery_status
          )
    ''')
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n


def get_campaign_opted_out_phones(campaign_id):
    """Set of phones in this campaign that replied with an opt-out (STOP)."""
    conn = get_db()
    rows = conn.execute('''
        SELECT DISTINCT ct.phone
        FROM contacts ct
        JOIN inbound_replies ir ON ir.from_phone = ct.phone
        WHERE ct.campaign_id = ? AND ir.reply_type = 'opted_out'
    ''', (campaign_id,)).fetchall()
    conn.close()
    return {r['phone'] for r in rows}


def get_campaign_tracking(campaign_id):
    """Full tracking snapshot for one campaign: headline stats, live delivery
    breakdown, error-code table, and per-variant send counts. Read-only."""
    conn = get_db()
    camp = conn.execute('SELECT * FROM campaigns WHERE id=?', (campaign_id,)).fetchone()
    if not camp:
        conn.close()
        return None
    camp = dict(camp)

    # Live delivery breakdown from the contacts table (source of truth)
    total   = conn.execute('SELECT COUNT(*) FROM contacts WHERE campaign_id=?', (campaign_id,)).fetchone()[0]
    sent    = conn.execute("SELECT COUNT(*) FROM contacts WHERE campaign_id=? AND sent_at IS NOT NULL", (campaign_id,)).fetchone()[0]
    delivered = conn.execute("SELECT COUNT(*) FROM contacts WHERE campaign_id=? AND delivery_status='delivered'", (campaign_id,)).fetchone()[0]
    undelivered = conn.execute("SELECT COUNT(*) FROM contacts WHERE campaign_id=? AND delivery_status='undelivered'", (campaign_id,)).fetchone()[0]
    failed  = conn.execute("SELECT COUNT(*) FROM contacts WHERE campaign_id=? AND delivery_status='failed'", (campaign_id,)).fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM contacts WHERE campaign_id=? AND status='pending'", (campaign_id,)).fetchone()[0]
    dnc_skipped = conn.execute("SELECT COUNT(*) FROM contacts WHERE campaign_id=? AND status='dnc_skipped'", (campaign_id,)).fetchone()[0]

    # Opt-outs (STOP): distinct texted contacts in this campaign who replied opt-out
    opted_out = conn.execute('''
        SELECT COUNT(DISTINCT ct.phone) FROM contacts ct
        JOIN inbound_replies ir ON ir.from_phone = ct.phone
        WHERE ct.campaign_id = ? AND ir.reply_type = 'opted_out'
    ''', (campaign_id,)).fetchone()[0]

    # Error-code breakdown (only sent messages with an error code)
    err_rows = conn.execute(
        "SELECT error_code, COUNT(*) AS cnt FROM contacts "
        "WHERE campaign_id=? AND error_code IS NOT NULL AND error_code != '' "
        "GROUP BY error_code ORDER BY cnt DESC", (campaign_id,)
    ).fetchall()
    errors = [{'code': r['error_code'], 'count': r['cnt']} for r in err_rows]

    # Per-variant send counts (what was actually sent to whom)
    var_rows = conn.execute(
        "SELECT variant_sent, COUNT(*) AS cnt FROM contacts "
        "WHERE campaign_id=? AND sent_at IS NOT NULL "
        "GROUP BY variant_sent ORDER BY cnt DESC", (campaign_id,)
    ).fetchall()
    variants = [{'text': (r['variant_sent'] or '(base message)'), 'count': r['cnt']} for r in var_rows]

    conn.close()
    return {
        'campaign': camp,
        'stats': {
            'total': total, 'sent': sent, 'delivered': delivered,
            'undelivered': undelivered, 'failed': failed, 'pending': pending,
            'dnc_skipped': dnc_skipped, 'errors_total': sum(e['count'] for e in errors),
            'opted_out': opted_out,
        },
        'errors': errors,
        'variants': variants,
    }


def get_campaign_texted_rows(campaign_id):
    """Every recipient that was actually texted (sent_at set), for CSV export.
    Includes an 'opted_out' column ('STOP' if they replied opt-out)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT ct.phone, ct.first_name, ct.sending_number, ct.variant_sent, "
        "ct.delivery_status, ct.error_code, ct.sent_at, "
        "CASE WHEN EXISTS (SELECT 1 FROM inbound_replies ir "
        "  WHERE ir.from_phone = ct.phone AND ir.reply_type='opted_out') "
        "  THEN 'STOP' ELSE '' END AS opted_out "
        "FROM contacts ct "
        "WHERE ct.campaign_id=? AND ct.sent_at IS NOT NULL ORDER BY ct.sent_at ASC",
        (campaign_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_texted_rows_by_range(start_date, end_date):
    """All texted recipients across every campaign in a date range (inclusive),
    tagged with campaign name — for the day/week/month rollup export."""
    conn = get_db()
    rows = conn.execute(
        "SELECT c.name AS campaign_name, ct.phone, ct.first_name, ct.sending_number, "
        "ct.variant_sent, ct.delivery_status, ct.error_code, ct.sent_at, "
        "CASE WHEN EXISTS (SELECT 1 FROM inbound_replies ir "
        "  WHERE ir.from_phone = ct.phone AND ir.reply_type='opted_out') "
        "  THEN 'STOP' ELSE '' END AS opted_out "
        "FROM contacts ct JOIN campaigns c ON c.id = ct.campaign_id "
        f"WHERE ct.sent_at IS NOT NULL AND {sql_date('ct.sent_at')} >= ? AND {sql_date('ct.sent_at')} <= ? "
        "ORDER BY ct.sent_at ASC",
        (start_date, end_date)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_campaigns_in_range(start_date, end_date):
    """Campaign-level summary for a date range — for the rollup overview."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, created_at, sent_count, delivered_count, undelivered_count "
        f"FROM campaigns WHERE {sql_date('created_at')} >= ? AND {sql_date('created_at')} <= ? "
        "ORDER BY created_at DESC",
        (start_date, end_date)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Campaigns ─────────────────────────────────────────────────────────────────

def create_campaign(name, message_body, message_type, media_filename=None, brand='',
                    agent_names='', callback_numbers='', message_variants=''):
    conn = get_db()
    cur = conn.execute(
        'INSERT INTO campaigns (name,message_body,message_type,media_filename,brand,agent_names,callback_numbers,message_variants) VALUES (?,?,?,?,?,?,?,?)',
        (name, message_body, message_type, media_filename, brand.strip(),
         agent_names, callback_numbers, message_variants)
    )
    campaign_id = cur.lastrowid
    conn.commit()
    conn.close()
    return campaign_id


def get_campaign(campaign_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM campaigns WHERE id=?', (campaign_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_campaigns(statuses=None):
    """Return campaigns, newest first. If `statuses` is a non-empty list,
    only campaigns whose status is in that list are returned."""
    conn = get_db()
    if statuses:
        placeholders = ','.join('?' for _ in statuses)
        rows = conn.execute(
            f'SELECT * FROM campaigns WHERE status IN ({placeholders}) ORDER BY created_at DESC',
            tuple(statuses)
        ).fetchall()
    else:
        rows = conn.execute('SELECT * FROM campaigns ORDER BY created_at DESC').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def log_campaign_event(campaign_id, event, sent_snapshot=None):
    """Append a row to the campaign audit timeline. Best-effort: a logging
    failure must never interrupt a running campaign."""
    def _do():
        conn = get_db()
        conn.execute(
            'INSERT INTO campaign_events (campaign_id, event, sent_snapshot) VALUES (?,?,?)',
            (campaign_id, event, sent_snapshot)
        )
        conn.commit()
        conn.close()
    try:
        _exec_with_retry(_do)
    except Exception:
        pass


def get_campaign_events(campaign_id):
    conn = get_db()
    rows = conn.execute(
        'SELECT event, sent_snapshot, created_at FROM campaign_events WHERE campaign_id=? ORDER BY created_at ASC, id ASC',
        (campaign_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_campaign_status(campaign_id, status):
    def _do():
        conn = get_db()
        # Read prior status + current sent_count so we can describe the
        # transition (start vs resume) and snapshot progress for the timeline.
        prior_row = conn.execute(
            'SELECT status, sent_count FROM campaigns WHERE id=?', (campaign_id,)
        ).fetchone()
        prior_status = prior_row['status'] if prior_row else None
        sent_snapshot = prior_row['sent_count'] if prior_row else None

        if status == 'running':
            conn.execute(
                'UPDATE campaigns SET status=?, started_at=COALESCE(started_at,?) WHERE id=?',
                (status, datetime.utcnow(), campaign_id)
            )
        elif status in ('completed', 'cancelled'):
            conn.execute(
                'UPDATE campaigns SET status=?, completed_at=? WHERE id=?',
                (status, datetime.utcnow(), campaign_id)
            )
        else:
            conn.execute('UPDATE campaigns SET status=? WHERE id=?', (status, campaign_id))
        conn.commit()
        conn.close()
        return prior_status, sent_snapshot

    prior_status, sent_snapshot = _exec_with_retry(_do)

    # Map the transition to a human-readable event, then log it.
    if status == 'running':
        event = 'resumed' if prior_status == 'paused' else 'started'
    else:
        event = status  # paused, completed, cancelled
    log_campaign_event(campaign_id, event, sent_snapshot)


def get_campaign_variant_counts(campaign_id):
    """How many sent messages used each variant. Empty variant_sent means the
    base campaign message was used."""
    conn = get_db()
    rows = conn.execute(
        "SELECT COALESCE(NULLIF(variant_sent, ''), '[base message]') AS variant, "
        "COUNT(*) AS count FROM contacts "
        "WHERE campaign_id=? AND status='sent' GROUP BY variant ORDER BY count DESC",
        (campaign_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_campaign_delivery_stats(campaign_id):
    conn = get_db()
    rows = conn.execute(
        'SELECT delivery_status, error_code, COUNT(*) as count FROM delivery_stats WHERE campaign_id=? GROUP BY delivery_status, error_code ORDER BY count DESC',
        (campaign_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Contacts ──────────────────────────────────────────────────────────────────

def bulk_insert_contacts(campaign_id, contacts):
    """
    contacts: list of dicts with at least 'phone' and 'first_name'.
    Extra keys stored as JSON in custom_fields.
    """
    import json
    STANDARD = {'phone', 'first_name'}
    rows = []
    for c in contacts:
        phone = normalize_phone(c['phone'])
        first_name = c.get('first_name', '')
        # Anything beyond phone/first_name goes into custom_fields
        custom = {k: v for k, v in c.items() if k not in STANDARD}
        rows.append((campaign_id, phone, first_name, json.dumps(custom)))

    conn = get_db()
    conn.executemany(
        'INSERT INTO contacts (campaign_id,phone,first_name,custom_fields) VALUES (?,?,?,?)',
        rows
    )
    conn.execute('UPDATE campaigns SET total_contacts=? WHERE id=?', (len(contacts), campaign_id))
    conn.commit()
    conn.close()


def get_next_pending_contact(campaign_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM contacts WHERE campaign_id=? AND status='pending' ORDER BY id ASC LIMIT 1",
        (campaign_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ── Concurrency-safe claim/release (new engine) ───────────────────────────────

def claim_next_pending_contact(campaign_id):
    """
    Atomically claim ONE pending contact by flipping it to 'sending'.
    This is the idempotency guard: once claimed, no other worker (or campaign
    thread, or a duplicate after a restart) can pick the same contact.
    Returns the contact dict, or None if none are pending.
    """
    def _do():
        conn = get_db()
        try:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute(
                "SELECT * FROM contacts WHERE campaign_id=? AND status='pending' ORDER BY id ASC LIMIT 1",
                (campaign_id,)
            ).fetchone()
            if not row:
                conn.commit()
                return None
            conn.execute("UPDATE contacts SET status='sending' WHERE id=?", (row['id'],))
            conn.commit()
            return dict(row)
        finally:
            conn.close()
    return _exec_with_retry(_do)


def requeue_contact(contact_id):
    """Return a claimed ('sending') contact to 'pending' so it can be retried."""
    def _do():
        conn = get_db()
        conn.execute("UPDATE contacts SET status='pending' WHERE id=? AND status='sending'", (contact_id,))
        conn.commit()
        conn.close()
    _exec_with_retry(_do)


def reset_inflight_contacts(campaign_id):
    """
    Reset any contacts stuck in 'sending' back to 'pending'. Call at campaign
    start so a crash/redeploy mid-send doesn't strand contacts. Returns count.
    """
    def _do():
        conn = get_db()
        cur = conn.execute(
            "UPDATE contacts SET status='pending' WHERE campaign_id=? AND status='sending'",
            (campaign_id,)
        )
        conn.commit()
        n = cur.rowcount
        conn.close()
        return n
    return _exec_with_retry(_do)


def claim_number_capacity(phone_number, effective_cap):
    """
    Atomically reserve one send slot on a number for TODAY, respecting the
    daily cap. Handles daily rollover. This is what makes caps correct under
    concurrency and across multiple simultaneous campaigns — the reservation
    and the cap check happen in one indivisible step.

    effective_cap: int daily ceiling, or None for uncapped.
    Returns True if reserved (caller MUST send or release), False if at cap.
    """
    today = date.today().isoformat()
    def _do():
        conn = get_db()
        try:
            conn.execute('BEGIN IMMEDIATE')
            # Roll the daily counter over if the stored date isn't today
            conn.execute(
                "UPDATE sending_numbers SET daily_sent=0, daily_sent_date=? "
                "WHERE phone_number=? AND daily_sent_date!=?",
                (today, phone_number, today)
            )
            if effective_cap is None:
                cur = conn.execute(
                    "UPDATE sending_numbers SET daily_sent=daily_sent+1, daily_sent_date=? "
                    "WHERE phone_number=? AND active=1",
                    (today, phone_number)
                )
            else:
                cur = conn.execute(
                    "UPDATE sending_numbers SET daily_sent=daily_sent+1, daily_sent_date=? "
                    "WHERE phone_number=? AND active=1 AND daily_sent < ?",
                    (today, phone_number, effective_cap)
                )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    return _exec_with_retry(_do)


def release_number_capacity(phone_number):
    """
    Give back a reservation made by claim_number_capacity() when the send did
    NOT actually go out (opt-out rejection, permanent error, no contact, etc.),
    so daily_sent stays aligned with messages truly sent. Floors at 0.
    """
    today = date.today().isoformat()
    def _do():
        conn = get_db()
        conn.execute(
            "UPDATE sending_numbers SET daily_sent=daily_sent-1 "
            "WHERE phone_number=? AND daily_sent_date=? AND daily_sent > 0",
            (phone_number, today)
        )
        conn.commit()
        conn.close()
    _exec_with_retry(_do)


def mark_contact_sent(contact_id, campaign_id, sending_number, message_sid='', variant_sent=''):
    today = date.today().isoformat()
    def _do():
        conn = get_db()
        conn.execute(
            "UPDATE contacts SET status='sent', sending_number=?, sent_at=?, message_sid=?, variant_sent=? WHERE id=?",
            (sending_number, datetime.utcnow(), message_sid, variant_sent or '', contact_id)
        )
        conn.execute('UPDATE campaigns SET sent_count=sent_count+1 WHERE id=?', (campaign_id,))
        conn.execute('UPDATE sending_numbers SET total_sent=total_sent+1 WHERE phone_number=?', (sending_number,))
        # NOTE: daily_sent is now incremented atomically by claim_number_capacity()
        # BEFORE the send, so we no longer touch it here (avoids double-counting).
        conn.commit()
        conn.close()
    _exec_with_retry(_do)


def mark_contact_failed(contact_id, campaign_id, error_message=''):
    def _do():
        conn = get_db()
        conn.execute(
            "UPDATE contacts SET status='failed', error_message=? WHERE id=?",
            (error_message[:500], contact_id)
        )
        conn.execute('UPDATE campaigns SET failed_count=failed_count+1 WHERE id=?', (campaign_id,))
        conn.commit()
        conn.close()
    _exec_with_retry(_do)


def mark_contact_dnc(contact_id, campaign_id):
    conn = get_db()
    conn.execute("UPDATE contacts SET status='dnc_skipped' WHERE id=?", (contact_id,))
    conn.execute('UPDATE campaigns SET dnc_skipped_count=dnc_skipped_count+1 WHERE id=?', (campaign_id,))
    conn.commit()
    conn.close()


def mark_contact_brand_skipped(contact_id, campaign_id):
    """Contact skipped due to brand conflict."""
    conn = get_db()
    conn.execute("UPDATE contacts SET status='brand_skipped' WHERE id=?", (contact_id,))
    conn.execute('UPDATE campaigns SET dnc_skipped_count=dnc_skipped_count+1 WHERE id=?', (campaign_id,))
    conn.commit()
    conn.close()


def get_campaign_contacts(campaign_id, limit=100, offset=0):
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM contacts WHERE campaign_id=? ORDER BY id DESC LIMIT ? OFFSET ?',
        (campaign_id, limit, offset)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Delivery Status Webhook ───────────────────────────────────────────────────

# Error codes that mean the destination is permanently unable to receive SMS —
# invalid number, deactivated handset, or landline. These get auto-added to the
# DNC list so re-uploading the same list never texts them again. Conservative on
# purpose: transient codes (30003 phone-off, 30007 carrier-filtered) are NOT here.
DEAD_NUMBER_CODES = {'21211', '21214', '21614', '30005', '30006'}


def update_delivery_status(message_sid, delivery_status, error_code=''):
    if not message_sid:
        return

    conn = get_db()
    existing = conn.execute(
        'SELECT * FROM delivery_stats WHERE message_sid=?', (message_sid,)
    ).fetchone()

    if existing:
        old_status = existing['delivery_status']
        conn.execute(
            'UPDATE delivery_stats SET delivery_status=?, error_code=?, updated_at=? WHERE message_sid=?',
            (delivery_status, error_code, datetime.utcnow(), message_sid)
        )
        # BUGFIX: keep the contacts row current on EVERY callback, not just the
        # first. Previously this branch skipped contacts, so contacts.delivery_status
        # froze at 'sent' and all reports reading contacts under-counted delivered.
        conn.execute(
            'UPDATE contacts SET delivery_status=?, error_code=? WHERE message_sid=?',
            (delivery_status, error_code, message_sid)
        )
        conn.commit()

        terminal = {'delivered', 'failed', 'undelivered'}
        if delivery_status in terminal and old_status not in terminal:
            campaign_id    = existing['campaign_id']
            sending_number = existing['sending_number']
            delivered = delivery_status == 'delivered'
            if delivered:
                conn.execute('UPDATE campaigns SET delivered_count=delivered_count+1 WHERE id=?', (campaign_id,))
                conn.execute('UPDATE sending_numbers SET delivered_count=delivered_count+1 WHERE phone_number=?', (sending_number,))
            else:
                conn.execute('UPDATE campaigns SET undelivered_count=undelivered_count+1 WHERE id=?', (campaign_id,))
                conn.execute('UPDATE sending_numbers SET failed_count=failed_count+1 WHERE phone_number=?', (sending_number,))
            conn.commit()

            # Auto-suppress permanently-dead destinations so future campaigns skip them
            if not delivered and str(error_code) in DEAD_NUMBER_CODES:
                _dead_phone = existing['contact_phone']
                conn.execute(
                    'INSERT INTO dnc_list (phone,reason,source) VALUES (?,?,?) ON CONFLICT (phone) DO NOTHING',
                    (normalize_phone(_dead_phone), 'dead_number', 'auto')
                )
                conn.commit()
    else:
        contact = conn.execute(
            'SELECT * FROM contacts WHERE message_sid=?', (message_sid,)
        ).fetchone()

        if contact:
            conn.execute(
                'INSERT INTO delivery_stats (message_sid,campaign_id,contact_phone,sending_number,delivery_status,error_code) VALUES (?,?,?,?,?,?) ON CONFLICT (message_sid) DO NOTHING',
                (message_sid, contact['campaign_id'], contact['phone'], contact['sending_number'] or '', delivery_status, error_code)
            )
            conn.execute(
                'UPDATE contacts SET delivery_status=?, error_code=? WHERE message_sid=?',
                (delivery_status, error_code, message_sid)
            )
            conn.commit()

            terminal = {'delivered', 'failed', 'undelivered'}
            if delivery_status in terminal:
                campaign_id    = contact['campaign_id']
                sending_number = contact['sending_number'] or ''
                delivered = delivery_status == 'delivered'
                if delivered:
                    conn.execute('UPDATE campaigns SET delivered_count=delivered_count+1 WHERE id=?', (campaign_id,))
                    if sending_number:
                        conn.execute('UPDATE sending_numbers SET delivered_count=delivered_count+1 WHERE phone_number=?', (sending_number,))
                else:
                    conn.execute('UPDATE campaigns SET undelivered_count=undelivered_count+1 WHERE id=?', (campaign_id,))
                    if sending_number:
                        conn.execute('UPDATE sending_numbers SET failed_count=failed_count+1 WHERE phone_number=?', (sending_number,))
                conn.commit()

                # Auto-suppress permanently-dead destinations so future campaigns skip them
                if not delivered and str(error_code) in DEAD_NUMBER_CODES:
                    conn.execute(
                        'INSERT INTO dnc_list (phone,reason,source) VALUES (?,?,?) ON CONFLICT (phone) DO NOTHING',
                        (normalize_phone(contact['phone']), 'dead_number', 'auto')
                    )
                    conn.commit()

    conn.close()


# ── Scrub Jobs ────────────────────────────────────────────────────────────────

def create_scrub_job(filename, total):
    conn = get_db()
    cur = conn.execute(
        'INSERT INTO scrub_jobs (filename, total, estimated_cost) VALUES (?,?,?)',
        (filename, total, round(total * 0.005, 2))
    )
    job_id = cur.lastrowid
    conn.commit()
    conn.close()
    return job_id


def update_scrub_progress(job_id, processed, mobile, landline, dead, errors):
    conn = get_db()
    conn.execute('''
        UPDATE scrub_jobs
        SET processed=?, mobile_count=?, landline_count=?, dead_count=?, error_count=?
        WHERE id=?
    ''', (processed, mobile, landline, dead, errors, job_id))
    conn.commit()
    conn.close()


def complete_scrub_job(job_id, mobile, landline, dead, errors):
    conn = get_db()
    conn.execute('''
        UPDATE scrub_jobs
        SET status='completed', processed=total,
            mobile_count=?, landline_count=?, dead_count=?, error_count=?,
            completed_at=?
        WHERE id=?
    ''', (mobile, landline, dead, errors, datetime.utcnow(), job_id))
    conn.commit()
    conn.close()


def fail_scrub_job(job_id, reason=''):
    conn = get_db()
    conn.execute(
        "UPDATE scrub_jobs SET status='failed', completed_at=? WHERE id=?",
        (datetime.utcnow(), job_id)
    )
    conn.commit()
    conn.close()


def get_phone_by_message_sid(message_sid):
    """Look up the contact phone number from a message SID."""
    if not message_sid:
        return None
    conn = get_db()
    # Try delivery_stats first (faster)
    row = conn.execute(
        'SELECT contact_phone FROM delivery_stats WHERE message_sid=?', (message_sid,)
    ).fetchone()
    if row:
        conn.close()
        return row['contact_phone']
    # Fall back to contacts table
    row2 = conn.execute(
        'SELECT phone FROM contacts WHERE message_sid=?', (message_sid,)
    ).fetchone()
    conn.close()
    return row2['phone'] if row2 else None


def get_campaign_dead_numbers(campaign_id):
    """Returns all dead/landline numbers from a campaign's delivery stats."""
    conn = get_db()
    rows = conn.execute('''
        SELECT ds.contact_phone as phone, ds.error_code,
               CASE ds.error_code
                   WHEN '30006' THEN 'dead_disconnected'
                   WHEN '21614' THEN 'landline'
                   ELSE 'other'
               END as reason
        FROM delivery_stats ds
        WHERE ds.campaign_id=? AND ds.error_code IN ('30006','21614')
        ORDER BY ds.error_code, ds.contact_phone
    ''', (campaign_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
    conn = get_db()
    row = conn.execute('SELECT * FROM scrub_jobs WHERE id=?', (job_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_scrub_jobs():
    conn = get_db()
    rows = conn.execute('SELECT * FROM scrub_jobs ORDER BY created_at DESC LIMIT 20').fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Analytics / Overview Stats ────────────────────────────────────────────────

# Area code → State mapping (US only, covers all major area codes)
AREA_CODE_STATE = {
    '201':'NJ','202':'DC','203':'CT','204':'MB','205':'AL','206':'WA','207':'ME',
    '208':'ID','209':'CA','210':'TX','212':'NY','213':'CA','214':'TX','215':'PA',
    '216':'OH','217':'IL','218':'MN','219':'IN','220':'OH','223':'PA','224':'IL',
    '225':'LA','228':'MS','229':'GA','231':'MI','234':'OH','239':'FL','240':'MD',
    '248':'MI','251':'AL','252':'NC','253':'WA','254':'TX','256':'AL','260':'IN',
    '262':'WI','267':'PA','269':'MI','270':'KY','272':'PA','276':'VA','281':'TX',
    '301':'MD','302':'DE','303':'CO','304':'WV','305':'FL','307':'WY','308':'NE',
    '309':'IL','310':'CA','312':'IL','313':'MI','314':'MO','315':'NY','316':'KS',
    '317':'IN','318':'LA','319':'IA','320':'MN','321':'FL','323':'CA','325':'TX',
    '330':'OH','331':'IL','332':'NY','334':'AL','336':'NC','337':'LA','339':'MA',
    '340':'VI','341':'CA','346':'TX','347':'NY','351':'MA','352':'FL','360':'WA',
    '361':'TX','364':'KY','380':'OH','385':'UT','386':'FL','401':'RI','402':'NE',
    '404':'GA','405':'OK','406':'MT','407':'FL','408':'CA','409':'TX','410':'MD',
    '412':'PA','413':'MA','414':'WI','415':'CA','417':'MO','419':'OH','423':'TN',
    '424':'CA','425':'WA','430':'TX','432':'TX','434':'VA','435':'UT','440':'OH',
    '442':'CA','443':'MD','445':'PA','447':'IL','448':'FL','458':'OR','463':'IN',
    '464':'IL','469':'TX','470':'GA','475':'CT','478':'GA','479':'AR','480':'AZ',
    '484':'PA','501':'AR','502':'KY','503':'OR','504':'LA','505':'NM','507':'MN',
    '508':'MA','509':'WA','510':'CA','512':'TX','513':'OH','515':'IA','516':'NY',
    '517':'MI','518':'NY','520':'AZ','530':'CA','531':'NE','534':'WI','539':'OK',
    '540':'VA','541':'OR','551':'NJ','557':'MO','559':'CA','561':'FL','562':'CA',
    '563':'IA','564':'WA','567':'OH','570':'PA','571':'VA','572':'OK','573':'MO',
    '574':'IN','575':'NM','580':'OK','585':'NY','586':'MI','601':'MS','602':'AZ',
    '603':'NH','605':'SD','606':'KY','607':'NY','608':'WI','609':'NJ','610':'PA',
    '612':'MN','614':'OH','615':'TN','616':'MI','617':'MA','618':'IL','619':'CA',
    '620':'KS','623':'AZ','626':'CA','628':'CA','629':'TN','630':'IL','631':'NY',
    '636':'MO','641':'IA','646':'NY','650':'CA','651':'MN','657':'CA','659':'AL',
    '660':'MO','661':'CA','662':'MS','667':'MD','669':'CA','671':'GU','678':'GA',
    '680':'NY','681':'WV','682':'TX','689':'FL','701':'ND','702':'NV','703':'VA',
    '704':'NC','706':'GA','707':'CA','708':'IL','712':'IA','713':'TX','714':'CA',
    '715':'WI','716':'NY','717':'PA','718':'NY','719':'CO','720':'CO','724':'PA',
    '725':'NV','726':'TX','727':'FL','731':'TN','732':'NJ','734':'MI','737':'TX',
    '740':'OH','743':'NC','747':'CA','754':'FL','757':'VA','760':'CA','762':'GA',
    '763':'MN','765':'IN','769':'MS','770':'GA','772':'FL','773':'IL','774':'MA',
    '775':'NV','779':'IL','781':'MA','785':'KS','786':'FL','787':'PR','801':'UT',
    '802':'VT','803':'SC','804':'VA','805':'CA','806':'TX','808':'HI','810':'MI',
    '812':'IN','813':'FL','814':'PA','815':'IL','816':'MO','817':'TX','818':'CA',
    '820':'CA','828':'NC','830':'TX','831':'CA','832':'TX','838':'NY','840':'CA',
    '843':'SC','845':'NY','847':'IL','848':'NJ','850':'FL','854':'SC','856':'NJ',
    '857':'MA','858':'CA','859':'KY','860':'CT','862':'NJ','863':'FL','864':'SC',
    '865':'TN','870':'AR','872':'IL','878':'PA','901':'TN','903':'TX','904':'FL',
    '906':'MI','907':'AK','908':'NJ','909':'CA','910':'NC','912':'GA','913':'KS',
    '914':'NY','915':'TX','916':'CA','917':'NY','918':'OK','919':'NC','920':'WI',
    '925':'CA','927':'FL','928':'AZ','929':'NY','930':'IN','931':'TN','934':'NY',
    '936':'TX','937':'OH','938':'AL','940':'TX','941':'FL','943':'CA','945':'TX',
    '947':'MI','949':'CA','951':'CA','952':'MN','954':'FL','956':'TX','959':'CT',
    '970':'CO','971':'OR','972':'TX','973':'NJ','975':'MO','978':'MA','979':'TX',
    '980':'NC','984':'NC','985':'LA','986':'ID','989':'MI',
}


def get_phone_state(phone):
    """Map a 10-digit phone number to US state via area code."""
    if not phone or len(phone) < 3:
        return 'Unknown'
    area = phone[:3]
    return AREA_CODE_STATE.get(area, 'Other')


def get_overview_stats():
    """Returns today's high-level stats."""
    from datetime import date as _date
    today = _date.today().isoformat()
    conn = get_db()

    # Sent today
    sent_today = conn.execute(
        f"SELECT COUNT(*) FROM contacts WHERE {sql_date('sent_at')}=? AND status='sent'", (today,)
    ).fetchone()[0]

    # Delivered today (from delivery_stats)
    delivered_today = conn.execute(
        f"SELECT COUNT(*) FROM delivery_stats WHERE {sql_date('updated_at')}=? AND delivery_status='delivered'", (today,)
    ).fetchone()[0]

    # Undelivered today
    undelivered_today = conn.execute(
        f"SELECT COUNT(*) FROM delivery_stats WHERE {sql_date('updated_at')}=? AND delivery_status IN ('undelivered','failed')", (today,)
    ).fetchone()[0]

    # STOPs today
    stops_today = conn.execute(
        f"SELECT COUNT(*) FROM dnc_list WHERE {sql_date('added_at')}=? AND source='inbound'", (today,)
    ).fetchone()[0]

    # Replies today (non-optout)
    replies_today = conn.execute(
        f"SELECT COUNT(*) FROM inbound_replies WHERE {sql_date('received_at')}=? AND reply_type != 'opted_out'", (today,)
    ).fetchone()[0]

    # Running campaigns
    running = conn.execute(
        "SELECT COUNT(*) FROM campaigns WHERE status='running'"
    ).fetchone()[0]

    conn.close()
    return {
        'sent_today':        sent_today,
        'delivered_today':   delivered_today,
        'undelivered_today': undelivered_today,
        'stops_today':       stops_today,
        'replies_today':     replies_today,
        'running_campaigns': running,
        'delivery_pct':      round(delivered_today / sent_today * 100, 1) if sent_today > 0 else 0,
    }


def get_daily_stats(days=10):
    """Returns per-day sent/delivered/undelivered for last N days."""
    conn = get_db()
    rows = conn.execute(f'''
        SELECT {sql_date('sent_at')} as day, COUNT(*) as sent
        FROM contacts
        WHERE sent_at >= {sql_now_minus_days(days)} AND status='sent'
        GROUP BY {sql_date('sent_at')}
        ORDER BY day ASC
    ''').fetchall()

    delivered_rows = conn.execute(f'''
        SELECT {sql_date('updated_at')} as day, COUNT(*) as cnt
        FROM delivery_stats
        WHERE updated_at >= {sql_now_minus_days(days)} AND delivery_status='delivered'
        GROUP BY {sql_date('updated_at')}
        ORDER BY day ASC
    ''').fetchall()

    undelivered_rows = conn.execute(f'''
        SELECT {sql_date('updated_at')} as day, COUNT(*) as cnt
        FROM delivery_stats
        WHERE updated_at >= {sql_now_minus_days(days)} AND delivery_status IN ('undelivered','failed')
        GROUP BY {sql_date('updated_at')}
        ORDER BY day ASC
    ''').fetchall()

    conn.close()

    sent_map       = {r['day']: r['sent'] for r in rows}
    delivered_map  = {r['day']: r['cnt']  for r in delivered_rows}
    undelivered_map= {r['day']: r['cnt']  for r in undelivered_rows}

    # Fill in all days
    from datetime import date as _date, timedelta
    result = []
    for i in range(days - 1, -1, -1):
        d = (_date.today() - timedelta(days=i)).isoformat()
        result.append({
            'day':         d,
            'sent':        sent_map.get(d, 0),
            'delivered':   delivered_map.get(d, 0),
            'undelivered': undelivered_map.get(d, 0),
        })
    return result


def get_state_distribution():
    """Returns message counts grouped by US state."""
    conn = get_db()
    # Get all sent contact phones
    rows = conn.execute(
        "SELECT phone FROM contacts WHERE status='sent'"
    ).fetchall()
    conn.close()

    state_counts = {}
    for r in rows:
        state = get_phone_state(r['phone'])
        state_counts[state] = state_counts.get(state, 0) + 1

    return state_counts


def get_campaign_costs():
    """Returns estimated cost per campaign."""
    conn = get_db()
    campaigns = conn.execute(
        "SELECT id, name, message_body, message_type, sent_count, brand, status, created_at FROM campaigns ORDER BY created_at DESC"
    ).fetchall()
    conn.close()

    SMS_COST = 0.0079  # per segment
    MMS_COST = 0.0200  # per message

    results = []
    for c in campaigns:
        sent = c['sent_count'] or 0
        if c['message_type'] == 'mms':
            cost = sent * MMS_COST
            cost_per = MMS_COST
            segments = 1
        else:
            # Calculate segments from message length
            # Use 153 chars per segment for multi-segment (to be safe)
            body_len = len(c['message_body'] or '')
            segments = 1 if body_len <= 160 else max(1, -(-body_len // 153))
            cost = sent * segments * SMS_COST
            cost_per = segments * SMS_COST

        results.append({
            'id':        c['id'],
            'name':      c['name'],
            'brand':     c['brand'] or '',
            'status':    c['status'],
            'sent':      sent,
            'type':      c['message_type'],
            'segments':  segments,
            'cost':      round(cost, 2),
            'cost_per':  round(cost_per, 4),
            'created_at': c['created_at'],
        })
    return results
