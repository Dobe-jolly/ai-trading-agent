# EcoCash desk

Part of the NMBZ helpdesk agents service. Installing, settings, commands and the console are in the [README](../README.md); this page covers what the EcoCash desk does. Its settings take the prefix `ECOCASH_` when they should differ from the other desks (`ECOCASH_AGENT_MODE=live`).

This agent resolves osTicket tickets about clients who can't link their NMB account to their EcoCash wallet. It works the way the SMS Alerts agent does: Claude reads the whole ticket (form, thread and attachments), looks up what actually happened when the client tried to link, and then replies to the branch with the fix, asks for what's missing, or escalates to ICT with a precise note. Rules in code decide what may be posted.

It comes with three other parts:

- **A new monitor.** It replaces the Java `ecocash-registration-monitor` and records every link and delink request that `ecocashreg.service` sends to EcoCash, successes included, not only failures.
- **An anomaly watch.** Every hour it compares link outcomes and ticket volume with the usual level, and alerts admins by SMS and e-mail when EcoCash or the bank side breaks.
- **A console.** It shows every decision, the conversation with the model, the open tickets, e-mails, runs and anomalies, with controls for the mode and a kill switch.

The agent never texts clients. Everything it does happens on the ticket, plus an e-mail to the person who raised it.

```
integrations host                                         agent host (with the SMS agent)
ecocashreg.service ─journal─► ecocash monitor (Python) ─► ecocash_monitor.ecocash_link_attempts
                                                                  │  (read through Metabase)
osTicket (OSTICKETS_NEW) ◄──────────────────────────────┐         ▼
                                                         │  every 15 min: open EcoCash tickets
Postilion (eftc_ecocash) ──Metabase──► lookups ◄──► Claude reads the masked ticket + attachments
Core banking (STANDBY DB) ─Metabase──┘                   │  submit_decision → guardrails (code)
                                                         └─ reply / close / note → branch e-mail
                                           hourly: anomaly watch → admin SMS + e-mail on an incident
```

All of the agent's data access goes through the Metabase API. The monitor writes to `ecocash_monitor` directly (as the Java did) or through Metabase (`MONITOR_STORE`).

## What EcoCash answers, and what the agent does

The codes below come from the registration log of 6 April 2026: 134 requests reached EcoCash, 61 linked and 73 failed.

| Latest attempt (EcoCash code) | Seen | Meaning | What the agent does |
|---|---|---|---|
| Linked | 61 | The link worked | Tells the branch it is done and closes |
| Already linked (20503) | 47 | The account is already linked to a wallet | Asks the branch to delink on the Universal dashboard and have the client link again; keeps the ticket open. If the delink fails, the branch replies with a screenshot and ICT delinks. |
| KYC verification failed (BCS50646) | 9 | Name or ID sent differs from EcoCash's records | Compares the ID the bank sent with the ID on the ticket or an attached ID copy. Same ID: the client updates details at an EcoCash shop, close. Different ID: the branch corrects the bank profile, close. No ID on the ticket: asks for it. |
| Number not on EcoCash (TP40288) | 6 | No wallet on that number | The client registers, or links with the wallet's number. Close. |
| Wallet closed (TP40050) | 6 | The client's wallet is closed | The client reactivates it at an EcoCash shop. Close. |
| EcoCash system error (TP40024) | 3 | Temporary, on EcoCash's side | The client tries again later. Close, but escalate after 3 in a day for the client or while an incident is open. |
| Account rejected (TC30174) | 2 | EcoCash rejected the account number on a branch delink (both delinks in the log) | Escalate to ICT |
| Delinked | | The account was delinked | Ask the branch to have the client link again; keep open |
| No answer from EcoCash | | Request logged, no response within 3 minutes | Escalate |
| No attempt found | | Nothing in the last 60 days | Ask the branch to confirm the number and account, and have the client try again |
| Anything else | | No rule yet | Escalate |

The wording is in `ecocash/ecocash.py` (`ERROR_TYPES`) and `ecocash/tools.py` (`standard_reply`). Claude adapts it to the thread. The standard wording is shown next to what was posted on each decision in the console.

## Tickets

**Which tickets:**
- Every open ticket under `ECOCASH_HELP_TOPICS`, subtopics included.
- Open tickets under other topics whose subject or owner messages mention EcoCash (`ECOCASH_KEYWORDS`), opened in the last `ECOCASH_CONTENT_DAYS`. These cost no model call unless the mobile, account or ID on the ticket has a link attempt. Otherwise they're logged as "not an EcoCash link case" and looked at again only when the ticket changes.
- Topics in `ECOCASH_EXCLUDE_TOPICS` are never touched (SMS Alerts by default, which the SMS agent owns).

**What Claude sees.** The subject, every form field whatever its label, the full thread, and what each attachment shows. The mobile numbers, accounts and IDs the owner gave are collected from all of these. Everything is masked first, so Claude sees `<MOBILE_1>`, `<NUMBER_1>` and `<NATIONAL_ID_1>`, never the values.

**The lookups Claude can call:**

| Tool | Source | Returns |
|---|---|---|
| `find_ecocash_attempts` | `ecocash_link_attempts` (and the Java's `failed_ecocash_registrations` for older history) | Every link and delink attempt for the client's mobile, account or ID: outcome, EcoCash code and meaning, who started it (USSD, branch staff, iApprove), what the bank sent, whether the name and ID sent match the ticket, and the standard reply for the latest one |
| `check_bank_link_record` | Postilion `eftc_ecocash` | Whether the bank holds a link record for the account or mobile |
| `check_bank_account` | Core banking | Whether the account exists, its currency, and whether the mobile on file matches |
| `get_ecocash_service_health` | `ecocash_link_attempts` | How EcoCash answered everyone in the last few hours, next to the same hours yesterday. It tells an EcoCash-side problem from this client's own. No customer data. |
| `get_pending_profile_changes` | Core banking (`PENDING_CHANGES_SQL`) | Profile changes captured at a branch but not yet authorised, such as a corrected national ID. Hidden until the query is set (see below). |

Lookups only accept identifiers that are on the ticket or came back from an earlier lookup, so the model can't look up anyone else. Names from the request are never returned, only whether they match.

**The delink conversation (20503):**
1. The agent asks the branch to delink and have the client retry, keeps the ticket open, and e-mails the branch.
2. On the next pass it looks again. If the monitor has since recorded a delink and a successful link, it closes with that as proof.
3. If the branch says the delink failed and attaches a screenshot, the agent reads it and escalates with what it shows.
4. If nobody answers within `REQUEST_INFO_TIMEOUT_HOURS`, the ticket is escalated without calling the model.

**Pending changes, once the Intellect tables are known.** Set `PENDING_CHANGES_SQL` to one SELECT on the core-banking database. Use `{account}`, `{internal}` or `{client}` to pick the customer, for example:

```
PENDING_CHANGES_SQL=SELECT FIELD_NAME, NEW_VALUE, ENTRY_DATE FROM <maker-checker table> WHERE CLIENT_CODE = '{client}' AND AUTH_STATUS = 'P'
```

## Approvals

With `APPROVAL_MODE=unscripted` (the default), a reply or close waits in the console in two cases:
- **The model says so.** Claude reports that it reasoned beyond the standard reply for the latest attempt.
- **The action differs.** Its action differs from the one the standard reply gives.

Standard outcomes are posted straight away. `all` holds every reply, `off` none. Escalations never wait, and shadow mode records "would wait" on the decision.

The approval desk (Approvals page) shows the waiting replies, oldest first, with each lookup Claude made and the proposed reply. Admins can approve, edit or reject a reply. An edit may move or reword the masked numbers but can't add new ones.

**On the agent's next pass:**
- **Approved:** the reply is posted (and the ticket closed when that was the action), and the branch gets its e-mail.
- **Rejected:** the agent posts an escalation note with the reviewer's comment.
- **Ticket changed meanwhile:** the waiting reply is dropped and the agent decides again.

The real numbers a waiting reply needs are kept only until then, as in the e-mail outbox.

## Rules the code enforces (`ecocash/guardrails.py`)

| Rule | Effect |
|---|---|
| The ticket closed, changed topic or got a new entry during analysis, or a lookup failed | No action; retried next pass |
| The model looked up a mobile, account or ID the ticket owner never gave | Escalate |
| The reply contains a number the model was never shown, a PIN/OTP/password mention, a link, or template leftovers | Escalate |
| Confidence below `CONFIDENCE_THRESHOLD` | Escalate |
| Close without looking up the attempts, or no attempt found | Escalate |
| The latest attempt doesn't match a mobile, account or ID the owner gave | Escalate |
| The latest attempt is already linked or delinked | Close becomes a reply that keeps the ticket open |
| The latest attempt is account rejected, no answer or unknown | Escalate |
| The latest failure is more than 14 days older than the ticket | Escalate |
| KYC failure and no national ID on the ticket to compare | Escalate |
| 3 or more system errors for the client in 24 h, or an EcoCash incident open | Escalate |
| The same failure again after the agent's advice | Escalate |
| The agent has already replied `MAX_INFO_REQUESTS` times | Escalate |
| More than `MAX_CLOSES_PER_RUN` closes in one pass | The rest wait for the next pass |

If Claude can't be reached (`MODEL_FALLBACK=rules`), tickets under an EcoCash topic get the standard reply for their latest attempt, through the same rules. Tickets where the branch has already answered the agent are escalated. Tickets found by keyword wait for the next pass.

## The monitor (replaces ecocash-registration-monitor)

`python -m nmbz monitor` follows `journalctl -u ecocashreg.service -o json -f` on the integrations host and saves one row per attempt in `ecocash_link_attempts`. What changes from the Java:

- **Every attempt is saved.** Successful links and delinks are saved, not just failures. This lets the agent confirm that a retry or delink worked, and the anomaly watch measure a failure rate.
- **Delinks are recorded.** `ECOCASH_DEREGISTRATION` requests from the branch are now saved.
- **Missing answers are caught.** A request with no answer from EcoCash within `MONITOR_NO_RESPONSE_SECONDS` is saved as `no_response`. The Java left it waiting, where it could pick up someone else's error later.
- **Each request counts once.** The service logs each request two or three times, milliseconds apart. These count once.
- **No guessing between clients.** The loose `Message:` lines are only used when exactly one request is waiting.
- **Nothing is lost on a restart.** The journal position is saved, so after a restart or reboot the monitor resumes where it stopped. Saving is idempotent (unique `attempt_key`), so nothing is duplicated. The Java started again at the end of the journal (`-n 0`) and lost what happened while it was down.
- **History stays readable.** Failed links are still written to `failed_ecocash_registrations` (`MONITOR_LEGACY_TABLE=true`), so existing reports keep working.
- **No root.** It runs as an unprivileged user in the `systemd-journal` group.

Checked against the 6 April log: 134 attempts, 61 linked and 73 failed, matching the log line for line.

**Cutover on the integrations host:**

```bash
# 1. The table (once). The monitor also creates it if its database user may.
psql -h 192.168.254.146 -p 5422 -d ecocash_monitor -f sql/ecocash_monitor.sql
# 2. Install (see "Install" below) and fill /etc/nmbz-agents/monitor.env from deploy/monitor.env.example
# 3. Optional: load history from a journal export, to check the parser on your data first
journalctl -u ecocashreg.service --since "2026-10-01" --no-pager > /tmp/ecoreg.txt
sudo -u ecomonitor .venv/bin/python -m nmbz --env-file /etc/nmbz-agents/monitor.env monitor --from-file /tmp/ecoreg.txt --dry-run
sudo -u ecomonitor .venv/bin/python -m nmbz --env-file /etc/nmbz-agents/monitor.env monitor --from-file /tmp/ecoreg.txt
shred -u /tmp/ecoreg.txt
# 4. Swap: stop the Java, start the Python
sudo systemctl disable --now ecocash-registration-monitor
sudo cp deploy/ecocash-monitor.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now ecocash-monitor && journalctl -u ecocash-monitor -f
```

## Anomaly watch

Every `ANOMALY_INTERVAL_MINUTES`, the watch compares the last 24 hours, and the last 2 to 3 hours, with the usual level over the past 14 days. It looks at:
- EcoCash ticket volume.
- The link failure rate.
- Each kind of EcoCash error.
- System errors and timeouts in the last hours, the earliest sign of an EcoCash outage.
- Overall request volume. A drop points at USSD or the registration service.

It also compares the failed links with the successful ones in the same window: channel, currency, network, account product and CIF series, ID format, and time of day. A detail much more common among failures points at the cause, for example "account opening (iApprove), ZiG accounts".

Thresholds in code decide the severity. Claude only writes the explanation, from counts with no customer data. Admins get an SMS and an e-mail when an incident opens, when it gets worse, every `ANOMALY_REALERT_HOURS` while it lasts, and when it's over. While an incident is open, each ticket starts with an operations line, and "try again later" closes are blocked.

## Attachments

Text, CSV, e-mail, Word and Excel files are read on the server. Images and PDFs are read by Claude in a separate call that returns facts only: an error screenshot from the phone, a Universal dashboard error, or an ID copy. For an ID copy it reports only the national ID number, to compare with the ID the bank sent. It never transcribes names, dates of birth or addresses. Skips and limits are the same as the SMS agent's (`ATTACHMENT_*`).

```bash
python -m nmbz --desk ecocash attachments --ticket 482913          # what the agent would see, masked
```

## Privacy

- Mobile numbers, account numbers, national IDs and e-mail addresses are masked before anything reaches Claude. Lookups use the real values on the server, and their results are masked again.
- Names from the registration request never reach the model, only whether they match the ticket. Names typed in the ticket text are not masked.
- With `ATTACHMENT_MODE=model`, images and PDFs go to Claude as they are. `local` keeps them on the server.
- The decision log stores masked text only, and the e-mail outbox erases message bodies once they are sent.
- The monitor stores what the Java stored (mobile, account, ID, names) plus the EcoCash name and transaction ID, in the same database. It doesn't store the IMEI, location or access token, which the registration service logs.

Sending ticket content to the Claude API needs your IT/risk sign-off, as for the SMS agent.

## Install

RHEL, Python 3.9 or newer. The same code goes on both hosts.

**Service host:** install the service as in the [README](../README.md#install); the EcoCash desk is part of it.

**Integrations host** (the monitor only, with no Claude key):

```bash
sudo useradd --system --home /opt/nmbz-agents --groups systemd-journal ecomonitor
sudo mkdir -p /opt/nmbz-agents/state /etc/nmbz-agents && sudo cp -r . /opt/nmbz-agents && sudo chown -R ecomonitor: /opt/nmbz-agents
cd /opt/nmbz-agents && sudo -u ecomonitor python3 -m venv .venv && sudo -u ecomonitor .venv/bin/pip install -r requirements.txt
sudo cp deploy/monitor.env.example /etc/nmbz-agents/monitor.env && sudo chmod 640 /etc/nmbz-agents/monitor.env
```

If the host can't reach PyPI, build wheels elsewhere with `pip download -r requirements.txt -d wheels` and install with `--no-index --find-links wheels`.

The desk shows in the service's console (`python -m nmbz portal`, port 8085) under **EcoCash**, with the same pages as every desk, including the approval desk and a live view of the pass in progress (the health strip and the Runs page show the ticket being worked and each lookup, masked).

**Keyboard:**
- <kbd>Ctrl</kbd>+<kbd>K</kbd> jumps to any ticket or page.
- On the approval desk, <kbd>J</kbd>/<kbd>K</kbd> move through the queue, <kbd>A</kbd> approves, <kbd>E</kbd> edits and <kbd>X</kbd> rejects. Approving and rejecting each ask for <kbd>Enter</kbd> to confirm.
- <kbd>?</kbd> lists the keys.

**Before going live:**
1. Start the Python monitor and let it record for a few days, so the lookups and the anomaly baseline have data.
2. Run `check`. It should find `OSTICKETS_NEW`, `ecocash_monitor`, Postilion and the standby database, and count the open EcoCash tickets and the last day's attempts.
3. Create the staff account the agent posts as, and set `REPLY_STAFF_ID`.
4. Try `lookup` on a client you know, for example `python -m nmbz lookup --mobile 0771234567`.
5. Run in shadow for a week (`ECOCASH_AGENT_MODE=shadow`) and review the decisions in the console. Then run `audit --since …` to replay closed tickets.
6. Go live with `ECOCASH_AGENT_MODE=live`. `notes` is an intermediate step that posts internal recommendations only.

## Commands

```bash
python -m nmbz --desk ecocash check [--ping-model]
python -m nmbz --desk ecocash run [--mode shadow|notes|live] [--ticket N] [--limit N]
python -m nmbz lookup --mobile 0771234567 | --account 86000013101001 | --id-number 63-1234567K12
python -m nmbz monitor [--from-file export.txt] [--dry-run]
python -m nmbz --desk ecocash attachments --ticket N [--local]
python -m nmbz --desk ecocash anomalies [--alert] [--no-model]
python -m nmbz --desk ecocash audit --since 2026-09-01
python -m nmbz --desk ecocash compare --since 2026-10-01
python -m nmbz --desk ecocash report --since 2026-10-01 --out decisions.csv
python -m nmbz --desk ecocash notify-test --to you@nmbz.co.zw
python -m nmbz portal
touch state/ecocash/STOP / rm state/ecocash/STOP                     # kill switch
```

## Found in the Java repository

1. **A production password is in source code.** `src/main/resources/application.yml` holds the PostgreSQL user and password as defaults. Change the password, and keep it only in `/etc/nmbz-agents/monitor.env` (mode 640).
2. **Real customer data is in the repository.** `EcocashJournalParserTest.java` holds a real customer's name, mobile, national ID and account. `ecoreg log.txt`, a production journal export, holds those details for hundreds of customers. Remove both from the repository and its history. This project's tests use invented data only.
3. **The registration service logs secrets and personal data at INFO.** This is `ecocashreg.service`, not the monitor. It logs the access token and the full request (names, national ID, account, mobile, IMEI, location). The monitor doesn't need the token, IMEI or location, so these can be removed from the logs.
4. **Branch delinks fail at EcoCash.** The registration service puts the client's mobile number, not the bank account, in EcoCash's account-number field (`bankAccNumber`). EcoCash accepts this for links, but both branch delinks on 6 April were rejected with TC30174 ("AccountNumber parameter is incorrect"). Worth checking with EcoCash what a delink expects, since the agent's advice for 20503 relies on branches being able to delink.

## To confirm

- **`ECOCASH_HELP_TOPICS`:** the exact osTicket help topic name for EcoCash ("EcoCash" is assumed).
- **"Universal dashboard":** whether this is what branches call the screen they delink on.
- **`POSTILION_DB_NAME`:** the name Metabase shows for Postilion, and what the status values in `eftc_ecocash` mean.
- **`CORE_SQL_*`:** whether you want the optional core-banking checks.
- **The monitor host:** that `psycopg` installs on the integrations host, or set `MONITOR_STORE=metabase` if Metabase's connection to `ecocash_monitor` may write.
- **The journal:** that the `ecomonitor` user can read the journal (`sudo -u ecomonitor journalctl -u ecocashreg.service -n 5`).

## Layout

What every desk shares (the reasoning loop, the pass, osTicket reads and writes, masking, approvals, attachments, the outbox, the anomaly service, the console) is in `agentcore/`, under the same file names; the desk's files below hold what is particular to it.

| Path | What it is |
|---|---|
| `ecocash/monitor/` | The journal parser, follower and store that replace the Java monitor |
| `ecocash/ecocash.py` | EcoCash codes, meanings and standard wording; the Metabase lookups (attempts, Postilion, core banking) |
| `ecocash/osticket.py` | EcoCash ticket discovery, form fields, identifiers, thread, attachments and writes |
| `ecocash/tools.py` | The lookups the model can call, with masking; the evidence the guardrails use |
| `ecocash/guardrails.py` | Rules in code |
| `ecocash/loop.py`, `ecocash/runner.py` | How the ticket is shown to Claude, and which tickets, notes and log columns; the loop and the pass are in `agentcore/` |
| `ecocash/rules.py` | Decisions when Claude can't be reached |
| `ecocash/anomaly/` | Counts, thresholds, the failed-vs-linked comparison, summaries and admin alerts |
| `ecocash/attachments.py` | Reading ticket attachments |
| `ecocash/approval.py` | Which replies wait for a person, and the waiting replies |
| `ecocash/notify.py` | Outbox: branch e-mails and admin alerts |
| `ecocash/portal/` | This desk's part of the console: `data.py` (outcome groups), `vocab.py` (status labels and charts) |
| `ecocash/sql/ecocash_monitor.sql` | The attempts table |
| `deploy/ecocash-monitor.service`, `deploy/monitor.env.example` | The monitor's unit and env file |
| `ecocash/prompts/`, `ecocash/templates/` | Instructions for the agent and incident summaries; branch e-mails |
| `tools/demo_ecocash.py` | This desk's demo data for `tools/build_preview.py` |

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q tests/ecocash
```

The tests run against an in-memory fake of Metabase that answers the agent's SQL, with a scripted model, and use invented data only.
