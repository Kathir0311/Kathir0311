"""Independent verification helpers used by the regression suite."""
from __future__ import annotations

import subprocess

import pikepdf
import pymupdf
from pikepdf import Array, Dictionary, Name


def render_diff(a: str, b: str, zoom: float = 1.0) -> list[int]:
    """Max per-channel pixel difference per page (0 = identical)."""
    da, db = pymupdf.open(a), pymupdf.open(b)
    out = []
    for pa, pb in zip(da, db):
        xa = pa.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        xb = pb.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        if (xa.width, xa.height) != (xb.width, xb.height):
            out.append(255)
            continue
        sa, sb = xa.samples, xb.samples
        if sa == sb:
            out.append(0)
            continue
        out.append(max(abs(x - y) for x, y in zip(sa, sb)))
    return out


def qpdf_check(path: str) -> str:
    r = subprocess.run(["qpdf", "--check", path], capture_output=True, text=True)
    lines = [l for l in (r.stdout + r.stderr).splitlines()
             if "WARNING" in l or "error" in l.lower()]
    lines = [l for l in lines if "may still contain" not in l and "cannot detect" not in l]
    return "ok" if r.returncode == 0 and not lines else "; ".join(lines[:5]) or f"rc={r.returncode}"


def _content_mcids(page) -> list[int]:
    out = []
    for ins in pikepdf.parse_content_stream(page):
        if str(ins.operator) == "BDC" and len(ins.operands) > 1:
            p = ins.operands[1]
            if isinstance(p, Dictionary) and "/MCID" in p:
                out.append(int(p.MCID))
    return out


def structure_integrity(path: str) -> list[str]:
    """Cross-check content MCIDs, parent tree and struct element kids."""
    errs: list[str] = []
    pdf = pikepdf.open(path)
    root = pdf.Root.get("/StructTreeRoot")
    if root is None:
        return ["no StructTreeRoot"]
    nums = root.ParentTree.get("/Nums") or []
    ptree = {int(nums[i]): nums[i + 1] for i in range(0, len(nums), 2)}
    page_ids = {p.objgen: i for i, p in enumerate(pdf.pages)}

    # every elem's MCID kids
    claimed: dict[tuple[int, int], object] = {}
    stack = [root.K] if not isinstance(root.K, Array) else list(root.K)
    seen = set()
    objr_ok = 0
    while stack:
        e = stack.pop()
        if not isinstance(e, Dictionary) or e.objgen in seen:
            continue
        seen.add(e.objgen)
        pg = e.get("/Pg")
        kids = e.get("/K")
        kids = list(kids) if isinstance(kids, Array) else ([kids] if kids is not None else [])
        for k in kids:
            if isinstance(k, int) or (hasattr(k, "__int__") and not isinstance(k, Dictionary)):
                if pg is None:
                    errs.append(f"{e.S}: integer MCID without /Pg")
                    continue
                key = (page_ids.get(pg.objgen, -1), int(k))
                if key in claimed:
                    errs.append(f"MCID {key} claimed twice")
                claimed[key] = e
            elif isinstance(k, Dictionary) and k.get("/Type") == Name.MCR:
                key = (page_ids.get(k.Pg.objgen, -1), int(k.MCID))
                if key in claimed:
                    errs.append(f"MCID {key} claimed twice")
                claimed[key] = e
            elif isinstance(k, Dictionary) and k.get("/Type") == Name.OBJR:
                a = k.Obj
                sp = a.get("/StructParent")
                if sp is None or int(sp) not in ptree or ptree[int(sp)].objgen != e.objgen:
                    errs.append(f"OBJR annotation not mapped back to {e.S}")
                else:
                    objr_ok += 1
            elif isinstance(k, Dictionary):
                if k.get("/P") is None or k.P.objgen != e.objgen:
                    errs.append(f"{k.get('/S')}: /P does not point to parent {e.S}")
                stack.append(k)

    # every content MCID resolves both ways
    for pno, page in enumerate(pdf.pages):
        mcids = _content_mcids(page)
        if not mcids:
            continue
        sp = page.obj.get("/StructParents")
        if sp is None:
            errs.append(f"page {pno + 1}: MCIDs but no /StructParents")
            continue
        arr = ptree.get(int(sp))
        if len(set(mcids)) != len(mcids):
            errs.append(f"page {pno + 1}: duplicate MCIDs in content")
        for m in mcids:
            if arr is None or m >= len(arr):
                errs.append(f"page {pno + 1}: MCID {m} missing from parent tree")
                continue
            owner = arr[m]
            c = claimed.get((pno, m))
            if c is None:
                errs.append(f"page {pno + 1}: MCID {m} not referenced by any element")
            elif c.objgen != owner.objgen:
                errs.append(f"page {pno + 1}: MCID {m} parent tree / element mismatch")
    for (pno, m) in claimed:
        if m not in set(_content_mcids(pdf.pages[pno])):
            errs.append(f"page {pno + 1}: element claims MCID {m} not in content")
    return errs


def mc_balance(path: str) -> list[str]:
    """BDC/EMC balanced per page, never interleaved with q/Q or BT/ET badly."""
    errs = []
    pdf = pikepdf.open(path)
    for pno, page in enumerate(pdf.pages):
        depth = 0
        for ins in pikepdf.parse_content_stream(page):
            op = str(ins.operator)
            if op in ("BDC", "BMC"):
                depth += 1
            elif op == "EMC":
                depth -= 1
                if depth < 0:
                    errs.append(f"page {pno + 1}: EMC without BDC")
                    depth = 0
        if depth:
            errs.append(f"page {pno + 1}: {depth} unclosed marked-content sequence(s)")
    return errs
