"""How the console talks about the router: its outcomes, the overview chart and the found panel. Served to the page
with the desk list, so the page itself has no desk-specific wording."""

VOCAB = {
    "title": "Router",
    "agent": "Routing agent",
    # The router's outcomes, in the order the overview shows them (labels override the page's defaults).
    "actions": {
        "move": {"label": "Moved to its topic", "short": "Moved", "series": "Moved", "tile": "Moved",
                 "sub": "of decisions"},
        "escalate": {"label": "Suggested to staff", "short": "Suggested", "series": "Suggested to staff",
                     "tile": "Suggested to staff", "sub": "note with the topic it fits", "cls": "warn",
                     "fill": "f-queued"},
        "request_info": {"label": "Asked the owner", "short": "Asked", "series": "Asked the owner",
                         "tile": "Asked the owner", "sub": "the problem was not described"},
        "no_action": {"label": "Left where it is", "short": "Left", "series": "Left where it is"},
    },
    "status": {},  # the found column shows the help topic itself
    "overview_sub": "where the tickets filed under a catch-all topic belong",
    "chart": {
        "key": "routes_by_day",
        "title": "Where the tickets belong",
        "desc": "Each decision by where it sends the ticket: a topic an agent works, another team's topic, or nowhere "
                "else. A rise in 'Unclear' means tickets the topic list does not fit.",
        "label": "Where each ticket belongs, per day",
        "series": [
            {"key": "desk_topic", "label": "A topic an agent works", "fill": "f-delivered"},
            {"key": "staff_topic", "label": "Another team's topic", "fill": "f-info"},
            {"key": "stays", "label": "Stays where it is", "fill": "f-unknown"},
            {"key": "unclear", "label": "Unclear", "fill": "f-failed"},
            {"key": "no_description", "label": "Problem not described", "fill": "f-ask"},
        ],
        "hops": ["catch-all or no topic", "Claude reads it", "similar closed tickets", "the right topic"],
    },
    "found_heading": "Where tickets were sent",
    "found_sub": "The help topic behind each decision, moved or suggested.",
    "found_col": "Topic",
    "found_none": "No topic was chosen.",
    "queue_sub": "Every open ticket under a catch-all topic or with no help topic",
    "queue_empty": "No open tickets to route at the last pass.",
    "template_label": "Standard request",
    "pause_placeholder": "e.g. help topics being reorganised",
    "anomaly": {
        "unavailable": "The router has no anomaly watch. A rise in 'Unclear' on the overview means tickets the topic "
                       "list does not fit.",
        "intro": "", "signals_sub": "", "ok_share_label": "Share of successes", "comparisons": [], "tickets_title": "",
        "hourly": {"source": "none", "title": "", "desc": "", "label": "", "empty": "", "series": []},
    },
}
