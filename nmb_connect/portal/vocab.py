"""How the console talks about the NMB Connect desk: status labels and the overview chart.
Served to the page with the desk list, so the page itself has no desk-specific wording."""

VOCAB = {
    "title": "NMB Connect",
    "agent": "NMB Connect agent",
    # Outcomes in the order the overview shows them. Asking is usually the branch acting (delete the profile, have
    # the number updated) and reporting back, not only missing details.
    "actions": {
        "reply_and_close": {},
        "request_info": {"label": "Asked the branch", "short": "Asked", "series": "Asked the branch",
                         "tile": "Asked the branch", "sub": "to act and report back, or for details"},
        "escalate": {},
        "no_action": {},
    },
    "status": {
        "registered": ["ok", "Registered with the right number: advice for the client"],
        "not_registered": ["ok", "Not registered: the branch registers the client"],
        "reregistered": ["ok", "Registered again: the fix is in place"],
        "mobile_updated": ["ok", "The profile has the new number"],
        "account_inactive": ["ok", "Account dormant, closed or frozen"],
        "registration_failed_email": ["ok", "Registration failed: e-mail did not match"],
        "registration_failed_temporary": ["ok", "Registration failed: temporary error"],
        "how_to": ["ok", "Branch procedure"],
        "mobile_mismatch": ["ask", "Profile has another number: branch and Data Science update it"],
        "already_registered": ["ask", "Old profile blocks registration: branch deletes it"],
        "account_active": ["ask", "Account active: branch deletes the profile, client re-registers"],
        "needs_details": ["ask", "No CIF or account to check"],
        "registered_unverified": ["ask", "Profile found; mobile not given"],
        "no_attempt_found": ["ask", "No profile and no failed registration recorded"],
        "account_not_found": ["ask", "Account not found in core banking"],
        "duplicate_profiles": ["crit", "More than one profile for the CIF"],
        "number_on_other_profile": ["crit", "The number is on another customer's profile"],
        "locked": ["warn", "Profile locked or blocked"],
        "otp_not_delivered": ["crit", "OTPs failing at the SMS gateway"],
        "registration_failed_duplicate": ["crit", "Registration failed: duplicate customer profile"],
        "registration_failed_unknown": ["warn", "Registration failed: unknown error"],
        "registered_cannot_login": ["warn", "Registered, right number, still cannot log in"],
        "account_other_cif": ["warn", "Account under another CIF"],
        "other": ["idle", "Other NMB Connect issue"],
    },
    "overview_sub": "where the clients stood on NMB Connect",
    "chart": {
        "key": "outcomes_by_day",
        "title": "Where the clients stood",
        "desc": "The assessment behind each decision, by who acts next. A rise in red means profiles that only ICT "
                "can fix (duplicates, failing OTPs); a rise in amber means branches deleting profiles or updating "
                "numbers.",
        "label": "Assessment outcome per day",
        "series": [
            {"key": "advice", "label": "Advice for the client", "fill": "f-delivered"},
            {"key": "register", "label": "Branch registers", "fill": "f-ok2"},
            {"key": "fixed", "label": "Fixed", "fill": "f-info"},
            {"key": "account", "label": "Account inactive", "fill": "f-unknown"},
            {"key": "branch_fix", "label": "Branch fixes the profile", "fill": "f-queued"},
            {"key": "needs_person", "label": "Needs ICT", "fill": "f-failed"},
            {"key": "details", "label": "Details missing", "fill": "f-ask"},
        ],
        "hops": ["NMB Connect profile", "core banking CIF", "app activity", "SMS gateway"],
    },
    "found_heading": "What the assessments found",
    "found_sub": "Where the client stood behind each decision.",
    "found_col": "Assessment",
    "found_none": "No client was assessed.",
    "queue_sub": "Every open NMB Connect ticket (registration, login, OTP, username, accounts)",
    "queue_empty": "No open NMB Connect tickets at the last pass.",
    "template_label": "Standard wording for this outcome",
    "pause_placeholder": "e.g. NMB Connect maintenance INC-2240",
    "anomaly": {
        "unavailable": "This desk has no anomaly watch yet. Failing OTPs and duplicate profiles show on the overview "
                       "as 'Needs ICT'.",
        "intro": "", "signals_sub": "", "ok_share_label": "Share of successes", "comparisons": [], "tickets_title": "",
        "hourly": {"source": "none", "title": "", "desc": "", "label": "", "empty": "", "series": []},
    },
}
