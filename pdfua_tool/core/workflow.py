"""The production workflow (V4.23): Production → QA → Final QA → Delivery,
with rework rounds, review comments, a QA checklist and PAC reports.

Roles
* admin       — everything; gives files out, overrides any step.
* production  — uploads and analyses files, tags them, sends them to QA with a
                PAC report; fixes what QA sends back (rework stage-1, -2 …).
                ("user", the role of earlier versions, is production.)
* qa          — no upload, no analysis: picks files from the QA queue, corrects
                and checks them, comments, approves (→ Final QA) or sends them
                to rework.
* final_qa    — the same for the last check: approves (→ Delivery) or sends
                to rework.

A file's place is its `work` state (meta["work"]); everything else of the
workflow sits in meta["wf"]: who checks it, the rework round, the comments,
the checklist, the PAC reports and the history of every step.

Everything here works on a job's meta dict and raises WorkflowError for a
step that is not allowed; the web app saves the meta and logs the step."""
from __future__ import annotations

import uuid
from datetime import datetime

ROLES = ("admin", "production", "qa", "final_qa", "user")
ROLE_LABELS = {"admin": "Admin", "production": "Production", "user": "Production",
               "qa": "QA", "final_qa": "Final QA"}

# work state → label (the order is the order of the pipeline)
WORK_STATES = {
    "": "not given",
    "assigned": "Production · given",
    "in_progress": "Production · in progress",
    "rework": "Production · rework",
    "returned": "Production · sent back",          # earlier versions
    "qa": "QA · waiting",
    "submitted": "QA · waiting",                    # earlier versions ("sent for checking")
    "qa_progress": "QA · checking",
    "final_qa": "Final QA · waiting",
    "fqa_progress": "Final QA · checking",
    "approved": "Delivery · ready",
    "delivered": "Delivered",
    "on_hold": "On hold",
}
STAGE_OF = {"": "production", "assigned": "production", "in_progress": "production", "rework": "production",
            "returned": "production", "qa": "qa", "submitted": "qa", "qa_progress": "qa",
            "final_qa": "final_qa", "fqa_progress": "final_qa", "approved": "delivery",
            "delivered": "delivered", "on_hold": "hold"}
STAGES = ("production", "qa", "final_qa", "delivery", "delivered")
STAGE_LABELS = {"production": "Production", "qa": "QA", "final_qa": "Final QA", "delivery": "Delivery",
                "delivered": "Delivered", "hold": "On hold"}

CHECKLIST = [
    ("tags", "Every content tagged · decorations are artifacts"),
    ("order", "Reading order follows the page"),
    ("headings", "Headings and their levels"),
    ("alt", "Alt text of every figure"),
    ("tables", "Tables: header cells, scope, no empty rows"),
    ("lists", "Lists: labels, items, nesting"),
    ("links", "Links, bookmarks, cross-references"),
    ("meta", "Title, language, document properties"),
    ("contrast", "Colour contrast"),
    ("pac", "PAC report checked — no PDF/UA failure"),
]
CATEGORIES = ["Tagging", "Reading order", "Headings", "Alt text", "Tables", "Lists", "Links",
              "Metadata", "Contrast", "Other"]
SEVERITIES = ["Minor", "Major", "Critical"]
PAC_EXT = (".pdf", ".html", ".htm", ".xml", ".txt", ".csv", ".png", ".jpg", ".jpeg", ".zip", ".docx", ".xlsx")

DEFAULTS = {
    "require_pac": True,            # Production → QA needs a PAC report
    "pac_must_pass": False,         # … that passed (no PDF/UA failure)
    "require_build": True,          # … and the accessible PDF built after the last change (also before an approval)
    "require_clean": False,         # … and the tool's own check with 0 errors
    "require_fixed": True,          # … and every QA comment of the rework marked fixed
    "final_qa": True,               # QA approval → Final QA (off: → Delivery)
    "return_to_sender": True,       # rework from Final QA goes back to Final QA
    "require_checklist": False,     # approval needs every checklist line ticked
    "auto_assign": False,           # a file reaching QA / Final QA goes to the checker with the fewest files
    "file_lock": True,              # V4.29: one person at a time in a file — the others can look, not change
}
SETTING_LABELS = {
    "require_pac": ("PAC report before QA", "Production uploads the PAC report of the built PDF to send a file to QA."),
    "pac_must_pass": ("PAC report must pass", "A PAC report marked failed (or with failures) stops the file from going to QA."),
    "require_build": ("Built after the last change", "The accessible PDF is built after the last tag change — before QA, and before every approval."),
    "require_clean": ("0 errors in the tool's check", "The tool's own PDF/UA check of the built file shows no error before QA."),
    "require_fixed": ("Every rework comment fixed", "After a rework, every open comment is marked fixed (with a reply) before the file goes back."),
    "final_qa": ("Final QA step", "QA approval sends the file to Final QA. Off: QA approval sends it straight to Delivery."),
    "return_to_sender": ("Rework goes back to who sent it", "A file Final QA sent to rework goes straight back to Final QA (not through QA again)."),
    "require_checklist": ("Checklist before approval", "QA and Final QA tick every line of the checklist (ok or n/a) to approve."),
    "auto_assign": ("Give checks out by themselves", "A file reaching QA / Final QA goes to the checker with the fewest files waiting. Off: checkers pick from the queue."),
    "file_lock": ("One person at a time in a file", "Whoever opens a file to work in it has it: everybody else can open it and look, but no change is accepted until the person leaves, goes idle for 10 minutes, or an admin takes the file over."),
}
PRIORITY_RANK = {"urgent": 0, "high": 1, "normal": 2, "low": 3}


class WorkflowError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def norm_role(role: str | None) -> str:
    r = (role or "").strip().lower()
    return "production" if r in ("user", "production", "") else r


def role_of(user: dict | None) -> str:
    return norm_role((user or {}).get("role"))


def wf(meta: dict) -> dict:
    w = meta.get("wf")
    if not isinstance(w, dict):
        w = {}
        meta["wf"] = w
    w.setdefault("rework", 0)
    w.setdefault("rework_from", "")
    w.setdefault("qa_user", "")
    w.setdefault("fqa_user", "")
    w.setdefault("comments", [])
    w.setdefault("history", [])
    w.setdefault("pac", [])
    w.setdefault("checklist", {})
    return w


def stage(meta: dict) -> str:
    return STAGE_OF.get(meta.get("work") or "", "production")


def _me(user: dict | None) -> str:
    return ((user or {}).get("username") or "").lower()


def _is(name: str, user: dict | None) -> bool:
    return bool(name) and name.lower() == _me(user)


# ------------------------------------------------------------ who may do what
def can_see(meta: dict, user: dict | None) -> bool:
    if user is None:
        return False
    r = role_of(user)
    if r == "admin":
        return True
    w = meta.get("wf") or {}
    if r == "production":
        return _is(meta.get("owner") or "", user) or _is(meta.get("assigned_to") or "", user)
    st = stage(meta)
    if r == "qa":
        if st == "qa" and (not w.get("qa_user") or _is(w.get("qa_user"), user)):
            return True
        return _is(w.get("qa_user") or "", user)          # files they checked before (to look at)
    if r == "final_qa":
        if st == "final_qa" and (not w.get("fqa_user") or _is(w.get("fqa_user"), user)):
            return True
        return _is(w.get("fqa_user") or "", user)
    return False


def can_edit(meta: dict, user: dict | None) -> bool:
    """Who may change the tags (and build) now: the file's stage decides."""
    if user is None:
        return False
    r = role_of(user)
    if r == "admin":
        return True
    w = meta.get("wf") or {}
    st = stage(meta)
    if r == "production":
        if st != "production" or (meta.get("work") == "on_hold"):
            return False
        a = meta.get("assigned_to") or ""
        return _is(a, user) or (not a and _is(meta.get("owner") or "", user))
    if r == "qa":
        return st == "qa" and (not w.get("qa_user") or _is(w.get("qa_user"), user))
    if r == "final_qa":
        return st == "final_qa" and (not w.get("fqa_user") or _is(w.get("fqa_user"), user))
    return False


def can_analyse(user: dict | None) -> bool:
    """Upload, analyse, re-analyse, auto-tag, change the profile: not for QA."""
    return role_of(user) in ("admin", "production")


def read_only_reason(meta: dict, user: dict | None) -> str:
    if can_edit(meta, user):
        return ""
    st = stage(meta)
    r = role_of(user)
    w = meta.get("wf") or {}
    if meta.get("work") == "on_hold":
        return "The file is on hold."
    if r == "production":
        return f"The file is with {STAGE_LABELS.get(st, st)} — you can look at it, not change it."
    if r == "qa" and st == "qa" and w.get("qa_user"):
        return f"{w['qa_user']} is checking this file."
    if r == "final_qa" and st == "final_qa" and w.get("fqa_user"):
        return f"{w['fqa_user']} is checking this file."
    return f"The file is in {STAGE_LABELS.get(st, st)} — you can look at it, not change it."


# ------------------------------------------------------------ history
def _event(meta: dict, user: dict | None, action: str, frm: str, to: str, note: str = "", **extra) -> dict:
    w = wf(meta)
    e = {"at": now(), "by": (user or {}).get("username", "system"), "role": role_of(user),
         "action": action, "from": frm, "to": to, "round": w.get("rework", 0)}
    if note:
        e["note"] = note[:1000]
    e.update(extra)
    w["history"] = (w["history"] + [e])[-300:]
    meta[f"{to or 'pool'}_at"] = e["at"]
    return e


def _set(meta: dict, user, to: str, action: str, note: str = "", **extra) -> dict:
    frm = meta.get("work") or ""
    meta["work"] = to
    return _event(meta, user, action, frm, to, note, **extra)


def open_comments(meta: dict, stage_: str | None = None) -> list:
    return [c for c in wf(meta)["comments"] if c.get("status") == "open"
            and (stage_ is None or c.get("stage") == stage_)]


# ------------------------------------------------------------ the steps
def start(meta: dict, user: dict, cfg: dict | None = None) -> dict:
    """Production starts the file; QA / Final QA pick it from their queue."""
    r = role_of(user)
    w = wf(meta)
    work = meta.get("work") or ""
    st = stage(meta)
    if work == "on_hold":
        raise WorkflowError("the file is on hold")
    if st == "production" and r in ("production", "admin"):
        if work in ("assigned", "returned", ""):
            return _set(meta, user, "in_progress", "started")
        if work == "rework":
            return _event(meta, user, "started rework", work, work)
        raise WorkflowError("the file is started already")
    if st == "qa" and r in ("qa", "admin"):
        if w["qa_user"] and not _is(w["qa_user"], user) and r != "admin":
            raise WorkflowError(f"{w['qa_user']} is checking this file", 403)
        if r == "qa":
            w["qa_user"] = user["username"]
        return _set(meta, user, "qa_progress", "QA started")
    if st == "final_qa" and r in ("final_qa", "admin"):
        if w["fqa_user"] and not _is(w["fqa_user"], user) and r != "admin":
            raise WorkflowError(f"{w['fqa_user']} is checking this file", 403)
        if r == "final_qa":
            w["fqa_user"] = user["username"]
        return _set(meta, user, "fqa_progress", "Final QA started")
    raise WorkflowError("there is nothing to start here for you", 403)


def check_ready_for_qa(meta: dict, cfg: dict, pac: dict | None, built: bool, dirty: bool) -> list[str]:
    """What still stops the file from going to QA (empty: nothing)."""
    c = {**DEFAULTS, **(cfg or {})}
    w = wf(meta)
    missing = []
    if c["require_build"] and not built:
        missing.append("build the accessible PDF first")
    elif c["require_build"] and dirty:
        missing.append("the tags changed after the last build — build again")
    if c["require_clean"] and built and not dirty:
        errs = int(((meta.get("score_after") or {}).get("errors")) or 0)
        if errs:
            missing.append(f"the tool's check of the built PDF shows {errs} error(s) — fix them first")
    if c["require_pac"] and not pac:
        missing.append("upload the PAC report")
    if c["pac_must_pass"] and pac and (pac.get("result") == "fail" or (pac.get("errors") or 0) > 0):
        missing.append("the PAC report shows failures — fix them and check again")
    if c["require_fixed"] and w["rework"]:
        left = [x for x in w["comments"] if x.get("status") == "open"]
        if left:
            missing.append(f"{len(left)} QA comment(s) not marked fixed yet")
    return missing


def submit(meta: dict, user: dict, cfg: dict | None, pac: dict | None, note: str = "",
           built: bool = True, dirty: bool = False) -> dict:
    """Production → QA (or straight back to Final QA after a rework it sent)."""
    r = role_of(user)
    if stage(meta) != "production":
        raise WorkflowError("the file is not in production")
    if meta.get("work") == "on_hold":
        raise WorkflowError("the file is on hold")
    if r not in ("production", "admin"):
        raise WorkflowError("only production sends a file to QA", 403)
    if r == "production" and not can_edit(meta, user):
        raise WorkflowError("this file is not yours", 403)
    c = {**DEFAULTS, **(cfg or {})}
    missing = check_ready_for_qa(meta, c, pac, built, dirty)
    if missing:
        raise WorkflowError("Before QA: " + "; ".join(missing))
    w = wf(meta)
    if pac:
        w["pac"] = (w["pac"] + [{**pac, "round": w["rework"], "at": now(), "by": user["username"]}])[-30:]
    to = "qa"
    if w["rework"] and w.get("rework_from") == "final_qa" and c["return_to_sender"] and c["final_qa"]:
        to = "final_qa"
    w["checklist"].pop(to, None)            # a fresh check
    return _set(meta, user, to, "sent to " + STAGE_LABELS[STAGE_OF[to]], note,
                pac=(pac or {}).get("name", ""))


def approve(meta: dict, user: dict, cfg: dict | None, note: str = "", built: bool = True,
            dirty: bool = False) -> dict:
    """QA → Final QA (or Delivery); Final QA → Delivery."""
    c = {**DEFAULTS, **(cfg or {})}
    r = role_of(user)
    st = stage(meta)
    w = wf(meta)
    if st == "qa":
        if r not in ("qa", "admin"):
            raise WorkflowError("only QA approves a file in QA", 403)
        if r == "qa" and w["qa_user"] and not _is(w["qa_user"], user):
            raise WorkflowError(f"{w['qa_user']} is checking this file", 403)
        if r == "qa":
            w["qa_user"] = user["username"]
    elif st == "final_qa":
        if r not in ("final_qa", "admin"):
            raise WorkflowError("only Final QA approves a file in Final QA", 403)
        if r == "final_qa" and w["fqa_user"] and not _is(w["fqa_user"], user):
            raise WorkflowError(f"{w['fqa_user']} is checking this file", 403)
        if r == "final_qa":
            w["fqa_user"] = user["username"]
    else:
        raise WorkflowError("the file is not in QA or Final QA")
    left = open_comments(meta)
    if left:
        raise WorkflowError(f"{len(left)} comment(s) are still open — close them or send the file to rework")
    if c["require_checklist"]:
        ck = w["checklist"].get(st) or {}
        todo = [k for k, _ in CHECKLIST if ck.get(k) not in ("ok", "na")]
        if todo:
            raise WorkflowError(f"tick the checklist first ({len(todo)} line(s) left)")
    if c["require_build"] and not built:
        raise WorkflowError("build the accessible PDF first — the approved file is the built one")
    if c["require_build"] and dirty:
        raise WorkflowError("the tags changed after the last build — build the accessible PDF again, then approve")
    # the fixes this stage saw are verified now
    for x in w["comments"]:
        if x.get("status") == "fixed":
            x["status"] = "closed"
            x["closed_by"] = user["username"]
            x["closed_at"] = now()
    to = "approved" if st == "final_qa" or not c["final_qa"] else "final_qa"
    return _set(meta, user, to, f"{STAGE_LABELS[st]} approved", note)


def rework(meta: dict, user: dict, note: str = "") -> dict:
    """QA / Final QA → Production: rework stage-1, stage-2 …"""
    r = role_of(user)
    st = stage(meta)
    w = wf(meta)
    if st not in ("qa", "final_qa", "delivery", "delivered"):
        raise WorkflowError("only a file in QA, Final QA or Delivery goes to rework")
    if st == "delivered" and r != "admin":
        raise WorkflowError("only an admin sends a delivered file to rework", 403)
    if r not in ("admin", "qa", "final_qa") or (r == "qa" and st != "qa") or (r == "final_qa" and st != "final_qa"):
        raise WorkflowError("you cannot send this file to rework", 403)
    holder = w["qa_user"] if st == "qa" else w["fqa_user"] if st == "final_qa" else ""
    if r != "admin" and holder and not _is(holder, user):
        raise WorkflowError(f"{holder} is checking this file", 403)
    if r == "qa":
        w["qa_user"] = user["username"]
    if r == "final_qa":
        w["fqa_user"] = user["username"]
    if not open_comments(meta) and not (note or "").strip():
        raise WorkflowError("add a comment (what to fix) before sending the file to rework")
    w["rework"] = int(w.get("rework") or 0) + 1
    w["rework_from"] = "final_qa" if st in ("final_qa", "delivery", "delivered") else "qa"
    for x in w["comments"]:
        if x.get("status") == "open" and not x.get("sent_round"):
            x["sent_round"] = w["rework"]
    return _set(meta, user, "rework", f"rework stage-{w['rework']}", note,
                comments=len(open_comments(meta)), stage_from=st)


def deliver(meta: dict, user: dict, note: str = "") -> dict:
    if role_of(user) not in ("admin", "final_qa"):
        raise WorkflowError("only an admin or Final QA marks a file delivered", 403)
    if (meta.get("work") or "") != "approved":
        raise WorkflowError("only a file ready for delivery is delivered")
    return _set(meta, user, "delivered", "delivered", note)


def hold(meta: dict, user: dict, on: bool, note: str = "") -> dict:
    if role_of(user) != "admin":
        raise WorkflowError("only an admin puts a file on hold", 403)
    w = wf(meta)
    if on:
        if meta.get("work") == "on_hold":
            raise WorkflowError("the file is on hold already")
        w["hold_from"] = meta.get("work") or ""
        return _set(meta, user, "on_hold", "put on hold", note)
    if meta.get("work") != "on_hold":
        raise WorkflowError("the file is not on hold")
    return _set(meta, user, w.pop("hold_from", "") or "assigned", "taken off hold", note)


def move(meta: dict, user: dict, to: str, note: str = "") -> dict:
    """The admin puts a file at any step."""
    if role_of(user) != "admin":
        raise WorkflowError("only an admin moves a file to any step", 403)
    if to not in WORK_STATES:
        raise WorkflowError("unknown step")
    return _set(meta, user, to, "moved by admin", note)


def set_checker(meta: dict, user: dict, which: str, who: str) -> dict:
    if role_of(user) != "admin":
        raise WorkflowError("only an admin chooses the checker", 403)
    w = wf(meta)
    key = "qa_user" if which == "qa" else "fqa_user"
    old = w.get(key) or ""
    w[key] = who or ""
    return _event(meta, user, f"{'QA' if which == 'qa' else 'Final QA'} checker", old, who or "")


def set_check(meta: dict, user: dict, item: str, value: str) -> dict:
    """A checklist line: ok / na / fail / "" (QA, Final QA, admin)."""
    st = stage(meta)
    if st not in ("qa", "final_qa"):
        raise WorkflowError("the checklist is for QA and Final QA")
    if not can_edit(meta, user):
        raise WorkflowError("this file is not with you", 403)
    if item not in dict(CHECKLIST):
        raise WorkflowError("unknown checklist line")
    if value not in ("ok", "na", "fail", ""):
        raise WorkflowError("ok, na, fail or empty")
    ck = wf(meta)["checklist"].setdefault(st, {})
    if value:
        ck[item] = value
    else:
        ck.pop(item, None)
    return ck


# ------------------------------------------------------------ comments
def add_comment(meta: dict, user: dict, text: str, page: int | None = None, category: str = "Other",
                severity: str = "Minor", tag: str = "") -> dict:
    r = role_of(user)
    st = stage(meta)
    if r not in ("qa", "final_qa", "admin"):
        raise WorkflowError("QA, Final QA and the admin write review comments", 403)
    if r != "admin" and not can_edit(meta, user):
        raise WorkflowError("the file is not with you", 403)
    text = (text or "").strip()
    if not text:
        raise WorkflowError("write the comment first")
    w = wf(meta)
    if r == "qa" and not w["qa_user"]:
        w["qa_user"] = user["username"]
    if r == "final_qa" and not w["fqa_user"]:
        w["fqa_user"] = user["username"]
    c = {"id": uuid.uuid4().hex[:10], "by": user["username"], "role": r, "at": now(), "text": text[:2000],
         "page": int(page) if page not in (None, "") else None,
         "category": category if category in CATEGORIES else "Other",
         "severity": severity if severity in SEVERITIES else "Minor",
         "stage": st if st in ("qa", "final_qa") else ("final_qa" if r == "final_qa" else "qa"),
         "round": int(w.get("rework") or 0) + 1, "status": "open", "edited": False, "tag": (tag or "")[:40]}
    w["comments"].append(c)
    return c


def _comment(meta: dict, cid: str) -> dict:
    for c in wf(meta)["comments"]:
        if c.get("id") == cid:
            return c
    raise WorkflowError("no such comment", 404)


def edit_comment(meta: dict, user: dict, cid: str, **kw) -> dict:
    c = _comment(meta, cid)
    if role_of(user) != "admin" and not _is(c.get("by") or "", user):
        raise WorkflowError("only who wrote a comment edits it", 403)
    changed = False
    if kw.get("text") is not None and kw["text"].strip() and kw["text"].strip() != c["text"]:
        c.setdefault("versions", []).append({"at": c.get("edited_at") or c["at"], "text": c["text"]})
        c["versions"] = c["versions"][-10:]
        c["text"] = kw["text"].strip()[:2000]
        changed = True
    if kw.get("category") in CATEGORIES and kw["category"] != c.get("category"):
        c["category"] = kw["category"]
        changed = True
    if kw.get("severity") in SEVERITIES and kw["severity"] != c.get("severity"):
        c["severity"] = kw["severity"]
        changed = True
    if "page" in kw and kw["page"] not in (None, "") and int(kw["page"]) != c.get("page"):
        c["page"] = int(kw["page"])
        changed = True
    if changed:
        c["edited"] = True
        c["edited_at"] = now()
        c["edited_by"] = user["username"]
    return c


def delete_comment(meta: dict, user: dict, cid: str) -> dict:
    c = _comment(meta, cid)
    if role_of(user) != "admin" and not _is(c.get("by") or "", user):
        raise WorkflowError("only who wrote a comment deletes it", 403)
    wf(meta)["comments"] = [x for x in wf(meta)["comments"] if x.get("id") != cid]
    return c


def comment_status(meta: dict, user: dict, cid: str, status: str, reply: str = "") -> dict:
    """Production marks a comment fixed (with a reply); QA reopens it or
    closes it (verified); a reply alone keeps the status."""
    c = _comment(meta, cid)
    r = role_of(user)
    if status == "fixed":
        if r not in ("production", "admin") and not can_edit(meta, user):
            raise WorkflowError("production marks a comment fixed", 403)
        c["status"] = "fixed"
        c["fixed_by"] = user["username"]
        c["fixed_at"] = now()
    elif status in ("open", "closed"):
        if r not in ("qa", "final_qa", "admin"):
            raise WorkflowError("QA reopens or closes a comment", 403)
        c["status"] = status
        c[f"{status}_by"] = user["username"]
        c[f"{status}_at"] = now()
    elif status != "reply":
        raise WorkflowError("fixed, open, closed or reply")
    if (reply or "").strip():
        c.setdefault("replies", []).append({"by": user["username"], "role": r, "at": now(), "text": reply.strip()[:1000]})
    return c


# ------------------------------------------------------------ for the lists
def summary(meta: dict) -> dict:
    """The workflow fields a file list needs."""
    w = meta.get("wf") or {}
    cm = w.get("comments") or []
    pac = w.get("pac") or []
    st = stage(meta)
    return {"wstage": st, "wstage_label": STAGE_LABELS.get(st, st), "rework": int(w.get("rework") or 0),
            "qa_user": w.get("qa_user") or "", "fqa_user": w.get("fqa_user") or "",
            "comments_open": sum(1 for c in cm if c.get("status") == "open"),
            "comments_fixed": sum(1 for c in cm if c.get("status") == "fixed"),
            "comments": len(cm), "pac": len(pac),
            "pac_last": ({k: pac[-1].get(k) for k in ("name", "at", "by", "result", "errors", "round")} if pac else None),
            "stage_since": (w.get("history") or [{}])[-1].get("at", "") if w.get("history") else ""}


def time_in_stages(meta: dict) -> dict:
    """Seconds spent in each stage, from the history (the current one up to now)."""
    hist = (meta.get("wf") or {}).get("history") or []
    out: dict = {}
    for a, b in zip(hist, hist[1:] + [{"at": now()}]):
        try:
            d = (datetime.fromisoformat(b["at"]) - datetime.fromisoformat(a["at"])).total_seconds()
        except Exception:
            continue
        st = STAGE_OF.get(a.get("to") or "", "production")
        out[st] = out.get(st, 0) + max(0, d)
    return out


# ------------------------------------------------------------ queues (who does what next)
def _queue_key(meta: dict):
    """Urgent first, then the earliest due date, then who waited longest."""
    w = meta.get("wf") or {}
    since = (w.get("history") or [{}])[-1].get("at", "") if w.get("history") else (meta.get("created") or "")
    return (PRIORITY_RANK.get(meta.get("priority") or "normal", 2), meta.get("due") or "9999", since)


def todo_kind(meta: dict, user: dict | None) -> str:
    """What this file asks of the user now: "" (nothing), "start" (given,
    not started), "work" (theirs, in hand), "rework", "pick" (in their queue,
    nobody's yet), "check" (they are checking it), "deliver"."""
    r = role_of(user)
    work = meta.get("work") or ""
    w = meta.get("wf") or {}
    if work == "on_hold":
        return ""
    if r in ("production", "admin") and stage(meta) == "production":
        a = meta.get("assigned_to") or ""
        mine = _is(a, user) or (not a and r == "production" and _is(meta.get("owner") or "", user))
        if not mine:
            return ""
        if work in ("", "assigned", "returned"):
            return "start"
        return "rework" if work == "rework" else "work"
    if r == "qa" and stage(meta) == "qa":
        if not w.get("qa_user"):
            return "pick"
        return "check" if _is(w["qa_user"], user) else ""
    if r == "final_qa":
        if stage(meta) == "final_qa":
            if not w.get("fqa_user"):
                return "pick"
            return "check" if _is(w["fqa_user"], user) else ""
        if work == "approved" and _is(w.get("fqa_user") or "", user):
            return "deliver"
    return ""


def my_queue(user: dict | None, metas: list[dict]) -> dict:
    """Counts for the header badge and the order to take files in."""
    out = {"start": 0, "work": 0, "rework": 0, "pick": 0, "check": 0, "deliver": 0}
    for m in metas:
        k = todo_kind(m, user)
        if k:
            out[k] += 1
    out["total"] = sum(out.values())
    return out


def next_for(user: dict | None, metas: list[dict]) -> dict | None:
    """The file the user should take next: their own work first (rework, in
    progress, given), then the queue (urgent, due date, waited longest)."""
    order = {"rework": 0, "check": 1, "work": 2, "start": 3, "pick": 4}
    cand = [(order[k], _queue_key(m), m) for m in metas for k in [todo_kind(m, user)] if k in order]
    if not cand:
        return None
    cand.sort(key=lambda x: (x[0], x[1]))
    return cand[0][2]


def pick_checker(which: str, users: list[str], metas: list[dict]) -> str:
    """The QA (or Final QA) user with the fewest files waiting with them."""
    if not users:
        return ""
    key = "qa_user" if which == "qa" else "fqa_user"
    load = {u.lower(): 0 for u in users}
    for m in metas:
        if stage(m) != which:
            continue
        who = ((m.get("wf") or {}).get(key) or "").lower()
        if who in load:
            load[who] += max(1, int(m.get("pages") or 1))
    return min(users, key=lambda u: (load[u.lower()], u.lower()))
