"""Background scheduler (Phase 4).

Runs the three engine jobs on a cadence so the machine operates without manual
"Run now" clicks:
  - RVM dispatch  (fires whatever RVM is due)
  - SMS pacer     (one minute's worth of the operator's hourly rate)
  - daily cleanup (once per weekday at/after 5:01 PM PT)

Safety:
  - Gated by ``scheduler_enabled`` (default OFF) — the loop starts at import but
    does nothing until you switch it on. Tests never enable it.
  - Still honors every per-channel switch (rvm_dry_run, sms_dry_run, sms_paused).
  - Single instance per process. Run the app as ONE process (gunicorn -w 1).
"""
import threading
import time
import logging
from datetime import datetime

import pytz

import database as db
import rvm
import pacer
import sequence  # noqa: F401  (dispatch_lock lives here)

logger = logging.getLogger(__name__)

PACIFIC = pytz.timezone('America/Los_Angeles')
TICK_SECONDS = 60

_lock = threading.Lock()
_started = False


def is_enabled():
    return str(db.get_setting('scheduler_enabled', '0')).strip().lower() in ('1', 'true', 'on', 'yes')


def tick():
    """One scheduler iteration. Safe to call directly (used by tests)."""
    # Serialize with manual "Run now" dispatches so a touch can't be sent twice.
    with sequence.dispatch_lock:
        try:
            rvm.dispatch_due_rvms()
        except Exception:
            logger.exception('[Scheduler] RVM dispatch failed')

        try:
            per_min = max(1, pacer.rate_per_hour() // 60)
            pacer.dispatch_due_sms(limit=per_min)
        except Exception:
            logger.exception('[Scheduler] SMS pacer failed')

    try:
        _maybe_cleanup()
    except Exception:
        logger.exception('[Scheduler] cleanup failed')


def _maybe_cleanup(nowpac=None):
    """Run the daily cleanup once, at/after 5:01 PM Pacific on a weekday."""
    now = nowpac or datetime.now(PACIFIC)
    if now.weekday() >= 5:                      # Sat/Sun
        return False
    if now.hour < 17 or (now.hour == 17 and now.minute < 1):
        return False
    today = now.strftime('%Y-%m-%d')
    if db.get_setting('scheduler_last_cleanup', '') == today:
        return False
    sequence.run_daily_cleanup()
    db.set_setting('scheduler_last_cleanup', today)
    return True


def _loop():
    while True:
        try:
            if is_enabled():
                tick()
        except Exception:
            logger.exception('[Scheduler] loop error')
        time.sleep(TICK_SECONDS)


def start():
    """Start the scheduler loop once. The loop is idle until scheduler_enabled=1."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
        threading.Thread(target=_loop, daemon=True, name='scheduler').start()
        logger.info('[Scheduler] loop started (idle until enabled)')
