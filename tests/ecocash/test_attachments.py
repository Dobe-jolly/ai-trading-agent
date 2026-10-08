"""Attachments on EcoCash tickets: an ID copy feeds the KYC comparison. All data invented."""
from __future__ import annotations

import io
import json
import zipfile
from types import SimpleNamespace

from ecocash.attachments import AttachmentReader, facts_summary, xlsx_text
from ecocash.osticket import OsTicket
from tests.ecocash.conftest import AGENT_STAFF, TICKET_DB, ScriptedModel, decision, find
from tests.ecocash.test_runner import build

try:
    from PIL import Image

    _buf = io.BytesIO()
    Image.new("RGB", (800, 500), "white").save(_buf, format="PNG")
    ID_IMAGE = _buf.getvalue()
except ImportError:  # without Pillow the reader sends a PNG as it is
    ID_IMAGE = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40_000


def id_copy_facts(id_number="63-1234567 K 12"):
    data = {"kind": "identity_document", "description": "Front of a Zimbabwe national ID card.", "legible": True,
            "national_ids": [id_number]}
    return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="submit_attachment_facts", id="f", input=data)],
                           usage=SimpleNamespace(input_tokens=1500, output_tokens=80))


def test_id_numbers_are_reported_and_partial_ones_hidden():
    kind, summary, values = facts_summary({"kind": "identity_document", "description": "ID card.", "legible": True,
                                           "national_ids": ["63-1234567 K 12", "58-76?4321 F 09"]}, 2000)
    assert kind == "Identity document" and "National IDs shown: 63-1234567 K 12, 1 partly unreadable" in summary
    assert values == ["63-1234567 K 12"]


def test_excel_list_of_accounts_is_read_locally(mb):
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{ns}"/>')
        z.writestr("xl/sharedStrings.xml", f'<sst xmlns="{ns}"><si><t>Account</t></si><si><t>Mobile</t></si></sst>')
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{ns}"><sheetData><row r="1"><c t="s"><v>0</v></c>'
                   f'<c t="s"><v>1</v></c></row><row r="2"><c><v>8.6000013101001E13</v></c><c><v>771234567</v></c></row>'
                   '</sheetData></worksheet>')
    assert "86000013101001 | 771234567" in xlsx_text(out.getvalue())


def test_an_attached_id_copy_settles_a_kyc_failure(settings, mb):
    """The branch attached the client's ID instead of typing it. The bank sent a different ID to EcoCash,
    so the fix is the bank profile, and the close is allowed because the ID is on the ticket."""
    mb.add_ticket(1, "100001", ["Client 0771234567 gets KYC failed when linking, ID copy attached"])
    entry = mb.thread_entries(1)[0]["id"]
    mb.add_file(entry, "id.png", ID_IMAGE)
    mb.add_attempt("kyc_mismatch")  # the bank sent 587654321F09
    reply = ("EcoCash rejected the link because the client's details do not match EcoCash's KYC records. The national ID "
             "the bank sent (<NATIONAL_ID_2>) differs from the client's ID on this ticket (<NATIONAL_ID_1>). Kindly correct "
             "the client's ID number on the bank profile, then ask the client to link the account again.")
    model = ScriptedModel(find(), decision("reply_and_close", reply, category="kyc_mismatch"))
    runner, audit = build(settings, mb, model, mode="live")
    runner.reader = AttachmentReader(runner.store, ScriptedModel(id_copy_facts()), "m", agent_staff_id=AGENT_STAFF)
    runner.run()
    prompt = model.calls[0]["messages"][0]["content"]
    assert "[Attachment 1: id.png" in prompt and "National IDs shown: <NATIONAL_ID_1>" in prompt
    assert "National IDs: <NATIONAL_ID_1>" in prompt  # counted among the details the owner gave
    result = json.loads(model.calls[1]["messages"][-1]["content"][0]["content"])
    assert result["attempts"][0]["id_sent_matches_an_id_on_ticket"] is False
    assert "correct the client's ID number on the bank profile" in result["summary"]["standard_reply"]
    row = audit.last_for_ticket(1)
    assert row["final_action"] == "reply_and_close" and mb.closed == ["100001"]
    posted = mb.thread_entries(1)[-1]["body"]  # the reply (the close adds no entry)
    assert "631234567K12" in posted.replace(" ", "").replace("-", "") and "587654321F09" in posted
