# Router

Part of the NMBZ helpdesk agents service. Installing, settings, commands and the console are in the
[README](../README.md); this page covers what the router does. Its settings take the prefix `ROUTER_` when they
should differ from the other desks (`ROUTER_AGENT_MODE=live`).

Many tickets arrive under a catch-all topic or with no help topic at all, although a more specific topic exists.
HelpDesk / Logging Ticket alone takes about 145 tickets a year, and e-mailed tickets often carry no topic. Filed
there, a ticket waits in a general queue, and an agent that could resolve it never sees it. The router reads those
tickets, works out where each one belongs, and:

- **Moves it** to that topic when an agent works the topic (SMS Alerts, EcoCash, the failed-transaction topics). The
  router runs first in each pass, so the desk picks the ticket up in the same pass.
- **Suggests the topic to staff** in an internal note when no agent works it, when two topics fit, or when a person
  is assigned to the ticket.
- **Asks the branch** to describe the problem when the ticket doesn't say what it is ("Please assist").
- **Leaves it where it is** when it is a general request with no better topic.

It never replies with a fix or closes a ticket: the team or agent that owns the topic does that.

```
osTicket ── every 15 min: open tickets under ROUTER_TOPICS, or with no help topic, opened in the last ROUTER_DAYS
   │
   ▼
Claude reads the masked ticket + attachments, with the active help topics (and what each is for) in its prompt
   ├─ find_similar_closed_tickets: where the service desk filed tickets with the same words
   └─ get_topic_examples: what is really filed under a topic
   │
   ▼ submit_decision: move / suggest / ask / leave ─► guardrails (code) ─► help topic changed + note, or note, or reply
then, in the same pass: SMS Alerts ─► EcoCash ─► Failed transactions pick up what was moved to them
```

## Tickets

**Which tickets.** Open tickets under `ROUTER_TOPICS` (subtopics included; default HelpDesk / Logging Ticket) and,
with `ROUTER_INCLUDE_NO_TOPIC=true`, tickets with no help topic, opened in the last `ROUTER_DAYS` (30). Topics in
`ROUTER_EXCLUDE_TOPICS` are never read.

**Left alone without a model call:** a ticket a person has replied to or left a note on (someone is handling it),
one the router moved before and a person filed back, one already noted by the router, and one waiting for the
branch's answer.

**What Claude sees.** The subject, every form field, the thread and what each attachment shows, masked as on the
other desks. Its prompt lists the active help topics, most used first, each with what it is for, how many tickets
it had in the last 90 days, and whether an agent works it. "What it is for" comes from
`router/topic_descriptions.json` (edit it; drafted from the closed-ticket analysis) or else from the notes kept on
the topic in osTicket.

## The lookups

| Tool | Reads | Returns |
|---|---|---|
| `find_similar_closed_tickets` | Closed tickets of the last `ROUTER_EXAMPLES_DAYS` (365) whose subject or first message has every word given | How many were filed under each topic, and a few subjects |
| `get_topic_examples` | Recent tickets under one topic | Their subjects and the start of the first message |

Both read osTicket only. They take plain words, never numbers, so no account, mobile or reference can be searched,
and their results are masked like the ticket.

## Rules the code enforces (`router/guardrails.py`)

| Rule | Effect |
|---|---|
| The ticket closed, was filed elsewhere or got a new entry while Claude worked | Nothing; looked at again next pass |
| The topic isn't an active help topic | Suggested to staff instead |
| The topic is the one the ticket is already under | Left where it is |
| The topic is a catch-all or in `ROUTER_NEVER_TO` (Escalated to Vendor) | Suggested to staff |
| Confidence below `CONFIDENCE_THRESHOLD` | Suggested to staff |
| The router moved this ticket before | Suggested to staff; never moved twice |
| A person is assigned to the ticket, or has replied or left a note | Suggested to staff |
| No agent works the topic, and it isn't in `ROUTER_MOVE_TO` | Suggested to staff (by design, not counted as a safety stop) |
| More than `ROUTER_MAX_MOVES_PER_RUN` moves in a pass | The rest wait for the next pass |
| A request for information that breaks the reply rules (numbers it was never shown, links, PIN/OTP talk) | Suggested to staff |
| The agent has already asked `MAX_INFO_REQUESTS` times | Suggested to staff |

**What a move writes.** The ticket's help topic changes (only if it is still under the topic it was read under,
then read back to check), and an internal note says where it came from, where it went, and why: "If this is
wrong, file it back or under the right topic; the routing agent will not move it again." The department and the
assignee are not changed.

**Approvals.** A request for information waits in the console when the ticket already describes the problem (the
standard outcome is then a move), or when Claude asked for something other than a description of the problem.

**Without Claude** (`MODEL_FALLBACK=rules`), a ticket whose owner wrote nothing about the problem is asked to
describe it; everything else waits for the next pass. The router never moves a ticket without the model.

## Commands

```bash
python -m nmbz topics                                 # every active topic: tickets in 90 days, moved on its own or not
python -m nmbz check --desk router                    # settings, the catch-all found, tickets to route
python -m nmbz run --desk router --ticket 482913      # one ticket
python -m nmbz audit --desk router --since 2026-09-01 # replay closed catch-all tickets
```

## Before going live

1. `python -m nmbz topics`: check the topic list, and that the topics the desks work show "yes".
2. Read `router/topic_descriptions.json` and correct what each topic is for. This is what Claude decides from.
3. Run the router in shadow (`ROUTER_AGENT_MODE=shadow`) for a week next to the service desk. In the console, open
   the Router desk and mark its decisions right or wrong on the Review page; `python -m nmbz report --desk router`
   exports them with the topic it chose.
4. Then notes mode (it suggests every move in a note, changes nothing), then live.

Topics that only staff work can be moved to on their own later by listing them in `ROUTER_MOVE_TO`, once the shadow
week shows the router gets them right.
