# SMS Alerts desk

Part of the NMBZ helpdesk agents service. Installing, settings, commands and the console are in the [README](../README.md); this page covers what the SMS Alerts desk does. Its settings take the prefix `SMS_ALERTS_` when they should differ from the other desks (`SMS_ALERTS_AGENT_MODE=live`).

This agent replaces the Java `sms-agent` service. It handles every open osTicket ticket under the **SMS Alerts** help topics (`SMS Alerts`, `SMS Alerts / Transactional`, `SMS Alerts / Salaries`). It also finds SMS alerts problems filed under the wrong help topic, or none (see [Tickets under other topics](#tickets-under-other-topics)).

The checks are ported from the Java: the same SQL, normalisation rules, status codes and reply wording, and the same write path through Metabase. On top of that, Claude reads the whole ticket and decides what to do:

- **Corrected values:** it pulls the account or phone number from the message when the form is empty or wrong, and uses corrections the ticket owner posts later in the thread.
- **Missing details:** it asks the ticket owner for a missing or invalid detail, keeps the ticket open, and re-checks when they reply.
- **One reply per ticket:** it writes a single reply that covers both alert types and both accounts.
- **Specific escalations:** it escalates unusual cases with a precise internal note, and can trace where an alert got lost.

Hard safety rules stay in code, so the model cannot override them. The main one comes from the Java: a ticket only auto-closes when every account was checked and none shows a gateway failure.

```
every 15 min ─► open SMS Alerts tickets + full threads (OSTICKETS_NEW via Metabase)
               ─► open tickets under other topics ─► triage: about SMS alerts? ─► yes: handled the same way
                     │  skip: already escalated · waiting on the ticket owner · unchanged
                     ▼
        Claude reads the masked ticket ◄──► check_sms_alert_account  (Java SmsAgentService + SocketDeliveryChecker)
                     │                       trace_alert_pipeline     (Java AlertDeliveryInvestigator)
                     ▼
        submit_decision: reply_and_close | request_info | escalate | no_action
                     ▼
        guardrails (evidence-based, in code)  ──►  writes exactly as the Java did  ──►  audit log
```

## What it does with a ticket

1. **Fetch.** It loads open tickets using the Java's query (help-topic path rebuild and form fields by label), plus the full thread.
2. **Skip.** It leaves a ticket alone if any of these is true:
   - It already carries an escalation note. The marker is identical to the Java's, so tickets the Java escalated are recognised too.
   - The latest entry is a staff reply waiting on the ticket owner.
   - Nothing has changed since the agent's last decision. After `RECHECK_AFTER_HOURS` it looks again anyway.
3. **Read attachments.** Screenshots, scanned forms, PDFs and spreadsheets on the thread are read (see [Attachments](#attachments)), and what they show is placed under the message they came with.
4. **Reason.** Claude sees the form fields and the thread, oldest first. Each entry is labelled as the ticket owner, staff, or an earlier agent post. Values the owner supplies later, in a message or an attached file, override the form.
5. **Check.** `check_sms_alert_account` runs the Java's per-account logic in this order:
   1. IACLINK
   2. ACNTS SMS flag
   3. COBICADDR registration
   4. TRAN/EVTSLOG triggers from the last 15 days
   5. Ticket number vs Intellect number
   6. SMS gateway reconciliation on `public.transaction_activity`
   7. The missing-email remediation, plus the salaries path via `customer_accounts_idc`
6. **Decide and act.**
   - `reply_and_close`: posts an `R` reply and closes the ticket, both read-back verified.
   - `request_info`: posts an `R` reply asking for the detail and keeps the ticket open.
   - `escalate`: posts an `N` internal note starting with `SMS Alerts agent did not auto-close this ticket.`.

## Tickets under other topics

Branches sometimes file an SMS alerts problem under another help topic, or none (common when a ticket is e-mailed in). With `SCAN_OTHER_TOPICS=true`, each pass also looks at every open ticket created in the last `SCAN_OTHER_TOPICS_DAYS` days under any other topic, except those in `EXCLUDE_TOPICS` (EcoCash by default, which the EcoCash agent owns).

**Triage.** Each of these tickets gets one short model call. It reads the masked topic, subject, form and thread, and sorts the ticket into one of three kinds:
- `sms_alerts`: only about SMS alerts. The agent handles it like any SMS Alerts ticket: the same checks, the same rules, and it may reply and close.
- `mixed`: an SMS alerts problem plus something else. The agent runs the checks but only leaves an internal note with its findings, for the team that owns the ticket. It never replies to or closes the ticket.
- `other`: not about SMS alerts. Left alone.

One-time PINs, USSD, EcoCash and marketing SMS count as `other`. A verdict below `TRIAGE_MIN_CONFIDENCE` is treated as `other`.

**Cost.**
- **Verdicts are kept.** Each verdict is stored in the decision log, and the ticket is read again only when its owner posts a new message.
- **No model call** for a ticket where someone on the desk has already replied or left a note. People are already working on it.
- **Per-pass limit.** At most `TRIAGE_MAX_PER_RUN` tickets are read per pass; the rest wait for the next one.
- **Model.** `TRIAGE_MODEL` can be a small model.
- **In the console.** Triage tokens are included in the model cost. The overview shows how many tickets were found under other topics, and the Open tickets page lists them with their own topic.

**What the agent sees.** A ticket from another topic tells the agent which topic it came from and why triage picked it, so it looks for the account and phone in the messages rather than the form. If the full read shows it isn't about SMS alerts after all, the agent records no action and doesn't look again until the ticket changes. Escalation notes on these tickets say which topic the ticket was under. If Claude is unreachable, these tickets wait, since the Java rules only know SMS Alerts tickets.

`python -m nmbz --desk sms_alerts run --ticket N` works on a ticket under any topic. It runs triage first, so the same rules apply.

### Transaction tables by year

Core banking keeps one table per year: `NMBPRODIDC.TRAN2026`, then `TRAN2027`, and so on. The agent picks the table from the dates it is checking. When the 15-day window crosses New Year, it reads both tables and merges the results.

If next year's table doesn't exist yet in the first days of January, that slice is skipped, as long as the other year's table was read. If the naming ever changes, set `TRAN_TABLE_PATTERN`.

## Rules the code enforces (`sms_alerts/guardrails.py`)

| Rule | Effect |
|---|---|
| Any checked account has `socket_pending`, `socket_failed`, `socket_not_found`, `socket_unknown` or `salaries_unverified` (the Java's escalating set) | Close becomes escalate |
| A form account was not checked and no replacement came from the owner's messages | Close becomes escalate |
| A checked account appears neither on the form, nor in an owner message, nor in a file the owner attached that was read | Close becomes escalate |
| A status is in `NO_CLOSE_STATUSES` (invalid account or phone) | Close becomes escalate (the model should use `request_info`) |
| The agent has already replied `MAX_INFO_REQUESTS` times | `request_info` becomes escalate |
| The reply contains a number the model was never shown, an unknown placeholder, a PIN/OTP/password mention, a link or template leftovers | Becomes escalate |
| Confidence is below `CONFIDENCE_THRESHOLD` | Becomes escalate |
| The ticket closed, changed topic or got a new entry while the model was working | No action this run; retried next run |
| The ticket is under another topic and triage didn't find it purely about SMS alerts (`mixed`) | A reply or close becomes an internal note |
| A data-source error on an account | No action; retried next run |
| `MAX_CLOSES_PER_RUN` reached, or the kill switch file exists | Stops |
| The owner hasn't answered the agent's question within `REQUEST_INFO_TIMEOUT_HOURS` | Escalated with a note, without calling the model |

### If Claude is unreachable

With `MODEL_FALLBACK=rules` (the default), each ticket is decided by the ported Java rules:

- **Closes** with the Java's standard reply when every check passes.
- **Escalates** otherwise, including a ticket with no usable account, which is escalated rather than closed with the Java's "no account number" reply.

These decisions are logged with the category `rules_fallback`. With `MODEL_FALLBACK=none`, tickets wait for the next run.

## Privacy

Before anything reaches Claude, mobile numbers, account and other long numbers, national IDs and emails are replaced with tokens such as `<NUMBER_1>` and `<MOBILE_1>`.

- **Checks** get the real values back on the server, and their results are masked again before the model sees them.
- **Replies and notes** have tokens filled in with real values before posting, so the branch sees real numbers.
- **The audit log** stores only masked text.
- **Attachments:** text-type files are read on the server and masked like the thread. Images and PDFs can't be masked, so in `ATTACHMENT_MODE=model` each one goes to Claude as it is, in a separate call that only returns facts. The deciding agent and the log only get those facts, masked. `ATTACHMENT_MODE=local` keeps images and PDFs on the server.

Names in free text are **not** masked. Claude is approved for use across the bank (October 2026), so the masking is not a condition of using it: the agent masks numbers because it doesn't need them to decide, and so the log and the console hold none.

## Checking the setup

Install the service as in the [README](../README.md#install), then:

```bash
sudo -u nmbzagents .venv/bin/python -m nmbz --env-file /etc/nmbz-agents/agents.env check --desk sms_alerts --ping-model
```

`check` confirms the following:

- Each Metabase database resolves by name.
- Open SMS tickets can be read.
- The poster name for `REPLY_STAFF_ID`.
- This year's TRAN table can be read.
- Claude answers.

## Attachments

Branches often attach a screenshot of the client's SMS inbox, a screen from Intellect, a scanned form or a list of accounts instead of typing the details. The agent reads them and uses what they show.

**Getting the files.** osTicket lists attachments in `ost_attachment` and `ost_file` and, by default, keeps the content in the database (`ost_file_chunk`). The agent reads it through Metabase like everything else: the SQL returns each chunk as base64 (`TO_BASE64`, or `HEX` on an old MySQL), and the size is checked against osTicket's record. Inline images pasted into a message are attachments too.

**Reading them:**

| File | How it's read |
|---|---|
| Text, CSV, e-mail (.eml), Word (.docx), Excel (.xlsx) | On the server. Excel account numbers stored as numbers come back whole. |
| Images (JPEG, PNG, GIF, WebP; BMP/TIFF with Pillow; iPhone HEIC with `pillow-heif`) and PDFs | By Claude in a separate call that sees only the file. It returns what the file is (an SMS inbox screenshot, a banking screen, an error, a form, an ID), the phone, account and customer numbers it shows, the bank SMS visible with their times, error messages and other relevant text. Digits it can't read are dropped instead of guessed. Photos are turned upright and shrunk to 1568 px. |
| Old .doc/.xls, Outlook .msg, archives | Listed by name only |

The facts go under the message they were attached to, masked with the rest of the ticket. An account read from a file the **ticket owner** attached counts as supplied by the owner, so the agent can check it and close on it. Text inside a file is treated as content, never as instructions, and the close rules in code still apply.

**What isn't read**, and the agent and the portal are told why:
- Files the agent posted itself.
- A second copy of the same file.
- Small inline images (under `ATTACHMENT_MIN_INLINE_KB`, or 150 px or less), which are e-mail signature logos.
- Files kept outside the database by a storage plugin.
- Anything over `ATTACHMENT_MAX_MB`.
- More than `ATTACHMENT_MAX_FILES` files per ticket. The newest files from the ticket owner are read first.

If a file the agent needs can't be read, it asks the branch to type the detail in a reply instead of guessing.

**Cost.** One model call per image or PDF, about 1,500–2,000 input tokens for a screenshot. It happens only when the ticket is processed, so not on passes that skip an unchanged ticket. The tokens are added to the decision's total in the portal.

**Portal.** Each decision lists its attachments, what was read from each one (masked) or why it wasn't read.

Try it on a real ticket before going live. Nothing is decided or written:

```bash
python -m nmbz --desk sms_alerts attachments --ticket 482913           # what the agent would see, numbers masked
python -m nmbz --desk sms_alerts attachments --ticket 482913 --local   # text-type files only, nothing sent to the model
```

## Cutover from the Java sms-agent

1. **Replay history (no writes).** Run the agent on recently closed tickets and compare with what was actually posted:

   ```bash
   python -m nmbz --env-file /etc/nmbz-agents/agents.env --desk sms_alerts audit --since 2026-09-01 --limit 100
   ```

   This writes `reports/closed_audit_sms_alerts_since_2026-09-01.csv` (each desk its own file). Each row shows the statuses, the agent's action and reply, the Java's template reply, and the reply that was actually posted. The agent only sees the thread as it was before the desk responded, and the 15-day window is anchored to each ticket's close date, as in the Java audit. Each ticket costs model calls, so keep `--limit` sensible.

2. **Shadow (no writes).** Install the timer with `AGENT_MODE=shadow` while the Java is still live. After a few days, run:

   ```bash
   python -m nmbz --desk sms_alerts compare --since <date>
   ```

   It shows, ticket by ticket, what the agent would have done next to what actually happened. Many tickets will already be closed by the Java before the agent sees them. That's expected, because the audit replay covers those.

3. **Optional notes mode.** `AGENT_MODE=notes` posts the agent's recommendation as an internal note, and staff act on it.

4. **Switch.** First send yourself a test email with `notify-test` (see Branch notifications); without working emails, branches never see the agent's questions. Then switch in one maintenance window, so the two agents never act on the same tickets:

   ```bash
   sudo systemctl disable --now sms-agent.service            # the Java service
   sudoedit /etc/nmbz-agents/agents.env                  # SMS_ALERTS_AGENT_MODE=live
   sudo systemctl start nmbz-agents.service             # one pass now; watch the log
   journalctl -u nmbz-agents -n 200 --no-pager
   ```

5. **Rollback.** `touch /opt/nmbz-agents/state/sms_alerts/STOP` (or disable the timer), then `sudo systemctl enable --now sms-agent.service`.

## Branch notifications

The agent writes replies directly into osTicket's tables, as the Java did, and osTicket sends no email for replies written that way. That was mostly harmless while the Java only closed tickets. Now the agent also **asks** branches for missing details, so it emails the person who raised the ticket itself.

| When | Email subject | What it says |
|---|---|---|
| The agent asks for information | `[#482913] SMS alerts ticket: information needed` | The question, how to answer, a link to the ticket, and that staff follow up after `REQUEST_INFO_TIMEOUT_HOURS` |
| The agent resolves and closes | `[#482913] SMS alerts ticket resolved` | The resolution, a link to the ticket, and what to do if alerts still don't arrive |

Escalations send no email; staff pick them up from the internal note. The wording lives in `templates/notify_request_info.txt` and `templates/notify_resolved.txt`, so it can be changed without touching the code.

**How it's sent:**
- **Outbox:** emails go into a queue and are sent at the end of each pass. A failed send is retried on the next passes, up to `NOTIFY_MAX_ATTEMPTS` times.
- **No duplicates:** each reply gets exactly one email, even after retries. The gateway also receives a stable `uuid`.
- **Privacy:** once an email is sent, its body is erased from the log, so real account and phone numbers don't linger there.
- **Recipient:** the ticket owner's email comes from osTicket (`ost_user` → `ost_user_email`). An owner without a valid address is recorded as "not sent".
- **Portal:** the **Emails & alerts** page shows every email with its status. Admins can re-send failed ones.

**Transports:**
- **`gateway`** (the default when `NOTIF_EMAIL_URL` is set) uses the bank notification gateway exactly as the Java `AdminNotifGateway` does: `POST NOTIF_EMAIL_URL` with `{to, subject, body, uuid, access_token}` and `Authorization: Basic NOTIF_GATEWAY_BASIC_AUTH`. Copy the three `NOTIF_*` values from the Java `sms-agent.env`.
- **`mailapi`** uses the bank mail API: `POST MAIL_API_URL` with `{operation: "SEND_EMAIL", channel: MAIL_API_CHANNEL, accessToken: MAIL_API_TOKEN, uuid, requestBody: {to, subject, message}}`. The API has no cc, so `NOTIFY_CC` addresses are added to `to`; `uuid` is the e-mail's own id, the same on every retry. An HTTP error or an answer that says it failed counts as not sent and is retried. The token is never written to the log or the outbox; keep it only in the env file, and use an `https://` URL if the API offers one.
- **`smtp`** sends through your mail relay and can set `Reply-To`. If osTicket fetches the `NOTIFY_REPLY_TO` mailbox, a branch can simply reply to the email. The `[#number]` in the subject threads the reply into the ticket as a new message, and the agent picks it up on its next pass.

Test before going live (sends one sample email, nothing is queued):

```bash
python -m nmbz --env-file /etc/nmbz-agents/agents.env --desk sms_alerts notify-test --to you@nmbz.co.zw
python -m nmbz --env-file /etc/nmbz-agents/agents.env --desk sms_alerts notify-retry     # re-send failed emails now
```

`check` warns when the agent is live with notifications switched off.

## Anomaly watch

Once an hour (at the end of a pass), the agent steps back from single tickets and looks at the whole SMS alert flow. It answers three questions:

1. **Are there more SMS Alerts tickets than usual?** It compares the last 24 hours with the same 24-hour window on each of the last 14 days, using the median and its typical spread. When the count is unusual, it says which branches and help topics the extra tickets come from.
2. **Are alerts being delivered?** It reads the delivery rate at the SMS gateway (overall and per route, plus per route over the last two to three hours so a sudden outage shows within the hour), sending volume, the backlog stuck "awaiting delivery", the dispatch service's SENT share, and the CAE render success share, all against the same 14-day baseline.
3. **What do the failures have in common?** It takes a sample of failed or stuck sends and a sample of delivered sends from the same 24 hours and compares them feature by feature. It does the same for the accounts the ticket agent checked (alerts delivered vs not delivered).

| Compared for gateway sends | Compared for checked accounts |
|---|---|
| route, mobile network (from the prefix), number format (e.g. doubled 263), currency, amount band, account in the text, single or multi-part, GSM-7 or Unicode, debit or credit, hour of day | alert type, account currency, mobile network, email on the core profile, IDCCOB registration, how many triggers, event code, gateway route, dispatch lag, raising branch |

A finding reads like "62% of failed sends are NetOne numbers, against 11% of delivered ones". A feature is only reported when it covers at least five failures and is at least 1.8 times as common among failures as among deliveries. The provider error texts most common among the failures are listed alongside (deliveries carry no error, so they are not compared).

**Who decides what:**
- **The thresholds decide the severity** (`ok`, `watch` or `incident`) in plain code, so an incident is never a model's opinion. For example, the delivery rate falling 20 points below usual, or 25 points on one route, is an incident; 8 or 10 points is a watch.
- **Claude explains it.** It gets only counts, rates and categories (no ticket text, phone or account numbers) and writes the headline, likely causes, next checks and an optional note for branches. If Claude is unreachable, a plain summary is built from the signals.

**What happens with an incident:**
- **Admins are alerted** by SMS and email through the same gateway as the Java `AdminNotifGateway`. They're alerted when an incident opens, when it gets worse (a new signal), every `ANOMALY_REALERT_HOURS` while it lasts, and once when it's over.
- **The ticket agent is briefed.** While an incident or watch is open, each ticket starts with an "Operations context" line. Tickets that fit the pattern are escalated with "matches the open incident" in the note instead of telling the branch to check spam, so staff can handle them together.
- **The portal** shows it on the **Anomalies** page: the signals, the 48-hour gateway chart, and the failed-vs-delivered tables. A red pill appears on every page.

Cost and load: the queries are aggregates over Metabase, and complete days are cached in the agent's own database, so after the first run only the current 24 hours are queried. One model call per hour, only when something is unusual.

```bash
python -m nmbz --desk sms_alerts anomalies --no-model    # run the analysis now and print it, no alerts, no model
python -m nmbz --desk sms_alerts anomalies --alert       # run it as the hourly job does, alerting admins if needed
```

`check` warns when the watch is on but no admin recipients are set.

## Portal (audit and monitoring)

The console (`python -m nmbz portal`, port 8085) shows this desk under **SMS Alerts**. It reads the desk's own decision log, so it needs no Metabase or Claude keys. It has these pages:

- **Overview:** health strip, decisions per day by outcome, and SMS gateway outcomes per day. A rise in "failed" or "never reached gateway" usually means a gateway or CAE incident. It also shows check statuses, why safety rules stepped in, and tokens or cost.
- **Anomalies:** the latest anomaly-watch report and its history: severity, Claude's summary, signals against their usual level, gateway outcomes per hour, and what failed sends and undelivered accounts have in common.
- **Open tickets:** what the last pass did with every open SMS Alerts ticket: closed, asked, escalated, or skipped and why.
- **Approvals:** the approval desk (see [Approvals](#approvals)).
- **Decisions:** filter by outcome, check status, mode, safety stop, fallback, error or review state, then export to CSV. Each decision shows the checks, the reply, the standard Java wording, what was actually posted (in audit replays), the reasoning, and the full masked conversation with the model.
- **Review:** mark a decision right or wrong and say what it should have done. This page also shows accuracy by outcome, the most common corrections, the decisions that most need a second look, and audit-replay agreement.
- **Emails & alerts:** every email to a ticket owner, and every admin alert, with its status.
- **Runs:** every pass, with duration and errors. While a pass runs, "Right now" shows the ticket being worked and each tool call as it happens, and the health strip on every page shows the current step. These steps are masked like everything else.
- **Controls (admins):** pause the agent, or hold it at notes or shadow. Every change is logged with who made it and why. The portal can only make the agent more careful; going live is done in the config.

**Keyboard:**
- <kbd>Ctrl</kbd>+<kbd>K</kbd> jumps to any ticket by number, or to a page.
- On the approval desk, <kbd>J</kbd>/<kbd>K</kbd> move through the queue, <kbd>A</kbd> approves, <kbd>E</kbd> edits the reply and <kbd>X</kbd> rejects. Approving and rejecting each ask for <kbd>Enter</kbd> to confirm.
- <kbd>?</kbd> lists the shortcuts.

The console is still one self-contained page with no external scripts.

`GET /healthz` needs no sign-in. It returns 200, or 503 when no pass has finished within `PORTAL_STALE_MINUTES` (a deliberate pause counts as healthy). Point your monitoring or an F5 health monitor at it.

Users and the console's systemd unit are set up once for every desk: see the [README](../README.md#install).

Security:

- **Network:** the portal binds to `127.0.0.1` by default. Publish it through the F5 or nginx with TLS, because sign-in is HTTP Basic.
- **Passwords:** stored as PBKDF2 hashes, and repeated failures lock out the source address for 5 minutes.
- **Writes:** need a custom request header and a same-origin check.
- **Browser:** the page sends a strict Content-Security-Policy and can't be framed.
- **Customer numbers:** stay masked everywhere in the portal, as in the log.

`python tools/build_preview.py` builds `preview.html`, the same console with invented demo data, for showing it off without server access.

## Investigating beyond the standard check

Besides the standard check and the pipeline trace, Claude has read-only lookups for tickets that don't fit the usual statuses. Each one is fixed SQL on tables the check already reads. It only accepts a masked number that appears on the ticket or came back from an earlier result, and its result is masked like the rest. A lookup for any other number is refused, and the ticket can then only be escalated.

| Lookup | What it answers |
|---|---|
| `get_account_activity(account, from, to)` | Every transaction on the account in up to 31 days, whether each raised an alert, and what the gateway did with those alerts. For "I withdrew on the 3rd and got no SMS", or dates outside the 15-day window. |
| `get_gateway_messages(mobile, from, to)` | Every SMS the gateway sent to a number, with network and delivery status. It returns only the amount, currency and kind of alert, never the message text. |
| `get_customer_profile(account)` | Every number and alert registration on file, the SMS flag, and whether a usable e-mail exists. |
| `get_pending_profile_changes(account)` | Changes captured at a branch but not yet authorised, such as a new mobile awaiting approval. Hidden until `PENDING_CHANGES_SQL` is set (see below). |

**Pending changes, once the Intellect tables are known.** Set `PENDING_CHANGES_SQL` to a single SELECT on the standby database (or on `PENDING_CHANGES_DB_NAME`). Use `{account}` (14 digits), `{internal}` (internal account number) or `{client}` (client number) to pick the customer. For example:

```
PENDING_CHANGES_SQL=SELECT FIELD_NAME, NEW_VALUE, ENTRY_DATE, ENTERED_BY FROM <maker-checker table> WHERE CLIENT_CODE = '{client}' AND AUTH_STATUS = 'P'
```

`LOOKUPS_ENABLED=false` turns them all off.

## Approvals

Some replies need a person before they go out. With `APPROVAL_MODE=unscripted` (the default), a reply or close waits in the console when either of these is true:
- **The model says so.** Claude reports that it reasoned beyond the standard statuses (from the lookups, the thread or attachments).
- **The action differs from the statuses.** For example, the checks lead to a close but Claude asked a question instead.

Standard outcomes are still posted straight away. `APPROVAL_MODE=all` holds every reply, which is a gentle step between notes and live; `off` holds none. Escalations are internal notes and never wait. Shadow mode records "would wait" on the decision.

**The approval desk** (Approvals page) has three panes:
- **Waiting:** the replies, oldest first.
- **How the agent got here:** each lookup Claude chose, with what it sent and what came back.
- **Proposed reply:** the reply, why it waits, and the Approve, Edit and Reject buttons.

Only admins can decide. An edit may move or reword the masked numbers but can't add new ones. Every decision is in the control log.

**On the agent's next pass:**
- **Approved:** the reply is posted, and the ticket closed when that was the action. The branch gets its e-mail.
- **Rejected:** the agent posts an escalation note with the reviewer's comment and the draft.
- **Ticket changed meanwhile:** if a new message arrived or the ticket closed, the waiting reply is dropped and the agent decides again.

A waiting reply keeps the real numbers it needs until then, like the e-mail outbox. They are erased once it is posted, rejected or dropped.

## Running

The service's timer (`nmbz-agents.timer`, see the [README](../README.md#install)) runs a pass of every desk every 15 minutes, like the Java service, and never starts a pass while the previous one is still running.

| Exit code | Meaning |
|---|---|
| 0 | OK |
| 1 | Config error |
| 2 | Metabase unreachable |
| 3 | Some tickets errored; they're retried next run, and the unit treats this as success |

## Day-to-day commands

```bash
python -m nmbz --desk sms_alerts run                                   # one pass in AGENT_MODE
python -m nmbz --desk sms_alerts run --ticket 482913 --mode shadow     # one ticket, ignore skip rules, no writes
python -m nmbz --desk sms_alerts notify-test --to you@nmbz.co.zw       # one sample branch email
python -m nmbz --desk sms_alerts attachments --ticket 482913           # read one ticket's attachments, no decision
python -m nmbz --desk sms_alerts anomalies --no-model                  # anomaly analysis now, printed only
python -m nmbz --desk sms_alerts check-account --account 1234567890 --phone 0772123456 --types both --trace
                                                      # the old single-CIF lookup, no model
python -m nmbz --desk sms_alerts report --since 2026-10-01 --out decisions.csv
python -m nmbz --desk sms_alerts compare --since 2026-10-01
touch state/sms_alerts/STOP / rm state/sms_alerts/STOP                      # kill switch
```

## Layout

What every desk shares (the reasoning loop, the pass, osTicket reads and writes, masking, approvals, attachments, the outbox, the anomaly service, the console) is in `agentcore/`, under the same file names; the desk's files below hold what is particular to it.

| Path | What it is |
|---|---|
| `sms_alerts/normalize.py` | Phone, account, currency and timestamp rules (`SmsAgentService` + `SocketMessageParser`) |
| `sms_alerts/sms_checks.py` | The per-account verdicts and the pipeline trace. The SQL is identical to the Java's. |
| `sms_alerts/delivery.py` | `SocketDeliveryChecker`: gateway reconciliation, dispatch lag, over-send detection |
| `sms_alerts/osticket.py` | Ticket and thread reads; reply, close and note writes with read-back checks |
| `agentcore/metabase.py` | Metabase client: `X-API-Key`, resolve by name, read retries, write handling (shared) |
| `sms_alerts/tools.py` | The model's tools, with masking; records evidence for the guardrails |
| `sms_alerts/loop.py` | How the ticket is shown to Claude; the tool-use loop itself is `agentcore/loop.py` |
| `sms_alerts/guardrails.py` | Code-enforced rules |
| `sms_alerts/rules.py` | Java-equivalent decisions when Claude is unreachable |
| `sms_alerts/runner.py` | Which tickets, notes and log columns; the pass itself is `agentcore/runner.py` |
| `sms_alerts/triage.py` | Sorting tickets under other topics: about SMS alerts, mixed, or not |
| `sms_alerts/lookups.py` | The read-only investigative lookups |
| `sms_alerts/approval.py` | Which replies wait for a person, and the waiting replies |
| `sms_alerts/attachments.py` | Reading ticket attachments: type detection, local text extraction, image preparation, the model's file reader |
| `sms_alerts/notify.py` | Branch emails and admin alerts: outbox, gateway email/SMS and SMTP transports |
| `sms_alerts/templates/` | Email wording |
| `sms_alerts/anomaly/` | Anomaly watch: `collect.py` (Metabase aggregates and samples), `features.py` (what is compared), `detect.py` (thresholds), `summarize.py` (Claude's summary), `service.py` (reports, admin alerts, ticket briefing) |
| `sms_alerts/prompts/anomaly_prompt.md` | Instructions for the incident summary |
| `agentcore/controls.py` | Pause switch and mode hold, shared by every desk and the console |
| `sms_alerts/portal/` | This desk's part of the console: `data.py` (gateway outcome groups), `vocab.py` (status labels and charts); the server and page are in `agentcore/portal/` |
| `tools/demo_sms_alerts.py` | This desk's demo data for `tools/build_preview.py` |
| `sms_alerts/prompts/system_prompt.md` | The agent's instructions: status meanings, when to close, ask or escalate, and reply style |

### Why values are written into the SQL text

Metabase runs a parameterised native query through a `PreparedStatement` with `executeQuery()`, and the MySQL driver refuses an `INSERT` or `UPDATE` that way. The Java writes literal SQL, which Metabase executes before reporting "did not produce a ResultSet". The agent does the same, and every value is either validated as digits or escaped exactly as the Java escapes it.

## Tests

```bash
pip install -r requirements-dev.txt && python -m pytest -q tests/sms_alerts
```

The tests run against a fake Metabase that answers the ported SQL and a scripted model. They cover:

- Each Java status.
- Gateway matching edge cases.
- The year split.
- The write path.
- The guardrails.
- The full loop, including the wrong-number → ask → corrected-on-thread → close scenario.


## query_data: tables beyond the checks

The agent can also read the SMS dispatch service's own tables (database SMS) with `query_data`
(`agentcore/dataquery.py`, catalogue `agentcore/data_catalogue.json`): every SMS sent or tried to a mobile (provider,
dispatch status, failure reason; never the text), matched through the recipient table, and the recipient list
(whether SMS is enabled for a mobile). A query takes only a masked number this ticket issued (from the ticket or an
earlier result); counts without a match, to see whether sending is failing for everyone, carry no numbers. At most 25
rows over at most 14 days. `python -m nmbz check --desk sms_alerts` tries each table's columns without reading rows;
`QUERY_DATA=false` switches it off.

## Staff knowledge and suggested replies

What the helpdesk taught this desk lives in staff notes (Teach the agent), not in the prompt or code:
`sms_alerts/knowledge_seed.json` holds them, and `python -m nmbz knowledge-seed --desk sms_alerts` proposes the ones not in the
console yet. An Admin switches each on; from then on staff can change or switch off any of them without a code change.
The notes: spam folder advice only when delivery was confirmed, a number that differs or is missing (and changes waiting for authorisation), the account-level SMS flag and the IDCCOB registration, the missing e-mail platform issue, no alertable activity, salary alerts, and gateway failures.

The check still works out each account's status and gives the helpdesk's usual wording (`reply`) as a suggestion; the agent writes the reply that fits the ticket. The code keeps what protects the client: only the ticket's own numbers, every account on the ticket checked, and statuses where delivery is not confirmed (socket_*, salaries_unverified) never close.
