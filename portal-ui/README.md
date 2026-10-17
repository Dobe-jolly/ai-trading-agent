# NMBZ ICT Helpdesk — Agent Supervision Portal

The supervision portal the console serves at `/` (`python -m nmbz portal`): a desktop-first workspace for ICT
service-desk staff to supervise the five desks (Router, SMS Alerts, EcoCash, Failed transactions, NMB Connect),
understand their decisions and act on them. It reads and writes the console's own API (`nmbz/console.py`,
`agentcore/portal/server.py`); there is no other backend.

## Build and run

```bash
npm install
npm run build        # type-checks, then writes ../agentcore/portal/web/ (served by the console; commit it)
npm run dev          # http://localhost:5173, API calls proxied to the console on :8085
npm run typecheck
```

Requires Node 20.19+ (Vite 8). Fonts are bundled with `@fontsource`, so nothing loads from outside at runtime,
which keeps the console's `Content-Security-Policy: default-src 'self'` intact.

For development, run the real console over invented data from the repository root:

```bash
python tools/demo_console.py     # dobe (admin) / viewer (viewer), password demo-password-123
```

`PORTAL_CONSOLE=http://host:port npm run dev` points the dev server at another console.

## How it talks to the console

- `src/lib/api/client.ts`: `fetch` with the browser's Basic sign-in. Every POST sends `X-Portal-Request: 1`
  (the console's CSRF check, with its Origin check).
- `src/lib/api/hooks.ts`: TanStack Query. Reads refresh every 30 s; each change (approve, review, hold, pause,
  re-send) refetches the service and that desk.
- `src/lib/api/types.ts`: the response shapes, as `agentcore/portal/data.py` and `nmbz/console.py` return them.
- `src/lib/portal.tsx`: `/api/service` (signed-in user and role, desks, holds, health, counts) and the desk filter.

The role comes from the sign-in. A viewer reads everything and can review; an admin can also approve, edit and
reject replies, pause and hold desks, and re-send e-mails. The server enforces it; the portal only hides what a
viewer can't do.

## Routes

| Path | Screen | API |
| --- | --- | --- |
| `/` | Overview: last 24 h, 7-day outcomes, needs attention, desk health | `overview`, `anomalies`, `approvals`, service |
| `/approvals` | Held replies, oldest first; decided recently | `approvals` |
| `/tickets` | Open tickets as of each desk's last pass | `queue` |
| `/tickets/$ticketNumber` | Ticket investigation and the approve / review step (`?desk=&decision=` picks one decision, else the newest) | `decisions/<id>`, `approvals/<id>`, `decisions/<id>/reviews` |
| `/decisions` | Decision log, filters, CSV export | `decisions`, `export/decisions.csv` |
| `/review` | Accuracy by desk, disagreements, decisions to review | `quality`, `decisions/<id>/reviews` |
| `/anomalies` | Desks with an anomaly watch (SMS Alerts, EcoCash) | `anomalies` |
| `/emails` | E-mails and alerts sent outside osTicket, admin re-send | `notifications`, `notifications/retry` |
| `/runs` | Each pass, the one running now, stale desks | `runs`, `live` |
| `/controls` | Pause / resume, notes and shadow holds, pause every desk, the control log | `controls`, `/api/service/controls` |

## Where things are

- `src/lib/status.ts`: the status vocabulary. Every pill comes from here: intent (proposed, approved) is outlined
  or dashed, facts (closed, posted) are filled, so Approved / Posted, No matching record / Lookup failed, Paused /
  Stale and Awaiting branch / Awaiting approval never look alike.
- `src/lib/vocab.ts`: how a decision, an approval and a queue entry map onto that vocabulary.
- `src/lib/transcript.ts`: the ticket header and the lookups (what was sent, what came back) from a transcript.
- `src/lib/timeline.ts`: the outcome timeline, so "approved, posts on the next pass" is never shown as posted.
- `src/components/shell/go-to.tsx`: Ctrl+K and the keyboard shortcuts, as on the classic page.
