"""How the console talks about the failed-transactions desk: status labels and the overview chart.
Served to the page with the desk list, so the page itself has no desk-specific wording."""

VOCAB = {
    "title": "Failed transactions",
    "agent": "Failed transactions agent",
    "status": {
        "succeeded": ["ok", "Went through: the money moved"],
        "declined": ["ok", "Declined before any money moved"],
        "failed_no_debit": ["ok", "Failed; core banking shows no debit"],
        "reversed": ["ok", "Failed and reversed: money returned"],
        "failed_refunded": ["ok", "Failed; debited and credited back"],
        "reversal_pending": ["warn", "Reversal queued, not yet confirmed"],
        "reversal_failed": ["crit", "Automatic reversal failed: client may be out of pocket"],
        "uncertain": ["crit", "No final answer (timeout or pending): possible debit"],
        "failed": ["crit", "Failed and debited without a refund"],
        "posted": ["idle", "Only a core banking posting was confirmed"],
        "not_found": ["ask", "No transaction found for the ticket's details"],
    },
    "overview_sub": "where the confirmed transactions left the clients",
    "chart": {
        "key": "outcomes_by_day",
        "title": "Where the clients stood",
        "desc": "The transaction behind each decision, by what it meant for the client. A rise in red means money "
                "may be stuck: reversals failing or switch timeouts.",
        "label": "Confirmed transaction outcome per day",
        "series": [
            {"key": "went_through", "label": "Went through", "fill": "f-delivered"},
            {"key": "no_money_moved", "label": "No money moved", "fill": "f-ok2"},
            {"key": "money_back", "label": "Money returned", "fill": "f-info"},
            {"key": "reversal_pending", "label": "Reversal pending", "fill": "f-queued"},
            {"key": "needs_ops", "label": "Needs operations", "fill": "f-failed"},
            {"key": "not_found", "label": "Not found", "fill": "f-unknown"},
        ],
        "hops": ["channel (app, USSD, card)", "switch / biller", "core banking", "reversal"],
    },
    "found_heading": "What the lookups found",
    "found_sub": "Where the confirmed transaction behind each decision left the client.",
    "found_col": "Transaction",
    "found_none": "No transaction was confirmed.",
    "queue_sub": "Every open failed-transaction ticket (ZIPIT, EcoCash B2W/W2B, card, NMB Connect, airtime, ZESA)",
    "queue_empty": "No open failed-transaction tickets at the last pass.",
    "template_label": "Standard wording for this outcome",
    "pause_placeholder": "e.g. Postilion maintenance INC-2231",
    "anomaly": {
        "unavailable": "This desk has no anomaly watch yet. Switch timeouts and failing reversals show on the overview "
                       "as 'Needs operations'.",
        "intro": "", "signals_sub": "", "ok_share_label": "Share of successes", "comparisons": [], "tickets_title": "",
        "hourly": {"source": "none", "title": "", "desc": "", "label": "", "empty": "", "series": []},
    },
}
