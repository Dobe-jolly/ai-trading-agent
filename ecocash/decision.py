"""The submit_decision tool worded for this agent; the Decision itself is shared (agentcore.decision)."""
from agentcore.decision import *  # noqa: F401,F403
from agentcore.decision import submit_decision_tool

SUBMIT_DECISION_TOOL = submit_decision_tool(
    close_help='the lookups explain the issue and the reply tells the branch what to do',
    ask_help='a specific detail is missing or the branch must act and report back (e.g. no mobile number, or delink first)',
    category_help='The main finding behind the decision: the error_type or status from the lookup (e.g. already_linked, kyc_mismatch, wallet_closed, linked), or e.g. missing_mobile_number.',
    standard_help='true when the action and reply follow the standard reply for the latest attempt; false when you reasoned beyond it (other lookups, the thread, attachments) to an outcome the standard reply would not give.',
)
