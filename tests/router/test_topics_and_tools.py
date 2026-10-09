"""The help-topic catalogue as osTicket keeps it, and the router's two lookups. All data invented."""
from __future__ import annotations

import json

from agentcore.pii import Pseudonymizer
from router import topics
from router.osticket import OsTicket
from router.tools import Evidence, Toolbox
from tests.router.conftest import OWNERS, TICKET_DB


def catalogue(mb, settings, owners=OWNERS):
    return topics.load(mb, TICKET_DB, descriptions=topics.load_descriptions(settings.topic_descriptions_file),
                       owners=owners, catch_alls=settings.topics, never_to=settings.never_to, move_to=settings.move_to)


def test_catalogue_builds_names_as_people_write_them(mb, settings):
    mb.add_ticket(1, "100001", ["x"], topic="SMS Alerts / Transactional")
    c = catalogue(mb, settings)
    names = {t.name for t in c.active}
    assert "SMS Alerts / Transactional" in names and "Ecocash / Failed Transactions" in names
    # inactive, a child of an inactive parent, and archived topics are not offered
    assert not names & {"zzGeneral Inquiry", "Security and Loss Control / Access Control", "Old Archived Topic"}
    assert c.find("ecocash / failed  transactions /").topic_id == 94  # spacing, case and the slash do not matter
    assert c.find("SMS Alerts / Transactional").desk == "SMS Alerts" and c.find("NMB Connect / OTP").desk is None
    assert c.find("SMS Alerts / Transactional").recent == 1
    assert c.find("HelpDesk / Login").description == "A staff member cannot log in to a bank system: wrong " \
                                                     "credentials, locked profile, password reset."
    targets = {t.name for t in c.targets()}
    assert "HelpDesk / Logging Ticket" not in targets and "Escalated to Vendor" not in targets
    listed = c.prompt_list()
    assert "- SMS Alerts / Transactional — " in listed and "worked by the SMS Alerts agent" in listed


def test_osticket_notes_describe_topics_the_file_does_not(mb, settings):
    mb.topic_rows[102]["notes"] = "<p>OTP problems on the <b>app</b></p>"
    c = topics.load(mb, TICKET_DB, descriptions={}, owners=OWNERS, catch_alls=settings.topics)
    assert c.find("NMB Connect / OTP").description == "OTP problems on the app"


def test_only_desk_topics_and_move_to_are_moved_without_a_person(mb, settings):
    c = catalogue(mb, settings)
    assert c.may_move_to(c.find("Failed ZIPIT Transaction")) and not c.may_move_to(c.find("NMB Connect / OTP"))
    c = topics.load(mb, TICKET_DB, owners=OWNERS, catch_alls=settings.topics, move_to=["NMB Connect"])
    assert c.may_move_to(c.find("NMB Connect / OTP"))


def toolbox(mb, settings):
    store = OsTicket(mb, TICKET_DB, topics=settings.topics)
    return Toolbox(store, catalogue(mb, settings))


def run(box, name, args, pseudo=None, ev=None):
    pseudo, ev = pseudo or Pseudonymizer(), ev or Evidence()
    text, error = box.run(name, args, pseudo, ev, None)
    return (text if error else json.loads(text)), ev


def test_similar_closed_tickets_show_where_people_filed_them(mb, settings):
    mb.add_closed(11, "100011", "ZIPIT not received", "Failed ZIPIT Transaction", "Client 0771234567 sent ZIPIT")
    mb.add_closed(12, "100012", "zipit reversal", "Failed ZIPIT Transaction")
    mb.add_closed(13, "100013", "ZIPIT query", "HelpDesk / Logging Ticket")
    mb.add_closed(14, "100014", "Login", "HelpDesk / Login", "cannot login to zipit portal")
    out, ev = run(toolbox(mb, settings), "find_similar_closed_tickets", {"words": ["ZIPIT"]})
    assert out["closed_tickets_found"] == 4
    assert out["filed_under"][0] == {"topic": "Failed ZIPIT Transaction", "tickets": 2, "active": True,
                                     "worked_by_agent": "Failed transactions"}
    assert {"topic": "HelpDesk / Logging Ticket", "tickets": 1, "active": True, "catch_all": True} in out["filed_under"]
    assert ev.similar_topics["Failed ZIPIT Transaction"] == 2


def test_lookups_refuse_numbers_and_unknown_topics(mb, settings):
    box = toolbox(mb, settings)
    out, ev = run(box, "find_similar_closed_tickets", {"words": ["0771234567"]})
    assert "not a plain word" in out and ev.refused == ["find_similar_closed_tickets"]
    out, _ = run(box, "find_similar_closed_tickets", {"words": ["client", "please"]})
    assert "distinctive word" in out
    out, _ = run(box, "get_topic_examples", {"topic": "Payroll"})
    assert "not an active help topic" in out


def test_topic_examples_are_masked(mb, settings):
    mb.add_ticket(21, "100021", ["Client 0771234567 acc 86000013101001 gets no OTP"], topic="NMB Connect / OTP",
                  subject="OTP not received")
    out, ev = run(toolbox(mb, settings), "get_topic_examples", {"topic": "nmb connect / otp"})
    assert out["topic"] == "NMB Connect / OTP" and out["examples"][0]["subject"] == "OTP not received"
    text = json.dumps(out)
    assert "0771234567" not in text and "86000013101001" not in text and "<MOBILE_1>" in text
    assert ev.topics_checked == ["NMB Connect / OTP"]
