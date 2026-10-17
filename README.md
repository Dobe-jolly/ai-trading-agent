# NMBZ helpdesk agents

One service that works osTicket tickets for the ICT service desk. Each kind of ticket has a **desk**: a set of
read-only lookups into the bank's systems (through Metabase), rules in code that the model cannot override,
and standard wording. Claude reads each ticket, investigates with the desk's lookups, and proposes a reply,
a request for information or an escalation; the desk's guardrails decide what actually happens.

| Desk | Tickets | What it checks | Details |
|---|---|---|---|
| `router` | Tickets under a catch-all topic (HelpDesk / Logging Ticket) or with none | Which help topic the ticket belongs under: the active topics, and where similar closed tickets were filed. Moves it there when an agent works the topic, otherwise suggests it to staff | [docs/router.md](docs/router.md) |
| `sms_alerts` | SMS Alerts (and SMS issues filed under other topics) | Registration in core banking, the CAE request, dispatch and the SMS gateway | [docs/sms_alerts.md](docs/sms_alerts.md) |
| `ecocash` | EcoCash linking | Every link and delink attempt (recorded by the EcoCash monitor), EcoCash's answer, the Postilion link record | [docs/ecocash.md](docs/ecocash.md) |
| `failed_txn` | Failed ZIPIT, EcoCash B2W/W2B, NMB Connect, airtime, ZESA and Postilion transactions | The transaction in Postilion, NMB Connect and the billers, or internet banking; then the debit and any refund in core banking | [docs/failed_txn.md](docs/failed_txn.md) |
| `nmb_connect` | NMB Connect registration, login, OTP, forgotten username, changed number, accounts not showing | The NMB Connect profile (filtered in code), the CIF's accounts and mobile in core banking, app and USSD activity, failed registrations, OTP delivery | [docs/nmb_connect.md](docs/nmb_connect.md) |

Every desk runs in the same pass, writes to osTicket the same way, and shows in the same console. How this maps onto the earlier on-prem plan, and what is left of it, is in [docs/architecture.md](docs/architecture.md).

```
every 15 min ─► python -m nmbz run ─► Router ─► SMS Alerts ─► EcoCash ─► Failed transactions ─► NMB Connect   (DESKS, in order)
                                        │
            each desk: its open tickets ─► skip rules ─► Claude + the desk's lookups (masked)
                                        ─► guardrails ─► approval for off-script replies ─► osTicket ─► e-mail to the branch
                                        ─► its own decision log (state/<desk>/agent.sqlite)

python -m nmbz portal ─► one console: a tab per desk, approvals, anomalies, controls, pause every desk
```

## Layout

| Path | What it is |
|---|---|
| `agentcore/` | What every desk shares: Metabase client, masking, osTicket reads and writes, the reasoning loop, the pass (`runner.py`), guardrail basics, approvals, attachments, e-mail outbox, anomaly watch, settings reader, the console API and pages (`portal/web/` is the built supervision portal, `portal/static/` the classic page) |
| `sms_alerts/` | The SMS Alerts desk: its checks, lookups, triage of other topics, prompts, wording |
| `ecocash/` | The EcoCash desk, and the monitor that replaces the Java `ecocash-registration-monitor` |
| `failed_txn/` | The failed-transactions desk: Postilion, NMB Connect and billers, internet banking and core-banking lookups |
| `nmb_connect/` | The NMB Connect desk: profile, core-banking, activity, failed-registration and OTP-delivery lookups, and the assessment of where the client stands |
| `router/` | The router: the help-topic catalogue, closed-ticket lookups, and the topic change |
| `nmbz/` | The service: the command line (`python -m nmbz`), the desk list, the console server |
| `tests/` | `core/` and one folder per desk (invented data only) |
| `portal-ui/` | Source of the supervision portal (React, TypeScript). Only needed to change the portal: the built files are in `agentcore/portal/web/`, so the server needs no Node |
| `tools/build_preview.py` | Builds `preview.html`: the classic console with every desk's demo data (`tools/demo_<desk>.py`) |
| `tools/demo_console.py` | Runs the real console over that demo data, with an admin and a viewer sign-in, for working on the portal |
| `docs/` | One page per desk, the architecture against the earlier plan, and [the log pipeline](docs/logs.md) |
| `deploy/` | systemd units and the monitor's env file |
| `state/` | Decision logs, pause files, console users (one folder per desk) |

## Install

RHEL, Python 3.9 or newer.

```bash
sudo useradd --system --home /opt/nmbz-agents nmbzagents
sudo mkdir -p /opt/nmbz-agents /etc/nmbz-agents
sudo cp -r . /opt/nmbz-agents && cd /opt/nmbz-agents
sudo python3 -m venv .venv && sudo .venv/bin/pip install -r requirements.txt
sudo cp .env.example /etc/nmbz-agents/agents.env && sudo chmod 640 /etc/nmbz-agents/agents.env   # fill it in
sudo chown -R nmbzagents: /opt/nmbz-agents/state /opt/nmbz-agents/reports && sudo chgrp nmbzagents /etc/nmbz-agents/agents.env
sudo -u nmbzagents .venv/bin/python -m nmbz --env-file /etc/nmbz-agents/agents.env check --ping-model
sudo -u nmbzagents .venv/bin/python -m nmbz --env-file /etc/nmbz-agents/agents.env portal-user add dobe --role admin
sudo cp deploy/nmbz-agents.{service,timer} deploy/nmbz-console.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now nmbz-agents.timer nmbz-console
journalctl -u nmbz-agents -f
```

If the host can't reach PyPI, build wheels elsewhere with `pip download -r requirements.txt -d wheels` and
install with `--no-index --find-links wheels`.

The EcoCash monitor runs on the integrations host, from the same code, with its own small env file; see
[The monitor](docs/ecocash.md#the-monitor-replaces-ecocash-registration-monitor).

## Settings

One `.env` for every desk ([.env.example](.env.example)). A setting without a prefix applies to every desk;
with a desk's prefix it applies to that desk only and wins there:

```
AGENT_MODE=shadow             # every desk decides and logs only...
ECOCASH_AGENT_MODE=live       # ...except EcoCash, which acts
```

Prefixes: `ROUTER_`, `SMS_ALERTS_`, `ECOCASH_`, `FAILED_TXN_`, `NMB_CONNECT_`. One exception: the router ignores `AGENT_MODE` and stays in shadow until `ROUTER_AGENT_MODE` is set, since it changes help topics. Each desk keeps its own decision log, pause file and mode hold under
`state/<desk>/`; console users (`state/portal_users.json`) are shared. `DESKS` picks which desks a pass visits
and in what order. `python -m nmbz desks` shows each desk's mode and log.

## Claude and the bank's data

Claude is approved for use across the bank (October 2026). What it gets, on every desk:

- **Masked text.** The ticket, the thread and every lookup result, with account numbers, references, mobiles,
  national IDs and e-mail addresses replaced by tokens (`<NUMBER_1>`, `<MOBILE_1>`). Lookups run on the server with
  the real values; replies get them back before they are posted.
- **Attachments**, with `ATTACHMENT_MODE=model`: text files masked like the thread; images and PDFs as they are, in a
  separate call that returns facts only. `local` keeps images and PDFs on the server.
- **Nothing else.** No database access, no SQL, no credentials: Claude can only call the desk's lookups (and the
  lookups staff wrote for switched-on notes, below), and only with values from the ticket. A lookup asked for a
  number the ticket never gave is refused, and the decision goes to a person.
- **Ticket text is data.** The ticket is fenced in `<ticket>` tags and the prompt says never to follow instructions
  in it; the code enforces what matters (lookups, guardrails, approvals) whatever the model says.

Two ways to reach Claude, chosen by `MODEL_BACKEND` (default `auto`): the Claude API with `ANTHROPIC_API_KEY` (the
setup for the service), or, until there is a key, a Claude subscription token (`CLAUDE_CODE_OAUTH_TOKEN`, from
`claude setup-token`) through the Claude Agent SDK (`agentcore/sdk_backend.py`, Python 3.10+). The SDK route runs the
same agent with the same lookups, masking, rules and log; Claude Code's own tools, settings and memory are switched
off and sessions are not saved. Usage counts against that person's subscription limits.

The decision log and the console hold masked text only, including what the model itself wrote. The service log keeps only
the last four digits of long numbers and never prints tokens or passwords (`agentcore/logsafe.py`). The agent host needs outbound HTTPS to `api.anthropic.com`;
for the bank's proxy, see the Claude section of [.env.example](.env.example).

## Commands

```bash
python -m nmbz check [--desk D] [--ping-model]       # settings, Metabase databases, prompts, for each desk
python -m nmbz run [--mode shadow|notes|live]        # one pass over every desk (the timer runs this)
python -m nmbz run --desk ecocash --ticket 482913    # one ticket on one desk, ignoring the skip rules
python -m nmbz portal                                # the console (port 8085)
python -m nmbz portal-user add|remove|list NAME [--role admin|viewer]
python -m nmbz desks                                 # which desks are on, their mode and log
python -m nmbz audit --desk D --since 2026-09-01     # replay closed tickets, compare with what was posted
python -m nmbz compare --desk D --since 2026-10-01   # shadow decisions vs what actually happened
python -m nmbz report --desk D --since 2026-10-01    # the decision log as CSV
python -m nmbz anomalies --desk D [--alert]          # the anomaly watch now
python -m nmbz attachments --desk D --ticket N       # what the agent would read from a ticket's files
python -m nmbz notify-test --desk D --to you@nmbz.co.zw ; python -m nmbz notify-retry --desk D
python -m nmbz check-account --account N --phone P   # SMS Alerts: one account, no model
python -m nmbz lookup --mobile 0771234567            # EcoCash: a client's link attempts, no model
python -m nmbz monitor                               # EcoCash: follow ecocashreg.service (its own unit)
python -m nmbz find-transactions --account N [--amount A] [--date D]   # Failed transactions: every source, no model
python -m nmbz connect-lookup --cif C | --account A [--mobile M]   # NMB Connect: profile, core banking, activity, no model
python -m nmbz topics                                # Router: the help topics, and which it moves to on its own
```

`python -m nmbz <command> --help` shows a command's options. Exit codes: 0 OK, 1 config error, 2 Metabase
unreachable, 3 some tickets errored (retried next pass; the unit treats it as success). A pass of every desk
returns the worst code; one desk failing does not stop the others.

## Console

`python -m nmbz portal` serves one console for every desk on `127.0.0.1:8085`. Publish it through the F5 or
nginx with TLS (sign-in is HTTP Basic, users from `python -m nmbz portal-user`). At `/` is the supervision portal;
the earlier single-page console is still at `/classic`.

The portal shows all desks together or one at a time (the desk buttons at the top, with replies waiting, open
incidents, and a paused or stale desk). Every page reads the same `/api/d/<desk>/...` endpoints as the classic page
and refreshes every 30 seconds.

- **Overview:** the last 24 hours, outcomes over 7 days, what needs attention, and each desk's health.
- **Approvals:** off-script replies waiting for a person, oldest first. Each opens the ticket page.
- **Ticket page** (`/tickets/<number>`): what the agent read, every lookup with what was sent and what came back,
  its recommendation and reply (beside the desk's standard wording), what the rules did, where it stands, and the
  approve, edit or reject step.
- **Open tickets, Decisions, Review:** the last pass, every decision with filters and CSV export, and marking
  decisions right or wrong (accuracy per desk).
- **Anomalies, Emails & alerts, Runs:** the hourly watch, what was sent outside osTicket (re-send failed ones), and
  each pass with the one in progress.
- **Knowledge:** what staff have taught each desk (below): notes waiting for an Admin, notes in use with how often
  decisions used them and how much prompt space they take, and the history.
- **Controls:** pause a desk or every desk, hold a desk at notes or shadow, and the control log.

### Teaching a desk

The last step on a ticket's page, **Teach the agent**, is where a person gives the desk what it needs for tickets like
that one: the logic in plain words (what to check, in which table or log, what each result means), the log lines or
rows that matter, and optionally a **lookup**: a read-only query they write, with `{{slots}}` for values from the
ticket. For example:

```sql
SELECT TOP 20 datetime_req, message_type, rsp_code_rsp FROM post_tran_leg WITH (NOLOCK)
WHERE from_account_id = {{account}} ORDER BY datetime_req DESC
```

- **Try it on this ticket** re-checks the open ticket with the desk's agent in shadow, with the note, and shows what
  it would do next to what it did. Nothing is posted, e-mailed or logged as a decision (one ticket's worth of model
  calls; 10 tries per person per hour).
- **Propose for future tickets** saves it as a note (K12), masked like the rest of the log. **An Admin who did not
  write it** switches it on from the Knowledge page (maker-checker); from the next pass every ticket under its help
  topic (or the whole desk) gets it, and the lookup becomes a tool named `k12_<title>`.
- A lookup must be one `SELECT` (or `WITH`) with a row limit of at most 200, with no write keywords, no `;` and no
  numbers typed in. The model only supplies slot values: masked tokens from this ticket (`<NUMBER_1>`), dates, short
  codes or amounts. Metabase binds them as template tags, so model text never becomes SQL; the agent sees at most 30
  rows, masked. Give the Metabase account read-only rights: that is the real limit.
- A note can't change the desk's rules: what it may post, when a person must approve, and its mode stay in code.
- Every decision records the notes that were on, the ones it used (`knowledge_used`) and the prompt version, so a bad
  decision can be traced to a note and that note switched off.

Switched-on notes share `KNOWLEDGE_MAX_CHARS` (16,000) of each desk's prompt; the newest go in first and the Knowledge
page says when one is left out. The Review page shows, per desk, how many reviewed closes were right and how many
closed tickets were reopened since (checked on each pass for 30 days).

### Roles

Roles come from the users file. **Viewers** read everything and can mark decisions right or wrong. Anyone signed in can teach a desk and try a note.
**Admins** can also approve, edit and reject replies, switch notes on or off (not their own), pause and hold desks, and
re-send e-mails. The server enforces this (403 for
a viewer), and the portal hides what a viewer can't do. Going live is never a portal action: it stays a server
setting, and a hold can only make a desk more careful.

Keys, as on the classic page: <kbd>Ctrl</kbd>+<kbd>K</kbd> goes to any ticket or page; on a reply opened from
Approvals, <kbd>J</kbd>/<kbd>K</kbd> next and previous, <kbd>A</kbd>pprove, <kbd>E</kbd>dit, <kbd>X</kbd> reject
(<kbd>Enter</kbd> confirms, <kbd>Esc</kbd> backs out); <kbd>?</kbd> lists them.

`GET /healthz` needs no sign-in: 200 when every desk has finished a pass within `PORTAL_STALE_MINUTES` (or
the service is paused on purpose), 503 otherwise.

`python tools/build_preview.py` writes `preview.html`, the classic console with invented data for every desk.

### Changing the portal

The portal is built from `portal-ui/` into `agentcore/portal/web/`, and the built files are committed. To work on
it you need Node 20.19 or newer, on a workstation rather than the agent host:

```bash
python tools/demo_console.py            # the real console over invented data: dobe or checker (admins) or viewer, password demo-password-123
cd portal-ui && npm install && npm run dev   # http://localhost:5173, API calls go to the demo console on :8085
npm run build                           # type-checks, then writes agentcore/portal/web/
```

Set `PORTAL_CONSOLE=http://host:port` to point the dev server at another console. Approvals, reviews, holds and
pauses made against the demo console go to its throwaway logs under `state/demo/`.

## Adding a desk

A desk is a package next to `sms_alerts/`, `ecocash/` and `failed_txn/` (the smallest, so the one to copy):

1. Its lookups (`tools.py`, the read-only SQL through Metabase) and the evidence they leave.
2. Its prompt (`prompts/system_prompt.md`) and standard wording.
3. Its guardrails (`guardrails.py`, on top of `agentcore.guardrails`).
4. A `runner.py` subclassing `agentcore.runner.DeskRunner`: which tickets it owns, its notes, its log columns.
5. `config.py` with its prefix, `portal/server.py` and `portal/data.py` (thin), and `portal/vocab.py` (how the
   console words its statuses and charts).
6. An entry in `nmbz/desks.py`, tests under `tests/<desk>/`, and invented data for the preview in
   `tools/demo_<desk>.py`.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Every test runs against in-memory fakes of Metabase and a scripted model, with invented data only.
