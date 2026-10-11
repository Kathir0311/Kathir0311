"""V4.33 — a person editing comes before a file being analysed or built.

The editor was starved while files were read on every core: the analysis and build workers ran at the web server's priority
(five files at once, ten page processes each, on four cores), so creating a tag or a click waited for the CPU. Now the worker
that analyses or builds a file — and the page processes it starts, which inherit it — runs below the web server.

* `lower_priority()` puts the process 8 steps down (PDFUA_JOB_NICE=… sets how far, 0 leaves it) and says where it is
* the worker that `run_task` really starts is at that priority while it works on a file (read from /proc, Linux)
* the file is analysed all the same
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main() -> int:
    bad: list = []

    def ok(cond, msg):
        if not cond:
            bad.append(msg)

    code = "import os; from core.jobrunner import lower_priority; a = os.nice(0); b = lower_priority(); print(a, b, os.nice(0))"
    if os.name != "nt":
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT)
        a, b, c = (int(x) for x in r.stdout.split())
        ok(b == a + 8 and c == b, f"the worker goes 8 steps down: {a} → {b} ({r.stderr[-120:]})")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, env=dict(os.environ, PDFUA_JOB_NICE="0"))
        a, b, c = (int(x) for x in r.stdout.split())
        ok(a == b == c, f"PDFUA_JOB_NICE=0 leaves it as it is: {a} {b} {c}")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, env=dict(os.environ, PDFUA_JOB_NICE="3"))
        a, b, c = (int(x) for x in r.stdout.split())
        ok(b == a + 3, f"…and how far is up to the admin: {a} → {b}")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, env=dict(os.environ, PDFUA_JOB_NICE="not a number"))
        a, b, c = (int(x) for x in r.stdout.split())
        ok(b == a + 8, f"a setting that is not a number does not stop the worker: {a} → {b}")

    # ------------------------------------------------------------------ the worker that run_task starts
    from core.jobrunner import run_task
    work = tempfile.mkdtemp()
    job = os.path.join(work, "job")
    os.makedirs(job)
    src = os.path.join(ROOT, "tests", "corpus", "book.pdf")
    seen: list = []

    def watch(proc):
        def poll():
            t0 = time.time()
            while time.time() - t0 < 20 and proc.poll() is None:
                try:
                    with open(f"/proc/{proc.pid}/stat") as fh:
                        f = fh.read().rsplit(")", 1)[1].split()
                    nice = int(f[16])                         # field 19 of stat: nice
                    seen.append(nice)
                    if nice >= 8:
                        return
                except (OSError, ValueError, IndexError):
                    pass
                time.sleep(0.02)
        threading.Thread(target=poll, daemon=True).start()

    from core.options import Options
    import dataclasses
    task = {"kind": "prepare", "filename": "book.pdf", "options": dataclasses.asdict(Options(fix_contrast="off"))}
    import shutil
    shutil.copyfile(src, os.path.join(job, "original.pdf"))
    try:
        res = run_task(job, task, lambda f, m: None, lambda: False, timeout_s=120, on_start=watch if os.name != "nt" else None)
    except Exception as exc:  # noqa: BLE001
        res = {"error": f"{type(exc).__name__}: {exc}"}
    ok(not res.get("error"), f"the file is analysed all the same: {str(res)[:160]}")
    if os.name != "nt":
        ok(seen and max(seen) >= 8, f"the worker run_task starts is below the web server's priority (nice {max(seen) if seen else '?'})")
    shutil.rmtree(work, ignore_errors=True)

    if bad:
        print("worker priority test: FAILED")
        for b_ in bad:
            print("   ", b_)
        return 1
    print("worker priority test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
