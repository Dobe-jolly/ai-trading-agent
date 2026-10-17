# NMB Connect

Part of the NMBZ helpdesk agents service. Installing, settings, commands and the console are in the
[README](../README.md); this page covers what the NMB Connect desk does. Its settings take the prefix `NMB_CONNECT_`
when they should differ from the other desks (`NMB_CONNECT_AGENT_MODE=live`).

The desk works tickets about getting into NMB Connect (the app, the USSD menu and NMB Connect online): registration
that fails ("already registered, kindly log in"), logins that fail, OTPs that do not arrive, a forgotten username, a
changed mobile number ("your mobile number does not match"), and accounts that do not show. Most of these come down
to a handful of fixes: delete the profile and register again, update the number through ICT Data Science, check the
spam folder, reactivate the account. The Java engine checked that the details were present and escalated; the rule
set for OTP and Forgot Username was written but never switched on.

Failed transfers and payments on NMB Connect are the failed-transactions desk's, including those filed under
NMB Connect / USSD / Failed Transactions.

```
osTicket ── every 15 min: open tickets under NMB_CONNECT_HELP_TOPICS (not the failed-transaction ones)
   │
   ▼
Claude reads the masked ticket + attachments (screenshots of the error, read word for word)
   ├─ get_connect_profile         CONECT 2.0 REG: the profile(s), the OTP number, created, active / locked
   ├─ check_core_banking          STANDBY DB: CIF, accounts and their state, mobile and e-mail on file, IDCCOB
   ├─ get_app_activity            CONECT 2.0: recent app and USSD requests, last success and failure
   ├─ find_registration_failures  the registration log analyzer: failed registrations and why (optional)
   └─ get_sms_to_number           SOCKET SMS DB: OTPs sent and delivered, never their text (optional)
   │
   ▼ assess_access_issue(problem) ─► where the client stands + the standard outcome, worked out in code
   ▼ submit_decision ─► guardrails (code) ─► reply and close / keep open for the branch / escalate
```

## The outcomes

Claude names the problem; the code works out where the client stands from what the lookups returned.

| Problem | Where the client stands | What happens |
|---|---|---|
| any | not registered (no profile for the CIF) | Close: register the client; any mobile format works |
| OTP not received | registered, the ticket's number | Close: blocked or spam SMS folder steps; reply if it still fails. When the SMS gateway lookup shows the last OTP delivered, the reply says so |
| OTP not received | OTPs failing at the gateway | Escalate to ICT Data Science |
| Forgot username | registered, the ticket's number | Close: Forgot Username on the login screen (account number, mobile, OTP) |
| any | the profile has another number | Keep open: the OTP goes to that number; if the client changed number, capture and authorise it on the CIF in IDCCOB, then ICT Data Science updates the profile. Checked again when the branch replies |
| Number changed | the profile already has the new number | Close |
| Cannot register | a profile already exists | Keep open: delete it on the dashboard and have the client register again |
| Cannot register, login, accounts | the profile was created after the ticket was raised | Close: the fix is in place |
| Cannot register | no profile, failed registration logged | By the analyzer's reason: e-mail or password did not match (close: register with the e-mail on the bank profile), temporary error (close: try again), duplicate customer profile or unknown (escalate) |
| Cannot register | no profile, nothing logged | Ask for a screenshot of the error and the mobile used |
| Account not showing | the account is active under the client's CIF | Keep open: delete the profile and have the client register again; it picks up all accounts |
| Account not showing, or app failing | the account is dormant, closed or frozen | Close: what the client does at the branch |
| Account not showing | the account is under another CIF | Escalate: confirm who owns it |
| any | more than one profile for the CIF, or the ticket's number on another customer's profile | Escalate to ICT Data Science |
| Cannot log in | registered, right number, locked or not | Escalate (locked: unlock or delete; not locked: check the login history) |
| Block mobile banking or EcoCash | | Close: the block option on the dashboard; reply with a screenshot if it fails |
| anything else (transfers, corporate authorisers, VISA withdrawals, ...) | | Escalate with what the lookups showed |

The wording is in `nmb_connect/tools.py` (`assess`). Claude adapts it to the thread; the standard wording shows next to
what was posted in the console. "Keep open" replies ask the branch to act and report back: the ticket stays open,
the branch gets an e-mail, and when they reply the client is looked up again, so the ticket closes on proof (the new
profile, the new number) rather than on the branch's word. With no reply in `REQUEST_INFO_TIMEOUT_HOURS`, a person
takes over.

## What Claude sees of the profile

The profile table's columns are not documented, so the desk reads each row whole and filters it in code
(`nmb_connect/sources.py`):

- **Never returned:** passwords, PINs, OTPs, usernames, salts, tokens, hashes, security questions, device, IMEI, IP
  and location data, card data, registration codes, and names, birth dates and addresses.
- **Compared in code, masked for the model:** the CIF, the mobile, the e-mail.
- **Returned:** dates and state-like columns (status, active, locked, login attempts, channel, type).
- **Not shown:** any other column. Only its name is visible, in `check`.

Counters and dates about secrets (`pin_tries`, `password_expired`, `otp_sent_at`) count as state. The SMS gateway
lookup returns each message's kind (OTP, alert) and delivery status, never its text, which holds the codes.

Replies may name OTPs and passwords, but the code blocks any reply that asks for one (or for a PIN or card code),
or writes one out, or gives a username. Usernames are never given out: the client recovers theirs with Forgot
Username.

## Rules the code enforces (`nmb_connect/guardrails.py`)

| Rule | Effect |
|---|---|
| The ticket closed, changed topic or got a new entry while Claude worked, or a lookup failed before a close | Nothing; looked at again next pass |
| Claude looked up a CIF, account, mobile or ID that is not the client's | Escalate |
| A close without an assessment, or on an outcome that needs a person | Escalate |
| A close on a keep-open outcome (the branch must act first) | Kept open: the reply is posted as a request |
| The reply says the client is not registered when a profile was found, or asks for a profile to be deleted when the assessment does not | Escalate |
| The reply asks for or gives out an OTP, PIN, password, username or card code; has a number Claude was never shown, a link, or template leftovers | Escalate |
| Confidence below `CONFIDENCE_THRESHOLD` | Escalate |
| The agent has already replied `MAX_INFO_REQUESTS` times | Escalate |
| More than `MAX_CLOSES_PER_RUN` closes in a pass | The rest wait for the next pass |

Whose client it is: a CIF on the ticket is the client's. With no CIF, the CIF of the account on the ticket is. An
account under another CIF stays someone else's (a joint or corporate account, or a typo), and so does a profile found
by the ticket's mobile under another CIF. Claude can see that such a profile exists, but cannot look up its owner.

**Approvals.** A reply waits in the console when Claude reasoned beyond the standard outcome, or when its action
differs from the assessment's. An example: deleting the profile for a login problem, where the standard outcome is
to escalate.

**Without Claude** (`MODEL_FALLBACK=rules`), a ticket with no CIF, account or mobile is asked for them; a ticket where
the branch has answered the agent is escalated; everything else waits for the next pass.

## Settings

| Setting | Default | |
|---|---|---|
| `NMB_CONNECT_HELP_TOPICS` | NMB Connect / Registration, OTP, Account, System Issue; NMB Connect / USSD; USSD / Account, System Error, Pin | Topics and their subtopics |
| `NMB_CONNECT_EXCLUDE_TOPICS` | NMB Connect / USSD / Failed Transactions | Left to the failed-transactions desk |
| `CONECT_REG_DB_NAME` | CONECT 2.0 REG | Required |
| `CONECT_DB_NAME`, `STANDBY_DB_NAME`, `SOCKET_SMS_DB_NAME` | as for the other desks | Optional: a missing one switches its lookup off |
| `REG_FAILURES_DB_NAME` | (empty) | The registration log analyzer's database (`registration_failures`), once Metabase has it |
| `NMB_CONNECT_PROFILE_TABLE`, `_CHANNELS_TABLE`, `_HISTORY_TABLE` | customer, user_channels, history | Channels and history are read when they exist |
| `NMB_CONNECT_ACTIVITY_DAYS`, `NMB_CONNECT_SMS_DAYS` | 14, 7 | How far back activity and SMS go |
| `PENDING_CHANGES_SQL` | (empty) | Shared: Intellect changes awaiting authorisation, shown with the core banking record |

## Commands

```bash
python -m nmbz check --desk nmb_connect                     # databases, open tickets per topic, the profile table's columns
python -m nmbz connect-lookup --cif 1234567 --mobile 0771234567   # what the lookups return for a client; no model
python -m nmbz run --desk nmb_connect --ticket 482913       # one ticket
python -m nmbz audit --desk nmb_connect --since 2026-09-01  # replay closed NMB Connect tickets
```

## Before going live

1. Run `python -m nmbz check --desk nmb_connect`. It lists the columns of `customer`, `user_channels` and `history` in
   CONECT 2.0 REG. These were never documented: check that the mobile is `mobile_number` and the CIF is
   `customer_number` (as the Java rules use), and look at how a deleted or locked profile is marked. The desk treats
   a profile as gone when a status says deleted, removed, retired, closed, inactive or disabled, when a flag such as
   `deleted` or `retired` is set, or when `active` / `enabled` is false; and as locked when a status or flag says
   locked, blocked, suspended or frozen. If yours differ, tell me and I will add them.
2. Run `connect-lookup` on a client you know, with and without a profile, and compare with the dashboard.
3. Add the registration log analyzer's database (`registration_failures` on 192.168.254.146:5422) to Metabase
   with a read-only user, and set `REG_FAILURES_DB_NAME`.
4. Confirm with ICT whether NMB Connect OTPs go through the SOCKET SMS gateway. If they do not, the OTP delivery check
   shows nothing, and the desk falls back to the handset steps.
5. Run in shadow (`NMB_CONNECT_AGENT_MODE=shadow`) for a week, review the decisions in the console, replay closed
   tickets with `audit`, then move to notes, then live.

The router moves tickets to these topics on its own once this desk is on (it works them), so tickets filed under
HelpDesk / Logging Ticket that are really NMB Connect problems reach this desk in the same pass.


## query_data: tables beyond the lookups

The agent can also read approved tables of the NMB Connect registration database (CONECT 2.0 REG) with `query_data`
(`agentcore/dataquery.py`, catalogue `agentcore/data_catalogue.json`): profiles (status, KYC document statuses,
notification settings), channel enrolments, logins per channel and failed attempts, credentials enabled or disabled
and why, profile activity with error messages, account-linking retries, linked cards and fraud alerts. No names, ID
numbers, e-mails, addresses, device data, usernames, passwords, credentials or card numbers are listed. Each query is
matched on a CIF, account or mobile the ticket gives or an earlier lookup found to be the client's (tables keyed by
the profile's internal id are reached through the profile table); another client's number is refused. At most 25
rows, masked; counts without a match carry no numbers. `python -m nmbz check --desk nmb_connect` tries each table's
columns without reading rows; `QUERY_DATA=false` switches it off.

## Staff knowledge and suggested replies

What the helpdesk taught this desk lives in staff notes (Teach the agent), not in the prompt or code:
`nmb_connect/knowledge_seed.json` holds them, and `python -m nmbz knowledge-seed --desk nmb_connect` proposes the ones not in the
console yet. An Admin switches each on; from then on staff can change or switch off any of them without a code change.
The notes: deleting an old profile when registration says already registered, changing a number (IDCCOB, then ICT Data Science), OTP and Forgot Username steps, not registered, failed registrations by reason, duplicates and locked profiles, dormant, frozen or closed accounts, and blocking access.

`assess_access_issue` still works out where the client stands from the lookups and gives a suggested outcome in the helpdesk's usual wording; the agent writes the reply that fits the ticket. The code keeps what protects the client: only the client's own CIF, account or mobile; a close only on an assessment whose status allows it; keep-open statuses stay open until the branch acts; and the reply may not say the client is not registered when a profile was found, or ask for a profile to be deleted unless the assessment allows it.
