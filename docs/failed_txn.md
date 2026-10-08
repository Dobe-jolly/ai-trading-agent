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
| `find_switch_transactions` | Postilion `post_tran` | Card and switch transactions on the account (or by RRN): ZIPIT, EcoCash B2W/W2B, POS, ATM. Each with the response code and its meaning, the reversal flag, RRN and STAN, and a reading |
| `find_nmb_connect_transactions` | CONECT 2.0 `transactions`, `zimswitch_payment`; BILLERS `econet_payment`, `netone_payment`, `telecel_payment`, `esol_zesa_payment`, `city_of_harare_transactions`, `offline_bill_payment` | NMB Connect, USSD and app transactions by kind (rtgs, transfer, zipit, airtime, bill, zimra or any), with the system's status, code and message, the recipient, and a reading |
| `find_internet_banking_transfers` | IB `rtgstransaction`, `internalfundstransfer`, `own_internalfundstransfer` | Internet banking transfers to or from the account, with their status and reversal flag |
| `check_account_postings` | Core banking (standby): `IACLINK`, `ACNTS`, `TRAN{year}` | Whether the account exists and its currency, and the debits and credits around a day |
| `confirm_transaction` | All of the above | Pins the transaction the ticket is about, checks core banking for the debit and any refund, compares the amount with the ticket, and returns where that leaves the client with the standard outcome and wording |

Lookups only accept accounts, references and mobiles that are on the ticket or came back from an earlier lookup,
so the model can't search anyone else. Each result gets an id (`T1`, `T2`, ...) for `confirm_transaction`.

A table that can't be read (a column named differently, a database Metabase doesn't have) is reported to the
model and in the note as "could not read", never as "nothing found", and it stops the ticket from closing.

## How each system's record is read

**Postilion.** Amounts are in cents and become dollars. The request and response legs of one exchange are merged
by RRN and STAN. A reversal advice (message type 04xx) is folded into the transaction it reverses. Balance and
available-funds inquiries are left out.

| Postilion says | Reading |
|---|---|
| Response 00, exchange completed, not reversed | succeeded |
| Response 00, but the exchange never completed | uncertain |
| `tran_reversed` = 2, or a reversal advice | reversed (fully) |
| `tran_reversed` = 1 | uncertain (partially reversed) |
| Response 68, 91, 96, 98 (late, switch down, malfunction, unable) | uncertain |
| Any other response (51 insufficient funds, 61 limit, 55 PIN, ...) | declined, with the reason |

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
