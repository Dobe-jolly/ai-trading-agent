# NMBZ helpdesk agents

One service that works osTicket tickets for the ICT service desk. Each kind of ticket has a **desk**: a set of
read-only lookups into the bank's systems (through Metabase), rules in code that the model cannot override,
and standard wording. Claude reads each ticket, investigates with the desk's lookups, and proposes a reply,
a request for information or an escalation; the desk's guardrails decide what actually happens.

| Desk | Tickets | What it checks | Details |
|---|---|---|---|
| `sms_alerts` | SMS Alerts (and SMS issues filed under other topics) | Registration in core banking, the CAE request, dispatch and the SMS gateway | [docs/sms_alerts.md](docs/sms_alerts.md) |
| `ecocash` | EcoCash linking | Every link and delink attempt (recorded by the EcoCash monitor), EcoCash's answer, the Postilion link record | [docs/ecocash.md](docs/ecocash.md) |
| `failed_txn` | Failed ZIPIT, EcoCash B2W/W2B, NMB Connect, airtime, ZESA and Postilion transactions | The transaction in Postilion, NMB Connect and the billers, or internet banking; then the debit and any refund in core banking | [docs/failed_txn.md](docs/failed_txn.md) |

Every desk runs in the same pass, writes to osTicket the same way, and shows in the same console.

```
every 15 min ─► python -m nmbz run ─► SMS Alerts ─► EcoCash ─► Failed transactions ─► …   (DESKS, in order)
                                        │
            each desk: its open tickets ─► skip rules ─► Claude + the desk's lookups (masked)
                                        ─► guardrails ─► approval for off-script replies ─► osTicket ─► e-mail to the branch
                                        ─► its own decision log (state/<desk>/agent.sqlite)

python -m nmbz portal ─► one console: a tab per desk, approvals, anomalies, controls, pause every desk
```

## Layout

| Path | What it is |
|---|---|
| `agentcore/` | What every desk shares: Metabase client, masking, osTicket reads and writes, the reasoning loop, the pass (`runner.py`), guardrail basics, approvals, attachments, e-mail outbox, anomaly watch, settings reader, the console API and page |
| `sms_alerts/` | The SMS Alerts desk: its checks, lookups, triage of other topics, prompts, wording |
| `ecocash/` | The EcoCash desk, and the monitor that replaces the Java `ecocash-registration-monitor` |
| `failed_txn/` | The failed-transactions desk: Postilion, NMB Connect and billers, internet banking and core-banking lookups |
| `nmbz/` | The service: the command line (`python -m nmbz`), the desk list, the console server |
| `tests/` | `core/` and one folder per desk (invented data only) |
| `tools/build_preview.py` | Builds `preview.html`: the console with every desk's demo data (`tools/demo_<desk>.py`) |
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

Prefixes: `SMS_ALERTS_`, `ECOCASH_`, `FAILED_TXN_`. Each desk keeps its own decision log, pause file and mode hold under
`state/<desk>/`; console users (`state/portal_users.json`) are shared. `DESKS` picks which desks a pass visits
and in what order. `python -m nmbz desks` shows each desk's mode and log.

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
```

`python -m nmbz <command> --help` shows a command's options. Exit codes: 0 OK, 1 config error, 2 Metabase
unreachable, 3 some tickets errored (retried next pass; the unit treats it as success). A pass of every desk
returns the worst code; one desk failing does not stop the others.

## Console

`python -m nmbz portal` serves one console for every desk on `127.0.0.1:8085`. Publish it through the F5 or
nginx with TLS (sign-in is HTTP Basic). The desk tabs show replies waiting for approval, an open incident and
a pause on each desk. For each desk:

- **Overview, Decisions, Review:** what the agent decided and why, with the full reasoning and every lookup.
- **Approvals:** off-script replies wait here for a person (<kbd>J</kbd>/<kbd>K</kbd>, <kbd>A</kbd>pprove,
  <kbd>E</kbd>dit, <kbd>X</kbd> reject; <kbd>Ctrl</kbd>+<kbd>K</kbd> jumps to any ticket).
- **Anomalies:** the hourly watch on the desk's own signals.
- **Open tickets, Runs:** the last pass, and a live view of the pass in progress.
- **Controls:** pause the desk, hold it at notes or shadow; admins can also pause every desk.

`GET /healthz` needs no sign-in: 200 when every desk has finished a pass within `PORTAL_STALE_MINUTES` (or
the service is paused on purpose), 503 otherwise.

`python tools/build_preview.py` writes `preview.html`, the console with invented data for every desk.

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
