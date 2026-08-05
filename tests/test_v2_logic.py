"""v2 logic tests — lanes, anchors, per-phone timezone, DNC split, carrier switches."""
import os, tempfile, sys
os.environ['DB_PATH'] = os.path.join(tempfile.mkdtemp(), 'v2.db')
os.environ.pop('DATABASE_URL', None); os.environ.pop('DASHBOARD_PASSWORD', None)

import app as appmod                       # initializes both schemas
import database as db, sequence as sq, rvm, pacer, drop

fails = []
def check(name, cond, detail=''):
    print(('  PASS  ' if cond else '  FAIL  ') + name + ('' if cond else f'   <- {detail}'))
    if not cond:
        fails.append(name)

# ── pools ────────────────────────────────────────────────────────────────────
sq.add_template('checking_in', 'Hi [first_name], [agent] here about [amount]. Call [callback]. STOP to opt out')
sq.add_template('following_up', 'Following up [first_name] — [agent] · [callback]. STOP to opt out')
sq.add_template('last_day', 'Last day [first_name]. [agent] · [callback]. STOP to opt out')
sq.add_agent('Amy'); sq.add_callback('5124443322'); sq.add_audio('A', 'https://x/a.wav', '1,3,5')
db.set_setting('seq_window_start', '0'); db.set_setting('seq_window_end', '24')
db.set_setting('rvm_dry_run', '1'); db.set_setting('sms_dry_run', '1')

print('\n── 1. Anchors + one clock ──')
check('ET RVM anchor 8:30 PT', sq.RVM_ANCHOR['ET'] == (8, 30), sq.RVM_ANCHOR['ET'])
check('CT RVM anchor 9:30 PT', sq.RVM_ANCHOR['CT'] == (9, 30), sq.RVM_ANCHOR['CT'])
check('MT/PT anchor 10:30 PT', sq.RVM_ANCHOR['MT'] == (10, 30) and sq.RVM_ANCHOR['PT'] == (10, 30))
check('SMS-only days use same anchors', sq.SMS_ONLY_START == sq.RVM_ANCHOR)

print('\n── 2. Per-phone timezone beats state (the TX address / LA phone case) ──')
check('TX state alone -> CT', sq.timezone_bucket_for('', 'TX') == 'CT')
check('TX addr + LA phone tz -> PT',
      sq.timezone_bucket_for('America/Los_Angeles', 'TX') == 'PT',
      sq.timezone_bucket_for('America/Los_Angeles', 'TX'))
check('America/New_York -> ET', sq.timezone_bucket_for('America/New_York', '') == 'ET')
check('America/Phoenix -> MT', sq.timezone_bucket_for('America/Phoenix', '') == 'MT')
check('blank tz falls back to state', sq.timezone_bucket_for('', 'NY') == 'ET')

print('\n── 3. Carrier normalization ──')
check('T-MOBILE -> tmobile', sq.normalize_carrier('T-MOBILE') == 'tmobile')
check('"T-Mobile USA, Inc." -> tmobile', sq.normalize_carrier('T-Mobile USA, Inc.') == 'tmobile')
check('Verizon Wireless -> verizon', sq.normalize_carrier('Verizon Wireless') == 'verizon')
check('blank stays blank', sq.normalize_carrier('') == '')

print('\n── 4. Lane routing from registry permissions ──')
rows = [
    # A: full sequence
    {'phone_primary': '2135550001', 'first_name': 'Ann', 'state': 'CA', 'total_unsecured': '53557',
     'timezone': 'America/Los_Angeles', 'carrier_name': 'VERIZON', 'line_type': 'MOBILE',
     'sms_ok': 'True', 'rvm_ok': 'True', 'dead_number': 'False', 'validation_status': 'OK',
     'propensity': '546', 'credit_score': '556'},                       # <- pass-through cols
    # B: SMS-only (registry says no RVM — e.g. national DNC)
    {'phone_primary': '2125550002', 'first_name': 'Ben', 'state': 'NY', 'total_unsecured': '20000',
     'timezone': 'America/New_York', 'carrier_name': 'T-MOBILE', 'line_type': 'MOBILE',
     'sms_ok': 'True', 'rvm_ok': 'False', 'dead_number': 'False'},
    # C: RVM-only landline
    {'phone_primary': '3125550003', 'first_name': 'Cal', 'state': 'IL', 'total_unsecured': '31000',
     'timezone': 'America/Chicago', 'carrier_name': 'AT&T', 'line_type': 'LANDLINE',
     'sms_ok': 'False', 'rvm_ok': 'True', 'dead_number': 'False'},
    # rejected: no channel
    {'phone_primary': '3035550004', 'first_name': 'Dee', 'state': 'CO', 'total_unsecured': '9000',
     'sms_ok': 'False', 'rvm_ok': 'False'},
    # rejected: dead
    {'phone_primary': '3055550005', 'first_name': 'Eli', 'state': 'FL', 'total_unsecured': '1000',
     'sms_ok': 'True', 'rvm_ok': 'True', 'dead_number': 'True'},
    # rejected: blacklist via validation_status
    {'phone_primary': '6175550006', 'first_name': 'Fay', 'state': 'MA', 'total_unsecured': '2000',
     'sms_ok': 'True', 'rvm_ok': 'True', 'validation_status': 'BLACKLIST'},
    # rejected: non-US (Toronto 416)
    {'phone_primary': '4165550007', 'first_name': 'Gil', 'state': 'ON', 'total_unsecured': '3000',
     'sms_ok': 'True', 'rvm_ok': 'True'},
]
res = sq.enroll_cohort('V2 Test', rows, brand='Liberty', start_date='2026-07-20')  # a Monday
print('   ', {k: v for k, v in res.items() if k != 'cohort_id'})
cid = res['cohort_id']
check('3 enrolled', res['loaded'] == 3, res['loaded'])
check('lane split A=1 B=1 C=1', (res['lane_a'], res['lane_b'], res['lane_c']) == (1, 1, 1))
check('no_channel rejected', res['no_channel'] == 1)
check('dead rejected', res['dead'] == 1)
check('blacklist rejected', res['blacklisted'] == 1)
check('non-US rejected', res['non_us'] == 1, res['non_us'])

leads = {l['first_name']: l for l in sq.get_cohort_leads(cid)}
check('Ann lane A', leads['Ann']['lane'] == 'A')
check('Ben lane B', leads['Ben']['lane'] == 'B')
check('Cal lane C', leads['Cal']['lane'] == 'C')
check('carrier stored normalized', leads['Ben']['carrier'] == 'tmobile', leads['Ben']['carrier'])

print('\n── 5. Lanes materialize only their own touches ──')
def touches(lead_id):
    conn = db.get_db()
    r = conn.execute("SELECT touch_type, COUNT(*) c FROM touches WHERE lead_id=? GROUP BY touch_type",
                     (lead_id,)).fetchall()
    conn.close()
    return {x['touch_type']: x['c'] for x in r}
ta, tb, tc = touches(leads['Ann']['id']), touches(leads['Ben']['id']), touches(leads['Cal']['id'])
check('A = 3 RVM + 10 SMS', ta == {'rvm': 3, 'sms': 10}, ta)
check('B = 10 SMS, zero RVM', tb == {'sms': 10}, tb)
check('C = 3 RVM, zero SMS', tc == {'rvm': 3}, tc)

print('\n── 6. Pass-through columns survive as merge tags ──')
plan = sq.get_lead_plan(leads['Ann']['id'])
import json as _j
custom = _j.loads(sq.get_lead(leads['Ann']['id'])['custom_fields'])
check('propensity carried through', custom.get('propensity') == '546', custom)
check('amount = total_unsecured', leads['Ann']['amount'] == '53557', leads['Ann']['amount'])
check('[amount] renders in body', '53557' in plan['steps'][1]['message'], plan['steps'][1]['message'])

print('\n── 7. DNC split: customer kills, national/state demotes ──')
check('Customer DNC -> already_client', rvm.classify_rejection('Failed-Customer DNC') == 'already_client')
check('National DNC -> registry_dnc', rvm.classify_rejection('Failed-National DNC') == 'registry_dnc')
check('State DNC -> registry_dnc', rvm.classify_rejection('Failed-State DNC') == 'registry_dnc')
check('Litigator -> blacklist', rvm.classify_rejection('Failed-Litigator') == 'blacklist')
check('unreachable is NOT dead', rvm.classify_drop_status(18, 'Failed-VM Unreachable') == 'unknown')

# live demotion on lane A lead
before = touches(leads['Ann']['id'])
sq.demote_to_sms_only(leads['Ann']['id'], 'dnc_registry')
after = touches(leads['Ann']['id'])
conn = db.get_db()
row = conn.execute("SELECT lane, status FROM leads WHERE id=?", (leads['Ann']['id'],)).fetchone()
pend_sms = conn.execute("SELECT COUNT(*) c FROM touches WHERE lead_id=? AND touch_type='sms' "
                        "AND status IN ('planned','eligible')", (leads['Ann']['id'],)).fetchone()['c']
conn.close()
check('demoted lead now lane B', row['lane'] == 'B', row['lane'])
check('demoted lead still ACTIVE', row['status'] in ('enrolled', 'in_progress'), row['status'])
check('all 10 SMS still pending after demotion', pend_sms == 10, pend_sms)

# a landline that gets a registry DNC has no channel left
sq.demote_to_sms_only(leads['Cal']['id'], 'dnc_registry')
cal = sq.get_lead(leads['Cal']['id'])
check('landline + DNC -> exits no_channel',
      cal['status'] == 'removed' and cal['outcome'] == 'no_channel', dict(cal))

print('\n── 8. Carrier no-text switch holds SMS but not RVM ──')
db.set_setting('sms_excluded_carriers', 'tmobile')
check('excluded set parsed', pacer.excluded_carriers() == {'tmobile'}, pacer.excluded_carriers())
conn = db.get_db()
conn.execute("UPDATE touches SET eligible_at='2020-01-01 00:00:00' WHERE status IN ('planned','eligible')")
conn.commit(); conn.close()
r = pacer.dispatch_due_sms()
print('   ', {k: r[k] for k in ('due', 'sent', 'held_carrier', 'skipped_wireless')})
check('T-Mobile lead held, not sent', r['held_carrier'] >= 10, r['held_carrier'])
conn = db.get_db()
held = conn.execute("SELECT COUNT(*) c FROM touches WHERE lead_id=? AND status IN ('planned','eligible')",
                    (leads['Ben']['id'],)).fetchone()['c']
conn.close()
check('held touches NOT cancelled (resume-able)', held == 10, held)

db.set_setting('sms_excluded_carriers', '')          # flip the switch back off
r2 = pacer.dispatch_due_sms()
check('switch off -> texts resume', r2['sent'] > 0, r2)

print('\n── 9. Upload cutoff ──')
from datetime import datetime as _dt
import pytz as _tz
early = sq.PACIFIC.localize(_dt(2026, 7, 20, 7, 0))    # Mon 7am PT
late  = sq.PACIFIC.localize(_dt(2026, 7, 20, 14, 0))   # Mon 2pm PT
check('before 8am -> starts today', sq.default_start_date(now_pac=early).isoformat() == '2026-07-20',
      sq.default_start_date(now_pac=early))
check('after 8am -> next business day', sq.default_start_date(now_pac=late).isoformat() == '2026-07-21',
      sq.default_start_date(now_pac=late))
fri_late = sq.PACIFIC.localize(_dt(2026, 7, 24, 14, 0))  # Fri 2pm PT
check('Friday afternoon -> Monday', sq.default_start_date(now_pac=fri_late).isoformat() == '2026-07-27',
      sq.default_start_date(now_pac=fri_late))

print('\n── 10. AUDIT: Drop de-dupe would swallow runs 3 and 5 ──')
import inspect
src = inspect.getsource(rvm.dispatch_due_rvms)
check('sequence drops pass allow_duplicates=True', 'allow_duplicates=True' in src,
      'Drop de-dupes 3 days; runs 1/3/5 are 2 days apart -> runs 3+5 silently dropped')
check('post_record defaults to allowing duplicates',
      inspect.signature(drop.post_record).parameters['allow_duplicates'].default is True)

print('\n── 11. AUDIT: no send lands before 8 AM / after 9 PM LOCAL (TCPA) ──')
import pytz
from datetime import datetime as _dtm
ZONES = {'ET': 'America/New_York', 'CT': 'America/Chicago', 'MT': 'America/Denver',
         'PT': 'America/Los_Angeles', 'HT': 'Pacific/Honolulu'}
EXTRA = {'MT': 'America/Phoenix', 'PT': 'America/Anchorage'}
bad = []
for day in (_dtm(2026, 7, 20), _dtm(2026, 1, 20)):          # summer + winter
    for bucket, zname in list(ZONES.items()) + list(EXTRA.items()):
        h, m = sq.RVM_ANCHOR[bucket]
        for offset in (0, 90, 180, 210):                     # RVM, SMS1, SMS2, SMS-only SMS2
            base = sq.PACIFIC.localize(_dtm(day.year, day.month, day.day, h, m))
            local = (base + __import__('datetime').timedelta(minutes=offset)).astimezone(pytz.timezone(zname))
            if local.hour < 8 or local.hour >= 21:
                bad.append(f'{bucket}->{zname} +{offset}m = {local:%H:%M}')
check('every touch lands 8 AM–9 PM local, both seasons', not bad, bad)
check('Hawaii has its own late anchor', sq.timezone_bucket_for('Pacific/Honolulu', '') == 'HT')
check('HI state maps to HT too', sq.timezone_bucket_for('', 'HI') == 'HT')

print('\n── 12. AUDIT: pandas "nan" never reaches a customer ──')
r_nan = sq.enroll_cohort('nan-test', [{
    'phone_primary': '2135550111', 'first_name': 'Nan', 'state': 'CA',
    'total_unsecured': 'nan', 'carrier_name': 'nan', 'timezone': 'nan',
    'line_type': 'nan', 'sms_ok': 'True', 'rvm_ok': 'True', 'notes': 'NaN'}],
    start_date='2026-07-20')
nl = sq.get_cohort_leads(r_nan['cohort_id'])[0]
check('nan amount stored blank', nl['amount'] == '', repr(nl['amount']))
check('nan carrier stored blank', nl['carrier'] == '', repr(nl['carrier']))
nplan = sq.get_lead_plan(nl['id'])
bodies = [s.get('message', '') for s in nplan['steps'] if s['type'] == 'sms']
check('no "nan" rendered into any SMS body', not any('nan' in b.lower() for b in bodies),
      bodies[:1])
check('nan timezone falls back to state', nl['timezone_bucket'] == 'PT', nl['timezone_bucket'])

print('\n── 13. AUDIT: webhook can identify the lead with no C1 and no PhoneTo ──')
conn = db.get_db()
tid = conn.execute("SELECT id, lead_id FROM touches WHERE touch_type='rvm' LIMIT 1").fetchone()
conn.execute("UPDATE touches SET drop_activity_token='AT-AUDIT-1' WHERE id=?", (tid['id'],))
conn.commit(); conn.close()
check('lead resolved from ActivityToken',
      sq.lead_id_for_activity_token('AT-AUDIT-1') == tid['lead_id'])
# the real payload shape: no PhoneTo, empty C1
resolved = rvm.handle_drop_status({'C1': '', 'DropStatusCode': 18,
                                   'DropStatusMessage': 'Failed-VM Unreachable',
                                   'OriginalActivityToken': 'AT-AUDIT-1'})
check('real-shaped webhook classified, not dropped', resolved == 'unknown', resolved)

print('\n── 14. AUDIT: dry-run must not overwrite a landline as wireless ──')
csrc = inspect.getsource(rvm.dispatch_due_rvms)
check('no blanket wireless simulation in dispatch',
      "apply_line_type(r['lead_id'], 'wireless')" not in csrc)

print('\n' + ('ALL v2 TESTS PASSED' if not fails else f'{len(fails)} FAILURE(S): {fails}'))
sys.exit(1 if fails else 0)
