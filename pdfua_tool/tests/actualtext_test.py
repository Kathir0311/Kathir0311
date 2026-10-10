"""Read as (/ActualText): spelled letters and the ones printed in colour (V4.4).

A sound-spelling card prints "cat" with the a in red and the pattern "u_e" with
the letters red and the underscore black. Read as it stands a screen reader says
"cat" and "u e", and the card loses the thing it is teaching. The letters and
the colour each glyph is printed in are on the page, so the sentence is built
from the page and offered for the person to apply — to one tag or to a hundred.
"""
import os
import sys
import tempfile

import pikepdf
import pymupdf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import edit                       # noqa: E402
from core.actualtext import describe, spell  # noqa: E402
from core.analyzer import analyze           # noqa: E402
from core.options import Options            # noqa: E402
from core.pipeline import process_file      # noqa: E402

RED = (0.8, 0.1, 0.1)


def card(path: str) -> None:
    d = pymupdf.open()
    p = d.new_page(width=300, height=300)
    p.insert_text((20, 50), "c", fontsize=30)
    p.insert_text((45, 50), "a", fontsize=30, color=RED)
    p.insert_text((70, 50), "t", fontsize=30)
    p.insert_text((20, 110), "u", fontsize=30, color=RED)
    p.insert_text((45, 110), "_", fontsize=30)
    p.insert_text((70, 110), "e", fontsize=30, color=RED)
    p.insert_text((20, 170), "o", fontsize=30, color=RED)
    p.insert_text((45, 170), "o", fontsize=30, color=RED)
    w = pymupdf.get_text_length("ta-", fontname="helv", fontsize=20)
    p.insert_text((20, 200), "ta-", fontsize=20)             # black
    p.insert_text((20 + w, 200), "ble", fontsize=20, color=RED)
    y = 215                                   # body text: the page's normal colour
    for _ in range(8):
        p.insert_text((20, y), "ordinary black running text printed on the card", fontsize=8)
        y += 9
    d.save(path)
    d.close()


def main() -> int:
    bad = []
    # the sentence itself, without a page
    if spell(list("cat")) != "c, a and t":
        bad.append(f"three letters spelled {spell(list('cat'))!r}")
    if spell(["u", "_", "e"]) != "u, underscore e":
        bad.append(f"a pattern spelled {spell(['u', '_', 'e'])!r}")
    if spell(list("ta-ble")) != "t, a, hyphen b, l, e":
        bad.append(f"a hyphenated word spelled {spell(list('ta-ble'))!r}")
    if spell(list("oo")) != "o, o":
        bad.append(f"two letters spelled {spell(list('oo'))!r}")
    if describe(list("cat"), [1], "red") != "c, a and t, a highlighted in red":
        bad.append(f"one highlighted letter: {describe(list('cat'), [1], 'red')!r}")
    if describe(["u", "_", "e"], [0, 2], "red") != "u, underscore e, u, e, are highlighted in red":
        bad.append(f"two of three: {describe(['u', '_', 'e'], [0, 2], 'red')!r}")
    if describe(list("ta-ble"), [3, 4, 5], "red") != \
            "t, a, hyphen b, l, e, b, l, e, are highlighted in red":
        bad.append(f"a hyphenated word: {describe(list('ta-ble'), [3, 4, 5], 'red')!r}")
    if describe(list("oo"), [0, 1], "red") != "o, o, are highlighted in red":
        bad.append(f"all highlighted: {describe(list('oo'), [0, 1], 'red')!r}")

    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "card.pdf")
    card(src)
    m = analyze(src, Options())
    m.path = src
    want = {"cat": "c, a and t, a highlighted in red",
            "u_e": "u, underscore e, u, e, are highlighted in red",
            "oo": "o, o, are highlighted in red",
            "ta-ble": "t, a, hyphen b, l, e, b, l, e, are highlighted in red"}
    ids = [n.id for n in m.ordered_nodes()
           if (n.text or "").strip().replace(" ", "") in want and not n.artifact]
    if len(ids) < 3:
        bad.append(f"the three cards were not tagged separately ({len(ids)})")
    items = edit.suggest_actual_text(m, src, ids)
    got = {it["text"].replace(" ", ""): it["suggestion"] for it in items}
    for k, v in want.items():
        if got.get(k) != v:
            bad.append(f"{k}: suggested {got.get(k)!r}, expected {v!r}")

    # apply one sentence per tag, then the same one to all, then clear
    r = edit.set_actual_text(m, [it["id"] for it in items],
                             [it["suggestion"] for it in items], "each")
    if r["changed"] != len(items):
        bad.append(f"applying one per tag changed {r['changed']} of {len(items)}")
    if any(m.nodes[it["id"]].actual_text != it["suggestion"] for it in items):
        bad.append("a tag did not keep its own sentence")
    r = edit.set_actual_text(m, ids, "say this instead", "same")
    if any(m.nodes[i].actual_text != "say this instead" for i in ids):
        bad.append("the same sentence was not applied to every tag")
    r = edit.set_actual_text(m, ids, "", "clear")
    if any(m.nodes[i].actual_text for i in ids):
        bad.append("clearing left an /ActualText behind")

    # and it reaches the built file
    job = os.path.join(tmp, "job")
    res = process_file(src, job, Options())
    if res.get("status") != "done":
        bad.append(f"build failed: {res.get('error')}")
    else:
        from core.pipeline import load_model, save_model
        m2 = load_model(job)
        ids2 = [n.id for n in m2.ordered_nodes()
                if (n.text or "").strip().replace(" ", "") in want and not n.artifact]
        items2 = edit.suggest_actual_text(m2, os.path.join(job, "working.pdf"), ids2)
        edit.set_actual_text(m2, [i["id"] for i in items2],
                             [i["suggestion"] for i in items2], "each")
        save_model(job, m2)
        from core.pipeline import finish
        finish(job, Options(), filename="card.pdf")
        pdf = pikepdf.open(os.path.join(job, "accessible.pdf"))
        found = set()
        stack = [pdf.Root.StructTreeRoot.get("/K")]
        while stack:
            e = stack.pop()
            if isinstance(e, pikepdf.Array):
                stack.extend(list(e))
                continue
            if isinstance(e, pikepdf.Dictionary) and "/S" in e:
                if "/ActualText" in e:
                    found.add(str(e.ActualText))
                k = e.get("/K")
                if k is not None:
                    stack.append(k)
        for v in want.values():
            if v not in found:
                bad.append(f"the built file does not read {v!r}")
                break

    # a page where most of the ink is red: colour is still the highlight
    syl = os.path.join(tmp, "syllables.pdf")
    d = pymupdf.open()
    p = d.new_page(width=612, height=360)
    y, boxes = 60, []
    for a, b, first_red in (("muf", "-fin", True), ("rab", "-bit", True),
                            ("kit", "-ten", False), ("fin", "-ish", True),
                            ("num", "-ber", False), ("fro", "-zen", False)):
        wa = pymupdf.get_text_length(a, fontname="hebo", fontsize=26)
        p.insert_text((60, y), a, fontsize=26, fontname="hebo",
                      color=RED if first_red else (0, 0, 0))
        p.insert_text((60 + wa, y), b, fontsize=26, fontname="hebo",
                      color=(0, 0, 0) if first_red else RED)
        boxes.append((a + b, (50, y - 26, 300, y + 8), first_red))
        y += 50
    d.save(syl)
    d.close()
    from core.actualtext import page_text_colour, suggest as suggest_one
    pg = pymupdf.open(syl)[0]
    base = page_text_colour(pg)
    if max(base) - min(base) < 0.2:
        bad.append("the syllable page should be mostly red — the test does not show the problem")
    for word, box, first_red in boxes:
        got = suggest_one(pg, box, word, base)
        if "highlighted in black" in got["text"]:
            bad.append(f"{word}: black called the highlight — {got['text']!r}")
            break
        red_part = word[:3] if first_red else word[4:]
        if not all(c in got["text"].split(", ") for c in red_part):
            bad.append(f"{word}: the red letters are not the highlighted ones — {got['text']!r}")
            break

    # a line tracked out letter by letter is read as the words it spells
    logo = os.path.join(tmp, "logo.pdf")
    d = pymupdf.open()
    p = d.new_page(width=612, height=260)
    p.insert_text((40, 50), "Sample Words and sounds for the card", fontsize=11)
    p.insert_text((40, 90), " ".join("abcdefghijklm"), fontsize=14)        # an alphabet row
    p.insert_text((40, 140), " ".join("BenchmarkeducationcompanY"), fontsize=9)
    p.insert_text((40, 170), "© Benchmark Education Company LLC", fontsize=9)
    d.save(logo)
    d.close()
    ml = analyze(logo, Options())
    spaced = [n for n in ml.ordered_nodes() if "B e n c h" in (n.text or "")]
    if not spaced:
        bad.append("the tracked logo line was not tagged")
    elif spaced[0].actual_text != "Benchmark education companY":
        bad.append(f"the logo line reads {spaced[0].actual_text!r}")
    letters = [n for n in ml.ordered_nodes() if (n.text or "").startswith("a b c d")]
    if letters and letters[0].actual_text:
        bad.append(f"an alphabet row was joined into a word: {letters[0].actual_text!r}")
    pgl = pymupdf.open(logo)[0]
    from core.actualtext import suggest as suggest_one
    if suggest_one(pgl, (34, 128, 420, 150), "")["text"] != "Benchmark education companY":
        bad.append("Suggest from the page does not join the tracked line")
    if "and m" not in suggest_one(pgl, (34, 74, 420, 96), "")["text"]:
        bad.append("Suggest from the page joined the alphabet row instead of spelling it")

    if bad:
        print("actual text test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("actual text test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
