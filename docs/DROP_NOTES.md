# Drop.co (VMDrop) — observed API behavior

Drop's public docs don't enumerate status codes or the exact status-lookup URL.
These are captured from **live responses** so we don't re-learn them the hard way.
Last verified: 2026-07.

## Endpoints

| What | Call | Notes |
|---|---|---|
| Send a drop | `POST /Delivery` (params in query string) | Returns `ActivityToken` + `ApiStatusCode`. |
| Per-drop status | `POST /VMDropStatus/` | **Needs the trailing slash AND `ApiKey`** — without either it 404s. |
| Balance | `POST /BalanceCheck` | Returns `CurrentBalance`, `PendingCost`. |

## Status codes

**`/Delivery` `ApiStatusCode` (post-time acceptance):**

| Code | Meaning | We treat it as |
|---|---|---|
| `1038` | `API Post Accepted` — record queued | **success** (accepted) |
| `1000` | `API Success` | success |
| `1009` | `Failed-Customer DNC` — **already a client** (they called in and signed up) | rejected → full kill, outcome `already_client` |
| TBD | `Failed-National DNC` / `Failed-State DNC` | rejected → **demote to SMS-only**, never kill |

> ⚠️ The DNC flavors are NOT interchangeable. **Customer DNC** means an existing
> client, so all marketing stops. **National/state DNC** leads opted in through our
> own form, so SMS continues — only RVM stops (`rvm.classify_rejection` enforces
> this: only an explicit *customer* DNC kills).

> ⚠️ Success is **`1038`**, not `1000`. The client accepts both (`DELIVERY_OK_CODES`).
> A rigid `== 1000` check would mark every accepted drop as an error.

**`/VMDropStatus/` — the actual per-drop outcome is in `DropStatusCode` / `DropStatusMessage`:**

| DropStatusCode | Message | Meaning |
|---|---|---|
| `18` | `Failed-VM Unreachable` | RVM couldn't drop this attempt |
| `23` | `Failed-RVM Vmail Not Detected` | no reachable mailbox — drop failed |
| `-1` | (null) | never queued (e.g. Customer-DNC rejected), or not processed yet |

> ⚠️ `18` and `23` are RVM **delivery** failures, not dead-number signals — the
> number may be a fine wireless line that just didn't take a voicemail. We map
> them to `unknown` (keep the lead; retry RVM next run) and deliberately do NOT
> treat "unreachable" as dead.

The lookup's own `ApiStatusCode` varies between `1000` and `1038`; **don't gate on it** —
read `DropStatusCode`/`DropStatusMessage` directly (the client uses `_request`, no gate).

## Carrier / line-type — important

Drop reports the **drop outcome** (accepted / DNC / vmail-not-detected / …). In the
responses we've captured it does **not** return a carrier name (e.g. "T-Mobile") or a
clean mobile-vs-landline flag. Treat Drop as the source of truth for *"did the
voicemail land / was it blocked,"* **not** for line-type cleaning.

For real line-type / carrier classification (wireless / landline / dead), use the
**Twilio Lookup scrubber** (Scrubber tab). `rvm.classify_drop_status` still keyword-
matches `DropStatusMessage` for any landline/dead/callback wording Drop does send via
the webhook, and `DROP_STATUS_MAP` can be filled in as more codes are observed.

## Delivery webhook (verified live 2026-07)

Set **account-level** at Drop → **Customer Profile → "web hook url"** (enable webhooks);
it applies to every campaign. Point it at `…/webhook/drop?token=<DROP_WEBHOOK_TOKEN>`.

A real captured payload (a `Failed-VM Unreachable` outcome):

```json
{
  "ApiStatusCode": 1000, "ApiStatusMessage": "API Success",
  "CampaignId": "69104", "DropId": "81e45d50-…",
  "DropStatusCode": 18, "DropStatusMessage": "Failed-VM Unreachable",
  "OriginalActivityToken": "5c49b6b8-…", "ResponseCase": "Nine",
  "C1": "", "C2": "", "C3": "", "C4": "", "C5": "",
  "Source": "relay-webhook-test", "ValidationLevel": -1
}
```

**Confirmed: the real webhook payload has NO `Carrier` field** (and no line-type). Carrier
exists only in Drop's dashboard export. Use `C1`/`C2` (lead_id / cohort_id) to route the
status back to a lead. `OriginalActivityToken` ties it to the `/Delivery` `ActivityToken`.
The app captures the last ~25 raw payloads (Engine → "Show recent Drop webhook events").
