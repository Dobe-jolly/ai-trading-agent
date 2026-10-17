# Failed-transactions desk

Part of the NMBZ helpdesk agents service. Installing, settings, commands and the console are in the
[README](../README.md); this page covers what the failed-transactions desk does. Its settings take the prefix
`FAILED_TXN_` when they should differ from the other desks (`FAILED_TXN_AGENT_MODE=live`).

Branches raise a ticket when a client says a transfer or payment failed: a ZIPIT that never arrived, an EcoCash
bank-to-wallet that took the money, airtime or a ZESA token that never came, an NMB Connect transfer stuck. The
desk finds the transaction in the system it went through, reads what that system says happened, checks core
banking for the debit and any refund, and then:

- **The money did not move, or came back:** replies to the branch with what happened and closes.
- **The money moved:** tells the branch it went through, with the reference for the recipient's bank.
- **The client may be out of pocket** (a reversal still pending, a reversal that failed, a timeout with no final
  answer, a debit without a refund): escalates to ICT with a note that names the transaction, the reference and
  what Operations should do.
- **Nothing to go on, or nothing found:** asks the branch for the account, date, amount and reference.

Like the other desks it only reads, through Metabase, and never texts clients. Everything happens on the ticket,
plus an e-mail to the person who raised it.

```
osTicket (OSTICKETS_NEW) ── every 15 min: open failed-transaction tickets
        │
        ▼
Claude reads the masked ticket + attachments ──► lookups (read-only, Metabase)
        │                                          ├─ Postilion (POST OFFICE): ZIPIT, B2W/W2B, card, ATM
        │                                          ├─ NMB Connect (CONECT 2.0) + billers (BILLERS): RTGS, IFT,
        │                                          │    ZIPIT, airtime, ZESA, City of Harare, ZIMRA, offline billers
        │                                          ├─ Internet banking (IB): RTGS and internal transfers
        │                                          └─ Core banking (STANDBY DB): the debit and any refund
        ▼
confirm_transaction ─► where it leaves the client ─► guardrails (code) ─► reply / close / note ─► branch e-mail
```

## Tickets

**Which tickets.** Every open ticket under `FAILED_TXN_HELP_TOPICS`, subtopics included. The default is the
topics these tickets are filed under today, flat and nested:

```
Failed ZIPIT Transaction, Ecocash / Failed Transactions, FAILED ECOCASH B2W / WB2, NMB Connect / Failed Transaction,
FAILED AIRTIME PURCHASE, FAILED ZESA, Postilion transactions, NMB Connect / USSD / Failed Transactions
```

So `NMB Connect / USSD / Failed Transactions / Airtime` is included, and EcoCash *linking* tickets
(`Ecocash-Registration`) are not; those belong to the EcoCash desk. The SMS Alerts and EcoCash desks exclude these
topics so no ticket is worked twice. `FAILED_TXN_EXCLUDE_TOPICS` removes any topic from this desk.

**What Claude sees.** The subject, every form field, the full thread, and what each attachment shows (a
screenshot of the client's SMS or app message often carries the reference). The desk pulls out the account
numbers, references (RRN), amounts, dates and mobile numbers the owner gave and lists them at the top. Everything
is masked first: Claude sees `<NUMBER_1>` and `<MOBILE_1>`, never the values. Amounts and dates stay readable.

## The lookups Claude can call

| Tool | Source | Returns |
|---|---|---|
| `find_switch_transactions` | Postilion `post_tran_leg` (or `post_tran`, `POSTILION_TABLE`) | Card and switch transactions on the account (or by RRN): ZIPIT, EcoCash B2W/W2B, POS, ATM. Each named as the bank names it ("Zipit Send", "Withdrawal - Us on Them"), with the response code and its meaning, whether the switch processed it, the nodes and merchant, the reversal flag, RRN and STAN, and a reading |
| `find_nmb_connect_transactions` | CONECT 2.0 `transactions`, `zimswitch_payment`; BILLERS `econet_payment`, `netone_payment`, `telecel_payment`, `esol_zesa_payment`, `city_of_harare_transactions`, `offline_bill_payment` | NMB Connect, USSD and app transactions by kind (rtgs, transfer, zipit, airtime, bill, zimra or any), with the system's status, code and message, the recipient, and a reading |
| `find_internet_banking_transfers` | IB `rtgstransaction`, `internalfundstransfer`, `own_internalfundstransfer` | Internet banking transfers to or from the account, with their status and reversal flag |
| `check_account_postings` | Core banking (standby): `IACLINK`, `ACNTS`, `TRAN{year}` | Whether the account exists and its currency, and the debits and credits around a day |
| `confirm_transaction` | All of the above | Pins the transaction the ticket is about, checks core banking for the debit and any refund, compares the amount with the ticket, and returns where that leaves the client with the standard outcome and wording |

Lookups only accept accounts, references and mobiles that are on the ticket or came back from an earlier lookup,
so the model can't search anyone else. Each result gets an id (`T1`, `T2`, ...) for `confirm_transaction`.

A table that can't be read (a column named differently, a database Metabase doesn't have) is reported to the
model and in the note as "could not read", never as "nothing found", and it stops the ticket from closing.

## How each system's record is read

**Postilion.** By default the desk reads `post_tran_leg` with the columns of the bank's own Postilion report
query: the client-facing leg (`tran_postilion_originated = '0'`), `settle_amount_req`, the source and sink nodes and
the merchant (`card_acceptor_name_loc`). Unlike the report, fully reversed transactions are kept: a reversal is often
the answer to the ticket. The card number (`pan`) is not read. `POSTILION_TABLE=post_tran` goes back to the columns
the Java investigator read (completion and abort codes, no nodes).

**The switch's own reason.** On both tables the desk also reads `extended_rsp_code` (Postilion's 4-character code,
e.g. P050 on response 91, P120 on 58) and `structured_data_rsp`, Postilion's key/value data. From the key/value data
only `Reason` (e.g. "Sender's card not found" on ZIPIT response 30), `Postilion:DeclineReason`, `EcocashErrorCode` and
`EcocashErrorDesc`, the extended response code and `ReceiverBank` are kept. The same field holds the sender's and
receiver's names, national ID, mobile number and e-mail: those are dropped as the row is read and never reach the
model or the log. `additional_rsp_data` is not read: on failed transactions it is empty. The meanings of the P codes
are not in the code; teach them as knowledge notes. `tools/postilion_profile.py` shows which keys each response code
carries without printing any one client's details.

**Core banking's own reason.** When the agent confirms a switch transaction that core banking (Intellect) did not
approve, it reads Intellect's switch-interface error log for it, the helpdesk's own lookup: `NMBPRODIDC.ASIERRLOG`
joined to `ASIINNP` on entity, branch, date and serial, by RRN (`ASIIN_FIELD_037`) and the interface date (a day either
side). The first message, e.g. "Transaction Limit for Cash Withdrawals exceeded for the month Limit of 300", becomes
the reason in the reply. It is read once per transaction, only when the sink node is Intellect (or not known), and a
failure to read it does not block the ticket.

**Response 57 (not permitted).** When no error is logged for it, the agent reads the account's flags in `ACNTS`
(`ACNTS_ATM_OPERN`, `ACNTS_DORMANT_ACNT`, `ACNTS_INOP_ACNT`, `ACNTS_DB_FREEZED`, `ACNTS_CLOSURE_DATE`; no name or
address): a restriction it finds ("the account is dormant", "ATM use is not enabled on the account") is the reason in
the suggested reply; "none found" or "could not be read" is shown as such. What the helpdesk does about each is a staff
note (see "Staff knowledge" below), not code.
`check_account_postings` shows the same status for any account on the ticket. (The mobile and internet banking flags
are not read: on closed tickets they did not explain EcoCash or ZIPIT failures, and the agent took them for a cause.)

**Response 61 (over a limit) with no core banking error.** The limit is then not the account's in core banking but
the switch's or the partner's (EcoCash); staff take such a ticket to the vendor (a staff note tells the agent so).
The agent is shown which of Postilion's own limits the attempt hit: on a 61 or 65, `structured_data_rsp` carries
`RemainingAdvancedLimits` (XML: per limit its name, period, association, per-transaction and total amounts, and what
remains). Amounts are held in cents and shown in money (a count of 12 digits means no count limit). The limit that
clearly stopped the attempt (in the transaction's currency: no transactions left, over the per-transaction amount, or
over what is left of the total) is the reason in the suggested reply, e.g. "Postilion's limit Contacless without PIN
ZWG: 3 transactions allowed per 24 hours, none left"; when none clearly did, it says so, with the limits listed.

**EcoCash links.** `find_ecocash_links` reads the bank's EcoCash registration log (`ECOCASH_REG_DB_NAME`, default
"Ecocash Reg", table `public.ecocash_registration`): account, mobile, event, currency, date. REGISTRATION is today's way
of linking a wallet, BANKREG the earlier one, DEREGISTRATION removes the link; the latest event per mobile and currency is
the link in force. Given the ticket's account and/or mobile, it returns each wallet's link in force, its recent history,
and the core banking status of the linked account (closed, dormant, ...). Names, ID numbers and the external reference
in that table are not read; the `trace` table in the same database (app devices, OTPs, locations) is not touched.

`confirm_ecocash_link` pins a ticket that is about the link itself (account and/or mobile from the ticket). From the
link log as it stood on the ticket's day (audits) and the linked account's status in core banking it gives where the
link leaves the client, with a suggested reply in the helpdesk's wording (the agent writes its own):

| Status | When | Suggested reply |
|---|---|---|
| not_linked | no link in force for the account or mobile | Kindly advise the client to link the account for B2W or W2B services. |
| linked_elsewhere | the wallet is linked now to an account the ticket does not give | Kindly note that the linked account is <account>. Kindly delink that account and advise the client to register with the new account. |
| linked_account_closed | the wallet is linked to an account that was closed | Kindly advise the client to link the correct account: the linked account is closed. |
| account_not_found | the ticket's account does not exist in core banking | Kindly advise the client to confirm the account number. It is failing to validate: account <account> does not exist in core banking. |
| links_differ | wallets linked to different accounts, or one fine and another linked elsewhere or to a closed account | Kindly note the EcoCash links we can see: <mobile> (USD) is linked to account <account> (the account on this ticket) since <date>; <mobile> (ZWG) is linked to account <account> (the account is dormant) since <date>. Kindly delink the wrong link and advise the client to register with the correct account. |
| linked_ok | linked to the client's account | (none: the link is not the problem; look for the transaction) |
| linked_no_account_given | linked, but the ticket gives no account to compare with | (a person) |

When the wallets differ, no rule picks which one is wrong: the reply says what is seen for each wallet (account,
since when, and the account's status in core banking) and the branch decides which link to delink (#426194,
#752791). Accounts are looked up in core banking in every form (as given, without leading zeros,
zero-padded to 14), since the link log does not always keep core banking's padding.

A link close needs an EcoCash ticket (help topic, subject or the branch's text mentions EcoCash, B2W, W2B or the
wallet): on a ZIPIT "no universal account" is the destination account failing validation (#971899).

A ticket with no confirmed transaction may close on the first five, only when the link was checked for an account or
mobile the ticket owner gave (the wording of the reply is the agent's).

**Zero-padded account numbers.** Corporate accounts are written with leading zeros (00000021269522, eight digits once
the zeros go): a zero-padded number of 10 digits or more counts as an account on the ticket.

**A CIF instead of an account number.** Branches often give the client's CIF, or put a bare 6 to 9 digit number in
the subject, with no account number. Such a number is listed for the agent as a CIF (a labelled "CIF 191565",
"customer number ..." anywhere the owner wrote; a bare number in the subject unless it is a mobile number), and
`find_client_accounts` turns it into the client's accounts: `ACNTS` by `ACNTS_CLIENT_NUM`, joined to `IACLINK` for the
account number, with currency and status (no name or address). If no client has that CIF, it is tried as an account
number. Accounts found from a CIF the ticket owner gave count as the owner's when a ticket is closed.

**The ticket's amount matches no attempt.** Branches sometimes give a balance or a credit as the amount (#631696:
USD 250 on the ticket; the attempts were USD 50 to 240). When the confirmed attempt was declined, the suggested
reply says so openly: "We could not find an attempt of 250.00 on the client's account. We can see the Bank to Wallet of
USD 50.00 on …, which was declined (…), and no money left the client's account. N other attempts on the account were
declined with the same response code." A close is then allowed only on a decline, and only when core banking's
postings around that day show no debit of the ticket's amount that stayed out (a money check, not a wording one);
any other reading with a different amount still goes to a person.

**Advice by reason.** When a system gave a specific reason (the switch's own words, core banking's error, the
account's restriction), the suggested reply gives that reason and no generic advice for the response code: what to
do about it is the agent's to write, from the staff notes (for example "Sender's card not found" → link the account
to the card at the branch; "Limit of 0" → a special limit of zero).

Amounts are in cents and become dollars. The request and response legs of one exchange are merged by RRN and STAN.
A reversal advice (message type 04xx) is folded into the transaction it reverses. Balance and available-funds
inquiries (type 3x) are left out. The transaction is named from the bank's map of (type, message type, source node,
sink node), for example 50 / 0200 / NmbZswIss / NmbIntellect is "Zipit Send", 27 / 0200 / NmbEcocashSc / NmbIntellect
is "Wallet to Bank"; a combination not on the map keeps the generic name ("card/switch transfer").

| Postilion says | Reading |
|---|---|
| Response 00, exchange completed, not reversed | succeeded |
| Response 00, but the exchange never completed | uncertain |
| `tran_reversed` = 2, or a reversal advice | reversed (fully) |
| `tran_reversed` = 1 | uncertain (partially reversed) |
| Response 68, 91, 96, 98 (late, switch down, malfunction, unable) | uncertain |
| A response the switch processed (the bank's list: 51 insufficient funds, 55 PIN, 61 limit, 05, 12, 13, 14, ...) | declined, a business decline, with the reason |
| Any other response (06, 92, ...) | failed: a technical failure, so core banking is checked for a debit |

**NMB Connect and the billers.**

| Status (and code) | Reading |
|---|---|
| SUCCESS | succeeded |
| REVERSED, or a reversal-successful code | reversed |
| FLAG_REVERSAL, FLAGED_FOR_RECON | reversal pending |
| REVERSAL_FAILED | reversal failed |
| PENDING, or a timeout message | uncertain |
| FAILED with code 51 | declined (insufficient funds) |
| FAILED with any other code | failed: core banking decides |

**Internet banking.** `successful` is succeeded, `failed` or `declined` is failed (core banking decides),
`is_txn_reversed` set is reversed, anything else (INCOMPLETE, pending) is uncertain.

**Core banking.** `confirm_transaction` looks for a debit of the amount within a day of the transaction, and a
credit of the same amount up to 7 days after it. A credit whose narration says REV, REFUND, RETURN or CANCEL is a
refund; any other credit of the same amount is only "possibly" one.

## Where it leaves the client, and what the agent does

| Where it leaves the client | Standard outcome |
|---|---|
| succeeded | Tells the branch it went through, with the reference for the recipient's bank or provider. Close. |
| declined | Declined (with the reason) and no money left the account, with advice for the reason (fund the account, a smaller amount, the right PIN, ...). Close. |
| reversed | Did not go through and was reversed; the money is back. Close. |
| failed, not debited | Did not go through and core banking shows no debit. Close. |
| failed, refunded | Did not go through; the client was debited and the amount came back on the day shown. Close. |
| failed, debited, no refund | Escalate: Operations reverses the debit, quoting the reference. |
| reversal pending | Escalate: Operations checks the reversal completes before the client is told. |
| reversal failed | Escalate: Operations reverses manually. |
| uncertain | Escalate: Operations checks core banking and reverses if the money didn't arrive. |
| not found | Ask the branch for the account, date and time, amount, and the reference or a screenshot. |

The wording is in `failed_txn/tools.py` (`standard_reply`, `DECLINE_ADVICE`). Claude adapts it to the thread;
the console shows the standard wording next to what was posted. Escalation notes list the confirmed transaction,
its codes and reference, what core banking shows, the other transactions found, and the next step for Operations.

## Approvals

With `APPROVAL_MODE=unscripted` (the default), a reply waits in the console when Claude says it reasoned beyond
the standard outcome (two transactions in one reply, extra advice) or when its action differs from the standard
one. Standard outcomes are posted straight away; escalations never wait. Approve, edit or reject works as on the
other desks.

## Rules the code enforces (`failed_txn/guardrails.py`)

| Rule | Effect |
|---|---|
| The ticket closed, changed topic or got a new entry during analysis | No action; retried next pass |
| The model tried to look up an account, reference or mobile the ticket never gave | Escalate |
| The reply contains a number the model was never shown, a PIN/OTP/password mention, a link, or template leftovers | Escalate |
| Confidence below `CONFIDENCE_THRESHOLD` | Escalate |
| The agent has already replied `MAX_INFO_REQUESTS` times | Escalate |
| A lookup failed or a table couldn't be read, and the decision is a close | No action; retried next pass |
| Close without `confirm_transaction` | Escalate |
| The confirmed transaction isn't on an account or reference the ticket owner gave | Escalate |
| The confirmed transaction is failed-and-debited, reversal pending, reversal failed or uncertain | Escalate |
| The ticket gives an amount and the confirmed transaction's amount is different | Escalate |
| Another transaction of the same amount that day reads differently, and the ticket gives no reference to tell them apart | Escalate |
| Declined, but core banking shows a debit with no refund | Escalate |
| Succeeded, but the client is the beneficiary rather than the sender | Escalate |
| The reply says the money came back, but it didn't (not reversed or refunded) | Escalate |
| The reply says it went through, but it didn't | Escalate |
| The reply says no money left the account, but the record says otherwise | Escalate |
| More than `MAX_CLOSES_PER_RUN` closes in one pass | The rest wait for the next pass |

If Claude can't be reached (`MODEL_FALLBACK=rules`), a ticket with no account and no reference is asked for them,
a ticket where the branch has answered the agent is escalated, and every other ticket waits for the next pass:
picking the transaction needs the thread read.

## What changed from the Java checks

The Java engine's failed-transaction rules only escalated, and read Postilion in ways that hid the answer:

- **Reversal flag.** `tran_reversed` comes back as text; `"2"` was compared with the number 2 and never matched,
  so fully reversed transactions read as failed. It is now read either way.
- **Amounts.** The ticket amount was multiplied by 100 and matched exactly, and `TOP 25` was taken before the amount
  filter, so a busy account's transaction could fall outside the 25 rows. The amount range is now in the SQL, in
  cents, and 20 cents can't match a $20 claim.
- **Legs and advices.** Request and response legs are merged, and reversal advices folded in, instead of showing
  as separate transactions.
- **Core banking.** Nothing checked whether the client was actually debited. Now every close of a failed
  transaction stands on core banking showing no debit, or a refund.

## Privacy

- Account numbers, references, mobile numbers, national IDs and e-mail addresses are masked before anything reaches
  Claude. Lookups use the real values on the server and mask their results again. Amounts and dates stay readable:
  the agent needs them to pick the transaction.
- Replies and notes get the real values back before they are posted, so the branch and Operations see real numbers.
- With `ATTACHMENT_MODE=model`, images and PDFs (screenshots of the client's message, statements) go to Claude as
  they are, in a separate call that only returns facts. `local` keeps them on the server.
- The decision log stores masked text only, and the e-mail outbox erases message bodies once they are sent.

Claude is approved for use across the bank (October 2026); the masking is kept because the agent doesn't need the
numbers to decide.

## Commands

```bash
python -m nmbz find-transactions --account N [--date YYYY-MM-DD] [--amount 20] [--kind zipit]   # every source, no model
python -m nmbz find-transactions --reference 612345678901                                     # Postilion by RRN
python -m nmbz run --desk failed_txn --ticket 700512                                         # one ticket
python -m nmbz audit --desk failed_txn --since 2026-09-01                                    # replay closed tickets
```

`find-transactions` prints what each lookup returns, with real values (it runs on the agent host, for staff), and
ends with each transaction's reading. `check` lists the open tickets it finds per help topic, and the configured
topics with none.

## Before going live

Run in shadow first (`FAILED_TXN_AGENT_MODE=shadow`), then `audit` on recently closed tickets, then compare with
what the service desk did.

Things to confirm on the first shadow runs:

- **Help topic names** match osTicket (`python -m nmbz check --desk failed_txn` counts open tickets per topic and
  lists configured topics with none).
- **Metabase database names** for `POST OFFICE`, `CONECT 2.0`, `BILLERS` and `IB`. A wrong name shows in `check`.
- **CONECT and biller columns.** The lookups read the columns they find by name; a table whose columns differ shows
  as "could not read" in the notes. Send me any that do.
- **Refund narrations.** Core banking refunds are recognised by REV, REFUND, RETURN or CANCEL in the narration. If
  Operations uses other words, a refund reads as "possibly" and the ticket escalates instead of closing.
- **Beneficiary complaints.** A client who says money never *arrived* is escalated when the record shows it went
  through to them; tell me if the branch should get a standard reply for that instead.

There is no anomaly watch for this desk yet.


## query_data: looking beyond the fixed lookups

The agent can read any table listed in `agentcore/data_catalogue.json` with `query_data`, a structured,
read-only query (the model never writes SQL; `agentcore/dataquery.py` builds it):

- one source (table) per query, only the columns the catalogue lists: no names, ID numbers, e-mails, addresses,
  card numbers, PINs, OTPs or request/response bodies (the catalogue is checked for personal columns on load);
- matched on an account, mobile, reference/RRN or CIF that is on the ticket or came back from an earlier lookup, in
  every form the number may be stored in; another customer's number is refused;
- or, without a match, counts only (mode=count) grouped by non-identifier columns over at most two days, to see
  whether a service is failing for everyone; counts never add numbers the agent may search with;
- a time window on the table's time column, never longer than the table allows (7 days on the airtime tables);
- at most 25 rows, masked before the model sees them.

Each query gets an id (Q1, Q2, ...) that the decision's evidence cites and the audit log keeps, with whether it was
matched on something the ticket owner gave. A query that fails (a wrong column, a database error) is reported to the
model and listed as unread; it does not hold up the ticket.

The first catalogue (from `reports/catalogue`, made with `tools/metabase_names.py --catalogue`):

| Source | Database | What it holds |
|---|---|---|
| zipit_middleware | Postilion Billers | ZIPIT as the payments middleware logged it: debit error, switch response, receiving bank |
| internal_transfer_middleware | Postilion Billers | Transfers between NMB accounts and tap-card transfers: status, error code |
| rtgs_middleware | Postilion Billers | RTGS from the digital channels: status, reason, T24 FT reference |
| zimra_payments | Postilion Billers | ZIMRA domestic and customs payments: status, reason, BP number, ZIMRA references |
| econet_airtime, netone_airtime | BILLERS | Airtime and bundles: the network's status, error code and reason |
| zesa_tokens | BILLERS | ZESA tokens: ZESA's response code and reason, receipt, whether a token was issued (not the token) |
| intellect_api_errors | intellect_errors | Core banking interface errors: operation, error code and message, category |
| ecocash_registration_failures | Ecocash erros | Failed EcoCash link attempts with the error message |
| ft_reversals | Batch Processing | Reversals done in batches by Operations: reference, amount, reason, batch status |
| card_requests | CARD ISSUANCE | New, replacement and duplicate card requests: type, operation, status (no card numbers) |
| channel_limits_by_category | connectorservices | Channel limits per account category (no client data) |

`python -m nmbz check --desk failed_txn` checks every source's table and columns without reading rows. Add a table by
adding it to the catalogue (database, table, about, identifiers with their kind, time column, columns); `QUERY_DATA=false`
turns the tool off and `DATA_CATALOGUE` points at another catalogue file.

## explore_data and query_tables: choosing the tables itself

`query_data` knows the tables its catalogue lists. For anything else the agent works the way a person on the helpdesk
does: it looks at what a database holds, decides which tables matter for the system the client used, and joins them.

- **`explore_data`** shows the approved databases (IB, CONECT 2.0 and its corporate and registration databases, BILLERS,
  Postilion Billers, connectorservices, VBS, POST OFFICE, NMBBIDW, the standby copy of core banking and the loans
  system, and others): the databases, a database's tables, a search for tables or columns by words ("zesa token",
  "reversal"), or one table's columns, date columns and identifier-like columns. Names and types only, never rows.
- **`query_tables`** reads one to three tables of one database, joined on columns the agent names (equality, inner or
  left): matched on the ticket's account, mobile, reference/RRN or CIF within a window (at most 31 days), or counts
  only (at most two days, never grouped by an identifier). Columns are written `alias.column`.

The model still writes no SQL (`agentcore/schemaquery.py` builds it) and the same limits hold as for `query_data`:
another customer's number is refused; counts never add numbers the agent may search with; at most 25 rows, masked
before the model sees them; each query gets an id (Q1, ...) that the evidence cites and a close can rest on under the
same `QUERY_CLOSE` rules. What the agent can name is limited by the schema files in `agentcore/schema/`:

- personal columns (names, ID numbers, e-mails, addresses, card numbers, PINs, OTPs) are not in the files at all, so
  they cannot be named; a mobile-number column can be matched on but never shown;
- free-text bodies (request/response payloads, raw messages, XML, JSON, blobs) cannot be shown or filtered on;
- a table of 100,000 rows or more (the database's own estimate) is only read inside a time window;
- every database the Metabase key can see is explorable. The five Oracle connections (Intellect CBS, STANDBY DB,
  Intellect LMS, Intellect_netguard, Backup) turned out to reach one logical standby, so they are one schema with the
  others as aliases; there is no live core banking behind them. Only tables our login can read are listed;
- the live Postilion realtime database (`Postilion Realtime`, and its `Realtime 144` restore copy) is a **protected
  tier**: a read-write production database, so every table is read only on the first column of one of its indexes, within
  a window of at most two days, and a table with no known index is not read at all;
- on any large table (100,000 rows or more, or a size the database does not report, as on Oracle) a query must use an
  index: `explore_data` lists each table's indexes in order (an index is used through its first column, then the next)
  and a query that uses none is refused with the list. Oracle DATE columns are compared with a DATE literal, since a
  TIMESTAMP literal would stop the index being used. `OLD POST OFFICE` currently drops the connection and is listed
  without indexes.

Rebuild the files after a database changes: `python tools/metabase_names.py --catalogue reports/catalogue`, then
`python tools/build_schema.py --oracle` (the Oracle schemas are read through STANDBY DB). `QUERY_TABLES=false` turns
both tools off and `SCHEMA_DIR` points at another folder. Not yet on the NMB Connect and SMS desks.

**Closing on a query finding (`QUERY_CLOSE`, off by default).** Off, a close still needs a confirmed transaction or
EcoCash link: when the explanation comes only from `query_data`, the agent escalates with its reply as the draft. On,
the guardrails (`query_close_problem`) also let a ticket close on a finding when:

- the decision's evidence cites the query (Q1, ...), which was matched on an account, mobile, reference or CIF the
  ticket owner gave (not one found along the way) and found rows;
- core banking's postings were checked (`check_account_postings`) for the ticket owner's account over the days of the
  finding;
- what the reply says about the money agrees with those postings: "no money left the account" only when there is no
  debit of the ticket's amount that stayed out; "reversed / refunded" only when the debit and the money coming back
  are both there (with no amount on the ticket, the reply may not say either unless there is no debit at all).

The usual rules still apply first (only the ticket's own numbers, no personal data in the reply, confidence, the
close limit per run). Such a reply is off the standard path, so with `APPROVAL_MODE=unscripted` it waits in the
console for a person to approve; with `APPROVAL_MODE=off` the agent posts and closes on its own.

## Staff knowledge and suggested replies

What the helpdesk taught this desk while it was built lives in staff notes (Teach the agent), not in code:
`failed_txn/knowledge_seed.json` holds them, and

```
python -m nmbz knowledge-seed --desk failed_txn
```

proposes the ones not in the console yet (a title already there, in any state, is left alone). An Admin switches each
on in the console; from then on staff can change or switch off any of them without a code change, and every decision
records which notes it used. The notes: response 57 (what to check on the account), response 61/65 (which limit, and
when it goes to the vendor), core banking's limit of zero, "Sender's card not found", another amount than the
ticket's, EcoCash link wording, EcoCash wallets that differ, EcoCash registration failing while linked (Universal
dashboard), and "no universal account" on a ZIPIT.

The lookups give facts (readings, core banking's error, the account's restrictions, Postilion's limits, link status)
and a suggested outcome in the helpdesk's usual wording. The suggestion is a starting point: the agent writes the
reply that fits the ticket. The code checks what matters for the client's money and privacy: whose account it is,
the reading (pending, failed reversals and uncertain outcomes go to a person), whether core banking shows a debit or
the money coming back, that the reply does not claim otherwise, no personal data, confidence and the close limit. The
one check kept that is not about money: a close on the EcoCash link only on an EcoCash ticket (#971899).

`follows_standard_outcome` is true when the agent's action is the suggested one and its reply says the same in
substance; with `APPROVAL_MODE=unscripted` the other replies wait for a person, and with `APPROVAL_MODE=off` they go
out on their own.
