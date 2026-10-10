"""Print the proposed tag tree for a PDF (debug helper)."""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])

from core.analyzer import analyze  # noqa: E402
from core.options import Options   # noqa: E402


def show(path, opts=None, limit=400):
    m = analyze(path, opts or Options())
    print(f"== {path.split('/')[-1]}  title={m.title!r} lang={m.lang} body={m.body_size} "
          f"nodes={len(m.nodes)} info={m.info} warnings={m.warnings[:3]}")
    depth = m.depth_of()
    for k, n in enumerate(m.ordered_nodes()):
        if k >= limit:
            break
        role = ("ART:" + (n.pagination_subtype or n.artifact_subtype)) if n.artifact else n.role
        extra = []
        if n.lang:
            extra.append(f"lang={n.lang}")
        if n.scope:
            extra.append(f"scope={n.scope}")
        if n.rowspan > 1 or n.colspan > 1:
            extra.append(f"span={n.rowspan}x{n.colspan}")
        if n.headers:
            extra.append(f"hdrs={len(n.headers)}")
        if n.list_numbering:
            extra.append(n.list_numbering)
        if n.parts:
            extra.append(f"parts={len(n.parts)}")
        if n.annots:
            extra.append(f"annot={n.annots}")
        print(f"{'  ' * depth[n.id]}p{n.page + 1} {role:9} {n.confidence:.2f} "
              f"{' '.join(extra)} | {(n.text or '')[:60]}")
    return m


if __name__ == "__main__":
    for p in sys.argv[1:]:
        show(p)
