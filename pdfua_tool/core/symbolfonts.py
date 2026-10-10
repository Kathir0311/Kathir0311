"""Symbol fonts: the bullets and signs Word, PowerPoint and InDesign set in
Wingdings, Wingdings 2/3, Symbol or ZapfDingbats (V4.19.4).

In those fonts a code is not a letter: Wingdings "q" is the box bullet ❑,
"Ø" the arrowhead ➢, "ü" the check mark ✓. A PDF often says nothing right
about them, so the text layer — and a screen reader — reads them wrong:

* ToUnicode points into the Private Use Area (U+F071) → "" / nothing
* ToUnicode says U+FFFD, or a CID font has no ToUnicode → "�"
* ToUnicode (or the font's encoding) gives the code as a Latin letter → "q",
  "Ø", "¾", "§", "ü"

`repair(pdf)` gives every such font a ToUnicode with the real characters
(Unicode's own mapping of these fonts), keeping every entry that was already
right. The analysis then reads "❑ The central nervous system …" and makes
the bullet the item's Lbl, and the built file reads right in NVDA and PAC.
"""
from __future__ import annotations

import io
import re

import pikepdf
from pikepdf import Dictionary, Name


# ------------------------------------------------------------ the fonts
def family(fontname: str) -> str:
    """'wingdings', 'wingdings2', 'wingdings3', 'webdings', 'symbol',
    'zapf' — or '' for a text font. The subset prefix is ignored."""
    s = str(fontname or "")
    s = s[1:] if s.startswith("/") else s
    if re.match(r"^[A-Z]{6}\+", s):
        s = s[7:]
    k = re.sub(r"[\s,_\-]+", "", s).lower()
    if k.startswith("wingdings3"):
        return "wingdings3"
    if k.startswith("wingdings2"):
        return "wingdings2"
    if k.startswith("wingding"):
        return "wingdings"
    if k.startswith("webdings"):
        return "webdings"
    if k.startswith(("zapfdingbats", "itczapfdingbats", "dingbats")) or k == "zadb":
        return "zapf"
    if (k.startswith("symbol") and not k.startswith("symbola")) or k in ("symb",):
        return "symbol"
    return ""


def _parse(spec: str) -> dict[int, str]:
    out = {}
    for tok in spec.split():
        code, cp = tok.split(":")
        out[int(code, 16)] = chr(int(cp, 16))
    return out


# Unicode's mapping of the Wingdings fonts (code → character)
_WD = _parse("""
21:1F589 22:2702 23:2701 24:1F453 25:1F56D 26:1F56E 27:1F56F 28:1F57F 29:2706 2A:1F582 2B:1F583 2C:1F4EA
2D:1F4EB 2E:1F4EC 2F:1F4ED 30:1F5C0 31:1F5C1 32:1F5CE 33:1F5CF 34:1F5D0 35:1F5C4 36:23F3 37:1F5AE 38:1F5B0
39:1F5B2 3A:1F5B3 3B:1F5B4 3C:1F5AB 3D:1F5AC 3E:2707 3F:270D 40:1F58E 41:270C 42:1F58F 43:1F44D 44:1F44E
45:261C 46:261E 47:261D 48:1F597 49:1F590 4A:263A 4B:1F610 4C:2639 4D:1F4A3 4E:1F571 4F:1F3F3 50:1F3F1
51:2708 52:263C 53:1F322 54:2744 55:1F546 56:271E 57:1F548 58:2720 59:2721 5A:262A 5B:262F 5C:1F549
5D:2638 5E:2648 5F:2649 60:264A 61:264B 62:264C 63:264D 64:264E 65:264F 66:2650 67:2651 68:2652 69:2653
6A:1F670 6B:1F675 6C:25CF 6D:1F53E 6E:25A0 6F:25A1 70:25FB 71:2751 72:2752 73:2B27 74:29EB 75:25C6 76:2756
77:2B25 78:2327 79:2BB9 7A:2318 7B:1F3F5 7C:1F3F6 7D:1F676 7E:1F677 80:1F10B 81:2780 82:2781 83:2782
84:2783 85:2784 86:2785 87:2786 88:2787 89:2788 8A:2789 8B:1F10C 8C:278A 8D:278B 8E:278C 8F:278D 90:278E
91:278F 92:2790 93:2791 94:2792 95:2793 96:1F662 97:1F660 98:1F661 99:1F663 9A:1F65E 9B:1F65C 9C:1F65D
9D:1F65F 9E:2219 9F:2022 A0:2B1D A1:25CB A2:1F786 A3:1F788 A4:25C9 A5:25CE A6:1F53F A7:25AA A8:25FB
A9:1F7C1 AA:2726 AB:2605 AC:2736 AD:2734 AE:2739 AF:2735 B0:2BD0 B1:2316 B2:2BCE B3:2BCF B4:2BD1 B5:272A
B6:2730 B7:1F550 B8:1F551 B9:1F552 BA:1F553 BB:1F554 BC:1F555 BD:1F556 BE:1F557 BF:1F558 C0:1F559
C1:1F55A C2:1F55B C3:2BB0 C4:2BB1 C5:2BB2 C6:2BB3 C7:2BB4 C8:2BB5 C9:2BB6 CA:2BB7 CB:1F66A CC:1F66B
CD:1F655 CE:1F654 CF:1F657 D0:1F656 D1:1F650 D2:1F651 D3:1F652 D4:1F653 D5:232B D6:2326 D7:2B98 D8:27A2
D9:2B99 DA:2B9B DB:2B88 DC:2B8A DD:2B89 DE:2B8B DF:1F868 E0:1F86A E1:1F869 E2:1F86B E3:1F86C E4:1F86D
E5:1F86F E6:1F86E E7:1F878 E8:2794 E9:1F879 EA:1F87B EB:1F87C EC:1F87D ED:1F87F EE:1F87E EF:21E6 F0:21E8
F1:21E7 F2:21E9 F3:2B04 F4:21F3 F5:2B01 F6:2B00 F7:2B03 F8:2B02 F9:1F8AC FA:1F8AD FB:2717 FC:2713 FD:2612
FE:2611 FF:229E
""")
# the commonest bullets above are given the plain form screen readers and
# fonts know (●, ■, □, ➢, ➔, ✗, ☒, ☑) rather than Unicode's newer look-alikes

_WD2 = _parse("""
21:1F58A 22:1F58B 23:1F58C 24:1F58D 25:2704 26:2700 27:1F57E 28:1F57D 29:1F5C5 2A:1F5C6 2B:1F5C7 2C:1F5C8
2D:1F5C9 2E:1F5CA 2F:1F5CB 30:1F5CC 31:1F5CD 32:1F4CB 33:1F5D1 34:1F5D4 35:1F5B5 36:1F5B6 37:1F5B7
38:1F5B8 39:1F5AD 3A:1F5AF 3B:1F5B1 3C:1F592 3D:1F593 3E:1F598 3F:1F599 40:1F59A 41:1F59B 42:1F448
43:1F449 44:1F59C 45:1F59D 46:1F59E 47:1F59F 48:1F5A0 49:1F5A1 4A:1F446 4B:1F447 4C:1F5A2 4D:1F5A3
4E:1F591 4F:2717 50:2713 51:2718 52:2611 53:2612 54:2612 55:2BBE 56:2BBF 57:1F6C7 58:29B8 59:1F671
5A:1F674 5B:1F672 5C:1F673 5D:203D 5E:1F679 5F:1F67A 60:1F67B 61:1F666 62:1F664 63:1F665 64:1F667
65:1F65A 66:1F658 67:1F659 68:1F65B 69:24EA 6A:2460 6B:2461 6C:2462 6D:2463 6E:2464 6F:2465 70:2466
71:2467 72:2468 73:2469 74:24FF 75:2776 76:2777 77:2778 78:2779 79:277A 7A:277B 7B:277C 7C:277D 7D:277E
7E:277F 80:2609 81:1F315 82:263D 83:263E 84:2E3F 85:271D 86:1F547 87:1F55C 88:1F55D 89:1F55E 8A:1F55F
8B:1F560 8C:1F561 8D:1F562 8E:1F563 8F:1F564 90:1F565 91:1F566 92:1F567 93:1F668 94:1F669 95:22C5
96:1F784 97:2981 98:25CF 99:25CB 9A:1F785 9B:1F787 9C:1F789 9D:2299 9E:29BF 9F:1F78C A0:1F78D A1:25FE
A2:25A0 A3:25A1 A4:1F791 A5:1F792 A6:1F793 A7:1F794 A8:25A3 A9:1F795 AA:1F796 AB:1F797 AC:1F798 AD:2B29
AE:2B25 AF:25C7 B0:1F79A B1:25C8 B2:1F79B B3:1F79C B4:1F79D B5:1F79E B6:2B2A B7:2B27 B8:25CA B9:1F7A0
BA:25D6 BB:25D7 BC:2BCA BD:2BCB BE:2BC0 BF:2BC1 C0:2B1F C1:2BC2 C2:2B23 C3:2B22 C4:2BC3 C5:2BC4 C6:1F7A1
C7:1F7A2 C8:1F7A3 C9:1F7A4 CA:1F7A5 CB:1F7A6 CC:1F7A7 CD:1F7A8 CE:1F7A9 CF:1F7AA D0:1F7AB D1:1F7AC
D2:1F7AD D3:1F7AE D4:1F7AF D5:1F7B0 D6:1F7B1 D7:1F7B2 D8:1F7B3 D9:1F7B4 DA:1F7B5 DB:1F7B6 DC:1F7B7
DD:1F7B8 DE:1F7B9 DF:1F7BA E0:1F7BB E1:1F7BC E2:1F7BD E3:1F7BE E4:1F7BF E5:1F7C0 E6:1F7C2 E7:1F7C4
E8:1F7C6 E9:1F7C9 EA:1F7CA EB:2736 EC:1F7CC ED:1F7CE EE:1F7D0 EF:1F7D2 F0:2739 F1:1F7C3 F2:1F7C7 F3:272F
F4:1F7CD F5:1F7D4 F6:2BCC F7:2BCD F8:203B F9:2042
""")

_WD3 = _parse("""
21:2B60 22:2B62 23:2B61 24:2B63 25:2B66 26:2B67 27:2B69 28:2B68 29:2B70 2A:2B72 2B:2B71 2C:2B73 2D:2B76
2E:2B78 2F:2B7B 30:2B7D 31:2B64 32:2B65 33:2B6A 34:2B6C 35:2B6B 36:2B6D 37:2B4D 38:2BA0 39:2BA1 3A:2BA2
3B:2BA3 3C:2BA4 3D:2BA5 3E:2BA6 3F:2BA7 40:2B90 41:2B91 42:2B92 43:2B93 44:2B80 45:2B83 46:2B7E 47:2B7F
48:2B84 49:2B86 4A:2B85 4B:2B87 4C:2B8F 4D:2B8D 4E:2B8E 4F:2B8C 50:2B6E 51:2B6F 52:238B 53:2324 54:2303
55:2325 56:2423 57:237D 58:21EA 59:2BB8 5A:1F8A0 5B:1F8A1 5C:1F8A2 5D:1F8A3 5E:1F8A4 5F:1F8A5 60:1F8A6
61:1F8A7 62:1F8A8 63:1F8A9 64:1F8AA 65:1F8AB 66:1F850 67:1F852 68:1F851 69:1F853 6A:1F854 6B:1F855
6C:1F857 6D:1F856 6E:1F858 6F:1F859 70:25B2 71:25BC 72:25B3 73:25BD 74:25C0 75:25B6 76:25C1 77:25B7
78:25E3 79:25E2 7A:25E4 7B:25E5 7C:1F780 7D:25BA 7E:1F781 80:1F783 81:2BC5 82:2BC6 83:2BC7 84:2BC8
85:2B9C 86:2B9E 87:2B9D 88:2B9F 89:1F810 8A:1F812 8B:1F811 8C:1F813 8D:1F814 8E:1F816 8F:1F815 90:1F817
91:1F818 92:1F81A 93:1F819 94:1F81B 95:1F81C 96:1F81E 97:1F81D 98:1F81F 99:1F800 9A:1F802 9B:1F801
9C:1F803 9D:1F804 9E:1F806 9F:1F805 A0:1F807 A1:1F808 A2:1F80A A3:1F809 A4:1F80B A5:1F820 A6:1F822
A7:1F824 A8:1F826 A9:1F828 AA:1F82A AB:1F82C AC:1F89C AD:1F89D AE:1F89E AF:1F89F B0:1F82E B1:1F830
B2:1F832 B3:1F834 B4:1F836 B5:1F838 B6:1F83A B7:1F839 B8:1F83B B9:1F898 BA:1F89A BB:1F899 BC:1F89B
BD:1F83C BE:1F83E BF:1F83D C0:1F83F C1:1F840 C2:1F842 C3:1F841 C4:1F843 C5:1F844 C6:1F846 C7:1F845
C8:1F847 C9:2BA8 CA:2BA9 CB:2BAA CC:2BAB CD:2BAC CE:2BAD CF:2BAE D0:2BAF D1:1F860 D2:1F862 D3:1F861
D4:1F863 D5:1F864 D6:1F865 D7:1F867 D8:1F866 D9:1F870 DA:1F872 DB:1F871 DC:1F873 DD:1F874 DE:1F875
DF:1F877 E0:1F876 E1:1F880 E2:1F882 E3:1F881 E4:1F883 E5:1F884 E6:1F885 E7:1F887 E8:1F886 E9:1F890
EA:1F892 EB:1F891 EC:1F893 ED:1F894 EE:1F896 EF:1F895 F0:1F897
""")

# Adobe's Symbol encoding (the letters are Greek)
_SYM = {c: chr(c) for c in range(0x20, 0x7F)}
_SYM.update(_parse("""
22:2200 24:2203 27:220B 2A:2217 2D:2212 40:2245 5C:2234 5E:22A5 60:203E 7E:223C
41:391 42:392 43:3A7 44:394 45:395 46:3A6 47:393 48:397 49:399 4A:3D1 4B:39A 4C:39B 4D:39C 4E:39D 4F:39F
50:3A0 51:398 52:3A1 53:3A3 54:3A4 55:3A5 56:3C2 57:3A9 58:39E 59:3A8 5A:396
61:3B1 62:3B2 63:3C7 64:3B4 65:3B5 66:3C6 67:3B3 68:3B7 69:3B9 6A:3D5 6B:3BA 6C:3BB 6D:3BC 6E:3BD 6F:3BF
70:3C0 71:3B8 72:3C1 73:3C3 74:3C4 75:3C5 76:3D6 77:3C9 78:3BE 79:3C8 7A:3B6
A1:3D2 A2:2032 A3:2264 A4:2044 A5:221E A6:192 A7:2663 A8:2666 A9:2665 AA:2660 AB:2194 AC:2190 AD:2191
AE:2192 AF:2193 B0:B0 B1:B1 B2:2033 B3:2265 B4:D7 B5:221D B6:2202 B7:2022 B8:F7 B9:2260 BA:2261 BB:2248
BC:2026 BD:23D0 BE:23AF BF:21B5 C0:2135 C1:2111 C2:211C C3:2118 C4:2297 C5:2295 C6:2205 C7:2229 C8:222A
C9:2283 CA:2287 CB:2284 CC:2282 CD:2286 CE:2208 CF:2209 D0:2220 D1:2207 D2:AE D3:A9 D4:2122 D5:220F
D6:221A D7:22C5 D8:AC D9:2227 DA:2228 DB:21D4 DC:21D0 DD:21D1 DE:21D2 DF:21D3 E0:25CA E1:2329 E2:AE
E3:A9 E4:2122 E5:2211 E6:239B E7:239C E8:239D E9:23A1 EA:23A2 EB:23A3 EC:23A7 ED:23A8 EE:23A9 EF:23AA
F1:232A F2:222B F3:2320 F4:23AE F5:2321 F6:239E F7:239F F8:23A0 F9:23A4 FA:23A5 FB:23A6 FC:23AB FD:23AC
FE:23AD
"""))


def _zapf(code: int) -> str | None:
    odd = {0x25: "☎", 0x2A: "☛", 0x2B: "☞", 0x48: "★", 0x6C: "●",
           0x6E: "■", 0x73: "▲", 0x74: "▼", 0x75: "◆", 0x77: "◗",
           0xA8: "♣", 0xA9: "♦", 0xAA: "♥", 0xAB: "♠",
           0xD5: "→", 0xD6: "↔", 0xD7: "↕"}
    if code in odd:
        return odd[code]
    if 0x21 <= code <= 0x7E:
        return chr(0x2701 + code - 0x21)
    if 0xA1 <= code <= 0xA7:
        return chr(0x2761 + code - 0xA1)
    if 0xAC <= code <= 0xD4:
        return chr(0x2460 + code - 0xAC) if code <= 0xB5 else chr(0x2776 + code - 0xB6)
    if 0xD8 <= code <= 0xEF:
        return chr(0x2798 + code - 0xD8)
    if 0xF1 <= code <= 0xFE:
        return chr(0x27B1 + code - 0xF1)
    return None


def char_for(fam: str, code: int) -> str | None:
    """The character a code of a symbol font prints."""
    if code == 0x20:
        return " "
    if fam == "wingdings":
        return _WD.get(code)
    if fam == "wingdings2":
        return _WD2.get(code)
    if fam == "wingdings3":
        return _WD3.get(code)
    if fam == "symbol":
        return _SYM.get(code)
    if fam == "zapf":
        return _zapf(code)
    return None


def decode(fam: str, ch: str) -> str:
    """A glyph of a symbol font as the text layer gave it ("", "q", "Ø")
    → the character it prints, or "" when that cannot be told."""
    if not ch or not fam:
        return ""
    cp = ord(ch[0])
    if 0xF020 <= cp <= 0xF0FF:
        cp -= 0xF000
    if fam == "symbol" and cp >= 0x100:
        return ""
    if cp > 0xFF:
        return ""
    return char_for(fam, cp) or ""


def unreadable(ch: str) -> bool:
    """A glyph the text layer could not name: Private Use, U+FFFD, a control."""
    if not ch:
        return True
    cp = ord(ch[0])
    return 0xE000 <= cp <= 0xF8FF or cp == 0xFFFD or cp < 0x20


# ------------------------------------------------------------ ToUnicode
_HEX = r"<([0-9A-Fa-f\s]*)>"


def parse_cmap(data: bytes) -> tuple[dict[int, str], bool]:
    """code → text of a ToUnicode CMap; and whether its codes are 2 bytes."""
    s = data.decode("latin-1", "replace")
    out: dict[int, str] = {}
    two = False
    m = re.search(r"begincodespacerange\s*" + _HEX, s)
    if m and len(re.sub(r"\s", "", m.group(1))) >= 4:
        two = True

    def txt(h: str) -> str:
        h = re.sub(r"\s", "", h)
        if len(h) % 4:
            h = h.rjust(len(h) + (4 - len(h) % 4), "0")
        try:
            return bytes.fromhex(h).decode("utf-16-be", "replace")
        except ValueError:
            return ""

    for blk in re.findall(r"beginbfchar(.*?)endbfchar", s, re.S):
        for a, b in re.findall(_HEX + r"\s*" + _HEX, blk):
            a = re.sub(r"\s", "", a)
            if a:
                out[int(a, 16)] = txt(b)
    for blk in re.findall(r"beginbfrange(.*?)endbfrange", s, re.S):
        for a, b, rest in re.findall(_HEX + r"\s*" + _HEX + r"\s*(\[[^\]]*\]|<[0-9A-Fa-f\s]*>)", blk):
            a, b = int(re.sub(r"\s", "", a) or "0", 16), int(re.sub(r"\s", "", b) or "0", 16)
            if b < a or b - a > 0xFFFF:
                continue
            if rest.startswith("["):
                for k, h in enumerate(re.findall(_HEX, rest)):
                    if a + k <= b:
                        out[a + k] = txt(h)
            else:
                base = txt(rest[1:-1])
                if not base:
                    continue
                for k in range(b - a + 1):
                    out[a + k] = base[:-1] + chr(min(0x10FFFF, ord(base[-1]) + k))
    return out, two


def _wrong(fam: str, code: int, old: str | None, new: str) -> bool:
    """Is `old` (what the file says code means) wrong for a symbol font?"""
    if old is None or old == "" or unreadable(old):
        return True
    if old == new:
        return False
    if len(old) != 1:
        return False
    cp = ord(old)
    if fam == "symbol":
        # Symbol's digits and punctuation are ASCII; a Latin letter or a
        # Latin-1 sign standing for the code itself is the encoding read as text
        return old == chr(code) or (0xA0 <= cp <= 0xFF and cp == code)
    # Wingdings and friends have no letters at all
    return cp < 0x2000


def _tt(fd) -> object | None:
    try:
        from fontTools.ttLib import TTFont
        ff = fd.get("/FontFile2") if isinstance(fd, Dictionary) else None
        if not isinstance(ff, pikepdf.Stream):
            return None
        return TTFont(io.BytesIO(ff.read_bytes()), lazy=True, fontNumber=0)
    except Exception:
        return None


def _gid_codes(tt) -> dict[int, int]:
    """glyph id → symbol code, from the font's own (3,0) symbol cmap (or a
    Unicode cmap holding U+F0xx / Latin-1 codes)."""
    out: dict[int, int] = {}
    if tt is None:
        return out
    try:
        order = tt.getGlyphOrder()
        gid = {n: i for i, n in enumerate(order)}
        for rec in tt["cmap"].tables:
            if rec.platformID not in (3, 1, 0):
                continue
            for cp, gname in rec.cmap.items():
                code = cp - 0xF000 if 0xF020 <= cp <= 0xF0FF else (cp if cp <= 0xFF else None)
                g = gid.get(gname)
                if code is not None and g is not None:
                    out.setdefault(g, code)
    except Exception:
        return {}
    return out


def _cmap_stream(pairs: dict[int, str], two_byte: bool) -> bytes:
    width = 4 if two_byte else 2
    lines = ["/CIDInit /ProcSet findresource begin", "12 dict begin", "begincmap",
             "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
             "/CMapName /Adobe-Identity-UCS def", "/CMapType 2 def", "1 begincodespacerange",
             "<0000> <FFFF>" if two_byte else "<00> <FF>", "endcodespacerange"]
    items = sorted(pairs.items())
    for i in range(0, len(items), 100):
        chunk = items[i:i + 100]
        lines.append(f"{len(chunk)} beginbfchar")
        for code, text in chunk:
            lines.append(f"<{code:0{width}X}> <{text.encode('utf-16-be').hex().upper()}>")
        lines.append("endbfchar")
    lines += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    return "\n".join(lines).encode("ascii")


def fix_font(pdf: pikepdf.Pdf, f: Dictionary) -> int:
    """Give one symbol font the right ToUnicode. Returns how many codes changed."""
    fam = family(str(f.get("/BaseFont", "")))
    if not fam or fam == "webdings":
        return 0
    sub = f.get("/Subtype")
    old_stream = f.get("/ToUnicode")
    old: dict[int, str] = {}
    two = sub == Name.Type0
    if isinstance(old_stream, pikepdf.Stream):
        try:
            old, two_old = parse_cmap(old_stream.read_bytes())
            two = two or two_old
        except Exception:
            old = {}
    elif fam == "zapf" and sub != Name.Type0:
        return 0            # MuPDF and Acrobat know ZapfDingbats' own encoding
    new: dict[int, str] = {}
    if sub == Name.Type0:
        enc = f.get("/Encoding")
        if not (isinstance(enc, Name) and str(enc) in ("/Identity-H", "/Identity-V")):
            return 0
        df = (f.get("/DescendantFonts") or [None])[0]
        if not isinstance(df, Dictionary):
            return 0
        codes = _gid_codes(_tt(df.get("/FontDescriptor")))
        c2g = df.get("/CIDToGIDMap")
        cid_of: dict[int, int] = {}
        if isinstance(c2g, pikepdf.Stream):
            raw = c2g.read_bytes()
            for cid in range(len(raw) // 2):
                g = int.from_bytes(raw[2 * cid:2 * cid + 2], "big")
                if g in codes and g not in cid_of:
                    cid_of[g] = cid
        else:
            cid_of = {g: g for g in codes}
        for g, code in codes.items():
            ch = char_for(fam, code)
            if ch and g in cid_of:
                new[cid_of[g]] = ch
        for cid, t in old.items():          # a PUA entry names the code itself
            if cid not in new and t and 0xF020 <= ord(t[0]) <= 0xF0FF:
                ch = char_for(fam, ord(t[0]) - 0xF000)
                if ch:
                    new[cid] = ch
    else:
        first = int(f.get("/FirstChar", 0x20)) if f.get("/FirstChar") is not None else 0x20
        last = int(f.get("/LastChar", 0xFF)) if f.get("/LastChar") is not None else 0xFF
        diffs = {}
        enc = f.get("/Encoding")
        if isinstance(enc, Dictionary) and isinstance(enc.get("/Differences"), pikepdf.Array):
            code = 0
            for item in enc.Differences:
                if isinstance(item, Name):
                    diffs[code] = str(item)[1:]
                    code += 1
                else:
                    code = int(item)
        for code in range(max(0x20, first), min(0xFF, last) + 1):
            sym = code
            gname = diffs.get(code, "")
            m = re.fullmatch(r"(?:uni|u)F0([0-9A-Fa-f]{2})", gname)
            if m:
                sym = int(m.group(1), 16)
            elif gname and fam != "symbol" and not re.fullmatch(r"(?:g|glyph|c|a)?\d+|[A-Za-z]|.notdef", gname):
                continue        # a named glyph: the producer said what it is
            ch = char_for(fam, sym)
            if ch:
                new[code] = ch
    changed = {c: t for c, t in new.items() if _wrong(fam, c, old.get(c), t)}
    if not changed:
        return 0
    merged = dict(old)
    merged.update(changed)
    f.ToUnicode = pdf.make_stream(_cmap_stream(merged, two))
    return len(changed)


def repair(pdf: pikepdf.Pdf) -> tuple[int, list[str]]:
    """Every symbol font in the file: (fonts fixed, their names)."""
    from .preflight import iter_font_dicts
    fixed, names = 0, []
    for f in iter_font_dicts(pdf):
        try:
            if fix_font(pdf, f):
                fixed += 1
                names.append(str(f.get("/BaseFont", ""))[1:])
        except Exception:
            continue
    return fixed, sorted(set(names))
