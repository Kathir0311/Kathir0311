"""Dezquare PDF Accessibility Studio — web app.

Automated PDF/UA tagging with an Acrobat-style manual editor:
  upload → preflight + analysis (or import of existing tags) → review/edit → build.
"""
from __future__ import annotations

import collections
import contextlib
import gzip
import io
import json
import os
import re
import queue
import shutil
import sys
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime

import pymupdf
from flask import (Flask, abort, g, has_request_context, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from werkzeug.utils import secure_filename

from core import checker, edit
from core.compat import use as use_engine
from core.ai import (AIConfig, OllamaError, alt_text, field_label, heading_levels,
                     installed_models,
                     link_description, math_from_image, ping, review_roles, table_summary, set_wait_hook)
from core.lint import lint, table_grid
from core.model import (ARTIFACT_TYPES, EDITOR_ROLES, LIST_NUMBERING, PAGINATION_SUBTYPES,
                        DocModel, node_dict)
from core.options import BEST_PROFILE, BUILTIN_PROFILES, META, SMART_PROFILE, Options, migrate_legacy_texts
from core import contrast as contrast_mod
from core import xref
from core.pipeline import (COLOUR_BACKUP, CONTRAST_BACKUP, save_auto_model, undo_contrast_fixes,
                           ai_alt_text_all, contrast_findings, finish, load_auto_model, load_contrast,
                           load_findings, load_model, prepare, save_contrast, save_findings,
                           save_model)
from core.importer import assess_tags
from core.textflags import printed_text
from core.jobrunner import JobFailed, JobStopped, run_task
from core.editlock import EditLocks, reason as lock_reason
from core.fairqueue import FairQueue
from core.jobindex import JobIndex
from core.modelsync import Shadow
from core import rights as RIGHTS
from core.worktime import WorkTimes, kind_of as time_kind, fmt as time_fmt
from core.locks import FairRLock, replace_file
from core.preflight import tesseract_info
from core.report import write_batch_summary

VERSION = "4.29.0"
BASE = os.path.dirname(os.path.abspath(__file__))
JOBS = os.environ.get("PDFUA_JOBS", os.path.join(BASE, "jobs"))
CONFIG_PATH = os.environ.get("PDFUA_CONFIG", os.path.join(BASE, "config.json"))

# V4.26: the privacy guard — no connection leaves this server's network
from core import netguard as NETGUARD  # noqa: E402
NETGUARD.install(CONFIG_PATH, process="web app")
os.makedirs(JOBS, exist_ok=True)
JOBINDEX = JobIndex(JOBS)            # V4.30: every file's meta, parsed once (core/jobindex.py)
HISTORY_LIMIT = 60

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024

# ============================================================== users (V4.19)
from datetime import timedelta  # noqa: E402

from core.accounts import AccountError, Accounts  # noqa: E402
from core import workflow as WF  # noqa: E402

USERS_PATH = os.environ.get("PDFUA_USERS", os.path.join(os.path.dirname(os.path.abspath(CONFIG_PATH)), "users.json"))
ACCOUNTS = Accounts(USERS_PATH, os.path.join(os.path.dirname(USERS_PATH), "activity.log"))
app.secret_key = ACCOUNTS.secret
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(hours=14))
OPEN_ENDPOINTS = {"login", "static", "logout"}
ADMIN_PREFIXES = ("/admin", "/api/admin/")


def current_user() -> dict | None:
    return getattr(g, "user", None)


def current_user_name() -> str:
    u = current_user()
    return u["username"] if u else ""


def is_admin() -> bool:
    u = current_user()
    return bool(u and u.get("role") == "admin")


def can_see(meta: dict, user: dict | None = None) -> bool:
    """An admin sees every file; production the files they uploaded or were
    given; QA / Final QA the files in their queue and the ones they checked
    (core/workflow.py)."""
    u = user or current_user()
    return WF.can_see(meta, u) or RIGHTS.allowed(u, "see_all")        # V4.30: a person an admin allowed to look at every file


def work_idle_limit() -> int:
    """Admin → Workflow: a pause longer than this many seconds is a break (not counted as work time)."""
    try:
        return max(30, min(900, int(wf_cfg().get("work_idle_s") or 120)))
    except Exception:
        return 120


def wf_cfg() -> dict:
    """The workflow settings (Admin → Workflow)."""
    return {**WF.DEFAULTS, **(load_config().get("workflow") or {})}


# ------------------------------------------------ one person at a time in a file (V4.29, core/editlock.py)
# Whoever opens a file in the editor has it; the others can look, not change. Every request that
# changes something is checked here, before it reaches its endpoint (423 Locked for anybody else).
EDITLOCK = EditLocks(ttl=float(os.environ.get("PDFUA_LOCK_TTL", "40") or 40),
                     idle=60.0 * float(os.environ.get("PDFUA_LOCK_IDLE_MIN", "10") or 10))
LOCKING_METHODS = ("POST", "PUT", "PATCH", "DELETE")
_LOCK_CFG = [0.0, True]


def lock_enabled() -> bool:
    """Admin → Workflow → "One person at a time in a file" (on by default); read at most every 2 s."""
    now = time.time()
    if now - _LOCK_CFG[0] > 2.0:
        try:
            _LOCK_CFG[1] = bool(wf_cfg().get("file_lock", True))
        except Exception:
            _LOCK_CFG[1] = True
        _LOCK_CFG[0] = now
    return _LOCK_CFG[1]


def _same_user(a: str, b: str) -> bool:
    return (a or "").strip().lower() == (b or "").strip().lower()


def lock_view(jid: str) -> dict:
    """The lock of one file as the person asking sees it."""
    me = current_user_name()
    h = EDITLOCK.holder(jid)
    mine = bool(h and _same_user(h["user"], me))
    return {"enabled": lock_enabled(), "holder": (h or {}).get("user", ""), "since": (h or {}).get("since", ""),
            "idle_s": (h or {}).get("idle_s", 0), "mine": mine, "free": h is None,
            "reason": "" if (h is None or mine) else lock_reason(h),
            "can_take_over": is_admin() or RIGHTS.allowed(current_user(), "take_over"),
            "idle_limit_s": int(EDITLOCK.idle)}


def _lock_gate(jid: str):
    """A request that changes a file: refused while somebody else works in it."""
    if not lock_enabled():
        return None
    me = current_user_name()
    h = EDITLOCK.holder(jid)
    if h is not None and not _same_user(h["user"], me):
        if request.endpoint == "api_stop" and is_admin():
            return None                                 # an admin may always stop a job that runs away
        return jsonify({"error": lock_reason(h), "locked": True, "by": h["user"], "since": h["since"]}), 423
    if h is not None:
        EDITLOCK.touch(jid, me)
    elif request.headers.get("X-UA-Edit") == "1":
        EDITLOCK.acquire(jid, me)                       # the editor of a person whose lock ran out takes it again
    return None


# what QA and Final QA may not do: upload, analyse, re-analyse, auto-tag,
# change the profile, delete (V4.23)
ANALYSE_ENDPOINTS = {"upload", "reanalyze", "api_retag", "api_job_profile", "delete_job", "api_job_samples_apply",
                     "api_answer", "api_job_brain_think"}
# V4.30: the right a request needs (core/rights.py) — the role's default unless an admin set it for the person
ENDPOINT_RIGHT = {"upload": "upload", "reanalyze": "reanalyse", "api_retag": "reanalyse", "api_job_profile": "reanalyse",
                  "api_job_samples_apply": "reanalyse", "api_answer": "reanalyse", "api_job_brain_think": "reanalyse",
                  "delete_job": "delete_files", "api_build": "build", "download": "download", "download_pac": "download",
                  "support_page": "download",
                  **{ep: "use_ai" for ep in ("api_ai_alt", "api_ai_alt_all", "api_ai_fields", "api_ai_headings", "api_ai_links",
                                             "api_ai_math", "api_ai_roles", "api_ai_tables", "api_order_ai",
                                             "api_table_summary_ai", "api_actual_text_suggest")}}


def _no_right(user: dict | None, key: str) -> str:
    r = RIGHTS.BY_KEY[key]
    if key in (RIGHTS.explicit(user) or {}):
        return f"You have not been given the right to: {r['label'].lower()}. Ask an admin."
    if key in ("upload", "reanalyse"):
        return "QA and Final QA do not upload or analyse files — they correct and check them"
    return f"Your role does not include: {r['label'].lower()}. An admin can give you the right."
# the steps a file's reader may take without being able to edit it
WF_OPEN_ENDPOINTS = {"api_job_work", "api_job_wf", "api_job_wf_submit", "api_job_wf_comment", "api_lock"}
# V4.29: requests that are not a change of the file (or manage the lock itself): the lock does not hold them back
LOCK_EXEMPT = WF_OPEN_ENDPOINTS | {"api_job_inventory", "api_job_brain_think", "api_actual_text_suggest"}


def _visible_jobs() -> set:
    return {m["id"] for m in list_jobs(100000) if can_see(m)}


def log_action(action: str, jid: str = "", filename: str = "", detail: str = "") -> None:
    try:
        ACCOUNTS.log(current_user_name() or "system", action, jid, filename, detail)
    except Exception:
        pass


@app.after_request
def _privacy_headers(resp):
    """V4.26: the pages may only talk to this server (no CDN, no tracker, no
    online service) — the browser half of the privacy guard."""
    resp.headers.setdefault("Content-Security-Policy", NETGUARD.CSP)
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=(), payment=(), usb=()")
    return resp


@app.before_request
def _auth():
    """Every page and API call needs a signed-in user (or HTTP Basic auth, for
    scripts); a file is only reachable for who may see it; the admin pages
    only for an admin."""
    g.user = None
    if request.endpoint in OPEN_ENDPOINTS:
        return None
    name = session.get("user")
    if name:
        u = ACCOUNTS.get(name)
        if u is not None and u.get("active", True):
            g.user = u
        else:
            session.clear()
    if g.user is None and request.authorization and request.authorization.username:
        g.user = ACCOUNTS.check(request.authorization.username, request.authorization.password or "")
    if g.user is None:
        if request.path.startswith("/api/") or request.method != "GET":
            return jsonify({"error": "sign in first", "login": True}), 401
        return redirect(url_for("login", next=request.full_path if request.query_string else request.path))
    if request.path.startswith(ADMIN_PREFIXES) and g.user.get("role") != "admin":
        if request.path.startswith("/api/"):
            return jsonify({"error": "only an admin can do that"}), 403
        abort(403)
    need = ENDPOINT_RIGHT.get(request.endpoint or "")
    if need and (request.method == "POST" or request.method == "GET" and need == "download") \
            and not RIGHTS.allowed(g.user, need):
        if request.path.startswith("/api/") or request.headers.get("X-Requested-With") == "fetch":
            return jsonify({"error": _no_right(g.user, need), "right": need}), 403
        abort(403)
    jid = (request.view_args or {}).get("jid")
    if jid and g.user.get("role") != "admin":
        p = os.path.join(JOBS, os.path.basename(jid), "meta.json")
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as fh:
                    meta = json.load(fh)
            except Exception:
                meta = {}
            if not can_see(meta):
                if request.path.startswith("/api/"):
                    return jsonify({"error": "this file is not yours"}), 403
                abort(403)
            # a change to the file: only who has it at its step (V4.23)
            if request.method == "POST" and request.endpoint not in WF_OPEN_ENDPOINTS \
                    and not WF.can_edit(meta, g.user):
                return jsonify({"error": "Read only — " + WF.read_only_reason(meta, g.user), "readonly": True}), 403
            # one person at a time in a file (V4.29)
            if request.method in LOCKING_METHODS and request.endpoint not in LOCK_EXEMPT:
                blocked = _lock_gate(secure_filename(jid))
                if blocked is not None:
                    return blocked
            # QA / Final QA correcting a file of the queue: it becomes theirs;
            # production changing a file given to them: it is in progress
            if request.method == "POST" and request.endpoint not in WF_OPEN_ENDPOINTS \
                    and WF.todo_kind(meta, g.user) in ("pick", "start"):
                _claim(os.path.basename(jid))
    elif jid and request.method in LOCKING_METHODS and request.endpoint not in LOCK_EXEMPT \
            and os.path.isdir(os.path.join(JOBS, secure_filename(jid))):
        blocked = _lock_gate(secure_filename(jid))              # an admin is held back by the lock as well
        if blocked is not None:
            return blocked
    return None


def _claim(jid: str) -> None:
    try:
        with job_lock(jid):
            meta = read_meta(jid)
            kind = WF.todo_kind(meta, g.user)
            if kind not in ("pick", "start"):
                return
            ev = WF.start(meta, g.user, wf_cfg())
            write_meta(jid, meta)
        log_action(ev["action"], jid, meta.get("filename", ""), "started correcting" if kind == "pick" else "first change")
    except Exception:
        pass


@app.context_processor
def _user_in_templates():
    u = current_user()
    r = WF.role_of(u) if u else ""
    return {"me": u, "is_admin": is_admin(), "my_role": r, "my_role_label": WF.ROLE_LABELS.get(r, r),
            "can_upload": bool(u) and RIGHTS.allowed(u, "upload"), "my_rights": RIGHTS.effective(u) if u else {}}


@app.route("/login", methods=["GET", "POST"])
def login():
    err = ""
    nxt = request.values.get("next") or "/"
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    if request.method == "POST":
        u = ACCOUNTS.check(request.form.get("username", ""), request.form.get("password", ""))
        if u is None:
            err = "That user name and password do not match (or the account is switched off)."
            try:
                ACCOUNTS.log(request.form.get("username", "")[:40], "login failed")
            except Exception:
                pass
        else:
            session.clear()
            # a sign-in lasts while the browser (and the app's tabs) stay open:
            # no lasting cookie; static/tabguard.js ends it when every tab is closed
            session.permanent = False
            session["user"] = u["username"]
            ACCOUNTS.log(u["username"], "signed in")
            return redirect(nxt)
    return render_template("login.html", error=err, next=nxt, version=VERSION, year=datetime.now().year,
                           fresh=bool(request.args.get("fresh")))


@app.route("/logout")
def logout():
    who = session.get("user", "")
    session.clear()
    if who:
        ACCOUNTS.log(who, "signed out")
    return redirect(url_for("login"))


@app.route("/api/me", methods=["GET", "POST"])
def api_me():
    u = current_user()
    if request.method == "GET":
        return jsonify(Accounts.public(u))
    b = request.get_json(force=True) or {}
    if not ACCOUNTS.check(u["username"], b.get("old") or ""):
        return _err("the current password is not right")
    try:
        ACCOUNTS.update(u["username"], password=b.get("new") or "", name=b.get("name"))
    except AccountError as exc:
        return _err(str(exc))
    log_action("changed own password")
    return jsonify({"ok": True})


@app.before_request
def _fresh_engine():
    """Every request starts on the V4 engine; a job's own options switch it
    (job_opts) — a thread that served a client's job before never carries it
    over into the next request."""
    use_engine("")
    # a request about one job (the Issues list, the model, a table, the links …)
    # works on that job's engine from the start — V4.18: the Issues list of a
    # Kriyadocs job was checked on the V4 engine and offered "Fix empty headers
    # → TD", which this client does not want
    jid = (request.view_args or {}).get("jid")
    if jid:
        try:
            job_opts(jid)
        except Exception:
            use_engine("")


# ============================================================== config
# How this installation is meant to run: the AI on, given time to think, and the
# machine's cores put to work. A fresh config.json starts like this, and an older
# one is brought up to it once (SITE_DEFAULTS_VERSION); after that whatever is
# changed in Settings stays as it is set. An update never ships a config.json, so
# settings and profiles survive it.
SITE_DEFAULTS_VERSION = 4
SITE_AI = {"enabled": True, "timeout": 600, "keep_alive": "30m", "max_image_px": 1100}
SITE_PARALLEL = {"files": 5, "page_procs": 10}
# V4.25: the upload default is no client's profile any more — any PDF is tagged
# by the PDF/UA rules, the local AI and every client's sample files
SITE_DEFAULT_PROFILE = SMART_PROFILE
OLD_DEFAULT_PROFILES = (None, "", "Default", BEST_PROFILE, "Academain")


# what the sample files do (V4.21): Admin → Sample files → Learning
# V4.25: brain — the self-learning brain decides the text tags of profiles that use it ·
# feedback — every built file's fixes teach it · ai_study — the AI studies each sample
# V4.28: zones — where a zone is cut (a paragraph break) is learned from what people change
LEARNING_DEFAULTS = {"enabled": True, "auto_approved": True, "use_ai": True, "rules": True,
                     "brain": True, "feedback": True, "ai_study": True, "zones": True}


def learning_cfg() -> dict:
    return {**LEARNING_DEFAULTS, **(load_config().get("learning") or {})}


def load_config() -> dict:
    cfg = {"ai": {**AIConfig().to_dict(), **SITE_AI}, "verapdf": "", "profiles": {},
           "default_profile": SITE_DEFAULT_PROFILE, "parallel": dict(SITE_PARALLEL),
           "site_defaults": SITE_DEFAULTS_VERSION, "learning": dict(LEARNING_DEFAULTS), "workflow": dict(WF.DEFAULTS)}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                saved = json.load(fh)
            cfg.update({k: v for k, v in saved.items() if k in cfg})
            for k, v in saved.items():
                cfg.setdefault(k, v)       # V4.28: "templates", "privacy" … stay when anything else is saved
            cfg["ai"] = {**AIConfig().to_dict(), **SITE_AI, **(saved.get("ai") or {})}
            if int(saved.get("site_defaults") or 0) < SITE_DEFAULTS_VERSION:
                # once: the AI on, 600 s to answer, 5 files at a time on 10 cores
                cfg["ai"]["enabled"] = True
                cfg["ai"]["timeout"] = max(SITE_AI["timeout"], int(cfg["ai"].get("timeout") or 0))
                # V4.16: the model stays in memory and the pictures are capped,
                # so a drawn zone is answered in seconds, not minutes
                cfg["ai"]["keep_alive"] = cfg["ai"].get("keep_alive") or SITE_AI["keep_alive"]
                cfg["ai"]["max_image_px"] = int(cfg["ai"].get("max_image_px") or SITE_AI["max_image_px"])
                cfg["parallel"] = {**(cfg.get("parallel") or {}), **SITE_PARALLEL}
                if cfg.get("default_profile") in OLD_DEFAULT_PROFILES:
                    # V4.25: Smart AI (all samples) — not a client's own profile
                    cfg["default_profile"] = SITE_DEFAULT_PROFILE
                cfg["site_defaults"] = SITE_DEFAULTS_VERSION
                try:
                    save_config(cfg)
                except Exception:
                    pass
        except Exception:
            pass
    return cfg


# V4.28: many people, several of them admins, change settings at the same time. Every change of
# config.json is "read it, change it, write it back" — done as one step (`config_edit`), so one
# person's change never wipes out another's — and written through a temporary file of its own.
# This lock is a leaf: nothing else is ever waited for while holding it.
CONFIG_LOCK = threading.RLock()


def save_config(cfg: dict) -> None:
    tmp = f"{CONFIG_PATH}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
        replace_file(tmp, CONFIG_PATH)
    finally:
        try:
            os.remove(tmp)                  # only still there when the replace failed
        except OSError:
            pass


@contextlib.contextmanager
def config_edit():
    """`with config_edit() as cfg:` — the settings, changed and written back as one step."""
    with CONFIG_LOCK:
        cfg = load_config()
        yield cfg
        save_config(cfg)


def ai_cfg() -> AIConfig:
    d = load_config()["ai"]
    return AIConfig(**{k: v for k, v in d.items() if k in AIConfig().__dict__})


def all_profiles() -> dict:
    out = {k: dict(v) for k, v in BUILTIN_PROFILES.items()}
    out.update({k: migrate_legacy_texts(dict(v)) for k, v in (load_config().get("profiles") or {}).items()})
    return out


# ============================================ one PyMuPDF user at a time — and nobody else waits (V4.28)
# MuPDF is not thread-safe: editor requests that open PDFs (page images, crops,
# zones, re-tag, contrast) run one after another. Long jobs run in their own
# process (core/jobrunner.py) so they never wait for this lock.
#
# V4.28, for many people at the same time. Before, EVERY request about a file took this one lock —
# a person's page picture made everybody else's "which tag is this" wait for it. Now a request
# waits only for what it needs:
#   * a request that only reads or changes the tags (no PDF: the tag list, the issues, setting a
#     tag, moving one, undo …) waits for its own file only — people working on other files, or
#     rendering a page, do not slow it down;
#   * a page picture that was drawn before is a file read (no lock at all); the geometry of the
#     pages the tag list needs is remembered while the PDF stays the same;
#   * everything else takes the MuPDF lock as before — in the order they came (FairRLock), so none
#     waits for ever while others pass it; and what a request waits for is counted (Admin → System).
# The lock order is always MuPDF first, then the file's: nobody holds the file's lock and waits for
# MuPDF, so the two can never wait for each other.
MUPDF_LOCK = FairRLock()
_NO_LOCK = ("/status", "/stop")
# requests that read or change the tags of a file and never open its PDF
PURE_EDITS = {"api_node", "api_move", "api_levels", "api_bulk", "api_order", "api_undo", "api_actual_text",
              "api_headers_fix", "api_order_fix"}
PURE_READS = {"api_job_summary", "api_findings", "api_job_info", "api_lint", "api_job_brain",
              "api_actualtext_tags", "api_zones_similar", "api_job_wf", "api_xref"}
_req_locks: dict[str, FairRLock] = {}
_req_guard = threading.Lock()
LOCK_AUDIT = os.environ.get("PDFUA_LOCK_AUDIT", "") == "1"      # tests: a request that opens a PDF unlocked fails
LOCK_NOTES: dict[str, int] = {}


def req_lock(jid: str) -> FairRLock:
    """What a request about one file holds while it reads or changes that file's tags."""
    with _req_guard:
        lk = _req_locks.get(jid)
        if lk is None:
            lk = _req_locks[jid] = FairRLock()
        return lk


_JOB_FLAGS: dict[str, tuple] = {}


def _job_is_house(jid: str) -> bool:
    """A job with a client house style: an edit also reads its PDF (core/house.py)."""
    p = os.path.join(JOBS, secure_filename(jid), "meta.json")
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return True
    hit = _JOB_FLAGS.get(jid)
    if hit and hit[0] == mt:
        return hit[1]
    try:
        with open(p, encoding="utf-8") as fh:
            meta = json.load(fh)
        house = (str((meta.get("options") or {}).get("house_style") or "").strip().lower() not in ("", "none", "off"))
    except Exception:
        house = True
    _JOB_FLAGS[jid] = (mt, house)
    return house


_geom: dict[str, tuple] = {}


def _pdf_key(path: str):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _geometry_cached(jid: str) -> bool:
    hit = _geom.get(jid)
    return bool(hit and hit[0] == _pdf_key(os.path.join(JOBS, secure_filename(jid), "working.pdf")))


def page_geometry(jid: str):
    """(display matrices, page sizes) of the job's working PDF — read once, kept while the file stays
    the same (an editor load used to open the PDF for it every time). Opens the PDF when it is not
    kept: the caller then holds the MuPDF lock."""
    path = os.path.join(job_dir(jid), "working.pdf")
    key = _pdf_key(path)
    hit = _geom.get(jid)
    if hit and hit[0] == key:
        return hit[1], hit[2]
    doc = pymupdf.open(path)
    try:
        mats = [tuple(edit.display_matrix(p)) for p in doc]
        sizes = [[round(p.rect.width, 2), round(p.rect.height, 2)] for p in doc]
    finally:
        doc.close()
    _geom[jid] = (key, mats, sizes)
    return mats, sizes


def _page_zoom(default: float = 1.5) -> float:
    try:
        z = float(request.args.get("z", default))
    except ValueError:
        z = default
    return round(min(max(z, 0.2), 8.0), 2)


def _page_source(jid: str) -> str:
    src = "accessible.pdf" if request.args.get("v") == "out" else "working.pdf"
    if not os.path.exists(os.path.join(job_dir(jid), src)):
        src = "original.pdf"
    return src


def _page_cache_file(jid: str, pno: int, zoom: float, src: str) -> str | None:
    key = _pdf_key(os.path.join(JOBS, secure_filename(jid), src))
    if key is None:
        return None
    return os.path.join(JOBS, secure_filename(jid), "cache", f"p{pno}_{src[:3]}_{int(round(zoom * 100))}_{key[0]}.png")


def _lock_plan() -> tuple[bool, bool, str]:
    """(needs the MuPDF lock, needs its file's lock, the file) for this request."""
    p = request.path
    if not (p.startswith("/api/job/") or p.startswith("/job/")) or p.endswith(_NO_LOCK) or p.endswith("/delete"):
        return False, False, ""
    ep = request.endpoint or ""
    jid = (request.view_args or {}).get("jid", "")
    if not jid:
        return True, False, ""
    try:
        if ep == "api_presence":
            return False, False, jid                  # who is here: a few bytes of memory, waits for nobody
        if ep == "page_png":
            f = _page_cache_file(jid, int((request.view_args or {}).get("pno", 0)), _page_zoom(), _page_source(jid))
            if f and os.path.exists(f):
                return False, False, jid               # drawn before: a file read
            return True, False, jid
        if ep == "api_contrast" and request.method == "GET":
            return (not _geometry_cached(jid)), True, jid       # V4.30: the kept page geometry; the PDF only when it is not kept
        if ep == "api_model":
            m = get_model(jid)
            cold = not m.info.get("cell_zones_checked") and any(
                n.role in ("TD", "TH") and n.bbox is None for n in m.nodes.values())
            if _geometry_cached(jid) and not cold and not _job_is_house(jid):
                return False, True, jid
            return True, True, jid
        if (ep in PURE_EDITS or (ep in PURE_READS and request.method == "GET")) and not _job_is_house(jid):
            return False, True, jid
    except Exception:
        pass
    return True, True, jid


@app.before_request
def _mupdf_lock_acquire():
    mupdf, job, jid = _lock_plan()
    g.locks = []
    g.lock_plan = (("mupdf " if mupdf else "") + ("file" if job else "")).strip() or "none"
    if mupdf:
        MUPDF_LOCK.acquire()
        g.locks.append(MUPDF_LOCK)
    if job and jid:
        lk = req_lock(jid)
        lk.acquire()
        g.locks.append(lk)
        g.req_lock = lk


@app.teardown_request
def _mupdf_lock_release(exc=None):
    for lk in reversed(g.pop("locks", None) or []):
        try:
            lk.release()
        except RuntimeError:
            pass


@contextmanager
def _mupdf_free_while_waiting():
    """Let go of the MuPDF lock — and of this request's file lock — while this thread only waits
    for Ollama, so a zone the model is reading (up to the 600 s timeout) does not freeze every
    other page of the editor. The locks are taken back (MuPDF first) before the PDF or the tags
    are used again."""
    held: list = []
    try:
        rl = g.get("req_lock") if has_request_context() else None
    except Exception:
        rl = None
    for lk in ([rl] if rl is not None else []) + [MUPDF_LOCK]:     # the file's first, MuPDF last
        n = 0
        try:
            while True:
                lk.release()
                n += 1
        except RuntimeError:              # not (or no longer) held by this thread
            pass
        if n:
            held.append((lk, n))
    try:
        yield
    finally:
        for lk, n in sorted(held, key=lambda t: t[0] is not MUPDF_LOCK):   # MuPDF first
            for _ in range(n):
                lk.acquire()


set_wait_hook(_mupdf_free_while_waiting)


def _guarded_open(*a, **k):
    """`pymupdf.open` in a request that did not take the MuPDF lock: take it now (it never should
    happen — the request was thought to need no PDF; a note says which one), or, in the tests, fail."""
    if has_request_context() and not MUPDF_LOCK._is_owned():
        ep = request.endpoint or "?"
        LOCK_NOTES[ep] = LOCK_NOTES.get(ep, 0) + 1
        if LOCK_AUDIT:
            raise RuntimeError(f"{ep} opened a PDF without the MuPDF lock")
        if not MUPDF_LOCK.acquire(timeout=60):
            raise RuntimeError("the PDF is busy — try again in a moment")
        g.locks = [MUPDF_LOCK] + list(g.get("locks") or [])      # released with the request
        if LOCK_NOTES[ep] == 1:
            app.logger.warning("%s opened a PDF without the MuPDF lock (now taken)", ep)
    return _real_open(*a, **k)


_real_open = pymupdf.open
pymupdf.open = _guarded_open


# the static files' own date: a browser never keeps last version's editor.js
ASSET_V = str(int(max([os.path.getmtime(os.path.join(app.static_folder, f))
                       for f in os.listdir(app.static_folder)] or [0])))


@app.context_processor
def _asset_version():
    return {"asset_v": ASSET_V, "app_version": VERSION}


# ================================================================ jobs
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def job_lock(jid: str) -> threading.RLock:
    with _locks_guard:
        if jid not in _locks:
            _locks[jid] = threading.RLock()
        return _locks[jid]


def job_dir(jid: str, must_exist: bool = True) -> str:
    d = os.path.join(JOBS, secure_filename(jid))
    if must_exist and not os.path.isdir(d):
        abort(404)
    return d


def read_meta(jid: str) -> dict:
    with open(os.path.join(job_dir(jid), "meta.json"), encoding="utf-8") as fh:
        return json.load(fh)


def write_meta(jid: str, meta: dict) -> None:
    p = os.path.join(job_dir(jid), "meta.json")
    tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"      # V4.28: never one temporary file for two writers
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=1, ensure_ascii=False, default=str)
    replace_file(tmp, p)
    JOBINDEX.touch(secure_filename(jid))


def update_meta(jid: str, **kw) -> dict:
    with job_lock(jid):
        m = read_meta(jid)
        m.update(kw)
        write_meta(jid, m)
        return m


# V4.30: how long people really work on a file — only the time they are active, never a break (core/worktime.py)
def _times_load(jid: str):
    m = JOBINDEX.get(secure_filename(jid))
    t = (m or {}).get("time")
    return json.loads(json.dumps(t)) if t else None


def _times_save(jid: str, totals: dict) -> None:
    update_meta(jid, time=totals)


def _times_file(jid: str) -> str:
    d = os.path.join(JOBS, secure_filename(jid))
    return os.path.join(d, "worktime.json") if os.path.isdir(d) else ""


TIMES = WorkTimes(_times_load, _times_save, _times_file)


def job_opts(jid: str) -> Options:
    o = Options.from_dict(read_meta(jid).get("options"))
    try:                                   # V4.28: the admin's switch for learning zones
        lc = learning_cfg()
        o.learn_zones = bool(o.learn_zones and lc["enabled"] and lc.get("zones", True))
    except Exception:
        pass
    # this request works on this job: the V4 engine, or its client's house
    # style engine (core/compat.py)
    use_engine(o)
    return o


# ---------------------------------------------------------- history
def _hist(jid: str, kind: str) -> str:
    d = os.path.join(job_dir(jid), "history", kind)
    os.makedirs(d, exist_ok=True)
    return d


def _stack(jid: str, kind: str) -> list[str]:
    d = _hist(jid, kind)
    return sorted(os.listdir(d))


def _push(jid: str, kind: str, src: str) -> None:
    d = _hist(jid, kind)
    items = sorted(os.listdir(d))
    n = int(items[-1].split(".")[0]) + 1 if items else 1
    dst = os.path.join(d, f"{n:06d}.json")
    try:
        if os.path.exists(dst):
            os.remove(dst)
        os.link(src, dst)             # V4.30: no copy of the whole file — model.json is replaced, never written in place
    except OSError:
        shutil.copyfile(src, dst)
    for old in items[:-HISTORY_LIMIT]:
        os.remove(os.path.join(d, old))


def _clear(jid: str, kind: str) -> None:
    shutil.rmtree(_hist(jid, kind), ignore_errors=True)


# V4.28: the tags of the files people have open stay in memory — the 24 used last (PDFUA_MODEL_CACHE).
# A server that runs for weeks with many people would otherwise keep every file anybody ever opened.
MODEL_CACHE_MAX = max(4, int(os.environ.get("PDFUA_MODEL_CACHE", "24") or 24))
_model_cache: "collections.OrderedDict[str, tuple[float, DocModel]]" = collections.OrderedDict()
_mc_lock = threading.Lock()


# V4.30: next to the tags of a file in memory, a copy of how they were when last saved (core/modelsync.py) —
# the difference after an edit says which tags to write again and which rows the editor must change.
_SHADOWS: "collections.OrderedDict[str, Shadow]" = collections.OrderedDict()
SHADOW_EVERY = 40            # every 40th save starts the copy afresh (a safety net, not a need)


def _cache_model(jid: str, mt: float, model: DocModel) -> None:
    with _mc_lock:
        _model_cache[jid] = (mt, model)
        _model_cache.move_to_end(jid)
        while len(_model_cache) > MODEL_CACHE_MAX:
            gone, _ = _model_cache.popitem(last=False)
            _SHADOWS.pop(gone, None)
        while len(_SHADOWS) > MODEL_CACHE_MAX:
            _SHADOWS.popitem(last=False)


def _shadow_of(jid: str, model: DocModel) -> Shadow:
    """The copy that belongs to this very model object (a model loaded again gets a new copy)."""
    with _mc_lock:
        sh = _SHADOWS.get(jid)
        if sh is not None and sh.owner is model and sh.saves < SHADOW_EVERY:
            _SHADOWS.move_to_end(jid)
            return sh
    sh = Shadow(model, model)
    with _mc_lock:
        _SHADOWS[jid] = sh
    return sh


# V4.30: the version of a file's tags. The file's date alone can be the same for two saves in the same instant,
# so every change made through mutate() also counts up.
_REV: dict[str, int] = {}


def get_model(jid: str) -> DocModel:
    """model.json, cached in memory while the file is unchanged."""
    path = os.path.join(job_dir(jid), "model.json")
    mt = os.path.getmtime(path)
    with _mc_lock:
        hit = _model_cache.get(jid)
        if hit and hit[0] == mt:
            _model_cache.move_to_end(jid)
            return hit[1]
    model = load_model(job_dir(jid))
    _cache_model(jid, mt, model)
    return model


class Busy(Exception):
    pass


@app.errorhandler(Busy)
def _busy(exc):
    return jsonify({"error": str(exc)}), 409


@app.errorhandler(WF.WorkflowError)
def _wf_error(exc):
    return jsonify({"error": str(exc)}), getattr(exc, "status", 400)


def _guard_busy(jid: str) -> None:
    m = read_meta(jid)
    st = m.get("stage")
    if (m.get("ai_task") or {}).get("running"):
        raise Busy("AI alt text is running for this file — wait for it to finish (or Stop it).")
    if (m.get("contrast_task") or {}).get("running"):
        raise Busy("Colours are being fixed for this file — wait a moment.")
    if (m.get("brain_task") or {}).get("running"):
        raise Busy("The AI brain is deciding the tags of this file — wait for it to finish (or Stop it).")
    if st in ("queued", "processing", "building"):
        raise Busy("This file is being " + ("built" if st == "building" else "analysed") +
                   " — wait for it to finish (or Stop it) before editing.")


@contextmanager
def mutate(jid: str):
    """Snapshot for undo, yield the (cached) model, save.

    V4.30: only what the edit changed is written again, and the editor is told exactly that (g.patch, added to
    the answer by _attach_patch) — so it changes those rows instead of asking for the whole file again."""
    _guard_busy(jid)
    with job_lock(jid):
        path = os.path.join(job_dir(jid), "model.json")
        model = get_model(jid)
        sh = _shadow_of(jid, model)
        if sh.nums is None and has_request_context():
            sh.nums, sh.order = _numbering(model)      # what the editor of this person holds (it loaded this state)
        base = model_rev(jid)
        _push(jid, "undo", path)
        _clear(jid, "redo")
        roles_before = {nid: n.role for nid, n in model.nodes.items()}
        try:
            job_opts(jid)                  # this job's engine for the edit (core/compat.py)
        except Exception:
            pass
        try:
            yield model
        except Exception:
            _model_cache.pop(jid, None)       # discard half-applied edits
            _SHADOWS.pop(jid, None)
            raise
        _house_after_edit(jid, model, roles_before)
        changed, gone, tops = sh.diff(model)
        renumber = bool(gone) or "root_children" in tops or any(_renumbers(sh.nodes.get(nid), model.nodes[nid])
                                                              for nid in changed)
        if gone or "root_children" in tops or any(
                (o := sh.nodes.get(nid)) is None or o["parent"] != model.nodes[nid].parent
                or o["children"] != model.nodes[nid].children for nid in changed):
            model.check_integrity()                # only a change of the structure can leave a loose end
            changed, gone, tops = sh.diff(model)
        sh.commit(model, changed, gone, tops)
        _write_model(jid, model, sh)
        _REV[secure_filename(jid)] = _REV.get(secure_filename(jid), 0) + 1
        _cache_model(jid, os.path.getmtime(path), model)
        rev = model_rev(jid)
        _note_patch(jid, model, sh, base, rev, changed, gone, tops, renumber)
        sh.saves += 1
        _touch_meta_edited(jid)
    try:                                  # V4.28: every change of the tags teaches the tool, a few seconds later
        learn_live(jid)
    except Exception:
        pass


def _write_model(jid: str, model: DocModel, sh: Shadow) -> None:
    """model.json from the kept pieces (core/modelsync.py): the text of every tag that did not change is reused."""
    d = job_dir(jid)
    tmp = os.path.join(d, "model.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(sh.dump(model))
    replace_file(tmp, os.path.join(d, "model.json"))


_EDIT_STAMP: dict[str, float] = {}


def _touch_meta_edited(jid: str) -> None:
    """The file's "last edited by" — written to meta.json at most once in 2 seconds per file while it is marked
    changed already (a person editing quickly rewrote it a dozen times a second for nothing: the page shows
    minutes). The first change after a pause, and the first one after a build, are written at once."""
    now = time.time()
    m = JOBINDEX.get(secure_filename(jid))
    if m is not None and m.get("dirty") and now - _EDIT_STAMP.get(jid, 0.0) < 2.0:
        return
    _EDIT_STAMP[jid] = now
    update_meta(jid, edited=datetime.now().isoformat(timespec="seconds"), dirty=True,
                edited_by=current_user_name() or "")


# ---------------------------------------------------------------- what the editor is told after an edit (V4.30)
PATCH_MAX = 400          # more changed tags than this (or an eighth of the file): the editor asks for the whole file


def _disp(mat, r) -> list:
    a, b, c, d_, e, f = mat
    xs, ys = [], []
    for x, y in ((r[0], r[1]), (r[2], r[1]), (r[0], r[3]), (r[2], r[3])):
        xs.append(a * x + c * y + e)
        ys.append(b * x + d_ * y + f)
    return [round(min(xs), 2), round(min(ys), 2), round(max(xs), 2), round(max(ys), 2)]


def _node_view(n, mats, order: dict, nums: dict) -> dict:
    """One tag as the editor sees it (what GET /model lists for it)."""
    d = node_dict(n)
    for k in ("mcids", "style", "parts", "bbox"):
        d.pop(k, None)
    d["order"] = order.get(n.id, 0)
    d["num"], d["block"] = nums.get(n.id, ("", None))
    rects = []
    if n.bbox is not None and 0 <= n.page < len(mats):
        rects.append([n.page, *_disp(mats[n.page], n.bbox), -1])
    for k, pr in enumerate(n.parts):
        pp = int(pr[0])
        if 0 <= pp < len(mats):
            rects.append([pp, *_disp(mats[pp], pr[1:]), k])
    d["rects"] = rects
    d["src_keys"] = len(n.src_keys)
    d["blank_line"] = bool((n.style or {}).get("blank_line"))
    return d


def _geom_stamp(jid: str) -> str:
    k = _geom.get(secure_filename(jid))
    return "" if not k or k[0] is None else f"{k[0][0]}.{k[0][1]}"


_TOP_TO_EDITOR = {"root_children": "roots", "title": "title", "lang": "lang", "mode": "mode",
                  "warnings": "warnings", "info": "info"}


def _numbering(model: DocModel) -> tuple[dict, dict]:
    """(reading numbers, reading order) of every tag — one walk through the tree."""
    ordered = model.ordered_nodes()
    return edit.reading_numbers(model, ordered), {n.id: k for k, n in enumerate(ordered, start=1)}


def _compute_patch(jid: str, model: DocModel, prev: dict, nums: dict, order: dict, base: str, rev: str,
                   changed: list, gone: list, tops: list, can_undo: bool, can_redo: bool) -> dict | None:
    """What the editor, which holds the file as of `base`, has to change to hold it as of `rev`; None: too much
    (or not known) — it asks for the whole file then."""
    key = secure_filename(jid)
    if not (_geometry_cached(key) or MUPDF_LOCK._is_owned()):
        return None
    mats, sizes = page_geometry(key)
    ids = set(changed)
    derived = {nid: list(nb) for nid, nb in nums.items() if nid not in ids and prev.get(nid) != nb}
    if len(changed) + len(gone) > max(PATCH_MAX, len(model.nodes) // 8):
        return None                      # (a long run of renumbered tags is small: an id and two short values each)
    return {"base": base, "rev": rev, "geom": _geom_stamp(key),
            "nodes": {nid: _node_view(model.nodes[nid], mats, order, nums) for nid in changed},
            "removed": gone, "derived": derived,
            "top": {_TOP_TO_EDITOR[k]: getattr(model, k) for k in tops if k in _TOP_TO_EDITOR},
            "can_undo": can_undo, "can_redo": can_redo}


def _renumbers(old: dict | None, n) -> bool:
    """Can this tag's change alter the reading numbers? They are made of the tree, which tags are artifacts, and
    which containers are 'flow' containers (a paragraph becoming a heading changes none of them)."""
    if old is None or old["artifact"] != n.artifact or old["parent"] != n.parent or old["children"] != n.children:
        return True
    return old["role"] != n.role and (old["role"] in edit.FLOW_ROLES or n.role in edit.FLOW_ROLES)


def _note_patch(jid: str, model: DocModel, sh: Shadow, base: str, rev: str, changed: list, gone: list,
                tops: list, renumber: bool = True) -> None:
    """Work out what the editor has to change for this edit (or that it has to ask for everything) and keep it
    for _attach_patch. The reading numbers are the editor's baseline: the ones it was told last."""
    if not has_request_context():
        sh.nums = None                              # nobody was told: the next edit starts again
        return
    patch: dict | None = None
    try:
        prev = sh.nums
        if renumber or prev is None or sh.order is None:
            nums, order = _numbering(model)
            sh.nums, sh.order = nums, order
        else:
            nums, order = prev, sh.order                # nothing the numbers are made of changed
        if prev is not None:
            patch = _compute_patch(jid, model, prev, nums, order, base, rev, changed, gone, tops, True, False)
    except Exception as exc:                       # never in the way of the edit: the editor asks for everything
        app.logger.warning("patch: %s", exc)
        patch = None
    _add_patch(patch, base, rev)


def _add_patch(p: dict | None, base: str, rev: str) -> None:
    """One request can change a file more than once: the editor gets one patch, from the first base to the last."""
    cur = g.get("patch")
    if cur is None:
        g.patch = p if p is not None else {"base": base, "rev": rev, "reload": True}
        return
    if p is None or cur.get("reload") or p.get("reload"):
        g.patch = {"base": cur["base"], "rev": rev, "reload": True}
        return
    cur["nodes"].update(p["nodes"])
    for nid in p["removed"]:
        cur["nodes"].pop(nid, None)
    cur["removed"] = sorted((set(cur["removed"]) | set(p["removed"])) - set(p["nodes"]))
    for nid in p["nodes"]:
        cur["derived"].pop(nid, None)
    cur["derived"].update(p["derived"])
    cur["top"].update(p["top"])
    for k in ("rev", "geom", "can_undo", "can_redo"):
        cur[k] = p[k]


_COMPRESSIBLE = {"application/json", "text/html", "text/css", "application/javascript", "text/javascript",
                 "image/svg+xml", "text/csv", "text/plain"}
_GZ_STATIC: dict[str, tuple[int, bytes]] = {}


@app.after_request
def _squeeze(resp):
    """V4.30: pages, answers and scripts travel compressed to a browser that accepts it (a 3 MB list of tags is ~300 KB on
    the wire — what matters to people on Wi-Fi, a VPN or a far-away office), and the scripts / styles, whose address
    carries the version (?v=), are kept by the browser for a year instead of being asked about at every page."""
    try:
        if resp.status_code != 200 or resp.headers.get("Content-Encoding"):
            return resp
        if request.endpoint == "static":
            if "v" in request.args:
                resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            if resp.mimetype in _COMPRESSIBLE and _gzip_ok():
                path = os.path.join(app.static_folder, request.view_args.get("filename", ""))
                mt = os.stat(path).st_mtime_ns
                hit = _GZ_STATIC.get(path)
                if hit is None or hit[0] != mt:
                    with open(path, "rb") as fh:
                        hit = _GZ_STATIC[path] = (mt, gzip.compress(fh.read(), 6))
                resp.direct_passthrough = False
                resp.set_data(hit[1])
                resp.headers["Content-Encoding"] = "gzip"
                resp.headers.add("Vary", "Accept-Encoding")
            return resp
        if (not resp.direct_passthrough and not resp.is_streamed and resp.mimetype in _COMPRESSIBLE and _gzip_ok()
                and "Content-Range" not in resp.headers):
            data = resp.get_data()
            if len(data) >= 4096:
                resp.set_data(gzip.compress(data, 1))
                resp.headers["Content-Encoding"] = "gzip"
                resp.headers.add("Vary", "Accept-Encoding")
    except Exception:
        pass
    return resp


@app.after_request
def _attach_patch(resp):
    """The answer of a request that changed the tags carries what changed (see _note_patch)."""
    p = g.get("patch") if has_request_context() else None
    if p and resp.status_code == 200 and resp.mimetype == "application/json":
        try:
            d = json.loads(resp.get_data())
            if isinstance(d, dict) and not d.get("error"):
                d["patch"] = p
                resp.set_data(json.dumps(d))
        except Exception:
            pass
    return resp


def _house_after_edit(jid: str, model, roles_before: dict) -> None:
    """A client's job (a house style in its options): what was drawn or
    converted by hand gets the client's structure as well (core/house.py)."""
    try:
        opts = job_opts(jid)
        changed = {nid for nid, n in model.nodes.items() if roles_before.get(nid) != n.role}
        if not changed:
            return
        if not getattr(opts, "table_thead_tbody", True):
            # the profile keeps rows straight under the Table: a table edit that
            # grouped them (THead / TBody) is undone for every table
            from core.model import unwrap_table_bodies
            unwrap_table_bodies(model)
        if (getattr(opts, "house_style", "") or "").lower() in ("", "none", "off"):
            return
        from core import house
        house.after_edit(model, os.path.join(job_dir(jid), "working.pdf"), opts, changed,
                         gone={nid for nid, r in roles_before.items() if r == "Link" and nid not in model.nodes})
    except Exception as exc:                      # never in the way of the edit itself
        model.warnings.append(f"house style after the edit: {exc}")


# ---------------------------------------------------------------- who else has the file open (V4.28)
# Several people can have the same file open. Every few seconds the editor asks "who is here, and has
# anybody changed the tags since I loaded them?" — so nobody works on a picture that is out of date
# without knowing it. Kept in memory only: it is who is here now, not a record.
PRESENCE: dict[str, dict[str, dict]] = {}
PRESENCE_TTL = 20.0
_presence_lock = threading.Lock()


def model_rev(jid: str) -> str:
    """The version of the tags: changes whenever anything writes them (an edit, an undo, a re-tag, a split).
    V4.30: the file's date and the number of changes made by this server since it started."""
    try:
        return f"{os.stat(os.path.join(job_dir(jid), 'model.json')).st_mtime_ns}.{_REV.get(secure_filename(jid), 0)}"
    except OSError:
        return ""


def presence_here(jid: str, me: str, page: int = 0, touch: bool = True) -> list[dict]:
    now = time.time()
    with _presence_lock:
        people = PRESENCE.setdefault(jid, {})
        if touch:
            people[me] = {"t": now, "page": page}
        for p in [p for p, v in people.items() if now - v["t"] > PRESENCE_TTL]:
            del people[p]
        out = [{"user": p, "page": v["page"] + 1, "ago": int(now - v["t"])} for p, v in sorted(people.items()) if p != me]
        if not people:
            PRESENCE.pop(jid, None)
        return out


def editing_now() -> dict[str, list[str]]:
    """{file: [people who have it open right now]}"""
    now = time.time()
    with _presence_lock:
        return {j: sorted(p for p, v in people.items() if now - v["t"] <= PRESENCE_TTL)
                for j, people in PRESENCE.items() if any(now - v["t"] <= PRESENCE_TTL for v in people.values())}


# ---------------------------------------------------------------- how busy the server is (V4.28)
def load_stats() -> dict:
    """Admin → System: is the one-at-a-time lock, the work queue or the learner a bottleneck?"""
    out: dict = {"mupdf": MUPDF_LOCK.snapshot(), "queue": TASKS.snapshot(), "people_waiting": TASKS.waiting_by(),
                 "workers": TARGET_FILES, "running": len(RUNNING), "editing": editing_now(),
                 "file_locks": {"on": lock_enabled(), **EDITLOCK.summary(), "held": EDITLOCK.snapshot()}}
    try:
        out["threads"] = threading.active_count()
        out["web_threads"] = int(os.environ.get("PDFUA_THREADS", 0) or 0) or None
    except Exception:
        pass
    return out


# ============================================================== worker
# V4.28: the files waiting to be analysed or built are served in turns — one file of each
# person, then the next person's — so one person's forty files never keep everybody else
# waiting; a file marked urgent (or high) goes first, a build somebody is watching goes before
# a new upload of the same priority, and the brain's background training goes last.
_PRIORITY_RANK = {"urgent": 0.0, "high": 1.0, "normal": 2.0, "low": 3.0}
_KIND_RANK = {"build": -0.5, "contrast_fix": -0.5, "ai_alt": -0.5, "brain": 2.0}


def _task_key(item: tuple[str, str]) -> tuple[str, float]:
    jid, kind = item
    try:
        m = read_meta(jid)
    except Exception:
        m = {}
    who = (m.get("owner") or m.get("assigned_to") or "")
    try:
        if has_request_context():                  # the person who pressed the button is the one waiting
            who = current_user_name() or who
    except Exception:
        pass
    rank = _PRIORITY_RANK.get(str(m.get("priority") or "normal"), 2.0) + _KIND_RANK.get(kind, 0.0)
    return str(who).strip().lower(), rank


TASKS = FairQueue(key=_task_key)


def _progress(jid: str):
    def cb(frac: float, msg: str):
        try:
            update_meta(jid, progress=round(max(0.0, min(1.0, frac)), 3), message=msg)
        except Exception:
            pass
    return cb


# jobs currently running: jid -> {"kind", "started", "proc"}
RUNNING: dict[str, dict] = {}
STOP: set[str] = set()
JOB_TIMEOUT = float(os.environ.get("PDFUA_JOB_TIMEOUT_MIN", "480")) * 60


def _run(jid: str, task: dict) -> dict:
    """Run a prepare/build in its own process (PyMuPDF is not thread-safe)."""
    def on_start(proc):
        RUNNING[jid]["proc"] = proc
    return run_task(job_dir(jid), task, _progress(jid), lambda: jid in STOP,
                    timeout_s=JOB_TIMEOUT or None, on_start=on_start)


def run_prepare(jid: str) -> None:
    meta = read_meta(jid)
    opts = Options.from_dict(meta.get("options"))
    if not opts.client:
        opts.client = meta.get("profile") or "Default"      # whose sample files (V4.20)
    lc = learning_cfg()
    opts.use_samples = bool(opts.use_samples and lc["enabled"])
    opts.ai_samples = bool(opts.ai_samples and lc["use_ai"])
    opts.sample_rules = bool(opts.sample_rules and lc["rules"])
    opts.ai_brain = bool(opts.ai_brain and lc["brain"])
    opts.learn_zones = bool(opts.learn_zones and lc["enabled"] and lc.get("zones", True))
    update_meta(jid, stage="processing", progress=0, message="starting", error="")
    cfg = ai_cfg()
    task = {"kind": "prepare", "options": opts.to_dict(), "filename": meta.get("filename", ""),
            "ai": cfg.to_dict() if cfg.enabled else None}
    try:
        r = _run(jid, task)
    except JobStopped:
        update_meta(jid, stage="error", progress=0, message="stopped",
                    error="Stopped by user. Use Re-analyse (or upload again) to start it again.")
        return
    except JobFailed as exc:
        update_meta(jid, stage="error", message="failed", error=str(exc), trace=exc.trace)
        return
    _model_cache.pop(jid, None)
    _clear(jid, "undo")
    _clear(jid, "redo")
    update_meta(jid, stage="review", progress=1, message="ready for review",
                score_before=r["score_before"], mode=r["mode"], assessment=r.get("assessment"),
                preflight=r["preflight"], pages=r["pages"], nodes=r["nodes"],
                title=r["title"], lang=r["lang"], ai_alt=r.get("ai_alt"), dirty=False,
                contrast=r.get("contrast"), contrast_error=r.get("contrast_error", ""),
                samples=r.get("samples"), brain=r.get("brain"))
    if opts.auto_build:
        RUNNING[jid]["kind"] = "build"
        run_build(jid)


def run_build(jid: str) -> None:
    meta = read_meta(jid)
    opts = Options.from_dict(meta.get("options"))
    if not opts.client:
        opts.client = meta.get("profile") or "Default"
    lc = learning_cfg()
    opts.learn_feedback = bool(lc["enabled"] and lc["feedback"])      # V4.25: the fixes teach the brain
    back = "done" if meta.get("built") else "review"
    update_meta(jid, stage="building", progress=0, message="building", build_error="")
    task = {"kind": "build", "options": opts.to_dict(), "filename": meta["filename"],
            "verapdf_exe": checker.find_verapdf(load_config().get("verapdf", "")),
            "preflight_info": meta.get("preflight"), "score_before": meta.get("score_before")}
    try:
        r = _run(jid, task)          # edits are refused while stage == building
    except JobStopped:
        update_meta(jid, stage=back, progress=1, message="build stopped",
                    build_error="Build stopped by user.")
        return
    except Exception as exc:
        # a failed build must never cost the user their edits
        trace = getattr(exc, "trace", "") or traceback.format_exc()[-2500:]
        update_meta(jid, stage=back, progress=1, message="build failed",
                    build_error=f"{exc}\n\n{trace}")
        return
    _model_cache.pop(jid, None)
    update_meta(jid, stage="done", built=True, progress=1, message="built", dirty=False, **r)
    if (r.get("learned") or {}).get("feedback"):
        kick_brain_training()
    try:
        ACCOUNTS.log(meta.get("assigned_to") or meta.get("owner") or "system", "built", jid,
                     meta.get("filename", ""), f"errors {((r.get('score_after') or {}).get('errors'))}")
    except Exception:
        pass


def run_ai_alt(jid: str) -> None:
    cfg = ai_cfg()

    def prog(frac, msg):
        try:
            update_meta(jid, ai_task={"running": True, "progress": round(frac, 3),
                                      "message": msg, "result": None})
        except Exception:
            pass
    prog(0, "starting")
    try:
        r = run_task(job_dir(jid), {"kind": "ai_alt", "ai": cfg.to_dict()}, prog,
                     lambda: jid in STOP, timeout_s=JOB_TIMEOUT or None,
                     on_start=lambda p: RUNNING[jid].__setitem__("proc", p))
    except JobStopped:
        r = {"error": "stopped by user — figures drafted so far were not saved"}
    except Exception as exc:
        r = {"error": str(exc)}
    _model_cache.pop(jid, None)
    update_meta(jid, ai_task={"running": False, "progress": 1, "message": "finished", "result": r},
                dirty=True)


def run_brain(jid: str) -> None:
    """V4.25: the brain decides the text tags of a file again (Client tab → Think again)."""
    meta = read_meta(jid)
    opts = Options.from_dict(meta.get("options"))
    if not opts.client:
        opts.client = meta.get("profile") or "Default"
    lc = learning_cfg()
    opts.use_samples = bool(opts.use_samples and lc["enabled"])
    cfg = ai_cfg()

    def prog(frac, msg):
        try:
            update_meta(jid, brain_task={"running": True, "progress": round(frac, 3), "message": msg, "result": None})
        except Exception:
            pass
    prog(0, "starting")
    try:
        r = run_task(job_dir(jid), {"kind": "brain", "options": opts.to_dict(),
                                    "ai": cfg.to_dict() if (cfg.enabled and lc["use_ai"]) else None}, prog,
                     lambda: jid in STOP, timeout_s=JOB_TIMEOUT or None,
                     on_start=lambda p: RUNNING[jid].__setitem__("proc", p))
    except JobStopped:
        r = {"error": "stopped by user — the tags were not changed"}
    except Exception as exc:
        r = {"error": str(exc)}
    _model_cache.pop(jid, None)
    kw = {"brain": r} if not r.get("error") else {}
    update_meta(jid, brain_task={"running": False, "progress": 1, "message": "finished", "result": r},
                dirty=True, **kw)


def run_contrast_fix(jid: str, req: dict | None) -> None:
    req = req or {}
    ids = req.get("ids")
    fix = req.get("fix") or _fix_settings(job_opts(jid))
    def prog(frac, msg):
        try:
            update_meta(jid, contrast_task={"running": True, "progress": round(frac, 3),
                                            "message": msg, "result": None})
        except Exception:
            pass
    prog(0, "starting")
    try:
        r = run_task(job_dir(jid), {"kind": "contrast_fix", "ids": ids, "fix": fix}, prog,
                     lambda: jid in STOP, timeout_s=JOB_TIMEOUT or None,
                     on_start=lambda p: RUNNING[jid].__setitem__("proc", p))
    except JobStopped:
        r = {"error": "stopped by user"}
    except Exception as exc:
        r = {"error": str(exc)}
    kw = {}
    if r.get("summary"):
        kw["contrast"] = r["summary"]
    if r.get("score_before"):
        kw["score_before"] = r["score_before"]
    update_meta(jid, contrast_task={"running": False, "progress": 1, "message": "finished", "result": r},
                dirty=True, **kw)


CONTRAST_IDS: dict[str, dict] = {}


def _fix_settings(o: Options) -> dict:
    return {"method": o.contrast_fix_method, "stroke_width": o.contrast_stroke_width,
            "stroke_color": o.contrast_stroke_color}


def worker(idx: int = 0) -> None:
    while True:
        if idx >= TARGET_FILES:              # this slot is switched off in Settings
            time.sleep(1.0)
            continue
        try:
            jid, kind = TASKS.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            if jid in STOP or not os.path.isdir(os.path.join(JOBS, secure_filename(jid))):
                if kind == "ai_alt" and jid in STOP:
                    update_meta(jid, ai_task={"running": False, "result": {"error": "stopped"}})
                if kind == "brain" and jid in STOP:
                    update_meta(jid, brain_task={"running": False, "result": {"error": "stopped"}})
                if kind == "contrast_fix" and jid in STOP:
                    update_meta(jid, contrast_task={"running": False, "result": {"error": "stopped"}})
                continue                      # stopped or deleted while queued
            RUNNING[jid] = {"kind": kind, "started": datetime.now().isoformat(timespec="seconds"),
                            "proc": None}
            if kind == "prepare":
                run_prepare(jid)
            elif kind == "build":
                run_build(jid)
            elif kind == "ai_alt":
                run_ai_alt(jid)
            elif kind == "brain":
                run_brain(jid)
            elif kind == "contrast_fix":
                run_contrast_fix(jid, CONTRAST_IDS.pop(jid, None))
        except Exception as exc:
            try:
                update_meta(jid, stage="error", error=str(exc),
                            trace=traceback.format_exc()[-4000:], message="failed")
            except Exception:
                pass
        finally:
            RUNNING.pop(jid, None)
            STOP.discard(jid)
            TASKS.task_done()


def queue_info(jid: str | None = None) -> dict:
    """What is running and where a waiting job sits in the queue."""
    waiting = [j for j, _ in list(TASKS.queue) if j not in STOP]
    running = []
    for j, r in list(RUNNING.items()):
        try:
            m = read_meta(j)
        except Exception:
            m = {}
        running.append({"id": j, "filename": m.get("filename", ""), "kind": r["kind"],
                        "since": r["started"], "message": m.get("message", ""),
                        "progress": m.get("progress", 0)})
    out = {"running": running, "waiting": len(waiting), "workers": TARGET_FILES}
    if jid and jid in waiting:
        out["position"] = waiting.index(jid) + 1
    return out


MAX_FILES = 16
TARGET_FILES = 2
PAGE_PROCS = 1


def parallel_settings(cfg: dict | None = None) -> dict:
    """Files processed at the same time, and CPU cores per file (0 = automatic)."""
    from core.parallel import cpu_count
    cfg = cfg or load_config()
    p = cfg.get("parallel") or {}
    cores = cpu_count()
    files_set = int(p.get("files") or 0)
    procs_set = int(p.get("page_procs") or 0)
    files = files_set or int(os.environ.get("PDFUA_WORKERS", 0) or 0) or max(1, min(4, cores // 2))
    files = max(1, min(MAX_FILES, files))
    procs = procs_set or max(1, min(8, cores // files))
    return {"cores": cores, "files": files, "page_procs": procs,
            "files_setting": files_set, "page_procs_setting": procs_set}


def apply_parallel(cfg: dict | None = None) -> dict:
    global TARGET_FILES, PAGE_PROCS
    s = parallel_settings(cfg)
    TARGET_FILES, PAGE_PROCS = s["files"], s["page_procs"]
    os.environ["PDFUA_PAGE_PROCS"] = str(PAGE_PROCS)   # inherited by every job process
    return s


def start_workers() -> None:
    apply_parallel()
    for i in range(MAX_FILES):
        threading.Thread(target=worker, args=(i,), daemon=True).start()
    # requeue work interrupted by a restart
    for name in sorted(os.listdir(JOBS)):
        p = os.path.join(JOBS, name, "meta.json")
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                m = json.load(fh)
        except Exception:
            continue
        for tk in ("ai_task", "contrast_task", "brain_task"):
            if (m.get(tk) or {}).get("running"):
                m[tk] = {"running": False, "result": {"error": "interrupted by a restart"}}
                with open(p, "w", encoding="utf-8") as fh:
                    json.dump(m, fh)
        if m.get("stage") in ("queued", "processing"):
            m["stage"] = "queued"
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(m, fh)
            TASKS.put((name, "prepare"))
        elif m.get("stage") == "building" or (
                m.get("stage") == "error" and m.get("nodes")
                and os.path.exists(os.path.join(JOBS, name, "model.json"))):
            # prepare had finished (model exists) — the failure was a build
            if m.get("stage") == "error":
                m["build_error"] = m.get("error", "")
                m["error"] = ""
            m["stage"] = "done" if m.get("built") else "review"
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(m, fh)
    JOBINDEX.touch()


# ================================================================ pages
def env_status() -> dict:
    t = tesseract_info()
    return {"tesseract": t.get("available", False), "tess_langs": t.get("langs", []),
            "verapdf": checker.find_verapdf(load_config().get("verapdf", "")),
            "ai_enabled": load_config()["ai"].get("enabled", True), "version": VERSION,
            "parallel": parallel_settings()}


def list_jobs(limit: int = 200, visible: bool = False) -> list[dict]:
    """The newest jobs first; `visible`: only the ones the signed-in user may see.
    V4.30: from the index (core/jobindex.py) — no file is opened unless it changed. The dicts are copies
    of the first level (the nested lists are shared: read them, do not change them)."""
    out = []
    for m in JOBINDEX.metas():
        if visible and not can_see(m):
            continue
        out.append(dict(m))
        if len(out) >= limit:
            break
    return out


# options shown only for some profiles: option -> the profiles that show it.
# Everywhere else the box is there but hidden, so its value still travels with
# the job's options. It is also shown wherever it is already ticked.
# (V4.16: /ActualText on a Span is every client's now, so nothing is hidden.)
CLIENT_ONLY: dict[str, list] = {}


@app.route("/")
def index():
    cfg = load_config()
    groups: dict[str, list] = {}
    defaults = Options().to_dict()
    for key, (label, help_, group, choices) in META.items():
        groups.setdefault(group, []).append({"key": key, "label": label, "help": help_,
                                             "choices": choices, "default": defaults[key],
                                             "type": type(defaults[key]).__name__})
    return render_template("index.html", jobs=list_jobs(visible=True), groups=groups, client_only=CLIENT_ONLY,
                           profiles=all_profiles(), default_profile=cfg.get("default_profile", "Default"),
                           builtin=list(BUILTIN_PROFILES), env=env_status(), cfg=cfg, work_states=WORK_STATES)


@app.route("/upload", methods=["POST"])
def upload():
    files = [f for f in request.files.getlist("pdf") if f and f.filename.lower().endswith(".pdf")]
    wants_json = request.headers.get("X-Requested-With") == "fetch"
    folder = (request.form.get("folder") or "").strip()[:120]
    if folder and not is_admin():
        return jsonify({"error": "only an admin can upload a folder"}), 403
    if not files:
        if wants_json:
            return jsonify({"error": "no PDF in the upload"}), 400
        return redirect(url_for("index"))
    try:
        rel_paths = json.loads(request.form.get("paths_json") or "[]")
    except ValueError:
        rel_paths = []
    # V4.25: no profile named (a script, a folder upload) → the upload default
    # (Smart AI — every client's samples and the brain), not a client's profile
    profile = request.form.get("profile") or load_config().get("default_profile") or "Default"
    if profile not in all_profiles():
        profile = "Default"
    base = dict(all_profiles().get(profile, {}))
    try:
        overrides = json.loads(request.form.get("options_json") or "{}")
    except Exception:
        overrides = {}
    base.update(overrides)
    if len(files) > 1 and request.form.get("batch_auto") == "on":
        base["auto_build"] = True
    opts = Options.from_dict(base)
    batch = re.sub(r"[^0-9a-f]", "", (request.form.get("batch") or "").lower())[:8] if is_admin() else ""
    if not batch:
        batch = uuid.uuid4().hex[:8] if (len(files) > 1 or folder) else ""
    me = current_user_name()
    admin = is_admin()
    # an admin's files wait in the pool until they are given to a user (or
    # straight to one, chosen at upload); a user's files are their own
    give_to = (request.form.get("assign_to") or "").strip() if admin else me
    if give_to and ACCOUNTS.get(give_to) is None:
        give_to = ""
    created = []
    first = None
    for i, f in enumerate(files):
        jid = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        d = job_dir(jid, must_exist=False)
        os.makedirs(d, exist_ok=True)
        f.save(os.path.join(d, "original.pdf"))
        rel = rel_paths[i] if i < len(rel_paths) and isinstance(rel_paths[i], str) else (f.filename or "")
        meta = {"id": jid, "filename": os.path.basename(f.filename.replace("\\", "/")), "batch": batch,
                "created": datetime.now().isoformat(timespec="seconds"),
                "stage": "queued", "progress": 0, "message": "waiting",
                "profile": profile, "options": opts.to_dict(), "built": False,
                "owner": me, "assigned_to": give_to or "", "work": "assigned" if give_to else "",
                "assigned_at": datetime.now().isoformat(timespec="seconds") if give_to else "",
                "priority": "normal", "due": "", "notes": [], "alloc_history": [],
                "batch_name": folder, "rel_path": rel[:300]}
        created.append(jid)
        # "ask": a file that already carries tags is a file somebody has worked
        # on — load those tags or analyse the file again is the author's call,
        # not a guess, so the job waits for an answer before anything is touched
        if opts.existing_tags == "ask" and not opts.auto_build and not folder:
            try:
                a = assess_tags(os.path.join(d, "original.pdf"))
            except Exception:
                a = {"tagged": False}
            if a.get("tagged"):
                meta.update(stage="question", message="this PDF already has tags",
                            assessment={k: a.get(k) for k in ("tagged", "elements", "roles",
                                                              "verdict", "reasons")})
                write_meta(jid, meta)
                first = first or jid
                continue
            meta["options"] = Options.from_dict({**base, "existing_tags": "retag"}).to_dict()
        elif opts.existing_tags == "ask":
            # a folder: nobody is asked file by file — the tool decides
            meta["options"] = Options.from_dict({**base, "existing_tags": "auto"}).to_dict()
        write_meta(jid, meta)
        TASKS.put((jid, "prepare"))
        first = first or jid
    log_action("uploaded", "", "", f"{len(files)} file(s)" + (f" from folder {folder}" if folder else "")
               + (f" for {give_to}" if give_to and give_to != me else ""))
    if wants_json:
        return jsonify({"ok": True, "jobs": created, "batch": batch})
    if len(files) == 1 and not folder:
        return redirect(url_for("editor", jid=first))
    return redirect(url_for("index", batch=batch))


@app.route("/api/job/<jid>/answer", methods=["POST"])
def api_answer(jid):
    """The answer to "this PDF already has tags": load them, or analyse it."""
    b = request.get_json(force=True) or {}
    mode = b.get("mode") if b.get("mode") in ("import", "retag", "auto") else "retag"
    meta = read_meta(jid)
    if meta.get("stage") != "question":
        return _err("this file is not waiting for an answer")
    opts = Options.from_dict(meta.get("options"))
    opts.existing_tags = mode
    update_meta(jid, options=opts.to_dict(), stage="queued", progress=0,
                message="waiting", assessment=None)
    STOP.discard(jid)
    TASKS.put((jid, "prepare"))
    return jsonify({"ok": True, "mode": mode})


@app.route("/job/<jid>")
def editor(jid):
    meta = read_meta(jid)
    ro = WF.read_only_reason(meta, current_user())
    lock = None
    if not ro and lock_enabled():                      # V4.29: opening a file to work in it takes it; ?view=1 only looks
        key = secure_filename(jid)
        if request.args.get("view") != "1":
            EDITLOCK.acquire(key, current_user_name())
        lock = lock_view(key)
        lock["view_only"] = request.args.get("view") == "1" and not lock["mine"]
    t = TIMES.totals(secure_filename(jid), current_user_name())          # V4.30: this person's working time on the file
    work = {"idle_limit": work_idle_limit(), "work_s": round(t["work"], 1), "qc_s": round(t["qc"], 1),
            "kind": time_kind(WF.stage(meta))}
    return render_template("editor.html", meta=meta, roles=EDITOR_ROLES, lock=lock, work=work,
                           readonly=ro,
                           artifact_types=ARTIFACT_TYPES, pagination=PAGINATION_SUBTYPES,
                           numbering=LIST_NUMBERING, env=env_status(), cfg=load_config(),
                           client_only=CLIENT_ONLY,
                           build_meta={k: META[k] for k in META if META[k][2] == "Build"},
                           opts=Options.from_dict(meta.get("options")).to_dict())


@app.route("/job/<jid>/delete", methods=["POST"])
def delete_job(jid):
    meta = read_meta(jid)
    if not is_admin() and (meta.get("owner") or "").lower() != current_user_name().lower():
        abort(403)                 # a file given by the admin stays
    log_action("deleted", jid, meta.get("filename", ""))
    _stop(jid, wait=True)
    shutil.rmtree(job_dir(jid), ignore_errors=True)
    JOBINDEX.touch(secure_filename(jid))
    EDITLOCK.release(secure_filename(jid), force=True)
    return redirect(url_for("index"))


def _stop(jid: str, wait: bool = False) -> str:
    """Stop a waiting or running job. Returns what happened."""
    queued = any(j == jid for j, _ in list(TASKS.queue))
    if jid not in RUNNING and not queued:
        return "idle"
    STOP.add(jid)
    if jid in RUNNING:
        if wait:
            for _ in range(60):
                if jid not in RUNNING:
                    break
                threading.Event().wait(0.25)
        return "stopping"
    try:
        m = read_meta(jid)
        if (m.get("ai_task") or {}).get("running"):
            update_meta(jid, ai_task={"running": False, "result": {"error": "stopped"}})
        if (m.get("contrast_task") or {}).get("running"):
            update_meta(jid, contrast_task={"running": False, "result": {"error": "stopped"}})
        if (m.get("brain_task") or {}).get("running"):
            update_meta(jid, brain_task={"running": False, "result": {"error": "stopped"}})
        if m.get("stage") == "queued":
            update_meta(jid, stage="error", progress=0, message="stopped",
                        error="Stopped by user before it started. Use Re-analyse to queue it again.")
        elif m.get("stage") == "building":
            update_meta(jid, stage="done" if m.get("built") else "review", message="build cancelled")
    except Exception:
        pass
    return "removed from queue"


@app.route("/api/job/<jid>/stop", methods=["POST"])
def api_stop(jid):
    job_dir(jid)
    return jsonify({"ok": True, "result": _stop(jid)})


@app.route("/api/queue")
def api_queue():
    return jsonify(queue_info())


@app.route("/job/<jid>/reanalyze", methods=["POST"])
def reanalyze(jid):
    """Analyse the whole file again, exactly as at upload (current or given options).
    The current tags are kept as model.before_reanalyse.json."""
    body = request.get_json(silent=True) or {}
    meta = read_meta(jid)
    if meta.get("stage") in ("queued", "processing", "building"):
        return _err("the file is busy — wait for it to finish (or Stop it)", 409)
    opts = Options.from_dict({**(meta.get("options") or {}), **(body.get("options") or {})})
    src = os.path.join(job_dir(jid), "model.json")
    if os.path.exists(src):
        shutil.copyfile(src, os.path.join(job_dir(jid), "model.before_reanalyse.json"))
    _model_cache.pop(jid, None)
    STOP.discard(jid)
    update_meta(jid, options=opts.to_dict(), stage="queued", progress=0, message="waiting",
                built=False, error="", build_error="", dirty=False,
                reanalysed=datetime.now().isoformat(timespec="seconds"))
    TASKS.put((jid, "prepare"))
    return jsonify({"ok": True})


@app.route("/batch/<batch>.xlsx")
def batch_summary(batch):
    rows = []
    for m in list_jobs(5000, visible=True):
        if m.get("batch") != batch:
            continue
        sa, sb = m.get("score_after") or {}, m.get("score_before") or {}
        rows.append({"file": m.get("filename"), "pages": m.get("pages"), "mode": m.get("mode"),
                     "errors_before": sb.get("errors"), "errors_after": sa.get("errors"),
                     "warnings_after": sa.get("warnings"),
                     "pass_rate_after": f"{sa.get('percent', '')}%" if sa else "",
                     "figures_without_alt": (m.get("stats") or {}).get("missing_alt", ""),
                     "verapdf": m.get("verapdf", ""), "status": m.get("stage"),
                     "contrast_issues": ((m.get("stats") or {}).get("contrast_summary") or m.get("contrast") or {}).get("open", ""),
                     "output": url_for("download", jid=m["id"], what="pdf", _external=True)})
    buf = os.path.join(JOBS, f"_batch_{secure_filename(batch)}.xlsx")
    write_batch_summary(buf, rows)
    return send_file(buf, as_attachment=True, download_name=f"batch_{batch}_summary.xlsx")


# ================================================================== API
def _page_doc(jid: str) -> pymupdf.Document:
    return pymupdf.open(os.path.join(job_dir(jid), "working.pdf"))


@app.route("/api/jobs")
def api_jobs():
    batch = request.args.get("batch")
    me = current_user()
    locks = EDITLOCK.snapshot() if lock_enabled() else {}
    stamp = etag_of(JOBINDEX.stamp(), _locks_stamp(locks), _run_stamp(), batch, (me or {}).get("username"),
                    (me or {}).get("role"), TIMES.stamp())

    def build():
        waiting = [j for j, _ in list(TASKS.queue) if j not in STOP]
        pend = TIMES.pending_all()
        see_times = is_admin() or RIGHTS.allowed(me, "view_times")
        out = []
        for m in list_jobs(5000 if is_admin() else 1000, visible=True):
            if batch and m.get("batch") != batch:
                continue
            d = {k: m.get(k) for k in ("id", "filename", "stage", "progress", "message",
                                       "score_before", "score_after", "pages", "batch",
                                       "built", "mode", "created", "error", "profile", "edited",
                                       "built_at", "title", "nodes", "dirty", "owner", "assigned_to",
                                       "work", "priority", "due", "batch_name")}
            d["running"] = m.get("id") in RUNNING
            d.update(WF.summary(m))
            d["can_edit"] = WF.can_edit(m, me)
            d["todo"] = WF.todo_kind(m, me)
            tf = _time_fields(m, pend)                          # V4.30: what the person spent on the file (and, with the right, everybody)
            mine_t = next((x for x in tf["time_by"] if x["user"].lower() == current_user_name().lower()), None)
            d["my_work_s"], d["my_qc_s"] = (mine_t["work_s"], mine_t["qc_s"]) if mine_t else (0, 0)
            if see_times:
                d["work_s"], d["qc_s"] = tf["work_s"], tf["qc_s"]
            h = locks.get(secure_filename(m.get("id") or ""))
            if h:
                d["locked_by"], d["lock_mine"] = h["user"], _same_user(h["user"], current_user_name())
            if m.get("id") in waiting:
                d["position"] = waiting.index(m["id"]) + 1
            out.append(d)
        return out
    return serve_cached(LIST_BYTES, ("jobs", (me or {}).get("username"), batch), stamp, build)


@app.route("/api/dashboard")
def api_dashboard():
    """The dashboard's numbers (V4.18): files by status, pages, errors fixed,
    the last two weeks day by day, the profiles in use."""
    from collections import Counter
    jobs = list_jobs(5000, visible=True)
    by_stage = Counter(m.get("stage") or "?" for m in jobs)
    by_profile = Counter(m.get("profile") or "Default" for m in jobs)
    today = datetime.now().date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    per_day = {d: {"uploaded": 0, "built": 0, "pages": 0} for d in days}
    pages = fixed = before = after = built = clean = 0
    for m in jobs:
        pages += int(m.get("pages") or 0)
        sb, sa = m.get("score_before") or {}, m.get("score_after") or {}
        if sa:
            built += 1
            before += int(sb.get("errors") or 0)
            after += int(sa.get("errors") or 0)
            fixed += max(0, int(sb.get("errors") or 0) - int(sa.get("errors") or 0))
            if not int(sa.get("errors") or 0):
                clean += 1
        c = (m.get("created") or "")[:10]
        if c in per_day:
            per_day[c]["uploaded"] += 1
            per_day[c]["pages"] += int(m.get("pages") or 0)
        b = (m.get("built_at") or "")[:10]
        if b in per_day:
            per_day[b]["built"] += 1
    active = sum(by_stage.get(k, 0) for k in ("queued", "processing", "building"))
    return jsonify({"files": len(jobs), "pages": pages, "built": built, "clean": clean,
                    "errors_before": before, "errors_after": after, "errors_fixed": fixed,
                    "active": active, "review": by_stage.get("review", 0) + by_stage.get("question", 0),
                    "failed": by_stage.get("error", 0), "by_stage": dict(by_stage),
                    "by_profile": dict(by_profile.most_common(8)),
                    "days": [{"day": d, **per_day[d]} for d in days],
                    "queue": queue_info()})


@app.route("/api/job/<jid>/summary")
def api_job_summary(jid):
    """One file in detail for the dashboard (V4.18): what was found and fixed."""
    from collections import Counter
    meta = read_meta(jid)
    d = job_dir(jid)
    out = {k: meta.get(k) for k in ("id", "filename", "stage", "profile", "mode", "created", "edited",
                                    "built_at", "pages", "title", "lang", "score_before", "score_after",
                                    "contrast", "error", "build_error", "built", "batch", "dirty",
                                    "owner", "assigned_to", "work", "priority", "due", "batch_name")}
    out["notes"] = (meta.get("notes") or [])[-10:]
    out["states"] = WORK_STATES
    out.update(WF.summary(meta))
    out["applied"] = meta.get("applied") if isinstance(meta.get("applied"), list) else []
    out["review"] = meta.get("review") if isinstance(meta.get("review"), list) else []
    sizes = {}
    for name in ("original.pdf", "accessible.pdf"):
        p = os.path.join(d, name)
        if os.path.exists(p):
            sizes[name.split(".")[0]] = os.path.getsize(p)
    out["sizes"] = sizes
    try:
        model = get_model(jid)
        roles = Counter("Artifact" if n.artifact else n.role for n in model.nodes.values())
        figs = [n for n in model.nodes.values() if n.role == "Figure" and not n.artifact]
        links = [n for n in model.nodes.values() if n.role == "Link" and not n.artifact]
        out["counts"] = {
            "tags": len(model.nodes), "headings": sum(v for k, v in roles.items() if re.fullmatch(r"H\d?", k)),
            "paragraphs": roles.get("P", 0), "figures": len(figs),
            "figures_no_alt": sum(1 for n in figs if not (n.alt or "").strip()),
            "tables": roles.get("Table", 0), "lists": roles.get("L", 0), "links": len(links),
            "links_by_hand": sum(1 for n in links if (n.style or {}).get("manual")),
            "notes": roles.get("Note", 0), "formulas": roles.get("Formula", 0), "artifacts": roles.get("Artifact", 0)}
        out["roles"] = dict(roles.most_common(14))
        issues = lint(model)
        out["issues"] = {"error": sum(1 for i in issues if i["severity"] == "error"),
                         "warning": sum(1 for i in issues if i["severity"] == "warning"),
                         "top": [{"severity": i["severity"], "message": i["message"], "page": i.get("page")}
                                 for i in issues[:6]]}
    except Exception as exc:
        out["counts"] = {}
        out["model_error"] = str(exc)
    return jsonify(out)


@app.route("/api/job/<jid>/status")
def api_status(jid):
    m = read_meta(jid)
    return jsonify({k: m.get(k) for k in ("stage", "progress", "message", "error", "built",
                                          "score_before", "score_after", "stats", "applied",
                                          "review", "verapdf", "dirty", "mode", "assessment",
                                          "preflight", "ai_alt", "build_error", "report_error",
                                          "contrast")} | {"queue": queue_info(jid)})


# V4.30: an answer that only changes when the file changes is built once for each version of it (and compressed
# once): the next person, tab or heartbeat that asks gets the same bytes — or a 304 with nothing in it.
class ByteCache:
    def __init__(self, size: int):
        self.size = size
        self.d: "collections.OrderedDict[tuple, list]" = collections.OrderedDict()
        self.lock = threading.Lock()

    def get(self, key, etag: str):
        with self.lock:
            hit = self.d.get(key)
            if hit is not None and hit[0] == etag:
                self.d.move_to_end(key)
                return hit
        return None

    def put(self, key, etag: str, raw: bytes) -> list:
        hit = [etag, raw, None]
        with self.lock:
            self.d[key] = hit
            while len(self.d) > self.size:
                self.d.popitem(last=False)
        return hit


MODEL_BYTES = ByteCache(6)
LINT_BYTES = ByteCache(24)
LIST_BYTES = ByteCache(64)          # the lists of files (Users page, Admin → Files, the queue badge) — one per person


def etag_of(*parts) -> str:
    import hashlib
    return 'W/"' + hashlib.md5(repr(parts).encode("utf-8")).hexdigest()[:20] + '"'


def _locks_stamp(locks: dict) -> tuple:
    return tuple(sorted((k, v["user"], int(v["idle_s"] // 60)) for k, v in locks.items()))


def _run_stamp() -> tuple:
    return (tuple(sorted(RUNNING)), tuple(j for j, _ in list(TASKS.queue)), tuple(sorted(STOP)))


def _gzip_ok() -> bool:
    return "gzip" in (request.headers.get("Accept-Encoding") or "")


def _bytes_response(raw: bytes, gz: bytes | None, etag: str, mimetype: str = "application/json"):
    use_gz = gz is not None and _gzip_ok()
    resp = app.response_class(gz if use_gz else raw, mimetype=mimetype)
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "private, no-cache"
    resp.headers["Vary"] = "Accept-Encoding"
    if use_gz:
        resp.headers["Content-Encoding"] = "gzip"
    return resp


def serve_cached(cache: ByteCache, key, etag: str, build):
    """`build()` (a JSON-able answer) once for each `etag`; 304 for a browser that holds it already."""
    if request.headers.get("If-None-Match") == etag:
        resp = app.response_class(status=304)
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = "private, no-cache"
        resp.headers["Vary"] = "Accept-Encoding"
        return resp
    hit = cache.get(key, etag)
    if hit is None:
        hit = cache.put(key, etag, json.dumps(build()).encode("utf-8"))
    if hit[2] is None and len(hit[1]) > 8192 and _gzip_ok():
        hit[2] = gzip.compress(hit[1], 3)
    return _bytes_response(hit[1], hit[2], etag)


@app.route("/api/job/<jid>/model")
def api_model(jid):
    key = secure_filename(jid)
    model = get_model(jid)
    if not model.info.get("cell_zones_checked") and any(
            n.role in ("TD", "TH") and n.bbox is None for n in model.nodes.values()):
        # jobs imported before cell zones existed
        with mutate(jid) as m2:
            edit.fill_cell_zones(m2, os.path.join(job_dir(jid), "working.pdf"))
            m2.info["cell_zones_checked"] = True
        model = get_model(jid)
    mats, sizes = page_geometry(jid)             # V4.28: read from the PDF once, not at every load
    rev = model_rev(jid)

    def build():
        order = {n.id: k for k, n in enumerate(model.ordered_nodes(), start=1)}
        nums = edit.reading_numbers(model)
        nodes = {n.id: _node_view(n, mats, order, nums) for n in model.nodes.values()}
        return {"nodes": nodes, "roots": model.root_children, "pages": len(sizes),
                "page_sizes": sizes, "title": model.title, "lang": model.lang,
                "mode": model.mode, "warnings": model.warnings, "info": model.info,
                "can_undo": bool(_stack(jid, "undo")), "can_redo": bool(_stack(jid, "redo")), "rev": rev,
                "geom": _geom_stamp(key)}
    resp = serve_cached(MODEL_BYTES, key, f'W/"{rev}-{_geom_stamp(key)}"', build)
    if resp.status_code == 200 and not (_SHADOWS.get(key) is not None and _SHADOWS[key].owner is model):
        resp.call_on_close(lambda: _warm_shadow(key, model, rev))      # so the first edit is as quick as the next
    return resp


def _warm_shadow(key: str, model: DocModel, rev: str) -> None:
    """After the editor got its tags: the copy for the first edit (core/modelsync.py), made while the person looks."""
    try:
        with req_lock(key):
            if model_rev(key) != rev or get_model(key) is not model:
                return                                 # changed meanwhile: the first edit makes it
            sh = _shadow_of(key, model)
            if sh.nums is None:
                sh.nums, sh.order = _numbering(model)
    except Exception:
        pass


@app.route("/api/job/<jid>/lock", methods=["GET", "POST"])
def api_lock(jid):
    """One person at a time in a file (V4.29). GET: who has it. POST: acquire (take it, or renew),
    release (let go), takeover (an admin takes it from whoever has it)."""
    job_dir(jid)
    jid = secure_filename(jid)
    if request.method == "GET":
        return jsonify({"ok": True, **lock_view(jid)})
    meta = read_meta(jid)
    me, me_name = current_user(), current_user_name()
    act = str((request.get_json(silent=True) or {}).get("action") or "")
    can = lock_enabled() and (WF.can_edit(meta, me) or is_admin())
    if act == "release":
        EDITLOCK.release(jid, me_name)
    elif act == "acquire":
        if not can:
            return jsonify({"ok": False, "error": WF.read_only_reason(meta, me) or "the lock is off", **lock_view(jid)})
        ok, h = EDITLOCK.acquire(jid, me_name)
        if not ok:
            return jsonify({"ok": False, "error": lock_reason(h), **lock_view(jid)})
    elif act == "takeover":
        if not (is_admin() or RIGHTS.allowed(me, "take_over")):
            return _err("only an admin (or a person with the right) takes a file over", 403)
        before = EDITLOCK.holder(jid)
        EDITLOCK.acquire(jid, me_name, force=True)
        if before and not _same_user(before["user"], me_name):
            log_action("took the file over", jid, meta.get("filename", ""), f"from {before['user']}")
    else:
        return _err("unknown step")
    return jsonify({"ok": True, **lock_view(jid)})


@app.route("/api/job/<jid>/presence")
def api_presence(jid):
    """Who else has this file open, and has anybody changed the tags since the editor loaded them? (V4.28)"""
    job_dir(jid)
    jid = secure_filename(jid)
    me = current_user_name() or "?"
    try:
        page = max(0, int(request.args.get("page", 0)))
    except ValueError:
        page = 0
    here = presence_here(jid, me, page)
    rev = model_rev(jid)
    mine = request.args.get("rev", "")
    try:
        meta = read_meta(jid)
    except Exception:
        meta = {}
    lock = None
    if lock_enabled() and not WF.read_only_reason(meta, current_user()):
        if request.args.get("hold") == "1":              # the editor of the person who has the file: "I am still here"
            EDITLOCK.renew(jid, me, active=request.args.get("active") == "1")
        lock = lock_view(jid)
    # V4.30: the seconds this person worked since the last beat (only the active ones: a break is not reported)
    # count for the file when the person is the one working in it — the holder of the lock
    kind = time_kind(WF.stage(meta))
    counting = bool(lock and lock.get("mine")) if lock_enabled() else bool(WF.can_edit(meta, current_user()))
    try:
        sec = float(request.args.get("w") or 0)
    except ValueError:
        sec = 0.0
    if counting and sec > 0:
        TIMES.add(jid, me, kind, sec)
    mine_t = TIMES.totals(jid, me)
    return jsonify({"rev": rev, "here": here, "changed": bool(mine) and bool(rev) and mine != rev,
                    "by": meta.get("edited_by") or "", "at": meta.get("edited") or "", "me": me, "lock": lock,
                    "time": {"mine_s": round(mine_t["work"] + mine_t["qc"], 1), "work_s": round(mine_t["work"], 1),
                             "qc_s": round(mine_t["qc"], 1), "kind": kind, "counting": counting,
                             "idle_limit": work_idle_limit()}})


@app.route("/job/<jid>/page/<int:pno>.png")
def page_png(jid, pno):
    """A page as a picture. V4.28: drawn once for everybody — the picture is kept next to the file
    (named after the PDF's own date, so a changed PDF is drawn again) and a later request, from any
    person, is a file read that waits for nobody; a browser that has it asks "has it changed?"
    and gets a 304."""
    zoom = _page_zoom()                                                 # up to 800 % (V4.21.1)
    src = _page_source(jid)
    f = _page_cache_file(jid, pno, zoom, src)
    etag = f'W/"{os.path.basename(f)}"' if f else ""
    if f and request.headers.get("If-None-Match") == etag and os.path.exists(f):
        resp = app.response_class(status=304)
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = "private, max-age=600"
        return resp
    data = _read_cached(f)
    if data is None:
        with MUPDF_LOCK:
            data = _read_cached(f)                       # twenty people open the same page: the first draws it
            if data is None:
                doc = pymupdf.open(os.path.join(job_dir(jid), src))
                try:
                    if pno < 0 or pno >= doc.page_count:
                        abort(404)
                    # a very big page (a poster) at 800 % is drawn at most ~60 million pixels;
                    # the browser stretches it to the size asked for
                    r = doc[pno].rect
                    zoom = min(zoom, (60_000_000 / max(1.0, r.width * r.height)) ** 0.5)
                    data = doc[pno].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).tobytes("png")
                finally:
                    doc.close()
                if f:
                    _page_cache_put(f, data)
    resp = send_file(io.BytesIO(data), mimetype="image/png")
    resp.headers["Cache-Control"] = "private, max-age=600"
    if etag:
        resp.headers["ETag"] = etag
    return resp


PAGE_CACHE_FILES = 240          # pictures kept for one file (a few tens of MB)


def _read_cached(path: str | None) -> bytes | None:
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _page_cache_put(path: str, data: bytes) -> None:
    """Keep a drawn page: written whole (a reader never sees half a picture); older pictures of the
    same page (the PDF changed) and the oldest ones of a file with very many go."""
    try:
        d = os.path.dirname(path)
        os.makedirs(d, exist_ok=True)
        tmp = path + f".{threading.get_ident()}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        stem = os.path.basename(path).rsplit("_", 1)[0] + "_"
        files = sorted((os.path.getmtime(os.path.join(d, x)), x) for x in os.listdir(d) if x.endswith(".png"))
        for _, x in files:
            if x.startswith(stem) and os.path.join(d, x) != path:
                os.remove(os.path.join(d, x))
        if len(files) > PAGE_CACHE_FILES:
            for _, x in files[:len(files) - PAGE_CACHE_FILES]:
                try:
                    os.remove(os.path.join(d, x))
                except OSError:
                    pass
    except OSError:
        pass


@app.route("/job/<jid>/support/<int:pno>.zip")
def support_page(jid, pno):
    """One page as PDF (original + working) with what the tool detected on it,
    zipped — to send when a page is tagged wrong."""
    import zipfile
    import pikepdf as _pk
    d = job_dir(jid)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ("original.pdf", "working.pdf"):
            src = os.path.join(d, name)
            if not os.path.exists(src):
                continue
            try:
                with _pk.open(src) as pdf:
                    if not 0 <= pno < len(pdf.pages):
                        abort(404)
                    out = _pk.new()
                    out.pages.append(pdf.pages[pno])
                    b = io.BytesIO()
                    out.save(b)
                    z.writestr(f"page{pno + 1}_{name}", b.getvalue())
            except Exception as exc:
                z.writestr(f"page{pno + 1}_{name}.error.txt", str(exc))
        try:
            doc = pymupdf.open(os.path.join(d, "working.pdf"))
            page = doc[pno]
            if page.rotation:
                page.set_rotation(0)
            drs = []
            for x in page.get_drawings():
                drs.append({"rect": [round(v, 2) for v in x["rect"]], "type": x.get("type"),
                            "color": x.get("color"), "fill": x.get("fill"), "width": x.get("width"),
                            "items": [[it[0]] + [str(v) for v in it[1:]] for it in x.get("items", [])][:20]})
            tabs = []
            for t in page.find_tables(strategy="lines").tables:
                tabs.append({"bbox": [round(v, 2) for v in t.bbox], "rows": t.row_count, "cols": t.col_count,
                             "cells": [[round(v, 2) for v in c] if c else None for c in t.cells]})
            z.writestr(f"page{pno + 1}_drawings.json", json.dumps(drs, indent=1, default=str))
            z.writestr(f"page{pno + 1}_tables.json", json.dumps(tabs, indent=1))
            z.writestr(f"page{pno + 1}_text.json", json.dumps(page.get_text("dict"), default=str)[:3_000_000])
            doc.close()
        except Exception as exc:
            z.writestr("detect.error.txt", f"{exc}\n{traceback.format_exc()}")
        try:
            model = get_model(jid)
            nodes = [node_dict(n) for n in model.ordered_nodes() if n.page == pno]
            z.writestr(f"page{pno + 1}_tags.json", json.dumps(nodes, indent=1, default=str))
        except Exception:
            pass
        z.writestr("about.txt", f"Dezquare PDF Accessibility Studio {VERSION}\njob {jid}\npage {pno + 1}\n"
                                f"options {json.dumps(read_meta(jid).get('options'), indent=1)}\n")
    resp = send_file(io.BytesIO(buf.getvalue()), mimetype="application/zip", as_attachment=True,
                     download_name=f"{os.path.splitext(read_meta(jid).get('filename','job'))[0]}_page{pno + 1}_support.zip")
    return resp


@app.route("/job/<jid>/crop/<nid>.png")
def crop_png(jid, nid):
    model = load_model(job_dir(jid))
    n = model.nodes.get(nid)
    if n is None or n.bbox is None:
        abort(404)
    doc = _page_doc(jid)
    page = doc[n.page]
    clip = pymupdf.Rect(n.bbox) * page.transformation_matrix * page.rotation_matrix
    clip.normalize()
    clip = (clip + (-4, -4, 4, 4)) & page.rect
    scale = min(3.0, max(0.5, 520 / max(1.0, clip.width)))
    data = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False).tobytes("png")
    doc.close()
    return send_file(io.BytesIO(data), mimetype="image/png")


def _err(msg: str, code: int = 400):
    return jsonify({"error": msg}), code


@app.route("/api/job/<jid>/node/<nid>", methods=["POST"])
def api_node(jid, nid):
    body = request.get_json(force=True) or {}
    try:
        with mutate(jid) as model:
            if nid not in model.nodes:
                return _err("no such tag", 404)
            edit.set_fields(model, nid, body)
    except edit.EditError as exc:
        return _err(str(exc))
    if "role" in body or "actual_text" in body or "alt" in body:
        learn_live(jid)                       # V4.26: a person's fix teaches the brain right away
    return jsonify({"ok": True})


@app.route("/api/job/<jid>/move", methods=["POST"])
def api_move(jid):
    b = request.get_json(force=True) or {}
    try:
        with mutate(jid) as model:
            if b.get("action") == "drop":
                edit.drop(model, b.get("ids") or [], b.get("target"), b.get("where", "before"))
            else:
                edit.move(model, b.get("id"), b.get("action"), b.get("parent"), b.get("index"))
    except edit.EditError as exc:
        return _err(str(exc))
    return jsonify({"ok": True})


@app.route("/api/job/<jid>/levels", methods=["POST"])
def api_levels(jid):
    """Several tags a level in / out, up / down, or their heading levels (V4.21.4)."""
    b = request.get_json(force=True) or {}
    try:
        with mutate(jid) as model:
            out = edit.levels(model, b.get("ids") or [], str(b.get("action") or ""))
    except edit.EditError as exc:
        return _err(str(exc))
    return jsonify({"ok": True, **out})


@app.route("/api/job/<jid>/bulk", methods=["POST"])
def api_bulk(jid):
    b = request.get_json(force=True) or {}
    cfg = ai_cfg() if b.get("ai") else None
    if cfg is not None and not cfg.enabled:
        cfg = None
    try:
        with mutate(jid) as model:
            new = edit.bulk(model, b.get("ids") or [], b.get("action"), b.get("role"), cfg)
    except edit.EditError as exc:
        return _err(str(exc))
    return jsonify({"ok": True, "id": new})


@app.route("/api/job/<jid>/page_numbers", methods=["POST"])
def api_page_numbers(jid):
    """Mark the selected tags as page numbers: read as "Page 5", tagged P."""
    b = request.get_json(force=True) or {}
    with mutate(jid) as model:
        out = edit.mark_page_numbers(model, b.get("ids") or [], str(b.get("text") or "Page")[:40])
    return jsonify({"ok": True, **out})


@app.route("/api/job/<jid>/merge_parts", methods=["POST"])
def api_merge_parts(jid):
    """Join tags that a page break split (list, table, paragraph …)."""
    b = request.get_json(force=True) or {}
    cfg = ai_cfg() if b.get("ai") else None
    if cfg is not None and not cfg.enabled:
        cfg = None
    if cfg is not None:
        cfg.timeout = int(cfg.timeout or 600)
    try:
        with mutate(jid) as model:
            out = edit.merge_parts(model, b.get("ids") or [], cfg,
                                   str(b.get("ask_ai") or ("auto" if cfg is not None else "never")))
    except edit.EditError as exc:
        return _err(str(exc))
    return jsonify({"ok": True, **out})


@app.route("/api/job/<jid>/create", methods=["POST"])
def api_create(jid):
    b = request.get_json(force=True) or {}
    cfg = ai_cfg() if b.get("ai") else None
    if cfg is not None and not cfg.enabled:
        cfg = None
    if cfg is not None:
        cfg.timeout = int(cfg.timeout or 600)
    # What the vision model says about the zone is asked FIRST, before the job
    # is locked for the edit (V4.16): the model can take a while on a CPU, and
    # the rest of the editor — pages, tags, checks — keeps answering meanwhile.
    answers: dict = {}
    if cfg is not None:
        try:
            answers = edit.zone_ai_answers(get_model(jid), os.path.join(job_dir(jid), "working.pdf"),
                                           int(b.get("page", 0)), b.get("rect"), b.get("role", "P"),
                                           job_opts(jid), cfg) or {}
        except Exception as exc:
            app.logger.warning("zone AI: %s", exc)
            answers = {}
    try:
        with mutate(jid) as model:
            before = set(model.nodes)
            new = edit.create_from_rect(model, os.path.join(job_dir(jid), "working.pdf"),
                                        int(b.get("page", 0)), b.get("rect"), b.get("role", "P"),
                                        bool(b.get("absorb", True)), job_opts(jid),
                                        deep=bool(b.get("deep")), ai_cfg=cfg,
                                        split=(b.get("split") if b.get("split") in ("lines", "paragraphs")
                                               else bool(b.get("split", True))),
                                        ai_answers=answers, inside=bool(b.get("inside", True)))
            node = model.nodes[new]
            role = "Artifact" if node.artifact else node.role
            made = sum(1 for nid in set(model.nodes) - before
                       if model.nodes[nid].parent == node.parent and
                       ("Artifact" if model.nodes[nid].artifact else model.nodes[nid].role) == role) or 1
            note = node.note or ""
    except edit.EditError as exc:
        return _err(str(exc))
    return jsonify({"ok": True, "id": new, "made": made, "role": role, "note": note[-300:],
                    "alt": node.alt or ""})


@app.route("/api/job/<jid>/geometry", methods=["POST"])
def api_geometry(jid):
    b = request.get_json(force=True) or {}
    try:
        with mutate(jid) as model:
            part = b.get("part")
            edit.set_geometry(model, os.path.join(job_dir(jid), "working.pdf"), b.get("id"),
                              None if part is None else int(part), int(b.get("page", 0)), b.get("rect"))
    except edit.EditError as exc:
        return _err(str(exc))
    return jsonify({"ok": True})


@app.route("/api/job/<jid>/rebuild", methods=["POST"])
def api_rebuild(jid):
    """Read a Table or list again from its own zone (deeper, optionally with AI)."""
    b = request.get_json(force=True) or {}
    cfg = ai_cfg() if b.get("ai") else None
    if cfg is not None and not cfg.enabled:
        cfg = None
    if cfg is not None:
        cfg.timeout = int(cfg.timeout or 600)
    try:
        with mutate(jid) as model:
            new = edit.rebuild_structured(model, os.path.join(job_dir(jid), "working.pdf"),
                                          b.get("id"), job_opts(jid), cfg)
    except edit.EditError as exc:
        return _err(str(exc))
    return jsonify({"ok": True, "id": new})


@app.route("/api/job/<jid>/order", methods=["POST"])
def api_order(jid):
    b = request.get_json(force=True) or {}
    try:
        with mutate(jid) as model:
            edit.set_order(model, b.get("id"), int(b.get("order", 1)))
    except (edit.EditError, ValueError) as exc:
        return _err(str(exc))
    return jsonify({"ok": True})


@app.route("/api/job/<jid>/retag", methods=["POST"])
def api_retag(jid):
    b = request.get_json(force=True) or {}
    opts = job_opts(jid)
    try:
        model = get_model(jid)
        pages = edit.parse_pages(str(b.get("pages", "")), model.page_count)
        if b.get("fresh") and len(pages) < model.page_count:
            # Re-analyse: only these pages are read again (V4.21.4)
            from core.analyzer import analyze
            from core.compat import use as use_engine
            use_engine(opts)
            o = Options.from_dict(opts.to_dict())
            o.ai_alt_text_auto = False
            auto = analyze(os.path.join(job_dir(jid), "working.pdf"), o, pages=pages)
            try:                                   # V4.28: these pages' pairs are the new proposal's
                from core import zonelearn
                if getattr(auto, "zone_log", None) is not None:
                    zonelearn.save_pairs(job_dir(jid), auto.zone_log, only_pages=set(pages))
            except Exception:
                pass
        else:
            auto = load_auto_model(job_dir(jid), opts, fresh=bool(b.get("fresh")))
        with mutate(jid) as model:
            r = edit.retag_pages(model, auto, pages)
    except edit.EditError as exc:
        return _err(str(exc))
    return jsonify({"ok": True, "fresh": bool(b.get("fresh")), **r})


@app.route("/api/job/<jid>/tags/remove", methods=["POST"])
def api_tags_remove(jid):
    """Remove every tag of this page, of pages from … to …, or of the whole file (V4.21.4)."""
    b = request.get_json(force=True) or {}
    try:
        model = get_model(jid)
        pages = list(range(model.page_count)) if b.get("all") else \
            edit.parse_pages(str(b.get("pages", "")), model.page_count)
        with mutate(jid) as model:
            removed = edit.clear_pages(model, pages)
    except edit.EditError as exc:
        return _err(str(exc))
    return jsonify({"ok": True, "pages": [p + 1 for p in pages], "removed": removed})


@app.route("/api/job/<jid>/reorder", methods=["POST"])
def api_reorder(jid):
    b = request.get_json(force=True) or {}
    with mutate(jid) as model:
        edit.reorder_page(model, int(b.get("page", 0)), b.get("ids") or [])
    return jsonify({"ok": True})


@app.route("/api/job/<jid>/table/<tid>", methods=["GET", "POST"])
def api_table(jid, tid):
    if request.method == "GET":
        model = load_model(job_dir(jid))
        if tid not in model.nodes:
            return _err("no such tag", 404)
        occ, widths = table_grid(model, tid)
        st = model.nodes[tid].style or {}
        return jsonify({"rows": len(widths), "cols": max(widths or [0]),
                        "regular": len(set(widths)) <= 1,
                        "header_rows": st.get("header_rows", 1), "header_cols": st.get("header_cols", 0),
                        "grid": {f"{r},{c}": v for (r, c), v in occ.items()}})
    b = request.get_json(force=True) or {}
    try:
        with mutate(jid) as model:
            edit.table_headers(model, tid, int(b.get("header_rows", 1)), int(b.get("header_cols", 0)),
                               b.get("mode", "auto"))
    except edit.EditError as exc:
        return _err(str(exc))
    return jsonify({"ok": True})


@app.route("/api/job/<jid>/table/<tid>/grid", methods=["GET", "POST"])
def api_table_grid(jid, tid):
    """Edit table (V4.18): the grid, and one change to it (insert / delete /
    split / merge rows and columns, merge and split cells, TH / TD, a line
    moved on the page)."""
    from core import tableedit
    work = os.path.join(job_dir(jid), "working.pdf")
    if request.method == "GET":
        try:
            return jsonify(tableedit.grid_view(get_model(jid), work, tid))
        except tableedit.TableEditError as exc:
            return _err(str(exc), 404)
    b = request.get_json(force=True) or {}
    try:
        with mutate(jid) as model:
            if b.get("op") == "headers":
                edit.table_headers(model, tid, int(b.get("header_rows", 1)), int(b.get("header_cols", 0)),
                                   b.get("mode", "auto"))
                r = {"ok": True}
            else:
                r = tableedit.apply(model, work, tid, b)
    except (tableedit.TableEditError, edit.EditError) as exc:
        return _err(str(exc))
    try:
        r["grid"] = tableedit.grid_view(get_model(jid), work, tid)
    except tableedit.TableEditError:
        pass
    return jsonify(r)


@app.route("/api/job/<jid>/table/<tid>/summary_ai", methods=["POST"])
def api_table_summary_ai(jid, tid):
    """The local AI drafts the table's summary from its caption and first rows (V4.21)."""
    from core import tableedit
    from core.ai import table_summary
    cfg = ai_cfg()
    if not cfg.enabled:
        return _err("the AI is switched off in Settings")
    m = get_model(jid)
    try:
        g = tableedit.Grid(m, tid)
    except tableedit.TableEditError as exc:
        return _err(str(exc), 404)
    rows = []
    for r in range(min(g.R, 8)):
        rows.append(" | ".join((g.node(c).text or "").strip() for c in sorted(
            [cid for cid, v in g.cells.items() if v[0] == r], key=lambda cid: g.cells[cid][1])))
    t = m.nodes[tid]
    cap = next((m.nodes[c].text for c in t.children if c in m.nodes and m.nodes[c].role == "Caption"), "")
    if not cap:
        sib = m.siblings(tid)
        i = sib.index(tid)
        for j in (i - 1, i + 1):
            if 0 <= j < len(sib) and sib[j] in m.nodes and m.nodes[sib[j]].role == "Caption":
                cap = m.nodes[sib[j]].text
                break
    try:
        text = table_summary(cfg, cap or "", rows[:1][0].split(" | ") if rows else [], rows[1:])
    except Exception as exc:
        return _err(f"the AI did not answer ({exc})")
    return jsonify({"ok": True, "summary": (text or "").strip()[:600]})


@app.route("/job/<jid>/region/<int:pno>.png")
def region_png(jid, pno):
    """A part of a page (display box x0,y0,x1,y1) as a picture — the table or
    list beside its grid in the Edit table / Edit list dialogs (V4.19)."""
    try:
        box = [float(v) for v in (request.args.get("box") or "").split(",")][:4]
        z = min(max(float(request.args.get("z", 2.0)), 0.5), 4.0)
    except ValueError:
        abort(400)
    if len(box) != 4:
        abort(400)
    doc = _page_doc(jid)
    try:
        if not 0 <= pno < doc.page_count:
            abort(404)
        page = doc[pno]
        clip = pymupdf.Rect(box) * ~page.rotation_matrix
        clip.normalize()
        clip = clip & page.cropbox
        pix = page.get_pixmap(matrix=pymupdf.Matrix(z, z), clip=clip, alpha=False)
        data = pix.tobytes("png")
    finally:
        doc.close()
    resp = send_file(io.BytesIO(data), mimetype="image/png")
    resp.headers["Cache-Control"] = "private, max-age=60"
    return resp


@app.route("/api/job/<jid>/list/<lid>/edit", methods=["GET", "POST"])
def api_list_edit(jid, lid):
    """Edit list (V4.19): the list's items, and one change to them."""
    from core import listedit
    work = os.path.join(job_dir(jid), "working.pdf")
    if request.method == "GET":
        try:
            return jsonify(listedit.view(get_model(jid), work, lid))
        except listedit.ListEditError as exc:
            return _err(str(exc), 404)
    b = request.get_json(force=True) or {}
    try:
        with mutate(jid) as model:
            r = listedit.apply(model, work, lid, b)
            top = lid if lid in model.nodes else None
            if top is None and r.get("gone"):
                top = None
    except (listedit.ListEditError, edit.EditError) as exc:
        return _err(str(exc))
    if lid in get_model(jid).nodes:
        try:
            r["view"] = listedit.view(get_model(jid), work, lid)
        except listedit.ListEditError:
            pass
    return jsonify(r)


@app.route("/api/templates", methods=["GET", "POST"])
def api_templates():
    """Table and list templates, kept for every file and every user (V4.19)."""
    from core import templates as tpl
    if request.method == "GET":
        return jsonify(tpl.listing(load_config()))
    b = request.get_json(force=True) or {}
    kind = str(b.get("kind") or "")
    if kind not in ("table", "list"):
        return _err("a template is for a table or a list")
    if b.get("delete"):
        with config_edit() as cfg:
            tpl.store(cfg)[kind].pop(str(b.get("number")), None)
        return jsonify({"ok": True, **tpl.listing(cfg)})
    jid, nid = str(b.get("job") or ""), str(b.get("id") or "")
    try:
        number = int(b.get("number") or 0)
    except ValueError:
        return _err("give the template a number")
    try:
        job_dir(jid)
        if jid not in _visible_jobs():
            return _err("no such file", 404)
        job_opts(jid)
        model = get_model(jid)
        work = os.path.join(job_dir(jid), "working.pdf")
        if kind == "table":
            spec = tpl.table_spec(model, work, nid)
        else:
            from core import listedit
            spec = listedit.list_spec(model, work, nid)
    except Exception as exc:
        return _err(f"could not read that {kind}: {exc}")
    spec.update({"name": str(b.get("name") or "")[:80], "by": current_user_name(),
                 "made": datetime.now().isoformat(timespec="seconds"), "from": f"{read_meta(jid).get('filename', '')}"})
    with config_edit() as cfg:               # the next free number is picked here, so two people never share one
        store = tpl.store(cfg)
        if number < 1:
            number = max([int(k) for k in store[kind]] or [0]) + 1
        spec["number"] = number
        store[kind][str(number)] = spec
    return jsonify({"ok": True, "number": number, "summary": tpl.summary(spec), **tpl.listing(cfg)})


@app.route("/api/job/<jid>/templates/apply", methods=["POST"])
def api_templates_apply(jid):
    """Give a table or list (or a zone drawn on the page) template #N's structure."""
    from core import templates as tpl
    from core import listedit
    b = request.get_json(force=True) or {}
    kind = str(b.get("kind") or "")
    if b.get("mark"):
        spec = (_read_marks(jid).get(kind) or {}).get(str(b.get("number")))
        if spec is None:
            return _err(f"no {kind} is marked #{b.get('number')} — select one and press Ctrl+{b.get('number')}")
    else:
        spec = (tpl.store(load_config()).get(kind) or {}).get(str(b.get("number")))
    if spec is None:
        return _err(f"there is no {kind} template #{b.get('number')}")
    work = os.path.join(job_dir(jid), "working.pdf")
    try:
        with mutate(jid) as model:
            nid = str(b.get("id") or "") or None
            page = int(b["page"]) if b.get("page") is not None else None
            if kind == "table":
                if nid and model.nodes.get(nid) is not None and model.nodes[nid].role != "Table":
                    nid = next((a.id for a in model.ancestors(nid) if a.role == "Table"), None)
                r = tpl.apply_table_spec(model, work, spec, tid=nid, page=page, rect=b.get("rect"), opts=job_opts(jid))
            else:
                if nid and model.nodes.get(nid) is not None and model.nodes[nid].role != "L":
                    nid = next((a.id for a in reversed(model.ancestors(nid)) if a.role == "L"), None)
                r = {"ok": True, "id": listedit.apply_spec(model, work, spec, lid=nid, page=page, rect=b.get("rect"))}
    except (tpl.TableEditError, listedit.ListEditError, edit.EditError) as exc:
        return _err(str(exc))
    return jsonify(r)


# ---------------------------------------------------- marks (V4.19.1)
# Ctrl+N (or Alt+N) on a table or list marks it #N: its tagging structure is
# read and kept for this file; the next tables (lists) clicked are read again
# and tagged the same way; Esc lets the mark go. Kept beside the job
# (marks.json), not in the tags, so Undo never takes a mark away.
def _marks_path(jid: str) -> str:
    return os.path.join(job_dir(jid), "marks.json")


def _read_marks(jid: str) -> dict:
    try:
        with open(_marks_path(jid), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _write_marks(jid: str, marks: dict) -> None:
    p = _marks_path(jid)
    with open(p + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(marks, fh)
    os.replace(p + ".tmp", p)


@app.route("/api/job/<jid>/marks", methods=["GET", "POST", "DELETE"])
def api_marks(jid):
    from core import templates as tpl
    from core import listedit
    marks = _read_marks(jid)
    if request.method == "GET":
        return jsonify({k: {n: {"source": v.get("source"), "page": v.get("source_page"), "summary": tpl.summary(v)}
                            for n, v in (marks.get(k) or {}).items()} for k in ("table", "list")})
    b = request.get_json(silent=True) or {}
    if request.method == "DELETE":
        n = str(b.get("number") or "")
        for k in ("table", "list"):
            if n:
                (marks.get(k) or {}).pop(n, None)
            else:
                marks[k] = {}
        _write_marks(jid, marks)
        return jsonify({"ok": True})
    model = get_model(jid)
    nid = str(b.get("id") or "")
    n = model.nodes.get(nid)
    if n is None:
        return _err("select a table or a list first")
    tbl = n if n.role == "Table" else next((a for a in model.ancestors(nid) if a.role == "Table"), None)
    lst = n if n.role == "L" else next((a for a in reversed(model.ancestors(nid)) if a.role == "L"), None)
    work = os.path.join(job_dir(jid), "working.pdf")
    try:
        number = int(b.get("number"))
    except (TypeError, ValueError):
        return _err("a mark is a number")
    try:
        if tbl is not None:
            kind, src = "table", tbl
            spec = tpl.table_spec(model, work, tbl.id)
        elif lst is not None:
            kind, src = "list", lst
            spec = listedit.list_spec(model, work, lst.id)
        else:
            return _err("that is not a table or a list — select one (or a cell / an item in it)")
    except (tpl.TableEditError, listedit.ListEditError) as exc:
        return _err(str(exc))
    spec.update({"number": number, "source": src.id, "source_page": src.page})
    marks.setdefault(kind, {})[str(number)] = spec
    _write_marks(jid, marks)
    return jsonify({"ok": True, "kind": kind, "number": number, "source": src.id, "page": src.page,
                    "summary": tpl.summary(spec)})


@app.route("/api/job/<jid>/zone/delete", methods=["POST"])
def api_zone_delete(jid):
    """A zone drawn on the page, then Delete: every tag printed wholly in it goes."""
    b = request.get_json(force=True) or {}
    try:
        with mutate(jid) as model:
            r = edit.delete_in_zone(model, os.path.join(job_dir(jid), "working.pdf"),
                                    int(b.get("page", 0)), b.get("rect"))
    except edit.EditError as exc:
        return _err(str(exc))
    if r["deleted"]:
        log_action("deleted tags in a zone", jid, "", f"{r['deleted']} tag(s), page {int(b.get('page', 0)) + 1}")
    return jsonify(r)


def _last_editor(jid: str) -> tuple[str, float | None]:
    """(who changed the tags last, seconds ago)"""
    try:
        m = read_meta(jid)
        at = m.get("edited") or ""
        ago = (datetime.now() - datetime.fromisoformat(at)).total_seconds() if at else None
        return str(m.get("edited_by") or ""), ago
    except Exception:
        return "", None


@app.route("/api/job/<jid>/undo", methods=["POST"])
@app.route("/api/job/<jid>/redo", methods=["POST"])
def api_undo(jid):
    kind = "undo" if request.path.endswith("/undo") else "redo"
    other = "redo" if kind == "undo" else "undo"
    _guard_busy(jid)
    with job_lock(jid):
        stack = _stack(jid, kind)
        if not stack:
            return _err(f"nothing to {kind}")
        # V4.28: the history belongs to the file, not to a person. When somebody else made the last change a
        # moment ago, ask before taking it back — it may not be what this person meant to undo.
        if not (request.get_json(silent=True) or {}).get("force"):
            who, ago = _last_editor(jid)
            if who and who.lower() != (current_user_name() or "").lower() and ago is not None and ago < 900:
                return jsonify({"error": f"{who} made the last change {max(1, int(ago // 60))} min ago — "
                                         f"{kind} it anyway?", "confirm": True, "by": who}), 409
        cur = os.path.join(job_dir(jid), "model.json")
        key = secure_filename(jid)
        base = model_rev(jid)
        with _mc_lock:
            hit = _model_cache.get(jid)
            old_sh = _SHADOWS.get(key)
        old_model = hit[1] if hit else None
        if old_sh is not None and old_sh.owner is not old_model:
            old_sh = None
        _push(jid, other, cur)
        last = os.path.join(_hist(jid, kind), stack[-1])
        tmp = cur + ".undo.tmp"                    # replaced whole, never half written (the learner reads it)
        shutil.copyfile(last, tmp)
        replace_file(tmp, cur)
        os.remove(last)
        _model_cache.pop(jid, None)
        _REV[key] = _REV.get(key, 0) + 1
        if old_sh is not None and has_request_context():
            try:                                   # V4.30: what the undo changed is told to the editor
                new_model = get_model(jid)
                changed, gone, tops = old_sh.diff(new_model)
                prev = old_sh.nums if old_sh.nums is not None else edit.reading_numbers(old_model)
                old_sh.commit(new_model, changed, gone, tops)
                old_sh.owner = new_model
                nums, order = _numbering(new_model)
                old_sh.nums, old_sh.order = nums, order
                with _mc_lock:
                    _SHADOWS[key] = old_sh
                rev = model_rev(jid)
                _add_patch(_compute_patch(jid, new_model, prev, nums, order, base, rev, changed, gone, tops,
                                          bool(_stack(jid, "undo")), bool(_stack(jid, "redo"))), base, rev)
            except Exception as exc:
                app.logger.warning("undo patch: %s", exc)
                _SHADOWS.pop(key, None)
    update_meta(jid, dirty=True, edited_by=current_user_name() or "")
    learn_live(jid)
    return jsonify({"ok": True})


@app.route("/api/job/<jid>/lint")
def api_lint(jid):
    model = get_model(jid)
    return serve_cached(LINT_BYTES, secure_filename(jid), f'W/"{model_rev(jid)}"', lambda: lint(model))


@app.route("/api/job/<jid>/doc", methods=["POST"])
def api_doc(jid):
    b = request.get_json(force=True) or {}
    with mutate(jid) as model:
        if "title" in b:
            model.title = str(b["title"])[:400]
        if "lang" in b:
            model.lang = str(b["lang"]).strip()[:35]
    update_meta(jid, title=b.get("title"), lang=b.get("lang"))
    return jsonify({"ok": True})


# ------------------------------------------------------------ contrast
def _contrast_payload(jid: str) -> dict:
    data = load_contrast(job_dir(jid))
    if data is None:
        return {"available": False}
    mats, _ = page_geometry(jid)                  # V4.30: kept while the PDF stays the same (was: the PDF opened at every load)
    out = []
    for i in data.get("issues", []):
        a, b, c, d_, e, f = mats[i["page"]]
        x0, y0, x1, y1 = i["bbox"]
        xs = [a * x + c * y + e for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
        ys = [b * x + d_ * y + f for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
        out.append({**i, "rect": [round(min(xs), 2), round(min(ys), 2), round(max(xs), 2), round(max(ys), 2)]})
    fix = _fix_settings(job_opts(jid))
    for i in out:
        i["fixable"] = {m: bool(contrast_mod.fix_method(i, m)) for m in contrast_mod.METHODS}
        i["auto_method"] = contrast_mod.fix_method(i, "auto")
    return {"available": True, "level": data.get("level", "AA"), "checked": data.get("checked", 0),
            "fix": fix, "summary": contrast_mod.summarize(data), "issues": out,
            "can_undo_fix": os.path.exists(os.path.join(job_dir(jid), COLOUR_BACKUP)),
            "task": read_meta(jid).get("contrast_task") or {}}


@app.route("/api/job/<jid>/contrast", methods=["GET", "POST"])
def api_contrast(jid):
    if request.method == "GET":
        return jsonify(_contrast_payload(jid))
    b = request.get_json(force=True) or {}
    _guard_busy(jid)
    with job_lock(jid):
        data = load_contrast(job_dir(jid))
        if data is None:
            return _err("no contrast analysis for this file")
        ids = set(b.get("ids") or [])
        method = job_opts(jid).contrast_fix_method
        for i in data["issues"]:
            fixable = bool(contrast_mod.fix_method(i, method))
            if b.get("all_fixable") and fixable:
                i["apply"] = bool(b.get("apply"))
            elif i["id"] in ids:
                i["apply"] = bool(b.get("apply")) and fixable
        save_contrast(job_dir(jid), data)
    update_meta(jid, dirty=True)
    return jsonify({"ok": True, "ticked": sum(1 for i in data["issues"] if i.get("apply"))})


# --------------------------------------------------------- cross-references
XREF_OPTS = ("cross_reference_tags", "link_web", "link_toc", "link_sections",
             "link_figures_tables", "link_references", "link_index", "link_footnotes", "link_back")


@app.route("/api/job/<jid>/xref", methods=["GET", "POST"])
def api_xref(jid):
    """What the cross-reference search found, and re-run it with other settings."""
    if request.method == "POST":
        b = request.get_json(force=True) or {}
        opts = job_opts(jid)
        for k in XREF_OPTS:
            if k in b:
                setattr(opts, k, bool(b[k]))
        opts.detect_cross_references = True
        with mutate(jid) as model:
            stats = xref.rescan(model, os.path.join(job_dir(jid), "working.pdf"), opts)
        update_meta(jid, options=opts.to_dict())
        return jsonify({"ok": True, "stats": stats})
    model = get_model(jid)
    o = job_opts(jid)
    info = model.info.get("xref") or {}
    return jsonify({"available": bool(model.info.get("xrefs") is not None),
                    "stats": info, "items": model.info.get("xrefs") or [],
                    "labels": xref.KIND_LABEL, "kind_option": xref.KIND_OPTION,
                    "options": {k: bool(getattr(o, k)) for k in XREF_OPTS}})


# ------------------------------------------------------------ Links stage
from core import links as links_mod


def _links_payload(jid: str) -> dict:
    model = get_model(jid)
    o = job_opts(jid)
    data = links_mod.list_links(model, os.path.join(job_dir(jid), "working.pdf"))
    data["search"] = {"available": bool(model.info.get("xrefs") is not None),
                      "stats": model.info.get("xref") or {},
                      "labels": xref.KIND_LABEL, "kind_option": xref.KIND_OPTION,
                      "options": {k: bool(getattr(o, k)) for k in XREF_OPTS}}
    data["direction"] = getattr(o, "link_direction", "one")
    data["reference"] = bool(getattr(o, "cross_reference_tags", False)) or \
        (getattr(o, "house_style", "") or "").lower() not in ("", "none", "off")
    data["removed_by_hand"] = len(model.info.get("links_removed") or [])
    return data


@app.route("/api/job/<jid>/links")
def api_links(jid):
    """Every link of the document, where each goes, and what the check found."""
    get_model(jid)
    try:
        job_opts(jid)
    except Exception:
        pass
    return jsonify(_links_payload(jid))


def _links_edit(jid, fn):
    try:
        with mutate(jid) as model:
            r = fn(model, os.path.join(job_dir(jid), "working.pdf"), job_opts(jid))
    except (links_mod.LinkError, edit.EditError) as exc:
        return _err(str(exc))
    return jsonify(r)


@app.route("/api/job/<jid>/links/set", methods=["POST"])
def api_links_set(jid):
    """A link set by hand: a box over the words -> a tag, a spot, a page, a
    web address or an e-mail address; one-way or two-way."""
    b = request.get_json(force=True) or {}

    def go(model, pdf, o):
        two = b.get("two_way")
        if two is None:
            two = getattr(o, "link_direction", "one") == "two"
        return links_mod.set_link(model, pdf, int(b.get("page", 0)), b.get("rect"), b.get("target") or {},
                                  bool(two), b.get("back_rect"), o, more_rects=b.get("more") or [],
                                  contents=str(b.get("contents") or ""),
                                  reference=b.get("reference") if b.get("reference") in (True, False) else None,
                                  replace=bool(b.get("replace")))
    return _links_edit(jid, go)


@app.route("/api/job/<jid>/links/<lid>/<what>", methods=["POST"])
def api_links_change(jid, lid, what):
    b = request.get_json(force=True) or {}
    acts = {
        "target": lambda m, p, o: links_mod.retarget(m, p, lid, b.get("target") or {}, o),
        "twoway": lambda m, p, o: links_mod.make_two_way(m, p, lid, b.get("back_rect"), o),
        "remove": lambda m, p, o: links_mod.remove_link(m, lid, bool(b.get("both", True))),
        "contents": lambda m, p, o: links_mod.set_contents(m, lid, str(b.get("contents") or "")),
        "zone": lambda m, p, o: links_mod.set_zone(m, p, lid, b.get("rect"), b.get("page")),
        "fix": lambda m, p, o: links_mod.fix_problem(m, p, lid, b.get("fix"), o),
    }
    if what not in acts:
        return _err("unknown link action", 404)
    return _links_edit(jid, acts[what])


@app.route("/api/job/<jid>/links/fix_all", methods=["POST"])
def api_links_fix_all(jid):
    b = request.get_json(force=True) or {}
    codes = set(b.get("codes") or []) or None
    return _links_edit(jid, lambda m, p, o: links_mod.fix_all(m, p, o, codes))


@app.route("/api/job/<jid>/links/forget_removed", methods=["POST"])
def api_links_forget(jid):
    """The links taken out by hand may be found again by the next search."""
    def go(model, pdf, o):
        n = len(model.info.get("links_removed") or [])
        model.info["links_removed"] = []
        return {"ok": True, "forgotten": n}
    return _links_edit(jid, go)


@app.route("/job/<jid>/links/peek/<int:pno>.png")
def links_peek(jid, pno):
    """Where a link lands: a band of the page with the spot marked."""
    try:
        y = request.args.get("y")
        data = links_mod.peek(os.path.join(job_dir(jid), "working.pdf"), pno,
                              float(y) if y not in (None, "", "null") else None)
    except links_mod.LinkError:
        abort(404)
    return send_file(io.BytesIO(data), mimetype="image/png")


@app.route("/api/job/<jid>/order/fix", methods=["POST"])
def api_order_fix(jid):
    """Put blocks that break the page sequence back on their page."""
    with mutate(jid) as model:
        moved = edit.fix_page_order(model, job_opts(jid))
    return jsonify({"ok": True, "moved": moved})


@app.route("/api/job/<jid>/labels/fix", methods=["POST"])
def api_labels_fix(jid):
    """Every list item keeps its number in a Lbl, not inside the text."""
    with mutate(jid) as model:
        fixed = edit.fix_item_labels(model, os.path.join(job_dir(jid), "working.pdf"),
                                     job_opts(jid))
        left = edit.items_missing_labels(model)
    return jsonify({"ok": True, "fixed": fixed, "left": left})


@app.route("/api/job/<jid>/table/<tid>/columns", methods=["POST"])
def api_table_columns(jid, tid):
    """One heading, then the words printed under it — group after group."""
    d = request.get_json(force=True) or {}
    mode = str(d.get("mode") or "cells")
    try:
        with mutate(jid) as model:
            r = edit.convert_table_columns(model, tid, mode)
    except edit.EditError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, **r})


@app.route("/api/job/<jid>/headings/fix", methods=["POST"])
def api_headings_fix(jid):
    """No skipped heading levels, and the first heading is H1 (PDF/UA 7.4)."""
    from core.model import clamp_heading_levels
    with mutate(jid) as model:
        changed = clamp_heading_levels(model)
    return jsonify({"ok": True, "changed": changed})


@app.route("/api/job/<jid>/structure/fix", methods=["POST"])
def api_structure_fix(jid):
    """Every tag back under a parent PDF/UA allows."""
    from core.repair import fix_structure, structure_problems
    with mutate(jid) as model:
        moved = fix_structure(model)
        left = len(structure_problems(model))
    return jsonify({"ok": True, "moved": moved, "left": left,
                    "total": sum(moved.values()) if moved else 0})


@app.route("/api/job/<jid>/blanklines/fix", methods=["POST"])
def api_blank_lines_fix(jid):
    """Write-in rules are read as “Blank Line”, wherever they are printed."""
    from core.repair import blank_line_spans, blank_lines_missing
    d = request.get_json(force=True) or {}
    ids = [str(i) for i in (d.get("ids") or [])] or None
    with mutate(jid) as model:
        done = blank_line_spans(model, os.path.join(job_dir(jid), "working.pdf"),
                                job_opts(jid), ids)
        left = len(blank_lines_missing(model))
    return jsonify({"ok": True, "fixed": done, "left": left})


@app.route("/api/job/<jid>/zones/split", methods=["GET", "POST"])
def api_split_zones(jid):
    """Tags joined from the words of a word grid become one tag per word again;
    with ids, any tag with several zones becomes one tag per zone."""
    from core.repair import joined_word_grids, split_zones
    if request.method == "GET":
        return jsonify({"ok": True, "ids": joined_word_grids(get_model(jid))})
    b = request.get_json(force=True) or {}
    ids = [str(i) for i in (b.get("ids") or [])] or None
    with mutate(jid) as model:
        n = split_zones(model, os.path.join(job_dir(jid), "working.pdf"), ids)
    return jsonify({"ok": True, "split": n})


@app.route("/api/job/<jid>/pagenumbers", methods=["GET", "POST"])
def api_page_numbers_all(jid):
    """Printed page numbers: read as P tags, or left out as artifacts."""
    from core.repair import page_number_tags, page_numbers_as
    if request.method == "GET":
        model = get_model(jid)
        return jsonify({"ok": True, "mode": getattr(job_opts(jid), "page_numbers", "artifact"),
                        "to_p": len(page_number_tags(model, True)),
                        "to_artifact": len(page_number_tags(model, False))})
    b = request.get_json(force=True) or {}
    as_p = str(b.get("as") or "P") == "P"
    word = getattr(job_opts(jid), "page_number_text", "Page")
    with mutate(jid) as model:
        n = page_numbers_as(model, as_p, word)
    return jsonify({"ok": True, "changed": n, "as": "P" if as_p else "artifact"})


SPLIT_BACKUP = "split_backup"
SPLIT_FILES = ("working.pdf", "model.json", "auto_model.json", "contrast.json")


@app.route("/api/job/<jid>/pages/spreads")
def api_spreads(jid):
    """Pages that look like two pages printed side by side."""
    from core.pages import spreads
    d = job_dir(jid)
    return jsonify({"ok": True, "pages": spreads(os.path.join(d, "working.pdf")),
                    "can_unsplit": os.path.isdir(os.path.join(d, SPLIT_BACKUP))})


@app.route("/api/job/<jid>/pages/split", methods=["POST"])
def api_split_pages(jid):
    """Split a two-page spread: the left half stays, the right half becomes the
    next page. Every tag goes with its half; every later page moves up by one."""
    from core.pages import split_model, split_pdf, spreads
    b = request.get_json(force=True) or {}
    d = job_dir(jid)
    work = os.path.join(d, "working.pdf")
    if b.get("all"):
        which = spreads(work)
    else:
        try:
            which = [int(p) for p in (b.get("pages") or [b.get("page")]) if p is not None]
        except (TypeError, ValueError):
            return _err("which page?")
    if not which:
        return _err("no page to split")
    where = float(b.get("where") or 0.5)
    _guard_busy(jid)
    with job_lock(jid):
        # one step back: the file, the tags and the automatic proposal as they were
        bk = os.path.join(d, SPLIT_BACKUP)
        shutil.rmtree(bk, ignore_errors=True)
        os.makedirs(bk)
        for f in SPLIT_FILES:
            if os.path.exists(os.path.join(d, f)):
                shutil.copyfile(os.path.join(d, f), os.path.join(bk, f))
        model = get_model(jid)
        auto = None
        ap = os.path.join(d, "auto_model.json")
        if os.path.exists(ap):
            with open(ap, encoding="utf-8") as fh:
                auto = DocModel.from_json(fh.read())
        moved = 0
        try:
            for pno in sorted(set(which), reverse=True):
                r = split_pdf(work, pno, where)
                moved += split_model(model, pno, r["left"], r["right"],
                                     r["left_size"], r["right_size"], r.get("annot_map"))
                if auto is not None:
                    split_model(auto, pno, r["left"], r["right"], r["left_size"], r["right_size"],
                                r.get("annot_map"))
        except Exception as exc:
            for f in SPLIT_FILES:                      # put everything back
                if os.path.exists(os.path.join(bk, f)):
                    shutil.copyfile(os.path.join(bk, f), os.path.join(d, f))
            _model_cache.pop(jid, None)
            return _err(f"the page could not be split ({exc})")
        opts = job_opts(jid)
        if opts.detect_cross_references:
            try:
                # printed page numbers now have a sheet each: search again
                xref.rescan(model, work, opts)
            except Exception as exc:
                model.warnings.append(f"cross-reference search after the split failed ({exc})")
        model.check_integrity()
        save_model(d, model)
        _model_cache.pop(jid, None)
        if auto is not None:
            save_auto_model(d, auto)
        # colours were measured per page: measure again; the undo history
        # describes the file before the split
        for f in ("contrast.json", COLOUR_BACKUP, CONTRAST_BACKUP):
            if os.path.exists(os.path.join(d, f)):
                os.remove(os.path.join(d, f))
        _clear(jid, "undo")
        _clear(jid, "redo")
        update_meta(jid, pages=model.page_count, dirty=True,
                    edited=datetime.now().isoformat(timespec="seconds"))
    return jsonify({"ok": True, "split": len(set(which)), "moved": moved,
                    "pages": model.page_count, "first": min(which)})


@app.route("/api/job/<jid>/pages/unsplit", methods=["POST"])
def api_unsplit_pages(jid):
    """Put the last split back: the file, the tags and the proposal as they were."""
    d = job_dir(jid)
    bk = os.path.join(d, SPLIT_BACKUP)
    if not os.path.isdir(bk):
        return _err("nothing to put back")
    _guard_busy(jid)
    with job_lock(jid):
        for f in SPLIT_FILES:
            if os.path.exists(os.path.join(bk, f)):
                shutil.copyfile(os.path.join(bk, f), os.path.join(d, f))
            elif f == "contrast.json" and os.path.exists(os.path.join(d, f)):
                os.remove(os.path.join(d, f))
        shutil.rmtree(bk, ignore_errors=True)
        _model_cache.pop(jid, None)
        _clear(jid, "undo")
        _clear(jid, "redo")
        model = get_model(jid)
        update_meta(jid, pages=model.page_count, dirty=True)
    return jsonify({"ok": True, "pages": model.page_count})


@app.route("/api/job/<jid>/create/empty", methods=["POST"])
def api_create_empty(jid):
    """A tag with nothing in it, next to or inside the tag you picked."""
    d = request.get_json(force=True) or {}
    try:
        with mutate(jid) as model:
            nid = edit.create_empty(model, str(d.get("role") or "P"), str(d.get("target") or ""),
                                    str(d.get("where") or "after"), str(d.get("text") or ""))
    except edit.EditError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "id": nid})


@app.route("/api/job/<jid>/lists/tidy", methods=["GET", "POST"])
def api_tidy_lists(jid):
    """Wrapped lines back in their item, and text without the characters that
    are not really printed."""
    from core.repair import (junk_in_text, list_children_wrong, list_values_apart,
                             lists_read_short, loose_sub_items, split_lists, stray_list_items,
                             tidy_lists)
    if request.method == "GET":
        model = load_model(job_dir(jid))
        return jsonify({"ok": True, "stray": stray_list_items(model),
                        "junk": junk_in_text(model),
                        "split": [a for a, _ in split_lists(model)],
                        "nested": loose_sub_items(model),
                        "values": list_values_apart(model),
                        "shape": list_children_wrong(model),
                        "short": lists_read_short(model)})
    d = request.get_json(force=True) or {}
    ids = [str(i) for i in (d.get("ids") or [])] or None
    with mutate(jid) as model:
        r = tidy_lists(model, ids, os.path.join(job_dir(jid), "working.pdf"), job_opts(jid))
    return jsonify({"ok": True, **r})


@app.route("/api/job/<jid>/bibliography", methods=["GET", "POST"])
def api_bibliography(jid):
    """Reference lists: one BibEntry per entry, then the citations linked again."""
    from core import biblio
    if request.method == "GET":
        model = load_model(job_dir(jid))
        p = biblio.problems(model)
        secs = [{"id": h, "page": model.nodes[h].page, "text": model.nodes[h].text}
                for h in p["sections"]]
        return jsonify({"ok": True, "sections": secs, "paragraphs": p["paragraphs"]})
    d = request.get_json(force=True) or {}
    opts = job_opts(jid)
    if d.get("as") in ("list", "entries", "plain"):
        opts.bibliography_as = d["as"]
    cfg = ai_cfg()
    if not cfg.enabled:
        cfg = None
    else:
        opts.ai_bibliography = True          # the person asked: ask the model when the layout is unclear
    heads = [str(i) for i in (d.get("ids") or [])] or None
    work = os.path.join(job_dir(jid), "working.pdf")
    with mutate(jid) as model:
        r = biblio.fix_bibliography(model, work, opts, cfg, heads)
        if r.get("entries") and opts.detect_cross_references:
            try:
                r["links"] = xref.rescan(model, work, opts).get("linked_by_kind", {})
            except Exception as exc:
                r["links_error"] = str(exc)
    return jsonify({"ok": True, **r})


@app.route("/api/job/<jid>/prompts", methods=["GET", "POST"])
def api_prompts(jid):
    """"Name: ____  Date: ____" — one P per prompt, only the underscores read as the blank."""
    from core.repair import blank_text_left, fix_prompt_rows, prompt_rows
    work = os.path.join(job_dir(jid), "working.pdf")
    if request.method == "GET":
        model = load_model(job_dir(jid))
        rows = prompt_rows(model, work, job_opts(jid))
        return jsonify({"ok": True, "ids": rows + [i for i in blank_text_left(model) if i not in rows]})
    d = request.get_json(force=True) or {}
    ids = [str(i) for i in (d.get("ids") or [])] or None
    with mutate(jid) as model:
        r = fix_prompt_rows(model, work, job_opts(jid), ids)
    return jsonify({"ok": True, **r})


@app.route("/api/job/<jid>/backmatter", methods=["GET", "POST"])
def api_backmatter(jid):
    """Endnotes as Note tags, the index as Index > L > LI — then the links again
    (footnotes both ways, captions both ways, every index page number)."""
    from core import backmatter
    if request.method == "GET":
        model = load_model(job_dir(jid))
        heads = backmatter.index_problems(model)
        return jsonify({"ok": True, "index": [{"id": h, "page": model.nodes[h].page} for h in heads]})
    opts = job_opts(jid)
    work = os.path.join(job_dir(jid), "working.pdf")
    with mutate(jid) as model:
        r = backmatter.fix_backmatter(model, work, opts)
        opts.detect_cross_references = True
        try:
            r["links"] = xref.rescan(model, work, opts).get("linked_by_kind", {})
        except Exception as exc:
            r["links_error"] = str(exc)
    return jsonify({"ok": True, **r})


@app.route("/api/job/<jid>/order/ai", methods=["POST"])
def api_order_ai(jid):
    """The vision model looks at the page with its reading order numbered and
    says whether a reader would go through it in that order."""
    b = request.get_json(force=True) or {}
    cfg = ai_cfg()
    if not cfg.enabled:
        return _err("The AI is switched off in Settings")
    cfg.timeout = int(cfg.timeout or 600)
    pno = int(b.get("page", 0))
    model = get_model(jid)
    try:
        r = edit.ai_reading_order(model, os.path.join(job_dir(jid), "working.pdf"), pno, cfg)
    except OllamaError as exc:
        return _err(str(exc))
    except edit.EditError as exc:
        return _err(str(exc))
    return jsonify({"ok": True, **r})


@app.route("/api/job/<jid>/order/apply", methods=["POST"])
def api_order_apply(jid):
    """Put a page's reading blocks in the order given (each block keeps its parent)."""
    b = request.get_json(force=True) or {}
    try:
        with mutate(jid) as model:
            moved = edit.reorder_blocks(model, int(b.get("page", 0)),
                                        [str(i) for i in (b.get("ids") or [])])
    except edit.EditError as exc:
        return _err(str(exc))
    return jsonify({"ok": True, "moved": moved})


@app.route("/api/job/<jid>/cells/reread", methods=["POST"])
def api_cells_reread(jid):
    """Each table cell takes the words printed inside it, lines cut at the borders."""
    from core.repair import reread_cells
    d = request.get_json(force=True) or {}
    ids = [str(i) for i in (d.get("ids") or [])] or None
    with mutate(jid) as model:
        r = reread_cells(model, os.path.join(job_dir(jid), "working.pdf"), job_opts(jid), ids)
    return jsonify({"ok": True, **r})


@app.route("/api/job/<jid>/cells/lists", methods=["GET", "POST"])
def api_cell_lists(jid):
    """A list printed inside a table cell becomes L > LI > Lbl + LBody there."""
    from core.repair import cell_lists, cells_with_lists
    if request.method == "GET":
        model = load_model(job_dir(jid))
        return jsonify({"ok": True, "ids": cells_with_lists(model)})
    d = request.get_json(force=True) or {}
    ids = [str(i) for i in (d.get("ids") or [])] or None
    with mutate(jid) as model:
        r = cell_lists(model, os.path.join(job_dir(jid), "working.pdf"), job_opts(jid), ids)
    return jsonify({"ok": True, **r})


@app.route("/api/job/<jid>/alt/captions", methods=["GET", "POST"])
def api_alt_captions(jid):
    """Figures with no /Alt and the caption printed with them; POST applies it."""
    from core.repair import alt_from_captions
    if request.method == "GET":
        model = load_model(job_dir(jid))
        return jsonify({"ok": True, "items": alt_from_captions(model)})
    d = request.get_json(force=True) or {}
    pick = {str(i) for i in (d.get("ids") or [])}
    with mutate(jid) as model:
        done = 0
        for item in alt_from_captions(model):
            if (pick and item["id"] not in pick) or not item["caption"]:
                continue
            n = model.nodes.get(item["id"])
            if n is None:
                continue
            n.alt = item["caption"][:2000]
            n.source = "user"
            n.note = (n.note + "; " if n.note else "") + "alt text taken from the caption"
            done += 1
    return jsonify({"ok": True, "changed": done})


@app.route("/api/job/<jid>/actualtext", methods=["POST"])
def api_actual_text(jid):
    """Write /ActualText on the selected tags — one, or all of them at once."""
    d = request.get_json(force=True) or {}
    ids = [str(i) for i in (d.get("ids") or [])]
    if not ids:
        return jsonify({"error": "nothing selected"}), 400
    mode = str(d.get("mode") or "same")
    text = d.get("text")
    if mode == "each" and not isinstance(text, list):
        return jsonify({"error": "mode 'each' needs one text per tag"}), 400
    with mutate(jid) as model:
        r = edit.set_actual_text(model, ids, text if text is not None else "", mode)
    return jsonify({"ok": True, **r})


@app.route("/api/job/<jid>/actualtext/suggest", methods=["POST"])
def api_actual_text_suggest(jid):
    """What each selected tag should be read as, from the glyphs on the page."""
    d = request.get_json(force=True) or {}
    ids = [str(i) for i in (d.get("ids") or [])]
    if not ids:
        return jsonify({"error": "nothing selected"}), 400
    model = load_model(job_dir(jid))
    items = edit.suggest_actual_text(model, os.path.join(job_dir(jid), "working.pdf"), ids)
    return jsonify({"ok": True, "items": items})


@app.route("/api/job/<jid>/actualtext/tags", methods=["GET"])
def api_actualtext_tags(jid):
    """The tags that carry /ActualText themselves instead of on a Span."""
    ids = edit.actualtext_on_tag(get_model(jid))
    return jsonify({"ids": ids, "count": len(ids)})


@app.route("/api/job/<jid>/actualtext/span", methods=["POST"])
def api_actualtext_span(jid):
    """Put every /ActualText on a Span inside its tag (every client)."""
    b = request.get_json(silent=True) or {}
    ids = [str(x) for x in (b.get("ids") or [])] or None
    with mutate(jid) as model:
        changed = edit.actualtext_to_span(model, ids)
    return jsonify({"ok": True, "changed": changed})


@app.route("/api/job/<jid>/headers/fix", methods=["POST"])
def api_headers_fix(jid):
    """Every empty header cell (TH) becomes a data cell (TD) — not for a client
    that keeps empty header cells as TH (Kriyadocs)."""
    if (getattr(job_opts(jid), "house_style", "") or "").lower() not in ("", "none", "off"):
        return jsonify({"ok": True, "changed": 0, "kept": "this client keeps empty header cells as TH"})
    with mutate(jid) as model:
        changed = edit.fix_empty_headers(model)
    return jsonify({"ok": True, "changed": changed})


@app.route("/api/job/<jid>/contrast/fix", methods=["GET", "POST"])
def api_contrast_fix(jid):
    """Fix low-contrast colours now (in working.pdf), in the background."""
    if request.method == "GET":
        return jsonify(read_meta(jid).get("contrast_task") or {})
    _guard_busy(jid)
    b = request.get_json(force=True) or {}
    ids = [str(x) for x in (b.get("ids") or [])] or None
    if load_contrast(job_dir(jid)) is None:
        return _err("contrast was not measured for this file — use Re-measure first")
    # the fix method chosen in the Contrast tab is kept as the job option (Build uses it too)
    opts, bad = _save_fix_settings(jid, b)
    if bad:
        return _err(bad)
    STOP.discard(jid)
    CONTRAST_IDS[jid] = {"ids": ids, "fix": _fix_settings(opts)}
    update_meta(jid, contrast_task={"running": True, "progress": 0,
                                    "message": "waiting for a free worker", "result": None})
    TASKS.put((jid, "contrast_fix"))
    return jsonify({"ok": True, "queued": True})


def _save_fix_settings(jid: str, b: dict) -> tuple[Options, str]:
    opts = job_opts(jid)
    if b.get("method") in contrast_mod.METHODS:
        opts.contrast_fix_method = b["method"]
    if b.get("stroke_color") in ("black", "white", "auto"):
        opts.contrast_stroke_color = b["stroke_color"]
    if b.get("stroke_width") not in (None, ""):
        try:
            opts.contrast_stroke_width = round(max(0.05, min(3.0, float(b["stroke_width"]))), 3)
        except (TypeError, ValueError):
            return opts, "outline width must be a number (points)"
    with job_lock(jid):
        update_meta(jid, options=opts.to_dict())
    return opts, ""


@app.route("/api/job/<jid>/contrast/method", methods=["POST"])
def api_contrast_method(jid):
    """Fix method for this file: auto | background | text | stroke (+ outline width/colour)."""
    _guard_busy(jid)
    opts, bad = _save_fix_settings(jid, request.get_json(force=True) or {})
    if bad:
        return _err(bad)
    return jsonify({"ok": True, "fix": _fix_settings(opts)})


@app.route("/api/job/<jid>/contrast/undo_fix", methods=["POST"])
def api_contrast_undo_fix(jid):
    _guard_busy(jid)
    with job_lock(jid):
        try:
            r = undo_contrast_fixes(job_dir(jid))
        except ValueError as exc:
            return _err(str(exc))
        update_meta(jid, contrast=r.get("summary"), score_before=r.get("score_before"), dirty=True)
    return jsonify({"ok": True, **r})


@app.route("/api/job/<jid>/contrast/recheck", methods=["POST"])
def api_contrast_recheck(jid):
    b = request.get_json(force=True) or {}
    level = b.get("level") if b.get("level") in ("AA", "AAA") else job_opts(jid).contrast_level
    model = get_model(jid)
    data = contrast_mod.analyze(os.path.join(job_dir(jid), "working.pdf"), level=level, model=model,
                                large_rule=job_opts(jid).contrast_large_text)
    _guard_busy(jid)
    with job_lock(jid):
        save_contrast(job_dir(jid), data)
        before = contrast_findings(load_findings(job_dir(jid), "before"), data)
        save_findings(job_dir(jid), "before", before)
        meta = read_meta(jid)
        opts = Options.from_dict(meta.get("options"))
        opts.contrast_level = level
        update_meta(jid, contrast=contrast_mod.summarize(data), options=opts.to_dict(),
                    score_before=checker.score(before), dirty=True)
    return jsonify({"ok": True, "summary": contrast_mod.summarize(data)})


@app.route("/api/job/<jid>/findings")
def api_findings(jid):
    d = job_dir(jid)
    return jsonify({"before": [asdict(f) for f in load_findings(d, "before")],
                    "after": [asdict(f) for f in load_findings(d, "after")]})


# ------------------------------------------------------------------ AI
@app.route("/api/ai/tasks")
def api_ai_tasks():
    """Which model each job uses, and what is installed to choose from."""
    from core.ai import TASKS
    cfg = ai_cfg()
    out = {"tasks": {k: {"kind": v[0], "label": v[1], "set": cfg.for_task(k)}
                     for k, v in TASKS.items()}, "models": []}
    try:
        out["models"] = installed_models(cfg)
    except Exception as exc:
        out["error"] = str(exc)
    return jsonify(out)


@app.route("/api/ai/ping", methods=["POST"])
def api_ai_ping():
    b = request.get_json(force=True) or {}
    cfg = ai_cfg()
    cfg.base_url = b.get("base_url") or cfg.base_url
    if "text_model" in b:
        cfg.text_model = str(b.get("text_model") or "")
    if "vision_model" in b:
        cfg.vision_model = str(b.get("vision_model") or "")
    if isinstance(b.get("task_models"), dict):
        cfg.task_models = {k: str(v or "") for k, v in b["task_models"].items()}
    try:
        return jsonify(ping(cfg))
    except OllamaError as exc:
        return jsonify({"ok": False, "error": str(exc)})


def _figure_png(jid: str, node) -> bytes:
    doc = _page_doc(jid)
    page = doc[node.page]
    clip = pymupdf.Rect(node.bbox) * page.transformation_matrix * page.rotation_matrix
    clip.normalize()
    pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=clip & page.rect, alpha=False)
    data = pix.tobytes("png")
    doc.close()
    return data


@app.route("/api/job/<jid>/ai/alt/<nid>", methods=["POST"])
def api_ai_alt(jid, nid):
    cfg = ai_cfg()
    model = load_model(job_dir(jid))
    node = model.nodes.get(nid)
    if node is None or not node.bbox:
        return _err("no such figure", 404)
    ctx = " ".join(n.text for n in model.ordered_nodes() if n.page == node.page and n.text)[:800]
    try:
        text = alt_text(cfg, _figure_png(jid, node), ctx)
    except OllamaError as exc:
        return jsonify({"error": str(exc)})
    except Exception as exc:
        return jsonify({"error": f"alt text failed: {exc}"})
    if text.upper().startswith("DECORATIVE"):
        return jsonify({"ok": True, "decorative": True, "alt": ""})
    with mutate(jid) as m2:
        if nid in m2.nodes:
            m2.nodes[nid].alt = text
            m2.nodes[nid].source = "ai"
    return jsonify({"ok": True, "alt": text})


@app.route("/api/job/<jid>/ai/alt_all", methods=["GET", "POST"])
def api_ai_alt_all(jid):
    """Runs in the background queue (can take long on a small GPU)."""
    if request.method == "GET":
        return jsonify(read_meta(jid).get("ai_task") or {})
    _guard_busy(jid)
    cfg = ai_cfg()
    if not cfg.enabled:
        return _err("AI is switched off in Settings")
    with job_lock(jid):
        _push(jid, "undo", os.path.join(job_dir(jid), "model.json"))
        _clear(jid, "redo")
    STOP.discard(jid)
    update_meta(jid, ai_task={"running": True, "queued": True, "progress": 0,
                              "message": "waiting for a free worker", "result": None})
    TASKS.put((jid, "ai_alt"))
    return jsonify({"ok": True, "queued": True})


@app.route("/api/job/<jid>/ai/roles", methods=["POST"])
def api_ai_roles(jid):
    cfg = ai_cfg()
    model = load_model(job_dir(jid))
    cands = [n for n in model.ordered_nodes()
             if n.text and (n.role in ("P", "H1", "H2", "H3", "H4", "H5", "H6", "Caption", "Note")
                            or n.artifact) and n.parent is None]
    index = {i: n.id for i, n in enumerate(cands, start=1)}
    blocks = [{"i": i, "role": "Artifact" if n.artifact else n.role, "text": n.text,
               "page": n.page} for i, n in enumerate(cands, start=1)]
    changes = []
    try:
        for s in range(0, len(blocks), 40):
            changes += review_roles(cfg, blocks[s:s + 40])
    except OllamaError as exc:
        return jsonify({"error": str(exc)})
    except Exception as exc:
        return jsonify({"error": f"role review failed: {exc}"})
    out = []
    for ch in changes:
        nid = index.get(ch["i"])
        if not nid:
            continue
        n = model.nodes[nid]
        cur = "Artifact" if n.artifact else n.role
        if ch["role"] != cur:
            out.append({"id": nid, "from": cur, "to": ch["role"], "why": ch.get("why", ""),
                        "text": n.text[:100], "page": n.page})
    return jsonify({"ok": True, "suggestions": out})


# ------------------------------------------------------- more AI helpers
def _ai_guard(fn):
    """Every AI endpoint answers the same way when the server is not there."""
    def wrapper(jid, *a, **kw):
        try:
            return fn(jid, *a, **kw)
        except OllamaError as exc:
            return jsonify({"error": str(exc)})
        except Exception as exc:
            return jsonify({"error": f"{fn.__name__.replace('api_ai_', '')} failed: {exc}"})
    wrapper.__name__ = fn.__name__
    return wrapper


@app.route("/api/job/<jid>/ai/headings", methods=["POST"])
@_ai_guard
def api_ai_headings(jid):
    """Are the heading levels right? Font size alone often gets them wrong."""
    cfg = ai_cfg()
    model = load_model(job_dir(jid))
    heads = [n for n in model.ordered_nodes() if n.is_heading and not n.artifact]
    if not heads:
        return jsonify({"ok": True, "suggestions": []})
    index = {i: n.id for i, n in enumerate(heads, start=1)}
    out = []
    for s0 in range(0, len(heads), 60):
        part = heads[s0:s0 + 60]
        blocks = [{"i": s0 + k + 1, "level": int(n.role[1]), "text": n.text, "page": n.page}
                  for k, n in enumerate(part)]
        for ch in heading_levels(cfg, blocks):
            nid = index.get(ch["i"])
            if not nid:
                continue
            n = model.nodes[nid]
            if f"H{ch['level']}" != n.role:
                out.append({"id": nid, "kind": "heading", "from": n.role, "to": f"H{ch['level']}",
                            "field": "role", "why": ch.get("why", ""), "text": n.text[:100],
                            "page": n.page})
    return jsonify({"ok": True, "suggestions": out})


@app.route("/api/job/<jid>/ai/links", methods=["POST"])
@_ai_guard
def api_ai_links(jid):
    """A description for links whose text says nothing on its own."""
    cfg = ai_cfg()
    model = load_model(job_dir(jid))
    poor = re.compile(r"^\s*(click here|here|read more|more|link|this|www\.|https?://|$)", re.I)
    out = []
    for n in model.ordered_nodes():
        if n.role != "Link" or n.alt:
            continue
        txt = (n.text or "").strip()
        if txt and not poor.match(txt) and len(txt) > 12:
            continue
        x = (n.style or {}).get("xref") or {}
        dest = x.get("uri") or (f"page {int(x['page']) + 1}" if x.get("page") is not None else "")
        if not dest:
            dest = txt
        parent = model.nodes.get(n.parent) if n.parent else None
        ctx = (parent.text if parent is not None else "")[:400]
        draft = link_description(cfg, txt, dest, ctx)
        if draft:
            out.append({"id": n.id, "kind": "link", "from": txt or "(no text)", "to": draft,
                        "field": "alt", "why": "", "text": ctx[:100], "page": n.page})
        if len(out) >= 60:
            break
    return jsonify({"ok": True, "suggestions": out})


@app.route("/api/job/<jid>/ai/fields", methods=["POST"])
@_ai_guard
def api_ai_fields(jid):
    """A tooltip for form fields that have none (PDF/UA 7.18.6)."""
    cfg = ai_cfg()
    model = load_model(job_dir(jid))
    doc = _page_doc(jid)
    out = []
    try:
        for n in model.ordered_nodes():
            if n.role != "Form" or (n.text or "").strip() or n.bbox is None or n.page < 0:
                continue
            page = doc[n.page]
            r = pymupdf.Rect(n.bbox) * page.transformation_matrix
            r.normalize()
            grab = lambda rect: re.sub(r"\s+", " ", printed_text(page, rect)).strip()[:120]
            left = grab(pymupdf.Rect(max(0, r.x0 - 220), r.y0 - 4, r.x0, r.y1 + 4))
            right = grab(pymupdf.Rect(r.x1, r.y0 - 4, r.x1 + 160, r.y1 + 4))
            above = grab(pymupdf.Rect(r.x0 - 20, max(0, r.y0 - 28), r.x1 + 20, r.y0))
            draft = field_label(cfg, (n.note or "field").split()[-1], n.note, left, right, above)
            if draft:
                out.append({"id": n.id, "kind": "field", "from": "(no tooltip)", "to": draft,
                            "field": "text", "why": "", "text": (left or above)[:100], "page": n.page})
            if len(out) >= 60:
                break
    finally:
        doc.close()
    return jsonify({"ok": True, "suggestions": out})


@app.route("/api/job/<jid>/ai/tables", methods=["POST"])
@_ai_guard
def api_ai_tables(jid):
    """A one-line summary for each table, written to its Summary attribute."""
    cfg = ai_cfg()
    model = load_model(job_dir(jid))
    out = []
    for n in model.ordered_nodes():
        if n.role != "Table" or n.alt:
            continue
        rows = [c for c in n.children if model.nodes.get(c) and model.nodes[c].role in ("TR", "THead", "TBody")]
        flat: list[str] = []
        for rid in rows[:6]:
            r = model.nodes[rid]
            kids = [r] if r.role == "TR" else [model.nodes[k] for k in r.children if k in model.nodes]
            for tr in kids:
                cells = [model.nodes[c].text for c in tr.children if c in model.nodes]
                if cells:
                    flat.append(" | ".join(t[:40] for t in cells))
        cap = ""
        for c in n.children:
            k = model.nodes.get(c)
            if k is not None and k.role == "Caption":
                cap = k.text
                break
        if not flat:
            continue
        draft = table_summary(cfg, cap, flat[0].split(" | "), flat[1:4])
        if draft:
            out.append({"id": n.id, "kind": "table", "from": "(no summary)", "to": draft,
                        "field": "alt", "why": "", "text": (cap or flat[0])[:100], "page": n.page})
        if len(out) >= 40:
            break
    return jsonify({"ok": True, "suggestions": out})


@app.route("/api/job/<jid>/ai/math", methods=["POST"])
@_ai_guard
def api_ai_math(jid):
    """Look at every Formula and every unexplained Figure: is it maths, is it
    decorative, and how should it be read aloud."""
    cfg = ai_cfg()
    model = load_model(job_dir(jid))
    doc = _page_doc(jid)
    out = []
    try:
        cands = [n for n in model.ordered_nodes()
                 if n.role in ("Formula", "Figure") and n.bbox is not None and n.page >= 0
                 and not n.artifact and not (n.role == "Figure" and n.alt)]
        for n in cands[:40]:
            page = doc[n.page]
            r = pymupdf.Rect(n.bbox) * page.transformation_matrix
            r.normalize()
            r += (-4, -4, 4, 4)
            png = page.get_pixmap(matrix=pymupdf.Matrix(2.6, 2.6), clip=r).tobytes("png")
            ctx = ""
            if n.parent and model.nodes.get(n.parent):
                ctx = model.nodes[n.parent].text[:200]
            ans = math_from_image(cfg, png, ctx or n.text[:200])
            if not ans:
                continue
            if ans["decorative"] and n.role == "Figure":
                out.append({"id": n.id, "kind": "math", "from": "Figure", "to": "Artifact",
                            "field": "role", "why": "no information in the image",
                            "text": n.text[:100], "page": n.page})
                continue
            if ans["is_math"]:
                if n.role != "Formula":
                    out.append({"id": n.id, "kind": "math", "from": n.role, "to": "Formula",
                                "field": "role", "why": ans["why"] if "why" in ans else "an equation",
                                "text": (ans["alt"] or n.text)[:100], "page": n.page})
                if ans["alt"] and ans["alt"] != n.alt:
                    out.append({"id": n.id, "kind": "math", "from": n.alt or "(no Alt)",
                                "to": ans["alt"], "field": "alt",
                                "why": ("LaTeX: " + ans["latex"]) if ans["latex"] else "read aloud",
                                "text": n.text[:100], "page": n.page})
    finally:
        doc.close()
    return jsonify({"ok": True, "suggestions": out})


# --------------------------------------------------------------- build
@app.route("/api/job/<jid>/build", methods=["POST"])
def api_build(jid):
    b = request.get_json(force=True) or {}
    meta = read_meta(jid)
    if meta.get("stage") in ("queued", "processing", "building"):
        return _err("the job is busy")
    opts = Options.from_dict({**(meta.get("options") or {}), **(b.get("options") or {})})
    STOP.discard(jid)
    update_meta(jid, options=opts.to_dict(), stage="building", progress=0, message="queued")
    TASKS.put((jid, "build"))
    return jsonify({"ok": True})


@app.route("/job/<jid>/download/<what>")
def download(jid, what):
    d = job_dir(jid)
    meta = read_meta(jid)
    stem = os.path.splitext(meta["filename"])[0]
    files = {"pdf": ("accessible.pdf", f"{stem}_accessible.pdf"),
             "report": ("report.xlsx", f"{stem}_accessibility_report.xlsx"),
             "original": ("original.pdf", meta["filename"]),
             "working": ("working.pdf", f"{stem}_preflight.pdf"),
             "model": ("model.json", f"{stem}_tags.json")}
    if what not in files:
        abort(404)
    name, dl = files[what]
    path = os.path.join(d, name)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=dl)


# ------------------------------------------------------------ settings
@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        cfg = load_config()
        return jsonify({**{k: v for k, v in cfg.items() if k not in ("templates", "privacy")}, "env": env_status()})
    if not is_admin():
        return _err("only an admin changes the settings", 403)
    b = request.get_json(force=True) or {}
    with config_edit() as cfg:
        _apply_settings(cfg, b)
    apply_parallel(cfg)
    return jsonify({"ok": True, "env": env_status()})


def _apply_settings(cfg: dict, b: dict) -> None:
    if "ai" in b and isinstance(b["ai"], dict):
        if b["ai"].get("base_url"):
            cfg["ai"]["base_url"] = str(b["ai"]["base_url"])[:300]
        for k in ("text_model", "vision_model"):
            if k in b["ai"]:
                cfg["ai"][k] = str(b["ai"][k] or "").strip()[:300]
        if "enabled" in b["ai"]:
            cfg["ai"]["enabled"] = bool(b["ai"]["enabled"])
        if "timeout" in b["ai"]:
            try:
                cfg["ai"]["timeout"] = max(10, min(900, int(b["ai"]["timeout"])))
            except Exception:
                pass
        if "keep_alive" in b["ai"]:
            cfg["ai"]["keep_alive"] = str(b["ai"]["keep_alive"] or "").strip()[:20]
        if "max_image_px" in b["ai"]:
            try:
                cfg["ai"]["max_image_px"] = max(300, min(4000, int(b["ai"]["max_image_px"])))
            except Exception:
                pass
        if isinstance(b["ai"].get("task_models"), dict):
            from core.ai import TASKS
            cfg["ai"]["task_models"] = {k: str(v or "").strip()[:120]
                                        for k, v in b["ai"]["task_models"].items()
                                        if k in TASKS and str(v or "").strip()}
    if "verapdf" in b:
        cfg["verapdf"] = str(b["verapdf"] or "")[:500]
    if "default_profile" in b:
        cfg["default_profile"] = str(b["default_profile"])[:80]
    if isinstance(b.get("parallel"), dict):
        p = cfg.setdefault("parallel", {})
        for k, hi in (("files", MAX_FILES), ("page_procs", 64)):
            if k in b["parallel"]:
                try:
                    p[k] = max(0, min(hi, int(b["parallel"][k] or 0)))
                except (TypeError, ValueError):
                    pass


@app.route("/api/profiles", methods=["POST"])
def api_profiles():
    if not is_admin():
        return _err("only an admin saves or deletes profiles", 403)
    b = request.get_json(force=True) or {}
    name = str(b.get("name", "")).strip()[:80]
    if not name or name in BUILTIN_PROFILES:
        return _err("choose a new profile name")
    opts = None if b.get("delete") else Options.from_dict(b.get("options") or {}).to_dict()
    with config_edit() as cfg:
        if opts is None:
            cfg["profiles"].pop(name, None)
        else:
            cfg["profiles"][name] = opts
    return jsonify({"ok": True, "profiles": all_profiles()})


# ================================================================ work + admin (V4.19)
WORK_STATES = WF.WORK_STATES


def _time_fields(m: dict, pend: dict | None = None) -> dict:
    """The working time of a file for a list (V4.30): Working and QC, and who spent it (what is written, and the
    seconds counted since — `pend` is TIMES.pending_all())."""
    t = m.get("time") or {}
    by: dict[str, dict] = {u: {"work": float(v.get("work") or 0), "qc": float(v.get("qc") or 0), "last": v.get("last") or ""}
                           for u, v in (t.get("by") or {}).items()}
    work, qc = float(t.get("work") or 0), float(t.get("qc") or 0)
    for (u, kind), sec in ((pend or {}).get(secure_filename(m.get("id") or "")) or {}).items():
        row = by.setdefault(u, {"work": 0.0, "qc": 0.0, "last": ""})
        row[kind] += sec
        if kind == "work":
            work += sec
        else:
            qc += sec
    rows = [{"user": u, "work_s": round(v["work"]), "qc_s": round(v["qc"]), "last": v["last"]} for u, v in by.items()]
    rows.sort(key=lambda x: -(x["work_s"] + x["qc_s"]))
    return {"work_s": round(work), "qc_s": round(qc), "time_by": rows[:12], "time_last": t.get("last") or ""}


def _job_row(m: dict, pend: dict | None = None) -> dict:
    sa, sb = m.get("score_after") or {}, m.get("score_before") or {}
    return {"id": m.get("id"), "filename": m.get("filename"), "stage": m.get("stage"),
            "progress": m.get("progress"), "message": m.get("message"), "pages": m.get("pages") or 0,
            "profile": m.get("profile") or "Default", "created": m.get("created"), "edited": m.get("edited"),
            "built": m.get("built"), "built_at": m.get("built_at"), "owner": m.get("owner") or "",
            "assigned_to": m.get("assigned_to") or "", "assigned_at": m.get("assigned_at") or "",
            "work": m.get("work") or "", "priority": m.get("priority") or "normal", "due": m.get("due") or "",
            "batch": m.get("batch") or "", "batch_name": m.get("batch_name") or "", "rel_path": m.get("rel_path") or "",
            "errors_before": sb.get("errors"), "errors_after": sa.get("errors"), "dirty": m.get("dirty"),
            "notes": (m.get("notes") or [])[-5:], "title": m.get("title") or "", "error": m.get("error") or "",
            "mode": m.get("mode") or "", "nodes": m.get("nodes") or 0, "sample_id": m.get("sample_id") or "",
            "samples_changed": (m.get("samples") or {}).get("changed"), "lang": m.get("lang") or "",
            "profile_history": (m.get("profile_history") or [])[-3:], "approved_at": m.get("approved_at") or "",
            **_time_fields(m, pend), **WF.summary(m)}


def _set_assignee(jid: str, to: str, how: str = "allocated") -> dict:
    """Give a file to a user (or "" — back to the pool), keeping the history
    so it can be put back (revert)."""
    with job_lock(jid):
        meta = read_meta(jid)
        old = meta.get("assigned_to") or ""
        if old == to and how != "revert":
            return meta
        hist = list(meta.get("alloc_history") or [])
        if how != "revert":
            hist.append({"at": datetime.now().isoformat(timespec="seconds"), "by": current_user_name(),
                         "from": old, "to": to, "work": meta.get("work") or ""})
        meta["alloc_history"] = hist[-50:]
        meta["assigned_to"] = to
        meta["assigned_at"] = datetime.now().isoformat(timespec="seconds") if to else ""
        if how != "revert":
            meta["work"] = "assigned" if to else ""
        write_meta(jid, meta)
    log_action(how, jid, meta.get("filename", ""), f"{old or 'pool'} → {to or 'pool'}")
    return meta


@app.route("/api/job/<jid>/work", methods=["POST"])
def api_job_work(jid):
    """The file's work status (V4.19), now the steps of the workflow (V4.23):
    start / pick, submit (to QA — see /wf/submit for the PAC report),
    approve, return (rework), reopen (admin), a note."""
    b = request.get_json(force=True) or {}
    act = str(b.get("action") or "")
    text = str(b.get("text") or "").strip()[:1000]
    me = current_user()
    with job_lock(jid):
        meta = read_meta(jid)
        if act == "start":
            WF.start(meta, me, wf_cfg())
        elif act == "submit":
            WF.submit(meta, me, wf_cfg(), None, text, built=bool(meta.get("built")), dirty=bool(meta.get("dirty")))
        elif act == "approve":
            if WF.role_of(me) == "admin" and WF.stage(meta) not in ("qa", "final_qa"):
                WF.move(meta, me, "approved", text)          # the admin's word: ready, the checks skipped
            else:
                WF.approve(meta, me, wf_cfg(), text, built=bool(meta.get("built")), dirty=bool(meta.get("dirty")))
        elif act == "return":
            WF.rework(meta, me, text)
        elif act == "reopen":
            WF.move(meta, me, "in_progress", text)
        elif act != "note":
            return _err("unknown action")
        if text or act == "note":
            if not text:
                return _err("write the note first")
            meta.setdefault("notes", []).append({"at": datetime.now().isoformat(timespec="seconds"),
                                                 "by": current_user_name(), "text": text, "action": act})
        write_meta(jid, meta)
    log_action({"start": "started", "submit": "sent to QA", "approve": "approved", "return": "sent to rework",
                "reopen": "reopened", "note": "noted"}[act], jid, meta.get("filename", ""), text)
    learned = _work_changed(jid, meta.get("work"))
    return jsonify({"ok": True, "work": meta.get("work"), "notes": meta.get("notes", [])[-20:], "sample": learned})


# ================================================================ the workflow (V4.23)
def _wf_users(role: str) -> list[str]:
    return [u["username"] for u in ACCOUNTS.all() if u.get("active", True) and WF.norm_role(u.get("role")) == role]


def _wf_view(jid: str, meta: dict) -> dict:
    """Everything the Review panel shows about a file and what the signed-in
    user may do with it."""
    me = current_user()
    r = WF.role_of(me)
    w = WF.wf(meta)
    st = WF.stage(meta)
    d = job_dir(jid)
    cfg = wf_cfg()
    editing = WF.can_edit(meta, me)
    holder = w["qa_user"] if st == "qa" else w["fqa_user"] if st == "final_qa" else meta.get("assigned_to") or ""
    mine_prod = r in ("production", "admin") and st == "production" and (r == "admin" or editing)
    out = {
        "id": jid, "filename": meta.get("filename"), "work": meta.get("work") or "", "stage": st,
        "stage_label": WF.STAGE_LABELS.get(st, st), "states": WF.WORK_STATES, "stages": WF.STAGES,
        "stage_labels": WF.STAGE_LABELS, "role": r, "role_label": WF.ROLE_LABELS.get(r, r), "me": current_user_name(),
        "assigned_to": meta.get("assigned_to") or "", "owner": meta.get("owner") or "",
        "qa_user": w["qa_user"], "fqa_user": w["fqa_user"], "holder": holder,
        "rework": w["rework"], "rework_from": w["rework_from"],
        "comments": w["comments"], "history": w["history"][-80:], "checklist": w["checklist"].get(st, {}),
        "checklist_items": WF.CHECKLIST, "categories": WF.CATEGORIES, "severities": WF.SEVERITIES,
        "pac": [{**{k: p.get(k) for k in ("name", "at", "by", "round", "result", "errors", "size")}, "k": k}
                for k, p in enumerate(w["pac"])],
        "pac_ext": list(WF.PAC_EXT), "cfg": cfg,
        "built": bool(meta.get("built")), "dirty": bool(meta.get("dirty")),
        "output": os.path.exists(os.path.join(d, "accessible.pdf")),
        "priority": meta.get("priority") or "normal", "due": meta.get("due") or "",
        "times": WF.time_in_stages(meta), "readonly": WF.read_only_reason(meta, me),
        "score_after": meta.get("score_after") or {}, "score_before": meta.get("score_before") or {},
        "can": {
            "edit": editing,
            "start": (st == "production" and mine_prod and (meta.get("work") or "") in ("assigned", "returned", ""))
                     or (st == "qa" and r in ("qa", "admin") and (meta.get("work") or "") in ("qa", "submitted")
                         and (not w["qa_user"] or w["qa_user"].lower() == current_user_name().lower() or r == "admin"))
                     or (st == "final_qa" and r in ("final_qa", "admin") and meta.get("work") == "final_qa"
                         and (not w["fqa_user"] or w["fqa_user"].lower() == current_user_name().lower() or r == "admin")),
            "submit": mine_prod and meta.get("work") != "on_hold",
            "approve": st in ("qa", "final_qa") and (r == "admin" or (r == "qa" and st == "qa" and editing)
                                                     or (r == "final_qa" and st == "final_qa" and editing)),
            "rework": (st in ("qa", "final_qa") and (r == "admin" or (r == "qa" and st == "qa" and editing)
                                                      or (r == "final_qa" and st == "final_qa" and editing)))
                      or (st in ("delivery", "delivered") and r == "admin"),
            "comment": r == "admin" or (r in ("qa", "final_qa") and editing),
            "fix": r == "admin" or (r == "production" and st == "production" and editing),
            "checklist": st in ("qa", "final_qa") and editing and r != "production",
            "deliver": (meta.get("work") or "") == "approved" and r in ("admin", "final_qa"),
            "admin": r == "admin",
        },
        "missing": WF.check_ready_for_qa(meta, cfg, {"name": "x"}, bool(meta.get("built")), bool(meta.get("dirty")))
                   if st == "production" else [],
    }
    if r == "admin":
        out["users"] = {"production": _wf_users("production"), "qa": _wf_users("qa"),
                        "final_qa": _wf_users("final_qa")}
    return out


def _auto_assign(meta: dict) -> None:
    """Admin → Workflow → "give checks out by themselves": a file reaching QA
    or Final QA with nobody to check it goes to the checker with the fewest
    pages waiting."""
    if not wf_cfg().get("auto_assign"):
        return
    st = WF.stage(meta)
    if st not in ("qa", "final_qa"):
        return
    w = WF.wf(meta)
    key = "qa_user" if st == "qa" else "fqa_user"
    if w.get(key):
        return
    who = WF.pick_checker(st, _wf_users(st), [m for m in list_jobs(100000) if m.get("id") != meta.get("id")])
    if who:
        w[key] = who
        WF._event(meta, {"username": "system", "role": "admin"}, f"given to {who}", meta.get("work") or "",
                  meta.get("work") or "", auto=True)


@app.route("/api/wf/queue")
def api_wf_queue():
    """What waits for the signed-in user (the header badge)."""
    me = current_user()

    def build():
        q = WF.my_queue(me, list_jobs(100000, visible=True))
        return {"ok": True, **q, "role": WF.role_of(me), "role_label": WF.ROLE_LABELS.get(WF.role_of(me), "")}
    return serve_cached(LIST_BYTES, ("wfq", (me or {}).get("username")),
                        etag_of(JOBINDEX.stamp(), (me or {}).get("username"), (me or {}).get("role")), build)


@app.route("/api/wf/next", methods=["POST"])
def api_wf_next():
    """Take the next file: your rework first, then what you are checking or
    working on, then the queue (urgent, due date, waited longest). A file in
    the QA / Final QA queue is picked (it becomes yours)."""
    me = current_user()
    m = WF.next_for(me, list_jobs(100000, visible=True))
    if m is None:
        return jsonify({"ok": True, "id": None, "message": "Nothing waits for you now 🎉"})
    jid = m["id"]
    kind = WF.todo_kind(m, me)
    if kind in ("pick", "start"):
        with job_lock(jid):
            meta = read_meta(jid)
            if WF.todo_kind(meta, me) in ("pick", "start"):
                ev = WF.start(meta, me, wf_cfg())
                write_meta(jid, meta)
                log_action(ev["action"], jid, meta.get("filename", ""), "took the next file")
    return jsonify({"ok": True, "id": jid, "filename": m.get("filename"), "kind": kind})


@app.route("/api/job/<jid>/wf", methods=["GET", "POST"])
def api_job_wf(jid):
    """The workflow of one file: GET what it is and what you may do; POST a
    step: start (pick), approve, rework, deliver, hold / unhold, move (admin),
    checker (admin), check (a checklist line), note."""
    if request.method == "GET":
        return jsonify(_wf_view(jid, read_meta(jid)))
    b = request.get_json(force=True) or {}
    act = str(b.get("action") or "")
    note = str(b.get("note") or "").strip()[:1000]
    me = current_user()
    with job_lock(jid):
        meta = read_meta(jid)
        if act == "start":
            ev = WF.start(meta, me, wf_cfg())
        elif act == "approve":
            ev = WF.approve(meta, me, wf_cfg(), note, built=bool(meta.get("built")), dirty=bool(meta.get("dirty")))
            _auto_assign(meta)
        elif act == "rework":
            ev = WF.rework(meta, me, note)
        elif act == "deliver":
            ev = WF.deliver(meta, me, note)
        elif act in ("hold", "unhold"):
            ev = WF.hold(meta, me, act == "hold", note)
        elif act == "move":
            ev = WF.move(meta, me, str(b.get("to") or ""), note)
        elif act == "checker":
            ev = WF.set_checker(meta, me, str(b.get("which") or "qa"), str(b.get("who") or ""))
        elif act == "check":
            WF.set_check(meta, me, str(b.get("item") or ""), str(b.get("value") or ""))
            ev = None
        elif act == "note":
            if not note:
                return _err("write the note first")
            ev = None
        else:
            return _err("unknown step")
        if note and act not in ("check",):
            meta.setdefault("notes", []).append({"at": datetime.now().isoformat(timespec="seconds"),
                                                 "by": current_user_name(), "text": note, "action": act})
        write_meta(jid, meta)
    if ev:
        log_action(ev["action"], jid, meta.get("filename", ""), note or f"{ev['from'] or 'pool'} → {ev['to']}")
        _work_changed(jid, meta.get("work"))
    return jsonify({"ok": True, **_wf_view(jid, meta)})


@app.route("/api/job/<jid>/wf/submit", methods=["POST"])
def api_job_wf_submit(jid):
    """Production → QA with the PAC report (multipart: pac = the file,
    result = pass / fail, errors = how many PAC failures, note)."""
    me = current_user()
    f = request.files.get("pac")
    note = (request.form.get("note") or "").strip()[:1000]
    pac = None
    with job_lock(jid):
        meta = read_meta(jid)
        if f and f.filename:
            ext = os.path.splitext(f.filename)[1].lower()
            if ext not in WF.PAC_EXT:
                return _err("the PAC report is a PDF, HTML, XML, text, image or zip file")
            folder = os.path.join(job_dir(jid), "pac")
            os.makedirs(folder, exist_ok=True)
            k = len(WF.wf(meta)["pac"])
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(f.filename))[-80:] or ("report" + ext)
            stored = f"{k + 1:02d}_{safe}"
            f.save(os.path.join(folder, stored))
            size = os.path.getsize(os.path.join(folder, stored))
            if size > 40 * 1024 * 1024:
                os.remove(os.path.join(folder, stored))
                return _err("the PAC report is too big (40 MB at most)")
            res = (request.form.get("result") or "").lower()
            try:
                errs = int(request.form.get("errors")) if (request.form.get("errors") or "").strip() else None
            except ValueError:
                errs = None
            pac = {"name": os.path.basename(f.filename)[:120], "file": stored, "size": size,
                   "result": res if res in ("pass", "fail") else "", "errors": errs}
        try:
            ev = WF.submit(meta, me, wf_cfg(), pac, note, built=bool(meta.get("built")), dirty=bool(meta.get("dirty")))
        except WF.WorkflowError:
            if pac:                      # the report stays out until the file can go
                try:
                    os.remove(os.path.join(job_dir(jid), "pac", pac["file"]))
                except OSError:
                    pass
            raise
        _auto_assign(meta)
        if note:
            meta.setdefault("notes", []).append({"at": ev["at"], "by": current_user_name(), "text": note,
                                                 "action": "submit"})
        write_meta(jid, meta)
    log_action(ev["action"], jid, meta.get("filename", ""), (pac or {}).get("name", "") + (" · " + note if note else ""))
    _work_changed(jid, meta.get("work"))
    return jsonify({"ok": True, **_wf_view(jid, meta)})


@app.route("/api/job/<jid>/wf/comment", methods=["POST"])
def api_job_wf_comment(jid):
    """Review comments: add, edit (marked edited), delete, status (fixed /
    open / closed / reply)."""
    b = request.get_json(force=True) or {}
    op = str(b.get("op") or "add")
    me = current_user()
    with job_lock(jid):
        meta = read_meta(jid)
        if op == "add":
            c = WF.add_comment(meta, me, b.get("text") or "", b.get("page"), b.get("category") or "Other",
                               b.get("severity") or "Minor", b.get("tag") or "")
        elif op == "edit":
            c = WF.edit_comment(meta, me, str(b.get("id") or ""), text=b.get("text"), category=b.get("category"),
                                severity=b.get("severity"), page=b.get("page"))
        elif op == "delete":
            c = WF.delete_comment(meta, me, str(b.get("id") or ""))
        elif op == "status":
            c = WF.comment_status(meta, me, str(b.get("id") or ""), str(b.get("status") or ""), b.get("reply") or "")
        else:
            return _err("unknown comment step")
        write_meta(jid, meta)
    log_action(f"comment {op}" if op != "status" else f"comment {b.get('status')}", jid, meta.get("filename", ""),
               (c.get("text") or "")[:120])
    return jsonify({"ok": True, "comment": c, **_wf_view(jid, meta)})


@app.route("/job/<jid>/pac/<int:k>")
def download_pac(jid, k):
    w = WF.wf(read_meta(jid))
    if not 0 <= k < len(w["pac"]):
        abort(404)
    p = w["pac"][k]
    path = os.path.join(job_dir(jid), "pac", p.get("file") or "")
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=p.get("name") or os.path.basename(path))


@app.route("/api/admin/workflow", methods=["GET", "POST"])
def api_admin_workflow():
    """The workflow settings and the pipeline: every file by step."""
    if request.method == "POST":
        b = request.get_json(force=True) or {}
        with config_edit() as cfg:
            cur = {**WF.DEFAULTS, **(cfg.get("workflow") or {})}
            for k in WF.DEFAULTS:
                if k in b:
                    if k == "work_idle_s":                    # V4.30: seconds, 30 … 900
                        try:
                            cur[k] = max(30, min(900, int(b[k])))
                        except (TypeError, ValueError):
                            return _err("the idle time is a number of seconds (30 to 900)")
                    else:
                        cur[k] = bool(b[k])
            cfg["workflow"] = cur
        log_action("workflow settings", "", "", ", ".join(
            f"{k}={v}" if k == "work_idle_s" else f"{k}={'on' if v else 'off'}" for k, v in cur.items()))
    from collections import Counter
    jobs = list_jobs(100000)
    by = Counter(WF.stage(m) for m in jobs)
    states = Counter(m.get("work") or "" for m in jobs)
    rework_files = [m for m in jobs if (m.get("wf") or {}).get("rework")]
    people: dict[str, dict] = {}
    cats: Counter = Counter()
    for m in jobs:
        w = m.get("wf") or {}
        prod = (m.get("assigned_to") or "").lower()          # who does the production work
        if prod:
            p = people.setdefault(prod, {"user": prod, "files": 0, "rework_files": 0, "reworks": 0, "comments": 0, "delivered": 0})
            p["files"] += 1
            p["reworks"] += int(w.get("rework") or 0)
            p["rework_files"] += 1 if w.get("rework") else 0
            p["comments"] += len(w.get("comments") or [])
            p["delivered"] += 1 if (m.get("work") in ("approved", "delivered")) else 0
        for c in w.get("comments") or []:
            cats[c.get("category") or "Other"] += 1
    checkers: dict[str, dict] = {}
    for m in jobs:
        w = m.get("wf") or {}
        for e in w.get("history") or []:
            if e.get("role") in ("qa", "final_qa") and e.get("action") in ("QA approved", "Final QA approved") or \
                    (e.get("role") in ("qa", "final_qa") and str(e.get("action", "")).startswith("rework")):
                c = checkers.setdefault(e["by"].lower(), {"user": e["by"], "role": e.get("role"), "approved": 0, "rework": 0})
                if "approved" in e["action"]:
                    c["approved"] += 1
                else:
                    c["rework"] += 1
    board = {}
    for m in jobs:
        st = WF.stage(m)
        if len(board.setdefault(st, [])) < 60:
            board[st].append({"id": m["id"], "filename": m.get("filename"), "work": m.get("work") or "",
                              "assigned_to": m.get("assigned_to") or "", "pages": m.get("pages") or 0,
                              "priority": m.get("priority") or "normal", "due": m.get("due") or "", **WF.summary(m)})
    first_pass = sum(1 for m in jobs if m.get("work") in ("approved", "delivered") and not (m.get("wf") or {}).get("rework"))
    done = sum(1 for m in jobs if m.get("work") in ("approved", "delivered"))
    return jsonify({"ok": True, "cfg": wf_cfg(), "setting_labels": WF.SETTING_LABELS,
                    "setting_order": list(WF.SETTING_LABELS), "stages": dict(by),
                    "states": dict(states), "labels": WF.STAGE_LABELS,
                    "work_labels": WF.WORK_STATES, "board": board, "people": sorted(people.values(), key=lambda p: -p["files"]),
                    "checkers": sorted(checkers.values(), key=lambda c: -(c["approved"] + c["rework"])),
                    "categories": dict(cats.most_common()), "rework_files": len(rework_files),
                    "first_pass": first_pass, "done": done,
                    "first_pass_rate": round(100 * first_pass / done) if done else None,
                    "roles": {r: len(_wf_users(r)) for r in ("production", "qa", "final_qa")}})


@app.route("/admin")
def admin_page():
    return render_template("admin.html", users=ACCOUNTS.all(), profiles=all_profiles(),
                           default_profile=load_config().get("default_profile", "Default"),
                           env=env_status(), cfg=load_config(), states=WORK_STATES)


@app.route("/api/admin/overview")
def api_admin_overview():
    from collections import Counter
    jobs = list_jobs(100000)
    users = ACCOUNTS.all()
    per: dict[str, dict] = {u["username"].lower(): {**u, "files": 0, "pages": 0, "by_work": Counter(),
                                                     "open": 0, "done": 0, "errors_left": 0, "last": ""} for u in users}
    pool = {"files": 0, "pages": 0}
    work = Counter()
    for m in jobs:
        a = (m.get("assigned_to") or "").lower()
        work[m.get("work") or ""] += 1
        if not a:
            pool["files"] += 1
            pool["pages"] += int(m.get("pages") or 0)
            continue
        p = per.get(a)
        if p is None:
            continue
        p["files"] += 1
        p["pages"] += int(m.get("pages") or 0)
        p["by_work"][m.get("work") or ""] += 1
        if (m.get("work") or "") in ("approved", "delivered"):
            p["done"] += 1
        else:
            p["open"] += 1
        p["errors_left"] += int(((m.get("score_after") or m.get("score_before") or {}).get("errors")) or 0)
        p["last"] = max(p["last"], m.get("edited") or m.get("assigned_at") or "")
    for p in per.values():
        p["by_work"] = dict(p["by_work"])
    # V4.20: profiles, due dates, the last two weeks, fixes, sample files
    from datetime import date, timedelta
    today = date.today().isoformat()
    soon = (date.today() + timedelta(days=3)).isoformat()
    prof: dict[str, dict] = {}
    stage = Counter()
    fixed_before = fixed_after = 0
    due_list, overdue = [], 0
    for m in jobs:
        pr = prof.setdefault(m.get("profile") or "Default", {"files": 0, "pages": 0, "approved": 0, "open": 0, "errors_left": 0})
        pr["files"] += 1
        pr["pages"] += int(m.get("pages") or 0)
        if (m.get("work") or "") in ("approved", "delivered"):
            pr["approved"] += 1
        else:
            pr["open"] += 1
        sa, sb = m.get("score_after") or {}, m.get("score_before") or {}
        pr["errors_left"] += int((sa or sb).get("errors") or 0)
        if sa and sb:
            fixed_before += int(sb.get("errors") or 0)
            fixed_after += int(sa.get("errors") or 0)
        stage[m.get("stage") or ""] += 1
        due = m.get("due") or ""
        if due and (m.get("work") or "") not in ("approved", "delivered"):
            if due < today:
                overdue += 1
            if due <= soon:
                due_list.append({"id": m["id"], "filename": m.get("filename"), "due": due,
                                 "assigned_to": m.get("assigned_to") or "", "priority": m.get("priority") or "normal",
                                 "work": m.get("work") or "", "late": due < today})
    due_list.sort(key=lambda d: (d["due"], d["filename"] or ""))
    days = [(date.today() - timedelta(days=k)).isoformat() for k in range(13, -1, -1)]
    trend = {d: {"uploaded": 0, "approved": 0, "built": 0} for d in days}
    log = ACCOUNTS.recent(5000)
    for e in log:
        d = (e.get("at") or "")[:10]
        if d not in trend:
            continue
        a = e.get("action") or ""
        if a == "uploaded":
            mm = re.match(r"(\d+)", e.get("detail") or "")
            trend[d]["uploaded"] += int(mm.group(1)) if mm else 1
        elif a in ("approved", "Final QA approved"):
            trend[d]["approved"] += 1
        elif a in ("built", "build done", "build started"):
            trend[d]["built"] += 1
    try:
        samp = {"clients": LEARN.clients(), "queued": len(LEARN.queued())}
    except Exception:
        samp = {"clients": [], "queued": 0}
    return jsonify({"users": list(per.values()), "pool": pool, "work": dict(work), "files": len(jobs),
                    "pages": sum(int(m.get("pages") or 0) for m in jobs),
                    "approved_pages": sum(int(m.get("pages") or 0) for m in jobs if (m.get("work") or "") in ("approved", "delivered")),
                    "busy": sum(1 for m in jobs if m.get("stage") in ("queued", "processing", "building")),
                    "failed": sum(1 for m in jobs if m.get("stage") == "error"),
                    "stages": dict(stage), "profiles": prof, "due": due_list[:12], "overdue": overdue,
                    "urgent": sum(1 for m in jobs if m.get("priority") == "urgent" and (m.get("work") or "") not in ("approved", "delivered")),
                    "fixed": {"before": fixed_before, "after": fixed_after},
                    "trend": [{"day": d, **trend[d]} for d in days], "recent": log[:10], "samples": samp,
                    "states": WORK_STATES, "queue": queue_info(), "version": VERSION})


@app.route("/api/admin/files")
def api_admin_files():
    locks = EDITLOCK.snapshot() if lock_enabled() else {}
    stamp = etag_of(JOBINDEX.stamp(), _locks_stamp(locks), TIMES.stamp())

    def build():
        pend = TIMES.pending_all()
        rows = [_job_row(m, pend) for m in list_jobs(100000)]
        for r in rows:
            h = locks.get(secure_filename(r.get("id") or ""))
            if h:
                r["locked_by"], r["lock_idle_s"] = h["user"], h["idle_s"]
        return rows
    return serve_cached(LIST_BYTES, ("admin_files",), stamp, build)


@app.route("/api/admin/files/unlock", methods=["POST"])
def api_admin_unlock():
    """Let go of the files nobody should hold any more (a person went home without closing the editor)."""
    n = 0
    for i in _ids(request.get_json(force=True) or {}):
        before = EDITLOCK.holder(secure_filename(i))
        if EDITLOCK.release(secure_filename(i), force=True):
            n += 1
            log_action("unlocked the file", i, "", f"was held by {before['user']}" if before else "")
    return jsonify({"ok": True, "unlocked": n})


def _held_by_other(i: str) -> str:
    """Who else has the file open for work ("" when nobody): the admin's steps for many files leave it alone (V4.29)."""
    if not lock_enabled():
        return ""
    h = EDITLOCK.holder(secure_filename(i))
    return h["user"] if h and not _same_user(h["user"], current_user_name()) else ""


def _ids(b: dict) -> list[str]:
    out = []
    for i in b.get("ids") or []:
        i = os.path.basename(str(i))
        if os.path.exists(os.path.join(JOBS, i, "meta.json")):
            out.append(i)
    return out


@app.route("/api/admin/files/assign", methods=["POST"])
def api_admin_assign():
    """Give files to users: all to one user, in turn to several (round robin),
    or so that every user ends up with about the same number of pages
    (balance). A file given before moves to its new user (reallocate)."""
    b = request.get_json(force=True) or {}
    ids = _ids(b)
    mode = str(b.get("mode") or "set")
    users = [u for u in (b.get("users") or ([b.get("to")] if b.get("to") else [])) if u and ACCOUNTS.get(u)]
    users = [ACCOUNTS.get(u)["username"] for u in users]
    if not ids:
        return _err("tick the files first")
    if not users:
        return _err("pick the user(s)")
    metas = {i: read_meta(i) for i in ids}
    plan: dict[str, str] = {}
    if mode == "round_robin":
        for k, i in enumerate(ids):
            plan[i] = users[k % len(users)]
    elif mode == "balance":
        load = {u.lower(): 0 for u in users}
        for m in list_jobs(100000):
            a = (m.get("assigned_to") or "").lower()
            if a in load and m["id"] not in metas and (m.get("work") or "") != "approved":
                load[a] += int(m.get("pages") or 1)
        for i in sorted(ids, key=lambda i: -int(metas[i].get("pages") or 1)):
            u = min(users, key=lambda u: load[u.lower()])
            plan[i] = u
            load[u.lower()] += int(metas[i].get("pages") or 1)
    else:
        plan = {i: users[0] for i in ids}
    for i, u in plan.items():
        _set_assignee(i, u, "reallocated" if metas[i].get("assigned_to") else "allocated")
    return jsonify({"ok": True, "plan": plan})


@app.route("/api/admin/files/unassign", methods=["POST"])
def api_admin_unassign():
    ids = _ids(request.get_json(force=True) or {})
    for i in ids:
        _set_assignee(i, "", "unassigned")
    return jsonify({"ok": True, "count": len(ids)})


@app.route("/api/admin/files/revert", methods=["POST"])
def api_admin_revert():
    """Put the last allocation change back (the file goes to whoever had it
    before, with the work status it had)."""
    ids = _ids(request.get_json(force=True) or {})
    done, skipped = 0, []
    for i in ids:
        with job_lock(i):
            meta = read_meta(i)
            hist = list(meta.get("alloc_history") or [])
            if not hist:
                skipped.append(meta.get("filename", i))
                continue
            last = hist.pop()
            meta["alloc_history"] = hist
            meta["work"] = last.get("work") or ("assigned" if last.get("from") else "")
            write_meta(i, meta)
        _set_assignee(i, last.get("from") or "", "revert")
        done += 1
    return jsonify({"ok": True, "reverted": done, "skipped": skipped})


@app.route("/api/admin/files/update", methods=["POST"])
def api_admin_update():
    """Priority, due date, work status (approve / send back / reopen) and a note, for many files."""
    b = request.get_json(force=True) or {}
    ids = _ids(b)
    for i in ids:
        with job_lock(i):
            meta = read_meta(i)
            if b.get("priority") in ("low", "normal", "high", "urgent"):
                meta["priority"] = b["priority"]
            if "due" in b:
                meta["due"] = str(b.get("due") or "")[:10]
            work = b.get("work")
            if work in WORK_STATES:
                note = (b.get("note") or "").strip()[:1000]
                try:
                    if work in ("returned", "rework") and WF.stage(meta) in ("qa", "final_qa", "delivery", "delivered"):
                        WF.rework(meta, current_user(), note or "sent back by the admin")
                    elif work == "on_hold" and meta.get("work") != "on_hold":
                        WF.hold(meta, current_user(), True, note)
                    elif work != (meta.get("work") or ""):
                        if meta.get("work") == "on_hold":
                            WF.wf(meta).pop("hold_from", None)
                        WF.move(meta, current_user(), "rework" if work == "returned" else work, note)
                except WF.WorkflowError:
                    pass
            if (b.get("note") or "").strip():
                meta.setdefault("notes", []).append({"at": datetime.now().isoformat(timespec="seconds"),
                                                     "by": current_user_name(), "text": b["note"].strip()[:1000],
                                                     "action": "note"})
            write_meta(i, meta)
        if b.get("priority") in ("low", "normal", "high", "urgent"):
            TASKS.rekey(lambda it, i=i: it[0] == i)           # a file made urgent moves up the queue
        log_action("updated", i, meta.get("filename", ""),
                   ", ".join(f"{k}={b[k]}" for k in ("priority", "due", "work") if k in b))
        if b.get("work") in WORK_STATES:
            if b["work"] == "approved":
                log_action("approved", i, meta.get("filename", ""))
            _work_changed(i, meta.get("work"))
    return jsonify({"ok": True, "count": len(ids)})


@app.route("/api/admin/files/delete", methods=["POST"])
def api_admin_delete():
    ids = _ids(request.get_json(force=True) or {})
    done, skipped = 0, []
    for i in ids:
        meta = read_meta(i)
        who = _held_by_other(i)
        if who:
            skipped.append(f"{meta.get('filename', i)} ({who} is working in it)")
            continue
        _stop(i, wait=True)
        shutil.rmtree(job_dir(i), ignore_errors=True)
        JOBINDEX.touch(secure_filename(i))
        EDITLOCK.release(secure_filename(i), force=True)
        log_action("deleted", i, meta.get("filename", ""))
        done += 1
    return jsonify({"ok": True, "count": done, "skipped": skipped})


@app.route("/api/admin/files/reanalyze", methods=["POST"])
def api_admin_reanalyze():
    ids = _ids(request.get_json(force=True) or {})
    started, busy, skipped = 0, 0, []
    for i in ids:
        meta = read_meta(i)
        if meta.get("stage") in ("queued", "processing", "building"):
            busy += 1
            continue
        who = _held_by_other(i)
        if who:
            skipped.append(f"{meta.get('filename', i)} ({who} is working in it)")
            continue
        update_meta(i, stage="queued", progress=0, message="waiting", built=False, error="", build_error="",
                    dirty=False, reanalysed=datetime.now().isoformat(timespec="seconds"))
        TASKS.put((i, "prepare"))
        started += 1
        log_action("re-analysed", i, meta.get("filename", ""))
    return jsonify({"ok": True, "started": started, "busy": busy, "skipped": skipped})


@app.route("/api/admin/files/build", methods=["POST"])
def api_admin_build():
    """Build the accessible PDF of many files at once (each with its own options)."""
    ids = _ids(request.get_json(force=True) or {})
    started, skipped = 0, []
    for i in ids:
        meta = read_meta(i)
        if meta.get("stage") not in ("review", "done") or not os.path.exists(os.path.join(job_dir(i), "model.json")):
            continue
        who = _held_by_other(i)
        if who:
            skipped.append(f"{meta.get('filename', i)} ({who} is working in it)")
            continue
        STOP.discard(i)
        update_meta(i, stage="building", progress=0, message="waiting to build")
        TASKS.put((i, "build"))
        started += 1
        log_action("build started", i, meta.get("filename", ""))
    return jsonify({"ok": True, "started": started, "skipped": skipped})


@app.route("/api/admin/files/download.zip")
def api_admin_download():
    """The accessible PDFs (and reports) of the files asked for, in one zip."""
    import zipfile
    ids = [os.path.basename(i) for i in (request.args.get("ids") or "").split(",") if i]
    with_reports = request.args.get("reports") == "1"
    buf = io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i in ids:
            d = os.path.join(JOBS, i)
            if not os.path.exists(os.path.join(d, "meta.json")):
                continue
            meta = read_meta(i)
            stem = os.path.splitext(meta.get("filename") or i)[0]
            folder = (meta.get("batch_name") or "").strip()
            pre = f"{folder}/" if folder else ""
            if os.path.exists(os.path.join(d, "accessible.pdf")):
                z.write(os.path.join(d, "accessible.pdf"), f"{pre}{stem}_accessible.pdf")
                n += 1
            if with_reports and os.path.exists(os.path.join(d, "report.xlsx")):
                z.write(os.path.join(d, "report.xlsx"), f"{pre}{stem}_accessibility_report.xlsx")
            if with_reports:               # the PAC reports of the workflow (V4.23)
                pacs = (meta.get("wf") or {}).get("pac") or []
                for k, pr in enumerate(pacs):
                    src = os.path.join(d, "pac", pr.get("file") or "")
                    if os.path.exists(src):
                        last = k == len(pacs) - 1
                        z.write(src, f"{pre}{stem}_PAC{'' if last else f'_round{k + 1}'}{os.path.splitext(src)[1]}")
            if request.args.get("original") == "1" and os.path.exists(os.path.join(d, "original.pdf")):
                z.write(os.path.join(d, "original.pdf"), f"{pre}{stem}_input.pdf")
    if not n:
        return _err("none of those files is built yet", 404)
    buf.seek(0)
    log_action("downloaded", "", "", f"{n} built file(s)")
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"accessible_pdfs_{datetime.now():%Y%m%d_%H%M}.zip")


# ------------------------------------------------------------------------ working time (V4.30)
@app.route("/api/job/<jid>/time")
def api_job_time(jid):
    """The working time of one file: Working and QC, and who spent it. Everybody who may see the file sees its totals
    and their own time; an admin sees every person and every day."""
    jid = secure_filename(jid)
    meta = read_meta(jid)
    t = TIMES.totals(jid)
    me = current_user_name()
    out = {"work_s": round(t["work"]), "qc_s": round(t["qc"]), "idle_limit": work_idle_limit(),
           "mine": {k: round(v) for k, v in TIMES.totals(jid, me).items()}}
    if is_admin() or RIGHTS.allowed(current_user(), "view_times"):
        by = {u: dict(v) for u, v in ((meta.get("time") or {}).get("by") or {}).items()}
        for (u, kind), sec in list((TIMES._pend.get(jid) or {}).items()):         # (held and not written yet)
            row = by.setdefault(u, {"work": 0.0, "qc": 0.0})
            row[kind] = float(row.get(kind) or 0) + sec
        out["users"] = sorted(({"user": u, "work_s": round(float(v.get("work") or 0)), "qc_s": round(float(v.get("qc") or 0)),
                                "last": v.get("last") or ""} for u, v in by.items()), key=lambda x: -(x["work_s"] + x["qc_s"]))
        out["days"] = TIMES.days(jid) if is_admin() else {}
    return jsonify(out)


def time_report(days: int, user: str = "") -> dict:
    """Admin: who worked how long, day by day — Working and QC — over the last `days` days."""
    from datetime import date
    since = (date.today() - timedelta(days=max(1, days) - 1)).isoformat()
    who: dict[str, dict] = {}
    per_day: dict[str, dict] = {}
    files: dict[str, int] = {}
    low = user.lower()
    for m in JOBINDEX.metas():
        t = m.get("time")
        if not t or (t.get("last") or "")[:10] < since:
            continue
        for day, people in TIMES.days(m.get("id") or "").items():
            if day < since:
                continue
            for u, v in people.items():
                if low and u.lower() != low:
                    continue
                row = who.setdefault(u, {"user": u, "work_s": 0.0, "qc_s": 0.0, "files": set(), "days": {}})
                row["work_s"] += float(v.get("work") or 0)
                row["qc_s"] += float(v.get("qc") or 0)
                row["files"].add(m.get("id"))
                dd = row["days"].setdefault(day, [0.0, 0.0])
                dd[0] += float(v.get("work") or 0)
                dd[1] += float(v.get("qc") or 0)
                pd = per_day.setdefault(day, {"day": day, "work_s": 0.0, "qc_s": 0.0})
                pd["work_s"] += float(v.get("work") or 0)
                pd["qc_s"] += float(v.get("qc") or 0)
    today = date.today().isoformat()
    for jid, d in TIMES.pending_all().items():                          # the seconds counted and not written yet
        for (u, kind), sec in d.items():
            if low and u.lower() != low or today < since:
                continue
            row = who.setdefault(u, {"user": u, "work_s": 0.0, "qc_s": 0.0, "files": set(), "days": {}})
            row[kind + "_s"] += sec
            row["files"].add(jid)
            dd = row["days"].setdefault(today, [0.0, 0.0])
            dd[0 if kind == "work" else 1] += sec
            pd = per_day.setdefault(today, {"day": today, "work_s": 0.0, "qc_s": 0.0})
            pd[kind + "_s"] += sec
    rows = []
    for row in who.values():
        row["files"] = len(row["files"])
        row["work_s"], row["qc_s"] = round(row["work_s"]), round(row["qc_s"])
        row["days"] = {d: [round(a), round(b)] for d, (a, b) in sorted(row["days"].items())}
        rows.append(row)
    rows.sort(key=lambda r: -(r["work_s"] + r["qc_s"]))
    return {"since": since, "days": max(1, days), "users": rows,
            "per_day": [{"day": d, "work_s": round(v["work_s"]), "qc_s": round(v["qc_s"])} for d, v in sorted(per_day.items())],
            "work_s": sum(r["work_s"] for r in rows), "qc_s": sum(r["qc_s"] for r in rows), "idle_limit": work_idle_limit()}


@app.route("/api/admin/time")
def api_admin_time():
    try:
        days = max(1, min(400, int(request.args.get("days", 30))))
    except ValueError:
        days = 30
    user = (request.args.get("user") or "").strip()
    return serve_cached(LIST_BYTES, ("admin_time", days, user.lower()),
                        etag_of(JOBINDEX.stamp(), TIMES.stamp(), days, user.lower(), int(time.time() // 20)),
                        lambda: time_report(days, user))


@app.route("/api/admin/export.csv")
def api_admin_export():
    import csv
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["file", "folder", "uploaded by", "given to", "work", "step", "rework rounds", "QA", "Final QA",
                "comments", "comments open", "PAC reports", "last PAC", "priority", "due", "pages", "profile",
                "status", "errors before", "errors after", "uploaded", "given", "last edit", "built",
                "working time (min)", "QC time (min)", "time by"])
    pend = TIMES.pending_all()
    for m in list_jobs(100000):
        r = _job_row(m, pend)
        pl = r.get("pac_last") or {}
        w.writerow([r["filename"], r["batch_name"], r["owner"], r["assigned_to"], WORK_STATES.get(r["work"], r["work"]),
                    r["wstage_label"], r["rework"], r["qa_user"], r["fqa_user"], r["comments"], r["comments_open"],
                    r["pac"], (pl.get("result") or "") + (f" ({pl.get('errors')} failures)" if pl.get("errors") else ""),
                    r["priority"], r["due"], r["pages"], r["profile"], r["stage"], r["errors_before"],
                    r["errors_after"], r["created"], r["assigned_at"], r["edited"], r["built_at"],
                    round(r["work_s"] / 60, 1), round(r["qc_s"] / 60, 1),
                    "; ".join(f"{x['user']} {round(x['work_s'] / 60)}+{round(x['qc_s'] / 60)} min" for x in r["time_by"])])
    data = io.BytesIO(out.getvalue().encode("utf-8-sig"))
    return send_file(data, mimetype="text/csv", as_attachment=True,
                     download_name=f"files_{datetime.now():%Y%m%d_%H%M}.csv")


@app.route("/api/admin/rights")
def api_admin_rights():
    """Every right and what each role means by default (the Rights dialog of Admin → Users)."""
    return jsonify({"rights": RIGHTS.catalog(), "roles": ["production", "qa", "final_qa"]})


@app.route("/api/admin/users", methods=["GET", "POST"])
def api_admin_users():
    if request.method == "GET":
        return jsonify(ACCOUNTS.all())
    b = request.get_json(force=True) or {}
    try:
        u = ACCOUNTS.create(b.get("username", ""), b.get("password", ""), b.get("name", ""),
                            b.get("role", "production"), b.get("email", ""), b.get("note", ""))
    except AccountError as exc:
        return _err(str(exc))
    log_action("created user", "", "", f"{u['username']} ({u['role']})")
    return jsonify({"ok": True, "user": u})


@app.route("/api/admin/users/<name>", methods=["POST", "DELETE"])
def api_admin_user(name):
    if request.method == "DELETE":
        b = request.get_json(silent=True) or {}
        to = b.get("give_to") or ""
        if to and ACCOUNTS.get(to) is None:
            return _err("no such user to give the files to")
        try:
            ACCOUNTS.delete(name)
        except AccountError as exc:
            return _err(str(exc))
        moved = 0
        for m in list_jobs(100000):
            if (m.get("assigned_to") or "").lower() == name.lower():
                _set_assignee(m["id"], ACCOUNTS.get(to)["username"] if to else "", "reallocated" if to else "unassigned")
                moved += 1
        log_action("deleted user", "", "", f"{name}; {moved} file(s) {'to ' + to if to else 'back to the pool'}")
        return jsonify({"ok": True, "moved": moved})
    b = request.get_json(force=True) or {}
    try:
        u = ACCOUNTS.update(name, name=b.get("name"), role=b.get("role"), active=b.get("active"),
                            password=b.get("password") or None, email=b.get("email"), note=b.get("note"),
                            rights=b.get("rights") if isinstance(b.get("rights"), dict) else None)
    except AccountError as exc:
        return _err(str(exc))
    what = [k for k in ("name", "role", "active", "email", "note") if b.get(k) is not None]
    if b.get("password"):
        what.append("password reset")
    if isinstance(b.get("rights"), dict):
        what.append("rights: " + ", ".join(f"{k} {'allow' if v is True or v == 'allow' else 'deny' if v is False or v == 'deny' else 'default'}"
                                           for k, v in b["rights"].items()))
    log_action("changed user", "", "", f"{name}: {', '.join(what)}")
    return jsonify({"ok": True, "user": u})


@app.route("/api/admin/log")
def api_admin_log():
    return jsonify(ACCOUNTS.recent(int(request.args.get("limit", 300) or 300),
                                   request.args.get("user") or None, request.args.get("job") or None))


@app.route("/api/admin/log.csv")
def api_admin_log_csv():
    import csv
    rows = ACCOUNTS.recent(20000, request.args.get("user") or None, request.args.get("job") or None)
    act = request.args.get("action") or ""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["when", "who", "action", "file", "job", "detail"])
    for e in rows:
        if act and not (e.get("action") or "").startswith(act):
            continue
        w.writerow([e.get("at"), e.get("by"), e.get("action"), e.get("file"), e.get("job"), e.get("detail")])
    return app.response_class("﻿" + buf.getvalue(), mimetype="text/csv",
                              headers={"Content-Disposition": f"attachment; filename=activity-{datetime.now():%Y%m%d}.csv"})


@app.route("/api/admin/users/<name>/detail")
def api_admin_user_detail(name):
    """One user: their files by work status, pages, errors left, what they did."""
    u = ACCOUNTS.get(name)
    if u is None:
        return _err("no such user", 404)
    from collections import Counter
    mine = [m for m in list_jobs(100000) if (m.get("assigned_to") or "").lower() == u["username"].lower()
            or (m.get("owner") or "").lower() == u["username"].lower()]
    work = Counter(m.get("work") or "" for m in mine)
    rows = [_job_row(m) for m in mine]
    log = ACCOUNTS.recent(200, u["username"])
    days = Counter((e.get("at") or "")[:10] for e in log)
    return jsonify({"user": {k: v for k, v in u.items() if k != "password"}, "files": rows[:300], "work": dict(work),
                    "pages": sum(int(m.get("pages") or 0) for m in mine),
                    "pages_done": sum(int(m.get("pages") or 0) for m in mine if (m.get("work") or "") == "approved"),
                    "errors_left": sum(int(((m.get("score_after") or m.get("score_before") or {}).get("errors")) or 0)
                                       for m in mine if (m.get("work") or "") != "approved"),
                    "log": log[:60], "active_days": len(days)})


def _dir_size(path: str) -> int:
    tot = 0
    for dp, _, fs in os.walk(path):
        for f in fs:
            try:
                tot += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return tot


@app.route("/api/admin/system")
def api_admin_system():
    import platform
    jobs = list_jobs(100000)
    try:
        import shutil as _sh
        du = _sh.disk_usage(JOBS)
        disk = {"total": du.total, "free": du.free}
    except Exception:
        disk = {}
    cfg = load_config()
    return jsonify({"version": VERSION, "python": platform.python_version(), "pymupdf": pymupdf.__version__,
                    "jobs": len(jobs), "jobs_size": _dir_size(JOBS), "disk": disk, "queue": queue_info(),
                    "parallel": cfg.get("parallel"), "ai": {k: cfg["ai"].get(k) for k in ("enabled", "base_url", "text_model", "vision_model")},
                    "learning": learning_cfg(), "samples": LEARN.db_stats(), "users": len(ACCOUNTS.all()),
                    "profiles": len(all_profiles()), "stages": {s: sum(1 for m in jobs if m.get("stage") == s)
                                                                for s in ("review", "done", "error", "queued", "processing", "building", "question")},
                    "load": load_stats(), "live": dict(LIVE_STATS)})


@app.route("/api/admin/cleanup", methods=["POST"])
def api_admin_cleanup():
    """Delete old files: approved (or every file) not touched for N days.
    `preview`: only count them. A file that is a sample keeps its sample."""
    b = request.get_json(force=True) or {}
    days = max(1, int(b.get("days") or 90))
    which = b.get("which") if b.get("which") in ("approved", "failed", "all") else "approved"
    from datetime import timedelta
    edge = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    hits = []
    for m in list_jobs(100000):
        last = m.get("edited") or m.get("built_at") or m.get("created") or ""
        if last >= edge or m.get("stage") in ("queued", "processing", "building"):
            continue
        if which == "approved" and (m.get("work") or "") != "approved":
            continue
        if which == "failed" and m.get("stage") != "error":
            continue
        hits.append(m)
    size = sum(_dir_size(job_dir(m["id"])) for m in hits)
    if b.get("preview", True):
        return jsonify({"ok": True, "count": len(hits), "size": size, "files": [m.get("filename") for m in hits[:20]]})
    for m in hits:
        shutil.rmtree(job_dir(m["id"]), ignore_errors=True)
        JOBINDEX.touch(secure_filename(m["id"]))
    log_action("cleaned up", "", "", f"{len(hits)} {which} file(s) older than {days} days")
    return jsonify({"ok": True, "count": len(hits), "size": size})


@app.route("/api/admin/samples/relearn_all", methods=["POST"])
def api_admin_samples_relearn_all():
    n = 0
    for x in LEARN.samples():
        if LEARN.set_sample(x["id"], status="queued"):
            n += 1
    kick_learning()
    log_action("samples learned again", "", "", f"{n} file(s)")
    return jsonify({"ok": True, "queued": n})


@app.route("/api/job/<jid>/info")
def api_job_info(jid):
    """Who has the file and where its work stands (the editor's header)."""
    m = read_meta(jid)
    r = _job_row(m)
    r["notes"] = (m.get("notes") or [])[-20:]
    r["states"] = WORK_STATES
    r["me"] = current_user_name()
    r["admin"] = is_admin()
    if is_admin():
        r["users"] = [u["username"] for u in ACCOUNTS.all() if u.get("active", True)]
    return jsonify(r)


# ================================================================ profile of a file (V4.20)
def _change_profile(jid: str, profile: str, reanalyse: bool) -> dict:
    """Give a file another profile. Re-analysed: the tags are made again with
    the new profile (the current ones kept as model.before_reanalyse.json);
    not: the new options apply from the next build on."""
    profs = all_profiles()
    if profile not in profs:
        return {"error": f"no profile called {profile}"}
    meta = read_meta(jid)
    if meta.get("stage") in ("queued", "processing", "building"):
        return {"error": "the file is busy — wait for it to finish (or Stop it)", "busy": True}
    old = Options.from_dict(meta.get("options"))
    new = Options.from_dict(profs[profile])
    if new.existing_tags == "ask":
        new.existing_tags = old.existing_tags if old.existing_tags != "ask" else "auto"
    was = meta.get("profile") or "Default"
    upd = {"profile": profile, "options": new.to_dict(),
           "profile_history": (list(meta.get("profile_history") or []) + [
               {"at": datetime.now().isoformat(timespec="seconds"), "by": current_user_name(),
                "from": was, "to": profile, "reanalysed": bool(reanalyse)}])[-20:]}
    if reanalyse:
        src = os.path.join(job_dir(jid), "model.json")
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(job_dir(jid), "model.before_reanalyse.json"))
        _model_cache.pop(jid, None)
        STOP.discard(jid)
        upd.update(stage="queued", progress=0, message="waiting", built=False, error="", build_error="",
                   dirty=False, reanalysed=datetime.now().isoformat(timespec="seconds"))
    else:
        upd["dirty"] = bool(meta.get("built"))
    update_meta(jid, **upd)
    if reanalyse:
        TASKS.put((jid, "prepare"))
    log_action("profile changed", jid, meta.get("filename", ""),
               f"{was} → {profile}" + (" (re-analysed)" if reanalyse else " (from the next build)"))
    return {"ok": True, "profile": profile, "from": was, "reanalysed": bool(reanalyse)}


@app.route("/api/job/<jid>/profile", methods=["GET", "POST"])
def api_job_profile(jid):
    if request.method == "GET":
        m = read_meta(jid)
        return jsonify({"profile": m.get("profile") or "Default", "profiles": list(all_profiles()),
                        "history": m.get("profile_history") or [], "samples": m.get("samples")})
    b = request.get_json(force=True) or {}
    r = _change_profile(jid, str(b.get("profile") or ""), bool(b.get("reanalyse", True)))
    return (jsonify(r), 400) if r.get("error") else jsonify(r)


@app.route("/api/admin/files/profile", methods=["POST"])
def api_admin_files_profile():
    b = request.get_json(force=True) or {}
    ids = _ids(b)
    if not ids:
        return _err("tick the files first")
    done, busy, err, skipped = 0, 0, "", []
    for i in ids:
        who = _held_by_other(i)
        if who:
            skipped.append(f"{read_meta(i).get('filename', i)} ({who} is working in it)")
            continue
        r = _change_profile(i, str(b.get("profile") or ""), bool(b.get("reanalyse", True)))
        if r.get("ok"):
            done += 1
        elif r.get("busy"):
            busy += 1
        else:
            err = r.get("error", "")
    if not done and err:
        return _err(err)
    return jsonify({"ok": True, "changed": done, "busy": busy, "skipped": skipped})


# ================================================================ sample files (V4.20)
from core import learn as LEARN  # noqa: E402

_LEARNER = {"thread": None}


def kick_learning() -> None:
    """Read the queued sample files, one after another, in a worker process."""
    t = _LEARNER.get("thread")
    if t is not None and t.is_alive():
        return

    def run():
        import subprocess
        for _ in range(1000):
            if not LEARN.queued():
                break
            try:
                subprocess.run([sys.executable, "-m", "core.learn", "queued"], cwd=BASE,
                               env=dict(os.environ), timeout=3600,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                time.sleep(2)

    t = threading.Thread(target=run, name="sample-learner", daemon=True)
    _LEARNER["thread"] = t
    t.start()


def _sample_clients() -> list[str]:
    names = list(all_profiles())
    for c in LEARN.clients():
        if c["client"] and c["client"] not in names:
            names.append(c["client"])
    return names


@app.route("/api/admin/samples")
def api_admin_samples():
    if LEARN.queued():
        kick_learning()
    rows = LEARN.samples()
    for r in rows:
        r.pop("stats", None) if not request.args.get("stats") else None
    return jsonify({"samples": rows, "clients": LEARN.clients(), "names": _sample_clients(),
                    "profiles": list(all_profiles()), "learning": bool(_LEARNER.get("thread") and _LEARNER["thread"].is_alive())})


@app.route("/api/admin/samples/upload", methods=["POST"])
def api_admin_samples_upload():
    files = [f for f in request.files.getlist("pdf") if f and f.filename.lower().endswith(".pdf")]
    if not files:
        return _err("choose the sample PDF(s)")
    client = (request.form.get("client") or "").strip()
    if client == "__new__":
        client = re.sub(r"\s+", " ", (request.form.get("new_client") or "")).strip()[:80]
        if not client:
            return _err("give the new client a name")
    made_profile = ""
    if client and request.form.get("make_profile") == "on" and client not in all_profiles():
        base = request.form.get("base_profile") or "Default"
        o = Options.from_dict(all_profiles().get(base, {}))
        o.client = client
        with config_edit() as cfg:
            cfg.setdefault("profiles", {})[client] = o.to_dict()
        made_profile = client
    ids = []
    import tempfile
    for f in files:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            f.save(tmp.name)
        try:
            ids.append(LEARN.add_sample(client, os.path.basename(f.filename.replace("\\", "/")), tmp.name,
                                        current_user_name()))
        finally:
            os.remove(tmp.name)
    log_action("sample files added", "", "", f"{len(ids)} for {client or 'every client (common)'}"
               + (f"; profile {made_profile} made" if made_profile else ""))
    kick_learning()
    return jsonify({"ok": True, "ids": ids, "client": client, "profile_made": made_profile})


@app.route("/api/admin/samples/from_jobs", methods=["POST"])
def api_admin_samples_from_jobs():
    """Finished files as samples: their tags, as they are now, teach the tool."""
    b = request.get_json(force=True) or {}
    ids = _ids(b)
    made, skipped = [], 0
    for i in ids:
        meta = read_meta(i)
        d = job_dir(i)
        mj, wp = os.path.join(d, "model.json"), os.path.join(d, "working.pdf")
        if not (os.path.exists(mj) and os.path.exists(wp)):
            skipped += 1
            continue
        client = b.get("client")
        if client is None:
            client = (Options.from_dict(meta.get("options")).client or meta.get("profile") or "")
        made.append(LEARN.add_sample(str(client), meta.get("filename", i), wp, current_user_name(),
                                     kind="file", job=i, model_src=mj))
        log_action("added as a sample", i, meta.get("filename", ""), str(client) or "common")
    kick_learning()
    return jsonify({"ok": True, "added": len(made), "skipped": skipped})


@app.route("/api/admin/samples/<sid>", methods=["POST", "DELETE"])
def api_admin_sample(sid):
    sid = re.sub(r"[^0-9a-f]", "", sid.lower())
    if request.method == "DELETE":
        ok = LEARN.delete_sample(sid)
        log_action("sample file deleted", "", "", sid)
        return jsonify({"ok": ok})
    b = request.get_json(force=True) or {}
    if b.get("relearn"):
        LEARN.set_sample(sid, status="queued")
        kick_learning()
        return jsonify({"ok": True})
    kw = {}
    if "enabled" in b:
        kw["enabled"] = 1 if b["enabled"] else 0
    if "client" in b:
        kw["client"] = str(b["client"] or "").strip()[:80]
    return jsonify({"ok": LEARN.set_sample(sid, **kw)})


@app.route("/api/admin/samples/knowledge")
def api_admin_samples_knowledge():
    return jsonify(LEARN.knowledge(request.args.get("client", "")))


@app.route("/admin/samples/<sid>.pdf")
def admin_sample_pdf(sid):
    sid = re.sub(r"[^0-9a-f]", "", sid.lower())
    p = os.path.join(LEARN.samples_dir(), "files", sid + ".pdf")
    if not os.path.exists(p):
        abort(404)
    return send_file(p, mimetype="application/pdf")


# ---------------------------------------------- approved files teach the tool (V4.21)
def _model_summary(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return {}
    from collections import Counter
    nodes = d.get("nodes") or {}
    roles = Counter(("Artifact" if v.get("artifact") else v.get("role")) for v in nodes.values())
    return {"nodes": len(nodes), "roles": dict(roles.most_common(18)),
            "hand_edits": sum(1 for v in nodes.values() if v.get("source") == "user"),
            "ai": sum(1 for v in nodes.values() if v.get("source") == "ai"),
            "low_confidence": sum(1 for v in nodes.values() if float(v.get("confidence") or 1) < 0.6)}


def _learn_from_approved(jid: str) -> str:
    """An approved file becomes a sample of its client: the file, its tag model,
    what the tool proposed and what people changed, its details (V4.21)."""
    lc = learning_cfg()
    if not (lc["enabled"] and lc["auto_approved"]):
        return ""
    meta = read_meta(jid)
    d = job_dir(jid)
    mj, wp, aj = (os.path.join(d, f) for f in ("model.json", "working.pdf", "auto_model.json"))
    zp = os.path.join(d, "zone_pairs.npz")             # V4.28: the pairs of lines the tool decided on
    if not (os.path.exists(mj) and os.path.exists(wp)):
        return ""
    o = Options.from_dict(meta.get("options"))
    client = o.client or meta.get("profile") or ""
    sa, sb = meta.get("score_after") or {}, meta.get("score_before") or {}
    details = {"filename": meta.get("filename"), "profile": meta.get("profile") or "Default", "client": client,
               "pages": meta.get("pages"), "title": meta.get("title"), "lang": meta.get("lang"),
               "mode": meta.get("mode"), "uploaded": meta.get("created"), "owner": meta.get("owner"),
               "worked_by": meta.get("assigned_to"), "approved_by": current_user_name(),
               "approved_at": datetime.now().isoformat(timespec="seconds"),
               "errors_before": sb.get("errors"), "errors_after": sa.get("errors"), "built": bool(meta.get("built")),
               "samples_changed": (meta.get("samples") or {}).get("changed"),
               "notes": [n.get("text") for n in (meta.get("notes") or [])[-5:]],
               "model": _model_summary(mj), "options": {k: v for k, v in (meta.get("options") or {}).items()
                                                        if k in ("house_style", "existing_tags", "section_wrap", "table_strategy")}}
    try:
        sid = LEARN.upsert_job_sample(jid, client, meta.get("filename", jid), wp, mj,
                                      aj if os.path.exists(aj) else "", current_user_name(), details,
                                      pairs_src=zp if os.path.exists(zp) else "")
    except Exception:
        return ""
    update_meta(jid, sample_id=sid)
    try:                                       # V4.26: the alt texts and readings, confirmed
        from core import memory as MEMORY
        from core.pipeline import load_model as _lm
        auto = None
        if os.path.exists(aj):
            with open(aj, encoding="utf-8") as fh:
                auto = DocModel.from_json(fh.read())
        with MUPDF_LOCK:
            MEMORY.learn(d, client, _lm(d), auto, verified=True)
    except Exception:
        pass
    log_action("learned from", jid, meta.get("filename", ""), f"sample of {client or 'every client'}")
    kick_learning()
    return sid


def _work_changed(jid: str, work: str) -> str:
    """Approved (Delivery) → the file teaches the tool; sent back to
    production or to a check again → it stops until it is approved again."""
    EDITLOCK.release(secure_filename(jid), force=True)          # V4.29: the file moved on, the person who had it is done
    try:
        if work == "approved":
            return _learn_from_approved(jid)
        if work in ("returned", "in_progress", "assigned", "submitted", "", "rework", "qa", "qa_progress",
                    "final_qa", "fqa_progress"):
            LEARN.set_job_sample_enabled(jid, False)
    except Exception:
        pass
    return ""


@app.route("/api/admin/learning", methods=["GET", "POST"])
def api_admin_learning():
    if request.method == "POST":
        b = request.get_json(force=True) or {}
        with config_edit() as cfg:
            cur = {**LEARNING_DEFAULTS, **(cfg.get("learning") or {})}
            for k in LEARNING_DEFAULTS:
                if k in b:
                    cur[k] = bool(b[k])
            cfg["learning"] = cur
        log_action("learning settings", "", "", ", ".join(f"{k}={'on' if v else 'off'}" for k, v in cur.items()))
    return jsonify({"ok": True, "learning": learning_cfg(), "db": LEARN.db_stats(),
                    "ai": bool(ai_cfg().enabled)})


@app.route("/api/admin/samples/<sid>/detail")
def api_admin_sample_detail(sid):
    sid = re.sub(r"[^0-9a-f]", "", sid.lower())
    d = LEARN.sample_detail(sid)
    if d is None:
        return _err("no such sample", 404)
    return jsonify(d)


@app.route("/admin/samples/<sid>/model.json")
def admin_sample_model(sid):
    sid = re.sub(r"[^0-9a-f]", "", sid.lower())
    raw = LEARN.sample_model(sid, request.args.get("which", "model"))
    if raw is None:
        abort(404)
    return app.response_class(raw, mimetype="application/json",
                              headers={"Content-Disposition": f"attachment; filename=sample-{sid}-{request.args.get('which', 'model')}.json"})


@app.route("/admin/samples/backup.db")
def admin_samples_backup():
    """The whole sample database (a consistent copy, safe while the tool runs)."""
    import sqlite3
    import tempfile
    if not LEARN.has_data():
        abort(404)
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    src = sqlite3.connect(LEARN.db_path())
    dst = sqlite3.connect(tmp.name)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    log_action("sample database downloaded")
    return send_file(tmp.name, as_attachment=True,
                     download_name=f"samples-{datetime.now().strftime('%Y%m%d-%H%M')}.db")


def _job_client(jid: str) -> str:
    meta = read_meta(jid)
    return Options.from_dict(meta.get("options")).client or meta.get("profile") or "Default"


def _job_know(jid: str):
    """The samples this file learns from: its client's — or, V4.25, every
    client's with the most similar files first (Smart AI)."""
    meta = read_meta(jid)
    o = Options.from_dict(meta.get("options"))
    client = o.client or meta.get("profile") or "Default"
    if o.samples_scope != "all":
        return LEARN.Knowledge(client)
    sim = ((meta.get("brain") or {}).get("similar") or []) or ((meta.get("samples") or {}).get("similar") or [])
    weights = {x["id"]: 0.4 + 1.6 * max(0.0, min(1.0, (float(x.get("sim") or 0) - 0.1) / 0.6)) for x in sim if x.get("id")}
    return LEARN.Knowledge(client, scope="all", weights=weights)


@app.route("/api/job/<jid>/samples/compare")
def api_job_samples_compare(jid):
    """This file against its client's samples (the editor's Client tab)."""
    if not LEARN.has_data():
        return jsonify({"files": 0, "findings": [], "nodes": [], "client": _job_client(jid)})
    client = _job_client(jid)
    try:
        with MUPDF_LOCK:
            r = LEARN.compare(get_model(jid), os.path.join(job_dir(jid), "working.pdf"), client,
                              know=_job_know(jid))
    except Exception as exc:
        return jsonify({"files": 0, "findings": [], "nodes": [], "error": str(exc), "client": client})
    return jsonify(r)


@app.route("/api/job/<jid>/samples/apply", methods=["POST"])
def api_job_samples_apply(jid):
    """Take what the samples say: [{id, role}] and/or the caption places."""
    b = request.get_json(force=True) or {}
    done = 0
    try:
        with mutate(jid) as model:
            for it in b.get("items") or []:
                nid, role = str(it.get("id") or ""), str(it.get("role") or "")
                if nid in model.nodes and role in (LEARN.SWAP | {"Artifact"}):
                    edit.set_fields(model, nid, {"role": role})
                    n = model.nodes.get(nid)
                    if n is not None:
                        n.note = (n.note + "; " if n.note else "") + "samples: taken in the editor"
                    done += 1
            if b.get("captions"):
                done += LEARN.place_captions(model, _job_know(jid))
    except edit.EditError as exc:
        return _err(str(exc))
    return jsonify({"ok": True, "changed": done})


@app.route("/api/job/<jid>/samples/suggest")
def api_job_samples_suggest(jid):
    """What the samples say about one tag (the editor's popover) — and, V4.25,
    what the brain's sources say (rules, samples, its model, the style map)."""
    if not LEARN.has_data():
        return jsonify({"files": 0, "votes": []})
    meta = read_meta(jid)
    o = Options.from_dict(meta.get("options"))
    client = o.client or meta.get("profile") or "Default"
    nid = request.args.get("id", "")
    try:
        with MUPDF_LOCK:
            m = get_model(jid)
            work = os.path.join(job_dir(jid), "working.pdf")
            r = LEARN.suggest(m, work, nid, client, know=_job_know(jid))
            try:
                r["brain"] = BRAIN.explain(m, work, nid, client, o.samples_scope)
            except Exception as exc:
                r["brain"] = {"error": str(exc)[:200]}
    except Exception as exc:
        return jsonify({"files": 0, "votes": [], "error": str(exc)})
    r["client"] = client
    return jsonify(r)


# ================================================================ the self-learning brain (V4.25)
from core import brain as BRAIN  # noqa: E402
from core import codegen as CODEGEN  # noqa: E402
from core import inventory as INVENTORY  # noqa: E402
from core import kb as KB  # noqa: E402

_LIVE: dict[str, threading.Timer] = {}
_LIVE_FIRST: dict[str, float] = {}
_LIVE_STATE: dict[str, dict] = {}
_LIVE_LOCK = threading.Lock()
# at most this many learner processes at the same time (each one is a Python with MuPDF and numpy)
_LIVE_SLOTS = threading.BoundedSemaphore(max(1, int(os.environ.get("PDFUA_LEARNERS", "2") or 2)))
LIVE_STATS = {"runs": 0, "failed": 0, "last": "", "last_seconds": 0.0, "waiting": 0, "running": 0}
LIVE_DELAY = float(os.environ.get("PDFUA_LIVE_DELAY", "4") or 4)       # seconds after the last edit
LIVE_MAX_WAIT = 45.0      # a person who keeps editing is learned from at least this often


def learn_live(jid: str, delay: float | None = None) -> None:
    """V4.26: what a person changed is remembered a few seconds after the last
    edit — not only at the build — so the next file of anybody benefits.

    V4.28: not in this process any more. Learning reads the PDF; with many people editing that
    would make every pause in typing take the editor's MuPDF lock from everybody else. The job
    is handed to a learner process (core/livelearn.py): at most PDFUA_LEARNERS (2) at once, one
    for each file at a time (what is changed meanwhile is learned by a second run right after),
    and a person who never pauses is learned from every 45 s all the same."""
    lc = learning_cfg()
    if not (lc["enabled"] and (lc["feedback"] or lc.get("zones", True))):
        return
    try:
        by = current_user_name()
        role = (current_user() or {}).get("role", "")
    except Exception:
        by, role = "", ""
    delay = LIVE_DELAY if delay is None else delay
    with _LIVE_LOCK:
        old = _LIVE.pop(jid, None)
        if old is not None:
            old.cancel()
        first = _LIVE_FIRST.setdefault(jid, time.time())
        wait = 0.2 if time.time() - first > LIVE_MAX_WAIT else delay
        t = threading.Timer(wait, _live_fire, args=(jid, by, role in ("admin", "qa", "final_qa")))
        t.daemon = True
        _LIVE[jid] = t
        t.start()


def _live_fire(jid: str, by: str, reviewer: bool) -> None:
    with _LIVE_LOCK:
        _LIVE.pop(jid, None)
        _LIVE_FIRST.pop(jid, None)
        st = _LIVE_STATE.setdefault(jid, {"running": False, "again": False})
        if st["running"]:
            st["again"] = True                  # being learned from right now: once more when it is done
            return
        st["running"] = True
    try:
        while True:
            _live_run(jid, by, reviewer)
            with _LIVE_LOCK:
                if st["again"]:
                    st["again"] = False
                    continue
                st["running"] = False
                break
    except Exception as exc:
        app.logger.warning("live learning: %s", exc)
        with _LIVE_LOCK:
            st["running"] = False


def _live_run(jid: str, by: str, reviewer: bool) -> dict:
    import subprocess
    try:
        d = job_dir(jid, must_exist=False)
        if not os.path.exists(os.path.join(d, "auto_model.json")):
            return {}
        lc = learning_cfg()
        client = _job_client(jid)
    except Exception:
        return {}
    cmd = [sys.executable, "-m", "core.livelearn", d, client, by, "1" if reviewer else "0",
           "1" if lc.get("zones", True) else "0", "1" if lc.get("feedback", True) else "0"]
    with _LIVE_LOCK:
        LIVE_STATS["waiting"] += 1
    with _LIVE_SLOTS:
        with _LIVE_LOCK:
            LIVE_STATS["waiting"] -= 1
            LIVE_STATS["running"] += 1
        t0 = time.time()
        res: dict = {}
        try:
            # V4.30: the learner works in the background — below the web server, so a person's next click is
            # never slower because the tool is learning from the last one
            kw: dict = {}
            if os.name == "nt":
                kw["creationflags"] = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
            else:
                kw["preexec_fn"] = lambda: os.nice(12)
            p = subprocess.run(cmd, cwd=BASE, env=dict(os.environ), timeout=600, capture_output=True, text=True, **kw)
            try:
                res = json.loads((p.stdout or "{}").strip().splitlines()[-1])
            except (ValueError, IndexError):
                res = {"error": (p.stderr or p.stdout or "")[-300:]}
        except Exception as exc:
            res = {"error": str(exc)[:300]}
        with _LIVE_LOCK:
            LIVE_STATS["running"] -= 1
            LIVE_STATS["runs"] += 1
            LIVE_STATS["failed"] += 1 if res.get("error") else 0
            LIVE_STATS["last"] = datetime.now().isoformat(timespec="seconds")
            LIVE_STATS["last_seconds"] = round(time.time() - t0, 2)
    if (res.get("feedback") or {}).get("feedback"):
        kick_brain_training()
    return res


_BRAIN_JOB = {"thread": None, "last": 0.0, "what": "", "result": None}


def _brain_bg(args: list[str], what: str) -> bool:
    """Train the brain / let the AI study the samples, in a worker process."""
    t = _BRAIN_JOB.get("thread")
    if t is not None and t.is_alive():
        return False

    def run():
        import subprocess
        try:
            p = subprocess.run([sys.executable, "-m", "core.brain", *args], cwd=BASE, env=dict(os.environ),
                               timeout=7200, capture_output=True, text=True)
            try:
                _BRAIN_JOB["result"] = json.loads(p.stdout or "{}")
            except ValueError:
                _BRAIN_JOB["result"] = {"error": (p.stderr or p.stdout or "")[-400:]}
        except Exception as exc:
            _BRAIN_JOB["result"] = {"error": str(exc)}
        _BRAIN_JOB["last"] = time.time()

    _BRAIN_JOB.update(what=what, result=None, started=datetime.now().isoformat(timespec="seconds"))
    t = threading.Thread(target=run, name="brain-" + what, daemon=True)
    _BRAIN_JOB["thread"] = t
    t.start()
    return True


_KICK: dict = {"timer": None}


def kick_brain_training() -> None:
    """After a fix or a build taught the brain something: train again — at most every 10 min.

    V4.27: a request that comes too early is not forgotten. Before, the fixes made in the last
    minutes of an editing session were learned only if somebody happened to fix or build something
    after the pause; now one training is made when the pause is over (it does nothing when there is
    nothing new — the brain compares the data with what it last trained on)."""
    t = _BRAIN_JOB.get("thread")
    wait = 600 - (time.time() - float(_BRAIN_JOB.get("last") or 0))
    if wait > 0 or (t is not None and t.is_alive()):
        if _KICK["timer"] is None:
            def later():
                _KICK["timer"] = None
                kick_brain_training()
            tm = threading.Timer(max(60.0, wait) + 2.0, later)
            tm.daemon = True
            _KICK["timer"] = tm
            tm.start()
        return
    _brain_bg(["train"], "train")


def _brain_upgrade() -> None:
    """Samples learned before V4.25 are read once more (their fonts, document,
    memory), then the AI studies them and the brain trains — by itself."""
    try:
        ids = BRAIN.needs_relearn()
        for sid in ids:
            LEARN.set_sample(sid, status="queued")
        if ids:
            kick_learning()
    except Exception:
        pass


@app.route("/api/admin/brain")
def api_admin_brain():
    try:
        st = BRAIN.status()
    except Exception as exc:
        st = {"error": str(exc)}
    t = _BRAIN_JOB.get("thread")
    lt = _LEARNER.get("thread")
    st["running"] = bool(t is not None and t.is_alive()) or bool(lt is not None and lt.is_alive()) \
        or bool(LEARN.queued())
    for h in st.get("history") or []:
        try:
            h["file"] = read_meta(h["job"]).get("filename", "") if h.get("job") else ""
        except Exception:
            h["file"] = ""
    st["job"] = {"what": _BRAIN_JOB.get("what"), "result": _BRAIN_JOB.get("result"),
                 "started": _BRAIN_JOB.get("started")}
    st["learning"] = learning_cfg()
    st["ai"] = bool(ai_cfg().enabled)
    st["default_profile"] = load_config().get("default_profile")
    try:
        st["code"] = CODEGEN.listing()
    except Exception as exc:
        st["code"] = []
        st["code_error"] = str(exc)
    try:
        from core import memory as MEMORY
        st["memories"] = MEMORY.stats()
    except Exception:
        st["memories"] = {}
    st["privacy"] = {**NETGUARD.status(), "recent": NETGUARD.recent(20)}
    try:                                           # V4.28: the zones people taught, and the learner processes
        st["zones"] = ZONES.status()
        st["live"] = dict(LIVE_STATS)
    except Exception as exc:
        st["zones"] = {"models": [], "error": str(exc)}
    st["load"] = load_stats()
    return jsonify(st)


@app.route("/api/admin/zones/train", methods=["POST"])
def api_admin_zones_train():
    r = ZONES.train_all(force=True)
    log_action("zone models trained", "", "", "by hand")
    return jsonify({"ok": True, **r})


@app.route("/api/admin/brain/train", methods=["POST"])
def api_admin_brain_train():
    ok = _brain_bg(["train", "--force"], "train")
    log_action("brain trained", "", "", "by hand")
    return jsonify({"ok": True, "started": ok})


@app.route("/api/admin/brain/study", methods=["POST"])
def api_admin_brain_study():
    if not ai_cfg().enabled:
        return _err("the AI is switched off in Settings")
    ok = _brain_bg(["study", "--all"], "study")
    log_action("brain: AI study of the samples", "", "", "")
    return jsonify({"ok": True, "started": ok})


@app.route("/api/job/<jid>/brain")
def api_job_brain(jid):
    meta = read_meta(jid)
    info = {}
    try:
        info = (get_model(jid).info or {}).get("brain") or {}
    except Exception:
        info = {}
    o = Options.from_dict(meta.get("options"))
    return jsonify({"brain": info or meta.get("brain") or {}, "task": meta.get("brain_task") or {},
                    "on": bool(o.ai_brain), "scope": o.samples_scope, "profile": meta.get("profile")})


@app.route("/api/job/<jid>/brain/think", methods=["GET", "POST"])
def api_job_brain_think(jid):
    """The brain decides the text tags of this file again (queued; Undo puts them back)."""
    if request.method == "GET":
        return jsonify(read_meta(jid).get("brain_task") or {})
    _guard_busy(jid)
    with job_lock(jid):
        _push(jid, "undo", os.path.join(job_dir(jid), "model.json"))
        _clear(jid, "redo")
    STOP.discard(jid)
    update_meta(jid, brain_task={"running": True, "queued": True, "progress": 0,
                                 "message": "waiting for a free worker", "result": None})
    TASKS.put((jid, "brain"))
    log_action("brain: think again", jid, read_meta(jid).get("filename", ""), "")
    return jsonify({"ok": True, "queued": True})


# ---------------------------------------------------------------- learned code (V4.26)
@app.route("/api/admin/code")
def api_admin_code():
    return jsonify({"rules": CODEGEN.listing()})


@app.route("/api/admin/code/write", methods=["POST"])
def api_admin_code_write():
    ok = _brain_bg(["code"], "code")
    log_action("learning engine: write rules", "", "", "")
    return jsonify({"ok": True, "started": ok})


@app.route("/api/admin/code/<rid>", methods=["POST", "DELETE"])
def api_admin_code_rule(rid):
    if request.method == "DELETE":
        ok = CODEGEN.delete(rid)
        log_action("learned rule deleted", "", "", rid)
        return jsonify({"ok": ok})
    b = request.get_json(force=True) or {}
    ok = CODEGEN.set_status(rid, str(b.get("status") or ""))
    log_action("learned rule " + str(b.get("status")), "", "", rid)
    return jsonify({"ok": ok})


# ---------------------------------------------------------------- the privacy guard (V4.26)
@app.route("/api/admin/privacy", methods=["GET", "POST"])
def api_admin_privacy():
    if request.method == "POST":
        b = request.get_json(force=True) or {}
        with config_edit() as cfg:
            pv = dict(cfg.get("privacy") or {})
            if "guard" in b:
                pv["guard"] = bool(b["guard"])
            if "allow" in b:
                pv["allow"] = [str(h).strip().lower()[:200] for h in (b.get("allow") or []) if str(h).strip()][:30]
            cfg["privacy"] = pv
        NETGUARD.install(CONFIG_PATH, process="web app")
        log_action("privacy settings", "", "", f"guard={'on' if pv.get('guard', True) else 'off'} "
                                               f"allow={','.join(pv.get('allow') or []) or '-'}")
    from urllib.parse import urlparse
    cfg = load_config()
    host = urlparse((cfg.get("ai") or {}).get("base_url") or "").hostname or ""
    return jsonify({**NETGUARD.status(), "recent": NETGUARD.recent(50), "settings": cfg.get("privacy") or {},
                    "ai_host": host, "ai_host_local": NETGUARD._local_name(host) if host else True})


# ---------------------------------------------------------------- the offline rule book (V4.26)
@app.route("/api/kb")
def api_kb():
    q = request.args.get("q", "")[:200]
    client = request.args.get("client", "")
    jid = request.args.get("job", "")
    if jid:
        try:
            client = _job_client(jid)
        except Exception:
            pass
    return jsonify({"results": KB.search(q, k=int(request.args.get("k", 8) or 8), client=client),
                    "offline": True})


@app.route("/api/admin/kb", methods=["POST"])
def api_admin_kb_add():
    b = request.get_json(force=True) or {}
    if not (b.get("title") and b.get("text")):
        return _err("a title and a text are needed")
    rid = KB.add(str(b["title"]), str(b["text"]), str(b.get("roles") or ""), str(b.get("refs") or ""),
                 str(b.get("client") or ""), current_user_name())
    log_action("house rule added", "", "", str(b["title"])[:60])
    return jsonify({"ok": True, "id": rid})


@app.route("/api/admin/kb/<rid>", methods=["DELETE"])
def api_admin_kb_del(rid):
    return jsonify({"ok": KB.remove(rid)})


# ---------------------------------------------------------------- the full inventory of a file (V4.26)
@app.route("/api/job/<jid>/inventory", methods=["GET", "POST"])
def api_job_inventory(jid):
    d = job_dir(jid)
    if request.method == "POST" or not os.path.exists(os.path.join(d, "inventory.json")):
        _guard_busy(jid)
        try:
            with MUPDF_LOCK:
                model = get_model(jid)
                if not os.path.exists(os.path.join(d, "inventory.json")):
                    inv = INVENTORY.build(os.path.join(d, "working.pdf"))
                    INVENTORY.save(d, inv, INVENTORY.coverage(inv, model, os.path.join(d, "working.pdf")))
                else:
                    INVENTORY.refresh_coverage(d, model)
        except Exception as exc:
            return _err(f"inventory: {exc}")
    inv = INVENTORY.load(d) or {}
    cov = inv.get("coverage") or {}
    try:
        update_meta(jid, inventory=INVENTORY.summary(inv, cov))
    except Exception:
        pass
    return jsonify({"inventory": {k: v for k, v in inv.items() if k != "pages"},
                    "pages": [{k: v for k, v in p.items() if k not in ("images",)} for p in inv.get("pages") or []],
                    "summary": INVENTORY.summary(inv, cov)})


@app.route("/api/job/<jid>/coverage/tag", methods=["POST"])
def api_job_coverage_tag(jid):
    """Tag what no tag holds: each line (or picture) in a zone of its own."""
    b = request.get_json(force=True) or {}
    role = b.get("role") if b.get("role") in ("P", "Figure", "Artifact", "Auto") else "P"
    items = [x for x in (b.get("items") or []) if isinstance(x, dict) and x.get("bbox")][:400]
    split: bool | str = False
    if role == "P" and len(items) > 1:
        # lines printed one under the other are one zone: the analysis' own
        # paragraph grouping then makes one P per paragraph, not one per line
        zones = []
        for x in sorted(items, key=lambda x: (int(x.get("page", 0)), x["bbox"][1], x["bbox"][0])):
            bb = [float(v) for v in x["bbox"]]
            last = zones[-1] if zones else None
            if last is not None and last["page"] == int(x.get("page", 0)):
                lb = last["bbox"]
                h = max(1.0, min(bb[3] - bb[1], lb[3] - lb[1]))
                if bb[1] - lb[3] <= 0.9 * h and min(bb[2], lb[2]) > max(bb[0], lb[0]):
                    last["bbox"] = [min(lb[0], bb[0]), min(lb[1], bb[1]), max(lb[2], bb[2]), max(lb[3], bb[3])]
                    continue
            zones.append({"page": int(x.get("page", 0)), "bbox": bb})
        items, split = zones, "paragraphs"
    made, failed = [], 0
    try:
        with mutate(jid) as model:
            work = os.path.join(job_dir(jid), "working.pdf")
            for x in items:
                try:
                    new = edit.create_from_rect(model, work, int(x.get("page", 0)), x["bbox"], role, False,
                                                job_opts(jid), split=split)
                    made.append(new)
                except edit.EditError:
                    failed += 1
    except edit.EditError as exc:
        return _err(str(exc))
    if made:
        learn_live(jid)
    return jsonify({"ok": True, "made": len(made), "failed": failed, "ids": made[:50]})


@app.route("/api/job/<jid>/brain/similar")
def api_job_brain_similar(jid):
    """V4.26: the tags of this file that look like the one a person just changed."""
    try:
        with MUPDF_LOCK:
            m = get_model(jid)
            items = BRAIN.similar_tags(m, os.path.join(job_dir(jid), "working.pdf"), request.args.get("id", ""),
                                       request.args.get("from", ""))
    except Exception as exc:
        return jsonify({"items": [], "error": str(exc)})
    return jsonify({"items": items})


@app.route("/api/job/<jid>/brain/similar/apply", methods=["POST"])
def api_job_brain_similar_apply(jid):
    """Give the tags that look like the one a person changed the same tag (one Undo)."""
    b = request.get_json(force=True) or {}
    role = str(b.get("role") or "")
    if role not in (set(EDITOR_ROLES) | {"Artifact"}):
        return _err("unknown tag type")
    done = 0
    try:
        with mutate(jid) as model:
            for nid in (b.get("ids") or [])[:500]:
                if nid in model.nodes and model.nodes[nid].source != "user":
                    edit.set_fields(model, nid, {"role": role})
                    if nid in model.nodes:
                        n = model.nodes[nid]
                        n.note = (n.note + "; " if n.note else "") + "brain: like the tag a person changed"
                    done += 1
    except edit.EditError as exc:
        return _err(str(exc))
    if done:
        learn_live(jid)
    return jsonify({"ok": True, "changed": done})


# ---------------------------------------------------------------- zones learned from people (V4.28)
from core import zonelearn as ZONES  # noqa: E402


_AUTO_KEPT: "collections.OrderedDict[str, tuple[int, DocModel]]" = collections.OrderedDict()


def _auto_of(jid: str):
    """The tool's own proposal for the file (auto_model.json) — read once, kept while the file is the same (it was
    parsed again for every question about zones: 160 ms for a 300-page book)."""
    ap = os.path.join(job_dir(jid), "auto_model.json")
    try:
        mt = os.stat(ap).st_mtime_ns
    except OSError:
        return None
    key = secure_filename(jid)
    with _mc_lock:
        hit = _AUTO_KEPT.get(key)
        if hit is not None and hit[0] == mt:
            _AUTO_KEPT.move_to_end(key)
            return hit[1]
    with open(ap, encoding="utf-8") as fh:
        auto = DocModel.from_json(fh.read())
    with _mc_lock:
        _AUTO_KEPT[key] = (mt, auto)
        while len(_AUTO_KEPT) > 6:
            _AUTO_KEPT.popitem(last=False)
    return auto


@app.route("/api/job/<jid>/zones/similar")
def api_zones_similar(jid):
    """The other places of this file that look like a zone a person has just cut or joined."""
    key = secure_filename(jid)
    try:
        d = job_dir(jid)
        stamp = etag_of(model_rev(jid), *(os.path.getmtime(os.path.join(d, f)) if os.path.exists(os.path.join(d, f)) else 0
                                         for f in ("zone_pairs.npz", "auto_model.json")))
    except Exception:
        stamp = None
    if stamp is not None:
        # V4.30: the same question about the same version of the file has the same answer
        hit = ZONE_BYTES.get(key, stamp)
        if hit is not None:
            return _bytes_response(hit[1], hit[2], stamp)
    resp = _zones_similar_answer(jid)
    if stamp is not None and resp.status_code == 200 and not resp.headers.get("Content-Encoding"):
        ZONE_BYTES.put(key, stamp, resp.get_data())
    return resp


ZONE_BYTES = ByteCache(24)


def _zones_similar_answer(jid):
    try:
        r = ZONES.similar(job_dir(jid), get_model(jid), _auto_of(jid))
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "cut": [], "join": []})
    pairs = ZONES.load_pairs(job_dir(jid))
    out = {"ok": True, "corrections": r["corrections"]}
    for kind in ("cut", "join"):
        out[kind] = {"n": len(r[kind]), "pairs": r[kind],
                     "pages": sorted({int(pairs["page"][i]) for i in r[kind]}) if pairs is not None else []}
        out[kind]["nodes"] = []
        if pairs is not None and r[kind]:
            m = get_model(jid)
            by_page = ZONES._text_nodes(m)
            for i in r[kind][:300]:
                box = pairs["box"][i]
                o = ZONES._owner(by_page.get(int(pairs["page"][i])) or [], (box[4] + box[6]) / 2, (box[5] + box[7]) / 2)
                if o is not None:
                    # the start of the line below the cut: what a person looks for on the page
                    out[kind]["nodes"].append({"id": o[0].id, "page": int(pairs["page"][i]),
                                               "text": str(pairs["txt"][i]) or " ".join((o[0].text or "").split())[:40]})
    return jsonify(out)


@app.route("/api/job/<jid>/zones/similar/apply", methods=["POST"])
def api_zones_similar_apply(jid):
    """Cut (or join) the zones at the places `zones/similar` found — one Undo puts them back."""
    b = request.get_json(force=True) or {}
    action = "join" if b.get("action") == "join" else "cut"
    try:
        with mutate(jid) as model:
            r = ZONES.apply(model, job_dir(jid), _auto_of(jid), os.path.join(job_dir(jid), "working.pdf"),
                            action, [int(i) for i in (b.get("pairs") or [])][:600])
    except edit.EditError as exc:
        return _err(str(exc))
    return jsonify({"ok": True, **r})


if __name__ != "__mp_main__":
    start_workers()
    threading.Thread(target=_brain_upgrade, name="brain-upgrade", daemon=True).start()

def serve() -> None:
    """V4.28: with waitress installed (pip install waitress) the tool serves many people at once with a fixed
    number of threads — PDFUA_THREADS, 24 by default — instead of one new thread for every request."""
    host, port = os.environ.get("HOST", "0.0.0.0"), int(os.environ.get("PORT", 8000))
    threads = max(4, int(os.environ.get("PDFUA_THREADS", 0) or 24))
    if os.environ.get("PDFUA_SERVER", "").lower() != "flask":
        try:
            from waitress import serve as waitress_serve
        except ImportError:
            waitress_serve = None
        if waitress_serve is not None:
            print(f"Dezquare PDF Accessibility Studio {VERSION} · http://{host}:{port} · waitress, {threads} threads",
                  flush=True)
            waitress_serve(app, host=host, port=port, threads=threads, connection_limit=max(200, threads * 10),
                           channel_timeout=300, ident="pdfua")
            return
        print("pip install waitress   — a production server for many people at the same time", flush=True)
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    serve()
