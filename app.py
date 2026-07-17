import os
import csv
import io
import re
import time
import threading
import logging
import hashlib
import hmac
from datetime import datetime
from flask import Flask, request, jsonify, render_template, send_from_directory, Response
from werkzeug.utils import secure_filename
import database as db
import sender
import drop
import sequence
import rvm
import pacer
import scheduler

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024  # 25 MB max upload
app.secret_key = os.urandom(24)

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_IMAGE_EXT = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
ALLOWED_CSV_EXT   = {'csv'}

# ── Password Auth ─────────────────────────────────────────────────────────────
# Set DASHBOARD_PASSWORD env var in Railway to enable.
# Webhooks are ALWAYS exempt — Twilio needs them 24/7 without auth.

DASHBOARD_PASSWORD = os.environ.get('DASHBOARD_PASSWORD', '').strip()

# Routes that external services (Twilio, Drop.co) call — must NEVER require auth
WEBHOOK_PATHS = {'/webhook/inbound', '/webhook/status', '/webhook/drop'}

@app.before_request
def require_auth():
    # Skip auth if no password set (local dev)
    if not DASHBOARD_PASSWORD:
        return None

    # Always allow Twilio webhooks — campaigns must keep running
    if request.path in WEBHOOK_PATHS:
        return None

    # Always allow static files (CSS, JS, images)
    if request.path.startswith('/static/'):
        return None

    # Health check (DigitalOcean App Platform) — must be reachable without auth
    if request.path == '/health':
        return None

    # Check Basic Auth
    auth = request.authorization
    if auth and hmac.compare_digest(auth.password, DASHBOARD_PASSWORD):
        return None

    # Return 401 with WWW-Authenticate to trigger browser password prompt
    return Response(
        'Authentication required',
        401,
        {'WWW-Authenticate': 'Basic realm="SMS Dashboard"'}
    )


# ── Webhook authentication ────────────────────────────────────────────────────
# Twilio signs requests (X-Twilio-Signature); Drop has no signature, so it carries
# a secret token in its webhook URL. Enforcement defaults OFF (log-only) so a URL /
# signature misconfig can't silently drop delivery callbacks — flip
# WEBHOOK_AUTH_ENFORCE=1 once the logs show real traffic validating cleanly.
WEBHOOK_AUTH_ENFORCE = os.environ.get('WEBHOOK_AUTH_ENFORCE', '0').strip().lower() in ('1', 'true', 'on', 'yes')
DROP_WEBHOOK_TOKEN   = os.environ.get('DROP_WEBHOOK_TOKEN', '').strip()


def _public_url():
    """The exact public URL the provider signed — rebuilt from base_url so it
    matches even behind DigitalOcean's proxy (where request.url can be wrong)."""
    base = (db.get_setting('base_url', '') or os.environ.get('APP_URL', '')).strip().rstrip('/')
    return base + request.full_path.rstrip('?') if base else request.url


def _verify_twilio():
    sig = request.headers.get('X-Twilio-Signature', '')
    if not sig:
        return False
    from twilio.request_validator import RequestValidator
    url, params = _public_url(), request.form.to_dict()
    for tok in {a['auth_token'] for a in db.get_accounts() if a.get('auth_token')}:
        try:
            if RequestValidator(tok).validate(url, params, sig):
                return True
        except Exception:
            continue
    return False


def _verify_drop():
    if not DROP_WEBHOOK_TOKEN:
        return True   # not configured -> skip (dev / dry-run)
    tok = request.args.get('token', '') or request.headers.get('X-Drop-Token', '')
    return hmac.compare_digest(tok, DROP_WEBHOOK_TOKEN)


def _webhook_guard(valid, kind):
    """None if allowed; a 403 tuple if invalid AND enforcement is on (log-only otherwise)."""
    if valid:
        return None
    if WEBHOOK_AUTH_ENFORCE:
        logger.warning('[webhook-auth] REJECTED %s — invalid/missing credentials', kind)
        return ('forbidden', 403)
    logger.warning('[webhook-auth] %s failed validation (log-only; still processing)', kind)
    return None


# Init DB at import time — works under gunicorn/wsgi AND direct python app.py
db.init_db()
sequence.init_sequence_db()
# Start the background scheduler loop (idle until scheduler_enabled=1).
scheduler.start()

# Lock prevents race condition where two simultaneous /start requests
# both read 'draft' status before either writes 'running'
_start_lock = threading.Lock()

# Track live campaign threads: {campaign_id: Thread}
campaign_threads: dict[int, threading.Thread] = {}





# ── Helpers ───────────────────────────────────────────────────────────────────

def allowed(filename, exts):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in exts


def get_base_url():
    saved = db.get_setting('base_url', '').strip()
    if saved:
        return saved
    # Fallback: try request context (works during route handlers)
    # Falls back to localhost if called outside a request (e.g. on startup)
    try:
        return request.host_url.rstrip('/')
    except RuntimeError:
        return 'http://localhost:5000'


def _start_thread(campaign_id):
    # Prune dead threads from previous campaigns to prevent memory leak
    dead = [cid for cid, t in campaign_threads.items() if not t.is_alive()]
    for cid in dead:
        del campaign_threads[cid]

    base_url = get_base_url()
    t = threading.Thread(
        target=sender.run_campaign,
        args=(campaign_id, base_url),
        daemon=True,
        name=f'campaign-{campaign_id}'
    )
    campaign_threads[campaign_id] = t
    t.start()
    return t


_resume_lock = threading.Lock()
_resumed_once = False

def _resume_running_campaigns():
    """
    Relaunch any campaign left in 'running' after a restart/redeploy.
    Runs at IMPORT time so it works under gunicorn (not just `python app.py`).
    Guarded so it only fires once per process. Contacts stranded in 'sending'
    are auto-recovered inside run_campaign via reset_inflight_contacts().

    IMPORTANT: run the app as a SINGLE process (e.g. gunicorn --workers 1
    --threads 8). Multiple worker processes would each keep their own rate-limit
    buckets and each resume the same campaigns, multiplying your real send rate.
    """
    global _resumed_once
    with _resume_lock:
        if _resumed_once:
            return
        _resumed_once = True
        try:
            for c in db.get_campaigns(statuses=['running']):
                logger.info(f"Resuming campaign {c['id']} after restart")
                _start_thread(c['id'])
        except Exception:
            logger.exception("Failed to resume running campaigns")


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('dashboard.html')


@app.route('/health')
def health():
    """Liveness probe for the platform health check (no auth, no DB dependency)."""
    return jsonify({'status': 'ok'})


@app.route('/static/uploads/<filename>')
def serve_upload(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


# ── Campaigns API ─────────────────────────────────────────────────────────────

@app.route('/api/campaigns', methods=['GET'])
def api_get_campaigns():
    # Optional ?filter= maps a friendly name to a set of statuses.
    # 'all' (or omitted) returns everything.
    filter_map = {
        'all':            None,
        'completed':      ['completed'],
        'cancelled':      ['cancelled'],
        'active':         ['running'],
        'active_paused':  ['running', 'paused'],
    }
    key = request.args.get('filter', 'all')
    statuses = filter_map.get(key, None)
    return jsonify(db.get_campaigns(statuses))


@app.route('/api/campaigns/<int:cid>/events', methods=['GET'])
def api_get_campaign_events(cid):
    return jsonify(db.get_campaign_events(cid))


@app.route('/api/campaigns/<int:cid>/variants', methods=['GET'])
def api_get_campaign_variants(cid):
    return jsonify(db.get_campaign_variant_counts(cid))


@app.route('/api/campaigns/<int:cid>', methods=['GET'])
def api_get_campaign(cid):
    c = db.get_campaign(cid)
    if not c:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(c)


@app.route('/api/campaigns/<int:cid>/contacts', methods=['GET'])
def api_get_contacts(cid):
    try:
        limit  = max(1, min(500, int(request.args.get('limit', 100))))
        offset = max(0, int(request.args.get('offset', 0)))
    except (ValueError, TypeError):
        return jsonify({'error': 'limit and offset must be integers'}), 400
    return jsonify(db.get_campaign_contacts(cid, limit, offset))


@app.route('/api/campaigns', methods=['POST'])
def api_create_campaign():
    try:
        name         = request.form.get('name', '').strip()
        message_body = request.form.get('message_body', '').strip()
        message_type = request.form.get('message_type', 'sms').strip()
        brand        = request.form.get('brand', '').strip()

        # Rotation fields (optional)
        agent_names_raw      = request.form.get('agent_names', '').strip()
        callback_numbers_raw = request.form.get('callback_numbers', '').strip()
        message_variants_raw = request.form.get('message_variants', '').strip()

        if not name or not message_body:
            return jsonify({'error': 'Campaign name and message are required'}), 400
        if message_type not in ('sms', 'mms'):
            return jsonify({'error': 'message_type must be sms or mms'}), 400

        # ── Brand keyword check ──────────────────────────────────────────
        # Prevent accidentally using wrong brand name in messages
        if brand:
            brand_lower = brand.lower()
            # Build list of all text to check (main body + all variants)
            all_bodies = [message_body] + [v.strip() for v in message_variants_raw.splitlines() if v.strip()]
            BRAND_PAIRS = [
                ('lendpoint', 'liberty'),
                ('liberty',   'lendpoint'),
            ]
            for check_brand, forbidden in BRAND_PAIRS:
                if check_brand in brand_lower:
                    for body_text in all_bodies:
                        if forbidden in body_text.lower():
                            return jsonify({
                                'error': f'Message contains "{forbidden}" but campaign brand is "{brand}". '
                                         f'Please check your message for brand name conflicts.'
                            }), 400

        # ── Parse CSV ────────────────────────────────────────────────────────
        if 'csv_file' not in request.files:
            return jsonify({'error': 'CSV file is required'}), 400

        csv_file = request.files['csv_file']
        if not csv_file.filename or not allowed(csv_file.filename, ALLOWED_CSV_EXT):
            return jsonify({'error': 'A valid .csv file is required'}), 400

        raw = csv_file.read().decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(raw))

        # Validate headers exist
        if reader.fieldnames is None:
            return jsonify({'error': 'CSV appears empty or has no header row'}), 400

        # Normalize headers for flexible matching
        headers = [h.strip().lower() for h in reader.fieldnames]
        phone_cols = [h for h in reader.fieldnames
                      if h.strip().lower() in ('phone', 'phone_number', 'phonenumber', 'telephone', 'mobile', 'cell')]
        name_cols  = [h for h in reader.fieldnames
                      if h.strip().lower() in ('first_name', 'firstname', 'name', 'first')]

        if not phone_cols:
            return jsonify({
                'error': f'No phone column found. Your CSV has: {", ".join(reader.fieldnames)}. '
                         f'Rename your phone column to "phone".'
            }), 400

        phone_col = phone_cols[0]
        name_col  = name_cols[0] if name_cols else None

        contacts = []
        seen     = set()
        invalid  = 0
        dupes    = 0

        for row in reader:
            phone = (row.get(phone_col) or '').strip()
            first_name = (row.get(name_col) or '').strip() if name_col else ''

            if not phone:
                continue

            normalized = db.normalize_phone(phone)

            if len(normalized) != 10:
                invalid += 1
                continue

            if normalized in seen:
                dupes += 1
                continue

            seen.add(normalized)

            # Capture all non-standard columns as custom fields
            custom = {}
            for col in reader.fieldnames:
                col_lower = col.strip().lower()
                if col_lower not in ('phone','phone_number','phonenumber','telephone','mobile','cell',
                                     'first_name','firstname','name','first'):
                    val = (row.get(col) or '').strip()
                    if val:
                        custom[col.strip()] = val

            contacts.append({
                'phone': normalized,
                'first_name': first_name,
                **custom
            })

        if not contacts:
            return jsonify({'error': 'No valid contacts found in CSV'}), 400

        # ── Handle MMS image ─────────────────────────────────────────────────
        media_filename = None
        if message_type == 'mms':
            if 'image_file' not in request.files or not request.files['image_file'].filename:
                return jsonify({'error': 'An image file is required for MMS'}), 400

            img = request.files['image_file']
            if not allowed(img.filename, ALLOWED_IMAGE_EXT):
                return jsonify({'error': 'Image must be PNG, JPG, GIF, or WEBP'}), 400

            ts  = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
            media_filename = f"{ts}_{secure_filename(img.filename)}"
            img.save(os.path.join(UPLOAD_FOLDER, media_filename))

        # ── Create campaign ──────────────────────────────────────────────────
        # Parse rotation lists — one item per line, store as JSON arrays
        import json as _json
        def parse_lines(raw):
            items = [line.strip() for line in raw.splitlines() if line.strip()]
            return _json.dumps(items) if items else ''

        agent_names_json      = parse_lines(agent_names_raw)
        callback_numbers_json = parse_lines(callback_numbers_raw)
        # Message variants separated by blank lines
        variant_blocks = [b.strip() for b in re.split(r'\n\s*\n', message_variants_raw) if b.strip()]
        message_variants_json = _json.dumps(variant_blocks) if variant_blocks else ''

        cid = db.create_campaign(
            name, message_body, message_type, media_filename,
            brand=brand,
            agent_names=agent_names_json,
            callback_numbers=callback_numbers_json,
            message_variants=message_variants_json,
        )
        db.bulk_insert_contacts(cid, contacts)

        return jsonify({
            'id':               cid,
            'contacts_loaded':  len(contacts),
            'duplicates_skipped': dupes,
            'invalid_skipped':  invalid,
            'message':          f'Campaign created with {len(contacts):,} contacts'
        })

    except Exception as e:
        logger.exception('Error creating campaign')
        return jsonify({'error': str(e)}), 500


@app.route('/api/campaigns/<int:cid>/start', methods=['POST'])
def api_start_campaign(cid):
    with _start_lock:
        c = db.get_campaign(cid)
        if not c:
            return jsonify({'error': 'Not found'}), 404
        if c['status'] in ('running', 'paused', 'completed', 'cancelled'):
            return jsonify({'error': f"Campaign cannot be started — status is '{c['status']}'"}), 400

        numbers = db.get_active_numbers()
        if not numbers:
            return jsonify({'error': 'No active sending numbers configured. Add numbers in the Numbers tab first.'}), 400

        db.update_campaign_status(cid, 'running')
        _start_thread(cid)
    return jsonify({'status': 'running'})


@app.route('/api/campaigns/<int:cid>/pause', methods=['POST'])
def api_pause_campaign(cid):
    c = db.get_campaign(cid)
    if not c or c['status'] != 'running':
        return jsonify({'error': 'Campaign is not currently running'}), 400
    db.update_campaign_status(cid, 'paused')
    return jsonify({'status': 'paused'})


@app.route('/api/campaigns/<int:cid>/resume', methods=['POST'])
def api_resume_campaign(cid):
    c = db.get_campaign(cid)
    if not c or c['status'] != 'paused':
        return jsonify({'error': 'Campaign is not paused'}), 400

    # Check if there are actually pending contacts
    pending = db.get_next_pending_contact(cid)
    if not pending:
        return jsonify({'error': 'No pending contacts — campaign is complete or all contacts already processed'}), 400

    db.update_campaign_status(cid, 'running')

    existing = campaign_threads.get(cid)
    if existing and existing.is_alive():
        logger.info(f"[Campaign {cid}] Resume: thread already alive")
    else:
        logger.info(f"[Campaign {cid}] Resume: starting new thread")
        _start_thread(cid)

    return jsonify({'status': 'running'})


@app.route('/api/campaigns/<int:cid>/cancel', methods=['POST'])
def api_cancel_campaign(cid):
    c = db.get_campaign(cid)
    if not c:
        return jsonify({'error': 'Not found'}), 404
    db.update_campaign_status(cid, 'cancelled')
    return jsonify({'status': 'cancelled'})


# ── DNC API ───────────────────────────────────────────────────────────────────

@app.route('/api/dnc', methods=['GET'])
def api_get_dnc():
    search = request.args.get('search', '').strip()
    # Paginated when limit is provided; otherwise falls back to full list
    # (kept for backward compatibility, e.g. small lists / manual use).
    limit  = request.args.get('limit')
    offset = request.args.get('offset', 0)
    if limit is not None:
        try:
            return jsonify(db.get_dnc_list(search=search, limit=int(limit), offset=int(offset)))
        except (ValueError, TypeError):
            return jsonify(db.get_dnc_list(search=search))
    return jsonify(db.get_dnc_list(search=search))


@app.route('/api/dnc/count', methods=['GET'])
def api_get_dnc_count():
    search = request.args.get('search', '').strip()
    return jsonify({'count': db.get_dnc_count(search=search)})


@app.route('/api/dnc/backfill-dead', methods=['POST'])
def api_backfill_dead():
    """One-time sweep of historically-dead numbers into the DNC list."""
    added = db.backfill_dead_numbers_to_dnc()
    return jsonify({'added': added, 'message': f'Added {added:,} previously-dead number(s) to DNC.'})


@app.route('/api/tracking/backfill-status', methods=['POST'])
def api_backfill_status():
    """One-time repair: sync contacts.delivery_status from delivery_stats so
    historical reports show the true delivered/undelivered instead of 'sent'."""
    fixed = db.backfill_contact_delivery_status()
    return jsonify({'fixed': fixed, 'message': f'Corrected {fixed:,} contact delivery statuses.'})


@app.route('/api/dnc', methods=['POST'])
def api_add_dnc():
    data   = request.get_json() or {}
    phone  = data.get('phone', '').strip()
    reason = data.get('reason', 'manual')

    if not phone:
        return jsonify({'error': 'Phone required'}), 400

    normalized = db.normalize_phone(phone)
    if len(normalized) != 10:
        return jsonify({'error': 'Invalid phone number — enter 10 digits'}), 400

    db.add_to_dnc(normalized, reason, 'manual')
    return jsonify({'status': 'added', 'phone': normalized})


@app.route('/api/dnc/bulk', methods=['POST'])
def api_bulk_dnc():
    """Bulk import — accepts raw text with one number per line."""
    data   = request.get_json() or {}
    raw    = data.get('numbers', '').strip()
    reason = data.get('reason', 'manual')

    if not raw:
        return jsonify({'error': 'No numbers provided'}), 400

    lines = [l.strip() for l in raw.replace(',', '\n').splitlines() if l.strip()]
    added = db.bulk_add_to_dnc(lines, reason=reason, source='manual')
    total = len(db.get_dnc_list())

    return jsonify({
        'status':  'imported',
        'added':   added,
        'total':   total,
        'message': f'Added {added:,} new numbers. Total DNC list: {total:,}'
    })


@app.route('/api/dnc/export', methods=['GET'])
def api_export_dnc():
    import csv, io
    rows = db.get_dnc_list()
    output = io.StringIO()
    fields = ['phone', 'reason', 'source', 'their_message', 'added_at']
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(rows)
    return (
        output.getvalue(),
        200,
        {'Content-Type': 'text/csv',
         'Content-Disposition': 'attachment; filename=dnc_list.csv'}
    )


# ── Tracking / Reporting (READ-ONLY) ─────────────────────────────────────────

@app.route('/api/tracking/<int:cid>', methods=['GET'])
def api_tracking(cid):
    data = db.get_campaign_tracking(cid)
    if not data:
        return jsonify({'error': 'Campaign not found'}), 404
    return jsonify(data)


def _slug(s):
    return ''.join(ch if ch.isalnum() else '_' for ch in (s or 'campaign')).strip('_') or 'campaign'


@app.route('/api/tracking/<int:cid>/export', methods=['GET'])
def api_tracking_export(cid):
    """CSV of everyone actually texted in this campaign, with status columns."""
    import csv, io
    camp = db.get_campaign(cid)
    if not camp:
        return jsonify({'error': 'Campaign not found'}), 404
    rows = db.get_campaign_texted_rows(cid)
    output = io.StringIO()
    fields = ['phone', 'first_name', 'sending_number', 'variant_sent',
              'delivery_status', 'error_code', 'opted_out', 'sent_at']
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(rows)
    fname = f"texted_{_slug(camp['name'])}.csv"
    return (output.getvalue(), 200,
            {'Content-Type': 'text/csv',
             'Content-Disposition': f'attachment; filename={fname}'})


@app.route('/api/tracking/<int:cid>/templates', methods=['GET'])
def api_tracking_templates(cid):
    """Plain-text export of the message templates/variants used in a campaign."""
    camp = db.get_campaign(cid)
    if not camp:
        return jsonify({'error': 'Campaign not found'}), 404
    lines = [f"Campaign: {camp['name']}", f"Created: {camp.get('created_at','')}", ""]
    import json as _json
    try:
        variants = _json.loads(camp.get('message_variants') or '[]')
        if not isinstance(variants, list):
            variants = []
    except (ValueError, TypeError):
        variants = []
    variants = [v for v in variants if str(v).strip()]
    if not variants:
        variants = [camp.get('message_body', '')]
    lines.append(f"MESSAGE TEMPLATES ({len(variants)}):")
    for i, v in enumerate(variants, 1):
        lines.append(f"\n--- Variant {i} ---\n{str(v).strip()}")
    if camp.get('agent_names'):
        lines.append(f"\nAgent names rotated: {camp['agent_names']}")
    if camp.get('callback_numbers'):
        lines.append(f"Callback numbers rotated: {camp['callback_numbers']}")
    fname = f"templates_{_slug(camp['name'])}.txt"
    return ('\n'.join(lines), 200,
            {'Content-Type': 'text/plain',
             'Content-Disposition': f'attachment; filename={fname}'})


@app.route('/api/tracking/rollup/export', methods=['GET'])
def api_tracking_rollup_export():
    """Combined CSV of everyone texted across all campaigns in a date range."""
    import csv, io
    start = request.args.get('start', '').strip()
    end   = request.args.get('end', '').strip()
    if not start or not end:
        return jsonify({'error': 'start and end dates required (YYYY-MM-DD)'}), 400
    rows = db.get_texted_rows_by_range(start, end)
    output = io.StringIO()
    fields = ['campaign_name', 'phone', 'first_name', 'sending_number',
              'variant_sent', 'delivery_status', 'error_code', 'opted_out', 'sent_at']
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(rows)
    return (output.getvalue(), 200,
            {'Content-Type': 'text/csv',
             'Content-Disposition': f'attachment; filename=texted_{start}_to_{end}.csv'})


@app.route('/api/tracking/rollup', methods=['GET'])
def api_tracking_rollup():
    """Campaign-level summary for a date range (for the rollup table)."""
    start = request.args.get('start', '').strip()
    end   = request.args.get('end', '').strip()
    if not start or not end:
        return jsonify({'error': 'start and end dates required'}), 400
    return jsonify(db.get_campaigns_in_range(start, end))


@app.route('/api/dnc/<phone>', methods=['DELETE'])
def api_remove_dnc(phone):
    db.remove_from_dnc(phone)
    return jsonify({'status': 'removed'})


# ── Replies API ───────────────────────────────────────────────────────────────

@app.route('/api/replies', methods=['GET'])
def api_get_replies():
    days = request.args.get('days')
    if days:
        try:
            days = int(days)
        except ValueError:
            days = None
    # Show all non-opt-out replies — includes 'other' AND 'opted_in'
    # so "Yes", "Sure", etc. show up as leads even if classified as opt-in
    rows = db.get_inbound_replies(reply_type='non_optout', days=days)
    return jsonify(rows)


@app.route('/api/replies/export', methods=['GET'])
def api_export_replies():
    import csv, io
    days = request.args.get('days')
    if days:
        try:
            days = int(days)
        except ValueError:
            days = None
    rows = db.get_inbound_replies(reply_type='non_optout', days=days)
    output = io.StringIO()
    fields = ['from_phone', 'body', 'reply_type', 'received_at']
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(rows)
    return (
        output.getvalue(),
        200,
        {'Content-Type': 'text/csv',
         'Content-Disposition': 'attachment; filename=interested_leads.csv'}
    )


# ── Accounts API ──────────────────────────────────────────────────────────────

@app.route('/api/accounts', methods=['GET'])
def api_get_accounts():
    return jsonify(db.get_accounts())


@app.route('/api/accounts', methods=['POST'])
def api_add_account():
    data = request.get_json() or {}
    name         = data.get('name', '').strip()
    account_sid  = data.get('account_sid', '').strip()
    auth_token   = data.get('auth_token', '').strip()
    brand        = data.get('brand', '').strip()
    daily_limit  = int(data.get('daily_limit', 10000))

    if not all([name, account_sid, auth_token]):
        return jsonify({'error': 'Name, Account SID, and Auth Token are all required'}), 400

    db.add_account(name, account_sid, auth_token, brand=brand, daily_limit=daily_limit)
    return jsonify({'status': 'added'})


@app.route('/api/accounts/<int:aid>', methods=['PUT'])
def api_update_account(aid):
    data        = request.get_json() or {}
    brand       = data.get('brand', '').strip()
    daily_limit = int(data.get('daily_limit', 10000))
    db.update_account_brand(aid, brand, daily_limit)
    # Per-sub auto-provisioning config (only touched when the keys are present)
    if 'voice_flow_sid' in data or 'inbound_webhook_url' in data:
        acct = db.get_account_by_id(aid) or {}
        db.update_account_provisioning(
            aid,
            data.get('voice_flow_sid', acct.get('voice_flow_sid', '')),
            data.get('inbound_webhook_url', acct.get('inbound_webhook_url', '')),
        )
    return jsonify({'status': 'updated'})


@app.route('/api/accounts/<int:aid>', methods=['DELETE'])
def api_delete_account(aid):
    db.delete_account(aid)
    return jsonify({'status': 'deleted'})


# ── Numbers API ───────────────────────────────────────────────────────────────

@app.route('/api/numbers', methods=['GET'])
def api_get_numbers():
    return jsonify(db.get_sending_numbers_list())


@app.route('/api/numbers', methods=['POST'])
def api_add_number():
    data         = request.get_json() or {}
    account_id   = data.get('account_id')
    phone_number = data.get('phone_number', '').strip()
    friendly     = data.get('friendly_name', '').strip()
    warmup_mode  = int(data.get('warmup_mode', 1))  # Default ON for new numbers
    daily_limit  = int(data.get('daily_limit', 500))

    if not account_id or not phone_number:
        return jsonify({'error': 'Account and phone number required'}), 400

    normalized = db.normalize_phone(phone_number)
    if len(normalized) != 10:
        return jsonify({'error': 'Invalid phone number — enter 10 digits'}), 400

    db.add_sending_number(account_id, normalized, friendly, warmup_mode=warmup_mode, daily_limit=daily_limit)
    return jsonify({'status': 'added'})


# ── Auto-provision (buy + configure) numbers ─────────────────────────────────

MAX_BUY_PER_REQUEST = 5  # hard cap — cannot be exceeded regardless of request

@app.route('/api/numbers/provision', methods=['POST'])
def api_provision_numbers():
    """
    Buy N numbers in an area code under a subaccount and configure each to match
    the account's Studio Flow (voice) + inbound reply webhook (SMS), then add
    them to the sending pool in Day-1 warmup.

    Body: { account_id, area_code, count, preview: bool }
    preview=true  -> search + show what WOULD be bought + est. cost. Buys nothing.
    preview=false -> actually purchases (capped at MAX_BUY_PER_REQUEST).
    """
    from twilio.rest import Client
    from twilio.base.exceptions import TwilioRestException
    from twilio.http.http_client import TwilioHttpClient

    data       = request.get_json() or {}
    account_id = data.get('account_id')
    area_code  = str(data.get('area_code', '')).strip()
    preview    = bool(data.get('preview', True))
    try:
        count = int(data.get('count', 1))
    except (ValueError, TypeError):
        count = 1

    # ---- Validation ----
    if not account_id:
        return jsonify({'error': 'Choose a subaccount.'}), 400
    if not (area_code.isdigit() and len(area_code) == 3):
        return jsonify({'error': 'Area code must be exactly 3 digits.'}), 400
    if count < 1:
        return jsonify({'error': 'Count must be at least 1.'}), 400
    if count > MAX_BUY_PER_REQUEST:
        return jsonify({'error': f'Max {MAX_BUY_PER_REQUEST} numbers per request.'}), 400

    acct = db.get_account_by_id(account_id)
    if not acct:
        return jsonify({'error': 'Subaccount not found.'}), 404

    flow_sid    = (acct.get('voice_flow_sid') or '').strip()
    webhook_url = (acct.get('inbound_webhook_url') or '').strip()
    if not flow_sid or not webhook_url:
        return jsonify({'error': 'This subaccount is missing its Studio Flow SID and/or reply webhook. '
                                 'Set them in the subaccount config first.'}), 400

    sub_sid = acct['account_sid']
    # Voice binding that matches your working number exactly (voice_url + status_callback both point at the flow)
    flow_url = f"https://webhooks.twilio.com/v1/Accounts/{sub_sid}/Flows/{flow_sid}"

    try:
        http_client = TwilioHttpClient(timeout=30)
        client = Client(sub_sid, acct['auth_token'], http_client=http_client)
    except Exception as e:
        return jsonify({'error': f'Could not connect to Twilio for this subaccount: {e}'}), 502

    # ---- Find available numbers in the area code ----
    try:
        available = client.available_phone_numbers('US').local.list(
            area_code=int(area_code), sms_enabled=True, voice_enabled=True, limit=count
        )
    except TwilioRestException as e:
        return jsonify({'error': f'Twilio search failed: {e}'}), 502

    if not available:
        return jsonify({'error': f'No SMS-capable numbers available in area code {area_code}.'}), 404

    found = [a.phone_number for a in available][:count]

    # ---- Preview: show what would happen, buy nothing ----
    if preview:
        return jsonify({
            'preview': True,
            'subaccount': acct['name'],
            'area_code': area_code,
            'available_found': found,
            'would_buy': len(found),
            'voice_flow_url': flow_url,
            'sms_webhook': webhook_url,
            'note': f'Preview only — nothing purchased. Confirm to buy {len(found)} number(s). '
                    f'Each number is a recurring monthly charge.'
        })

    # ---- Real purchase ----
    results = []
    for num in found:
        try:
            purchased = client.incoming_phone_numbers.create(
                phone_number=num,
                voice_url=flow_url,
                voice_method='POST',
                status_callback=flow_url,
                status_callback_method='POST',
                sms_url=webhook_url,
                sms_method='POST',
            )
            # Add to sending pool in Day-1 warmup (warmup_mode=1 sets start date = today)
            normalized = db.normalize_phone(purchased.phone_number)
            db.add_sending_number(account_id, normalized, friendly_name='',
                                  warmup_mode=1, daily_limit=500)
            logger.info(f"[Provision] Bought {purchased.phone_number} ({purchased.sid}) "
                        f"under {acct['name']} → flow {flow_sid}, warmup Day 1")
            results.append({'phone_number': purchased.phone_number, 'sid': purchased.sid, 'status': 'bought'})
        except TwilioRestException as e:
            logger.error(f"[Provision] FAILED to buy {num} under {acct['name']}: {e}")
            results.append({'phone_number': num, 'status': 'failed', 'error': str(e)[:200]})

    bought = [r for r in results if r['status'] == 'bought']
    return jsonify({
        'preview': False,
        'subaccount': acct['name'],
        'bought_count': len(bought),
        'results': results,
        'message': f'Bought and configured {len(bought)} of {len(found)} number(s) under {acct["name"]}, '
                   f'added in Day-1 warmup.'
    })


@app.route('/api/numbers/<int:nid>/warmup', methods=['POST'])
def api_update_warmup(nid):
    data        = request.get_json() or {}
    warmup_mode = int(data.get('warmup_mode', 0))
    daily_limit = int(data.get('daily_limit', 500))
    db.update_number_warmup(nid, warmup_mode, daily_limit)
    return jsonify({'status': 'updated'})


@app.route('/api/brands', methods=['GET'])
def api_get_brands():
    return jsonify(db.get_brands())


@app.route('/api/numbers/<int:nid>', methods=['DELETE'])
def api_deactivate_number(nid):
    db.deactivate_number(nid)
    return jsonify({'status': 'deactivated'})


@app.route('/api/numbers/<int:nid>/reactivate', methods=['POST'])
def api_reactivate_number(nid):
    db.reactivate_number(nid)
    return jsonify({'status': 'reactivated'})


# ── Settings API ──────────────────────────────────────────────────────────────

@app.route('/api/settings', methods=['GET'])
def api_get_settings():
    schedule = db.get_send_schedule()
    return jsonify({
        'base_url':      db.get_setting('base_url', ''),
        'send_delay':    db.get_setting('send_delay', '1.0'),
        'send_rate_mps': db.get_setting('send_rate_mps', '0.2'),
        'global_pause':  db.get_setting('global_pause', '0'),
        'max_workers':   db.get_setting('max_workers', '16'),
        'start_hour':    schedule['start_hour'],
        'end_hour':      schedule['end_hour'],
        'send_days':     schedule['send_days'],
        'timezone':      schedule['timezone'],
    })


@app.route('/api/settings', methods=['POST'])
def api_save_settings():
    data = request.get_json() or {}
    for key in ('base_url', 'send_delay'):
        if key in data:
            db.set_setting(key, str(data[key]).strip())

    # New engine controls (all live-adjustable)
    if 'send_rate_mps' in data:
        try:
            rate = float(data['send_rate_mps'])
            rate = min(10.0, max(0.01, rate))   # clamp to a sane 0.01–10 msg/sec per number
            db.set_setting('send_rate_mps', str(rate))
        except (ValueError, TypeError):
            pass
    if 'global_pause' in data:
        db.set_setting('global_pause', '1' if str(data['global_pause']).strip() in ('1', 'true', 'True', 'on') else '0')
    if 'max_workers' in data:
        try:
            db.set_setting('max_workers', str(max(1, min(64, int(data['max_workers'])))))
        except (ValueError, TypeError):
            pass

    if 'start_hour' in data:
        db.set_setting('send_start_hour', str(int(data['start_hour'])))
    if 'end_hour' in data:
        db.set_setting('send_end_hour', str(int(data['end_hour'])))
    if 'send_days' in data:
        days = ','.join(str(d) for d in data['send_days'])
        db.set_setting('send_days', days)
    if 'timezone' in data:
        db.set_setting('send_timezone', str(data['timezone']).strip())
    return jsonify({'status': 'saved'})


# ── Twilio Webhooks ───────────────────────────────────────────────────────────

@app.route('/webhook/inbound', methods=['POST'])
def webhook_inbound():
    guard = _webhook_guard(_verify_twilio(), 'inbound')
    if guard:
        return guard
    from_phone = request.form.get('From', '')
    to_number  = request.form.get('To', '')
    body       = request.form.get('Body', '')

    if from_phone and body:
        result = sender.handle_inbound(from_phone, body, to_number)
        logger.info(f"Inbound {from_phone}: '{body[:50]}' → {result}")
        # Opt-out suppresses + cancels; any other reply = engaged lead, so stop the
        # drip (hand to reps) without adding them to DNC.
        if result == 'opted_out':
            sequence.suppress_and_cancel(from_phone, 'stop_reply', 'opted_out_sms',
                                         source='inbound', their_message=body)
        else:
            sequence.mark_replied_and_stop(from_phone)

    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        200,
        {'Content-Type': 'text/xml'}
    )


@app.route('/webhook/status', methods=['POST'])
def webhook_status():
    guard = _webhook_guard(_verify_twilio(), 'status')
    if guard:
        return guard
    sid        = request.form.get('MessageSid', '')
    status     = request.form.get('MessageStatus', '')
    error_code = request.form.get('ErrorCode', '')

    if sid and status:
        db.update_delivery_status(sid, status, error_code)
        # Sequence texts store their SID in `touches` (not contacts) — apply there
        # too, so delivery is tracked and dead numbers get suppressed + cancelled.
        sequence.apply_delivery_status(sid, status, error_code)

        # Auto-DNC permanent failures — dead numbers and landlines
        # 30006 = dead/disconnected, 21614 = landline (can't receive SMS)
        if error_code in ('30006', '21614'):
            # Look up the contact phone from delivery_stats or contacts
            phone = db.get_phone_by_message_sid(sid)
            if phone:
                reason = 'dead_number' if error_code == '30006' else 'landline'
                db.add_to_dnc(phone, reason, 'auto')
                logger.info(f"Auto-DNC ({reason}): {phone} — error {error_code}")

        logger.info(f"Status: {sid} → {status}" + (f" (error {error_code})" if error_code else ''))

    return '', 204


@app.route('/webhook/drop', methods=['POST'])
def webhook_drop():
    """Drop.co (VMDrop) status webhook.

    Drop blind-POSTs JSON drop statuses here and only checks for a 200 response.
    Applies the status to the sequence via rvm.handle_drop_status: sets the lead's
    line type (wireless/landline/dead/blacklist), gates SMS accordingly, and
    removes callers who opted out via IVR. C1/C2 carry lead_id / cohort_id.
    """
    guard = _webhook_guard(_verify_drop(), 'drop')
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    if not data and request.form:
        data = request.form.to_dict()

    try:
        cls = rvm.handle_drop_status(data)
    except Exception:
        logger.exception('[Drop webhook] failed to apply status')
        cls = 'error'

    logger.info(
        "[Drop webhook] DropId=%s status=%s (%s) C1=%s -> %s",
        data.get('DropId'), data.get('DropStatusCode'),
        data.get('DropStatusMessage'), data.get('C1'), cls,
    )
    return '', 200


# ── Dead Numbers Export ───────────────────────────────────────────────────────

@app.route('/api/campaigns/<int:cid>/dead', methods=['GET'])
def api_campaign_dead(cid):
    """Export dead/landline numbers from a campaign as CSV."""
    import csv, io as _io
    c = db.get_campaign(cid)
    if not c:
        return jsonify({'error': 'Not found'}), 404

    dead = db.get_campaign_dead_numbers(cid)
    output = _io.StringIO()
    writer = csv.DictWriter(output, fieldnames=['phone', 'error_code', 'reason'], extrasaction='ignore')
    writer.writeheader()
    writer.writerows(dead)
    return (
        output.getvalue(),
        200,
        {'Content-Type': 'text/csv',
         'Content-Disposition': f'attachment; filename="campaign_{cid}_dead.csv"'}
    )


# ── Delivery Stats API ────────────────────────────────────────────────────────

@app.route('/api/campaigns/<int:cid>/delivery', methods=['GET'])
def api_campaign_delivery(cid):
    """Returns live delivery breakdown for a campaign."""
    c = db.get_campaign(cid)
    if not c:
        return jsonify({'error': 'Not found'}), 404

    stats = db.get_campaign_delivery_stats(cid)

    # Summarize
    delivered   = sum(s['count'] for s in stats if s['delivery_status'] == 'delivered')
    undelivered = sum(s['count'] for s in stats if s['delivery_status'] == 'undelivered')
    failed      = sum(s['count'] for s in stats if s['delivery_status'] == 'failed')
    pending     = c['sent_count'] - delivered - undelivered - failed

    # Error code breakdown
    error_codes = {}
    for s in stats:
        if s['error_code']:
            ec = s['error_code']
            error_codes[ec] = error_codes.get(ec, 0) + s['count']

    return jsonify({
        'sent':         c['sent_count'],
        'delivered':    delivered,
        'undelivered':  undelivered,
        'failed':       failed,
        'pending':      max(0, pending),
        'delivery_pct': round(delivered / c['sent_count'] * 100, 1) if c['sent_count'] > 0 else 0,
        'error_codes':  error_codes,
        'raw':          stats,
    })


# ── Number Health API ─────────────────────────────────────────────────────────

@app.route('/api/numbers/health', methods=['GET'])
def api_numbers_health():
    """Returns health stats for all numbers."""
    numbers = db.get_sending_numbers_list()
    result  = []

    for n in numbers:
        total     = n.get('total_sent', 0)
        delivered = n.get('delivered_count', 0)
        failed    = n.get('failed_count', 0)

        if total > 0:
            delivery_pct = round(delivered / total * 100, 1)
            failure_pct  = round(failed / total * 100, 1)
        else:
            delivery_pct = None
            failure_pct  = None

        # Health badge
        if total < 50 or delivery_pct is None:
            health = 'unknown'
        elif delivery_pct >= 80:
            health = 'healthy'
        elif delivery_pct >= 60:
            health = 'warning'
        else:
            health = 'critical'

        result.append({
            **n,
            'delivery_pct': delivery_pct,
            'failure_pct':  failure_pct,
            'health':       health,
        })

    return jsonify(result)


# ── Scrubber ──────────────────────────────────────────────────────────────────

SCRUB_FOLDER = os.path.join(os.path.dirname(__file__), 'static', 'scrub_results')
os.makedirs(SCRUB_FOLDER, exist_ok=True)

# Track running scrub threads: {job_id: Thread}
scrub_threads: dict[int, threading.Thread] = {}


def _run_scrub(job_id, rows, base_filename, account_sid, auth_token):
    """Background thread: looks up each number and writes result CSVs."""
    from twilio.rest import Client
    from twilio.base.exceptions import TwilioRestException
    from twilio.http.http_client import TwilioHttpClient

    MOBILE_TYPES   = {'mobile', 'voip'}
    LANDLINE_TYPES = {'landline', 'fixed-voip'}

    mobile   = []
    landline = []
    dead     = []
    errors   = []

    try:
        http_client = TwilioHttpClient(timeout=30)
        client = Client(account_sid, auth_token, http_client=http_client)

        for i, row in enumerate(rows, 1):
            phone_e164 = f"+1{row['phone']}"
            try:
                result = client.lookups.v2.phone_numbers(phone_e164).fetch(
                    fields='line_type_intelligence'
                )
                line_type = None
                if result.line_type_intelligence:
                    line_type = result.line_type_intelligence.get('type', 'unknown')

                if line_type in MOBILE_TYPES:
                    mobile.append(row)
                elif line_type in LANDLINE_TYPES:
                    landline.append(row)
                else:
                    # unknown type → treat as mobile (safer)
                    mobile.append(row)

            except TwilioRestException as e:
                if e.code in (20404, 21211, 21612):
                    dead.append(row)
                else:
                    errors.append(row)
                    logger.warning(f"[Scrub {job_id}] Twilio error {e.code} for {row['phone']}: {e}")

            except Exception as e:
                errors.append(row)
                logger.error(f"[Scrub {job_id}] Unexpected error for {row['phone']}: {e}")

            # Update progress every 25 numbers
            if i % 25 == 0 or i == len(rows):
                db.update_scrub_progress(job_id, i, len(mobile), len(landline), len(dead), len(errors))

            time.sleep(0.05)  # rate limiting

        # Write result CSVs
        fieldnames = ['phone', 'first_name']

        def write_csv(suffix, data):
            path = os.path.join(SCRUB_FOLDER, f"{job_id}_{suffix}.csv")
            with open(path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(data)

        write_csv('mobile',   mobile)
        write_csv('landline', landline)
        write_csv('dead',     dead)

        db.complete_scrub_job(job_id, len(mobile), len(landline), len(dead), len(errors))
        logger.info(f"[Scrub {job_id}] Done — mobile={len(mobile)} landline={len(landline)} dead={len(dead)}")

    except Exception as e:
        logger.exception(f"[Scrub {job_id}] Thread crashed: {e}")
        db.fail_scrub_job(job_id, str(e))


@app.route('/api/scrub', methods=['POST'])
def api_start_scrub():
    try:
        if 'csv_file' not in request.files:
            return jsonify({'error': 'CSV file required'}), 400

        csv_file = request.files['csv_file']
        if not csv_file.filename or not allowed(csv_file.filename, ALLOWED_CSV_EXT):
            return jsonify({'error': 'A valid .csv file is required'}), 400

        account_id = request.form.get('account_id', '').strip()
        if not account_id:
            return jsonify({'error': 'Select a Twilio account to use for lookups'}), 400

        # Get account credentials
        accounts = db.get_accounts()
        account = next((a for a in accounts if str(a['id']) == str(account_id)), None)
        if not account:
            return jsonify({'error': 'Account not found'}), 400

        # Parse CSV
        raw = csv_file.read().decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(raw))

        if not reader.fieldnames:
            return jsonify({'error': 'CSV appears empty or has no header row'}), 400

        phone_col = next(
            (h for h in reader.fieldnames
             if h.strip().lower() in ('phone','phone_number','telephone','mobile','cell')),
            None
        )
        if not phone_col:
            return jsonify({
                'error': f'No phone column found. Your CSV has: {", ".join(reader.fieldnames)}. Rename to "phone".'
            }), 400

        name_col = next(
            (h for h in reader.fieldnames
             if h.strip().lower() in ('first_name','firstname','name','first')),
            None
        )

        rows = []
        seen = set()
        for row in reader:
            phone = db.normalize_phone(row.get(phone_col, ''))
            name  = (row.get(name_col, '') or '').strip() if name_col else ''
            if len(phone) == 10 and phone not in seen:
                seen.add(phone)
                rows.append({'phone': phone, 'first_name': name})

        if not rows:
            return jsonify({'error': 'No valid 10-digit numbers found in CSV'}), 400

        base_filename = os.path.splitext(csv_file.filename)[0]
        job_id = db.create_scrub_job(base_filename, len(rows))

        # Start background thread
        t = threading.Thread(
            target=_run_scrub,
            args=(job_id, rows, base_filename, account['account_sid'], account['auth_token']),
            daemon=True,
            name=f'scrub-{job_id}'
        )
        scrub_threads[job_id] = t
        t.start()

        return jsonify({
            'job_id':         job_id,
            'total':          len(rows),
            'estimated_cost': round(len(rows) * 0.005, 2),
            'message':        f'Scrub started for {len(rows):,} numbers (~${len(rows)*0.005:.2f})'
        })

    except Exception as e:
        logger.exception('Error starting scrub')
        return jsonify({'error': str(e)}), 500


@app.route('/api/scrub', methods=['GET'])
def api_get_scrub_jobs():
    return jsonify(db.get_scrub_jobs())


@app.route('/api/scrub/<int:job_id>', methods=['GET'])
def api_get_scrub_job(job_id):
    job = db.get_scrub_job(job_id)
    if not job:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(job)


@app.route('/api/scrub/<int:job_id>/download/<scrub_type>', methods=['GET'])
def api_download_scrub(job_id, scrub_type):
    if scrub_type not in ('mobile', 'landline', 'dead'):
        return jsonify({'error': 'Invalid type — must be mobile, landline, or dead'}), 400

    job = db.get_scrub_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    if job['status'] != 'completed':
        return jsonify({'error': 'Job not completed yet'}), 400

    path = os.path.join(SCRUB_FOLDER, f"{job_id}_{scrub_type}.csv")
    if not os.path.exists(path):
        return jsonify({'error': 'File not found'}), 404

    base = job['filename']
    suffix = {'mobile': 'mobile_ready', 'landline': 'landline_ready', 'dead': 'DEAD'}[scrub_type]
    download_name = f"{base}_{suffix}.csv"

    with open(path, 'r') as f:
        content = f.read()

    return (
        content,
        200,
        {'Content-Type': 'text/csv',
         'Content-Disposition': f'attachment; filename="{download_name}"'}
    )


# ── Overview / Analytics API ──────────────────────────────────────────────────

@app.route('/api/admin/cleanup', methods=['POST'])
def api_cleanup():
    """Free up disk space — delete old scrub result files and vacuum DB."""
    import glob
    removed = 0
    # Delete scrub result CSVs older than 7 days
    scrub_folder = os.path.join(os.path.dirname(__file__), 'static', 'scrub_results')
    if os.path.exists(scrub_folder):
        import time
        now = time.time()
        for f in glob.glob(os.path.join(scrub_folder, '*.csv')):
            if os.path.getmtime(f) < now - 7 * 86400:
                os.remove(f)
                removed += 1

    # Vacuum + measure the DB file (SQLite only; Postgres autovacuums)
    db_size = 0
    if not db.IS_PG:
        conn = db.get_db()
        conn.execute('VACUUM')
        conn.close()
        db_size = os.path.getsize(db.DB_PATH) / (1024 * 1024)

    return jsonify({
        'status': 'ok',
        'scrub_files_removed': removed,
        'db_size_mb': round(db_size, 2)
    })


# ── Sent Contacts Export ──────────────────────────────────────────────────────

@app.route('/api/campaigns/<int:cid>/export-sent', methods=['GET'])
def api_export_campaign_sent(cid):
    """Export all sent contacts for a specific campaign as CSV."""
    import csv, io as _io
    c = db.get_campaign(cid)
    if not c:
        return jsonify({'error': 'Not found'}), 404

    conn = db.get_db()
    rows = conn.execute('''
        SELECT
            c.phone,
            c.first_name,
            c.status,
            c.sent_at,
            ca.name as campaign_name,
            ca.brand
        FROM contacts c
        JOIN campaigns ca ON ca.id = c.campaign_id
        WHERE c.campaign_id = ?
        AND c.status IN ('sent', 'delivered', 'undelivered', 'failed')
        ORDER BY c.sent_at ASC
    ''', (cid,)).fetchall()
    conn.close()

    output = _io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['phone', 'first_name', 'status', 'sent_at', 'campaign_name', 'brand'])
    for row in rows:
        writer.writerow([row[0], row[1], row[2], row[3], row[4], row[5]])

    campaign_name = c['name'].replace(' ', '_')[:40]
    return (
        output.getvalue(),
        200,
        {'Content-Type': 'text/csv',
         'Content-Disposition': f'attachment; filename="campaign_{cid}_{campaign_name}_sent.csv"'}
    )


@app.route('/api/export/all-sent', methods=['GET'])
def api_export_all_sent():
    """Export ALL sent contacts across ALL campaigns as one CSV.
    Optional filters: ?start=YYYY-MM-DD&end=YYYY-MM-DD&brand=Liberty+Choice
    """
    import csv, io as _io
    start = request.args.get('start', '').strip()
    end   = request.args.get('end', '').strip()
    brand = request.args.get('brand', '').strip()

    query = '''
        SELECT
            c.phone,
            c.first_name,
            c.status,
            c.sent_at,
            ca.name as campaign_name,
            ca.brand,
            ca.id as campaign_id
        FROM contacts c
        JOIN campaigns ca ON ca.id = c.campaign_id
        WHERE c.status IN ('sent', 'delivered', 'undelivered', 'failed')
    '''
    params = []

    if start:
        query += ' AND c.sent_at >= ?'
        params.append(start)
    if end:
        query += ' AND c.sent_at <= ?'
        params.append(end + ' 23:59:59')
    if brand:
        query += ' AND ca.brand = ?'
        params.append(brand)

    query += ' ORDER BY c.sent_at ASC'

    conn = db.get_db()
    rows = conn.execute(query, params).fetchall()
    conn.close()

    output = _io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['phone', 'first_name', 'status', 'sent_at', 'campaign_name', 'brand', 'campaign_id'])
    for row in rows:
        writer.writerow([row[0], row[1], row[2], row[3], row[4], row[5], row[6]])

    filename = 'all_sent'
    if start:
        filename += f'_{start}'
    if end:
        filename += f'_to_{end}'
    if brand:
        filename += f'_{brand.replace(" ", "_")}'

    return (
        output.getvalue(),
        200,
        {'Content-Type': 'text/csv',
         'Content-Disposition': f'attachment; filename="{filename}.csv"'}
    )


@app.route('/api/stats/overview', methods=['GET'])
def api_stats_overview():
    return jsonify(db.get_overview_stats())


@app.route('/api/stats/daily', methods=['GET'])
def api_stats_daily():
    days = int(request.args.get('days', 10))
    return jsonify(db.get_daily_stats(days))


@app.route('/api/stats/states', methods=['GET'])
def api_stats_states():
    return jsonify(db.get_state_distribution())


@app.route('/api/stats/costs', methods=['GET'])
def api_stats_costs():
    return jsonify(db.get_campaign_costs())


# ── Sequence: Message Pools API ───────────────────────────────────────────────

@app.route('/api/pools/templates', methods=['GET'])
def api_get_templates():
    stage = request.args.get('stage') or None
    return jsonify(sequence.get_templates(stage=stage))


@app.route('/api/pools/templates', methods=['POST'])
def api_add_template():
    data  = request.get_json() or {}
    stage = (data.get('stage') or '').strip()
    body  = (data.get('body') or '').strip()
    if stage not in sequence.STAGES:
        return jsonify({'error': f'stage must be one of {", ".join(sequence.STAGES)}'}), 400
    if not body:
        return jsonify({'error': 'Template body required'}), 400
    tid = sequence.add_template(stage, body, weight=int(data.get('weight', 1) or 1))
    return jsonify({'id': tid, 'status': 'added'})


@app.route('/api/pools/templates/<int:tid>', methods=['PUT'])
def api_update_template(tid):
    data = request.get_json() or {}
    sequence.update_template(tid, body=data.get('body'),
                             active=data.get('active'), weight=data.get('weight'))
    return jsonify({'status': 'updated'})


@app.route('/api/pools/callbacks', methods=['GET'])
def api_get_callbacks():
    return jsonify(sequence.get_callbacks())


@app.route('/api/pools/callbacks', methods=['POST'])
def api_add_callback():
    data   = request.get_json() or {}
    number = db.normalize_phone(data.get('number', ''))
    if len(number) != 10:
        return jsonify({'error': 'Enter a valid 10-digit number'}), 400
    cid = sequence.add_callback(number, notes=data.get('notes', ''))
    return jsonify({'id': cid, 'status': 'added'})


@app.route('/api/pools/callbacks/<int:cid>', methods=['PUT'])
def api_update_callback(cid):
    data = request.get_json() or {}
    num  = data.get('number')
    if num is not None and len(db.normalize_phone(num)) != 10:
        return jsonify({'error': 'Enter a valid 10-digit number'}), 400
    sequence.update_callback(cid, number=num, active=data.get('active'), notes=data.get('notes'))
    return jsonify({'status': 'updated'})


@app.route('/api/pools/agents', methods=['GET'])
def api_get_seq_agents():
    return jsonify(sequence.get_agents())


@app.route('/api/pools/agents', methods=['POST'])
def api_add_agent():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Agent name required'}), 400
    aid = sequence.add_agent(name)
    return jsonify({'id': aid, 'status': 'added'})


@app.route('/api/pools/agents/<int:aid>', methods=['PUT'])
def api_update_agent(aid):
    data = request.get_json() or {}
    sequence.update_agent(aid, name=data.get('name'), active=data.get('active'))
    return jsonify({'status': 'updated'})


@app.route('/api/pools/audio', methods=['GET'])
def api_get_audio():
    return jsonify(sequence.get_audio())


@app.route('/api/pools/audio', methods=['POST'])
def api_add_audio():
    data  = request.get_json() or {}
    label = (data.get('label') or '').strip()
    url   = (data.get('url') or '').strip()
    if not label or not url:
        return jsonify({'error': 'Label and audio URL are required'}), 400
    aid = sequence.add_audio(label, url, run_mapping=(data.get('run_mapping') or '').strip())
    return jsonify({'id': aid, 'status': 'added'})


@app.route('/api/pools/audio/<int:aid>', methods=['PUT'])
def api_update_audio(aid):
    data = request.get_json() or {}
    sequence.update_audio(aid, label=data.get('label'), url=data.get('url'),
                          run_mapping=data.get('run_mapping'), active=data.get('active'))
    return jsonify({'status': 'updated'})


# ── Sequence: Cohorts API ─────────────────────────────────────────────────────

@app.route('/api/cohorts', methods=['GET'])
def api_get_cohorts():
    return jsonify(sequence.get_cohorts())


@app.route('/api/cohorts/upload', methods=['POST'])
def api_upload_cohort():
    try:
        name          = request.form.get('name', '').strip()
        brand         = request.form.get('brand', '').strip()
        texts_per_day = int(request.form.get('texts_per_day', 2) or 2)
        sms_rate      = int(request.form.get('sms_rate', 12000) or 12000)
        start_date    = request.form.get('start_date', '').strip() or None

        if not name:
            return jsonify({'error': 'Cohort name required'}), 400
        if 'csv_file' not in request.files:
            return jsonify({'error': 'CSV file required'}), 400

        csv_file = request.files['csv_file']
        if not csv_file.filename or not allowed(csv_file.filename, ALLOWED_CSV_EXT):
            return jsonify({'error': 'A valid .csv file is required'}), 400

        raw    = csv_file.read().decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(raw))
        if reader.fieldnames is None:
            return jsonify({'error': 'CSV appears empty or has no header row'}), 400

        rows = list(reader)
        if not rows:
            return jsonify({'error': 'CSV has no data rows'}), 400

        res = sequence.enroll_cohort(name, rows, brand=brand, start_date=start_date,
                                     texts_per_day=texts_per_day, sms_rate=sms_rate)
        res['message'] = f"Cohort created — {res['loaded']:,} leads enrolled."
        return jsonify(res)
    except Exception as e:
        logger.exception('Error uploading cohort')
        return jsonify({'error': str(e)}), 500


@app.route('/api/cohorts/<int:cid>/leads', methods=['GET'])
def api_get_cohort_leads(cid):
    try:
        limit  = max(1, min(500, int(request.args.get('limit', 100))))
        offset = max(0, int(request.args.get('offset', 0)))
    except (ValueError, TypeError):
        return jsonify({'error': 'limit and offset must be integers'}), 400
    return jsonify(sequence.get_cohort_leads(cid, limit, offset))


@app.route('/api/leads/<int:lead_id>/plan', methods=['GET'])
def api_get_lead_plan(lead_id):
    plan = sequence.get_lead_plan(lead_id)
    if not plan:
        return jsonify({'error': 'Lead not found'}), 404
    return jsonify(plan)


@app.route('/api/activity', methods=['GET'])
def api_activity():
    """The tracking feed — every RVM + SMS drop with its status/delivery."""
    try:
        limit  = int(request.args.get('limit', 100))
        offset = int(request.args.get('offset', 0))
    except (ValueError, TypeError):
        return jsonify({'error': 'limit and offset must be integers'}), 400
    touch_type = request.args.get('type') or None
    status     = request.args.get('status') or None
    phone      = request.args.get('phone') or None
    cohort_id  = request.args.get('cohort_id') or None
    try:
        cohort_id = int(cohort_id) if cohort_id else None
    except (ValueError, TypeError):
        cohort_id = None
    return jsonify(sequence.get_activity(
        touch_type=touch_type, status=status, cohort_id=cohort_id,
        phone=phone, limit=limit, offset=offset))


@app.route('/api/command', methods=['GET'])
def api_command():
    """Live headline numbers + engine status for the Command home."""
    data = sequence.get_command_summary()
    data['engine'] = {
        'rvm_dry_run':       rvm.is_dry_run(),
        'sms_dry_run':       pacer.is_dry_run(),
        'sms_paused':        pacer.is_paused(),
        'scheduler_enabled': scheduler.is_enabled(),
        'drop_campaign_token': bool(db.get_setting('drop_campaign_token', '')),
        'in_window':         sequence.within_send_window(),
    }
    return jsonify(data)


# ── Sequence: RVM engine + suppression (Phase 2) ──────────────────────────────

def _truthy(v):
    return str(v).strip().lower() in ('1', 'true', 'on', 'yes')


@app.route('/api/engine/settings', methods=['GET'])
def api_get_engine_settings():
    return jsonify({
        'drop_campaign_token': db.get_setting('drop_campaign_token', ''),
        'rvm_dry_run':         rvm.is_dry_run(),
        'sms_dry_run':         pacer.is_dry_run(),
        'sms_paused':          pacer.is_paused(),
        'sms_rate_per_hour':   pacer.rate_per_hour(),
        'sms_texts_per_day':   pacer.texts_per_day(),
        'scheduler_enabled':   scheduler.is_enabled(),
        'seq_window_start':    int(db.get_setting('seq_window_start', '7')),
        'seq_window_end':      int(db.get_setting('seq_window_end', '21')),
    })


@app.route('/api/engine/settings', methods=['POST'])
def api_save_engine_settings():
    data = request.get_json() or {}
    if 'drop_campaign_token' in data:
        db.set_setting('drop_campaign_token', str(data['drop_campaign_token']).strip())
    if 'rvm_dry_run' in data:
        db.set_setting('rvm_dry_run', '1' if _truthy(data['rvm_dry_run']) else '0')
    if 'sms_dry_run' in data:
        db.set_setting('sms_dry_run', '1' if _truthy(data['sms_dry_run']) else '0')
    if 'sms_paused' in data:
        db.set_setting('sms_paused', '1' if _truthy(data['sms_paused']) else '0')
    if 'sms_rate_per_hour' in data:
        try:
            db.set_setting('sms_rate_per_hour', str(max(1, int(data['sms_rate_per_hour']))))
        except (ValueError, TypeError):
            pass
    if 'sms_texts_per_day' in data:
        try:
            db.set_setting('sms_texts_per_day', '1' if int(data['sms_texts_per_day']) <= 1 else '2')
        except (ValueError, TypeError):
            pass
    if 'scheduler_enabled' in data:
        db.set_setting('scheduler_enabled', '1' if _truthy(data['scheduler_enabled']) else '0')
    if 'seq_window_start' in data:
        try:
            db.set_setting('seq_window_start', str(max(0, min(23, int(data['seq_window_start'])))))
        except (ValueError, TypeError):
            pass
    if 'seq_window_end' in data:
        try:
            db.set_setting('seq_window_end', str(max(1, min(24, int(data['seq_window_end'])))))
        except (ValueError, TypeError):
            pass
    return jsonify({
        'status':              'saved',
        'rvm_dry_run':         rvm.is_dry_run(),
        'drop_campaign_token': db.get_setting('drop_campaign_token', ''),
        'sms_dry_run':         pacer.is_dry_run(),
        'sms_paused':          pacer.is_paused(),
        'sms_rate_per_hour':   pacer.rate_per_hour(),
        'sms_texts_per_day':   pacer.texts_per_day(),
        'scheduler_enabled':   scheduler.is_enabled(),
        'seq_window_start':    int(db.get_setting('seq_window_start', '7')),
        'seq_window_end':      int(db.get_setting('seq_window_end', '21')),
    })


@app.route('/api/rvm/dispatch', methods=['POST'])
def api_rvm_dispatch():
    """Fire all currently-due RVM touches. Honors the dry-run switch."""
    with sequence.dispatch_lock:
        return jsonify(rvm.dispatch_due_rvms())


@app.route('/api/sms/dispatch', methods=['POST'])
def api_sms_dispatch():
    """Send currently-due SMS touches. Honors pause / dry-run / rate / texts-per-day."""
    data = request.get_json(silent=True) or {}
    try:
        limit = int(data['limit']) if data.get('limit') else None
    except (ValueError, TypeError):
        limit = None
    with sequence.dispatch_lock:
        return jsonify(pacer.dispatch_due_sms(limit=limit))


@app.route('/api/cleanup/run', methods=['POST'])
def api_cleanup_run():
    """Manually run the end-of-day cleanup (complete/advance + cancel missed)."""
    return jsonify(sequence.run_daily_cleanup())


@app.route('/api/cohorts/<int:cid>/ledger', methods=['GET'])
def api_cohort_ledger(cid):
    return jsonify(sequence.get_cohort_ledger(cid))


@app.route('/api/cohorts/<int:cid>/nonresponders/export', methods=['GET'])
def api_cohort_nonresponders(cid):
    import csv, io as _io
    rows = sequence.get_cohort_nonresponders(cid)
    output = _io.StringIO()
    writer = csv.DictWriter(output, fieldnames=['first_name', 'last_name', 'phone', 'state', 'amount'],
                            extrasaction='ignore')
    writer.writeheader()
    writer.writerows(rows)
    return (output.getvalue(), 200,
            {'Content-Type': 'text/csv',
             'Content-Disposition': f'attachment; filename="cohort_{cid}_nonresponders.csv"'})


@app.route('/api/cohorts/<int:cid>/outcomes/export', methods=['GET'])
def api_cohort_outcomes_export(cid):
    """The full uploaded list annotated with each lead's final result — the
    end-of-week report (non-responders / opt-outs / called-in / dead / blocked)."""
    import csv, io as _io
    rows = sequence.get_cohort_outcomes_rows(cid)
    output = _io.StringIO()
    fields = ['first_name', 'last_name', 'phone', 'state', 'amount', 'result',
              'line_type', 'status', 'outcome', 'enrolled_at', 'completed_at', 'removed_at']
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(rows)
    return (output.getvalue(), 200,
            {'Content-Type': 'text/csv',
             'Content-Disposition': f'attachment; filename="cohort_{cid}_outcomes.csv"'})


@app.route('/api/suppress', methods=['POST'])
def api_suppress():
    """Manual called-in / opt-out upload. Body: {numbers: "raw text", reason}.
    Each number is added to DNC and its pending sequence touches cancelled."""
    data   = request.get_json() or {}
    raw    = (data.get('numbers') or '').strip()
    reason = (data.get('reason') or 'called_in').strip()
    if not raw:
        return jsonify({'error': 'No numbers provided'}), 400

    outcome_map = {'called_in': 'called_in', 'stop_reply': 'opted_out_sms',
                   'dnc_ivr': 'dnc_ivr', 'manual': 'blocked'}
    outcome = outcome_map.get(reason, 'blocked')

    lines = [l.strip() for l in raw.replace(',', '\n').splitlines() if l.strip()]
    total_leads = total_touches = matched = 0
    for ln in lines:
        res = sequence.suppress_and_cancel(ln, reason, outcome, source='manual')
        total_leads   += res['leads']
        total_touches += res['cancelled_touches']
        if res['leads']:
            matched += 1

    conn = db.get_db()
    conn.execute('INSERT INTO called_in_uploads (source, row_count, matched_count) VALUES (?,?,?)',
                 ('manual', len(lines), matched))
    conn.commit()
    conn.close()

    return jsonify({
        'status':            'done',
        'numbers':           len(lines),
        'leads_removed':     total_leads,
        'touches_cancelled': total_touches,
        'message': f'Suppressed {len(lines):,} number(s); removed {total_leads:,} active lead(s), '
                   f'cancelled {total_touches:,} pending touch(es).'
    })


# ── Connection preflight + Drop campaign create ───────────────────────────────

@app.route('/api/preflight', methods=['POST'])
def api_preflight():
    """Verify everything is wired: Drop balance + each Twilio sub-account's creds."""
    result = {
        'base_url': (db.get_setting('base_url', '') or os.environ.get('APP_URL', '')).strip(),
        'drop_campaign_token': bool(db.get_setting('drop_campaign_token', '')),
        'drop': None,
        'twilio': [],
    }
    try:
        bal = drop.check_balance()
        result['drop'] = {'ok': True, 'balance': bal.get('CurrentBalance'),
                          'pending': bal.get('PendingCost')}
    except Exception as e:
        result['drop'] = {'ok': False, 'error': str(e)[:200]}

    from twilio.rest import Client
    for a in db.get_accounts():
        try:
            acct = Client(a['account_sid'], a['auth_token']).api.accounts(a['account_sid']).fetch()
            result['twilio'].append({'name': a['name'], 'ok': True, 'status': acct.status})
        except Exception as e:
            result['twilio'].append({'name': a['name'], 'ok': False, 'error': str(e)[:200]})
    return jsonify(result)


@app.route('/api/drop/create-campaign', methods=['POST'])
def api_drop_create_campaign():
    """Create a persistent VMDrop campaign on Drop and store its token."""
    data      = request.get_json() or {}
    name      = (data.get('name') or '').strip()
    audio_url = (data.get('audio_url') or '').strip()
    transfer  = (data.get('transfer_number') or '').strip() or None
    ivr       = (data.get('ivr_file_url') or '').strip() or None
    try:
        fwd = int(data.get('callback_forwarding_type', 1) or 1)
    except (ValueError, TypeError):
        fwd = 1
    if not name or not audio_url:
        return jsonify({'error': 'Campaign name and a public audio URL are required'}), 400
    if fwd == 1 and not transfer:
        return jsonify({'error': 'A transfer number is required for immediate transfer (type 1)'}), 400
    if fwd in (2, 3) and not ivr:
        return jsonify({'error': 'An IVR audio URL is required for IVR forwarding (types 2/3)'}), 400
    try:
        resp = drop.create_campaign(name, audio_url, callback_forwarding_type=fwd,
                                    transfer_number=transfer, ivr_file_url=ivr)
        token = resp.get('CampaignToken', '')
        if token:
            db.set_setting('drop_campaign_token', token)
        return jsonify({'status': 'created', 'campaign_token': token,
                        'campaign_id': resp.get('CampaignId'), 'note': resp.get('Results', '')})
    except Exception as e:
        return jsonify({'error': str(e)[:300]}), 502


if __name__ == '__main__':
    # Direct run (python app.py): resume here, then serve.
    _resume_running_campaigns()
    app.run(host='0.0.0.0', port=5000, debug=False)
else:
    # Under gunicorn/wsgi: resume running campaigns at import time.
    _resume_running_campaigns()
