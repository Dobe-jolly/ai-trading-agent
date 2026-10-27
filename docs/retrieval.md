# Retrieval over resolved tickets (proposed)

Status: design, not built. This is Phase C's open item in [architecture.md](architecture.md): "the last 12 like this
were fixed by ...". It gives each desk past cases to read beside the ticket, the way a new officer looks back at how
the same complaint was handled before.

## The rule it must keep

**A past case is a hint, never evidence.** It can point the model at the right lookup or the right reading of a
code. It can't close a ticket, fill a reply, or count toward a guardrail. Guardrails still decide from what this
ticket's lookups returned. If a past case and today's lookups disagree, the lookups win and the model says so in its
reason, the same as with a staff note (`agentcore/knowledge.py`, `PREAMBLE`).

## What desks have today

| Source | What the model gets | Masking | Who vouches for it |
|---|---|---|---|
| Desk prompt (`<desk>/prompts/system_prompt.md`) | Fixed instructions | None needed | Whoever writes the code |
| Staff notes (`agentcore/knowledge.py`) | Notes in the system prompt, cached; their lookups as `k<id>_...` tools | Masked when saved | Maker-checker: a second Admin |
| Router's `find_similar_closed_tickets` (`router/tools.py`) | Closed tickets with the given words: subject and topic only | `pseudo.mask` with the ticket's own pseudonymiser | No one: it counts filing, not fixes |

Nothing gives a desk *how a ticket like this was resolved*. The decisions log (`agentcore/audit.py`) already has
what's needed for that from our own work: masked reason, evidence, reply, final action, `reopened_at`, and review
verdicts in `reviews`. osTicket has the rest: tickets people closed by hand.

## Design

### 1. A case store, built offline

A new `cases` table plus an FTS5 index, in the same SQLite file as the audit log. FTS5 is in the bundled SQLite
(3.45.1, checked). A nightly command, `python -m nmbz cases build --desk <desk>`, fills it from two sources:

| Source | Taken when | Text kept |
|---|---|---|
| Agent decisions | Final action closed or replied, posted (`executed`), no `reopened_at` after `CASES_SETTLE_DAYS` (14), and no review marking it wrong | Category, the model's reason, the lookups it ran (names only), the posted reply |
| Tickets people resolved | Closed under one of the desk's topics in the last `CASES_DAYS` (365), not reopened, with a staff reply | Subject, owner's first message (cut to 600 characters), the last staff reply before closing (cut to 600) |

Each row has `id` (shown as `C123`), `desk`, `topic`, `source`, `closed_on`, `action`, `category`, `asked`, `found`,
`resolution`, `status` (`active` / `suppressed`) and `source_ticket` (the ticket number, kept for the console and
never sent to the model). A ticket that's later reopened, or a decision whose latest review is `incorrect`, is marked
`suppressed` on the next build. A case an Admin suppressed stays suppressed. When the agent escalated a ticket and a
person then resolved it, the staff case takes the agent's masked reason as `found`. That's often the best case there
is: what the lookups showed, and what a person did about it.

Built: `agentcore/cases/store.py` and `agentcore/cases/build.py`, run from the desk's CLI:

```
python -m nmbz --desk failed_txn cases build                 # nightly; --full re-reads every thread
python -m nmbz --desk failed_txn cases search --words zipit --code 91
python -m nmbz --desk failed_txn cases cutoff --since 2026-09-02 --note "EcoCash API v3" --by admin1 --kind channel --value ecocash
python -m nmbz --desk failed_txn cases suppress --id C12 --by admin1 --reason "a third party's name got through"
python -m nmbz --desk failed_txn cases stats
```

A new ticket's thread is read once. Later builds only re-check reopens and reviews, so a nightly build costs one
query for the closed-ticket list, one per 200 tickets for reopens, and one per new ticket.

### 1a. Tags and recency

The same symptom ("money left, not received") can have several causes, and a cause can change after an upgrade. So a
case is matched on exact facts first and on words second. Each case carries tags in `case_tags (case_id, kind,
value)`:

| Kind | From an agent decision | From a ticket staff resolved |
|---|---|---|
| `signature` | Log signatures in `log_search` results in the masked transcript: the client's own trail, not `log_errors`' service-wide lists | None |
| `code` | The confirmed transaction's response code (`facts.confirmed.code`) | Codes written as "response code 91", "rsp 05" or "error code E102" in the masked text |
| `channel` | `facts.confirmed.channel` (failed transactions), `facts.latest.via` (EcoCash) | None |
| `reading` | `facts.confirmed.status` (`reversed`, `failed_refunded`, ...), `facts.latest.status` (EcoCash) | None |
| `action` | The final action and the category | `staff_resolved` |

Search ranks each candidate by weighted tag matches (signature 3, code 2, channel 1, reading 1), plus the full-text
rank over `asked`, `found` and `resolution`. The total is multiplied by a recency factor that halves every
`CASES_HALF_LIFE_DAYS` (90). So a case from last month that has the same signature beats a better-worded case from
last year. Each result says what it matched on (`"matched": ["signature", "code"]`), so the model and the console can
see why it was offered.

Upgrades are handled with **cutoffs**, not guesswork. An Admin records "EcoCash API v3 went live on 2026-09-02" as a
row in `case_cutoffs (desk, kind, value, since, note, by, at)`. Cases closed before `since` are left out, either for
the whole desk or only for cases carrying that tag (`channel = ZIPIT`, `code = 91`). Cases older than `CASES_DAYS` are
always left out.

### 2. Masking: one-way, and not on the ticket's pseudonymiser

The case text is masked **when it's stored**, with placeholders that don't match `TOKEN_RE`:
`[NUMBER]` (accounts, cards, references), `[MOBILE]`, `[NATIONAL_ID]`, `[EMAIL]`, `[CUSTOMER]`, `[STAFF]`. Raw text is
never written to the store. Tokens already in the decisions log (`<NUMBER_1>`) become the same placeholders. Built:
`agentcore/cases/mask.py`, tested in `tests/core/test_case_mask.py`.

- **Why not `<NUMBER_n>` tokens.** Each desk's tools refuse any identifier that isn't on this ticket or from an
  earlier lookup (`Pseudonymizer.unknown_tokens`). If case text were masked with the ticket's own `Pseudonymizer`,
  a past customer's account would come back as a valid token, and the model could look that customer up. One-way
  placeholders have nothing to unmask, so the provenance rule holds on its own.
- **Names.** `pii.py` doesn't mask names. For cases we can do better than a regex: the builder knows the ticket
  owner's name (`ost_user`) and the staff names on the thread. It replaces those exactly, then drops greeting and
  signature lines. A name it can't recognise may still get through. That's the same exposure as today's ticket
  text, but it now crosses tickets. So the console's case view is reviewed for names during shadow, and an Admin can
  suppress any case (section 5). Tests prove the exact replacement on synthetic tickets.
- **What the first real build found** (failed transactions, 108 cases from a year of tickets; scanned locally, counts
  only, none of the text read into an AI tool). One card number written in groups of four, and three national IDs
  written without the check letter or straight after `ID-`, got through. Both were gaps in `agentcore/pii.py`, so
  live tickets were affected too, not just cases. The fixes are in `pii.py` and so cover every desk: grouped numbers
  (`4000 1234 5678 9010`, `0012-63-1234567`) and those IDs are masked now, while thousands-format amounts and dates
  are left alone (`tests/core/test_pii.py`). The same scan also found client names after "Client", "Mr", "account
  holder", and so on. The case masker now masks the capitalised words after such a cue, up to the first ordinary
  word, and then masks that name wherever else it appears in the case. After a `--full` rebuild the counts were:
  0 digit runs, 0 tokens or patterns, 2 cue words and 21 capitalised word pairs left for a person to read.
- **Open: names still get through.** Reading those flagged cases found client names that none of these rules catch.
  Until the masker covers those patterns and a rescan comes back clean, no desk should run with `CASES=on`, and the
  case store shouldn't leave the agent host.
- **Router follow-up.** `router/tools.py` masks similar-ticket subjects with the ticket's pseudonymiser. It's
  harmless there because the router has no customer lookups. It should switch to the one-way masker so the pattern
  isn't copied into a desk.

### 3. How the model gets cases: a tool, not the prompt

Each desk that opts in gets one more lookup, `find_past_cases`, added the way `GuidedToolbox` adds note lookups:

```
find_past_cases(words: 0-4 plain words, signature?: from a log result, code?: a response code,
                channel?: ..., reading?: ...)  ->  up to 5 cases from this desk's topics, ranked as in 1a:
  [{"case": "C123", "source": "agent" | "staff", "closed": "2026-08-14", "topic": "...", "matched": ["signature"],
    "what_was_asked": "...", "what_was_found": "...", "how_it_was_resolved": "...", "outcome": "not reopened"}]
```

- Words are letters only, validated like `router.Toolbox.words`. Tag values are short codes (`CODE_RE`, no 9+ digit
  runs, no tokens). Neither can hold a customer identifier.
- The usual order is: run the lookups, then ask with what they showed ("signature 3f9a…, code 91, ZIPIT"). The
  tool comes after the investigation has started, not instead of it.
- Results go inside `<past_cases>` fences (a fence written inside a case is defused). `DATA_RULE` says past cases
  are other clients' tickets: examples, never evidence about this one. Nothing is closed or stated in a reply because
  a case says so, placeholders are never copied into a reply, and a case relied on is cited by its id.
- A ticket never finds its own case. In an `audit` replay, the lookup offers only cases closed before the ticket was
  opened, and ranks them from that day. So a replay sees what the agent would have had then, not the answer.
- Results aren't re-masked with the ticket's pseudonymiser (that would only issue tokens). The tests check that a
  call issues none.
- Built: `agentcore/cases/tools.py`. It's on when the desk has `CASES=on` (`FAILED_TXN_CASES=on` for one desk; off by
  default) and is loaded each pass, after the notes' lookups (`DeskRunner.load_cases`). `CASES_DAYS` and
  `CASES_HALF_LIFE_DAYS` set the window and the recency halving.
- A tool rather than auto-injection, because it keeps the cached prefix (system + tools) unchanged, costs nothing on
  tickets that don't need it, and shows in `tools_used` like any lookup. If shadow runs show the model rarely
  calls it on tickets where it would help, try putting the top three in the first message instead.

### 4. Guardrails: what changes

Little changes, by construction.

- **Evidence.** `find_past_cases` writes nothing into the desk's `Evidence` fields that guardrails read (accounts
  confirmed, transaction readings, and so on). It appends only its own name to `tools_used`. Guardrails look for
  their own lookups there by name (EcoCash: `find_ecocash_attempts`), so this can't stand in for one. A close
  whose only support is a `C<id>` therefore fails the existing evidence gate, as a close with no lookups does now.
  `tests/core/test_case_guard.py` proves it on each desk. A confident close with a clean reply, citing only C1, is
  refused by the desk's own gate:

  | Desk | Refused with |
  |---|---|
  | Failed transactions | close blocked: no transaction or EcoCash link was confirmed |
  | EcoCash | close blocked: the client's link attempts were not looked up |
  | SMS alerts | close blocked: account(s) … not checked |
  | NMB Connect | close blocked: the client's issue was not assessed |
  | Router | can't close at all (`reply_and_close` isn't a router action) |

- **Placeholders block.** A reply containing `[NUMBER]`, `[MOBILE]`, `[NATIONAL_ID]`, `[EMAIL]`, `[CUSTOMER]` or
  `[STAFF]` can't be posted as written, so `reply_problems` blocks it (escalates) on every desk, router replies
  included. Built: `agentcore/cases/guard.py`, called from `agentcore/guardrails.py`.
- **Copies wait.** A reply that repeats 25 words in a row of a case the agent was shown waits for a person in
  Approvals, whatever `APPROVAL_MODE` says. Shadow and notes record "would wait". Words are compared letters only,
  so changing a number or a masked value doesn't hide a copy. Runs that are also in the desk's standard reply for
  this ticket don't count, because agent cases often carry the standard wording. Built:
  `DeskRunner.case_copy_reason`, ahead of the desk's approval rule.
- **Budget.** `MAX_CLOSES_PER_RUN`, modes, pauses and `MODEL_FALLBACK=rules` don't change. Under the rules
  fallback, cases aren't used.

### 5. Trace and control

- `decisions.cases_seen` and `decisions.cases_used`: the cases `find_past_cases` showed, and the ones the decision
  cited as `C<id>` in its reason or evidence. A citation counts only if that case was actually shown. They're
  recorded alongside `knowledge_used` (`DeskRunner.trace_fields`). Built; the console doesn't show them yet.
- The console decision view lists the cases with a link to each source ticket. An Admin can **suppress** a case;
  the next decision won't see it, and the suppression is a `control_events` row.
- A per-desk `<DESK>_CASES=off|shadow|on`. In `shadow`, the desk decides without cases, then a second model call
  decides the same ticket with them, and both are logged. Nothing is posted from the second call. This doubles cost
  on that desk for the shadow period only.

### 6. Proving it helps before it's on

1. `python -m nmbz audit --desk <desk> --since ...` already replays closed tickets. Run it twice, with cases off
   and on, and compare agreement with what was posted, the escalation rate, and turns and tokens per ticket.
2. Shadow on one desk (failed transactions has the most volume and the most varied causes) for two weeks. Look at
   the Review page's close precision and reopen rate for decisions that cited a case.
3. Turn it on for that desk if escalations drop with no change in reopen rate. Then do the other desks.

## Not in this step

- **Embeddings or a vector database.** BM25 over masked text is enough to test the idea, and keeps everything on
  the agent host. Semantic search would need either a local embedding model or a third-party embedding API (a new
  processor for bank data). Revisit if keyword recall is the measured gap.
- **Attachments.** Screenshots and PDFs on past tickets aren't indexed.
- **Turning cases into notes** (Phase E). Clusters of cases resolved the same way are the natural input for proposed
  staff notes, but they'd still go through maker-checker. That comes after this works.

## Build order

| Step | Where | Done when |
|---|---|---|
| One-way case masker with names from osTicket (**built**) | `agentcore/cases/mask.py` | Tests on synthetic tickets: no placeholder matches `TOKEN_RE`, known names gone |
| Case store with tags, cutoffs and ranked search; builder from both sources (**built**) | `agentcore/cases/store.py`, `agentcore/cases/build.py`, `cli.py` `cases` | Rebuild is idempotent; reopened tickets and cases reviewed incorrect drop out; tag matches outrank words; cutoffs hold |
| `find_past_cases` tool, `DATA_RULE` line, `cases_seen` / `cases_used` columns, `CASES` setting (**built**) | `agentcore/cases/tools.py`, `loop.py`, `runner.py`, `audit.py`, each desk's `config.py` | Shows in the decision log; no identifier gets in, no token comes out; replays can't see the answer |
| Console: cases on the decision view, suppress button, cutoffs | `agentcore/portal`, `portal-ui` | An Admin can see and suppress a case without the CLI |
| Guardrail checks: evidence untouched, placeholders block, copies wait (**built**) | `agentcore/cases/guard.py`, `agentcore/guardrails.py`, `agentcore/runner.py` | A case-only close is refused on every desk |
| Shadow mode and audit replay comparison | `runner.py`, `cli.py` | Side-by-side report for one desk |
| Router switches to the one-way masker | `router/tools.py` | No `<NUMBER_n>` from other tickets in router transcripts |
