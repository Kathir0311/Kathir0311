"""Users, roles and the work log (V4.19; V4.23: the production roles).

Roles (core/workflow.py has what each may do):

* admin — sees every file and every user; creates, edits and switches off
  users; uploads whole folders; gives files to users (allocate), moves them to
  another user (reallocate), takes them back (unassign), puts the last change
  back (revert); moves a file to any step; changes the settings.
* production — uploads and analyses files, tags them and sends them to QA
  with a PAC report; fixes what QA sends back. ("user" in earlier versions.)
* qa / final_qa — no upload or analysis: check and correct the files in
  their queue, comment, approve or send to rework.

Users live in users.json next to config.json (PDFUA_USERS to put it
elsewhere); passwords are kept only as salted hashes. The first start makes
the admin account "Kathir". The work log (activity.log, one JSON line per
action) records who did what to which file and when."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from datetime import datetime

from werkzeug.security import check_password_hash, generate_password_hash

from . import rights as R

ROLES = ("admin", "production", "qa", "final_qa", "user")
ROLE_NAMES = "admin, production, qa or final_qa"
# the first admin (the password is only kept as this hash)
SEED_ADMIN = {
    "username": "Kathir", "name": "Kathir", "role": "admin",
    "pw": "pbkdf2:sha256:600000$ST6JsznHb1Ai1KiG$5bf2eafc8cde29f0c89ac0df15169077c33ec63c0572ad7a0d076ec9851af496",
}

_LOCK = threading.RLock()


class AccountError(Exception):
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Accounts:
    def __init__(self, path: str, log_path: str):
        self.path = path
        self.log_path = log_path
        self._data: dict | None = None
        self._mtime = 0.0
        self._ok: dict[str, tuple[str, float]] = {}        # username -> (tag of the stored hash + password, when it was found right)

    # ------------------------------------------------------------ storage
    def _load(self) -> dict:
        with _LOCK:
            try:
                mt = os.path.getmtime(self.path)
            except OSError:
                mt = -1
            if self._data is not None and mt == self._mtime:
                return self._data
            data = {}
            if mt >= 0:
                try:
                    with open(self.path, encoding="utf-8") as fh:
                        data = json.load(fh)
                except Exception:
                    data = {}
            data.setdefault("users", {})
            changed = False
            if not data.get("secret"):
                data["secret"] = secrets.token_hex(32)
                changed = True
            if not any(u.get("role") == "admin" for u in data["users"].values()):
                key = SEED_ADMIN["username"].lower()
                data["users"][key] = {**SEED_ADMIN, "active": True, "created": _now(), "last_login": ""}
                changed = True
            self._data = data
            if changed:
                self._save()
            else:
                self._mtime = mt
            return data

    def _save(self) -> None:
        with _LOCK:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=1)
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            self._mtime = os.path.getmtime(self.path)

    @property
    def secret(self) -> str:
        return self._load()["secret"]

    # ------------------------------------------------------------ users
    def get(self, username: str) -> dict | None:
        if not username:
            return None
        return self._load()["users"].get(username.strip().lower())

    def all(self) -> list[dict]:
        return sorted((self.public(u) for u in self._load()["users"].values()),
                      key=lambda u: (u["role"] != "admin", u["username"].lower()))

    @staticmethod
    def public(u: dict) -> dict:
        out = {k: u.get(k, "") for k in ("username", "name", "role", "active", "created", "last_login",
                                         "email", "note")}
        out["rights"] = R.explicit(u)                      # V4.30: what an admin set for this person (see core/rights.py)
        out["effective"] = R.effective(u)
        return out

    def check(self, username: str, password: str) -> dict | None:
        u = self.get(username)
        if u is None or not u.get("active", True):
            return None
        # V4.30: a script that sends the password with every request (HTTP Basic) paid ~0.4 s of hashing each time.
        # What was found right is remembered for a minute — against the same stored hash only, so a changed password
        # (or an account switched off) is refused at once.
        tag = hashlib.sha256((u.get("pw", "") + "\0" + (password or "")).encode("utf-8")).hexdigest()
        now = time.time()
        with _LOCK:
            if self._ok.get(u["username"].lower()) and self._ok[u["username"].lower()][0] == tag \
                    and now - self._ok[u["username"].lower()][1] < 60:
                return u
        if not check_password_hash(u.get("pw", ""), password or ""):
            return None
        with _LOCK:
            self._ok[u["username"].lower()] = (tag, now)
            if len(self._ok) > 500:
                self._ok.clear()
            u["last_login"] = _now()
            self._save()
        return u

    def create(self, username: str, password: str, name: str = "", role: str = "production",
               email: str = "", note: str = "") -> dict:
        username = (username or "").strip()
        if not username or len(username) > 40 or not all(c.isalnum() or c in "._-" for c in username):
            raise AccountError("a user name is letters, digits, . _ or - (up to 40)")
        if role not in ROLES:
            raise AccountError(f"the role is {ROLE_NAMES}")
        if len(password or "") < 6:
            raise AccountError("the password needs at least 6 characters")
        with _LOCK:
            data = self._load()
            key = username.lower()
            if key in data["users"]:
                raise AccountError(f"there is a user {username} already")
            data["users"][key] = {"username": username, "name": (name or username)[:80], "role": role,
                                  "pw": generate_password_hash(password, method="pbkdf2:sha256:600000"),
                                  "active": True, "created": _now(), "last_login": "",
                                  "email": (email or "")[:120], "note": (note or "")[:300]}
            self._save()
            return self.public(data["users"][key])

    def update(self, username: str, **kw) -> dict:
        with _LOCK:
            data = self._load()
            u = data["users"].get((username or "").lower())
            if u is None:
                raise AccountError("no such user")
            if "role" in kw and kw["role"] is not None:
                if kw["role"] not in ROLES:
                    raise AccountError(f"the role is {ROLE_NAMES}")
                if u["role"] == "admin" and kw["role"] != "admin" and self._admins() <= 1:
                    raise AccountError("the last admin stays an admin")
                u["role"] = kw["role"]
            if "active" in kw and kw["active"] is not None:
                if u["role"] == "admin" and not kw["active"] and self._admins() <= 1:
                    raise AccountError("the last admin cannot be switched off")
                u["active"] = bool(kw["active"])
            for k in ("name", "email", "note"):
                if kw.get(k) is not None:
                    u[k] = str(kw[k])[:300]
            if kw.get("rights") is not None:                    # V4.30: {"delete_files": false, "see_all": true, "build": null}
                try:
                    u["rights"] = R.clean(u.get("rights"), kw["rights"])
                except ValueError as exc:
                    raise AccountError(str(exc))
            if kw.get("password"):
                if len(kw["password"]) < 6:
                    raise AccountError("the password needs at least 6 characters")
                u["pw"] = generate_password_hash(kw["password"], method="pbkdf2:sha256:600000")
            self._save()
            return self.public(u)

    def delete(self, username: str) -> None:
        with _LOCK:
            data = self._load()
            u = data["users"].get((username or "").lower())
            if u is None:
                raise AccountError("no such user")
            if u["role"] == "admin" and self._admins() <= 1:
                raise AccountError("the last admin cannot be deleted")
            del data["users"][username.lower()]
            self._save()

    def _admins(self) -> int:
        return sum(1 for u in self._load()["users"].values() if u.get("role") == "admin" and u.get("active", True))

    # ------------------------------------------------------------ the log
    def log(self, who: str, action: str, job: str = "", filename: str = "", detail: str = "") -> dict:
        e = {"at": _now(), "by": who, "action": action, "job": job, "file": filename, "detail": detail[:300]}
        with _LOCK:
            try:
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(e) + "\n")
            except OSError:
                pass
        return e

    def recent(self, limit: int = 300, who: str | None = None, job: str | None = None) -> list[dict]:
        try:
            with open(self.log_path, encoding="utf-8") as fh:
                lines = fh.readlines()[-20000:]
        except OSError:
            return []
        out = []
        for line in reversed(lines):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if who and e.get("by", "").lower() != who.lower() and (e.get("detail") or "").lower().find(who.lower()) < 0:
                continue
            if job and e.get("job") != job:
                continue
            out.append(e)
            if len(out) >= limit:
                break
        return out
