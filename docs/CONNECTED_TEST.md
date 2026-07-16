# Connected Test Runbook

How to stand up a full, everything-connected test on DigitalOcean, safely (in
dry-run first, then a tiny live send).

## 0. Prereqs you provide

- A **DigitalOcean** account (App Platform).
- A **Twilio** account with sub-account(s), Auth Token(s), and at least one
  SMS-capable number — **10DLC-registered** if you want SMS to actually deliver.
- A **Drop.co** account with the **Customer API enabled** and your API key.
- Public URLs for your **RVM audio** (≤60s) and, if using IVR forwarding, an IVR clip.

## 1. Deploy (dry-run, nothing sends)

1. `doctl apps create --spec .do/app.yaml` (or import in the DO console).
2. Set secrets: `DASHBOARD_PASSWORD`, `DROP_API_KEY`, `DROP_WEBHOOK_TOKEN`
   (any long random string). Leave `WEBHOOK_AUTH_ENFORCE=0` for now.
3. Wait for the app + Managed Postgres to come up (health check `/health`).

## 2. Wire the connections (in the dashboard)

1. **Settings → Base URL** = the app's public URL (needed for delivery callbacks).
2. **Numbers tab** → add your Twilio sub-account(s) + sending number(s).
3. **Sequences → Engine → Create Drop Campaign** → fills the campaign token
   (or paste a token you made in Drop). Allow ~10 min for Drop to finalize.
4. **Sequences → Engine → 🔌 Test Connections** → confirm Drop balance and each
   Twilio account read green, Base URL set, Drop token set.
5. In **Twilio**, point the number's **inbound** and **status callback** at
   `https://<app>/webhook/inbound` and `/webhook/status`.
   In **Drop**, set the webhook URL to `https://<app>/webhook/drop?token=<DROP_WEBHOOK_TOKEN>`.

## 3. Load content + a tiny test list

1. **Pools** → add a few SMS templates per stage, agent name(s), callback
   number(s), and the RVM audio URL.
2. **Sequences → Upload Cohort** → a small CSV (**a handful of numbers you own**),
   columns `phone, first_name, state, amount`.
3. Open a lead → confirm the plan preview reads correctly.

## 4. Dry-run the whole flow

Keep RVM + SMS **dry-run ON**. Click **Run due RVMs**, then **Run due SMS**. Watch:
the RVM marks leads wireless (simulated), SMS "send" with `DRYRUN` SIDs, the
Lead Plan + Cohort Report update. No real messages go out.

## 5. Go live — small

1. In **Engine**, turn **RVM dry-run OFF**. Run due RVMs → real drops to your
   own test numbers. Watch `/webhook/drop` set real line types (and note the raw
   `DropStatusCode`s in the logs — send them to finalize the code map).
2. Turn **SMS dry-run OFF**, set a low rate. Run due SMS → real texts to your
   wireless test numbers. Reply STOP from one to confirm opt-out + cancel.
   Reply "yes" from another to confirm it's pulled out as a lead.
3. Check **Test Connections** and the **Cohort Report / Full outcomes CSV**.
4. Once `/webhook/status` logs show Twilio signatures validating, set
   **`WEBHOOK_AUTH_ENFORCE=1`** and redeploy to lock the webhooks down.

## 6. Turn on automation

Set the SMS rate + 1-or-2 texts, enable the **Scheduler**. It now runs RVM +
SMS + the 5:01 PM cleanup on its own, honoring the quiet-hours window and every
dry-run/pause switch.

---

**Safety recap:** dry-run defaults ON; the quiet-hours window blocks off-hours
sends; DNC is checked before every send; webhook auth starts log-only so a
misconfig can't silently break delivery tracking.
