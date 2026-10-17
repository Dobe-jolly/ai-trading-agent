"""The submit_decision tool worded for this agent; the Decision itself is shared (agentcore.decision)."""
from agentcore.decision import *  # noqa: F401,F403
from agentcore.decision import submit_decision_tool

SUBMIT_DECISION_TOOL = submit_decision_tool(
    close_help='the checks explain the issue and the reply tells the branch what to do',
    ask_help='one specific detail is missing or contradictory (e.g. no account number)',
    category_help='The main check status behind the decision (e.g. delivered_check_spam, different_number, socket_failed, missing_account_number).',
    standard_help='true when your action is the one the check statuses suggest and your reply says the same thing in substance (your own wording is fine); false when you reasoned beyond them (lookups, the thread, attachments) to a different action or explanation.',
)
