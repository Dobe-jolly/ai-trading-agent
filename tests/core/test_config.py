"""One .env for every desk: a desk's own prefixed setting wins over the shared one."""
from __future__ import annotations

import pytest

from agentcore.config import Env
from ecocash.config import Settings as EcoSettings
from sms_alerts.config import Settings as SmsSettings


def test_prefixed_setting_wins_for_that_desk_only(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_MODE", "shadow")
    monkeypatch.setenv("ECOCASH_AGENT_MODE", "live")
    monkeypatch.setenv("MAX_TICKETS_PER_RUN", "7")
    eco = EcoSettings.from_env(env_file=tmp_path / "none.env")
    sms = SmsSettings.from_env(env_file=tmp_path / "none.env")
    assert (eco.mode, sms.mode) == ("live", "shadow")
    assert eco.max_tickets_per_run == sms.max_tickets_per_run == 7


def test_each_desk_keeps_its_own_state_and_shares_the_console_users(monkeypatch, tmp_path):
    for name in ("STATE_DB", "KILL_SWITCH_FILE", "PORTAL_USERS_FILE"):
        monkeypatch.delenv(name, raising=False)
    eco = EcoSettings.from_env(env_file=tmp_path / "none.env")
    sms = SmsSettings.from_env(env_file=tmp_path / "none.env")
    assert eco.state_db != sms.state_db and eco.kill_switch_file != sms.kill_switch_file
    assert eco.portal_users_file == sms.portal_users_file
    assert eco.prompt_file.exists() and sms.prompt_file.exists()


def test_errors_name_the_variable_that_is_set(monkeypatch):
    monkeypatch.setenv("SMS_ALERTS_MAX_TICKETS_PER_RUN", "many")
    with pytest.raises(ValueError, match="SMS_ALERTS_MAX_TICKETS_PER_RUN"):
        Env("SMS_ALERTS").int("MAX_TICKETS_PER_RUN", 25)
    assert Env("SMS_ALERTS").names("NOPE_TOPICS", "A, B / C") == ("A", "B / C")
