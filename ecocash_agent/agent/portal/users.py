"""Portal users: PBKDF2-SHA256 password hashes in a JSON file (no plaintext passwords anywhere).

Roles: viewer (read, review decisions) and admin (also pause the agent / hold its mode).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
from pathlib import Path
from typing import Optional

ROLES = ("viewer", "admin")
ITERATIONS = 240_000
NAME_RE = re.compile(r"^[a-z0-9._-]{2,32}$")


def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, digest = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


class UserStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"users": {}}
        return data if isinstance(data.get("users"), dict) else {"users": {}}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def users(self) -> dict[str, str]:
        return {name: info.get("role", "viewer") for name, info in self._load()["users"].items()}

    def set(self, name: str, password: str, role: str) -> None:
        if not NAME_RE.match(name):
            raise ValueError("user names are 2-32 characters: lowercase letters, digits, dot, dash, underscore")
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        if len(password) < 12:
            raise ValueError("passwords must be at least 12 characters")
        data = self._load()
        data["users"][name] = {"role": role, "hash": hash_password(password)}
        self._save(data)

    def remove(self, name: str) -> bool:
        data = self._load()
        if data["users"].pop(name, None) is None:
            return False
        self._save(data)
        return True

    def authenticate(self, name: str, password: str) -> Optional[str]:
        """Returns the role, or None."""
        info = self._load()["users"].get(name)
        if info is None:
            verify_password(password, hash_password("timing-equaliser", 1000))
            return None
        return info.get("role", "viewer") if verify_password(password, info.get("hash", "")) else None
