"""The submit_decision tool worded for this desk; the Decision itself is shared (agentcore.decision)."""
from agentcore.decision import *  # noqa: F401,F403
from agentcore.decision import submit_decision_tool

SUBMIT_DECISION_TOOL = submit_decision_tool(
    close_help='a confirmed transaction explains what happened and the reply tells the branch what to tell the client',
    ask_help='a detail is needed to find the transaction (account it was made from, date, amount, reference or a screenshot)',
    category_help='Where the confirmed transaction leaves the client (succeeded, declined, reversed, failed_no_debit, failed_refunded, reversal_pending, reversal_failed, uncertain), or what is missing (e.g. missing_account_number, not_found).',
    standard_help='true when your action is the one the suggested outcome gives and your reply says the same thing in substance (your own wording is fine); false when you reached a different action or explanation.',
)
