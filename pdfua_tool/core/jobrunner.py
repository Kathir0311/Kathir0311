"""Run prepare/build in a separate Python process.

Why: PyMuPDF (MuPDF) is not thread-safe. Running a long job in a thread of the
web server while the editor renders page images in other threads can deadlock
the whole queue ("waiting" forever). A child process has its own MuPDF, can be
stopped at any time, and a crash there cannot take the web app down.

Protocol: the parent writes task.json into the job folder and starts
    python -m core.jobrunner <job_dir>
The child prints progress lines "@@P <fraction> <message>" on stdout and writes
task_result.json. Everything else it prints goes to job_dir/worker.log.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import traceback
import queue as _queue
from typing import Callable

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TAG = "@@P "


class JobStopped(RuntimeError):
    pass


class JobFailed(RuntimeError):
    def __init__(self, msg: str, trace: str = ""):
        super().__init__(msg)
        self.trace = trace


def run_task(job: str, task: dict, progress: Callable[[float, str], None],
             should_stop: Callable[[], bool], timeout_s: float | None = None,
             on_start: Callable[[subprocess.Popen], None] | None = None) -> dict:
    """Parent side. Blocks until the child finishes; returns its result dict."""
    tpath = os.path.join(job, "task.json")
    rpath = os.path.join(job, "task_result.json")
    with open(tpath, "w", encoding="utf-8") as fh:
        json.dump(task, fh, default=str)
    if os.path.exists(rpath):
        os.remove(rpath)
    log = open(os.path.join(job, "worker.log"), "a", encoding="utf-8", errors="replace")
    log.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} {task.get('kind')}\n")
    log.flush()
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen([sys.executable, "-m", "core.jobrunner", job], cwd=BASE,
                            stdout=subprocess.PIPE, stderr=log, stdin=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace", env=env,
                            creationflags=flags)
    if on_start:
        on_start(proc)
    lines: "_queue.Queue[str | None]" = _queue.Queue()

    def reader():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=reader, daemon=True).start()
    t0 = time.time()
    stopped = timed_out = False
    eof = False
    try:
        while True:
            try:
                line = lines.get(timeout=0.5)
            except _queue.Empty:
                line = ""
            if line is None:
                eof = True
            elif line.startswith(TAG):
                try:
                    frac, msg = line[len(TAG):].rstrip("\n").split(" ", 1)
                    progress(float(frac), msg)
                except Exception:
                    pass
            elif line:
                log.write(line)
                log.flush()
            if eof and proc.poll() is not None:
                break
            if should_stop():
                stopped = True
                break
            if timeout_s and time.time() - t0 > timeout_s:
                timed_out = True
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(10)
        log.close()
    if stopped:
        raise JobStopped("stopped by user")
    if timed_out:
        raise JobFailed(f"took longer than {int(timeout_s // 60)} min and was stopped "
                        "(raise PDFUA_JOB_TIMEOUT_MIN if this file really needs longer)")
    if not os.path.exists(rpath):
        raise JobFailed(f"the worker process ended unexpectedly (exit code {proc.returncode}); "
                        "see worker.log in the job folder")
    with open(rpath, encoding="utf-8") as fh:
        res = json.load(fh)
    if not res.get("ok"):
        raise JobFailed(res.get("error", "failed"), res.get("trace", ""))
    return res["result"]


# ------------------------------------------------------------------ child
def lower_priority() -> int:
    """V4.33: a person editing comes before a file being analysed or built. This process — and the page processes it starts,
    which inherit it — runs below the web server's priority: on an idle machine nothing is slower, on a busy one the editor
    (creating a tag, scrolling, a click) is no longer starved by five files being read on every core. PDFUA_JOB_NICE sets how
    far below (default 8; 0 leaves it as it is). Returns the niceness now."""
    try:
        n = int(os.environ.get("PDFUA_JOB_NICE", "8"))
    except ValueError:
        n = 8
    try:
        if n > 0:
            if os.name == "nt":
                import ctypes
                ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)   # BELOW_NORMAL
            else:
                os.nice(n)
        return os.nice(0) if os.name != "nt" else n
    except Exception:
        return 0


def _child(job: str) -> int:
    sys.path.insert(0, BASE)
    lower_priority()
    real_stdout = sys.stdout
    sys.stdout = sys.stderr            # stray prints go to worker.log

    def progress(frac: float, msg: str) -> None:
        msg = " ".join(str(msg).split())
        real_stdout.write(f"{TAG}{float(frac):.4f} {msg}\n")
        real_stdout.flush()

    rpath = os.path.join(job, "task_result.json")
    try:
        from core import netguard                  # V4.26: nothing leaves the server
        netguard.install(process="worker")
    except Exception:
        pass
    try:
        from core.ai import AIConfig
        from core.options import Options
        from core.pipeline import ai_alt_text_all, finish, load_model, prepare, save_model
        with open(os.path.join(job, "task.json"), encoding="utf-8") as fh:
            t = json.load(fh)
        opts = Options.from_dict(t.get("options"))
        try:                                       # V4.31: the lessons Ollama gives here belong to this file
            from core import teacher
            teacher.set_context(os.path.basename(os.path.normpath(job)), getattr(opts, "client", "") or "")
        except Exception:
            pass
        if t["kind"] == "prepare":
            ai = t.get("ai")
            cfg = AIConfig(**{k: v for k, v in ai.items() if k in AIConfig().__dict__}) if ai else None
            r = prepare(job, opts, ai_cfg=cfg, progress=progress, filename=t.get("filename", ""))
        elif t["kind"] == "build":
            r = finish(job, opts, filename=t.get("filename", ""), verapdf_exe=t.get("verapdf_exe", ""),
                       progress=progress, preflight_info=t.get("preflight_info"),
                       score_before=t.get("score_before"))
        elif t["kind"] == "contrast_fix":
            from core.pipeline import fix_contrast_now
            r = fix_contrast_now(job, t.get("ids") or None, progress, fix=t.get("fix"))
        elif t["kind"] == "brain":
            # V4.25: the brain decides the text tags of a file being edited again
            from core.brain import think
            ai = t.get("ai")
            cfg = AIConfig(**{k: v for k, v in ai.items() if k in AIConfig().__dict__}) if ai else None
            model = load_model(job)
            r = think(model, os.path.join(job, "working.pdf"), opts, ai_cfg=cfg, client=opts.client or "",
                      job=os.path.basename(os.path.normpath(job)), progress=progress,
                      use_samples=bool(opts.use_samples))
            save_model(job, model)
            r = {k: v for k, v in r.items() if k != "changes"}
        elif t["kind"] == "ai_alt":
            ai = t.get("ai") or {}
            cfg = AIConfig(**{k: v for k, v in ai.items() if k in AIConfig().__dict__})
            model = load_model(job)
            r = ai_alt_text_all(job, model, cfg, progress)
            if r.get("drafted") or r.get("decorative"):
                save_model(job, model)
        else:
            raise ValueError(f"unknown task {t['kind']}")
        out = {"ok": True, "result": r}
    except Exception as exc:
        out = {"ok": False, "error": f"{exc}", "trace": traceback.format_exc()[-4000:]}
        traceback.print_exc()
    try:
        from core.teacher import flush_counts
        flush_counts()
    except Exception:
        pass
    with open(rpath + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(out, fh, default=str)
    os.replace(rpath + ".tmp", rpath)
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(_child(sys.argv[1]))
