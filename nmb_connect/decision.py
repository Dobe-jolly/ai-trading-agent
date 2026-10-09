"""The submit_decision tool worded for this desk; the Decision itself is shared (agentcore.decision)."""
from agentcore.decision import *  # noqa: F401,F403
from agentcore.decision import submit_decision_tool

SUBMIT_DECISION_TOOL = submit_decision_tool(
    close_help=("the assessment explains where the client stands and the reply tells the branch what to do or tell "
                "the client"),
    ask_help=("a detail is missing (CIF, account, mobile, a screenshot of the error), or the branch must act and report "
              "back (delete the profile and have the client register again, update the mobile)"),
    category_help=("Where the client stands, from assess_access_issue (e.g. not_registered, registered, "
                   "mobile_mismatch, already_registered, account_inactive, duplicate_profiles), or what is missing "
                   "(e.g. needs_details)."),
    standard_help=("true when the action and reply follow the standard outcome assess_access_issue gave; false when you "
                   "reasoned beyond it to an outcome it would not give."),
)
