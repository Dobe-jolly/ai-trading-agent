"""The lookups Claude can call on an NMB Connect ticket, with masking, and the evidence they leave for the rules in
code.

Claude looks up the client's NMB Connect profile and core banking record (and, when useful, their recent app activity,
failed registrations and the SMS sent to their number), then names the problem with assess_access_issue. That
assessment is worked out here, from what the lookups returned: where the client stands and the standard outcome
and wording, taken from how the service desk resolves these tickets. The guardrails only let a ticket close on an
assessment whose outcome is final.

Every CIF, account, mobile or ID the model passes must be on the ticket (form, thread, files read) or be the
client's own from an earlier result (the CIF of the client's account, the client's other accounts, the number on
the client's profile), so it cannot go looking at other customers.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

from agentcore import sql
from agentcore.metabase import MetabaseError
from agentcore.pii import Pseudonymizer
from .normalize import account_key, cif_key, day_text, format_timestamp, id_key, mobile_key, today_local
from .osticket import Details, Ticket, find_details
from .sources import (
    INACTIVE_STATES,
    AppActivity,
    CoreAccount,
    CoreBanking,
    Profile,
    Registration,
    RegistrationFailures,
    SmsGateway,
    otp_failing,
    summarise_activity,
    summarise_sms,
    window_start,
)

log = logging.getLogger(__name__)
MAX_RESULT_CHARS = 9000

PROBLEMS = {
    "otp_not_received": "the client gets no OTP (app, USSD or online)",
    "forgot_username": "the client forgot the NMB Connect username",
    "cannot_register": "registration fails, e.g. 'already registered, kindly log in', or an error while registering",
    "cannot_login": "the client is registered but cannot log in (wrong password or username, locked, blocked)",
    "accounts_not_visible": "an account does not show on NMB Connect ('no linked account', cannot add the ZiG account)",
    "number_changed": "the client changed mobile number and NMB Connect still has the old one ('mobile number does "
                      "not match')",
    "account_inactive": "the app fails because the account is dormant, closed or frozen",
    "block_service": "the branch wants the client's mobile banking or EcoCash access blocked",
    "other": "anything else: app errors on transfers, corporate authorisers, card or biller issues, ...",
}
PROFILE_PROBLEMS = {"otp_not_received", "forgot_username", "cannot_register", "cannot_login", "accounts_not_visible",
                    "number_changed"}
ACCOUNT_PROBLEMS = {"accounts_not_visible", "account_inactive"}

# Where the client stands. Only these close a ticket; keep-open ones ask the branch to act and are checked again.
CLOSABLE = {"not_registered", "registered", "reregistered", "account_inactive", "registration_failed_email",
            "registration_failed_temporary", "mobile_updated", "how_to"}
KEEP_OPEN = {"mobile_mismatch", "already_registered", "account_active", "needs_details", "no_attempt_found",
             "account_not_found", "registered_unverified"}
DELETE_OK = {"already_registered", "account_active", "locked", "registered_cannot_login"}
STATUS_TEXT = {
    "not_registered": "not registered on NMB Connect",
    "registered": "registered with the mobile on the ticket",
    "reregistered": "registered again since the ticket was raised",
    "mobile_mismatch": "registered with a different mobile number",
    "mobile_updated": "the profile already has the new number",
    "already_registered": "already has a profile (registration says 'already registered')",
    "registered_unverified": "has a profile; the ticket gives no mobile to compare",
    "registered_cannot_login": "registered with the right number, profile not marked locked",
    "locked": "profile locked or blocked",
    "duplicate_profiles": "more than one profile for the CIF",
    "number_on_other_profile": "the mobile is on another customer's profile",
    "account_active": "the account is active under the client's CIF",
    "account_inactive": "the account is dormant, closed or frozen",
    "account_other_cif": "the account is under another CIF",
    "account_not_found": "the account was not found in core banking",
    "registration_failed_email": "registration failed: e-mail or password did not match",
    "registration_failed_temporary": "registration failed: temporary system error",
    "registration_failed_duplicate": "registration failed: duplicate customer profile",
    "registration_failed_unknown": "registration failed: unknown error",
    "no_attempt_found": "no profile and no failed registration recorded",
    "otp_not_delivered": "OTPs to the number are failing at the SMS gateway",
    "needs_details": "no CIF or account to check",
    "how_to": "a branch procedure",
    "other": "needs a person",
}

CIF_PROP = {"type": "string", "description": "The client's CIF (customer number), from the ticket or an earlier result."}
ACCOUNT_PROP = {"type": "string", "description": "The client's account number, e.g. <NUMBER_1>."}
MOBILE_PROP = {"type": "string", "description": "The client's mobile number, e.g. <MOBILE_1>."}
PROFILE_TOOL = {
    "name": "get_connect_profile",
    "description": (
        "The client's NMB Connect profile (CONECT 2.0 REG): whether one exists for the CIF, how many, the mobile the "
        "OTPs go to and whether it matches the ticket, when it was created, and its state (active, locked); also any "
        "profile that holds the ticket's mobile under another CIF, and the channels and recent logins when recorded. "
        "Give the CIF, or an account (its CIF is found in core banking), and the mobile from the ticket."),
    "input_schema": {"type": "object", "properties": {"cif": CIF_PROP, "account_number": ACCOUNT_PROP,
                                                       "mobile": MOBILE_PROP}},
}
CORE_TOOL = {
    "name": "check_core_banking",
    "description": (
        "Core banking (standby copy): for an account, whether it exists, its CIF, currency and state (active, dormant, "
        "closed, frozen); for the CIF, all the client's accounts with their state, and the mobile and e-mail on the "
        "customer record (and the mobile on IDCCOB), compared with the ticket and the NMB Connect profile."),
    "input_schema": {"type": "object", "properties": {"account_number": ACCOUNT_PROP, "cif": CIF_PROP}},
}
ACTIVITY_TOOL = {
    "name": "get_app_activity",
    "description": (
        "What the client did on NMB Connect (app, USSD, online) lately, from CONECT 2.0: requests by channel and "
        "status, the last success and the last failure with its message. A recent success (e.g. a balance enquiry) "
        "shows they could log in."),
    "input_schema": {"type": "object", "properties": {"cif": CIF_PROP, "mobile": MOBILE_PROP,
                                                       "days": {"type": "integer", "minimum": 1, "maximum": 60}}},
}
FAILURES_TOOL = {
    "name": "find_registration_failures",
    "description": (
        "Failed NMB Connect registrations the registration log analyzer recorded for the mobile or national ID, newest "
        "first, each with a reason: DUPLICATE_PROFILE, CREDENTIAL_VALIDATION_FAILED (e-mail or password did not "
        "match), DB_QUERY_ERROR or UPSTREAM_TIMEOUT (temporary), UNKNOWN."),
    "input_schema": {"type": "object", "properties": {"mobile": MOBILE_PROP, "id_number": {
        "type": "string", "description": "National ID from the ticket, e.g. <NATIONAL_ID_1>."}}},
}
SMS_TOOL = {
    "name": "get_sms_to_number",
    "description": (
        "SMS the bank's gateway sent to a mobile lately and whether each was delivered, by kind (otp, username, alert, "
        "other). Never the text. If no OTP shows, that alone does not prove none was sent."),
    "input_schema": {"type": "object", "properties": {"mobile": MOBILE_PROP, "days": {
        "type": "integer", "minimum": 1, "maximum": 30}}, "required": ["mobile"]},
}
ASSESS_TOOL = {
    "name": "assess_access_issue",
    "description": (
        "Name the client's problem once you have looked them up. It works out where the client stands from what the "
        "lookups returned, and gives the standard outcome and wording for the branch. A ticket can only be closed on "
        "an assessment. Problems: " + "; ".join(f"{k}: {v}" for k, v in PROBLEMS.items()) + "."),
    "input_schema": {"type": "object", "properties": {
        "problem": {"type": "string", "enum": list(PROBLEMS)},
        "account_number": {"type": "string", "description": "For accounts_not_visible and account_inactive: the "
                                                            "account concerned, e.g. <NUMBER_1>."},
        "mobile": {"type": "string", "description": "For number_changed: the number the client uses now."}},
        "required": ["problem"]},
}


@dataclass
class Assessment:
    problem: str
    status: str
    action: str                   # reply_and_close | request_info | escalate
    reply: str                    # the standard reply to the branch (real values); empty for escalate
    next_step: str = ""           # for the person, when it needs one
    cif: Optional[str] = None
    account: Optional[str] = None
    facts: dict = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        out = {"problem": self.problem, "where_the_client_stands": self.status,
               "meaning": STATUS_TEXT.get(self.status, self.status),
               "standard_outcome": {"action": self.action, "reply": self.reply or None}}
        if self.action == "request_info" and self.status in KEEP_OPEN:
            out["note"] = "Keeps the ticket open: the branch acts and replies, and the client is checked again."
        if self.action == "escalate":
            out["needs_a_person"] = True
            if self.next_step:
                out["for_the_person"] = self.next_step
        if self.facts:
            out["facts"] = self.facts
        return out


@dataclass
class Evidence:
    """What the lookups found, for the guardrails and the log."""
    known: Details = field(default_factory=Details)        # what the model may search with
    ticket_mobiles: set = field(default_factory=set)        # the mobiles the ticket owner gave (compared with profiles)
    ticket_cifs: set = field(default_factory=set)           # the CIFs written on the ticket
    client_cifs: set = field(default_factory=set)           # CIFs that are the client's (ticket, or the client's account)
    profiles: dict = field(default_factory=dict)            # ref -> Profile
    profile_lookups: list = field(default_factory=list)     # {"by", "value", "found"}
    customers: dict = field(default_factory=dict)           # cif -> core banking customer
    accounts: dict = field(default_factory=dict)            # account_key -> CoreAccount
    activity: Optional[dict] = None
    failures: Optional[list] = None
    sms: Optional[dict] = None
    pending: Optional[list] = None
    assessment: Optional[Assessment] = None
    searches: list = field(default_factory=list)
    unavailable: list = field(default_factory=list)
    check_errors: list = field(default_factory=list)
    tools_used: list = field(default_factory=list)
    refused: list = field(default_factory=list)

    @property
    def status(self) -> str:
        return self.assessment.status if self.assessment else "none"

    @property
    def statuses(self) -> dict[str, str]:
        return {"connect": self.assessment.status} if self.assessment else {}

    def summary(self) -> dict[str, Any]:
        return {"status": self.status, "profiles": len(self.profiles), "problem": getattr(self.assessment, "problem", None),
                "errors": self.check_errors[:3]}

    def client_profiles(self) -> list[Profile]:
        """Active profiles under the client's CIF(s)."""
        return [p for p in self.profiles.values() if p.active and p.cif in self.client_cifs]

    def other_profiles(self, mobiles: set) -> list[Profile]:
        """Active profiles holding one of these mobiles under a CIF that is not the client's."""
        return [p for p in self.profiles.values() if p.active and p.mobile9 in mobiles and p.cif not in self.client_cifs]


# ---- the standard outcomes -------------------------------------------------------------------------------------
FORMATS = "the mobile can be captured as 07…, 7… or 263…"
HANDSET_STEPS = ("Kindly ask the client to check the blocked or spam SMS folder (Android Messages: Spam & blocked; "
                 "Samsung Messages: Settings, Block numbers and messages, Blocked messages; iPhone: Settings, Messages, "
                 "turn off Filter Unknown Senders), unblock the bank's sender, restart the phone and request a new OTP.")
WHY = {"otp_not_received": ", so no OTP can be sent", "forgot_username": ", so Forgot Username has nothing to match",
       "cannot_login": ", so there is nothing to log in to", "accounts_not_visible": "",
       "number_changed": "", "cannot_register": ""}
EFFECT = {"dormant": "dormant", "inoperative": "inoperative", "closed": "closed", "frozen": "frozen"}


def _mobile_hint(customer: Optional[dict[str, Any]], ticket_mobile: Optional[str], profile_mobile: Optional[str]) -> str:
    on_file = mobile_key((customer or {}).get("mobile_on_file"))
    if not on_file:
        return ""
    if ticket_mobile and on_file == mobile_key(ticket_mobile):
        return f" (core banking already shows {ticket_mobile})"
    if profile_mobile and on_file == mobile_key(profile_mobile):
        return f" (core banking still shows {profile_mobile})"
    return ""


def assess(problem: str, ticket: Ticket, ev: Evidence, account: Optional[str] = None,
           mobile_now: Optional[str] = None) -> Assessment:
    """Where the client stands for this problem, from the evidence only, with the standard outcome."""
    if problem not in PROBLEMS:
        raise ValueError(f"problem must be one of {', '.join(PROBLEMS)}")
    if problem == "block_service":
        return Assessment(problem, "how_to", "reply_and_close", (
            "Mobile banking and EcoCash access can be blocked by the branch on the dashboard, with the block option "
            "on the client's profile. If the dashboard gives an error, reply here with a screenshot and we will block "
            "it."))
    if problem == "other":
        return Assessment(problem, "other", "escalate", "", "Read the ticket and the findings above; this is not a "
                          "registration, login, OTP or account-visibility problem the agent resolves.")
    owner = ticket.owner_details()
    ticket_mobiles = set(owner.mobiles)
    cif = next(iter(sorted(ev.client_cifs)), None)

    if problem in ACCOUNT_PROBLEMS:
        return _assess_account(problem, ev, account, ticket.opened, cif)

    if not ev.profile_lookups:
        raise ValueError("look the client up first with get_connect_profile")
    if cif is None:
        return Assessment(problem, "needs_details", "request_info", (
            "To check the client's NMB Connect profile we need the client's CIF or account number"
            + ("" if ticket_mobiles else " and the mobile number they use") + ". Kindly reply with it, and with a "
            "screenshot of the error if there is one."))
    mine = ev.client_profiles()
    others = ev.other_profiles(ticket_mobiles)
    if len(mine) > 1:
        return Assessment(problem, "duplicate_profiles", "escalate", "", (
            f"ICT Data Science: the client has {len(mine)} active NMB Connect profiles for CIF {cif}; remove the "
            "duplicate so that registration and login work, then advise the branch."), cif=cif,
            facts={"profiles": [p.ref for p in mine]})
    if others:
        other = others[0]
        return Assessment(problem, "number_on_other_profile", "escalate", "", (
            f"The mobile on the ticket, {other.mobile}, is on an NMB Connect profile under CIF {other.cif}, not the "
            f"client's CIF {cif}. Check whether it is the client's old profile before deleting it; if it belongs to "
            "someone else, the client must register with another number."), cif=cif, facts={"profile": other.ref})
    profile = mine[0] if mine else None
    opened = ticket.opened

    if profile is None:
        if problem == "cannot_register":
            return _assess_failed_registration(problem, ev, cif, owner)
        reply = (f"The client is not registered on NMB Connect: there is no profile for CIF {cif}{WHY.get(problem, '')}."
                 f" Kindly register the client ({FORMATS}), then ask them to log in.")
        if problem == "number_changed":
            reply = (f"There is no NMB Connect profile for CIF {cif}, so there is no old number to change. Kindly "
                     f"register the client with the new number ({FORMATS}).")
        return Assessment(problem, "not_registered", "reply_and_close", reply, cif=cif)

    created_after = bool(profile.created and opened and profile.created > opened)
    if created_after and problem in ("cannot_register", "cannot_login", "accounts_not_visible"):
        return Assessment(problem, "reregistered", "reply_and_close", (
            f"The client's NMB Connect profile was created again on {day_text(profile.created)} with mobile "
            f"{profile.mobile}. The client can now log in; if anything still fails, reply on this ticket with a "
            "screenshot of the error."), cif=cif, facts={"profile": profile.ref})
    if problem == "cannot_register":
        return Assessment(problem, "already_registered", "request_info", (
            f"The client already has an NMB Connect profile (created {day_text(profile.created)}, mobile "
            f"{profile.mobile}), which is why registration says the client is already registered. Kindly delete the "
            f"profile on the dashboard and have the client register again ({FORMATS}). If the delete fails, reply here "
            "with a screenshot of the error."), cif=cif, facts={"profile": profile.ref})

    customer = ev.customers.get(cif)
    if problem == "number_changed":
        now = mobile_key(mobile_now) if mobile_now else None
        wanted = {now} if now else ticket_mobiles
        if profile.mobile9 and profile.mobile9 in wanted and (now or len(ticket_mobiles) == 1):
            return Assessment(problem, "mobile_updated", "reply_and_close", (
                f"The client's NMB Connect profile already has the new number, {profile.mobile}. The client can log "
                "in and request the OTP on that number; if it still fails, reply on this ticket with a screenshot."),
                cif=cif, facts={"profile": profile.ref})
        new = mobile_now or next((f"0{m}" for m in sorted(ticket_mobiles) if m != profile.mobile9), None)
        return _mismatch(problem, profile, new, customer, cif)

    if profile.mobile9 is not None and ticket_mobiles and profile.mobile9 not in ticket_mobiles:
        return _mismatch(problem, profile, f"0{sorted(ticket_mobiles)[0]}", customer, cif)
    if not ticket_mobiles:
        return Assessment(problem, "registered_unverified", "request_info", (
            "The client has an NMB Connect profile. Kindly confirm the mobile number the client is using, so that we "
            "can check it against the profile."), cif=cif, facts={"profile": profile.ref})
    if profile.locked:
        return Assessment(problem, "locked", "escalate", "", (
            f"The client's NMB Connect profile is locked or blocked ({profile.state}). Unlock it on the dashboard, or "
            "delete it and have the client register again, then advise the branch."), cif=cif,
            facts={"profile": profile.ref})
    if problem == "otp_not_received":
        if otp_failing(ev.sms):
            return Assessment(problem, "otp_not_delivered", "escalate", "", (
                f"ICT Data Science: OTPs to {profile.mobile} are failing at the SMS gateway "
                f"({(ev.sms or {}).get('otp_by_status')}); check the gateway and the network, then advise the branch."),
                cif=cif, facts={"profile": profile.ref})
        last = (ev.sms or {}).get("last_otp") or {}
        sent = f": the last OTP was delivered on {last['when']}" if last.get("status") == "delivered" else ""
        return Assessment(problem, "registered", "reply_and_close", (
            f"The client is registered on NMB Connect with {profile.mobile}, the number on the ticket, so the OTP goes "
            f"to the right number{sent}. {HANDSET_STEPS} If it still does not arrive, reply on this ticket and we "
            "will check the SMS gateway."), cif=cif, facts={"profile": profile.ref})
    if problem == "forgot_username":
        return Assessment(problem, "registered", "reply_and_close", (
            f"The client is registered on NMB Connect with {profile.mobile}, the number on the ticket. On the login "
            "screen they can tap Forgot Username, enter the account number and this mobile number, submit, and enter "
            "the OTP that follows: the username is then sent by SMS and e-mail. If the OTP does not arrive, ask them to "
            "check the blocked or spam SMS folder, then reply on this ticket."), cif=cif, facts={"profile": profile.ref})
    # cannot_login, registered with the right number and not marked locked
    return Assessment(problem, "registered_cannot_login", "escalate", "", (
        "The client is registered with the right number and the profile is not marked locked. Check the login history "
        "and app activity above; the usual fix is to delete the profile and have the client register again."),
        cif=cif, facts={"profile": profile.ref})


def _mismatch(problem: str, profile: Profile, new: Optional[str], customer: Optional[dict[str, Any]],
              cif: str) -> Assessment:
    new_text = new or "the number the client uses now"
    hint = _mobile_hint(customer, new, profile.mobile)
    return Assessment(problem, "mobile_mismatch", "request_info", (
        f"The OTP and NMB Connect messages go to the number on the client's profile, {profile.mobile}, not {new_text}. "
        f"If the client has changed number, kindly make sure the new number is captured and authorised on the CIF in "
        f"IDCCOB{hint}, then email ICT Data Science to update the number on the NMB Connect profile, and reply here "
        f"once it is done. If {profile.mobile} is still the client's number, the OTP is sent there."), cif=cif,
        facts={"profile": profile.ref, "profile_mobile": profile.mobile, "ticket_mobile": new})


def _assess_failed_registration(problem: str, ev: Evidence, cif: str, owner: Details) -> Assessment:
    latest = (ev.failures or [None])[0]
    if latest is None:
        looked = ev.failures is not None
        return Assessment(problem, "no_attempt_found", "request_info", (
            f"There is no NMB Connect profile for CIF {cif}" + (", and no failed registration is recorded for the "
                                                                "client" if looked else "")
            + ". Kindly reply with a screenshot of the error the client gets when registering, and the mobile number "
            "they register with."), cif=cif)
    when = day_text(latest["when"])
    code = latest["reason_code"]
    if code == "CREDENTIAL_VALIDATION_FAILED":
        email = (ev.customers.get(cif) or {}).get("email_on_file")
        return Assessment(problem, "registration_failed_email", "reply_and_close", (
            f"The client's NMB Connect registration on {when} failed because the e-mail or password entered did not "
            "match the bank's records" + (f"; the e-mail on the client's bank profile is {email}" if email else "")
            + ". Kindly ask the client to register again using the e-mail on their bank profile; if that e-mail is "
            "wrong or missing, update it at the branch first."), cif=cif, facts={"failure": code})
    if code in ("DB_QUERY_ERROR", "UPSTREAM_TIMEOUT"):
        return Assessment(problem, "registration_failed_temporary", "reply_and_close", (
            f"The client's NMB Connect registration on {when} failed because of a temporary system error. Kindly ask "
            "the client to try again; if it fails again, reply on this ticket with a screenshot of the error."),
            cif=cif, facts={"failure": code})
    if code == "DUPLICATE_PROFILE":
        return Assessment(problem, "registration_failed_duplicate", "escalate", "", (
            f"ICT Data Science: the registration on {when} failed because the client has more than one customer "
            "profile (NonUniqueResultException). Merge or remove the duplicate, then ask the client to register "
            "again."), cif=cif, facts={"failure": code})
    return Assessment(problem, "registration_failed_unknown", "escalate", "", (
        f"Check the registration service log for the failure on {when}: {latest.get('message') or 'no message'}."),
        cif=cif, facts={"failure": code})


def _assess_account(problem: str, ev: Evidence, account: Optional[str], opened: Optional[datetime],
                    cif: Optional[str]) -> Assessment:
    if account is None:
        raise ValueError("give account_number: the account concerned")
    acct: Optional[CoreAccount] = ev.accounts.get(account_key(account) or "")
    if acct is None:
        raise ValueError("check the account in core banking first (check_core_banking)")
    if not acct.exists:
        return Assessment(problem, "account_not_found", "request_info", (
            f"We could not find account {acct.account} in core banking. Kindly confirm the account number"
            + (" that does not show on NMB Connect." if problem == "accounts_not_visible" else ".")),
            account=acct.account)
    if cif and acct.cif and acct.cif not in ev.client_cifs:
        return Assessment(problem, "account_other_cif", "escalate", "", (
            f"Account {acct.account} is under CIF {acct.cif}, not the client's CIF {cif}: confirm who owns it (joint or "
            "corporate account?) before it is added to the client's NMB Connect."), cif=cif, account=acct.account)
    cif = cif or acct.cif
    if acct.state in INACTIVE_STATES:
        if acct.state == "closed":
            reply = (f"Account {acct.account} is closed in core banking, so it cannot be used on NMB Connect. The "
                     "client can use another account, or open a new one at the branch.")
        else:
            fix = "have the freeze lifted" if acct.state == "frozen" else "have it reactivated"
            after = ("; once that is done, delete the client's NMB Connect profile on the dashboard and have them "
                     "register again so that it picks up the account" if problem == "accounts_not_visible" else "")
            effect = "does not show on" if problem == "accounts_not_visible" else "fails on"
            reply = (f"Account {acct.account} is {EFFECT[acct.state]} in core banking, which is why it {effect} NMB "
                     f"Connect. The client should visit the branch to {fix}{after}.")
        return Assessment(problem, "account_inactive", "reply_and_close", reply, cif=cif, account=acct.account,
                          facts={"state": acct.state, "from": acct.state_from})
    if problem == "account_inactive":
        return Assessment(problem, "account_active", "escalate", "", (
            f"Account {acct.account} is {acct.state} in core banking, so its state does not explain the failure; check "
            "the app activity above."), cif=cif, account=acct.account, facts={"state": acct.state})
    if not ev.profile_lookups:
        raise ValueError("look the client's NMB Connect profile up first with get_connect_profile")
    mine = ev.client_profiles()
    if not mine:
        return Assessment(problem, "not_registered", "reply_and_close", (
            f"The client is not registered on NMB Connect: there is no profile for CIF {cif}. Kindly register the "
            f"client ({FORMATS}); the new profile picks up all their accounts, including {acct.account}."), cif=cif,
            account=acct.account)
    if len(mine) > 1:
        return Assessment(problem, "duplicate_profiles", "escalate", "", (
            f"ICT Data Science: the client has {len(mine)} active NMB Connect profiles for CIF {cif}; remove the "
            "duplicate, then have the client register again so that all accounts show."), cif=cif, account=acct.account)
    profile = mine[0]
    if profile.created and opened and profile.created > opened:
        return Assessment(problem, "reregistered", "reply_and_close", (
            f"The client's NMB Connect profile was created again on {day_text(profile.created)}, so it now includes "
            f"the client's accounts, among them {acct.account}. Kindly ask the client to log in again; if the account "
            "still does not show, reply on this ticket with a screenshot."), cif=cif, account=acct.account,
            facts={"profile": profile.ref})
    return Assessment(problem, "account_active", "request_info", (
        f"Account {acct.account} is active under the client's CIF {cif}. NMB Connect picks up the client's accounts "
        "when the profile is created, so kindly delete the client's NMB Connect profile on the dashboard and have them "
        "register again; it will include all their accounts. If the delete fails, reply here with a screenshot of the "
        "error."), cif=cif, account=acct.account, facts={"profile": profile.ref, "state": acct.state})


# ---- the toolbox -------------------------------------------------------------------------------------------------
class Toolbox:
    def __init__(self, registration: Optional[Registration] = None, core: Optional[CoreBanking] = None,
                 activity: Optional[AppActivity] = None, failures: Optional[RegistrationFailures] = None,
                 sms: Optional[SmsGateway] = None, activity_days: int = 14, sms_days: int = 7) -> None:
        self.registration, self.core, self.activity, self.failures, self.sms = registration, core, activity, failures, sms
        self.activity_days, self.sms_days = activity_days, sms_days
        self.as_of: Optional[date] = None  # closed-ticket replays: what was known then

    def schemas(self) -> list[dict[str, Any]]:
        tools = []
        if self.registration is not None:
            tools.append(PROFILE_TOOL)
        if self.core is not None:
            tools.append(CORE_TOOL)
        if self.activity is not None:
            tools.append(ACTIVITY_TOOL)
        if self.failures is not None:
            tools.append(FAILURES_TOOL)
        if self.sms is not None:
            tools.append(SMS_TOOL)
        tools.append(ASSESS_TOOL)
        return tools

    def run(self, name: str, args: dict[str, Any], pseudo: Pseudonymizer, evidence: Evidence,
            ticket: Optional[Ticket]) -> tuple[str, bool]:
        """Returns (masked result text, is_error)."""
        if ticket is not None and not evidence.known:
            owner = ticket.owner_details()
            evidence.known.update(find_details(ticket.all_text()))
            evidence.known.update(owner)
            evidence.client_cifs |= evidence.known.cifs
            evidence.ticket_cifs = set(evidence.known.cifs)
            evidence.ticket_mobiles = set(owner.mobiles)
        try:
            values = self._inputs(pseudo.unmask_obj(args or {}), evidence, ticket is not None)
            if name == "get_connect_profile" and self.registration is not None:
                data = self._profile(values, evidence)
            elif name == "check_core_banking" and self.core is not None:
                data = self._core(values, evidence)
            elif name == "get_app_activity" and self.activity is not None:
                data = self._activity(values, evidence)
            elif name == "find_registration_failures" and self.failures is not None:
                data = self._failures(values, evidence)
            elif name == "get_sms_to_number" and self.sms is not None:
                data = self._sms(values, evidence)
            elif name == "assess_access_issue":
                if ticket is None:
                    raise ValueError("assess_access_issue needs the ticket")
                data = self._assess(values, evidence, ticket)
            else:
                return f"Unknown tool {name!r}.", True
        except (ValueError, sql.UnsafeValue) as exc:
            return f"Invalid input: {pseudo.mask(str(exc))}", True
        except MetabaseError as exc:
            evidence.check_errors.append(f"{name}: {exc}"[:300])
            return pseudo.mask(f"Lookup failed (data source error): {exc}. You may retry once.")[:800], True
        evidence.tools_used.append(name)
        text = json.dumps(pseudo.mask_obj(data), default=str, ensure_ascii=False)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + " ...[truncated]"
        return text, False

    # ---- inputs ------------------------------------------------------------------------------------
    def _inputs(self, args: dict[str, Any], evidence: Evidence, enforce: bool) -> dict[str, Any]:
        out: dict[str, Any] = {}
        checks = (("cif", cif_key, "cifs"), ("account_number", account_key, "accounts"), ("mobile", mobile_key, "mobiles"),
                  ("id_number", id_key, "ids"))
        for name, keyer, pool in checks:
            raw = str(args.get(name) or "").strip()
            if not raw:
                continue
            if raw.startswith("<") and raw.endswith(">"):
                raise ValueError(f"{name}: {raw} is not a value from this ticket")
            key = keyer(raw)
            if key is None:
                raise ValueError(f"{name} is not a valid {name.replace('_', ' ')}")
            if enforce and key not in getattr(evidence.known, pool):
                evidence.refused.append(name)
                raise ValueError(f"{name} is not on this ticket or the client's in an earlier result")
            out[name] = raw
        if args.get("days") not in (None, ""):
            try:
                out["days"] = int(args["days"])
            except (TypeError, ValueError):
                raise ValueError("days must be a whole number") from None
        if args.get("problem"):
            out["problem"] = str(args["problem"]).strip()
        return out

    def _today(self) -> date:
        return self.as_of or today_local()

    # ---- lookups ---------------------------------------------------------------------------------------
    def _client_account(self, account: str, evidence: Evidence) -> CoreAccount:
        assert self.core is not None
        key = account_key(account) or ""
        if key not in evidence.accounts:
            acct = self.core.account(account)
            evidence.accounts[key] = acct
            evidence.searches.append({"tool": "check_core_banking", "args": {"account_number": account},
                                      "found": int(acct.exists)})
            if acct.exists and acct.cif and not evidence.ticket_cifs:
                # With no CIF on the ticket, the CIF of the account the branch gave is the client's. With one, an
                # account under another CIF stays someone else's (a joint or corporate account, or a typo).
                evidence.known.cifs.add(acct.cif)
                evidence.client_cifs.add(acct.cif)
        return evidence.accounts[key]

    def _profile(self, values: dict[str, Any], evidence: Evidence) -> dict[str, Any]:
        assert self.registration is not None
        cifs = [cif_key(values["cif"])] if values.get("cif") else []
        if values.get("account_number"):
            if self.core is None:
                raise ValueError("core banking is not available here: give the CIF")
            acct = self._client_account(values["account_number"], evidence)
            if not acct.exists:
                return {"account_number": acct.account, "note": "This account number does not exist in core banking."}
            if acct.cif and acct.cif not in cifs:
                cifs.append(acct.cif)
        mobiles = [mobile_key(values["mobile"])] if values.get("mobile") else []
        if not cifs and not mobiles:
            raise ValueError("give the client's CIF, account number or mobile")
        found: list[tuple[str, str, dict[str, Any]]] = []
        for c in cifs:
            rows = self.registration.by_cif(c)
            evidence.profile_lookups.append({"by": "cif", "value": c, "found": len(rows)})
            found += [("cif", c, r) for r in rows]
        for m in mobiles:
            rows = self.registration.by_mobile(m)
            evidence.profile_lookups.append({"by": "mobile", "value": f"0{m}", "found": len(rows)})
            found += [("mobile", m, r) for r in rows]
        shown: list[Profile] = []
        for by, _value, row in found:
            profile = Profile.from_row(f"P{len(evidence.profiles) + 1}", row)
            twin = next((p for p in evidence.profiles.values() if p.same_row(profile)), None)
            if twin is None:
                evidence.profiles[profile.ref] = profile
            else:
                profile = twin
            profile.found_by.add(by)
            if profile not in shown:
                shown.append(profile)
            if profile.cif in evidence.client_cifs and profile.mobile9:  # the number on the client's own profile
                evidence.known.mobiles.add(profile.mobile9)
        evidence.searches.append({"tool": "get_connect_profile", "args": {k: str(v) for k, v in values.items()},
                                  "found": len(shown)})
        out: dict[str, Any] = {
            "profiles": [p.describe(evidence.ticket_mobiles, evidence.client_cifs) for p in shown],
            "active_profiles_for_cif": {c: sum(1 for p in shown if p.active and p.cif == c) for c in cifs},
        }
        if mobiles:
            out["active_profiles_with_this_mobile_under_another_cif"] = sum(
                1 for p in shown if p.active and p.mobile9 in mobiles and p.cif not in evidence.client_cifs)
        hidden = sorted({c for p in shown for c in p.other_columns})
        if hidden:
            out["not_shown"] = f"secrets, names and {len(hidden)} other column(s) of the profile table"
        ids = [p.row_id for p in shown if p.row_id and p.cif in evidence.client_cifs]
        for label, reader in (("channels", self.registration.channels), ("recent_logins", self.registration.history)):
            if not ids:
                break
            try:
                rows = reader(ids)
                if rows:
                    out[label] = rows
            except MetabaseError as exc:
                evidence.unavailable.append(f"{label}: {str(exc)[:100]}")
                out.setdefault("not_available", []).append(label)
        if not shown:
            out["note"] = "No NMB Connect profile found."
        return out

    def _core(self, values: dict[str, Any], evidence: Evidence) -> dict[str, Any]:
        assert self.core is not None
        out: dict[str, Any] = {}
        cif = cif_key(values["cif"]) if values.get("cif") else None
        if values.get("account_number"):
            acct = self._client_account(values["account_number"], evidence)
            out["account"] = acct.describe()
            if not acct.exists:
                out["note"] = "This account number does not exist in core banking."
                return out
            cif = cif or acct.cif
        if cif is None:
            raise ValueError("give the client's account number or CIF")
        if cif not in evidence.client_cifs:
            evidence.refused.append("cif")
            raise ValueError("that CIF is not the client's (not on the ticket, nor the CIF of the client's account)")
        customer = evidence.customers.get(cif)
        if customer is None:
            customer = self.core.customer(cif)
            evidence.customers[cif] = customer
            for acct in customer["accounts"]:  # the client's other accounts may be looked at too
                key = account_key(acct.account)
                if key:
                    evidence.known.accounts.add(key)
                    evidence.accounts.setdefault(key, acct)
            for number in [customer.get("mobile_on_file")] + list(customer.get("idccob_mobiles") or []):
                if mobile_key(number):
                    evidence.known.mobiles.add(mobile_key(number))
            evidence.searches.append({"tool": "check_core_banking", "args": {"cif": cif},
                                      "found": len(customer["accounts"])})
        ticket_mobiles = evidence.ticket_mobiles
        mine = evidence.client_profiles()
        on_file = customer.get("mobile_on_file")
        out["customer"] = {
            "cif": cif, "found": customer["exists"],
            "accounts": [{**a.describe(), "on_ticket": account_key(a.account) in evidence.known.accounts}
                         for a in customer["accounts"]],
            "mobile_on_file": on_file,
            "mobile_on_file_matches_ticket": (mobile_key(on_file) in ticket_mobiles) if on_file else None,
            "email_on_file": customer.get("email_on_file"),
            "idccob_mobiles": customer.get("idccob_mobiles"),
        }
        if mine:
            out["customer"]["mobile_on_file_matches_profile"] = bool(on_file) and mobile_key(on_file) == mine[0].mobile9
            if mine[0].email and customer.get("email_on_file"):
                out["customer"]["email_matches_profile"] = mine[0].email.lower() == customer["email_on_file"]
        for key in ("contact_error", "idccob_error"):
            if customer.get(key):
                out.setdefault("not_available", []).append(key.replace("_error", ""))
        if self.core.pending_sql:
            try:
                evidence.pending = self.core.pending_changes(cif, values.get("account_number"))
                out["pending_changes"] = evidence.pending
            except (ValueError, MetabaseError) as exc:
                out["pending_changes"] = f"not available: {str(exc)[:120]}"
        return out

    def _activity(self, values: dict[str, Any], evidence: Evidence) -> dict[str, Any]:
        assert self.activity is not None
        days = max(1, min(int(values.get("days") or self.activity_days), 60))
        cif = cif_key(values["cif"]) if values.get("cif") else None
        if cif and cif not in evidence.client_cifs and evidence.client_cifs:
            evidence.refused.append("cif")
            raise ValueError("that CIF is not the client's")
        rows = self.activity.recent(cif, values.get("mobile"), window_start(self._today(), days))
        evidence.activity = summarise_activity(rows, days)
        evidence.searches.append({"tool": "get_app_activity", "args": {k: str(v) for k, v in values.items()},
                                  "found": len(rows)})
        return evidence.activity

    def _failures(self, values: dict[str, Any], evidence: Evidence) -> dict[str, Any]:
        assert self.failures is not None
        rows = self.failures.find(values.get("mobile"), values.get("id_number"))
        evidence.failures = rows
        evidence.searches.append({"tool": "find_registration_failures", "args": {k: str(v) for k, v in values.items()},
                                  "found": len(rows)})
        return {"found": len(rows), "failures": [{**r, "when": format_timestamp(r["when"])} for r in rows],
                "note": None if rows else "No failed registration recorded for this mobile or ID."}

    def _sms(self, values: dict[str, Any], evidence: Evidence) -> dict[str, Any]:
        assert self.sms is not None
        if not values.get("mobile"):
            raise ValueError("mobile is required")
        days = max(1, min(int(values.get("days") or self.sms_days), 30))
        rows = self.sms.sent_to(values["mobile"], window_start(self._today(), days))
        evidence.sms = summarise_sms(rows, days)
        evidence.searches.append({"tool": "get_sms_to_number", "args": {"mobile": values["mobile"], "days": str(days)},
                                  "found": len(rows)})
        return evidence.sms

    def _assess(self, values: dict[str, Any], evidence: Evidence, ticket: Ticket) -> dict[str, Any]:
        problem = values.get("problem") or ""
        account = values.get("account_number")
        if problem in ACCOUNT_PROBLEMS and account is None:
            owned = sorted(ticket.owner_details().accounts)
            if len(owned) == 1:
                account = owned[0]
        if problem in ACCOUNT_PROBLEMS and account and self.core is not None:
            self._client_account(account, evidence)
        evidence.assessment = assess(problem, ticket, evidence, account=account, mobile_now=values.get("mobile"))
        return evidence.assessment.describe()
