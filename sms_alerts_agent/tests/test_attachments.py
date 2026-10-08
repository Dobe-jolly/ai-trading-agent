"""Ticket attachments: fetching through Metabase, reading, and how the agent uses them."""
from __future__ import annotations

import base64
import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from agent.attachments import (
    DOCX,
    XLSX,
    AttachmentReader,
    docx_text,
    eml_text,
    facts_summary,
    render_attachment,
    sniff,
    xlsx_text,
)
from agent.osticket import OsTicket, Ticket
from agentcore.pii import Pseudonymizer
from tests.conftest import AGENT_STAFF, TICKET_DB, ScriptedModel, check, decision
from tests.test_agent_runner import CLOSE_REPLY, build, seed

try:
    from PIL import Image
except ImportError:  # the agent works without Pillow; some tests need it to make images
    Image = None

FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40_000  # big enough not to look like a logo


def png(width: int = 600, height: int = 1200) -> bytes:
    if Image is None:
        return FAKE_PNG
    out = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(out, format="PNG")
    return out.getvalue()


def docx(*paragraphs: str) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", f'<w:document xmlns:w="w"><w:body>{body}</w:body></w:document>')
    return out.getvalue()


def xlsx(rows: list[list[str]]) -> bytes:
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    shared, xml_rows = [], []
    for r, row in enumerate(rows, 1):
        cells = []
        for value in row:
            if value.replace(".", "").replace("E", "").isdigit():
                cells.append(f"<c><v>{value}</v></c>")
            else:
                shared.append(value)
                cells.append(f'<c t="s"><v>{len(shared) - 1}</v></c>')
        xml_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{ns}"/>')
        z.writestr("xl/sharedStrings.xml", f'<sst xmlns="{ns}">' + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{ns}"><sheetData>{"".join(xml_rows)}</sheetData></worksheet>')
    return out.getvalue()


def facts(**over) -> SimpleNamespace:
    data = {"kind": "phone_sms_screenshot", "description": "SMS inbox on the client's phone; last NMB alert on 20-09.",
            "legible": True, "phone_numbers": ["0772 123 456"], "account_numbers": ["12345678901"],
            "sms_messages": [{"sender": "NMB BANK", "received": "20-09-2026 14:02", "text": "Acc ****8901 debited USD 15.00"}]}
    data.update(over)
    return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="submit_attachment_facts", id="f", input=data)],
                           usage=SimpleNamespace(input_tokens=1600, output_tokens=120))


def ticket_with_thread(mb, store):
    t = Ticket(ticket_id=7, number="482913", help_topic="SMS Alerts / Transactional", created="", phone_number=None,
               cif=None, account_number=None, account_number_2=None, currency=None, alert_type_field=None)
    return store.load_thread(t)


# ---- fetching -------------------------------------------------------------------------------
def test_file_content_reassembles_chunks_and_falls_back_to_hex(mb):
    seed(mb)
    data = bytes(range(256)) * 50  # 12.8 KB over four chunks, every byte value
    entry = mb.thread_entries(7)[0]["id"]
    fid = mb.add_attachment(entry, "scan.pdf", data, "application/pdf")
    store = OsTicket(mb, TICKET_DB)
    assert store.file_content(fid, len(data)) == data
    assert any("TO_BASE64(filedata)" in q for _, q in mb.sql_log)
    mb.no_to_base64 = True
    store = OsTicket(mb, TICKET_DB)
    assert store.file_content(fid, len(data)) == data
    assert any("HEX(filedata)" in q for _, q in mb.sql_log)
    with pytest.raises(Exception, match="osTicket records"):
        store.file_content(fid, len(data) + 1)


def test_attachments_are_listed_per_entry_in_thread_order(mb):
    seed(mb, messages=("first", "second"))
    first, second = (e["id"] for e in mb.thread_entries(7))
    mb.add_attachment(second, "b.png", FAKE_PNG)
    mb.add_attachment(first, "a.png", FAKE_PNG)
    store = OsTicket(mb, TICKET_DB)
    ticket = ticket_with_thread(mb, store)
    found = store.load_attachments(ticket)
    assert [(a.number, a.name) for a in found] == [(1, "a.png"), (2, "b.png")]
    assert [a.name for a in ticket.entries[1].attachments] == ["b.png"]


# ---- local extraction ------------------------------------------------------------------------
def test_text_formats_are_read_locally():
    assert sniff(docx("x")) == DOCX and sniff(xlsx([["a"]])) == XLSX and sniff(b"%PDF-1.7") == "application/pdf"
    assert "Account 00012345678901" in docx_text(docx("Client: T Moyo", "Account 00012345678901"))
    sheet = xlsx_text(xlsx([["Account", "Phone"], ["2.0123456789012E13", "772123456"]]))
    assert "20123456789012 | 772123456" in sheet  # Excel's float account number comes back whole
    mail = (b"Subject: Alerts not received\r\nDate: Mon, 5 Oct 2026 09:00:00 +0200\r\n"
            b"Content-Type: text/plain\r\n\r\nPlease check acc 12345678901\r\n")
    assert "Please check acc 12345678901" in eml_text(mail) and "Alerts not received" in eml_text(mail)


# ---- what is read and what is not ----------------------------------------------------------------
def test_reader_decides_what_to_read(mb):
    seed(mb, messages=("see attached", "and this"))
    first, second = (e["id"] for e in mb.thread_entries(7))
    agent_entry = mb.add_entry(7, "R", "Kindly share the account", staff_id=AGENT_STAFF)
    mb.add_attachment(first, "accounts.xlsx", xlsx([["Account"], ["12345678901"]]), XLSX)
    mb.add_attachment(first, "logo.png", b"\x89PNG" + b"\x00" * 3000, inline=True)
    mb.add_attachment(second, "again.xlsx", b"same", XLSX, signature="sig1")  # same content as accounts.xlsx
    mb.add_attachment(second, "plugin.pdf", b"%PDF", "application/pdf", bk="F")
    mb.add_attachment(second, "huge.pdf", b"%PDF", "application/pdf", size=50_000_000)
    mb.add_attachment(second, "archive.zip", b"PK\x03\x04", "application/zip")
    mb.add_attachment(second, "photo.png", FAKE_PNG)
    mb.add_attachment(agent_entry, "ours.txt", b"agent file", "text/plain")
    store = OsTicket(mb, TICKET_DB)
    ticket = ticket_with_thread(mb, store)
    result = AttachmentReader(store, None, "m", mode="local", agent_staff_id=AGENT_STAFF).read(ticket)
    status = {a.name: (a.status, a.note) for a in result.attachments}
    assert status["accounts.xlsx"][0] == "read"
    assert "e-mail signature logo" in status["logo.png"][1]
    assert status["again.xlsx"] == ("skipped", "same file as attachment 1")
    assert status["plugin.pdf"][0] == "failed" and "storage plugin" in status["plugin.pdf"][1]
    assert "limit" in status["huge.pdf"][1]
    assert status["archive.zip"] == ("not_read", "file type not supported (application/zip)")
    assert status["photo.png"][0] == "not_read" and "ATTACHMENT_MODE=local" in status["photo.png"][1]
    assert status["ours.txt"] == ("skipped", "posted by the agent")
    xl = next(a for a in result.attachments if a.name == "accounts.xlsx")
    assert "12345678901" in xl.values and xl.kind == "Excel workbook"
    assert result.input_tokens == 0  # nothing went to a model


def test_newest_owner_files_first_when_over_the_limit(mb):
    seed(mb, messages=("old", "new"))
    old, new = (e["id"] for e in mb.thread_entries(7))
    for i in range(3):
        mb.add_attachment(old, f"old{i}.txt", f"old {i}".encode(), "text/plain")
    mb.add_attachment(new, "new.txt", b"new file", "text/plain")
    store = OsTicket(mb, TICKET_DB)
    result = AttachmentReader(store, None, "m", mode="local", max_files=2).read(ticket_with_thread(mb, store))
    read = sorted(a.name for a in result.attachments if a.status == "read")
    assert read == ["new.txt", "old0.txt"]


def test_image_is_read_by_the_model_and_summarised(mb):
    seed(mb, messages=("screenshot attached",))
    entry = mb.thread_entries(7)[0]["id"]
    mb.add_attachment(entry, "IMG_2031.png", png())
    store = OsTicket(mb, TICKET_DB)
    reader_model = ScriptedModel(facts(phone_numbers=["0772 123 456", "07?2 999 111"]))
    result = AttachmentReader(store, reader_model, "vision-model").read(ticket_with_thread(mb, store))
    item = result.attachments[0]
    assert item.status == "read" and item.kind == "Screenshot of SMS messages on a phone"
    assert "Mobile numbers shown: 0772123456, 1 partly unreadable (not shown)" in item.summary
    assert "[20-09-2026 14:02] NMB BANK: Acc ****8901 debited USD 15.00" in item.summary
    assert "12345678901" in item.values and result.input_tokens == 1600
    call = reader_model.calls[0]
    assert call["model"] == "vision-model" and call["tool_choice"]["name"] == "submit_attachment_facts"
    block = call["messages"][0]["content"][0]
    assert block["type"] == "image" and block["source"]["media_type"] in ("image/jpeg", "image/png")
    masked = Pseudonymizer().mask(render_attachment(item))
    assert "0772123456" not in masked and "12345678901" not in masked and "<MOBILE_1>" in masked


def test_pdf_goes_as_a_document_and_failures_are_contained(mb):
    seed(mb, messages=("form attached",))
    entry = mb.thread_entries(7)[0]["id"]
    mb.add_attachment(entry, "form.pdf", b"%PDF-1.4 fake", "application/octet-stream")
    mb.add_attachment(entry, "second.pdf", b"%PDF-1.4 other", "application/pdf")
    store = OsTicket(mb, TICKET_DB)

    class HalfBroken(ScriptedModel):
        def create(self, **kwargs):
            if len(self.calls) == 1:
                self.calls.append(kwargs)
                raise RuntimeError("overloaded")
            return super().create(**kwargs)

    model = HalfBroken(facts(kind="form_or_letter", description="SMS alert application form.", sms_messages=[]))
    result = AttachmentReader(store, model, "m").read(ticket_with_thread(mb, store))
    by_name = {a.name: a for a in result.attachments}
    assert model.calls[0]["messages"][0]["content"][0]["type"] == "document"
    assert by_name["form.pdf"].status == "read" and by_name["form.pdf"].kind == "Form or letter"
    assert by_name["second.pdf"].status == "failed" and "overloaded" in by_name["second.pdf"].note


@pytest.mark.skipif(Image is None, reason="needs Pillow")
def test_tiny_images_are_logos_and_big_photos_are_shrunk(mb):
    seed(mb, messages=("pics",))
    entry = mb.thread_entries(7)[0]["id"]
    mb.add_attachment(entry, "icon.png", png(120, 120), "image/png")
    mb.add_attachment(entry, "photo.png", png(4000, 3000), "image/png")
    store = OsTicket(mb, TICKET_DB)
    model = ScriptedModel(facts(kind="photo_other"))
    result = AttachmentReader(store, model, "m", min_inline_bytes=0).read(ticket_with_thread(mb, store))
    by_name = {a.name: a for a in result.attachments}
    assert by_name["icon.png"].status == "skipped" and "logo" in by_name["icon.png"].note
    sent = model.calls[0]["messages"][0]["content"][0]["source"]
    shrunk = Image.open(io.BytesIO(base64.b64decode(sent["data"])))
    assert sent["media_type"] == "image/jpeg" and max(shrunk.size) == 1568


def test_facts_summary_never_shows_partial_numbers():
    kind, summary, values = facts_summary({"kind": "core_banking_screenshot", "description": "Customer profile.",
                                           "legible": False, "account_numbers": ["0201-2345-678?"],
                                           "other_text": "Email on file: none"}, 2000)
    assert kind == "Screenshot of a banking system screen" and "0201" not in summary
    assert "1 partly unreadable" in summary and "(Parts of the file could not be read.)" in summary and values == []


# ---- end to end -------------------------------------------------------------------------------
def test_account_only_in_a_screenshot_is_checked_and_can_close(settings, mb):
    """The form has no account; the branch attached a screenshot showing it. The agent reads it,
    checks it, and the close is allowed because the account came from the ticket owner's file."""
    seed(mb, account="", messages=("Client not receiving debit alerts, see screenshot",))
    entry = mb.thread_entries(7)[0]["id"]
    mb.add_attachment(entry, "IMG_2031.png", png())
    model = ScriptedModel(check(), decision("reply_and_close", CLOSE_REPLY))
    runner, audit = build(settings, mb, model, mode="live")
    runner.reader = AttachmentReader(runner.store, ScriptedModel(facts()), "m", agent_staff_id=AGENT_STAFF)
    runner.run()
    prompt = model.calls[0]["messages"][0]["content"]
    assert "[Attachment 1: IMG_2031.png" in prompt and "Screenshot of SMS messages on a phone" in prompt
    assert "Account numbers shown: <NUMBER_1>" in prompt and "12345678901" not in prompt
    row = audit.last_for_ticket(7)
    assert row["final_action"] == "reply_and_close" and "close" in json.loads(row["executed"])
    files = json.loads(row["attachments"])
    assert files[0]["status"] == "read" and "<NUMBER_1>" in files[0]["summary"]
    assert "12345678901" not in row["attachments"] and row["input_tokens"] >= 1600


def test_unread_attachment_does_not_count_as_owner_supplied(settings, mb):
    """If the screenshot could not be read, an account the model invents cannot pass the close gate."""
    seed(mb, account="", messages=("see screenshot",))
    entry = mb.thread_entries(7)[0]["id"]
    mb.add_attachment(entry, "IMG.heic", b"\x00\x00\x00\x18ftypheic" + b"\x00" * 20_000, "image/heic")
    model = ScriptedModel(decision("request_info", "Kindly type the client's account number in a reply; the attached photo could not be opened."))
    runner, audit = build(settings, mb, model, mode="shadow")
    runner.reader = AttachmentReader(runner.store, ScriptedModel(), "m", agent_staff_id=AGENT_STAFF)
    runner.run()
    prompt = model.calls[0]["messages"][0]["content"]
    assert "[Attachment 1: IMG.heic, image/heic" in prompt and "Could not be read" in prompt
    assert audit.last_for_ticket(7)["final_action"] == "request_info"


def test_portal_and_cli_show_what_was_read(settings, mb, monkeypatch, capsys):
    from agent import __main__ as cli
    from agent.portal import data

    seed(mb, messages=("list attached",))
    entry = mb.thread_entries(7)[0]["id"]
    mb.add_attachment(entry, "accounts.csv", b"account,phone\n12345678901,0772123456\n", "text/csv")
    mb.add_attachment(entry, "photo.png", FAKE_PNG)
    model = ScriptedModel(check(), decision("escalate"))
    runner, audit = build(settings, mb, model, mode="shadow")
    runner.reader = AttachmentReader(runner.store, None, "m", mode="local", agent_staff_id=AGENT_STAFF)
    runner.run()
    detail = data.decision_detail(audit.db, audit.last_for_ticket(7)["id"])
    assert [a["status"] for a in detail["attachments"]] == ["read", "not_read"]
    assert "<NUMBER_1> | <MOBILE_1>" in detail["attachments"][0]["summary"]

    mb.close = lambda: None
    monkeypatch.setattr(cli, "build_metabase", lambda s: mb)
    assert cli.main(["attachments", "--ticket", "482913", "--local"]) == 0
    out = capsys.readouterr().out
    assert "[Attachment 1: accounts.csv, text/csv" in out and "<NUMBER_1> | <MOBILE_1>" in out
    assert "12345678901" not in out and "Not read: images and PDFs are not read in ATTACHMENT_MODE=local" in out
