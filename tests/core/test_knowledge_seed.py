"""knowledge-seed: a desk's built-in staff notes are proposed once, for an Admin to switch on; titles already in the
console (in any state) are left alone."""
from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

from agentcore.audit import AuditLog
from agentcore.knowledge import KnowledgeStore
from failed_txn.__main__ import CLI as _CLI


def seed(tmp_path, **kw):
    args = argparse.Namespace(by="built-in (from the code)", file=None, dry_run=False, **kw)
    return _CLI.cmd_knowledge_seed(SimpleNamespace(state_db=tmp_path / "agent.sqlite"), args)


def test_the_built_in_notes_are_proposed_once_and_wait_for_an_admin(tmp_path, capsys):
    shipped = json.loads((_CLI.base_dir / "failed_txn" / "knowledge_seed.json").read_text(encoding="utf-8"))["notes"]
    assert seed(tmp_path) == 0
    audit = AuditLog(tmp_path / "agent.sqlite")
    store = KnowledgeStore(audit.db)
    rows = store.listing()
    assert len(rows) == len(shipped) and {r["status"] for r in rows} == {"proposed"}
    assert store.active() == []  # nothing is used until an Admin switches it on
    first = rows[-1]["id"]
    store.decide(first, "reject", "admin1", "admin", "not ours")
    audit.close()
    capsys.readouterr()
    assert seed(tmp_path) == 0
    out = capsys.readouterr().out
    assert "proposed K" not in out and "already there (rejected)" in out
    audit = AuditLog(tmp_path / "agent.sqlite")
    assert len(KnowledgeStore(audit.db).listing()) == len(shipped)
    audit.close()


def test_every_shipped_note_is_valid_and_holds_no_long_numbers():
    from agentcore.knowledge import Draft
    import re
    for desk in ("failed_txn", "nmb_connect", "sms_alerts"):
        notes = json.loads((_CLI.base_dir / desk / "knowledge_seed.json").read_text(encoding="utf-8"))["notes"]
        for note in notes:
            draft = Draft.from_body(note)
            assert draft.problems() == [], draft.title
            assert not re.search(r"\d{8,}", draft.body), draft.title


def test_each_converted_desk_proposes_its_own_notes(tmp_path):
    import importlib
    for desk in ("nmb_connect", "sms_alerts"):
        cli = importlib.import_module(f"{desk}.__main__").CLI
        args = argparse.Namespace(by="built-in (from the code)", file=None, dry_run=False)
        assert cli.cmd_knowledge_seed(SimpleNamespace(state_db=tmp_path / f"{desk}.sqlite"), args) == 0
        audit = AuditLog(tmp_path / f"{desk}.sqlite")
        shipped = json.loads((cli.base_dir / desk / "knowledge_seed.json").read_text(encoding="utf-8"))["notes"]
        assert len(KnowledgeStore(audit.db).listing()) == len(shipped) > 0
        audit.close()
