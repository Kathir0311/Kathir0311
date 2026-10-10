"""CPU-parallel page processing inside one file.

Pages are analysed independently once the document-wide statistics are known,
so big files are split into page chunks that run in separate processes (each
with its own MuPDF, which is not thread-safe). Used by the layout analysis and
the colour-contrast measurement.

Number of processes: PDFUA_PAGE_PROCS (set per job by the web app from
Settings → "CPU cores per file"); 0 or unset = automatic.
"""
from __future__ import annotations

import multiprocessing as mp
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Callable

MIN_PAGES = 24          # below this, starting processes costs more than it saves


def cpu_count() -> int:
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except Exception:
        return max(1, os.cpu_count() or 1)


def page_procs() -> int:
    try:
        v = int(os.environ.get("PDFUA_PAGE_PROCS", "0") or 0)
    except ValueError:
        v = 0
    if v > 0:
        return v
    return max(1, min(8, cpu_count()))


def chunks(pages: list[int], procs: int, min_size: int = 6) -> list[list[int]]:
    """~3 chunks per process (for progress and load balance), in page order."""
    n = len(pages)
    k = max(1, min(procs * 3, n // min_size or 1))
    size = -(-n // k)
    return [pages[i:i + size] for i in range(0, n, size)]


def run_chunks(fn: Callable, jobs: list[tuple], procs: int,
               progress: Callable[[int, int], None] | None = None) -> list:
    """Run fn(*args) for every args tuple in `jobs` on `procs` processes;
    results come back in job order. progress(done, total) after each job."""
    import sys
    out: list = [None] * len(jobs)
    ctx = mp.get_context("spawn")
    # spawned workers re-import the launching script; a script that is not a
    # file (stdin, notebook) cannot be — hide it so only our modules load
    main = sys.modules.get("__main__")
    hidden = None
    f = getattr(main, "__file__", None)
    if f and not os.path.exists(f):
        hidden = f
        del main.__file__
    try:
        with ProcessPoolExecutor(max_workers=min(procs, len(jobs)), mp_context=ctx) as ex:
            futs = {ex.submit(fn, *args): i for i, args in enumerate(jobs)}
            done = 0
            for fu in as_completed(futs):
                out[futs[fu]] = fu.result()
                done += 1
                if progress:
                    progress(done, len(jobs))
    finally:
        if hidden is not None:
            main.__file__ = hidden
    return out
