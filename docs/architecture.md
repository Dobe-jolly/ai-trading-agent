# Architecture: what was planned, what is built

This page replaces the model-hosting part of the earlier plan (`ticket_analysis/AI_AGENT_ARCHITECTURE.md`). That
plan put every model call on a GPU server in the bank (Ollama or vLLM, a 7–14B open model) behind a sidecar the Java
engine called, so that no ticket text left the network. In October 2026 the bank approved Claude for use across
the organisation, and the agents call Claude through the Anthropic API instead. Everything else in the plan stands,
above all its governing rule.

## The rule that did not change

**The model proposes, code disposes.** Claude reads the ticket, decides which lookups to run, and proposes a reply,
a request for information or an escalation. Each desk's guardrails (`<desk>/guardrails.py`) decide what is posted,
from what the lookups returned, not from what the model says they returned. A close needs evidence that passes a
rule written in code; anything uncertain becomes an escalation note for a person. The model never writes SQL, never
reaches a database, and never writes to osTicket.

## The plan, item by item

| In the plan | As built |
|---|---|
| Java `InvestigationEngine` is the only writer | The Python service (`python -m nmbz run`) is the only writer, through `agentcore/writer.py`: the Java engine's native-SQL write path through Metabase, verified by reading back. The Java engine is retired desk by desk. |
| LLM sidecar (FastAPI) with `/analyze`, `/draft`, `/similar` | No sidecar. Each desk calls Claude directly, in a loop where Claude calls the desk's lookups as tools (`agentcore/loop.py`). |
| On-prem model (Ollama/vLLM, Qwen 2.5 14B or Llama 3.1 8B) | Claude through the Anthropic API (`AGENT_MODEL`, Sonnet by default). No GPU server. |
| `/analyze` replaces regex extraction | Claude reads the whole ticket: form fields, thread and attachments (screenshots, PDFs, spreadsheets). The regex extraction stays, for the list of details the owner gave and for the fallback rules. |
| The engine runs only allowlisted tools, the LLM picks names and parameters | The same: each desk's `tools.py`. The tools also refuse any account, mobile, ID or reference that isn't on the ticket or from an earlier lookup. |
| `/draft` writes the reply from evidence | Claude drafts. Each desk's standard wording is in code and shown beside the draft in the console. A draft that goes beyond the standard outcome waits for a person (Approvals). |
| Deterministic evidence gate per category | Each desk's guardrails, e.g. a failed transaction closes only on a confirmed transaction whose reading is final and whose reply says what the record says. |
| Close budget per cycle, halt and alert | `MAX_CLOSES_PER_RUN` per desk; the anomaly watch alerts admins by SMS and e-mail. |
| Every LLM interaction logged | Every decision is logged with the full exchange (masked), every lookup and its result, the model, tokens and cost, confidence, and what the guardrails changed. The console shows it. |
| Kill switch `--no-llm` | `MODEL_FALLBACK=rules` (each desk's rules decide what they safely can when Claude is unreachable), a pause per desk and for the whole service (`state/STOP`), and a mode hold (shadow / notes) from the console. |
| PII stays on the network by construction | Numbers are masked before anything reaches Claude (accounts, references, mobiles, national IDs, e-mails), and lookups unmask on the server. With `ATTACHMENT_MODE=model`, images and PDFs go to Claude as they are. The approval makes masking a choice rather than a condition; it is kept because the model doesn't need the numbers and the log stays free of them. |
| Data access through Metabase only, read-only checks | Unchanged for the agents. The one exception is the EcoCash monitor on the integrations host, which writes link attempts to its own PostgreSQL table, as the Java monitor did (or through Metabase, `MONITOR_STORE`). |

## Where things run

```
agent host (RHEL)                                   integrations host
  nmbz-agents.timer ─► python -m nmbz run              ecocashreg.service ─journal─► python -m nmbz monitor
     every desk, in order                                                               └─► ecocash_link_attempts
  nmbz-console ─► one console, port 8085 (behind F5/nginx, TLS)
        │                        │                    │
        ▼                        ▼                    ▼
  Metabase API ──► osTicket, core banking (standby), Postilion, CONECT and its registrations, billers, IB, SMS gateway, EcoCash monitor
  api.anthropic.com (443, via the bank's proxy if there is one)
  notification gateway or SMTP ──► branch e-mails, admin SMS
```

## Roadmap status

| Phase in the plan | Status |
|---|---|
| A. Finish the rule rollout | Superseded: the rules were ported into the desks as guardrails and fallback rules. |
| B. Extraction and classification | Done inside each desk: Claude reads the ticket. The SMS desk also triages tickets filed under other topics. |
| C. Retrieval and drafting | Drafting done. Retrieval over resolved tickets ("the last 12 like this were fixed by ...") is not built yet. |
| D. Evidence-gated auto-close | Built for SMS Alerts, EcoCash linking, failed transactions and NMB Connect registration and login, each starting in shadow, then notes, then live. The router files tickets from catch-all topics under the topic a desk works. |
| E. Continuous adaptation | Partly: the anomaly watch, the audit replay of closed tickets, and review marks in the console. Mining new patterns into proposed rules is not built. |

## Next desks

The router ([docs/router.md](router.md)) and NMB Connect registration and login ([docs/nmb_connect.md](nmb_connect.md))
are built. Next, in the order proposed: internet banking, and the HelpDesk topics. Each is a package beside the
others (see "Adding a desk" in the [README](../README.md#adding-a-desk)); once a desk works a topic, the router moves
tickets to it on its own.
