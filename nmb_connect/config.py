"""Settings for the NMB Connect desk, from the service's .env.

Shared names (METABASE_*, NOTIF_*, ATTACHMENT_*, ...) mean the same for every desk; prefix one with NMB_CONNECT_
to change it for this desk only (NMB_CONNECT_AGENT_MODE=live).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from dotenv import load_dotenv

from agentcore.config import Env

BASE_DIR = Path(__file__).resolve().parent.parent
DESK = "nmb_connect"
PREFIX = "NMB_CONNECT"  # NMB_CONNECT_<SETTING> overrides <SETTING> for this desk
VALID_MODES = ("shadow", "notes", "live")
VALID_REPLY_SOURCES = ("model", "template")
# The help topics these tickets are filed under (the older flat ones and the NMB Connect / USSD family)
DEFAULT_TOPICS = ("NMB Connect / Registration,NMB Connect / OTP,NMB Connect / Account,NMB Connect / System Issue,"
                  "NMB Connect / USSD,USSD / Account,USSD / System Error,USSD / Pin")
# Failed transactions are the failed-transactions desk's, even under NMB Connect / USSD
DEFAULT_EXCLUDE = "NMB Connect / USSD / Failed Transactions"


@dataclass(frozen=True)
class Settings:
    # Model
    anthropic_api_key: Optional[str]
    model: str
    max_agent_turns: int
    # Behaviour
    mode: str
    confidence_threshold: float
    max_tickets_per_run: int
    max_closes_per_run: int
    allow_request_info: bool
    reply_source: str
    max_info_requests: int
    recheck_after_hours: float
    model_fallback: str
    request_info_timeout_hours: float
    kill_switch_file: Path
    mode_override_file: Path
    prompt_file: Path
    state_db: Path
    # osTicket writes (as the Java: --reply-staff-id / --close-status-id)
    reply_staff_id: Optional[int]
    close_status_id: int
    # Metabase
    metabase_host: str
    metabase_api_key: Optional[str]
    metabase_timeout: float
    metabase_retry_count: int
    metabase_retry_delay: float
    metabase_verify: Union[bool, str]
    # Databases (Metabase names)
    ticket_db_name: str
    reg_db_name: str               # CONECT 2.0 REG: the NMB Connect profiles
    conect_db_name: str            # CONECT 2.0: app and USSD activity
    standby_db_name: str           # core banking standby
    reg_failures_db_name: str      # the registration log analyzer's database (optional)
    socket_db_name: str            # the SMS gateway (optional)
    # The profile tables (CONECT 2.0 REG)
    reg_customer_table: str
    reg_channels_table: str
    reg_history_table: str
    # Which tickets, and how far the lookups reach
    topics: list
    exclude_topics: list
    activity_days: int
    sms_days: int
    # Portal
    portal_host: str = "127.0.0.1"
    portal_port: int = 8085
    portal_users_file: Path = BASE_DIR / "state/portal_users.json"
    portal_stale_minutes: int = 40
    osticket_ticket_url: Optional[str] = None
    price_input_per_mtok: Optional[float] = None
    price_output_per_mtok: Optional[float] = None
    # Branch notifications
    notify_transport: str = "off"
    notify_on: frozenset = frozenset({"request_info", "reply_and_close"})
    notify_cc: tuple = ()
    notify_signature: str = "ICT Service Desk"
    notify_reply_to: Optional[str] = None
    notify_max_attempts: int = 5
    osticket_client_url: Optional[str] = None
    template_dir: Path = BASE_DIR / "nmb_connect/templates"
    notif_email_url: Optional[str] = None
    notif_email_access_token: Optional[str] = None
    notif_gateway_basic_auth: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: int = 25
    smtp_from: Optional[str] = None
    smtp_from_name: Optional[str] = None
    smtp_starttls: bool = True
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    # Admin alerts (same gateway as the Java AdminNotifGateway)
    notif_sms_url: Optional[str] = None
    notif_sms_access_token: Optional[str] = None
    notif_sms_originator: str = "NMB Bank"
    admin_sms_recipients: tuple = ()
    admin_email_recipients: tuple = ()
    portal_url: Optional[str] = None
    # Anomaly watch
    anomaly_enabled: bool = False  # no anomaly watch for this desk
    anomaly_interval_minutes: int = 60
    anomaly_window_hours: int = 24
    anomaly_baseline_days: int = 14
    anomaly_min_tickets: int = 5
    anomaly_min_attempts: int = 30
    anomaly_sample_size: int = 400
    anomaly_alert_level: str = "incident"
    anomaly_realert_hours: float = 4.0
    anomaly_model: Optional[str] = None
    anomaly_prompt_file: Path = BASE_DIR / "nmb_connect/prompts/anomaly_prompt.md"
    # ticket attachments
    attachment_mode: str = "model"  # off | local | model
    attachment_model: Optional[str] = None
    attachment_max_bytes: int = 10_000_000
    attachment_max_files: int = 6
    attachment_min_inline_bytes: int = 10_000
    attachment_max_text_chars: int = 2500

    # Investigation and approval
    approval_mode: str = "unscripted"  # off | unscripted | all
    pending_changes_sql: Optional[str] = None

    @classmethod
    def from_env(cls, env_file: Union[str, Path, None] = None, prefix: Optional[str] = PREFIX) -> "Settings":
        load_dotenv(env_file or BASE_DIR / ".env", override=False)
        env = Env(prefix, BASE_DIR)
        _str, _int, _float, _bool, _names, _path = env.str, env.int, env.float, env.bool, env.names, env.path
        _list = lambda name, default: list(_names(name, default))  # noqa: E731
        ca_bundle = _str("METABASE_CA_BUNDLE")
        return cls(
            anthropic_api_key=_str("ANTHROPIC_API_KEY"),
            model=_str("AGENT_MODEL", "claude-sonnet-5-5") or "claude-sonnet-5-5",
            max_agent_turns=_int("AGENT_MAX_TURNS", 10) or 10,
            mode=(_str("AGENT_MODE", "shadow") or "shadow").lower(),
            confidence_threshold=_float("CONFIDENCE_THRESHOLD", 0.85),
            max_tickets_per_run=_int("MAX_TICKETS_PER_RUN", 25) or 25,
            max_closes_per_run=_int("MAX_CLOSES_PER_RUN", 20) or 0,
            allow_request_info=_bool("ALLOW_REQUEST_INFO", True),
            reply_source=(_str("REPLY_SOURCE", "model") or "model").lower(),
            max_info_requests=_int("MAX_INFO_REQUESTS", 2) or 0,
            recheck_after_hours=_float("RECHECK_AFTER_HOURS", 24.0),
            model_fallback=(_str("MODEL_FALLBACK", "rules") or "rules").lower(),
            request_info_timeout_hours=_float("REQUEST_INFO_TIMEOUT_HOURS", 72.0),
            kill_switch_file=_path("KILL_SWITCH_FILE", "state/nmb_connect/STOP"),
            mode_override_file=_path("MODE_OVERRIDE_FILE", "state/nmb_connect/MODE_OVERRIDE"),
            prompt_file=_path("PROMPT_FILE", "nmb_connect/prompts/system_prompt.md"),
            state_db=_path("STATE_DB", "state/nmb_connect/agent.sqlite"),
            reply_staff_id=_int("REPLY_STAFF_ID", None),
            close_status_id=_int("CLOSE_STATUS_ID", 3) or 3,
            metabase_host=(_str("METABASE_HOST", "http://192.168.243.65:3000") or "").rstrip("/"),
            metabase_api_key=_str("METABASE_API_KEY"),
            metabase_timeout=float(_int("METABASE_TIMEOUT_SECONDS", 180) or 180),
            metabase_retry_count=max(1, _int("METABASE_QUERY_RETRY_COUNT", 3) or 1),
            metabase_retry_delay=max(0, _int("METABASE_QUERY_RETRY_DELAY_MILLIS", 5000) or 0) / 1000.0,
            metabase_verify=ca_bundle if ca_bundle else _bool("METABASE_VERIFY_TLS", True),
            ticket_db_name=_str("TICKET_DB_NAME", "OSTICKETS_NEW") or "OSTICKETS_NEW",
            reg_db_name=_str("CONECT_REG_DB_NAME", "CONECT 2.0 REG") or "CONECT 2.0 REG",
            conect_db_name=_str("CONECT_DB_NAME", "CONECT 2.0") or "CONECT 2.0",
            standby_db_name=_str("STANDBY_DB_NAME", "STANDBY DB") or "STANDBY DB",
            reg_failures_db_name=_str("REG_FAILURES_DB_NAME", "") or "",
            socket_db_name=_str("SOCKET_SMS_DB_NAME", "SOCKET SMS DB") or "SOCKET SMS DB",
            reg_customer_table=_str("NMB_CONNECT_PROFILE_TABLE", "customer") or "customer",
            reg_channels_table=_str("NMB_CONNECT_CHANNELS_TABLE", "user_channels") or "",
            reg_history_table=_str("NMB_CONNECT_HISTORY_TABLE", "history") or "",
            topics=_list("NMB_CONNECT_HELP_TOPICS", DEFAULT_TOPICS),
            exclude_topics=_list("NMB_CONNECT_EXCLUDE_TOPICS", DEFAULT_EXCLUDE),
            activity_days=max(1, min(_int("NMB_CONNECT_ACTIVITY_DAYS", 14) or 14, 60)),
            sms_days=max(1, min(_int("NMB_CONNECT_SMS_DAYS", 7) or 7, 30)),
            portal_host=_str("PORTAL_HOST", "127.0.0.1") or "127.0.0.1",
            portal_port=_int("PORTAL_PORT", 8085) or 8085,
            portal_users_file=_path("PORTAL_USERS_FILE", "state/portal_users.json"),
            portal_stale_minutes=_int("PORTAL_STALE_MINUTES", 40) or 40,
            osticket_ticket_url=_str("OSTICKET_TICKET_URL"),
            price_input_per_mtok=_float("PRICE_INPUT_PER_MTOK", 0.0) or None,
            price_output_per_mtok=_float("PRICE_OUTPUT_PER_MTOK", 0.0) or None,
            notify_transport=(_str("NOTIFY_TRANSPORT") or ("gateway" if _str("NOTIF_EMAIL_URL") else "off")).lower(),
            notify_on=frozenset(x.strip() for x in (_str("NOTIFY_ON", "request_info,reply_and_close") or "").split(",")
                                if x.strip()),
            notify_cc=tuple(x.strip() for x in (_str("NOTIFY_CC", "") or "").split(",") if x.strip()),
            notify_signature=(_str("NOTIFY_SIGNATURE", "ICT Service Desk") or "ICT Service Desk").replace("\\n", "\n"),
            notify_reply_to=_str("NOTIFY_REPLY_TO"),
            notify_max_attempts=max(1, _int("NOTIFY_MAX_ATTEMPTS", 5) or 5),
            osticket_client_url=_str("OSTICKET_CLIENT_URL"),
            template_dir=_path("TEMPLATE_DIR", "nmb_connect/templates"),
            notif_email_url=_str("NOTIF_EMAIL_URL"),
            notif_email_access_token=_str("NOTIF_EMAIL_ACCESS_TOKEN"),
            notif_gateway_basic_auth=_str("NOTIF_GATEWAY_BASIC_AUTH"),
            smtp_host=_str("SMTP_HOST"),
            smtp_port=_int("SMTP_PORT", 25) or 25,
            smtp_from=_str("SMTP_FROM"),
            smtp_from_name=_str("SMTP_FROM_NAME"),
            smtp_starttls=_bool("SMTP_STARTTLS", True),
            smtp_username=_str("SMTP_USERNAME"),
            smtp_password=_str("SMTP_PASSWORD"),
            notif_sms_url=_str("NOTIF_SMS_URL"),
            notif_sms_access_token=_str("NOTIF_SMS_ACCESS_TOKEN"),
            notif_sms_originator=_str("NOTIF_SMS_ORIGINATOR", "NMB Bank") or "NMB Bank",
            admin_sms_recipients=tuple(x.strip() for x in (_str("ADMIN_SMS_RECIPIENTS", "") or "").split(",") if x.strip()),
            admin_email_recipients=tuple(x.strip() for x in (_str("ADMIN_EMAIL_RECIPIENTS", "") or "").split(",") if x.strip()),
            portal_url=(_str("PORTAL_URL") or "").rstrip("/") or None,
            anomaly_enabled=False,
            anomaly_interval_minutes=max(5, _int("ANOMALY_INTERVAL_MINUTES", 60) or 60),
            anomaly_window_hours=max(1, _int("ANOMALY_WINDOW_HOURS", 24) or 24),
            anomaly_baseline_days=max(3, _int("ANOMALY_BASELINE_DAYS", 14) or 14),
            anomaly_min_tickets=max(1, _int("ANOMALY_MIN_TICKETS", 5) or 5),
            anomaly_min_attempts=max(5, _int("ANOMALY_MIN_ATTEMPTS", 30) or 30),
            anomaly_sample_size=max(50, _int("ANOMALY_SAMPLE_SIZE", 400) or 400),
            anomaly_alert_level=(_str("ANOMALY_ALERT_LEVEL", "incident") or "incident").lower(),
            anomaly_realert_hours=_float("ANOMALY_REALERT_HOURS", 4.0),
            anomaly_model=_str("ANOMALY_MODEL"),
            anomaly_prompt_file=_path("ANOMALY_PROMPT_FILE", "nmb_connect/prompts/anomaly_prompt.md"),
            attachment_mode=(_str("ATTACHMENT_MODE", "model") or "model").lower(),
            attachment_model=_str("ATTACHMENT_MODEL"),
            attachment_max_bytes=int(_float("ATTACHMENT_MAX_MB", 10.0) * 1_000_000),
            attachment_max_files=max(0, _int("ATTACHMENT_MAX_FILES", 6) or 0),
            attachment_min_inline_bytes=int(_float("ATTACHMENT_MIN_INLINE_KB", 10.0) * 1000),
            attachment_max_text_chars=max(300, _int("ATTACHMENT_MAX_TEXT_CHARS", 2500) or 2500),
            approval_mode=(_str("APPROVAL_MODE", "unscripted") or "unscripted").lower(),
            pending_changes_sql=_str("PENDING_CHANGES_SQL"),
        )

    def problems(self, mode: Optional[str] = None, need_model: bool = True) -> list[str]:
        mode = mode or self.mode
        errors: list[str] = []
        if mode not in VALID_MODES:
            errors.append(f"AGENT_MODE must be one of {VALID_MODES}, got {mode!r}")
        if self.approval_mode not in ("off", "unscripted", "all"):
            errors.append(f"APPROVAL_MODE must be off, unscripted or all, got {self.approval_mode!r}")
        if self.pending_changes_sql and not re.search(r"\{(account|internal|client)\}", self.pending_changes_sql):
            errors.append("PENDING_CHANGES_SQL must use {account}, {internal} or {client} to select the customer")
        if self.pending_changes_sql and not self.pending_changes_sql.lstrip().upper().startswith(("SELECT", "WITH")):
            errors.append("PENDING_CHANGES_SQL must be a SELECT")
        if self.reply_source not in VALID_REPLY_SOURCES:
            errors.append(f"REPLY_SOURCE must be one of {VALID_REPLY_SOURCES}, got {self.reply_source!r}")
        if self.model_fallback not in ("rules", "none"):
            errors.append(f"MODEL_FALLBACK must be rules or none, got {self.model_fallback!r}")
        if mode in ("notes", "live") and not self.reply_staff_id:
            errors.append(f"REPLY_STAFF_ID is required for AGENT_MODE={mode} (the staff account the agent posts as)")
        if need_model and not self.anthropic_api_key:
            errors.append("ANTHROPIC_API_KEY is not set")
        if not self.metabase_host:
            errors.append("METABASE_HOST is not set")
        if not self.metabase_api_key:
            errors.append("METABASE_API_KEY is not set")
        if not 0 < self.confidence_threshold <= 1:
            errors.append("CONFIDENCE_THRESHOLD must be between 0 and 1")
        if not self.topics:
            errors.append("NMB_CONNECT_HELP_TOPICS is empty")
        for name, value in (("NMB_CONNECT_PROFILE_TABLE", self.reg_customer_table),
                            ("NMB_CONNECT_CHANNELS_TABLE", self.reg_channels_table),
                            ("NMB_CONNECT_HISTORY_TABLE", self.reg_history_table)):
            if value and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?", value):
                errors.append(f"{name} must be a table name, got {value!r}")
        if self.notify_transport not in ("off", "gateway", "smtp"):
            errors.append(f"NOTIFY_TRANSPORT must be off, gateway or smtp, got {self.notify_transport!r}")
        if self.notify_transport == "gateway" and not self.notif_email_url:
            errors.append("NOTIFY_TRANSPORT=gateway needs NOTIF_EMAIL_URL")
        if self.notify_transport == "smtp" and not (self.smtp_host and self.smtp_from):
            errors.append("NOTIFY_TRANSPORT=smtp needs SMTP_HOST and SMTP_FROM")
        if self.attachment_mode not in ("off", "local", "model"):
            errors.append(f"ATTACHMENT_MODE must be off, local or model, got {self.attachment_mode!r}")
        if self.attachment_max_bytes > 30_000_000:
            errors.append("ATTACHMENT_MAX_MB can be at most 30 (the model's request limit)")
        if self.anomaly_alert_level not in ("watch", "incident"):
            errors.append("ANOMALY_ALERT_LEVEL must be watch or incident")
        if not self.prompt_file.exists():
            errors.append(f"Prompt file not found: {self.prompt_file}")
        return errors


    def warnings(self, mode: Optional[str] = None) -> list[str]:
        mode = mode or self.mode
        notes: list[str] = []
        if mode == "live" and self.notify_transport == "off" and self.allow_request_info:
            notes.append("Branch notifications are off: when the agent asks a branch for information, nobody is "
                         "emailed (osTicket does not send its own alert for replies written by the agent). "
                         "Set NOTIF_EMAIL_URL (gateway) or NOTIFY_TRANSPORT=smtp.")
        if self.attachment_mode == "model":
            try:
                import PIL  # noqa: F401
            except ImportError:
                notes.append("Pillow is not installed: large photos and BMP/TIFF images attached to tickets cannot be "
                             "shrunk or converted, so they are not read. pip install Pillow")
        return notes


def owned_topics(s: Settings) -> tuple[list, list]:
    """(topics this desk works, subtopics it leaves alone): the router moves tickets here on its own."""
    return list(s.topics), list(s.exclude_topics)
