# Application logs: what no database records

Some failures never reach a database: an exception in a menu handler, a connection pool that lost its database, a
downstream that refused the connection, a response with a field missing. Staff find them by reading logs. The agents
get the same view through two tools, `log_errors` and `log_search`, over a store that a **pipeline** fills. The agent
never reads a log file and no model reads log lines in bulk.

```
service log (journald / file)
  └─ profile (per service: what it is, its correlation ids, known noise)
      └─ parse     one record per log line; the stack trace or dump that follows is attached to it
          └─ mask  names, PINs, tokens, secrets removed; accounts, mobiles and long numbers become keyed hashes
              └─ signatures   the same problem counted per minute, whatever its numbers and ids
                  └─ store (state/logs.db)  ──►  log_errors  (what is going wrong)
                                              └►  log_search  (the lines about this client's attempt)
```

## Why a pipeline and not a second agent

The first six sample logs held 1.1 million lines; about 1,300 of them (0.1 %) were errors or warnings. Reading logs
with a model would spend almost all its effort on `INFO` chatter, cost the most where nothing is wrong, and put a
client's identifiers in front of the model for no reason. Everything that is mechanical is code: parsing, masking,
grouping, counting, spotting a spike. A model is useful at the edges and is used there:

- **writing a new service's profile** from a sample (not built yet: today a profile is a short JSON file, most
  of them a few lines, written by hand);
- **explaining a new signature once** (a signature is new a few times a day, not a million times): staff or a model
  draft what it means, a person approves it, and it is stored with the signature (`python -m agentcore.logs note`).
  From then on every desk that meets it sees the cause (`known_cause`);
- **the desk agent** then uses the tools like any other lookup, with the same ticket-ownership rules.

## What is stored

| Table | Holds |
|---|---|
| `signatures` | One row per distinct WARN/ERROR: service, logger, the message with its variable parts replaced, exception class, first application stack frame, first/last seen, count, one masked example, `noise` flag |
| `sig_notes` | What staff say a signature means (kept when logs are re-ingested) |
| `sig_buckets` | Count of each signature per minute (what makes "new" and "spiking" answerable) |
| `volume` | Lines, warnings and errors per service per minute |
| `events` | Masked lines: every WARN/ERROR, and every other line that carries an identifier or a correlation id |
| `event_keys` | Identifier hashes and correlation ids, by event (how `log_search` finds lines) |

Nothing unmasked is written. Ingestion reads the raw log once; it is not kept.

## Identifiers: keyed hashes

An account, mobile, ID or other long number becomes `<NUMBER:abcdefghijkl>` (letters only), a keyed hash (HMAC-SHA256)
of its canonical form: an account with or without leading zeros is one account; a mobile in any of its forms is one
mobile. A ticket's account is hashed the same way to search, so **no account number is stored, shown or logged**, and
without `LOG_HASH_KEY` a hash cannot be recomputed or reversed. Keep the key out of git and out of the store (`.env` is
ignored); changing it makes earlier hashes unsearchable, so re-ingest after a rotation. Names, addresses, PINs, OTPs,
tokens, JWTs and other personal values in `key=value`, JSON or XML form are replaced outright. Names inside free text
cannot be found by pattern and are not masked, as elsewhere in the agents.

## The tools

**`log_errors`**: service-wide, no client data. `mode=top` (most frequent in the window), `spikes` (a signature far
above its own usual level, in five-minute bins, with *clusters* when several services spike within ten minutes of each
other: a possible shared cause) or `new` (first seen in the window; relative to what has been ingested). Each
signature shows its count, first and last time, the exception and application frame, a masked example, and `known_cause`
when staff have written one.

**`log_search`**: the lines about one client's attempt, found by an identifier the ticket gives (account, mobile or a
reference with long digits; checked as for the other lookups, another customer's number is refused) or by a correlation
id (requestId, sessionId, uuid, ...) that this ticket or an earlier log result showed. It returns the matching lines and
the **other lines of the same requests** (ten minutes either side), in time order, so the trail through the services is
visible. Only an id that identifies one request joins lines into a trail: `requestId`, `correlationId`, `transactionId`,
`rrn` do; `sessionId` and `uuid` do not; and any id found on more than about 30 lines is skipped whatever it is called
(in the sample, a `requestId` in the log prefix turned out to be a caller's id reused for a whole USSD session, while the
payment's own `requestId=MB...` in the message was one request: five lines). A skipped id still finds lines when it is
searched for directly. The service and level filters apply to that whole trail, so an `ERROR` on a line that does not itself mention
the client is still found. A CIF is not logged as such and is not searchable.

Both record a query id (Q1, ...) that evidence cites. `log_errors` is recorded as counts, so it can inform a reply but
never carries a close; a close on `log_search` follows the same `QUERY_CLOSE` rules as `query_data`.

## What the first six logs showed

Ingested through the pipeline (1.1 million lines from the integrations server; 164 MB store; no raw number, email or
token in it, checked by scan): the 1,058 warnings of the Intellect integration service were five business reasons
(insufficient funds 677, duplicate transaction 142, debit balances not allowed 92, dormant account 74, debits frozen
31: customer-side declines, not faults); the USSD service lost its PostgreSQL connections for a few minutes around
15:12 (Hikari pool "not available", `CannotCreateTransactionException` in `MenuServiceImpl.findSubMenuHeader`, found as
a spike with no line read); the ZIPIT bridge logged one identical error 922 times, which buries its real ones (29
ZIPIT validations failing for a missing name in Postilion's reply). `log_errors` reproduces each of these in one call.
Signatures matter: grouping by the message with its ids normalised by field name, and the failure reason kept, took
1,073 warnings to 22 signatures (a first version that left the id fields as they were produced 750).

## Adding a service

1. Ship its log (journald output or the application's file) to wherever ingestion runs.
2. Add `agentcore/logs/profiles/<service>.json`: `service`, `about` (one line: what the service is, shown to the agent),
   `format` (`journald` or `plain`), optional `correlation` regexes (one capture group) beyond the defaults,
   `noise` regexes for known harmless repeats, `clock_offset` seconds if its clock differs from the others, `owner`.
   A service with no profile is still ingested with the generic defaults.
3. `python -m agentcore.logs ingest --service <service> <file>` (replaces that service's data; `--append` adds).

Most services share one format (journald lines carrying Spring Boot logback lines), so one parser covers them; a service
with another format needs a parser added once, not per service.

## Settings

`LOG_DB` (default `state/logs.db`), `LOG_HASH_KEY` (16+ characters), `QUERY_LOGS=false` to switch the tools off. With no
key or no store the desk simply runs without them. Currently on the failed-transactions desk.

## What the first slice does not do yet

- **Live ingestion.** It ingests files you give it. For over a hundred services it should read a shipped stream
  (Vector, Filebeat or journald forwarding into a collector) continuously, keep a retention window, and run on a schedule.
- **A larger store.** SQLite holds the first samples comfortably; a hundred services for days belongs in a log platform
  (Loki, OpenSearch) or PostgreSQL, behind the same queries. The tables above are what that store keeps.
- **Clock alignment.** Services report their own clocks; a measured `clock_offset` per service is needed to line them up
  (the ASI servers differed by about 99 seconds). The profile field exists; measuring it is manual.
- **Alerting.** Spikes and new signatures are found when asked. A scheduled job that writes an incident note when
  several services spike together would make them found without anyone asking.
- **Other desks.** Only failed transactions has the tools so far.
