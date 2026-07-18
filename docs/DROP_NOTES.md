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
| `1009` | `Failed-Customer DNC` — number is on the Drop account's DNC | rejected → suppress the lead (`drop_dnc`) |

> ⚠️ Success is **`1038`**, not `1000`. The client accepts both (`DELIVERY_OK_CODES`).
> A rigid `== 1000` check would mark every accepted drop as an error.

**`/VMDropStatus/` — the actual per-drop outcome is in `DropStatusCode` / `DropStatusMessage`:**

| DropStatusCode | Message | Meaning |
|---|---|---|
| `23` | `Failed-RVM Vmail Not Detected` | no reachable mailbox — drop failed |
| `-1` | (null) | never queued (e.g. was Customer-DNC rejected), or not processed yet |

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
