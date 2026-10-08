"""The submit_decision tool worded for this desk; the Decision itself is shared (agentcore.decision)."""
from agentcore.decision import *  # noqa: F401,F403
from agentcore.decision import submit_decision_tool

SUBMIT_DECISION_TOOL = submit_decision_tool(
    close_help='a confirmed transaction explains what happened and the reply tells the branch what to tell the client',
    ask_help='a detail is needed to find the transaction (account it was made from, date, amount, reference or a screenshot)',
    category_help='Where the confirmed transaction leaves the client (succeeded, declined, reversed, failed_no_debit, failed_refunded, reversal_pending, reversal_failed, uncertain), or what is missing (e.g. missing_account_number, not_found).',
    standard_help='true when the action and reply follow the standard outcome confirm_transaction gave; false when you reasoned beyond it to an outcome it would not give.',
)
