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

import pymupdf
try:                                    # package import (pip install)
    from .pdfsurgery import parse, edit, mat_mul, Y_SLOTS, X_SLOTS
    from .keysig import positions, clone
    from . import chords as CH
    from . import vector as VEC
    from .omr import (staves, run, is_music_font, is_chord_font, norm_glyph,
                      font_roles, set_font_roles)
except ImportError:                     # flat directory (the Air deployment)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from pdfsurgery import parse, edit, mat_mul, Y_SLOTS, X_SLOTS
    from keysig import positions, clone
    import chords as CH
    import vector as VEC
    from omr import (staves, run, is_music_font, is_chord_font, norm_glyph,
                     font_roles, set_font_roles)

STEPS = "CDEFGAB"
SEMI = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
SIG = {"C": 0, "G": 1, "D": 2, "A": 3, "E": 4, "B": 5, "F#": 6, "C#": 7,
       "F": -1, "Bb": -2, "Eb": -3, "Ab": -4, "Db": -5, "Gb": -6, "Cb": -7}
ORDER = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]

# Sibelius music fonts remap ASCII; these are the glyphs that ride with a note.
# Notehead glyphs across the Sibelius font variants and both encodings:
# filled, hollow (minim) and cross heads. A hollow head often repeats at the
# same staff position across phrases, so "sits at a fixed position" is NOT a
# safe test for a rest - check for an attached stem instead.
NOTEHEADS = set("w\u0153\u02d9\u00cf\u00fa\u00c0")
# Flags, dots and articulations all hang off a notehead and must ride
# with it, or they end up detached from the note they belong to.
ARTICULATIONS = set("^.>-_jJ\u2019\u201a")                # accent, staccato, tenuto
CLEFS = set("&?B")            # treble, bass, alto in the ASCII remap
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
    """Steps to move on the staff (negative = down) and semitone delta.

    The step count must go the same way as the pitch: six semitones up
    written as three letters DOWN leaves every note an octave out. Of the
    two candidate letter distances, take the one whose size best matches
    the semitone movement (a diatonic step averages two semitones).
    """
    s_pc, s_min, s_sig, s_root = parse_key(src)
    d_pc, d_min, d_sig, d_root = parse_key(dst)
    semis = (d_pc - s_pc) % 12
    if semis > 6:
        semis -= 12
    # Letters and semitones must describe the SAME interval, or every note
    # needs an extra accidental to make up the difference - Bb minor to
    # A minor read as "down one letter, down one semitone" turns each of the
    # five flats into a double flat. Both directions round the same letter
    # gap; take the one whose size matches the semitone move.
    up = (STEPS.index(d_root[0]) - STEPS.index(s_root[0])) % 7
    steps = min((up, up - 7), key=lambda k: (abs(2 * k - semis), abs(k)))
    # How far the spelling is off: the interval the letters describe versus
    # the interval actually played. Anything past a single accidental means
    # this key pair cannot be written as asked, so respell the destination
    # enharmonically - the same sounding key, spelled the way a player
    # expects (A major, not B double-flat major).
    def sig_of(root_pc, minor):
        maj = ORDER[(root_pc + 3) % 12] if minor else ORDER[root_pc]
        return SIG.get(maj, 0)

    letter_semis = (SEMI[STEPS[(STEPS.index(s_root[0]) + steps) % 7]]
                    - SEMI[s_root[0]])
    letter_semis -= 12 * round((letter_semis - semis) / 12.0)
    drift = semis - letter_semis
    if abs(d_sig) > 7 or abs(drift) > 1:
        for cand in (d_sig + 12, d_sig - 12):
            if abs(cand) <= 7:
                d_sig = cand
                break
    return steps, semis, s_sig, d_sig


def activate_roles(doc):
    """Install content-based font roles for this document."""
    set_font_roles(font_roles(doc))


def detect_source_key(pdf):
    """Read the key signature by counting the accidentals in a staff header.

    Only the first system of a page prints a clef and key signature; later
    systems start straight into the music. So look for a staff that actually
    has a clef, and count the accidentals between it and the first note.
    """
    doc = pymupdf.open(pdf)
    activate_roles(doc)
    for pno in range(doc.page_count):
        page = doc[pno]
        sts = staves(page)
        if not sts:
            continue
        # The low-level trace sees every glyph; the ordinary extractor drops
        # whole header runs on some exports and on this tool's own output.
        glyphs = []
        for sp in page.get_texttrace():
            if sp.get("type") != 0:
                continue
            if not is_music_font(sp["font"]) or is_chord_font(sp["font"]):
                continue
            for ucs, _g, org, _b in sp["chars"]:
                glyphs.append((org[0], org[1], norm_glyph(chr(ucs))))
        for st in sts:
            mid = (st[0][0] + st[4][0]) / 2
            clef_x = None
            acc = []
            for x, y, g in glyphs:
                if abs(y - mid) > 26:
                    continue
                if g in ("&", "?") and (clef_x is None or x < clef_x):
                    clef_x = x
                elif g in ("b", "#"):
                    acc.append((x, g))
            if clef_x is None:
                continue                      # continuation system: no header
            # accidentals in the header sit just right of the clef, and the
            # first inline accidental in the music must not count: stop at
            # the first gap wider than a signature ever leaves.
            hdr = sorted((x, g) for x, g in acc if clef_x < x < clef_x + 90)
            run_ = []
            for k, (x, g) in enumerate(hdr):
                if k and x - run_[-1][0] > 9:
                    break
                run_.append((x, g))
            if not run_:
                return 0
            return -len(run_) if run_[0][1] == "b" else len(run_)
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
    activate_roles(doc)
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
                    m = re.match(r"^([A-G])([\u00a8\u00a9\u00ac#b]?)", txt)
                    if not m:
                        continue
                    acc = m.group(2)
                    alt = (-1 if acc in ("\u00a8", "b", "\u00ac")
                           else 1 if acc in ("\u00a9", "#") else 0)
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
                        c["c"], s["font"].split("+")[-1], c["bbox"], s["size"])
    return g


def align_glyphs(page, els, H):
    """Snap every parsed glyph to the position the renderer actually drew.

    The parser cannot know glyph advances, so within a TJ array or a
    multi-character string it reports every glyph at the string's origin.
    The text extractor's low-level trace enumerates glyphs in the same paint
    order WITH their true origins, so when the counts agree, its positions are
    authoritative.
    """
    # every span type counts here: invisible text (render mode 3) is still a
    # show operator in the stream, and skipping it desynchronises the zip
    trace = [org for sp in page.get_texttrace() if sp.get("chars")
             for _u, _g, org, _b in sp["chars"]]
    slots = [(e, gi) for e in els if e.kind == "text"
             for gi in range(len(e.glyphs))]
    if len(trace) == len(slots):
        for (e, gi), org in zip(slots, trace):
            x, y, f, cid = e.glyphs[gi]
            e.glyphs[gi] = (org[0], H - org[1], f, cid)
        return True
    # The renderer split some glyph differently (a ligature reported as two
    # characters is enough) and the zip cannot be trusted. Fall back to the
    # embedded fonts' own advance widths: within each multi-glyph string,
    # every glyph after the first steps right by the previous glyph's width.
    fonts = {}
    for f in page.get_fonts(full=True):
        try:
            fonts["/" + f[4]] = pymupdf.Font(
                fontbuffer=page.parent.extract_font(f[0])[3])
        except Exception:
            pass
    for e in els:
        if e.kind != "text" or len(e.glyphs) < 2:
            continue
        runs_ = collections.defaultdict(list)
        for gi in range(len(e.glyphs)):
            if gi < len(e.gspans):
                runs_[e.gspans[gi]].append(gi)
        for gis in runs_.values():
            if len(gis) < 2:
                continue
            fo = fonts.get(e.glyphs[gis[0]][2] or "")
            if fo is None or gis[0] >= len(e.gmats):
                continue
            ctm_g, tm_g, size = e.gmats[gis[0]]
            m = mat_mul(tm_g, ctm_g)
            scale = math.hypot(m[0], m[1]) * size
            x = e.glyphs[gis[0]][0]
            for gi in gis[1:]:
                pcid = e.glyphs[gi - 1][3]
                try:
                    x += fo.glyph_advance(pcid if pcid and pcid < 0x110000
                                          else 32) * scale
                except Exception:
                    x += 0.5 * scale
                gx, gy, gf, gcid = e.glyphs[gi]
                e.glyphs[gi] = (x, gy, gf, gcid)
    return False


def merged_glyph_map(page, els, H, gm):
    """`gm`, plus every glyph that get_text failed to report.

    Some exports are almost invisible to get_text: on one chart it reports the
    repeat sign and a single notehead and nothing else, so the clef, the key
    signature and the time signature are all missing, and the header measures
    as empty space that the new accidentals are free to occupy.

    get_texttrace sits below the text extractor and does report them, with real
    origins and real bounding boxes, so it is the one that decides geometry.
    """
    out = dict(gm)
    for sp in page.get_texttrace():
        if sp.get("type") != 0:               # 0 = ordinary filled text
            continue
        font = sp["font"].split("+")[-1]
        for ucs, _gid, org, bb in sp["chars"]:
            if ucs == 32:
                continue
            key = (round(org[0], 1), round(org[1], 1))
            if key in out:
                continue
            out[key] = (chr(ucs), font, (bb[0], bb[1], bb[2], bb[3]),
                        sp["size"])
    return out


def first_note_x(page, gm, geom):
    """Left edge of real music on each staff (past clef + key signature)."""
    out = []
    for st in geom:
        xs = [x for (x, y), (ch, fn, bb, *_r) in gm.items()
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
        # Pick the staff whose GRID the note actually sits on, not merely the
        # nearest one. A low treble ledger note can be closer to the bass staff
        # while only fitting the treble grid; choosing by distance alone leaves
        # it behind while its stem and beam move without it.
        best = None
        for st in geom:
            pos = (y - st["top"]) / st["half"]
            if not -12 <= pos <= 20:
                continue
            resid = abs(pos - round(pos))
            dist = abs(y - (st["top"] + st["bot"]) / 2)
            if resid <= 0.3 and (best is None or (resid, dist) < best[:2]):
                best = (resid, dist, pos)
        return best is not None and -12 <= best[2] <= 20
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


GAP = 1.0          # clearance the key signature must leave before the next item
# Only a time signature or a start-repeat can follow a key signature, and both
# are narrow. Anything further right than this is music - a bar-repeat sign or
# a rest - which must neither move nor be mistaken for something that can.
HEADER_REACH = 20.0


def first_music_x(gm, st, lx, nx, hi_x, skip=()):
    """Leftmost music that is not part of the header, i.e. what it may not hit.

    `notex` only sees noteheads, so a bar opening with a rest or a bar-repeat
    sign reports its first note far to the right and the header looks free to
    expand over the top of it.
    """
    lo = st["top"] - 2.5 * st["half"]
    hi = st["bot"] + 2.5 * st["half"]
    xs = [gx for (gx, gy), (c, f, bb, *_r) in gm.items()
          if lo <= gy <= hi and hi_x <= gx < nx
          and (round(gx, 1), round(gy, 1)) not in skip
          and is_music_font(f) and not is_chord_font(f)]
    return min(xs, default=nx)


def header_items(page, gm, st, lx, nx, skip=()):
    """Ink drawn between the key signature and the first note of this staff.

    Only what shares the staff counts. A chord symbol floats above it and is
    tied to a note, so treating it as header content both understates the room
    available and drags the symbol away from the note it names. `skip` holds
    the signature's own accidentals, which are what we are making room for.
    """
    lo = st["top"] - 2.5 * st["half"]
    hi = st["bot"] + 2.5 * st["half"]
    out = []
    for (gx, gy), (c, f, bb, *_r) in gm.items():
        if lo <= gy <= hi and lx + 1 < gx < nx - 0.5 \
           and (round(gx, 1), round(gy, 1)) not in skip \
           and is_music_font(f) and not is_chord_font(f):
            out.append((gx, bb[2]))
    for d in page.get_drawings():
        r = d["rect"]
        if d["type"] != "s" and r.width < 40 \
           and lx + 1 < r.x0 < nx - 0.5 and lo <= (r.y0 + r.y1) / 2 <= hi:
            out.append((r.x0, r.x1))
    return sorted(out)


def clef_right(gm, st, lx):
    """Right edge of the clef, the leftmost the key signature may start."""
    lo = st["top"] - 2.5 * st["half"]
    hi = st["bot"] + 2.5 * st["half"]
    xs = [bb[2] for (gx, gy), (c, f, bb, *_r) in gm.items()
          if lo <= gy <= hi and st["x0"] - 1 < gx < lx - 0.5
          and is_music_font(f) and not is_chord_font(f)]
    return max(xs) if xs else st["x0"] + 4.0


def keysig_plan(page, els, gm, geom, notex, H, dst_sig, half):
    """Where every key-signature accidental should end up, and at what size.

    Widening a signature is the only edit in a transposition that needs
    horizontal room. Each staff reports how much it has; the tightest one on
    the page sets a single scale, because signatures of different sizes on one
    page look like a mistake.
    """
    plans, scale = [], 1.0
    for st, nx in zip(geom, notex):
        mid = (st["top"] + st["bot"]) / 2
        acc = []
        for e in els:
            if e.kind != "text" or not e.glyphs:
                continue
            for gi, (gx, gy, _f, _c) in enumerate(e.glyphs):
                x, y = gx, H - gy
                ch, fn, bb = gm.get((round(x, 1), round(y, 1)),
                                    ("?", "?", (0, 0, 0, 0), 0))[:3]
                ch = norm_glyph(ch)
                # A key-signature accidental sits in the staff header,
                # immediately after the clef - not anywhere left of the note.
                # Glyphs are judged one by one: some exports batch the clef
                # and the whole signature into a single text run.
                if ch in ACCIDENTALS and is_music_font(fn) \
                   and not is_chord_font(fn) and abs(y - mid) < 28 \
                   and x < nx - 12 and x < st["x0"] + 95:
                    acc.append((x, y, e, (bb[2] - bb[0]) or 5.0, ch, gi))
        if not acc:
            # Nothing to clone from and nothing to remove: this staff prints no
            # key signature (common on continuation systems and on charts that
            # rely on chord symbols alone). Leave it be.
            continue
        acc.sort()
        # The signature is measured with the width of the glyph it will
        # DRAW, not the one already printed: a sharp is a third wider than
        # a flat, and sizing flats-to-sharps by the flat leaves the last
        # sharp lying across whatever follows the header.
        want_g = "b" if dst_sig < 0 else "#"
        have_g = acc[0][4]
        widen = 1.0
        if want_g != have_g:
            dw = accidental_donor(els, gm, H, want_g)
            widen = (dw[3] / max(acc[0][3], 0.1)) if dw else (
                1.33 if want_g == "#" else 0.78)
        # The clef names the staff, not the accidental's position: a sharp
        # signature starts at the TOP of the staff and the position test
        # would call every bass staff treble.
        bass = False
        for (gx, gy), (c, fn, *_r) in gm.items():
            if gx < st["x0"] + 40 \
               and abs(gy - (st["top"] + st["bot"]) / 2) < 30 \
               and norm_glyph(c) in ("&", "?"):
                bass = norm_glyph(c) == "?"
                break
        want = positions(dst_sig, bass)
        spacing = (acc[1][0] - acc[0][0] if len(acc) > 1
                   else 2.258 * half) * widen
        # Neighbours must clear each other whatever the scale: spacing and
        # glyph width shrink together, so a spacing narrower than the glyph
        # overlaps just as badly at 75% as at full size.
        spacing = max(spacing, max(a[3] for a in acc) * widen + 0.35)
        lx, width = acc[0][0], max(a[3] for a in acc) * widen
        skip = {(round(a[0], 1), round(a[1], 1)) for a in acc}
        ksend = max(a[0] + a[3] for a in acc)
        limit = first_music_x(gm, st, lx, nx, ksend + HEADER_REACH, skip)
        hdr = [g for g in header_items(page, gm, st, lx, nx, skip)
               if g[0] < limit]
        nextx = min([g[0] for g in hdr], default=limit)
        # How far that header content may move before it would collide with the
        # first note. Without this cap the repeat dots land on top of the music.
        right = max([g[1] for g in hdr], default=lx)
        cap = max(0.0, limit - right - GAP) if hdr else 0.0
        # width the signature needs at full size, and what it can be given
        need = (len(want) - 1) * spacing + width + GAP if want else 0.0
        shift = min(cap, max(0.0, need - (nextx - lx)))
        if want and need > nextx + shift - lx:
            # Still short: close the gap the engraver left between the clef and
            # the signature before resorting to drawing it smaller.
            short = need - (nextx + shift - lx)
            lx = min(lx, max(clef_right(gm, st, lx) + 0.75, lx - short))
        room = nextx + shift - lx
        if want and need > room:
            # The floor only guards against a signature so small it stops being
            # readable; it must stay below anything a real chart asks for, or
            # it silently reintroduces the overlap it exists to prevent.
            scale = min(scale, max(0.40, (room - GAP) /
                                   max(need - GAP, 1e-6)))
        plans.append({"st": st, "acc": acc, "want": want, "spacing": spacing,
                      "lx": lx, "shift": shift, "nx": limit, "bass": bass,
                      "lo": st["top"] - 2.5 * st["half"],
                      "hi": st["bot"] + 2.5 * st["half"]})
    return plans, scale


def accidental_donor(els, gm, H, glyph):
    """A drawn accidental of the given kind, usable as a template.

    A transposition that flips the sign of the key - E major's sharps to C
    minor's flats - has nothing in the signature to clone, but the music
    itself usually prints the needed glyph somewhere as an inline accidental.
    Prefer the largest one: grace-note accidentals are drawn small.
    """
    best = None
    for e in els:
        if e.kind != "text" or len(e.glyphs) != 1:
            continue
        x, y = e.glyphs[0][0], H - e.glyphs[0][1]
        g = gm.get((round(x, 1), round(y, 1)))
        if not g or norm_glyph(g[0]) != glyph:
            continue
        if not is_music_font(g[1]) or is_chord_font(g[1]):
            continue
        size = g[3] if len(g) > 3 else 0
        if best is None or size > best[0]:
            best = (size, x, y, e,
                    (g[2][2] - g[2][0]) if g[2] else 5.0)
    return best and best[1:]


def keysig_edits(data, plans, scale, half, H, dst_sig, donor=None):
    """Add, move, erase or replace key-signature accidentals to match the plan.

    When the transposition flips the sign of the key - sharps to flats or
    back - nothing in the old signature can be cloned, so every accidental is
    erased and the new ones are cloned from `donor`, an inline accidental of
    the right kind found elsewhere in the music. Staves that cannot be served
    either way are returned for drawing with an installed font.
    """
    ins, blanks, zones, undrawable = [], {}, [], []
    want_glyph = "b" if dst_sig < 0 else "#"

    def blank(a):
        """Erase one accidental. Its own string+Tj bytes when it shares a text
        run with other glyphs (a batched clef-and-signature element), the whole
        element when it stands alone."""
        e, gi = a[2], a[5]
        if len(e.glyphs) > 1 and gi < len(e.gspans):
            blanks[e.gspans[gi]] = b" "
        else:
            blanks[(e.qstart, e.qend)] = b" "

    for p in plans:
        st, acc, want = p["st"], p["acc"], p["want"]
        spacing = p["spacing"] * scale
        # Only a whole element drawing nothing but this accidental can serve
        # as a clone template; cloning a batched run would redraw all of it.
        templates = [a for a in acc
                     if a[4] == want_glyph and len(a[2].glyphs) == 1]
        same_sign = all(a[4] == want_glyph for a in acc)
        keepable = same_sign and len(want) <= len(acc) \
            and all(scale == 1.0
                    and abs(p["lx"] + i * spacing - acc[i][0]) < 0.05
                    and abs(st["top"] + idx * half - acc[i][1]) < 0.05
                    for i, idx in enumerate(want))
        if keepable:
            # Same sign, same or fewer accidentals, already in place: keep the
            # prefix, erase the surplus. Signatures nest - D major's two
            # sharps are the first two of E major's four.
            for a in acc[len(want):]:
                blank(a)
        else:
            for a in acc:
                blank(a)
            if not want:
                pass
            elif templates or donor:
                sx, sy, e = templates[0][:3] if templates else donor[:3]
                for i, idx in enumerate(want):
                    tx = p["lx"] + i * spacing
                    ty = st["top"] + idx * half
                    ins.append((e.qstart,
                                clone(data, e, tx - sx, sy - ty, scale,
                                      (sx, H - sy))))
            else:
                undrawable.append(p)
        if p["shift"] > 0:
            zones.append((p["lx"], p["nx"], p["lo"], p["hi"], p["shift"]))
    return ins, blanks, zones, undrawable


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
        hits = [d for lx, nx, lo, hi, d in zones
                if lo <= y <= hi and lx + 1 < x < nx - 0.5]
        return min(hits) if hits else None

    for e in els:
        if e.kind != "text" or not e.glyphs:
            continue
        # A clef is fixed furniture. A mid-system clef change sits inside the
        # header window of the staff it belongs to, and shifting it drags it
        # away from the barline it announces.
        if any(norm_glyph(gm.get((round(x, 1), round(H - y, 1)),
                                 (" ", ""))[0] or " ") in CLEFS
               for x, y, _, _ in e.glyphs):
            continue
        ds = [zone_dx(x, H - y) for x, y, _, _ in e.glyphs]
        if any(d is None for d in ds):
            continue
        dx = min(ds)
        d = e.ctm0[0]
        if dx <= 0 or abs(d) < 1e-9:
            continue
        if e.tm_slots and len(e.tm_slots) >= 6:
            # Rewrite this element's own Tm rather than injecting a cm. A cm
            # stays in force until the enclosing Q, so it moves every later
            # glyph in the same block too - and with nothing to restore it at
            # all, a standalone run shifts the rest of the page.
            val, s0, s1 = e.tm_slots[4]
            edits[(s0, s1)] = b"%.5f" % (val + dx / d)
        else:
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
        # A beam spans a few notes at most. A filled box wider than that is
        # furniture - a text frame, a rehearsal box, a rule under a heading -
        # and moving it with the music detaches it from the text it belongs to.
        if w > 120 and h < 3:
            return "furniture"
        return "beam"
    if h < 1.2:                                   # horizontal
        for st in geom:
            for ln in st["lines"]:
                if abs(ymid - ln) < 0.8 and w > 60:
                    return "staffline"
        if w > 120:
            return "furniture"        # a rule under a heading, not a beam
        return "ledger" if w < 40 else "beam"
    if w < 1.2:                                   # vertical
        for st in geom:
            if min(ys) <= st["top"] + 1 and max(ys) >= st["bot"] - 1:
                return "barline"
        return "stem"
    if w > 120 and h < 3:
        return "furniture"        # a rule or frame, whatever paints it
    return "curve"


TD_RE = re.compile(rb"([-\d.]+)\s+([-\d.]+)\s+Td")


def chord_font_advances(page):
    """Advance width of each chord glyph, read from the embedded font.

    Returned in the same 1000-per-em text-space units the Td offsets use.
    """
    out = {}
    for f in page.get_fonts(full=True):
        if not is_chord_font(f[3]):
            continue
        try:
            font = pymupdf.Font(fontbuffer=page.parent.extract_font(f[0])[3])
        except Exception:
            continue
        for ch in "ABCDEFG/¨«‹Œ„Š#b":
            try:
                a = font.glyph_advance(ord(ch)) * 1000.0
            except Exception:
                continue
            if a > 0:
                out.setdefault(ch, round(a, 3))
    return out


def chord_edits(page, data, els, gm, H, steps, semis, flats):
    """Retypeset every chord symbol in the transposed key."""

    class Piece:
        """One shown string of chord text: the unit that can be rewritten.

        For the classic exports one BT run is one string and the whole run is
        rewritten in place. A batched export shows a whole line of chords
        inside a single element, so each string is erased and repainted
        individually instead.
        """
        __slots__ = ("e", "gis", "text", "span", "batched", "glyphs",
                     "bt", "et", "ctm")

        def __init__(self, e, gis, text, batched):
            self.e, self.gis, self.text = e, gis, text
            self.span = e.gspans[gis[0]] if gis[0] < len(e.gspans) else None
            self.batched = batched
            self.glyphs = [e.glyphs[k] for k in gis]
            self.bt, self.et, self.ctm = e.bt, e.et, e.ctm

    runs, items = [], []
    for e in els:
        if e.kind != "text" or not e.glyphs or e.bt is None:
            continue
        # Group glyphs by shown string. A batched export interleaves chord
        # strings with bar numbers and notation in ONE element, so the
        # element's first glyph says nothing about any particular string.
        spans = collections.defaultdict(list)
        for gi in range(len(e.glyphs)):
            spans[e.gspans[gi] if gi < len(e.gspans) else (0, 0)].append(gi)
        nspans = len(spans)
        whole_chord_element = None
        for span, gis in sorted(spans.items()):
            gtab = [gm.get((round(e.glyphs[k][0], 1),
                            round(H - e.glyphs[k][1], 1))) for k in gis]
            if not all(g and is_chord_font(g[1]) for g in gtab):
                continue
            # (char, cid) stay PAIRED even when a separator space is added:
            # zipping two lists of different lengths silently skews the cid
            # table one place per space and every rebuilt letter goes wrong.
            pairs, prev_right = [], None
            for g, k in zip(gtab, gis):
                x = e.glyphs[k][0]
                # Text extraction drops spaces, so a run holding two chords
                # arrives glued together and only the first would transpose.
                # Whitespace after the previous glyph's own ink is the
                # separator - a distance between origins would also fire
                # inside one symbol, right after any wide ligature ("sus").
                if prev_right is not None and x - prev_right > 3.0:
                    pairs.append((" ", None))
                pairs.append((g[0], e.glyphs[k][3]))
                bb = g[2] if len(g) > 2 and g[2] else None
                prev_right = bb[2] if bb else x + 6.0
            chars = [c for c, _cid in pairs]
            fontname = gtab[0][1]
            items.append((Piece(e, gis, "".join(chars), nspans > 1),
                          "".join(chars), fontname))
            if nspans == 1:
                whole_chord_element = (pairs,)
        if whole_chord_element:
            pairs, = whole_chord_element
            bt = data[e.bt:e.et]
            tds = [float(m.group(1)) for m in TD_RE.finditer(bt)]
            # each Td after the first is the advance of the glyph before it
            deltas = (tds[1:] + [None])[:len(pairs)]
            runs.append(([c for c, _ in pairs], [c2 for _, c2 in pairs],
                         deltas,
                         (e.glyphs[gis[0]][2] or "").lstrip("/")))


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
            # Distance alone glues two neighbouring chords into one symbol, and
            # then only the first root gets transposed. A fresh root letter
            # starts a new chord unless it follows a slash (a bass note).
            nxt = items[j][1][:1]
            if nxt and nxt in "ABCDEFG" and not grp[-1][1].endswith("/"):
                break
            grp.append(items[j])
            j += 1
        merged.append((grp[0][0], "".join(g[1] for g in grp), grp[0][2],
                       [g[0] for g in grp]))
        i = j
    items = merged
    if not items:                       # chart carries no chord symbols
        return {}, 0, [], []
    for _e, _t, _f, group in items:
        for pc in group:
            if pc.batched:
                cs, ks = [], []
                ki = iter(pc.gis)
                for ch in pc.text:
                    cs.append(ch)
                    ks.append(None if ch == " "
                              else pc.e.glyphs[next(ki)][3])
                runs.append((cs, ks, [None] * len(cs),
                             (pc.e.glyphs[pc.gis[0]][2] or "").lstrip("/")))
    cids, width = CH.learn(runs, chord_font_advances(page))
    fallback = sorted(width.values())[len(width) // 2] if width else 0
    allch = {ch for t in cids.values() for ch in t}
    # Chord fonts disagree about accidentals: Sibelius Chords faces keep the
    # flat at 0xA8, a Type3 export just uses the letters. Write whichever this
    # document's own fonts actually map.
    # A Sibelius chord face keeps the flat at 0xA8 and the sharp at 0xA9
    # (0xAB is the DOUBLE sharp); a Type3 export just uses the letters. The
    # subset embedded in a flat-key chart contains no sharp at all, so the
    # symbol goes to the redraw path with the installed face - which needs
    # the face's own codepoint, not the ASCII letter.
    sib = any(ch in allch for ch in "\u00a8\u2039\u00ab")
    flatg = "\u00a8" if ("\u00a8" in allch or sib) else "b"
    sharpg = "\u00a9" if ("\u00a9" in allch or sib) else "#"
    simple = simple_font_resources(page)
    # horizontal room before the next chord symbol on the same line
    pos = sorted((round(H - e.glyphs[0][1], 1), e.glyphs[0][0], i)
                 for i, (e, _t, _f, _g) in enumerate(items))
    room = {}
    for k, (y, x, i) in enumerate(pos):
        nxt = next((px for py, px, _ in pos[k + 1:] if abs(py - y) < 2), None)
        room[i] = (nxt - x - 1.5) if nxt else 1e9
    edits, n, missed, redraw = {}, 0, [], []
    tail = b""

    def esc(bts):
        return (b"(" + bts.replace(b"\\", rb"\\\\")
                          .replace(b"(", rb"\(").replace(b")", rb"\)") + b")")

    for i, (e, text, fontname, group) in enumerate(items):
        new = "".join(CH.transpose_glyphs(list(text), steps, semis,
                                          flatg, sharpg, flats))
        if new == text:
            continue
        cid = cids.get((e.glyphs[0][2] or "").lstrip("/"), {})
        if any(pc.batched for pc in group):
            # Batched export: the element holds a whole line of chords, so the
            # symbol's own strings are erased and repainted individually.
            # Each transposed character remembers which input glyph produced
            # it, so the result can be split back over the original strings
            # even when an accidental appears or disappears.
            comb, owner = [], []
            for pi, pc in enumerate(group):
                comb.extend(list(pc.text))
                owner.extend([pi] * len(pc.text))
            pairs = CH.transpose_pairs(comb, steps, semis, flatg, sharpg,
                                       flats)
            newtexts = ["" for _ in group]
            for ch, src in pairs:
                newtexts[owner[min(src, len(owner) - 1)]] += ch
            onebyte = (e.glyphs[0][2] or "").lstrip("/") in simple
            plan = []
            ok = True
            for pc, newt in zip(group, newtexts):
                if newt == pc.text:
                    continue
                if any(ch not in cid for ch in newt if ch != " "):
                    ok = False
                    break
                codes = [cid[ch] for ch in newt if ch != " "]
                if onebyte:
                    tok = esc(bytes(c & 0xFF for c in codes))
                else:
                    # a composite font reads TWO bytes per glyph: write a hex
                    # string, one four-digit code per character
                    tok = b"<" + b"".join(b"%04X" % c for c in codes) + b">"
                plan.append((pc, tok))
            if ok:
                for pc, tok in plan:
                    # Replace the string INSIDE its own show operator. These
                    # runs position glyphs by accumulated advances, so
                    # erasing a string and repainting it elsewhere would
                    # shift every glyph drawn after it in the same run.
                    s0, s1 = pc.span
                    snippet = data[s0:s1]
                    m = STR_RE.search(snippet)
                    if not m:
                        continue
                    edits[pc.span] = (snippet[:m.start()] + tok
                                      + snippet[m.end():])
                n += 1
            else:
                # the subset font lacks a needed letter: erase the strings and
                # draw the whole symbol with an installed face
                full = (CH.find_full_font(fontname)
                        or CH.find_full_font("OpusChordsStd")
                        or CH.find_full_font("Inkpen2ChordsStd"))
                if full:
                    for pc in group:
                        if pc.span:
                            edits[pc.span] = b" "
                    bb = gm.get((round(e.glyphs[0][0], 1),
                                 round(H - e.glyphs[0][1], 1)))
                    size = bb[3] if bb and len(bb) > 3 else 10.0
                    redraw.append({"x": e.glyphs[0][0],
                                   "y": H - e.glyphs[0][1],
                                   "text": new.replace("b", "\u00a8")
                                              .replace("#", "\u00ab"),
                                   "size": max(4.0, size), "font": full})
                    n += 1
                else:
                    missed.append(new)
            continue
        sq = 1.0
        sc = abs(e.ctm[0]) or 1.0
        nat = CH.natural_width(new, width, fallback)
        avail = room.get(i, 1e9) / sc
        if nat > avail > 0:
            sq = max(0.78, avail / nat)
        onebyte = (e.glyphs[0][2] or "").lstrip("/") in simple
        rep = CH.rebuild(data[e.bt:e.et], new, cid, width, fallback, sq,
                         hexw=2 if onebyte else 4)
        if rep is not None:
            edits[(e.bt, e.et)] = rep
            for extra in group[1:]:          # symbol was split across blocks
                edits[(extra.bt, extra.et)] = b" "
            n += 1
        else:
            # The embedded font is subsetted and lacks a letter we now need.
            # Blank the old symbol and redraw it with the full installed face.
            full = (CH.find_full_font(fontname)
                    or CH.find_full_font("OpusChordsStd")
                    or CH.find_full_font("Inkpen2ChordsStd"))
            if full:
                # Font size may come from Tf, or from the Tm scale when Tf is
                # "1" - measure the drawn glyph instead of trusting either.
                bb = gm.get((round(e.glyphs[0][0], 1),
                             round(H - e.glyphs[0][1], 1)))
                # use the span's own point size; deriving it from the glyph
                # bounding box makes the replacement noticeably larger
                size = bb[3] if bb and len(bb) > 3 else 10.0
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
    if tail:
        # one past the end so it cannot collide with the glyph mover's tail
        edits[(len(data) + 1, len(data) + 1)] = tail
    return edits, n, sorted(set(missed)), redraw


def on_staff_grid(y, geom, half):
    """True if y is a plausible notehead position on some staff.

    A notehead sits on an exact half-step of its staff. Anything drifting off
    that grid is text or ornament that merely happens to be nearby.
    """
    for st in geom:
        pos = (y - st["top"]) / half
        if -12 <= pos <= 20 and abs(pos - round(pos)) <= 0.3:
            return True
    return False


def ledger_edits(subs, gm, geom, H, steps, half, want_new=None,
                 note_pts=None):
    """Recompute ledger lines instead of translating them.

    Ledger lines only exist at line positions (even staff indices outside the
    staff), so moving them by one diatonic step would land them in a space.
    Each note's stack is rebuilt from the note's new position and any surplus
    line is collapsed to zero length.
    """
    edits = {}
    # The notehead table is a set of ASCII letters that the music fonts remap,
    # so an ordinary 'w' in a text label matches it. Without the font test a
    # word above the staff asks for a stack of ledger lines of its own.
    notes = note_pts if note_pts is not None else \
        [(x, y) for (x, y), v in gm.items()
         if norm_glyph(v[0]) in NOTEHEADS
         and is_music_font(v[1]) and not is_chord_font(v[1])
         and on_staff_grid(y, geom, half)]
    have = collections.defaultdict(set)     # (staff, x-bucket) -> slots present
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
        have[(id(st), round(cx / 6))].update(want)
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

    # A note can move ONTO a ledger position that had no line before; without
    # this the notehead floats above the staff with nothing under it.
    if want_new is not None:
        seen = collections.defaultdict(set)
        for k, v in have.items():
            seen[k] = v
        for nx_, ny in notes:
            st = min(geom, key=lambda g: abs(ny - (g["top"] + g["bot"]) / 2))
            nidx = round((ny - st["top"]) / half) - steps
            need = []
            if nidx <= -2:
                need = list(range(-2, nidx - 1, -2))
            elif nidx >= 10:
                need = list(range(10, nidx + 1, 2))
            if not need:
                continue
            key = (id(st), round(nx_ / 6))
            for slot in need:
                if slot in seen.get(key, ()):  
                    continue
                seen.setdefault(key, set()).add(slot)
                want_new.append({"x": nx_, "y": st["top"] + slot * half,
                                 "half": half})
    return edits



FLAT_ORDER = ["B", "E", "A", "D", "G", "C", "F"]
SHARP_ORDER = ["F", "C", "G", "D", "A", "E", "B"]


def key_alters(sig):
    if sig < 0:
        return {n: -1 for n in FLAT_ORDER[:-sig]}
    return {n: 1 for n in SHARP_ORDER[:sig]}


def barlines(geom, pops, H, half, heads=()):
    """x of every barline, per staff.

    A barline is often drawn in PIECES - a section bar as two strokes, a
    repeat bar as stroke plus block - so vertical segments sharing an x are
    unioned first. What then tells a barline from a stem long enough to
    cross the staff is where it ENDS: a barline runs from the top line to
    the bottom line and stops, a stem always overshoots to reach its note.
    """
    subs_ = collections.defaultdict(list)
    for o in pops:
        subs_[o.sub].append(o)
    segs = collections.defaultdict(lambda: [1e9, -1e9])
    for ops_ in subs_.values():
        pts = [pt for o in ops_ for pt in o.pts]
        xs = [pt[0] for pt in pts]
        ys = [H - pt[1] for pt in pts]
        if max(xs) - min(xs) > 3.2 or max(ys) - min(ys) < 2 * half:
            continue
        k = round(sum(xs) / len(xs) * 2) / 2
        segs[k][0] = min(segs[k][0], min(ys))
        segs[k][1] = max(segs[k][1], max(ys))
    bars = [[] for _ in geom]
    for x, (ylo, yhi) in segs.items():
        for si, st in enumerate(geom):
            # A barline covers the staff exactly, top line to bottom line.
            # It may also run FURTHER when one stroke spans a whole grand
            # staff, but it never stops short inside the staff - which is
            # what a stem reaching across it does.
            # A barline covers its staff exactly, top line to bottom line.
            # Strokes that merely reach across - a stem, or a bracket drawn
            # through a whole system - do not, and taking them as bar
            # boundaries resets accidental state in the middle of a bar.
            # A barline covers its staff exactly, top line to bottom line.
            # A stroke that crosses a bracketed system is not recognised
            # here; accidental state then runs to the end of the system,
            # which is safe - it never drops a sign the music needs.
            if not (abs(ylo - st["top"]) < 1.5
                    and abs(yhi - st["bot"]) < 1.5):
                continue
            # A stem can also cover the staff exactly. What it cannot do is
            # stand alone: it always carries a notehead at its side - on the
            # left when it points down, the right when it points up.
            mid = (st["top"] + st["bot"]) / 2
            if any(abs(hy - mid) < 26 and -1.5 < x - hx < 8.0
                   for hx, hy in heads):
                continue
            bars[si].append(x)
    for b_ in bars:
        b_.sort()
    return bars


def respell_accidentals(data, els, gm, geom, notex, H, steps, semis,
                        src_sig, dst_sig, half, page, pops=()):
    """Re-spell every inline accidental for the new key.

    Moving an accidental with its note preserves the GLYPH but not the
    MEANING: a natural cancelling E major's D sharp names D natural, and after
    transposing to C that note is B flat - the printed sign must become a
    flat, not stay a natural. For every notehead: read its sounded pitch in
    the old key, compute what the new key needs printed, and erase, swap or
    add the accidental accordingly.

    Accidentals last until the barline. Both the reading of the old pitch
    and the decision of what the new bar needs printed carry state from note
    to note within each measure, per staff position - dropping a sign because
    it matches the new key signature is wrong if a note two beats earlier
    already changed what that line means.
    """
    src_alt, dst_alt = key_alters(src_sig), key_alters(dst_sig)
    blanks, ins, redraw = {}, [], []
    stats = collections.Counter()

    # per-staff clef: a grand staff mixes treble and bass
    sbase = []
    for st in geom:
        base = 38                              # treble: top line F5
        for (x, y), (c, fn, *_r) in gm.items():
            if x < st["x0"] + 40 \
               and abs(y - (st["top"] + st["bot"]) / 2) < 30 \
               and norm_glyph(c) in ("&", "?"):
                base = 38 if norm_glyph(c) == "&" else 26
                break
        sbase.append(base)

    # inline accidentals (header/key-signature ones are handled elsewhere)
    accs = []
    for e in els:
        if e.kind != "text" or not e.glyphs:
            continue
        for gi, (gx, gy, gf, cid) in enumerate(e.glyphs):
            x, y = gx, H - gy
            g = gm.get((round(x, 1), round(y, 1)))
            if not g:
                continue
            ch = norm_glyph(g[0])
            if ch not in ACCIDENTALS or not is_music_font(g[1]) \
               or is_chord_font(g[1]):
                continue
            si = min(range(len(geom)), key=lambda k: abs(
                y - (geom[k]["top"] + geom[k]["bot"]) / 2))
            if x < notex[si] - 12:
                continue                       # key signature, not inline
            accs.append({"x": x, "y": y, "e": e, "gi": gi, "ch": ch,
                         "used": False})

    donors = {g: accidental_donor(els, gm, H, g) for g in "b#n"}
    font = None                                # installed face, found lazily

    # barline x positions per staff, so accidental state can reset per bar
    import bisect
    bars = [[] for _ in geom]
    subs_ = collections.defaultdict(list)
    for o in pops:
        subs_[o.sub].append(o)
    # A barline is often drawn in PIECES (a section bar as two strokes, a
    # repeat bar as stroke plus block); union the vertical segments sharing
    # an x before asking whether the whole thing spans the staff.
    segs = collections.defaultdict(lambda: [1e9, -1e9])
    for ops_ in subs_.values():
        pts = [pt for o in ops_ for pt in o.pts]
        xs = [pt[0] for pt in pts]
        ys = [H - pt[1] for pt in pts]
        if max(xs) - min(xs) > 3.2 or max(ys) - min(ys) < 2 * half:
            continue
        k = round(sum(xs) / len(xs) * 2) / 2
        segs[k][0] = min(segs[k][0], min(ys))
        segs[k][1] = max(segs[k][1], max(ys))
    for x, (ylo, yhi) in segs.items():
        for si, st in enumerate(geom):
            # A barline runs from the top line to the bottom line and stops.
            # A stem long enough to cross the staff always overshoots one end
            # to reach its notehead, so the ENDS tell them apart exactly -
            # no guessing from what sits nearby.
            if abs(ylo - st["top"]) < 1.5 and abs(yhi - st["bot"]) < 1.5:
                bars[si].append(x)
    for b_ in bars:
        b_.sort()

    heads = sorted(((x, y) for (x, y), v in gm.items()
                    if norm_glyph(v[0]) in NOTEHEADS
                    and is_music_font(v[1]) and not is_chord_font(v[1])
                    and on_staff_grid(y, geom, half)))
    # A stem long enough to span the staff is indistinguishable from a
    # barline by geometry alone; a real barline never has a notehead beside
    # it. (Chords widen the test: the head sits on either side of its stem.)
    old_state, new_state, drawn = {}, {}, set()
    for nx_, ny in heads:
        si = min(range(len(geom)), key=lambda k: abs(
            ny - (geom[k]["top"] + geom[k]["bot"]) / 2))
        st = geom[si]
        idx = round((ny - st["top"]) / half)
        dia = sbase[si] - idx
        step_o = STEPS[dia % 7]
        att = None
        for a in accs:
            # A chord stacks its accidentals in columns to the left of the
            # noteheads, so the outermost of three sits far further out than
            # a single one. Scale the reach with the staff rather than fixing
            # it in points, which only ever fits one engraving size.
            if not a["used"] and 0 < nx_ - a["x"] < half * 9.0 \
               and abs(a["y"] - ny) < half * 0.9:
                if att is None or a["x"] > att["x"]:
                    att = a
        bar = bisect.bisect(bars[si], nx_)
        # Keyed by the exact staff position, not the note name: an
        # accidental governs ONE line, and sharing state with the same
        # letter an octave away silences signs that are really needed.
        key = (si, bar, idx)
        if att:
            alt_old = ACCIDENTALS[att["ch"]]
        elif key in old_state:
            alt_old = old_state[key]           # earlier sign still in force
        else:
            alt_old = src_alt.get(step_o, 0)
        old_state[key] = alt_old
        midi_old = (dia // 7 + 1) * 12 + SEMI[step_o] + alt_old
        dia_n = dia + steps
        step_n = STEPS[dia_n % 7]
        need = midi_old + semis - ((dia_n // 7 + 1) * 12 + SEMI[step_n])
        key_n = (si, bar, idx - steps)
        cur_new = new_state.get(key_n, dst_alt.get(step_n, 0))
        want = None if need == cur_new else need
        new_state[key_n] = need
        want_ch = {None: None, -1: "b", 0: "n", 1: "#",
                   -2: "bb", 2: "##"}.get(want, "?!")
        if want_ch == "?!":
            stats["accidental_impossible"] += 1
            continue
        cur_ch = att["ch"] if att else None
        if att:
            att["used"] = True
        # One accidental per staff line per bar: two voices can land on the
        # same line, and a second glyph beside the first reads as a double
        # flat. The note keeps its own sign; only the redundant DRAW is
        # skipped, so the pitch is unchanged either way.
        slot = (si, bar, round((ny - steps * half) / half))
        if cur_ch == want_ch:
            drawn.add(slot)
            continue                           # the moved glyph already reads right
        if att:
            e, gi = att["e"], att["gi"]
            if len(e.glyphs) > 1 and gi < len(e.gspans):
                blanks[e.gspans[gi]] = b" "
            else:
                blanks[(e.qstart, e.qend)] = b" "
            stats["accidental_erased"] += 1
        if want_ch:
            # the note's OWN new position: deriving it from the staff index
            # re-rounds through whichever staff was picked and can land the
            # sign on a different system entirely
            if slot in drawn and not att:
                # Already printed on this line in this bar by another voice;
                # it governs both notes. A second glyph beside the first
                # reads as a double flat. A note that carried its OWN sign
                # keeps one: its glyph was erased and must be replaced.
                stats["accidental_shared"] += 1
                continue
            drawn.add(slot)
            new_y = ny - steps * half
            ax = att["x"] if att else nx_ - 7.0
            if not att:
                # never on top of the header: the first note of a system sits
                # right after the time signature
                hdr_right = max((bb2[2] for (gx2, gy2), (c2, f2, bb2, *_r2)
                                 in gm.items()
                                 if bb2 and gx2 < nx_ - 2
                                 and abs(gy2 - (st["top"] + st["bot"]) / 2) < 26
                                 and norm_glyph(c2) not in NOTEHEADS),
                                default=0.0)
                # the glyph's ink can start left of its origin, so clear the
                # header by a whole accidental's width
                ax = max(ax, hdr_right + 4.6)
                ax = min(ax, nx_ - 3.0)
            d = donors.get(want_ch[0])
            if d and len(want_ch) == 1:
                dx_, dy_, de = d[0], d[1], d[2]
                ins.append((de.qstart,
                            clone(data, de, ax - dx_, dy_ - new_y, 1.0,
                                  (dx_, H - dy_))))
            elif d and len(want_ch) == 2:
                # a double accidental: two glyphs side by side
                dx_, dy_, de = d[0], d[1], d[2]
                for k in range(2):
                    ins.append((de.qstart,
                                clone(data, de,
                                      ax - 3.2 - dx_ + k * 3.4,
                                      dy_ - new_y, 1.0, (dx_, H - dy_))))
            else:
                if font is None:
                    font = installed_music_font(page) or ""
                if font:
                    redraw.append({"x": ax - (3.2 if len(want_ch) == 2
                                              else 0.0),
                                   "y": new_y, "text": want_ch,
                                   "size": half * 8.0, "font": font})
            stats["accidental_set_" + want_ch] += 1
    return blanks, ins, redraw, stats


STR_RE = re.compile(rb"(\((?:\\.|[^\\()])*\))|(<[0-9A-Fa-f\s]*>)")


def repaint_span(data, e, gi, dy_user, replace=None):
    """Bytes that redraw one shown string displaced vertically.

    For a text run that mixes moving and fixed glyphs - one batched element
    holding noteheads next to rests and barline numerals - the whole-element
    translation is wrong for someone. The string is erased where it was and
    repainted at the end of the page with its own full matrix, so the move
    affects nothing else.
    """
    s0, s1 = e.gspans[gi]
    m = STR_RE.search(data[s0:s1])
    if not m or gi >= len(e.gmats):
        return None
    ctm_g, tm_g, size = e.gmats[gi]
    res = (e.glyphs[gi][2] or "")
    if not res.startswith("/"):
        return None
    mat = mat_mul(tm_g, ctm_g)
    # The scale and skew come from the parsed matrices, but the ORIGIN comes
    # from the aligned glyph position: the parser's own text-line tracking
    # drifts on quote-operator runs, and a repaint at the drifted origin puts
    # the glyph visibly in the wrong place.
    gx, gy = e.glyphs[gi][0], e.glyphs[gi][1]
    mat = (mat[0], mat[1], mat[2], mat[3], gx, gy + dy_user)
    return (b" q BT %s %.4f Tf %.5f %.5f %.5f %.5f %.5f %.5f Tm %s Tj ET Q "
            % ((res.encode("latin-1"), size) + mat
               + (replace if replace is not None else m.group(0),)))


def resource_fonts(page):
    """Map a content-stream resource name to its base font name.

    Type3 fonts have no base name at all; synthesise the label the text
    extractor uses ("Type3 (10 0 R)") so the role classifier can find them.
    """
    out = {}
    for f in page.get_fonts(full=True):
        name = f[3].split("+")[-1]
        if not name and f[2] == "Type3":
            name = "Type3 (%d 0 R)" % f[0]
        out[f[4]] = name
    return out


def simple_font_resources(page):
    """Resource names whose fonts use one byte per glyph."""
    out = set()
    for f in page.get_fonts(full=True):
        if f[2] != "Type0":
            out.add(f[4])
    return out



def installed_music_font(page):
    """A full system music font matching the document's, or any Sibelius one.

    A Type3 export names no fonts at all, so when nothing matches fall back to
    whichever standard Sibelius face is installed - the accidental shapes are
    interchangeable enough for a key signature.
    """
    for f in page.get_fonts(full=True):
        name = f[3]
        if name and is_music_font(name) and not is_chord_font(name) \
           and "Script" not in name and "Text" not in name \
           and "Special" not in name:
            hit = CH.find_full_font(name)
            if hit:
                return hit
    for generic in ("OpusStd", "Inkpen2Std", "Opus", "Inkpen2"):
        hit = CH.find_full_font(generic)
        if hit:
            return hit
    return None


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
        # Anchor to the clef and existing signature, NOT to the first note.
        # A bar that opens with rests puts its first note far to the right,
        # which would strand the added accidentals in the middle of the bar.
        hx = st["x0"] + 12
        for o in pops:
            for px, py in o.pts:
                ppy = page.rect.height - py
                if abs(ppy - mid) < 22 and st["x0"] + 2 < px < st["x0"] + 70:
                    hx = max(hx, px)
        x0 = hx + spacing * 0.75
        # never run into the music
        limit = nx - 3.5 - spacing * (len(add) - 1)
        if nx > st["x0"] + 30 and x0 > limit:
            x0 = max(st["x0"] + 12, limit)
        for k, idx in enumerate(add):
            out.append({"x": x0 + k * spacing,
                        "y": st["top"] + idx * half,
                        "text": glyph, "size": size, "font": font,
                        "keysig": True})
    return out

def keysig_scale(doc, src_sig, dst_sig):
    """The smallest key-signature scale any page needs, so all pages match.

    Sizing each page on its own puts a full-size signature on one page and a
    squeezed one on the next, which reads as a printing fault.
    """
    if src_sig == dst_sig:
        return 1.0
    s = 1.0
    for page in doc:
        geom = staff_geom(page)
        if not geom:
            continue
        gm = glyph_map(page)
        els, _ = parse(page.read_contents(),
                       simple_fonts=simple_font_resources(page))
        gmx = merged_glyph_map(page, els, page.rect.height, gm)
        _, ps = keysig_plan(page, els, gmx, geom,
                            first_note_x(page, gmx, geom),
                            page.rect.height, dst_sig, geom[0]["half"])
        s = min(s, ps)
    return s



def vector_keysig(objs, kinds, info, geom, half, dst_sig, page,
                  notex=None):
    """Adjust a key signature that is drawn as vector art.

    Signatures of the same sign nest, so going to a smaller one only needs
    the surplus accidentals collapsed to nothing. Growing or changing sign
    erases them all and draws the target signature with an installed music
    font - the shapes match closely enough for a signature.
    """
    edits, redraw = {}, []
    per_staff = collections.defaultdict(list)
    for pid, kind in kinds.items():
        if kind != "keysig":
            continue
        x0, y0, x1, y1, w, h, cx, cy, curved, st = info[pid]
        per_staff[id(st)].append((cx, cy, pid, st))

    def collapse(pid):
        # every x of the object onto ONE anchor: a zero-width fill paints
        # nothing, but per-op anchors leave a nonzero sliver behind
        anchor = None
        for o in objs[pid]:
            for si in X_SLOTS.get(o.op, ()):
                if si < len(o.slots):
                    val, s0, s1 = o.slots[si]
                    if anchor is None:
                        anchor = val
                    edits[(s0, s1)] = b"%.5f" % anchor

    font = None
    for _sid, accs in per_staff.items():
        accs.sort()
        st = accs[0][3]
        idx0 = round((accs[0][1] - st["top"]) / half)
        # a vector glyph's control-point centre sits lower than its origin;
        # judge the sign of the EXISTING signature by the source key instead
        have_flats = idx0 >= 2
        want = positions(dst_sig, bass=False)
        same_sign = (dst_sig < 0) == have_flats
        if same_sign and len(want) <= len(accs):
            for _cx, _cy, pid, _st in accs[len(want):]:
                collapse(pid)
            continue
        for _cx, _cy, pid, _st in accs:
            collapse(pid)
        if not want:
            continue
        if font is None:
            font = installed_music_font(page) or ""
        if not font:
            continue
        glyph = "b" if dst_sig < 0 else "#"
        spacing = accs[1][0] - accs[0][0] if len(accs) > 1 else 2.258 * half
        if dst_sig > 0:
            spacing *= 1.22                  # sharps are wider than flats
        lx = accs[0][0] - 1.2
        # A longer signature must still stop before whatever follows the
        # header. Shrink it to fit rather than laying the last accidental
        # across the barline that starts the music.
        size = half * 8.0
        si = geom.index(st) if st in geom else 0
        limit = (notex[si] if notex else st["x0"] + 60)
        # Anything drawn on this staff after the signature bounds it: the
        # thick block of a repeat bar is a FILL, not a stroked barline, so
        # asking for barlines alone misses exactly the shape that collides.
        for pid, (bx0, _y0, _x1, _y1, _w, bh, _cx, _cy, _c, bst) in \
                info.items():
            if bst is st and kinds.get(pid) != "keysig" \
               and bh > 2 * half and lx + 2 < bx0 < limit:
                limit = min(limit, bx0)
        need = (len(want) - 1) * spacing + spacing * 0.9
        room = max(limit - lx - 1.2, 4.0)
        if need > room:
            k = max(0.55, room / need)
            spacing *= k
            size *= k
        for i, idx in enumerate(want):
            redraw.append({"x": lx + i * spacing,
                           "y": st["top"] + idx * half,
                           "text": glyph, "size": size, "font": font,
                           "keysig": True})
    return edits, redraw


def transpose_page(page, steps, src_sig, dst_sig, semis=0, verbose=False,
                   scale=1.0):
    H = page.rect.height
    data = page.read_contents()
    els, pops = parse(data, simple_fonts=simple_font_resources(page))
    align_glyphs(page, els, H)
    geom = staff_geom(page)
    if not geom:
        return None, {}, []
    gm = glyph_map(page)
    resfonts = resource_fonts(page)
    notex = first_note_x(page, gm, geom)
    # Header geometry is measured from the merged map, so charts whose header
    # is invisible to text extraction are laid out from the content stream.
    gmx = merged_glyph_map(page, els, H, gm)
    notex_hdr = first_note_x(page, gmx, geom)
    half = geom[0]["half"]
    # A page with staves but no music-font glyphs is engraved as pure vector
    # art; notes are found and moved geometrically instead.
    vec_mode = not any(is_music_font(v[1]) and not is_chord_font(v[1])
                       for v in gmx.values())
    vkinds = vheads = vinfo = vobjs = None
    if vec_mode:
        vobjs = VEC.group(pops)
        vkinds, vheads, vinfo = VEC.classify(vobjs, geom, half, H)
        notex = [min([hx for hx, hy in vheads
                      if abs(hy - (g["top"] + g["bot"]) / 2) < 40],
                     default=g["x0"] + 60) for g in geom]
        notex_hdr = notex
    dy_page = -steps * half          # steps<0 (down) -> positive page dy
    dy_user = -dy_page               # PDF user space has y up

    edits = {}
    stats = collections.Counter()

    # ---- accidental re-spelling runs first, so the glyph mover knows which
    # strings it has already erased and replaced
    redraw_acc, respelled = [], set()
    if src_sig != dst_sig or semis:
        ab, ai, ar, ast = respell_accidentals(data, els, gmx, geom, notex, H,
                                              steps, semis, src_sig, dst_sig,
                                              half, page, pops)
        edits.update(ab)
        respelled = set(ab)
        for off, payload in ai:
            key = (off, off)
            edits[key] = edits.get(key, b"") + payload
        redraw_acc = ar
        stats.update(ast)

    # ---- glyphs: shift whatever rides with the notes
    tail = b""
    for e in els if steps else ():
        if e.kind != "text" or not e.glyphs:
            continue
        # Text extraction silently omits some glyphs (overprinted noteheads,
        # for one). Falling back to the run text the parser decoded keeps those
        # notes in the transposition instead of stranding them at the old pitch.
        flags = []
        for gi, (x, y, gf, cid) in enumerate(e.glyphs):
            g = gm.get((round(x, 1), round(H - y, 1)))
            if g is not None:
                ch, fn = g[0], g[1]
            elif e.text and gi < len(e.text):
                ch = e.text[gi]
                fn = resfonts.get((gf or "").lstrip("/"), "")
            else:
                flags.append(None)
                continue
            flags.append(bool(classify_glyph(ch, fn, x, H - y, geom, notex)))
        if not any(flags):
            continue
        d = e.ctm0[3]
        if all(f for f in flags) and abs(d) > 1e-9:
            # every glyph moves: translate the whole run at once
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
            continue
        # Mixed run: move string by string. A string only moves when every
        # glyph in it rides with the notes; erase it in place and repaint it
        # displaced, so its neighbours in the same run stay put.
        by_span = collections.defaultdict(list)
        for gi, f in enumerate(flags):
            if gi < len(e.gspans):
                by_span[e.gspans[gi]].append((gi, f))
        for span, members in by_span.items():
            fs = [f for _gi, f in members]
            if not all(f is True for f in fs):
                if any(fs):
                    stats["glyph_mixed_span"] += len(members)
                continue
            if span in respelled or span in edits:
                continue                     # someone else already edited it
            rp = repaint_span(data, e, members[0][0], dy_user)
            if rp is None:
                stats["glyph_unmovable"] += len(members)
                continue
            edits[span] = b" "
            tail += rp
            stats["glyph"] += len(members)
    if tail:
        edits[(len(data), len(data))] = tail

    # ---- paths: rewrite the y operand of moving subpaths
    subs = collections.defaultdict(list)
    for o in pops:
        subs[o.sub].append(o)
    if vec_mode:
        # geometric classification decides what rides with the notes
        for pid, ops in (vobjs.items() if steps else ()):
            kind = vkinds.get(pid, "other")
            stats["vec_" + kind] += 1
            if kind not in VEC.MOVING:
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
    else:
        for sid, ops in (subs.items() if steps else ()):
            pts = [p for o in ops for p in o.pts]
            painted = ops[-1].painted
            kind = subpath_kind(pts, painted, ops[0].lw, geom, H)
            stats["path_" + kind] += 1
            if kind in ("staffline", "barline", "ledger", "furniture"):
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
    new_ledgers = []
    if steps:
        edits.update(ledger_edits(subs, gm, geom, H, steps, half,
                                  want_new=new_ledgers,
                                  note_pts=vheads if vec_mode else None))


    ce, ncho, miss, redraw = chord_edits(page, data, els, gm, H, steps, semis,
                                         dst_sig <= 0)
    edits.update(ce)
    stats["chords"] = ncho
    if miss:
        stats["chords_unavailable"] = ",".join(miss)

    if vec_mode and src_sig != dst_sig:
        vke, vkr = vector_keysig(vobjs, vkinds, vinfo, geom, half,
                                 dst_sig, page, notex)
        edits.update(vke)
        redraw.extend(vkr)
        stats["keysig_vector"] = len(vke) + len(vkr)
    if src_sig != dst_sig and not vec_mode:
        plans, page_scale = keysig_plan(page, els, gmx, geom, notex_hdr, H,
                                        dst_sig, half)
        ks_scale = min(page_scale, scale if scale else 1.0)
        donor = accidental_donor(els, gmx, H, "b" if dst_sig < 0 else "#")
        ins, blanks, zones, undrawable = keysig_edits(data, plans, ks_scale,
                                                      half, H, dst_sig, donor)
        for p in undrawable:
            # No glyph of the needed sign anywhere in the document: draw the
            # signature with an installed music font instead.
            font = installed_music_font(page)
            if not font:
                continue
            glyph = "b" if dst_sig < 0 else "#"
            spacing = p["spacing"] * ks_scale
            for i, idx in enumerate(p["want"]):
                redraw.append({"x": p["lx"] + i * spacing,
                               "y": p["st"]["top"] + idx * half,
                               "text": glyph, "size": half * 8.0 * ks_scale,
                               "font": font, "keysig": True})
                stats["keysig_drawn"] += 1
        edits.update(blanks)
        for off, payload in ins:
            key = (off, off)
            edits[key] = edits.get(key, b"") + payload
            stats["keysig_added"] += 1
        stats["keysig_removed"] += len(blanks)
        if ks_scale < 1.0:
            stats["keysig_scale"] = round(ks_scale, 3)
        if not plans:
            vk = draw_keysig(page, geom, notex, src_sig, dst_sig, half, gm, pops)
            redraw.extend(vk)
            if vk:
                stats["keysig_drawn"] = len(vk)
        if zones:
            hs = header_shift(els, pops, gmx, geom, notex, H, zones)
            for k, v in hs.items():
                edits[k] = edits.get(k, b"") + v if k[0] == k[1] else v
            stats["header_shift"] = round(max(z[4] for z in zones), 2)

    redraw.extend(redraw_acc)
    for L in new_ledgers:
        redraw.append({"ledger": True, "x": L["x"], "y": L["y"],
                       "half": L["half"]})
    if new_ledgers:
        stats["ledgers_added"] = len(new_ledgers)
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
    activate_roles(doc)
    ks = keysig_scale(doc, s_sig, d_sig)
    if ks < 1.0:
        print(f"  key signature drawn at {ks:.0%} to fit the space the "
              f"engraver left")
    for i, page in enumerate(doc):
        new, st, redraw = transpose_page(page, steps, s_sig, d_sig, semis,
                                         a.verbose, scale=ks)
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
            if r.get("ledger"):
                w = r["half"] * 2.3          # a ledger overhangs the notehead
                page.draw_line((r["x"] - w * 0.35, r["y"]),
                               (r["x"] + w, r["y"]),
                               color=(0, 0, 0), width=r["half"] * 0.42)
                continue
            page.insert_text((r["x"], r["y"]), r["text"], fontsize=r["size"],
                             fontfile=r["font"],
                             fontname="cf" + str(abs(hash(r["font"])) % 9999))
        print(f"  page {i+1}: moved {st['glyph']} glyphs, "
              f"{sum(v for k, v in st.items() if k.startswith('path_') and k not in ('path_staffline','path_barline'))} subpaths")
    doc.save(out, garbage=0, deflate=True, clean=False)
    print("->", out)


if __name__ == "__main__":
    main()
