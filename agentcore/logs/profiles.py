"""A service's log profile: what it is, how its correlation ids look, and which of its lines are known noise.

Most services share one format (journald lines carrying Spring Boot logback lines), so a profile is small. A new
service needs a file in agentcore/logs/profiles/ (a first one can be drafted from a sample by a person or a model;
a command for that is not built yet); until it has one, the generic defaults apply."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

PROFILE_DIR = Path(__file__).with_name("profiles")
# The ids most services log, as name=value, name: value or "Name":"value" (a value may contain a masked token).
# REQUEST_IDS identify one request, so the lines that share one are one trail through the services; the others
# (a session, a customer's uuid) cover many requests: they find lines, but never join lines into a trail.
REQUEST_IDS = ("requestid", "correlationid", "traceid", "transactionid", "rrn")
SESSION_IDS = ("sessionid", "uuid")
DEFAULT_CORRELATION = (r"(?i)\b(requestId|correlationId|traceId|transactionId|rrn|sessionId|uuid)\b['\"]?\s*[=:]\s*['\"]?"
                       r"([A-Za-z0-9_<>:~\-]{4,80})")


CANONICAL = {"requestid": "requestId", "correlationid": "correlationId", "traceid": "traceId",
             "transactionid": "transactionId", "rrn": "rrn", "sessionid": "sessionId", "uuid": "uuid"}


def scope_of(name: str) -> str:
    return "session" if name.lower() in SESSION_IDS or "session" in name.lower() else "request"


@dataclass
class Profile:
    service: str
    about: str = ""
    format: str = "journald"  # journald (journalctl output) | plain (the application's own file)
    owner: str = ""
    app_packages: list[str] = field(default_factory=lambda: ["com.nmbz", "com.visa", "nmbz"])
    # extra ids beyond the defaults: name -> regex with one group (the name decides the scope: *session* ids are session-wide)
    correlation: dict[str, str] = field(default_factory=dict)
    noise: list[str] = field(default_factory=list)  # regexes on a message: known and harmless repeats
    clock_offset: int = 0  # seconds to add to this service's clock to match the others'

    def __post_init__(self) -> None:
        if self.format not in ("journald", "plain"):
            raise ValueError(f"profile {self.service}: format must be journald or plain")
        for rx in [*self.correlation.values(), *self.noise]:
            re.compile(rx)  # a bad pattern fails when the profile loads, not on the millionth line
        self.default_re = re.compile(DEFAULT_CORRELATION)
        self.extra_res = [(name, re.compile(rx)) for name, rx in self.correlation.items()]
        self.noise_res = [re.compile(r) for r in self.noise]

    def is_noise(self, text: str) -> bool:
        return any(r.search(text) for r in self.noise_res)

    def correlations(self, text: str) -> list[tuple[str, str]]:
        """(name, value) for each id in a line, names in their canonical spelling."""
        out: list[tuple[str, str]] = []
        for m in self.default_re.finditer(text):
            pair = (CANONICAL.get(m.group(1).lower(), m.group(1)), m.group(2))
            if pair not in out:
                out.append(pair)
        for name, rx in self.extra_res:
            for m in rx.finditer(text):
                pair = (name, m.group(1))
                if pair not in out:
                    out.append(pair)
        return out[:12]


def load_profiles(path: Path = PROFILE_DIR) -> dict[str, Profile]:
    out = {}
    for file in sorted(Path(path).glob("*.json")):
        profile = Profile(**json.loads(file.read_text(encoding="utf-8")))
        out[profile.service] = profile
    return out


def profile_for(service: str, path: Path = PROFILE_DIR) -> Profile:
    """The profile of a service, or the generic one (journald + Spring Boot) when it has none yet."""
    return load_profiles(path).get(service) or Profile(service=service, about="(no profile yet)")


def describe(profiles: dict[str, Profile]) -> str:
    return "; ".join(f"{p.service}: {p.about}" for p in profiles.values() if p.about)
