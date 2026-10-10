"""What people change in a file they are working on, learned in a process of its own (V4.28).

The web app notices an edit and, a few seconds after the last one, starts this module for
the job (`python -m core.livelearn <job folder> <client> <who> <reviewer 0/1> <zones 0/1> <roles 0/1>`).
It compares the tags as they are now with the tool's own proposal (`auto_model.json`) and
writes what it finds to the learning database:

* the roles people changed (P → H2, a stamp → artifact) — the brain's feedback, V4.25;
* the alt texts and readings people gave — the memory, V4.26;
* the zones people cut or joined — the pairs of lines the analysis decided on, V4.28
  (core/zonelearn.py); when something was corrected, the zone models are trained again.

Why a process of its own: reading the PDF to measure the corrected tags would otherwise
take the editor's MuPDF lock — and with many people editing, every pause in typing would
make everybody else wait. In its own process it never waits for anybody and nobody waits
for it. The files it reads are replaced whole (never half written), so it can run while the
person goes on editing; what they do next is learned by the next run.
"""
from __future__ import annotations

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(job_dir: str, client: str = "", by: str = "", reviewer: bool = False, zones: bool = True,
        roles: bool = True) -> dict:
    t0 = time.time()
    from . import brain, memory, zonelearn
    from .model import DocModel
    from .pipeline import load_model
    out: dict = {"job": os.path.basename(os.path.normpath(job_dir)), "by": by}
    model = load_model(job_dir)
    auto = None
    ap = os.path.join(job_dir, "auto_model.json")
    if os.path.exists(ap):
        with open(ap, encoding="utf-8") as fh:
            auto = DocModel.from_json(fh.read())
    if roles and auto is not None:
        try:
            out["feedback"] = brain.record_feedback(job_dir, client, model, auto, by=by)
        except Exception as exc:
            out["feedback"] = {"error": str(exc)[:200]}
        try:
            out["memory"] = memory.learn(job_dir, client, model, auto, verified=False)
        except Exception as exc:
            out["memory"] = {"error": str(exc)[:200]}
    if zones:
        try:
            out["zones"] = zonelearn.record_job(job_dir, client, model, auto, by=by, reviewer=reviewer)
            if out["zones"].get("rows"):
                out["zone_models"] = [{k: m[k] for k in ("scope", "status", "note", "fixed", "broken")}
                                      for m in zonelearn.train_all().get("models", [])]
        except Exception as exc:
            out["zones"] = {"error": str(exc)[:200]}
    out["seconds"] = round(time.time() - t0, 2)
    return out


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m core.livelearn <job folder> [client] [who] [reviewer 0/1] [zones 0/1] [roles 0/1]", file=sys.stderr)
        return 2
    try:
        from . import netguard                       # nothing leaves the server
        netguard.install(process="live learning")
    except Exception:
        pass
    a = argv + [""] * 6
    try:
        r = run(a[0], a[1], a[2], a[3] == "1", a[4] != "0", a[5] != "0")
    except Exception as exc:
        r = {"error": str(exc)[:300]}
    print(json.dumps(r, default=str), flush=True)
    return 0 if "error" not in r else 1


if __name__ == "__main__":
    sys.path.insert(0, ROOT)
    sys.exit(main(sys.argv[1:]))
