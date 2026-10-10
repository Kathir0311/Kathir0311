"""The privacy guard: nothing leaves the server (V4.26).

Every process of the tool (the web app, the analysis and build workers, the
learner, the brain, the command line) installs this guard at start. From
then on a connection may only go to

* this machine (loopback) and the private network (10.x, 172.16–31.x,
  192.168.x, fc00::/7, link-local) — where the Ollama server runs;
* a host the admin has named in Settings → Privacy (the allow list) — for
  an Ollama server on another address, say.

Anything else — a public address, a public DNS name — is refused before a
byte is sent, and written to the guard's log (time, address, which part of
the tool tried). The browser side has the same rule through a
Content-Security-Policy: the pages may only talk to this server, and the
Reader uses only voices that run on the computer itself (some browser
voices send the text to an online service).

Switch it off only for a test: PDFUA_NETGUARD=0, or Settings → Privacy.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import threading
import time
import traceback

_STATE = {"installed": False, "on": True, "allow_hosts": set(), "allow_ips": set(), "log": None, "blocked": 0,
          "process": ""}
_LOCK = threading.Lock()
_orig_connect = socket.socket.connect
_orig_connect_ex = socket.socket.connect_ex
_orig_getaddrinfo = socket.getaddrinfo

LOCAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", ""}
LOCAL_SUFFIXES = (".local", ".lan", ".internal", ".home.arpa", ".localdomain", ".intranet", ".corp", ".home")


class Blocked(PermissionError):
    pass


def _private_ip(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return a.is_loopback or a.is_private or a.is_link_local or a.is_unspecified


def _local_name(host: str) -> bool:
    h = (host or "").lower().rstrip(".")
    if h in LOCAL_NAMES or "." not in h:
        return True                               # a bare machine name on the local network
    try:
        return _private_ip(h)
    except Exception:
        pass
    return h.endswith(LOCAL_SUFFIXES)


def _log(kind: str, target: str) -> None:
    _STATE["blocked"] += 1
    path = _STATE.get("log")
    if not path:
        return
    try:
        where = ""
        for fr in reversed(traceback.extract_stack()[:-3]):
            if "netguard" not in fr.filename and "socket" not in os.path.basename(fr.filename) \
                    and "urllib" not in fr.filename and "http" not in os.path.basename(fr.filename):
                where = f"{os.path.basename(fr.filename)}:{fr.lineno}"
                break
        rec = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "target": target,
               "process": _STATE.get("process", ""), "where": where}
        with _LOCK:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _allowed_host(host: str) -> bool:
    h = (host or "").lower().rstrip(".")
    return h in _STATE["allow_hosts"] or _local_name(h)


def _guarded_getaddrinfo(host, *a, **kw):
    if _STATE["on"] and host is not None:
        h = host.decode() if isinstance(host, bytes) else str(host)
        try:
            ipaddress.ip_address(h.split("%")[0])
            return _orig_getaddrinfo(host, *a, **kw)     # an address needs no DNS: the connect decides
        except ValueError:
            pass
        if not _allowed_host(h):
            _log("dns", h)
            raise socket.gaierror(socket.EAI_NONAME, f"blocked by the privacy guard: {h} is not on this "
                                                     "server's network (Settings → Privacy)")
        res = _orig_getaddrinfo(host, *a, **kw)
        if h.lower() in _STATE["allow_hosts"]:
            for r in res:
                try:
                    _STATE["allow_ips"].add(r[4][0])
                except Exception:
                    pass
        return res
    return _orig_getaddrinfo(host, *a, **kw)


def _check(sock, address) -> None:
    if not _STATE["on"]:
        return
    fam = getattr(sock, "family", None)
    if fam not in (socket.AF_INET, socket.AF_INET6):
        return                                    # unix sockets and the like stay on the machine
    ip = str(address[0]) if isinstance(address, tuple) and address else str(address)
    if _private_ip(ip) or ip in _STATE["allow_ips"]:
        return
    port = address[1] if isinstance(address, tuple) and len(address) > 1 else ""
    _log("connect", f"{ip}:{port}")
    raise Blocked(f"blocked by the privacy guard: {ip}:{port} is outside this server's network "
                  "(no document data leaves the server — Settings → Privacy)")


def _connect(self, address):
    _check(self, address)
    return _orig_connect(self, address)


def _connect_ex(self, address):
    _check(self, address)
    return _orig_connect_ex(self, address)


def install(config_path: str | None = None, process: str = "", log_dir: str | None = None) -> dict:
    """Switch the guard on for this process (safe to call more than once)."""
    on = os.environ.get("PDFUA_NETGUARD", "1") != "0"
    allow: set = set()
    cfgp = config_path or os.environ.get("PDFUA_CONFIG") or \
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
    try:
        with open(cfgp, encoding="utf-8") as fh:
            cfg = json.load(fh)
        pv = cfg.get("privacy") or {}
        if pv.get("guard") is False:
            on = False
        allow |= {str(h).lower().strip() for h in pv.get("allow") or [] if str(h).strip()}
        # the Ollama server the admin set is reachable on purpose
        from urllib.parse import urlparse
        ou = urlparse((cfg.get("ai") or {}).get("base_url") or "")
        if ou.hostname and pv.get("allow_ai_host", True):
            allow.add(ou.hostname.lower())
    except Exception:
        pass
    _STATE["on"] = on
    _STATE["allow_hosts"] = allow
    _STATE["process"] = process or _STATE.get("process", "")
    _STATE["log"] = os.path.join(log_dir or os.path.dirname(os.path.abspath(cfgp)), "privacy.log")
    if not _STATE["installed"]:
        socket.socket.connect = _connect
        socket.socket.connect_ex = _connect_ex
        socket.getaddrinfo = _guarded_getaddrinfo
        _STATE["installed"] = True
    return status()


def status() -> dict:
    return {"on": _STATE["on"], "installed": _STATE["installed"], "allow": sorted(_STATE["allow_hosts"]),
            "blocked_here": _STATE["blocked"], "log": _STATE.get("log")}


def recent(n: int = 50) -> list[dict]:
    p = _STATE.get("log")
    if not p or not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8") as fh:
            lines = fh.readlines()[-n:]
        return [json.loads(x) for x in reversed(lines) if x.strip()]
    except Exception:
        return []


CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; media-src 'self' data: blob:; "
       "frame-src 'self' blob:; worker-src 'self' blob:; object-src 'self' blob:; base-uri 'self'; "
       "form-action 'self'; frame-ancestors 'self'")
