"""PDF-native melody extraction: pitch + duration straight from the vector /
text layer of a digitally-engraved score. No OMR, no rasterising.

Why this beats OMR here: in a Sibelius/Finale/MuseScore PDF the noteheads are
font glyphs at exact coordinates and the staff lines are vector graphics, so
pitch is *measured*, not recognised. Every notehead lands on an exact integer
half-step of the staff. Durations come from the glyph identity plus beams and
flags, then get reconciled against the time signature so each bar adds up.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pymupdf
from omr import run, STEPS, is_music_font, is_chord_font
from rhythm import features, stem_for, beams_at

# Sibelius music fonts remap ASCII. Base duration by glyph, in quarter notes.
GLYPH_DUR = {
    "w": 4.0,     # whole
    "": 2.0,     # half  (U+2D9)
    "œ": 1.0,     # quarter or shorter; beams/flags shorten it
    "": 1.0,
    "": 1.0,
}
DOT = ("", "")     # augmentation dot glyphs
FLAGS = {"j", "J", ""}   # flag glyphs seen in Opus/Inkpen
# Accidental glyphs printed before a notehead (Sibelius ASCII remapping).
ACCIDENTAL = {"b": -1, "": -1, "#": 1, "": 1, "": 0, "n": 0}


def collect_glyphs(page, st):
    """Every non-chord music glyph near this staff, with coordinates."""
    y0, y1 = st[0][0], st[4][0]
    gap = (y1 - y0) / 4
    lo, hi = y0 - 8 * gap, y1 + 8 * gap
    out = []
    for b in page.get_text("rawdict")["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                if not is_music_font(s["font"]) or is_chord_font(s["font"]):
                    continue
                for c in s["chars"]:
                    if c["c"] == " " or not (lo < c["origin"][1] < hi):
                        continue
                    out.append({"c": c["c"], "x": c["origin"][0],
                                "y": c["origin"][1], "bb": c["bbox"],
                                "f": s["font"]})
    return sorted(out, key=lambda g: g["x"])


KEY_FLAT_ORDER = ["B", "E", "A", "D", "G", "C", "F"]
KEY_SHARP_ORDER = ["F", "C", "G", "D", "A", "E", "B"]


def key_alters(nsig):
    """Accidentals implied by a key signature (negative = flats)."""
    if nsig < 0:
        return {s: -1 for s in KEY_FLAT_ORDER[:abs(nsig)]}
    return {s: 1 for s in KEY_SHARP_ORDER[:nsig]}


def pitch_at(y, st):
    """Staff position -> (step, octave). Top line of a treble staff is F5."""
    top = st[0][0]
    half = (st[4][0] - top) / 8.0
    idx = round((y - top) / half)
    dia = 38 - idx                     # 38 = diatonic index of F5
    return STEPS[dia % 7], dia // 7, idx


def base_duration(note, feat, glyphs):
    """Duration in quarters from glyph + beams + flags + dots."""
    d = GLYPH_DUR.get(note["c"], 1.0)
    if d >= 2.0:
        dur = d
    else:
        st = stem_for(note, feat)
        nb = beams_at(st[0], feat) if st else 0
        if nb <= 0:
            # unbeamed: look for a flag glyph riding the stem
            nf = 0
            if st:
                for g in glyphs:
                    if g["c"] in FLAGS and abs(g["x"] - st[0]) < 4:
                        nf += 1
            nb = nf
        dur = 1.0 / (2 ** nb) if nb else 1.0
    # augmentation dot sits just right of the notehead, same line/space
    for g in glyphs:
        if g["c"] in DOT and 0 < g["x"] - note["bb"][2] < 8 \
           and abs(g["y"] - note["y"]) < 2.0:
            dur *= 1.5
            break
    return dur


def durations_from_spacing(notes, lo, hi, total=4.0):
    """Infer durations from horizontal spacing.

    Engravers space notes proportionally to their duration, so within one bar
    the x-gaps are a direct read on relative note values. This is far more
    robust than counting beams, because a beam edge that is a fraction of a
    point inside the stem makes a whole note-value vanish. Gaps are snapped to
    the nearest power-of-two fraction and normalised to fill the bar.
    """
    if not notes:
        return []
    xs = [n["x"] for n in notes] + [hi]
    gaps = [xs[i + 1] - xs[i] for i in range(len(notes))]
    span = sum(gaps)
    if span <= 0:
        return [total / len(notes)] * len(notes)
    raw = [g / span * total for g in gaps]
    # Snap to real note values. Clamping matters: on a grand staff the two
    # staves interleave in x, which produces near-zero gaps that would
    # otherwise snap to absurd durations MusicXML cannot represent.
    values = [4.0, 3.0, 2.0, 1.5, 1.0, 0.75, 0.5, 0.375, 0.25, 0.125, 0.0625]
    snapped = [min(values, key=lambda v: abs(v - max(r, 0.0625))) for r in raw]
    s = sum(snapped)
    if abs(s - total) < 1e-6:
        return snapped
    # nudge the single worst-fitting note to close a small residual
    if 0 < abs(s - total) <= 1.0:
        diff = total - s
        j = max(range(len(snapped)), key=lambda i: abs(snapped[i] - raw[i]))
        cand = snapped[j] + diff
        if cand > 0 and abs(cand * 4 - round(cand * 4)) < 1e-9:
            snapped[j] = cand
            return snapped
    # Fall back to a strict 16th grid. Scaling by an arbitrary ratio produces
    # durations like 0.203125 that MusicXML has no note type for, so quantise
    # instead and let the caller pad the bar.
    return [max(round(v * 4) / 4, 0.25) for v in snapped]


def reconcile(durs, total=4.0):
    """Make a bar sum to `total`.

    Beam/flag counting is the weak step (a beam endpoint can be missed), but the
    time signature is a hard constraint, so scale to fit when we are close and
    report the bar as uncertain when we are not.
    """
    s = sum(durs)
    if not durs or abs(s - total) < 1e-6:
        return durs, True
    if 0 < s < total * 4:
        r = total / s
        # only trust a clean power-of-two correction
        for cand in (0.5, 1.0, 2.0):
            if abs(r - cand) < 0.02:
                return [d * cand for d in durs], True
    return durs, False


def extract(pdf, nsig=0):
    """-> list of measures: {'notes':[(step,octave,alter,dur)], 'ok':bool}

    `nsig` is the source key signature (negative = flats); it supplies the
    accidental each step carries so the caller gets real sounding pitches.
    """
    alters = key_alters(nsig)
    doc = pymupdf.open(pdf)
    measures = []
    for s in run(pdf):
        page = doc[s["page"] - 1]
        st = s["st"]
        feat = features(page, st)
        glyphs = collect_glyphs(page, st)
        # Drop the clef/key-signature zone: those flats and sharps are not note
        # accidentals and would otherwise alter the first notes of each system.
        first_note_x = min((g["x"] for g in s["notes"]), default=st[0][1])
        glyphs = [g for g in glyphs if g["x"] >= first_note_x - 16]
        edges = [st[0][1] - 2] + sorted(feat["bars"]) + [st[0][2] + 2]
        for k in range(len(edges) - 1):
            lo, hi = edges[k], edges[k + 1]
            notes = [g for g in s["notes"] if lo < g["x"] <= hi]
            if not notes:
                continue
            # Explicit accidentals sit immediately left of their notehead on the
            # same line/space, and hold for the rest of the bar.
            bar_acc = {}
            out = []
            for n in notes:
                step, octv, _ = pitch_at(n["y"], st)
                acc = None
                for g in glyphs:
                    if g["c"] not in ACCIDENTAL:
                        continue
                    if 0 < n["x"] - g["x"] < 14 and abs(g["y"] - n["y"]) < 1.6:
                        acc = ACCIDENTAL[g["c"]]
                        break
                if acc is not None:
                    bar_acc[(step, octv)] = acc
                alter = bar_acc.get((step, octv), alters.get(step, 0))
                out.append([step, octv, alter, 0.0])
            glyph_durs = [base_duration(n, feat, glyphs) for n in notes]
            durs, ok = reconcile(glyph_durs)
            if not ok:
                # glyph/beam reading did not add up; spacing is the better
                # signal because it degrades gracefully instead of dropping a
                # whole note value when a beam edge is missed
                durs = durations_from_spacing(notes, lo, hi)
                ok = abs(sum(durs) - 4.0) < 1e-6
            for i, d in enumerate(durs):
                out[i][3] = d
            measures.append({"notes": [tuple(o) for o in out], "ok": ok})
    return measures


if __name__ == "__main__":
    ms = extract(sys.argv[1])
    good = sum(1 for m in ms if m["ok"])
    print(f"measures={len(ms)} valid={good} ({100*good//max(len(ms),1)}%)")
    for i, m in enumerate(ms[:8], 1):
        print(i, "ok" if m["ok"] else "??",
              " ".join(f"{s}{'b'*-a}{'#'*a}{o}/{d}" for s, o, a, d in m["notes"]))
