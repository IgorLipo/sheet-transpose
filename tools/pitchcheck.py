"""Compare the sounding pitch of every note before and after a transposition.

Notes are paired by horizontal position, which a vertical transposition never
changes. Pairing by list index instead desynchronises the moment the two files
disagree on note count, which makes every later note look wrong.
"""
import sys, os, collections
sys.path.insert(0, os.path.expanduser(
    "~/Projects/il_projects/sheet-transpose/sheet_transpose"))
import pymupdf
from omr import staves, norm_glyph, is_music_font, is_chord_font, NOTEHEADS
from pdfsurgery import parse
import transpose_inplace as TI

STEPS = "CDEFGAB"
SEMI = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
FLAT_ORDER = ["B", "E", "A", "D", "G", "C", "F"]
SHARP_ORDER = ["F", "C", "G", "D", "A", "E", "B"]
ACCID = {"b": -1, "#": 1, "n": 0}


def key_alters(sig):
    if sig < 0:
        return {s: -1 for s in FLAT_ORDER[:abs(sig)]}
    return {s: 1 for s in SHARP_ORDER[:sig]}


def read_notes(path, sig, base=38):
    """base is the diatonic number of the top staff line: 38 = F5 (treble)."""
    alters = key_alters(sig)
    doc = pymupdf.open(path)
    out = []
    for pno, page in enumerate(doc):
        sts = sorted(staves(page), key=lambda t: t[0][0])
        glyphs = []
        seen = set()
        for b in page.get_text("rawdict")["blocks"]:
            for l in b.get("lines", []):
                for sp in l["spans"]:
                    if not is_music_font(sp["font"]) or is_chord_font(sp["font"]):
                        continue
                    for c in sp["chars"]:
                        if c["c"] != " ":
                            glyphs.append((c["origin"][0], c["origin"][1],
                                           norm_glyph(c["c"])))
                            seen.add((round(c["origin"][0], 1),
                                      round(c["origin"][1], 1)))
        # Text extraction drops some glyphs entirely, so read the content
        # stream too - otherwise a note the transposer handled correctly looks
        # like it appeared from nowhere.
        H = page.rect.height
        resf = TI.resource_fonts(page)
        els, _ = parse(page.read_contents(),
                       simple_fonts=TI.simple_font_resources(page))
        for e in els:
            if e.kind != "text" or not e.text:
                continue
            for gi, (gx, gy, gf, cid) in enumerate(e.glyphs):
                if gi >= len(e.text):
                    break
                py = H - gy
                if (round(gx, 1), round(py, 1)) in seen:
                    continue
                fn = resf.get((gf or "").lstrip("/"), "")
                if not is_music_font(fn) or is_chord_font(fn):
                    continue
                glyphs.append((gx, py, norm_glyph(e.text[gi])))
        # Assign every glyph to its NEAREST staff exactly once. Scanning a
        # band per staff counts anything between two staves twice, which shows
        # up as phantom extra notes.
        owner = {}
        for gi, g in enumerate(glyphs):
            si = min(range(len(sts)),
                     key=lambda k: abs(g[1] - (sts[k][0][0] + sts[k][4][0]) / 2))
            owner.setdefault(si, []).append(g)
        for si, st in enumerate(sts):
            top, bot = st[0][0], st[4][0]
            half = (bot - top) / 8.0
            mid = (top + bot) / 2
            band = sorted(owner.get(si, []))
            # a grand staff mixes clefs, so read each staff's own
            sbase = base
            for gx, gy, gc in band:
                if gx < st[0][1] + 30 and gc in ("&", "?"):
                    sbase = 38 if gc == "&" else 26
                    break
            accs = [(x, y, ch) for x, y, ch in band
                    if ch in ACCID and x > st[0][1] + 42]
            for x, y, ch in band:
                if ch not in NOTEHEADS:
                    continue
                idx = round((y - top) / half)
                if not -8 <= idx <= 16:
                    continue
                dia = sbase - idx
                step, octv = STEPS[dia % 7], dia // 7
                alt = alters.get(step, 0)
                for ax, ay, ac in accs:
                    if 0 < x - ax < 16 and abs(ay - y) < half * 0.8:
                        alt = ACCID[ac]
                        break
                out.append({"page": pno, "sys": si, "x": round(x, 2),
                            "y": round(y, 2), "idx": idx,
                            "name": step + ("b" * -alt if alt < 0 else "#" * alt) + str(octv),
                            "midi": (octv + 1) * 12 + SEMI[step] + alt})
    return out


def midi_name(m):
    n = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
    return n[m % 12] + str(m // 12 - 1)


def compare(src, dst, src_sig, dst_sig, semis, verbose=True):
    a = read_notes(src, src_sig)
    b = read_notes(dst, dst_sig)
    pool = collections.defaultdict(list)
    for n in b:
        pool[(n["page"], n["x"])].append(n)
    bad, unmatched = [], []
    for o in a:
        cand = pool.get((o["page"], o["x"]), [])
        if not cand:
            unmatched.append(o)
            continue
        # the partner is the one nearest the expected new staff position
        g = min(cand, key=lambda n: abs(n["midi"] - (o["midi"] + semis)))
        cand.remove(g)
        if g["midi"] != o["midi"] + semis:
            bad.append((o, g, o["midi"] + semis))
    extra = [n for v in pool.values() for n in v]
    if verbose:
        print("original notes: %d    transposed notes: %d" % (len(a), len(b)))
        print("WRONG PITCH : %d" % len(bad))
        print("UNMATCHED   : %d (original notes with no note at that x)" % len(unmatched))
        print("EXTRA       : %d (notes in the output with no original)" % len(extra))
        if bad:
            print()
            print("  page sys x        original   got      expected")
            for o, g, want in bad[:40]:
                print("  %-4d %-3d %-8.1f %-10s %-8s %s"
                      % (o["page"], o["sys"], o["x"], o["name"], g["name"],
                         midi_name(want)))
        if extra:
            print()
            print("  extra notes:", [(n["x"], n["y"], n["name"]) for n in extra[:8]])
    return bad, unmatched, extra


if __name__ == "__main__":
    compare(sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]),
            int(sys.argv[5]))
