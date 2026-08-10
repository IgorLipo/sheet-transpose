"""Report collisions between the key signature and the rest of a staff header.

Widening a key signature is the one edit that needs horizontal room, so it is
the one edit that can push glyphs on top of each other. Everything else in a
transposition only moves vertically. This checks the result the way a reader
would: does any ink overlap ink that is not its own?
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pymupdf
from sheet_transpose.transpose_inplace import (
    staff_geom, glyph_map, merged_glyph_map, first_note_x, simple_font_resources,
    ACCIDENTALS, NOTEHEADS, norm_glyph)
from sheet_transpose.pdfsurgery import parse
from sheet_transpose.omr import is_music_font, is_chord_font

MIN_OVERLAP = 0.4          # points; less than this reads as touching, not colliding


def header_items(page, st, nx, gm, ksx):
    """Everything drawn in this staff's header, glyph or vector.

    `ksx` holds the x of each key-signature accidental, decided by the caller.
    Classifying them here by "left of the first note" would call a misplaced
    accidental something else and then never test it - the checker would agree
    with the bug it is supposed to catch.
    """
    lo, hi = st["top"] - 2 * st["half"], st["bot"] + 2 * st["half"]
    out = []
    for (x, y), (ch, fn, bb, *_r) in gm.items():
        if not (lo <= y <= hi) or x > nx + 24:
            continue
        if not is_music_font(fn) or is_chord_font(fn):
            continue
        kind = "keysig" if round(x, 1) in ksx else "other"
        out.append({"kind": kind, "ch": norm_glyph(ch), "x": x,
                    "bb": (bb[0], bb[1], bb[2], bb[3])})
    for d in page.get_drawings():
        if d["type"] == "s":                     # stroked lines: staff, barlines
            continue
        r = d["rect"]
        if r.x1 > nx + 1 or r.x0 < st["x0"] - 2 or r.width > 40:
            continue
        if not (lo <= (r.y0 + r.y1) / 2 <= hi):
            continue
        out.append({"kind": "other", "ch": "<fill>", "x": r.x0,
                    "bb": (r.x0, r.y0, r.x1, r.y1)})
    return sorted(out, key=lambda g: g["x"])


def overlap(a, b):
    dx = min(a[2], b[2]) - max(a[0], b[0])
    dy = min(a[3], b[3]) - max(a[1], b[1])
    return min(dx, dy) if dx > 0 and dy > 0 else 0.0


def keysig_xs(gm, st, nx):
    """x of every accidental this staff prints as a key signature.

    Taken as the accidentals left of the first note, PLUS any accidental that
    is not attached to a notehead - a stray one sitting over the time signature
    or out in the bar is exactly the failure this tool exists to find.
    """
    lo, hi = st["top"] - 2 * st["half"], st["bot"] + 2 * st["half"]
    heads = [(x, y) for (x, y), (ch, fn, bb, *_r) in gm.items()
             if lo <= y <= hi and norm_glyph(ch) in NOTEHEADS
             and is_music_font(fn) and not is_chord_font(fn)]
    out = set()
    for (x, y), (ch, fn, bb, *_r) in gm.items():
        if not (lo <= y <= hi) or norm_glyph(ch) not in ACCIDENTALS:
            continue
        if not is_music_font(fn) or is_chord_font(fn):
            continue
        # attached = a notehead just right of it, ON ITS OWN LINE. Ignoring
        # the vertical test calls an accidental in a chord "stray" and then
        # reports the neighbour it has always sat beside as a new collision.
        attached = any(0 < hx - x < 14 and abs(hy - y) < st["half"] * 1.2
                       for hx, hy in heads)
        if (x < nx - 6 and not attached) or not attached:
            out.add(round(x, 1))
    return out


def check(path, verbose=True):
    doc = pymupdf.open(path)
    hits = []
    for pno, page in enumerate(doc):
        geom = staff_geom(page)
        els, _ = parse(page.read_contents(),
                       simple_fonts=simple_font_resources(page))
        gm = merged_glyph_map(page, els, page.rect.height, glyph_map(page))
        notex = first_note_x(page, gm, geom)
        for si, (st, nx) in enumerate(zip(geom, notex)):
            ksx = keysig_xs(gm, st, nx)
            items = header_items(page, st, nx, gm, ksx)
            ks = [g for g in items if g["kind"] == "keysig"]
            for i, a in enumerate(ks):
                for b in items:
                    if b is a or (b["kind"] == "keysig"
                                  and ks.index(b) < i):
                        continue
                    # Two accidentals of a CHORD stack vertically at nearly
                    # the same x: their glyph boxes touch but the ink does
                    # not, and an engraver draws them exactly this way.
                    if b["kind"] == "keysig" \
                       and abs(a["x"] - b["x"]) < 6 \
                       and abs((a["bb"][1] + a["bb"][3])
                               - (b["bb"][1] + b["bb"][3])) / 2 \
                       > st["half"] * 1.2:
                        continue
                    ov = overlap(a["bb"], b["bb"])
                    if ov > MIN_OVERLAP:
                        hits.append((pno, si, a["ch"], round(a["x"], 1),
                                     b["ch"], round(b["x"], 1), round(ov, 2)))
    if verbose:
        print("%-28s collisions: %d" % (os.path.basename(path), len(hits)))
        for h in hits:
            print("    p%d sys%-2d %-3s x=%-7.1f overlaps %-8s x=%-7.1f by %.2fpt"
                  % h)
    return hits


if __name__ == "__main__":
    bad = 0
    for p in sys.argv[1:]:
        bad += len(check(p))
    sys.exit(1 if bad else 0)
