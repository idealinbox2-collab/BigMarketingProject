# Marketing Automation Platform — Design Spec

**Status:** Draft v1 for review · **Owner:** (you) · **Author:** working draft
**Scope:** The full multi-touch RVM + SMS sequence engine ("the machine"), built on top of the existing internal SMS platform and the external Drop.co (VMDrop) RVM API.

> This document is the source of truth for the design. Nothing is built yet.
> Read **§13 Open Decisions** first — those are the points where I made an
> assumption and need a yes/no before code. Everything else reflects what we
> agreed in discussion.

---

## Table of Contents
1. [Goal & Guiding Principles](#1-goal--guiding-principles)
2. [Vocabulary](#2-vocabulary)
3. [The Sequence (the 5-run cycle)](#3-the-sequence-the-5-run-cycle)
4. [Timezone & Scheduling Math](#4-timezone--scheduling-math)
5. [Pacing & Operator Control](#5-pacing--operator-control)
6. [Line-Type Gating (RVM ↔ SMS)](#6-line-type-gating-rvm--sms)
7. [Merge / Stickiness Model](#7-merge--stickiness-model)
8. [Resource Pools](#8-resource-pools)
9. [Exit / Suppression / Ledger](#9-exit--suppression--ledger)
10. [Data Model](#10-data-model)
11. [Engine & Workers](#11-engine--workers)
12. [Drop.co Integration Details](#12-dropco-integration-details)
13. [Open Decisions (need your yes/no)](#13-open-decisions-need-your-yesno)
14. [Compliance Notes](#14-compliance-notes)
15. [Build Phases](#15-build-phases)

---

## 1. Goal & Guiding Principles

We are **not** building a blaster. With 100–200k leads a week and **only 4 agents**,
the machine is a **call-generation drip**: it meters outbound so inbound calls
trickle in at a rate 4 agents can actually work.

- **RVM is primarily a list-cleaning tool** (it classifies every number as
  wireless / landline / dead / blacklist) plus a secondary source of callbacks.
- **SMS is the primary call driver** — most people call back off a text.
- **Full manual control is a hard requirement.** Bad lists happen (ads run to
  people who never needed the service). The operator must be able to slow, pause,
  or cut a run to 1 text at any moment.
- **Start small:** first real cohorts are ~4k leads. Design for 200k, don't
  over-engineer for it yet.
- **Reuse what already works:** the existing Twilio sending engine (number
  rotation, warmup, per-number caps, dead-on-send removal), the opt-out
  classifier, the DNC list, and delivery-status webhooks all stay. This project
  *wraps* them in a per-lead sequence engine and adds the Drop.co RVM channel.

---

## 2. Vocabulary

| Term | Meaning |
|---|---|
| **Lead / Client** | One person from an uploaded list (first, last, phone, state, amount, + extras). |
| **Cohort** | One uploaded weekly list. Cohorts overlap (a 5-day cycle vs a weekly cadence). |
| **Run** | One business day of the sequence, 1–5. Runs 1/3/5 are RVM days; 2/4 are SMS-only. |
| **Touch** | One planned send (one RVM or one SMS) to one lead. The atomic unit the engine schedules. |
| **Slot** | A stable reference into a resource pool (template slot, callback slot, agent slot). A lead is stuck to a slot; the *value* behind the slot is editable. |
| **Sending number** | Your outbound Twilio line. Chosen live by the existing rotation engine. **Not** a slot. |
| **Callback number** | The number printed *in* the SMS body that the lead calls. A sticky slot. |

---

## 3. The Sequence (the 5-run cycle)

Every lead runs a **5-run cycle, one run per weekday**, then completes. Weekends
are skipped entirely (the daily cleanup only fires Mon–Fri).

### RVM days — Runs 1, 3, 5
1. **RVM drop** in the morning, staggered by timezone (see §4).
2. **SMS #1** — 1.5h after *that lead's actual RVM drop*.
3. **SMS #2** — 3h after *that lead's actual RVM drop*.

### SMS-only days — Runs 2, 4
1. **SMS #1** at a timezone-based start time (see §4).
2. **SMS #2** — 3.5h after SMS #1.

### Weekly totals per lead
- **Wireless:** 3 RVM + 10 SMS.
- **Landline / VoIP:** 3 RVM, **0 SMS** (RVM-only on runs 1/3/5, nothing on 2/4).
- **Dead / blacklist:** removed entirely — never RVM'd or texted again.

### Advancement
- At **5:01 PM PT each weekday**, a cleanup job advances every in-progress lead to
  the next run. After Run 5's SMS #2, the lead is marked **complete**.
- Advancement is **unconditional** by default (a lead moves to the next run even
  if some of today's sends were throttled, skipped, or missed) — see §13.

### One worked example — ET wireless lead, enrolled Monday
| Day | Run | Sends (Pacific) |
|---|---|---|
| Mon | 1 (RVM) | RVM 7:30 → SMS1 ~9:00 (+1.5h) → SMS2 ~10:30 (+3h) |
| Tue | 2 (SMS) | SMS1 8:00 → SMS2 11:30 (+3.5h) |
| Wed | 3 (RVM) | RVM 7:30 → SMS1 ~9:00 → SMS2 ~10:30 |
| Thu | 4 (SMS) | SMS1 8:00 → SMS2 11:30 |
| Fri | 5 (RVM) | RVM 7:30 → SMS1 ~9:00 → SMS2 ~10:30 → **complete** |

(A landline version of this lead: RVM only on Mon/Wed/Fri, nothing Tue/Thu.)

---

## 4. Timezone & Scheduling Math

- **Reference clock:** US Pacific — `America/Los_Angeles`, DST-aware (7:30 AM = 7:30 on the Pacific wall clock year-round). *(Confirmed.)*
- **A lead's timezone bucket is derived from the `state` field.**

### RVM anchor (2 buckets)
| Bucket | RVM time (PT) |
|---|---|
| ET / CT | 7:30 AM |
| MT / PT | 8:30 AM |

### SMS-only-day start (3 buckets)
| Bucket | SMS #1 time (PT) |
|---|---|
| ET | 8:00 AM (11 AM local) |
| CT | 9:00 AM |
| MT / PT | 10:00 AM |

### Key rule: offsets are relative to the lead's *actual* RVM send time
On RVM days, RVMs blast fast (whole list in an hour or two) but SMS is metered.
So a lead's SMS#1/#2 times are computed as **actual RVM `sent_at` + 1.5h / + 3h**,
not off the fixed anchor. This is why the plan stores an *eligibility time*, and
the RVM's completion is what stamps the concrete eligibility onto that lead's
same-day SMS touches.

### Eligibility vs. firing
The schedule sets **when a touch becomes eligible**; the pacer decides **when it
actually fires**. A touch that's eligible sits in a FIFO queue and goes out when
throughput allows. This cleanly resolves "RVMs fast, SMS slow" — nothing bursts,
nothing piles up.

---

## 5. Pacing & Operator Control

**v1 pacing is manual** (no live dialer-availability feed yet; see §13 for the
future auto-backpressure option).

- **RVM:** fire the **full run list** in the morning. No throttle, no daily cutoff.
  (RVM is the cleaner — we want the whole list classified fast.)
- **SMS:** the **metered channel**. Operator sets, per day:
  - **Send rate** (e.g., 10–20k/hr) — global dial.
  - **1 or 2 texts** for the day (default 2).
  - **Pause** (emergency brake; reuses existing `global_pause`).
- SMS sends are drawn from the eligible FIFO queue at the set rate, then handed to
  the **existing** send engine (which picks the outbound Twilio number via
  rotation/warmup/caps).

### The daily control panel (new dashboard tab)
Each morning the operator sees active cohorts/runs and can, per run:
`[RVM GO]  ·  SMS rate: [____]/hr  ·  Texts today: (1 | 2)  ·  [PAUSE]`
plus live counters (queued / sent / delivered / callbacks-in / opt-outs).

---

## 6. Line-Type Gating (RVM ↔ SMS)

**SMS only goes to wireless numbers.** Line type comes from two sources:

1. **Pre-scrub at upload (recommended safety net):** run each list through the
   existing Twilio Lookup scrubber at enrollment, so wireless/landline is known
   *instantly* and doesn't depend on RVM timing.
2. **RVM confirmation:** Drop reports wireless / landline / dead / blacklist on
   each drop; we update the lead's `line_type` from the webhook.

Rules:
- **Wireless** → eligible for SMS (after the RVM on RVM days).
- **Landline / VoIP** → RVM-only, never texted.
- **Dead / blacklist** → permanent suppression, removed from the whole machine.
- Existing SMS behavior stays: if we text a number and it comes back dead
  (carrier error codes), it's auto-suppressed from future texting.
- **Unknown at SMS time** (pre-scrub missing and RVM status not yet back): default
  is to **hold within the day, then skip** if still unknown at cutoff (§13).

---

## 7. Merge / Stickiness Model

Everything a lead "is" gets **frozen to slots at upload**; the **value** behind
each slot stays live-editable in the dashboard.

| Field | Source | Stickiness | Resolved |
|---|---|---|---|
| `first_name` | list | — | at send |
| `amount` | list | sticky (per lead) | at send |
| **agent name** | pool | **sticky per day** (Amy Mon, maybe Sarah Wed) | at send |
| **callback number** | pool (~100) | **sticky per lead** (by slot) | at send |
| **template / spin** | per-stage pool | sticky slot per send; round-robin across leads; no-repeat per lead | at send |
| **RVM audio** | pool | per run | at drop |
| **sending number** | existing Twilio pool | **not assigned — rotated live** | at send |

- **Locked assignment, live value:** editing a template's copy or replacing a dead
  callback number updates every *not-yet-sent* touch that references that slot.
  This is what lets a dying callback number **heal** — swap the value, and all
  leads on that slot get the new number on their next text instead of dying.
- **Sticky-to-slot, not sticky-to-value:** normally a lead sees one consistent
  callback number, but a mid-flight swap means later texts carry the new number.
  That's the intended trade (a live number beats a dead one).
- Optional **"lock cohort"** button snapshots rendered text at launch if you ever
  want a hard freeze for a specific list.

---

## 8. Resource Pools

All pools are **DB-backed and editable in the dashboard** (not hardcoded).

| Pool | Contents | Key operations |
|---|---|---|
| **SMS templates** | per **stage** pool (checking_in / following_up / last_day); the 10 weekly sends map to stages | add / edit / enable-disable / weight / A/B report |
| **Callback numbers** | ~100 numbers | edit / **replace (heals slot)** / enable-disable |
| **Agent names** | Amy, Sarah, Kim, … (a real person may wear 2–3 personas) | add / edit / enable-disable |
| **RVM audio** | uploaded recordings, mapped per run | upload / map to run / enable-disable |
| **Sending numbers** | existing Twilio pool | unchanged (warmup, rotation, health) |

**Template stages & mapping (default, editable):** three stage pools —
`checking_in` → `following_up` → `last_day`. The 10 weekly sends map by day:
**Days 1–2 (sends 1–4) = checking_in · Days 3–4 (sends 5–8) = following_up ·
Day 5 (sends 9–10) = last_day.** ~6–8 variations per stage (≥ the most sends in
one stage, so no lead repeats within a stage; because the stage pools are
disjoint, that also guarantees no repeat across the whole week). The send→stage
map is a config setting, not hardcoded.

**Template A/B:** each send records the template slot it used, so we report
delivered / callbacks / opt-outs / STOPs per template. Editing a template's
wording mid-test blends its stats — we version/timestamp edits so the report can
segment (or treat a big rewrite as a new variant).

---

## 9. Exit / Suppression / Ledger

### Exit conditions — any one removes the lead and cancels all pending touches
1. **SMS opt-out** — the existing classifier (STOP, "no", "f u", fuzzy/typo match).
2. **Drop IVR DNC** — a lead who opts out via Drop's callback IVR.
3. **Called-in feed (the big one)** — a manual upload from the dialer, **several
   times a day**. Anyone who called in — helped or not — is removed and tagged.
4. **Dead / blacklist** — from RVM status or an SMS carrier-dead error → permanent
   suppression (never contact again).

All four write to the global suppression list (extended DNC), which is **checked
before every send** (RVM and SMS). Called-in uploads take effect **before the
lead's next scheduled send** (near-real-time is not required, "same-day, before
next touch" is).

### The Ledger — the original list is never discarded
Every uploaded lead keeps its final weekly **outcome**:

| Outcome | Meaning |
|---|---|
| `no_response` | Ran the full cycle, never engaged. |
| `opted_out_sms` | Texted back a stop/opt-out. |
| `called_in` | Called in (removed regardless of result). |
| `dnc_ivr` | Opted out via Drop's IVR. |
| `dead` / `blacklist` | Bad number, permanently suppressed. |
| `in_progress` / `complete` | Lifecycle states. |

- **End-of-week export** per cohort shows every lead with its outcome.
- **Re-touch:** non-responders are re-worked 2–3 weeks later. Default is a
  **manual export → re-upload** of filtered non-responders as a new cohort (auto
  re-enrollment is a later option — §13).

### Dedupe
- **Within a list:** drop the second duplicate.
- **Across cohorts / global:** on upload, skip phones already active in another
  cohort or already on a suppression list (§13 to confirm).

---

## 10. Data Model

New tables (SQLite to start; the design ports cleanly to Postgres later). Existing
tables — `twilio_accounts`, `sending_numbers`, `dnc_list`, `inbound_replies`,
`delivery_stats`, `settings`, `scrub_jobs` — are reused as-is or lightly extended.

### `cohorts`
```
id, name, brand, uploaded_at, size,
status              -- active | complete | archived
default_texts_per_day (1|2, default 2),
default_sms_rate,   -- msgs/hr
notes
```

### `leads`  (the ledger — one row per lead per cohort)
```
id, cohort_id,
first_name, last_name, phone (normalized 10-digit), state, amount,
custom_fields (JSON),
timezone_bucket     -- ET | CT | MT | PT  (derived from state)
line_type           -- unknown | wireless | landline | voip | dead | blacklist
callback_slot_id    -- sticky FK -> callback_numbers
status              -- enrolled | in_progress | complete | removed
outcome             -- null | no_response | opted_out_sms | called_in | dnc_ivr | dead | blacklist
current_run         -- 1..5
enrolled_at, completed_at, removed_at, removed_reason
UNIQUE(cohort_id, phone)
```

### `touches`  (the materialized plan — the heart of the engine)
One row per planned send. This is the queue the pacer drains.
```
id, lead_id, cohort_id,
run_number          -- 1..5
touch_type          -- rvm | sms
step_in_day         -- rvm=0, sms=1|2
-- assignment (frozen at upload)
template_slot_id    -- FK -> sms_templates (sms only)
agent_slot_id       -- FK -> agent_names   (sticky per day)
audio_slot_id       -- FK -> rvm_audio     (rvm only)
-- scheduling
eligible_at         -- computed; for RVM-day SMS, stamped when the RVM sends
status              -- planned | eligible | sending | sent | delivered
                    --  | undelivered | failed | skipped | cancelled
-- resolved at send time
sending_number      -- Twilio line chosen by existing rotation (sms)
message_sid         -- Twilio (sms)
drop_activity_token -- Drop (rvm)
sent_at, delivery_status, error_code,
skipped_reason
INDEX(status, eligible_at)   -- the pacer's hot path
```
> **Agent sticky-per-day** is enforced at assignment: all SMS touches sharing a
> `(lead_id, run_number)` get the same `agent_slot_id`.

### Resource pools
```
sms_templates:    id, stage (checking_in|following_up|last_day), body, active, weight, version, updated_at
callback_numbers: id (slot), number, active, notes, updated_at     -- value editable, id stable
agent_names:      id (slot), name, active
rvm_audio:        id (slot), label, url, run_mapping, active
```

### Suppression — extend existing `dnc_list`
Reuse `dnc_list(phone, reason, source, their_message, added_at)`; add reasons:
`called_in`, `dnc_ivr`, `blacklist` (dead already exists). One global gate for
both channels.

### `called_in_uploads`  (audit)
```
id, uploaded_at, source, row_count, matched_count
```

---

## 11. Engine & Workers

### A. Enrollment (on list upload)
1. Parse + validate CSV (reuse existing parser; require phone, first_name, state,
   amount; keep extras as custom_fields).
2. **Dedupe within list**; skip globally-suppressed / already-active phones.
3. **Pre-scrub** line type via Twilio Lookup (recommended).
4. Create `leads` rows; derive `timezone_bucket` from `state`.
5. **Assign slots:** callback (sticky per lead), agent (sticky per run/day),
   templates (round-robin per position, no-repeat per lead).
6. **Materialize `touches`** for all 5 runs. RVM-day SMS touches get a placeholder
   `eligible_at` ("after this run's RVM"); SMS-only-day touches get concrete
   anchor times.
7. Show a **preview**: open any lead and read their full planned 3 RVM + 10 SMS.

### B. RVM dispatcher (morning, per active RVM run)
- At the tz anchor, post the **full run list** to Drop `/Delivery` (no throttle).
- Store `drop_activity_token` per touch.
- On Drop status webhook: set `line_type`; **dead/blacklist → suppress**;
  **wireless → stamp `eligible_at` on this lead's same-day SMS touches**
  (`rvm.sent_at + 1.5h / + 3h`).

### C. SMS pacer (continuous tick)
- Every tick: pull `status=eligible AND touch_type=sms AND eligible_at<=now`,
  FIFO, up to the operator's **rate**.
- Enforce **texts-per-day** (skip SMS#2 touches if set to 1) and **pause**.
- **Last-second eligibility check:** suppressed? line_type still wireless?
  → if not, `cancelled`, don't send.
- Hand to the **existing send engine** (rotation / warmup / caps / retry). Record
  `message_sid`, `sending_number`.

### D. Suppression ingest
- Endpoint + dashboard upload (paste or CSV), used several times a day.
- Add to `dnc_list` with reason; **cancel all planned/eligible touches** for those
  phones; set lead `outcome` + `removed_at`.

### E. Webhooks (extend existing)
- `/webhook/inbound` — opt-out → suppress + cancel + tag `opted_out_sms`.
- `/webhook/status` — delivery + dead-error auto-suppress (existing).
- `/webhook/drop` — **new** — VMDrop status → line type + IVR-DNC + callback count.

### F. Daily cleanup (5:01 PM PT, Mon–Fri)
- Advance each in-progress lead: `current_run += 1`; activate next run's touches.
- After Run 5 SMS#2 → `complete`; finalize `no_response` for the unengaged.
- Weekend-safe: only runs Mon–Fri.

### G. Reporting / Ledger
- Cohort outcome breakdown, A/B by template, per-number health (existing),
  day/week rollups (existing), non-responder export for re-touch.

---

## 12. Drop.co Integration Details

- **Base:** `https://customerapi.drop.co` · auth = `ApiKey` (query string, server-side only).
- **Success:** `ApiStatusCode == 1000` in the JSON body (not just HTTP 200).
- **Campaign strategy:** campaign creation is async (up to ~10 min). So we **do not**
  create a campaign per cohort. Instead we create a **small set of persistent
  VMDrop campaigns** (one per callback-forwarding configuration we need), then push
  records via `/Delivery`, using the per-record **`Audio` override** to set the
  right recording per run. Store each campaign's `CampaignToken` in settings.
- **Posting records:** `/Delivery` with `CampaignToken`, `PhoneTo`, `Audio`
  (override), `AllowDuplicates=false`, and map lead identity into `C1–C5` (e.g.,
  `C1=lead_id`, `C2=cohort_id`) so the webhook can tie status back to the lead.
- **Status:** prefer the **webhook** (`/webhook/drop`) over polling; fall back to
  `/VMDropStatus` by `ActivityToken` if needed.
- **Line type:** taken from Drop's per-record status where available, backstopped
  by the upload pre-scrub.
- **Balance:** surface `/BalanceCheck` in the dashboard so we don't run dry mid-run.
- **DNC:** Drop's National/State DNC toggles shift liability to us — **our own
  suppression list stays the authoritative gate before any `/Delivery`.**

---

## 13. Open Decisions (need your yes/no)

These are the spots where I assumed a default. Confirm or correct:

1. **Clock:** ✅ **RESOLVED** — Pacific `America/Los_Angeles` (DST-aware; 7:30 AM = Pacific wall-clock year-round).
2. **Unknown line type at SMS time:** hold-within-day-then-skip? *(Assumed yes,
   with upload pre-scrub making this rare.)*
3. **Agent stickiness:** ✅ **RESOLVED** — sticky **per day**; agent names editable in the pool.
4. **Run advancement:** unconditional at 5:01 PM even if a lead's sends were
   throttled/skipped today? *(Assumed unconditional.)*
5. **Re-touch of non-responders:** manual export → re-upload *(assumed)*, or should
   the system auto-hold and re-enroll after N weeks?
6. **Cross-cohort dedupe:** skip a phone that's already active in another cohort or
   on suppression *(assumed)* — or allow re-enroll?
7. **Template inventory:** ✅ **RESOLVED** — **stage-based** (checking_in /
   following_up / last_day); sends mapped by day (1–2 / 3–4 / 5); ~6–8 variations
   per stage; no-repeat per client. See §8.
8. **Callback numbers:** ✅ **RESOLVED** — **manually submitted** by the operator
   into the editable pool; replace-in-slot heals a dead number for every lead on it.
   *(Sending numbers = existing Twilio rotation logic, untouched.)*
9. **Texts-per-day default = 2**, operator-overridable each morning — correct?
10. **Auto-backpressure (future):** do you expect a live agent-availability signal
    from the dialer later, so we can auto-slow SMS when all agents are busy? If so
    we'll leave a clean seam for it now.

---

## 14. Compliance Notes

This is TCPA-sensitive lead-gen; mistakes here are the expensive kind, so the
design bakes in the safeguards:

- **Every send (RVM and SMS) checks the suppression list first.** One global gate.
- **All four exit paths** (SMS opt-out, IVR DNC, called-in, dead/blacklist) write
  to that gate and cancel pending touches.
- **Send windows / quiet hours** carry over from the existing platform.
- **Drop's DNC indemnifies Drop, not us** — our list is authoritative.
- **Gap to close:** leads currently arrive as anonymous list rows with no consent
  provenance (which form, when, what language, source ad). For TCPA defensibility
  and for attribution, we should capture that at intake — recommend adding
  `source`, `consent_timestamp`, and `consent_language`/`lead_url` fields to the
  lead schema when the form/API feed is wired up. Not blocking v1, but flagged.

---

## 15. Build Phases

Each phase is independently testable; nothing later depends on a phase that isn't
green.

- **Phase 0 — Foundation:** stand up the repo with the existing platform
  (app/database/sender/dashboard), add a Drop.co API client + config, wire the
  `/webhook/drop` skeleton.
- **Phase 1 — Data model & enrollment:** new tables, resource pools, CSV upload,
  dedupe, pre-scrub, slot assignment, `touches` materialization, **lead preview**.
- **Phase 2 — RVM + gating:** Drop dispatch, Drop webhook, line-type gating,
  suppression ingest + cancel.
- **Phase 3 — SMS pacer + control panel:** eligible-queue drain, rate/pause/1-or-2
  controls, hand-off to the existing send engine.
- **Phase 4 — Cleanup + ledger:** 5:01 PM advancement, completion, outcome
  finalization, cohort/A-B reporting, non-responder export.
- **Phase 5 — Hardening:** re-touch flow, edge cases, load test at 4k→ scale,
  optional auto-backpressure seam.

---

*End of draft v1. Please mark up §13 first.*
