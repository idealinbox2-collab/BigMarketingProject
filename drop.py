"""Drop.co (VMDrop) ringless-voicemail API client.

Thin wrapper over the Drop Customer API at https://customerapi.drop.co. Every
endpoint is an HTTP POST with parameters in the query string and a JSON response;
success is signaled by ``ApiStatusCode == 1000`` in the body (not by HTTP status
alone). The API key is read from the ``DROP_API_KEY`` environment variable and is
never logged.

Docs: https://apidocs.drop.co

This module is intentionally side-effect free — it does not touch the database.
Higher layers (the RVM dispatcher, added in a later phase) own persistence and
sequence logic.
"""
import os
import logging

import requests

logger = logging.getLogger(__name__)

BASE_URL = os.environ.get('DROP_BASE_URL', 'https://customerapi.drop.co').rstrip('/')
API_SUCCESS_CODE = 1000        # generic "API Success" (status lookups, campaign create, balance)
POST_ACCEPTED_CODE = 1038      # /Delivery: record accepted into the campaign queue
# ApiStatusCodes that mean a /Delivery record was taken (queued). Anything else
# (e.g. 1009 "Failed-Customer DNC") is a business rejection the caller records —
# NOT a transport error. Verified against live Drop responses 2026-07.
DELIVERY_OK_CODES = (API_SUCCESS_CODE, POST_ACCEPTED_CODE)
DEFAULT_TIMEOUT = 30


class DropError(Exception):
    """Raised on transport failure or a non-success ApiStatusCode from Drop."""

    def __init__(self, message, status_code=None, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


def _api_key(explicit=None):
    key = (explicit or os.environ.get('DROP_API_KEY', '')).strip()
    if not key:
        raise DropError('DROP_API_KEY is not set')
    return key


def _redacted(params):
    """Copy of params with the API key masked, for safe logging."""
    safe = dict(params)
    if 'ApiKey' in safe:
        safe['ApiKey'] = '***'
    return safe


def _request(path, params, timeout=DEFAULT_TIMEOUT):
    """POST to a Drop endpoint (params in the query string) and return parsed JSON.

    Raises DropError only on a transport failure or a non-JSON body — the
    ApiStatusCode is left for the caller to interpret, so a business outcome
    (accepted, DNC, etc.) is data, not an exception.
    """
    url = f"{BASE_URL}/{path.lstrip('/')}"
    try:
        resp = requests.post(url, params=params, timeout=timeout)
    except requests.RequestException as e:
        raise DropError(f'Drop request to {path} failed: {e}')

    try:
        return resp.json()
    except ValueError:
        raise DropError(
            f'Drop {path} returned non-JSON (HTTP {resp.status_code})',
            status_code=resp.status_code, payload=resp.text[:500],
        )


def _post(path, params, ok_codes=DELIVERY_OK_CODES, timeout=DEFAULT_TIMEOUT):
    """_request + raise DropError unless ApiStatusCode is in ok_codes.

    Used by control endpoints (create/balance/stats) that must succeed cleanly.
    Drop uses both 1000 ("API Success") and 1038 ("API Post Accepted") as ok, so
    both are accepted by default.
    """
    data = _request(path, params, timeout=timeout)
    code = data.get('ApiStatusCode')
    if code not in ok_codes:
        raise DropError(
            f"Drop {path} error: {data.get('ApiStatusMessage', 'unknown')} (code {code})",
            status_code=code, payload=data,
        )
    return data


# ── Endpoints ────────────────────────────────────────────────────────────────

def create_campaign(name, audio_url, callback_forwarding_type=1,
                    transfer_number=None, ivr_file_url=None,
                    enable_missed_call=True, rep_id=None, api_key=None):
    """Create a VMDrop campaign. Returns the parsed response (incl. CampaignToken).

    Campaign creation is asynchronous on Drop's side — allow up to ~10 minutes for
    it to finalize before posting records. Per the design, we create a small set of
    persistent campaigns (one per callback-forwarding configuration) and reuse them
    across cohorts, overriding the audio per record.

    callback_forwarding_type:
        1 = immediate transfer          (requires transfer_number)
        2 = IVR + transfer + DNC option (requires ivr_file_url)
        3 = IVR recording only          (requires ivr_file_url)
    """
    params = {
        'ApiKey': _api_key(api_key),
        'VMDropName': name,
        'VMDropFileUrl': audio_url,
        'EnableMissedCall': str(bool(enable_missed_call)).lower(),
        'CallbackForwardingType': int(callback_forwarding_type),
    }
    if transfer_number:
        params['TransferNumber'] = transfer_number
    if ivr_file_url:
        params['IVRFileUrl'] = ivr_file_url
    if rep_id:
        params['RepId'] = rep_id
    logger.info("[Drop] Create campaign '%s' (forwarding=%s)", name, callback_forwarding_type)
    return _post('/VMDropCreate', params)


def post_record(campaign_token, phone_to, audio_url=None, allow_duplicates=False,
                source=None, ip_address=None, custom=None, api_key=None):
    """Post one phone into a campaign's drop queue (/Delivery).

    Returns the parsed response, which includes the ``ActivityToken`` used for
    per-record status lookups.

    custom: dict of C1..C5 informational fields. We use these to tie a drop back to
    a lead (e.g. {'C1': lead_id, 'C2': cohort_id}) so the status webhook can route.
    ``audio_url`` overrides the campaign's saved recording for this record.
    """
    params = {
        'ApiKey': _api_key(api_key),
        'CampaignToken': campaign_token,
        'PhoneTo': phone_to,
        'AllowDuplicates': str(bool(allow_duplicates)).lower(),
    }
    if audio_url:
        params['Audio'] = audio_url
    if source:
        params['Source'] = source
    if ip_address:
        params['IPAddress'] = ip_address
    if custom:
        for slot in ('C1', 'C2', 'C3', 'C4', 'C5'):
            val = custom.get(slot)
            if val not in (None, ''):
                params[slot] = str(val)
    # Do NOT raise on a business ApiStatusCode: 1038 = accepted, 1009 =
    # "Failed-Customer DNC", etc. The caller inspects ``accepted`` + the code.
    data = _request('/Delivery', params)
    data['accepted'] = data.get('ApiStatusCode') in DELIVERY_OK_CODES
    return data


def get_status(activity_token, api_key=None):
    """Status of one drop by its ActivityToken (/VMDropStatus/).

    NOTE (verified live 2026-07): this endpoint needs the trailing slash AND the
    ApiKey — without either it 404s. Its ApiStatusCode varies (1000 / 1038), so
    we don't gate on it — the per-drop outcome is in DropStatusCode /
    DropStatusMessage, which the caller reads directly.
    """
    return _request('/VMDropStatus/', {
        'ActivityToken': activity_token,
        'ApiKey': _api_key(api_key),
    })


def get_campaign_stats(campaign_token, date_from, date_to, api_key=None):
    """Campaign totals over a date range (/VMDropStats). Dates like '10/15/2019'."""
    return _post('/VMDropStats', {
        'ApiKey': _api_key(api_key),
        'CampaignToken': campaign_token,
        'DateFrom': date_from,
        'DateTo': date_to,
    })


def get_activity_stats(campaign_token, date_from, date_to, api_key=None):
    """Validation-level activity breakdown (/VMDropActivityStats)."""
    return _post('/VMDropActivityStats', {
        'ApiKey': _api_key(api_key),
        'CampaignToken': campaign_token,
        'DateFrom': date_from,
        'DateTo': date_to,
    })


def check_balance(api_key=None):
    """Account balance (/BalanceCheck) — CurrentBalance / PendingCost."""
    return _post('/BalanceCheck', {'ApiKey': _api_key(api_key)})
