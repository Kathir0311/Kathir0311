"""Rights — what a person may do, on top of their role (V4.30).

The role (core/workflow.py) says what a person does in the workflow: production tags files, QA and Final QA check
them, an admin runs everything. A *right* is one thing on top of that, which an admin gives to a person or takes
away — "this production user may not delete files", "this QA lead may see every file", "this one may use the AI
helpers". Each right has what the role means by default; a person's own setting (Allow / Deny) wins over it. An
admin may always do everything: rights narrow or widen the others only (so nobody locks the last admin out).

users.json keeps only the settings an admin made: {"rights": {"delete_files": false, "see_all": true}}.
"""
from __future__ import annotations

ROLE_ALIAS = {"user": "production"}

# key, label, help, group, what each role may by default
RIGHTS: list[dict] = [
    {"key": "upload", "group": "Files", "label": "Upload files and folders",
     "help": "Add PDFs (and whole folders) to be analysed.",
     "default": {"production": True, "qa": False, "final_qa": False}},
    {"key": "reanalyse", "group": "Files", "label": "Analyse again · change the profile · auto-tag",
     "help": "Run the analysis again, give a file another profile, let the tool tag pages again, answer “this PDF has tags”.",
     "default": {"production": True, "qa": False, "final_qa": False}},
    {"key": "delete_files", "group": "Files", "label": "Delete files",
     "help": "Delete a file they uploaded (a file an admin gave them stays).",
     "default": {"production": True, "qa": False, "final_qa": False}},
    {"key": "build", "group": "Output", "label": "Build the accessible PDF",
     "help": "Make the tagged PDF and the reports from the tags as they are now.",
     "default": {"production": True, "qa": True, "final_qa": True}},
    {"key": "download", "group": "Output", "label": "Download PDFs and reports",
     "help": "Download the original, the accessible PDF, the reports and a page's support zip.",
     "default": {"production": True, "qa": True, "final_qa": True}},
    {"key": "use_ai", "group": "Tools", "label": "Use the AI helpers",
     "help": "Draft alt text, heading levels, table summaries, link and field wording, reading order … with the local AI.",
     "default": {"production": True, "qa": True, "final_qa": True}},
    {"key": "see_all", "group": "Visibility", "label": "See every file (look only)",
     "help": "Open any file of the server and read its tags and checks. Changing it stays with whoever has it at its step.",
     "default": {"production": False, "qa": False, "final_qa": False}},
    {"key": "take_over", "group": "Visibility", "label": "Take over a file somebody is working in",
     "help": "Take a file from the person who has it open (one person at a time in a file).",
     "default": {"production": False, "qa": False, "final_qa": False}},
    {"key": "view_times", "group": "Visibility", "label": "See who spent the working time on a file",
     "help": "Working and QC time of every person on the files they can see (everybody sees their own).",
     "default": {"production": False, "qa": False, "final_qa": False}},
]
BY_KEY = {r["key"]: r for r in RIGHTS}


def role_of(user: dict | None) -> str:
    r = (user or {}).get("role") or ""
    return ROLE_ALIAS.get(r, r)


def default_for(role: str, key: str) -> bool:
    return bool(BY_KEY[key]["default"].get(ROLE_ALIAS.get(role, role), False))


def allowed(user: dict | None, key: str) -> bool:
    """May this person? (An admin always may.)"""
    if not user or key not in BY_KEY:
        return False
    role = role_of(user)
    if role == "admin":
        return True
    mine = (user.get("rights") or {}).get(key)
    if mine is True or mine is False:
        return mine
    return default_for(role, key)


def effective(user: dict | None) -> dict[str, bool]:
    return {r["key"]: allowed(user, r["key"]) for r in RIGHTS}


def explicit(user: dict | None) -> dict[str, bool]:
    """The settings an admin made for this person (not the defaults of their role)."""
    mine = (user or {}).get("rights") or {}
    return {k: bool(v) for k, v in mine.items() if k in BY_KEY and v in (True, False)}


def clean(current: dict | None, change: dict) -> dict:
    """`current` with `change` applied: a key set to true / false is an Allow / Deny, null (or "default") removes it.
    Unknown keys are refused."""
    out = {k: v for k, v in (current or {}).items() if k in BY_KEY and v in (True, False)}
    for k, v in (change or {}).items():
        if k not in BY_KEY:
            raise ValueError(f"unknown right: {k}")
        if v is None or v == "default":
            out.pop(k, None)
        elif v in (True, "allow"):
            out[k] = True
        elif v in (False, "deny"):
            out[k] = False
        else:
            raise ValueError(f"{k}: allow, deny or default")
    return out


def catalog() -> list[dict]:
    """For the Admin page: every right with what each role means by default."""
    return [{"key": r["key"], "group": r["group"], "label": r["label"], "help": r["help"],
             "default": {role: default_for(role, r["key"]) for role in ("production", "qa", "final_qa")}} for r in RIGHTS]
