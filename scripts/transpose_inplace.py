#!/usr/bin/env python3
"""Transpose a sheet-music PDF in place, preserving the original page exactly.

Only three things change: noteheads (and everything attached to them) move
vertically by the transposition's diatonic step count, the key signature is
redrawn, and chord symbols are rewritten. Staff lines, barlines, clefs, rests,
slash bars, repeats, rehearsal marks and every text instruction are untouched
because their bytes are never edited.

    transpose_inplace.py IN.pdf --from Dm --to Cm -o OUT.pdf
"""
import argparse, os, re, sys, math, collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pymupdf
from pdfsurgery import parse, edit, Y_SLOTS, X_SLOTS
from keysig import positions, clone
import chords as CH
from omr import staves, run, is_music_font, is_chord_font, norm_glyph

STEPS = "CDEFGAB"
SEMI = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
SIG = {"C": 0, "G": 1, "D": 2, "A": 3, "E": 4, "B": 5, "F#": 6, "C#": 7,
       "F": -1, "Bb": -2, "Eb": -3, "Ab": -4, "Db": -5, "Gb": -6, "Cb": -7}
ORDER = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]

# Sibelius music fonts remap ASCII; these are the glyphs that ride with a note.
# Whole, half and quarter/eighth heads in both font encodings. Note that
# \u00fa is a REST in some variants - it sits at a fixed staff position, so
# including it would drag rests around with the notes.
NOTEHEADS = set("w\u0153\u02d9\u00cf")
ARTICULATIONS = set("^.>-_")                # accent, staccato, tenuto
# A natural has to ride with its note too. Leaving it behind strands it on
# the old staff position, and the note then takes whatever the new key
# signature says - which is how a B natural became an A flat, not an A.
ACCIDENTALS = {"b": -1, "#": 1, "n": 0,
               "\u266d": -1, "\u266f": 1, "\u266e": 0}


def parse_key(name):
    m = re.fullmatch(r"([A-G])([b#]?)(m|min|minor)?", name.strip())
    if not m:
        raise SystemExit(f"bad key {name!r}")
    root, acc, minor = m.group(1), m.group(2), bool(m.group(3))
    pc = (SEMI[root] + (1 if acc == "#" else -1 if acc == "b" else 0)) % 12
    maj = ORDER[(pc + 3) % 12] if minor else ORDER[pc]
    return pc, minor, SIG.get(maj, 0), root + acc


def diatonic_shift(src, dst):
    """Steps to move on the staff (negative = down) and semitone delta."""
    s_pc, _, s_sig, s_root = parse_key(src)
    d_pc, _, d_sig, d_root = parse_key(dst)
    semis = (d_pc - s_pc) % 12
    if semis > 6:
        semis -= 12
    steps = (STEPS.index(d_root[0]) - STEPS.index(s_root[0])) % 7
    if steps > 3:
        steps -= 7
    return steps, semis, s_sig, d_sig


def detect_source_key(pdf):
    """Read the key signature by counting the accidentals in a staff header.

    Only the first system of a page prints a clef and key signature; later
    systems start straight into the music. So look for a staff that actually
    has a clef, and count the accidentals between it and the first note.
    """
    doc = pymupdf.open(pdf)
    for pno in range(doc.page_count):
        page = doc[pno]
        for s in run(pdf) if pno == 0 else []:
            pass
        sysl = [x for x in run(pdf) if x["page"] == pno + 1]
        for st_info in sysl:
            st = st_info["st"]
            y0, y1 = st[0][0], st[4][0]
            mid = (y0 + y1) / 2
            clef_x = None
            acc = []
            for b in page.get_text("rawdict")["blocks"]:
                for l in b.get("lines", []):
                    for sp in l["spans"]:
                        if not is_music_font(sp["font"]) or is_chord_font(sp["font"]):
                            continue
                        for c in sp["chars"]:
                            if abs(c["origin"][1] - mid) > 26:
                                continue
                            g = norm_glyph(c["c"])
                            x = c["origin"][0]
                            if g in ("&", "?") and (clef_x is None or x < clef_x):
                                clef_x = x
                            elif g in ("b", "#"):
                                acc.append((x, g))
            if clef_x is None:
                continue                      # continuation system: no header
            # accidentals in the header sit just right of the clef
            hdr = [g for x, g in acc if clef_x < x < clef_x + 90]
            if not hdr:
                return 0
            return -len(hdr) if hdr[0] == "b" else len(hdr)
    return None                               # no header found anywhere


def source_key_signature(pdf):
    """Best available reading of the source key signature."""
    sig = detect_source_key(pdf)
    return key_from_chords(pdf) if sig is None else sig


def key_from_chords(pdf):
    """Infer the key signature from the chord symbols.

    Some exports draw the clef as vector paths and print no key signature at
    all, so there is no header to measure. The chord roots still pin the key
    down: score each of the 12 signatures by how many chords are diatonic to it.
    """
    import collections
    doc = pymupdf.open(pdf)
    roots = []
    for page in doc:
        for b in page.get_text("rawdict")["blocks"]:
            for l in b.get("lines", []):
                for sp in l["spans"]:
                    if not is_chord_font(sp["font"]):
                        continue
                    txt = "".join(c["c"] for c in sp["chars"]).strip()
                    # Only the ROOT names the key. Matching every letter would
                    # count the bass of a slash chord and the B of a passing
                    # diminished, which is enough to flip the answer.
                    m = re.match(r"^([A-G])([\u00a8\u00ab#b]?)", txt)
                    if not m:
                        continue
                    acc = m.group(2)
                    alt = -1 if acc in ("\u00a8", "b") else 1 if acc in ("\u00ab", "#") else 0
                    dim = "\u00ba" in txt or "dim" in txt   # passing chord, weak evidence
                    roots.append(((SEMI[m.group(1)] + alt) % 12, 0.25 if dim else 1.0))
    if not roots:
        return 0
    cnt = collections.Counter()
    for pc, w in roots:
        cnt[pc] += w
    best, best_score = 0, -1e9
    for sig in range(-7, 8):
        tonic = (7 * sig) % 12                       # major tonic for this signature
        scale = {(tonic + i) % 12 for i in (0, 2, 4, 5, 7, 9, 11)}
        # Reward diatonic roots, but penalise non-diatonic ones. Counting only
        # matches lets a superset signature always win: every chord of F major
        # is also in C major, so C would tie or beat F on raw hits alone. The
        # Bb that forces F major has to cost C major something.
        inside = sum(n for pc, n in cnt.items() if pc in scale)
        outside = sum(n for pc, n in cnt.items() if pc not in scale)
        score = inside - 2.0 * outside - abs(sig) * 0.1
        if score > best_score:
            best, best_score = sig, score
    return best

def staff_geom(page):
    sts = staves(page)
    out = []
    for st in sts:
        top = st[0][0]
        half = (st[4][0] - top) / 8.0
        out.append({"lines": [l[0] for l in st], "top": top, "half": half,
                    "x0": st[0][1], "x1": st[0][2],
                    "bot": st[4][0]})
    return out


def glyph_map(page):
    """(x, y) -> (char, font) for every drawn glyph, in page coordinates."""
    g = {}
    for b in page.get_text("rawdict")["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                for c in s["chars"]:
                    if c["c"] == " ":
                        continue
                    g[(round(c["origin"][0], 1), round(c["origin"][1], 1))] = (
                        c["c"], s["font"].split("+")[-1], c["bbox"])
    return g


def first_note_x(page, gm, geom):
    """Left edge of real music on each staff (past clef + key signature)."""
    out = []
    for st in geom:
        xs = [x for (x, y), (ch, fn, bb) in gm.items()
              if norm_glyph(ch) in NOTEHEADS
              and abs(y - (st["top"] + st["bot"]) / 2) < 40]
        out.append(min(xs) if xs else st["x0"] + 60)
    return out


def classify_glyph(ch, font, x, y, geom, notex):
    """True if this glyph rides with the notes."""
    # Use the shared font test: exports vary between "Inkpen2Std" and plain
    # "Inkpen2", and hardcoding the former silently skipped every note.
    if not is_music_font(font) or is_chord_font(font):
        return False
    if "Script" in font or "Text" in font:
        return False
    ch = norm_glyph(ch)
    if ch in NOTEHEADS:
        # A notehead sits on the staff grid. Judge it against the NEAREST staff:
        # on a grand staff an adjacent one is often within range too, and
        # answering for the wrong staff strands legitimate ledger notes.
        if not geom:
            return False
        st = min(geom, key=lambda s: abs(y - (s["top"] + s["bot"]) / 2))
        pos = (y - st["top"]) / st["half"]
        return -9 <= pos <= 17 and abs(pos - round(pos)) <= 0.3
    if ch in ARTICULATIONS:
        return True
    if ch in ACCIDENTALS:
        # A key-signature accidental sits far left of the first note; one that
        # belongs to a note hugs it. Only the latter rides with the music.
        if not geom:
            return False
        i = min(range(len(geom)),
                key=lambda k: abs(y - (geom[k]["top"] + geom[k]["bot"]) / 2))
        return x >= notex[i] - 12
    return False


def keysig_edits(data, els, gm, geom, notex, H, dst_sig, half):
    """Add or remove key-signature accidentals so the staff shows `dst_sig`."""
    ins, blanks, warn = [], {}, []
    spacing = 2.258 * half                      # authentic Sibelius spacing
    for st, nx in zip(geom, notex):
        mid = (st["top"] + st["bot"]) / 2
        acc = []
        for e in els:
            if e.kind != "text" or len(e.glyphs) != 1:
                continue
            x, y = e.glyphs[0][0], H - e.glyphs[0][1]
            ch, fn = gm.get((round(x, 1), round(y, 1)), ("?", "?", None))[:2]
            ch = norm_glyph(ch)
            if ch not in ACCIDENTALS or "Chords" in fn:
                continue
            # A key-signature accidental sits in the staff header, immediately
            # after the clef - not merely anywhere left of the first note.
            if abs(y - mid) < 28 and x < nx - 12 and x < st["x0"] + 95:
                acc.append((x, y, e))
        if not acc:
            # Nothing to clone from and nothing to remove: this staff prints no
            # key signature (common on continuation systems and on charts that
            # rely on chord symbols alone). Leave it be.
            continue
        acc.sort()
        idx0 = round((acc[0][1] - st["top"]) / half)
        bass = idx0 >= 6                        # first flat: treble 4, bass 6
        want = positions(dst_sig, bass)
        if len(acc) > 1:
            spacing = acc[1][0] - acc[0][0]
        if len(want) < len(acc):                # fewer accidentals: erase extras
            for _, _, e in acc[len(want):]:
                blanks[(e.qstart, e.qend)] = b" "
            continue
        lx, ly, le = acc[-1]
        lidx = round((ly - st["top"]) / half)
        for i in range(len(acc), len(want)):
            dx = (i - (len(acc) - 1)) * spacing
            dy_page = (want[i] - lidx) * half
            ins.append((le.qstart, clone(data, le, dx, -dy_page)))
        # A wider key signature needs room. Record how far the rest of the
        # header (time signature, repeat sign) must move right to clear it.
        endx = lx + (len(want) - len(acc)) * spacing + 5.3
        hdr = [(gx, bb[2]) for (gx, gy), (c, f, bb) in gm.items()
               if abs(gy - mid) < 28 and lx + 1 < gx < nx - 0.5]
        nextx = min([g[0] for g in hdr], default=nx)
        # How far the header can move before it would collide with the first
        # note. Without this cap the repeat dots get pushed on top of the music.
        right = max([g[1] for g in hdr], default=lx)
        cap = max(0.0, nx - right - 1.0)
        if endx + 1.0 > nextx:
            warn.append((endx + 1.0 - nextx, lx, nx, mid, cap))
    return ins, blanks, warn


def header_shift(els, pops, gm, geom, notex, H, zones):
    """Move header content (time signature, repeat sign) right to clear the
    wider key signature.

    Each system negotiates its own shift, capped so nothing is ever pushed on
    top of the first note. Only the strip between the key signature and that
    note moves, so note positions - and the whole layout - stay put.
    """
    edits = {}

    def zone_dx(x, y):
        """Shift for a point, or None if it is not header content."""
        hits = [d for lx, nx, mid, d in zones
                if abs(y - mid) < 34 and lx + 1 < x < nx - 0.5]
        return min(hits) if hits else None

    for e in els:
        if e.kind != "text" or not e.glyphs:
            continue
        ds = [zone_dx(x, H - y) for x, y, _, _ in e.glyphs]
        if any(d is None for d in ds):
            continue
        dx = min(ds)
        d = e.ctm0[0]
        if dx > 0 and abs(d) > 1e-9:
            k = (e.inject_at, e.inject_at)
            edits[k] = edits.get(k, b"") + b" 1 0 0 1 %.5f 0 cm " % (dx / d)
    subs = collections.defaultdict(list)
    for o in pops:
        subs[o.sub].append(o)
    for ops in subs.values():
        pts = [p for o in ops for p in o.pts]
        ds = [zone_dx(px, H - py) for px, py in pts]
        if any(d is None for d in ds):
            continue
        dx = min(ds)
        if dx <= 0:
            continue
        for o in ops:
            d = o.ctm[0]
            if abs(d) < 1e-9:
                continue
            for si in X_SLOTS.get(o.op, ()):
                if si < len(o.slots):
                    val, s0, s1 = o.slots[si]
                    edits[(s0, s1)] = b"%.5f" % (val + dx / d)
    return edits


def subpath_kind(pts, painted, lw, geom, H):
    """staffline | barline | stem | beam | ledger | curve"""
    xs = [p[0] for p in pts]
    ys = [H - p[1] for p in pts]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    ymid = (max(ys) + min(ys)) / 2
    if painted in ("f", "f*", "F", "B", "B*"):
        return "beam"
    if h < 1.2:                                   # horizontal
        for st in geom:
            for ln in st["lines"]:
                if abs(ymid - ln) < 0.8 and w > 60:
                    return "staffline"
        return "ledger" if w < 40 else "beam"
    if w < 1.2:                                   # vertical
        for st in geom:
            if min(ys) <= st["top"] + 1 and max(ys) >= st["bot"] - 1:
                return "barline"
        return "stem"
    return "curve"


TD_RE = re.compile(rb"([-\d.]+)\s+([-\d.]+)\s+Td")


def chord_edits(data, els, gm, H, steps, semis, flats):
    """Retypeset every chord symbol in the transposed key."""
    runs, items = [], []
    for e in els:
        if e.kind != "text" or not e.glyphs or e.bt is None:
            continue
        # A single Tj can show several characters, so trust the decoded run
        # text when the parser has it and only fall back to per-glyph lookups.
        first = gm.get((round(e.glyphs[0][0], 1), round(H - e.glyphs[0][1], 1)))
        if e.text:
            if not first or "Chords" not in first[1]:
                continue
            chars = list(e.text)
        else:
            chars, ok = [], True
            for x, y, f, cid in e.glyphs:
                g = gm.get((round(x, 1), round(H - y, 1)))
                if g is None or "Chords" not in g[1]:
                    ok = False
                    break
                chars.append(g[0])
            if not ok or not chars:
                continue
        if not chars:
            continue
        bt = data[e.bt:e.et]
        tds = [float(m.group(1)) for m in TD_RE.finditer(bt)]
        # each Td after the first is the advance of the glyph before it
        deltas = (tds[1:] + [None])[:len(chars)]
        runs.append((chars, [g[3] for g in e.glyphs], deltas))
        items.append((e, "".join(chars), first[1]))

    # Some exports emit one BT block per character, so "Bb" arrives as two
    # separate elements. Merge neighbours on the same line into one symbol,
    # otherwise only the bare root is seen and the accidental is lost.
    items.sort(key=lambda it: (round(H - it[0].glyphs[0][1], 1),
                               it[0].glyphs[0][0]))
    merged, i = [], 0
    while i < len(items):
        e, txt, fn = items[i]
        grp = [items[i]]
        j = i + 1
        while j < len(items):
            pe, ptxt, _ = grp[-1]
            ne = items[j][0]
            if abs((H - ne.glyphs[0][1]) - (H - pe.glyphs[0][1])) > 1.5:
                break
            gap = ne.glyphs[0][0] - pe.glyphs[-1][0]
            if not (0 <= gap < 11):
                break
            grp.append(items[j])
            j += 1
        merged.append((grp[0][0], "".join(g[1] for g in grp), grp[0][2],
                       [g[0] for g in grp]))
        i = j
    items = merged
    if not items:                       # chart carries no chord symbols
        return {}, 0, [], []
    cid, width = CH.learn(runs)
    fallback = sorted(width.values())[len(width) // 2] if width else 0
    # horizontal room before the next chord symbol on the same line
    pos = sorted((round(H - e.glyphs[0][1], 1), e.glyphs[0][0], i)
                 for i, (e, _t, _f, _g) in enumerate(items))
    room = {}
    for k, (y, x, i) in enumerate(pos):
        nxt = next((px for py, px, _ in pos[k + 1:] if abs(py - y) < 2), None)
        room[i] = (nxt - x - 1.5) if nxt else 1e9
    edits, n, missed, redraw = {}, 0, [], []
    for i, (e, text, fontname, group) in enumerate(items):
        new = "".join(CH.transpose_glyphs(list(text), steps, semis))
        if new == text:
            continue
        sq = 1.0
        sc = abs(e.ctm[0]) or 1.0
        nat = CH.natural_width(new, width, fallback)
        avail = room.get(i, 1e9) / sc
        if nat > avail > 0:
            sq = max(0.78, avail / nat)
        rep = CH.rebuild(data[e.bt:e.et], new, cid, width, fallback, sq)
        if rep is not None:
            edits[(e.bt, e.et)] = rep
            for extra in group[1:]:          # symbol was split across blocks
                edits[(extra.bt, extra.et)] = b" "
            n += 1
        else:
            # The embedded font is subsetted and lacks a letter we now need.
            # Blank the old symbol and redraw it with the full installed face.
            full = CH.find_full_font(fontname)
            if full:
                # Font size may come from Tf, or from the Tm scale when Tf is
                # "1" - measure the drawn glyph instead of trusting either.
                bb = gm.get((round(e.glyphs[0][0], 1),
                             round(H - e.glyphs[0][1], 1)))
                size = (bb[2][3] - bb[2][1]) * 1.0 if bb and bb[2] else 10.0
                redraw.append({"x": e.glyphs[0][0],
                               "y": H - e.glyphs[0][1],
                               "text": new,
                               "size": max(4.0, size),
                               "font": full})
                edits[(e.bt, e.et)] = b" "
                for extra in group[1:]:      # symbol split across blocks
                    edits[(extra.bt, extra.et)] = b" "
                n += 1
            else:
                missed.append("".join(new))
    return edits, n, sorted(set(missed)), redraw


def ledger_edits(subs, gm, geom, H, steps, half):
    """Recompute ledger lines instead of translating them.

    Ledger lines only exist at line positions (even staff indices outside the
    staff), so moving them by one diatonic step would land them in a space.
    Each note's stack is rebuilt from the note's new position and any surplus
    line is collapsed to zero length.
    """
    edits = {}
    notes = [(x, y) for (x, y), (ch, fn, bb) in gm.items()
             if norm_glyph(ch) in NOTEHEADS and "Chords" not in fn]
    for ops in subs.values():
        pts = [p for o in ops for p in o.pts]
        xs = [p[0] for p in pts]
        ys = [H - p[1] for p in pts]
        if max(ys) - min(ys) > 1.2 or max(xs) - min(xs) > 40:
            continue
        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
        st = min(geom, key=lambda g: abs((g["top"] + g["bot"]) / 2 - cy))
        idx = round((cy - st["top"]) / half)
        if 0 <= idx <= 8:
            continue                      # inside the staff: not a ledger
        near = [n for n in notes if abs(n[0] - cx) < 9
                and abs(n[1] - (st["top"] + st["bot"]) / 2) < 60]
        if not near:
            continue
        # after the shift, which line slots does this note still need?
        tgt = []
        for _, ny in near:
            nidx = round((ny - st["top"]) / half) - steps
            if nidx <= -2:
                tgt += list(range(-2, nidx - 1, -2))
            if nidx >= 10:
                tgt += list(range(10, nidx + 1, 2))
        tgt = sorted(set(tgt), key=abs)
        want = [t for t in tgt if (t < 0) == (idx < 0)]
        # keep this line only if its rank still exists in the new stack
        rank = abs(idx) // 2 - (1 if idx < 0 else 5)
        rank = max(0, (abs(idx + 2) // 2) if idx < 0 else (idx - 10) // 2)
        newidx = want[rank] if rank < len(want) else None
        anchor = ops[0].slots[0][0]       # x of this subpath's 'm'
        for o in ops:
            d = o.ctm[3]
            if abs(d) < 1e-9:
                continue
            if newidx is None:
                # No longer needed: collapse every point onto the start x so the
                # butt-capped stroke has zero length and paints nothing.
                for si in X_SLOTS.get(o.op, ()):
                    if si < len(o.slots):
                        val, s0, s1 = o.slots[si]
                        edits[(s0, s1)] = b"%.5f" % anchor
                continue
            dy_page = (newidx - idx) * half
            for si in Y_SLOTS.get(o.op, ()):
                if si < len(o.slots):
                    val, s0, s1 = o.slots[si]
                    edits[(s0, s1)] = b"%.5f" % (val + (-dy_page) / d)
    return edits


def resource_fonts(page):
    """Map a content-stream resource name to its base font name."""
    return {f[4]: f[3].split("+")[-1] for f in page.get_fonts(full=True)}


def simple_font_resources(page):
    """Resource names whose fonts use one byte per glyph."""
    out = set()
    for f in page.get_fonts(full=True):
        if f[2] != "Type0":
            out.add(f[4])
    return out



def draw_keysig(page, geom, notex, src_sig, dst_sig, half, gm, pops=()):
    """Draw the accidentals a staff needs when its key signature is vector art.

    Some exports draw the clef and key signature as paths rather than glyphs,
    so there is nothing to clone or erase. The signature still has to change,
    so the extra accidentals are drawn with the installed music font.
    """
    if src_sig == dst_sig or (src_sig < 0) != (dst_sig < 0) and src_sig and dst_sig:
        return []
    if abs(dst_sig) <= abs(src_sig):
        return []                       # removing vector accidentals is not possible
    font = None
    for f in page.get_fonts(full=True):
        if is_music_font(f[3]) and not is_chord_font(f[3]) \
           and "Script" not in f[3] and "Text" not in f[3] \
           and "Special" not in f[3]:
            font = CH.find_full_font(f[3])
            if font:
                break
    if not font:
        return []
    glyph = "b" if dst_sig < 0 else "#"
    out = []
    for st, nx in zip(geom, notex):
        mid = (st["top"] + st["bot"]) / 2
        bass = False                    # vector clefs: assume treble
        want = positions(dst_sig, bass)
        add = want[abs(src_sig):]       # the ones not already printed
        if not add:
            continue
        spacing = 2.258 * half
        size = half * 8.0               # one staff height, as the font expects
        # The clef and existing signature are vector art, so find where that
        # art ends and start just after it - measuring from the first note
        # instead gives a different answer on every system.
        hx = st["x0"] + 12
        for o in pops:
            for px, py in o.pts:
                ppy = page.rect.height - py
                if abs(ppy - mid) < 22 and st["x0"] + 2 < px < nx - 2:
                    hx = max(hx, px)
        x0 = hx + spacing * 0.55
        if x0 + spacing * (len(add) - 1) > nx - 3.5:
            x0 = max(st["x0"] + 12, nx - 3.5 - spacing * (len(add) - 1))
        for k, idx in enumerate(add):
            out.append({"x": x0 + k * spacing,
                        "y": st["top"] + idx * half,
                        "text": glyph, "size": size, "font": font,
                        "keysig": True})
    return out

def transpose_page(page, steps, src_sig, dst_sig, semis=0, verbose=False):
    H = page.rect.height
    data = page.read_contents()
    els, pops = parse(data, simple_fonts=simple_font_resources(page))
    geom = staff_geom(page)
    if not geom:
        return None, {}, []
    gm = glyph_map(page)
    resfonts = resource_fonts(page)
    notex = first_note_x(page, gm, geom)
    half = geom[0]["half"]
    dy_page = -steps * half          # steps<0 (down) -> positive page dy
    dy_user = -dy_page               # PDF user space has y up

    edits = {}
    stats = collections.Counter()

    # ---- glyphs: shift whole q-blocks that draw a moving glyph
    for e in els:
        if e.kind != "text" or not e.glyphs:
            continue
        # Text extraction silently omits some glyphs (overprinted noteheads,
        # for one). Falling back to the run text the parser decoded keeps those
        # notes in the transposition instead of stranding them at the old pitch.
        move = True
        for gi, (x, y, gf, cid) in enumerate(e.glyphs):
            g = gm.get((round(x, 1), round(H - y, 1)))
            if g is not None:
                ch, fn = g[0], g[1]
            elif e.text and gi < len(e.text):
                ch = e.text[gi]
                fn = resfonts.get((gf or "").lstrip("/"), "")
            else:
                move = False
                break
            if not classify_glyph(ch, fn, x, H - y, geom, notex):
                move = False
                break
        if not move:
            continue
        d = e.ctm0[3]
        if abs(d) < 1e-9:
            continue
        if e.standalone and e.tm_slots and len(e.tm_slots) >= 6:
            # Text drawn outside q...Q: move it by rewriting its own text
            # matrix. Wrapping it in q/Q instead would disturb the graphics
            # state and can un-hide glyphs the original had clipped away.
            val, s0, s1 = e.tm_slots[5]
            edits[(s0, s1)] = b"%.5f" % (val + dy_user / d)
        else:
            edits[(e.inject_at, e.inject_at)] = (
                b" 1 0 0 1 0 %.5f cm " % (dy_user / d))
        stats["glyph"] += len(e.glyphs)

    # ---- paths: rewrite the y operand of moving subpaths
    subs = collections.defaultdict(list)
    for o in pops:
        subs[o.sub].append(o)
    for sid, ops in subs.items():
        pts = [p for o in ops for p in o.pts]
        painted = ops[-1].painted
        kind = subpath_kind(pts, painted, ops[0].lw, geom, H)
        stats["path_" + kind] += 1
        if kind in ("staffline", "barline", "ledger"):
            continue
        for o in ops:
            d = o.ctm[3]
            if abs(d) < 1e-9:
                continue
            for si in Y_SLOTS.get(o.op, ()):
                if si >= len(o.slots):
                    continue
                val, s0, s1 = o.slots[si]
                edits[(s0, s1)] = b"%.5f" % (val + dy_user / d)
    edits.update(ledger_edits(subs, gm, geom, H, steps, half))

    ce, ncho, miss, redraw = chord_edits(data, els, gm, H, steps, semis,
                                         dst_sig <= 0)
    edits.update(ce)
    stats["chords"] = ncho
    if miss:
        stats["chords_unavailable"] = ",".join(miss)

    if src_sig != dst_sig:
        ins, blanks, warn = keysig_edits(data, els, gm, geom, notex, H,
                                         dst_sig, half)
        edits.update(blanks)
        for off, payload in ins:
            key = (off, off)
            edits[key] = edits.get(key, b"") + payload
            stats["keysig_added"] += 1
        stats["keysig_removed"] += len(blanks)
        if not ins and not blanks:
            vk = draw_keysig(page, geom, notex, src_sig, dst_sig, half, gm, pops)
            redraw.extend(vk)
            if vk:
                stats["keysig_drawn"] = len(vk)
        if warn:
            # one shift for the whole page keeps systems aligned with each other
            zones = [(w[1], w[2], w[3], min(w[0], w[4])) for w in warn]
            dx = max(z[3] for z in zones)
            hs = header_shift(els, pops, gm, geom, notex, H, zones)
            for k, v in hs.items():
                edits[k] = edits.get(k, b"") + v if k[0] == k[1] else v
            stats["header_shift"] = round(dx, 2)

    if verbose:
        print("   ", dict(stats))
    return edit(data, edits), stats, redraw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--from", dest="src", required=True)
    ap.add_argument("--to", dest="dst", required=True)
    ap.add_argument("-o", "--out")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    steps, semis, s_sig, d_sig = diatonic_shift(a.src, a.dst)
    out = a.out or re.sub(r"\.pdf$", "", a.pdf) + f"-{a.dst}.pdf"
    print(f"{a.src} -> {a.dst}: {semis:+d} semitones = {steps:+d} diatonic steps"
          f"   key sig {s_sig} -> {d_sig}")

    doc = pymupdf.open(a.pdf)
    for i, page in enumerate(doc):
        new, st, redraw = transpose_page(page, steps, s_sig, d_sig, semis,
                                         a.verbose)
        if new is None:
            print(f"  page {i+1}: no staves found, left unchanged")
            continue
        # Replace the page's content stream. read_contents() concatenates all
        # streams, so the rewritten result goes into the first and the rest are
        # emptied to avoid drawing anything twice.
        xrefs = page.get_contents()
        doc.update_stream(xrefs[0], new)
        for extra in xrefs[1:]:
            doc.update_stream(extra, b" ")
        for r in redraw:
            page.insert_text((r["x"], r["y"]), r["text"], fontsize=r["size"],
                             fontfile=r["font"],
                             fontname="cf" + str(abs(hash(r["font"])) % 9999))
        print(f"  page {i+1}: moved {st['glyph']} glyphs, "
              f"{sum(v for k, v in st.items() if k.startswith('path_') and k not in ('path_staffline','path_barline'))} subpaths")
    doc.save(out, garbage=0, deflate=True, clean=False)
    print("->", out)


if __name__ == "__main__":
    main()
