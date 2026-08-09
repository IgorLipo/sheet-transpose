#!/usr/bin/env python3
"""Transpose a lead-sheet PDF to a new key and re-engrave it as PDF + MusicXML.

Pipeline:
  1. Clarity-OMR (PDF -> MusicXML)  gives pitches + rhythm + barlines
  2. PDF text layer (Sibelius/Finale music fonts) gives the chord symbols,
     which Clarity-OMR does not emit
  3. Merge, transpose, re-engrave with verovio

Usage:
  transpose.py INPUT.pdf --to Cm [-o OUT.pdf] [--from Bbm] [--clarity score.musicxml]
"""
import argparse, os, re, sys, subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pymupdf, music21 as m21
from omr import run, STEPS, is_music_font, is_chord_font
from rhythm import features

SEMI = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
FLAT_N = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
SHARP_N = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
KEY_FLATS = ["B", "E", "A", "D", "G", "C", "F"]
KEY_SHARPS = ["F", "C", "G", "D", "A", "E", "B"]
# keys whose signature uses sharps (major names)
SHARP_KEYS = {"G", "D", "A", "E", "B", "F#", "C#"}

# Chord-font glyph -> text. Sibelius "Opus"/"Inkpen" chord fonts remap ASCII.
CHORD_GLYPH = {
    "¨": "b",      # flat
    "‹": "m",      # minor
    "Œ": "ma",     # maj ligature
    "„": "j",
    "Š": "",
    "«": "#",      # sharp (some fonts)
}


# ---------------------------------------------------------------- key helpers
def parse_key(name):
    """'Cm' / 'Bbm' / 'Eb' -> (tonic_pc, is_minor, n_accidentals, uses_flats)."""
    m = re.fullmatch(r"([A-G])([b#]?)(m|min|minor)?", name.strip())
    if not m:
        raise SystemExit(f"bad key: {name!r} (use e.g. Cm, Bbm, Eb, F#m)")
    root, acc, minor = m.group(1), m.group(2), bool(m.group(3))
    pc = (SEMI[root] + (1 if acc == "#" else -1 if acc == "b" else 0)) % 12
    # relative major for signature purposes
    maj_pc = (pc + 3) % 12 if minor else pc
    order = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
    maj = order[maj_pc]
    sig = {"C": 0, "G": 1, "D": 2, "A": 3, "E": 4, "B": 5, "Gb": -6, "Db": -5,
           "Ab": -4, "Eb": -3, "Bb": -2, "F": -1, "F#": 6, "C#": 7}
    n = sig.get(maj, 0)
    return pc, minor, n, n <= 0


def key_alter_map(nsig):
    """Accidentals implied by a key signature: n<0 flats, n>0 sharps."""
    if nsig < 0:
        return {s: -1 for s in KEY_FLATS[:abs(nsig)]}
    return {s: 1 for s in KEY_SHARPS[:nsig]}


def detect_source_key(pdf):
    """Read the key signature from the engraved staff: count flat/sharp glyphs
    between the clef and the time signature on the first system."""
    doc = pymupdf.open(pdf)
    page = doc[0]
    sysl = run(pdf)
    if not sysl:
        raise SystemExit("no staves found")
    st = sysl[0]["st"]
    y0, y1 = st[0][0], st[4][0]
    flats = sharps = 0
    for b in page.get_text("rawdict")["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                if not is_music_font(s["font"]) or is_chord_font(s["font"]):
                    continue
                for c in s["chars"]:
                    if not (y0 - 12 < c["origin"][1] < y1 + 12):
                        continue
                    if c["origin"][0] > st[0][1] + 60:   # past the key sig
                        continue
                    if c["c"] == "b":
                        flats += 1
                    elif c["c"] == "#":
                        sharps += 1
    return -flats if flats else sharps


# ------------------------------------------------------------------- chords
def parse_chords(glyphs):
    """Cluster chord-font glyphs into symbols, left to right."""
    out, cur = [], []
    for g in glyphs:
        newroot = bool(cur) and g["c"] in "ABCDEFG" and cur[-1]["c"] != "/"
        if cur and (g["x"] - cur[-1]["bb"][2] > 3 or newroot):
            out.append(cur)
            cur = []
        cur.append(g)
    if cur:
        out.append(cur)
    res = []
    for cl in out:
        s = "".join(CHORD_GLYPH.get(g["c"], g["c"]) for g in cl)
        if s.strip():
            res.append((round(cl[0]["x"], 1), s))
    return res


def transpose_chord(sym, semis, flats=True):
    if not sym:
        return sym
    i = 2 if len(sym) > 1 and sym[1] in "b#" else 1
    root, rest = sym[:i], sym[i:]
    base = SEMI[root[0]] + (-1 if root[1:] == "b" else 1 if root[1:] == "#" else 0)
    tbl = FLAT_N if flats else SHARP_N
    new = tbl[(base + semis) % 12]
    if "/" in rest:
        q, bass = rest.split("/", 1)
        bi = 2 if len(bass) > 1 and bass[1] in "b#" else 1
        bb = SEMI[bass[0]] + (-1 if bass[1:bi] == "b" else 1 if bass[1:bi] == "#" else 0)
        rest = q + "/" + tbl[(bb + semis) % 12] + bass[bi:]
    return new + rest


def safe_chord(fig):
    """ChordSymbol for `fig`, degrading rather than crashing.

    Chord fonts carry suffix glyphs music21 cannot parse (Sibelius writes sus2
    as a superscript ligature). Keep the root so the chart stays usable and
    show the original text.
    """
    try:
        return m21.harmony.ChordSymbol(fig)
    except Exception:
        m = re.match(r"([A-G][b#-]?)", fig)
        cs = m21.harmony.ChordSymbol(m.group(1) if m else "C")
        cs.chordKindStr = fig
        return cs


def m21_figure(sym):
    """music21 spells flats as '-': Bbm -> B-m, Dbmaj7/Ab -> D-maj7/A-."""
    out, i = "", 0
    while i < len(sym):
        c = sym[i]
        out += c
        if c in "ABCDEFG" and i + 1 < len(sym) and sym[i + 1] == "b":
            out += "-"
            i += 1
        i += 1
    return out


def align_measures(src, xml_meas):
    """Pair PDF measures with engraved measures.

    Clarity-OMR can drop or merge bars (especially on handwritten-style fonts),
    so a positional zip misplaces every chord after the first discrepancy.
    Align on the note-count sequence with a small edit-distance DP instead, and
    only pair bars that actually correspond.
    """
    a = [len(m["notes"]) for m in src]
    b = [len([n for n in m.notes if isinstance(n, m21.note.Note)])
         for m in xml_meas]
    n, k = len(a), len(b)
    INF = float("inf")
    # cost[i][j] = best cost aligning a[i:] with b[j:]
    cost = [[INF] * (k + 1) for _ in range(n + 1)]
    cost[n][k] = 0
    for i in range(n, -1, -1):
        for j in range(k, -1, -1):
            if i == n and j == k:
                continue
            best = INF
            if i < n and j < k:                      # match
                best = min(best, abs(a[i] - b[j]) + cost[i + 1][j + 1])
            if i < n:                                 # src bar unmatched
                best = min(best, 3 + cost[i + 1][j])
            if j < k:                                 # xml bar unmatched
                best = min(best, 3 + cost[i][j + 1])
            cost[i][j] = best
    pairs, i, j = [], 0, 0
    while i < n and j < k:
        if cost[i][j] == abs(a[i] - b[j]) + cost[i + 1][j + 1]:
            pairs.append((src[i], xml_meas[j]))
            i, j = i + 1, j + 1
        elif cost[i][j] == 3 + cost[i + 1][j]:
            i += 1
        else:
            j += 1
    return pairs


def source_layout(pdf):
    """Per source measure: its noteheads and the chord symbols above it."""
    doc = pymupdf.open(pdf)
    out = []
    for s in run(pdf):
        feat = features(doc[s["page"] - 1], s["st"])
        edges = [s["st"][0][1] - 2] + sorted(feat["bars"])
        chords = parse_chords(s["chords"])
        notes = s["notes"]
        slashes = s.get("slashes", [])
        for k in range(len(edges) - 1):
            lo, hi = edges[k], edges[k + 1]
            mn = [g for g in notes if lo < g["x"] <= hi]
            ms = [g for g in slashes if lo < g["x"] <= hi]
            if mn or ms:
                out.append({"notes": mn, "slashes": ms,
                            "chords": [c for c in chords if lo < c[0] <= hi]})
        lo = edges[-1]
        mn = [g for g in notes if g["x"] > lo]
        ms = [g for g in s.get("slashes", []) if g["x"] > lo]
        if mn or ms:
            out.append({"notes": mn, "slashes": ms,
                        "chords": [c for c in chords if c[0] > lo]})
    return out


def build_native(pdf, semis, target, out_xml, title=None, src_sig=0):
    """Engrave melody + chords entirely from the PDF's own vector/text layer.

    Preferred path for digitally-engraved scores: pitches are measured from
    staff geometry rather than recognised, so it does not care whether the font
    is clean (Opus) or handwritten-style (Inkpen2), where OMR degrades badly.
    """
    from native import extract
    _, _, tgt_sig, tgt_flats = parse_key(target)
    mel = extract(pdf, src_sig)
    src = source_layout(pdf)

    sc = m21.stream.Score()
    part = m21.stream.Part()
    part.insert(0, m21.clef.TrebleClef())
    part.insert(0, m21.key.KeySignature(tgt_sig))
    part.insert(0, m21.meter.TimeSignature("4/4"))

    nch = 0
    for i, mm in enumerate(mel):
        m = m21.stream.Measure(number=i + 1)
        off = 0.0
        for step, octv, alter, dur in mm["notes"]:
            n = m21.note.Note()
            n.pitch.step, n.pitch.octave = step, octv
            if alter:
                n.pitch.accidental = m21.pitch.Accidental(alter)
            n.quarterLength = max(dur, 0.0625)
            m.insert(off, n)
            off += n.quarterLength
        if off < 4.0 - 1e-6:
            m.insert(off, m21.note.Rest(quarterLength=4.0 - off))
        # chords for this bar, anchored to the nearest notehead by x
        if i < len(src):
            sm = src[i]
            anchors = sm["notes"] or sm.get("slashes") or []
            beats = [n.offset for n in m.notes]
            for cx, sym in sorted(sm["chords"]):
                if anchors and beats:
                    j = min(range(len(anchors)),
                            key=lambda t: abs(anchors[t]["x"] - cx))
                    beat = beats[min(j, len(beats) - 1)]
                else:
                    beat = 0.0
                cs = safe_chord(
                    m21_figure(transpose_chord(sym, semis, tgt_flats)))
                cs.writeAsChord = False
                m.insert(beat, cs)
                nch += 1
        part.append(m)

    sc.insert(0, part)
    # Transpose the NOTES only. Chord symbols were transposed as they were
    # inserted, so a whole-score transpose would shift them a second time.
    iv = m21.interval.Interval(semis)
    for n in list(part.recurse().notes):
        if isinstance(n, m21.note.Note):
            n.pitch = n.pitch.transpose(iv)
    for msr in part.getElementsByClass('Measure'):
        for old in msr.getElementsByClass(m21.key.KeySignature):
            msr.remove(old)
    part.getElementsByClass('Measure')[0].insert(0, m21.key.KeySignature(tgt_sig))
    want_sharp = not tgt_flats
    for n in part.recurse().notes:
        if isinstance(n, m21.note.Note) and n.pitch.accidental is not None:
            nm = n.pitch.accidental.name
            if (nm == "sharp" and not want_sharp) or (nm == "flat" and want_sharp):
                n.pitch = n.pitch.getEnharmonic()

    sc.metadata = m21.metadata.Metadata()
    sc.metadata.title = f"{title or os.path.basename(pdf)}  ({target})"
    sc.write("musicxml", fp=out_xml)
    bad = sum(1 for m in mel if not m["ok"])
    return len(mel), nch, bad


def build_chord_chart(pdf, semis, target, out_xml, title=None, src_sig=0):
    """Engrave a chords-only chart straight from the PDF text layer.

    Used when the source is mostly rhythm slashes, where OMR has no noteheads
    to read and would emit empty bars. Every measure becomes a slash bar with
    its chord symbols, so nothing depends on the OMR result.
    """
    _, _, tgt_sig, tgt_flats = parse_key(target)
    src = source_layout(pdf)
    sc = m21.stream.Score()
    part = m21.stream.Part()
    part.insert(0, m21.clef.TrebleClef())
    part.insert(0, m21.key.KeySignature(tgt_sig))
    part.insert(0, m21.meter.TimeSignature("4/4"))
    from native import extract
    mel = extract(pdf, src_sig)
    iv = m21.interval.Interval(semis)

    for i, sm in enumerate(src, 1):
        m = m21.stream.Measure(number=i)
        anchors = sm["notes"] or sm.get("slashes") or []
        # A slash chart can still contain written-out melody bars; engrave the
        # real notes where they exist instead of throwing the melody away.
        mm = mel[i - 1] if i - 1 < len(mel) else None
        if sm["notes"] and mm and mm["notes"]:
            off = 0.0
            for step, octv, alter, dur in mm["notes"]:
                n = m21.note.Note()
                n.pitch.step, n.pitch.octave = step, octv
                if alter:
                    n.pitch.accidental = m21.pitch.Accidental(alter)
                n.pitch = n.pitch.transpose(iv)
                n.quarterLength = max(dur, 0.0625)
                m.insert(off, n)
                off += n.quarterLength
            if off < 4.0 - 1e-6:
                m.insert(off, m21.note.Rest(quarterLength=4.0 - off))
        else:
            for b in range(4):
                r = m21.note.Rest(quarterLength=1)
                r.style.hideObjectOnPrint = True
                m.insert(float(b), r)
        chs = sorted(sm["chords"])
        for k, (cx, sym) in enumerate(chs):
            if anchors:
                j = min(range(len(anchors)), key=lambda t: abs(anchors[t]["x"] - cx))
                beat = round(4.0 * j / max(len(anchors), 1) * 2) / 2
            else:
                beat = k * (4.0 / max(len(chs), 1))
            beat = min(3.5, max(0.0, beat))
            cs = safe_chord(m21_figure(transpose_chord(sym, semis, tgt_flats)))
            cs.writeAsChord = False
            m.insert(beat, cs)
        part.append(m)
    sc.insert(0, part)
    # respell accidentals to suit the target signature (G# -> Ab in a flat key)
    want_sharp = not tgt_flats
    for n in part.recurse().notes:
        if isinstance(n, m21.note.Note) and n.pitch.accidental is not None:
            nm = n.pitch.accidental.name
            if (nm == "sharp" and not want_sharp) or (nm == "flat" and want_sharp):
                n.pitch = n.pitch.getEnharmonic()
    sc.metadata = m21.metadata.Metadata()
    sc.metadata.title = f"{title or os.path.basename(pdf)}  ({target})"
    sc.write("musicxml", fp=out_xml)
    return len(src), sum(len(m["chords"]) for m in src)


# --------------------------------------------------------------- clarity omr
def run_clarity(pdf, workdir, clarity_dir, python_bin):
    xml = os.path.join(workdir, "clarity.musicxml")
    if os.path.exists(xml):
        return xml
    cmd = [python_bin, "omr.py", os.path.abspath(pdf), "-o", os.path.abspath(xml),
           "--device", "cpu"]
    r = subprocess.run(cmd, cwd=clarity_dir, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(xml):
        sys.stderr.write(r.stdout[-3000:] + r.stderr[-3000:])
        raise SystemExit("Clarity-OMR failed")
    return xml


# ------------------------------------------------------------------- render
ACC_CPS = ("", "", "", "", "", "")


def render_pdf(xml, out_pdf, scale=36):
    import verovio, cairosvg
    tk = verovio.toolkit()
    tk.setOptions({
        "pageWidth": 2100, "pageHeight": 2970, "scale": scale,
        "adjustPageHeight": False, "header": "auto", "footer": "none",
        "spacingStaff": 14, "spacingSystem": 24, "harmDist": 2.0,
        "spacingLinear": 0.30, "spacingNonLinear": 0.55,
        "smuflTextFont": "linked",
    })
    if not tk.loadFile(xml):
        raise SystemExit("verovio could not load " + xml)
    doc = pymupdf.open()
    tmp = os.path.join(os.path.dirname(out_pdf) or ".", "_svg")
    os.makedirs(tmp, exist_ok=True)
    for p in range(1, tk.getPageCount() + 1):
        svg = tk.renderToSVG(p)
        # chord accidentals must be set in a real SMuFL text font or cairo
        # renders them as tofu boxes
        parts = []
        for chunk in re.split(r"(<tspan[^>]*>[^<]*</tspan>)", svg):
            if chunk.startswith("<tspan") and any(c in chunk for c in ACC_CPS):
                if "font-family=" in chunk:
                    chunk = re.sub(r'font-family="[^"]*"',
                                   'font-family="Bravura Text"', chunk, count=1)
                else:
                    chunk = chunk.replace("<tspan",
                                          '<tspan font-family="Bravura Text"', 1)
            parts.append(chunk)
        f = os.path.join(tmp, f"p{p}.svg")
        open(f, "w").write("".join(parts))
        doc.insert_pdf(pymupdf.open("pdf", cairosvg.svg2pdf(url=f)))
    doc.save(out_pdf)
    return tk.getPageCount()


# --------------------------------------------------------------------- main
def build(pdf, clarity_xml, semis, target, out_xml, title=None):
    tgt_pc, tgt_minor, tgt_sig, tgt_flats = parse_key(target)
    score = m21.converter.parse(clarity_xml)
    part = score.parts[0]
    xml_meas = list(part.getElementsByClass('Measure'))
    src = source_layout(pdf)

    pairs = align_measures(src, xml_meas)
    pending = []
    for sm, xm in pairs:
        xnotes = [n for n in xm.notes if isinstance(n, m21.note.Note)]
        pn = sm["notes"]
        for cx, sym in sm["chords"]:
            fig = transpose_chord(sym, semis, tgt_flats)
            if xnotes and pn:
                j = min(range(len(pn)), key=lambda i: abs(pn[i]["x"] - cx))
                beat = xnotes[min(j, len(xnotes) - 1)].offset
            else:
                beat = 0.0        # empty/rest bar: put the chord on beat 1
            pending.append((xm, beat, fig))

    # Clarity repeats clef + time signature on every system; keep only the first
    for i, m in enumerate(xml_meas):
        if i == 0:
            continue
        for cls in (m21.clef.Clef, m21.meter.TimeSignature):
            for obj in list(m.getElementsByClass(cls)):
                m.remove(obj)

    score.transpose(m21.interval.Interval(semis), inPlace=True)

    # respell accidentals to match the target signature
    want_sharp = not tgt_flats
    for n in part.recurse().notes:
        if isinstance(n, m21.note.Note) and n.pitch.accidental is not None:
            nm = n.pitch.accidental.name
            if (nm == "sharp" and not want_sharp) or (nm == "flat" and want_sharp):
                n.pitch = n.pitch.getEnharmonic()

    # a mostly-rest bar can collapse several chords onto one beat; spread them
    grouped = {}
    for xm, beat, sym in pending:
        grouped.setdefault(id(xm), (xm, []))[1].append((beat, sym))
    for xm, items in grouped.values():
        items.sort()
        if len({b for b, _ in items}) < len(items):
            step = 4.0 / len(items)
            items = [(round(i * step, 3), s) for i, (_, s) in enumerate(items)]
        for beat, sym in items:
            cs = safe_chord(m21_figure(sym))
            cs.writeAsChord = False
            xm.insert(beat, cs)

    for m in part.getElementsByClass('Measure'):
        for old in m.getElementsByClass(m21.key.KeySignature):
            m.remove(old)
    part.getElementsByClass('Measure')[0].insert(
        0, m21.key.KeySignature(tgt_sig))

    score.metadata = m21.metadata.Metadata()
    score.metadata.title = f"{title or os.path.basename(pdf)}  ({target})"
    score.write("musicxml", fp=out_xml)
    return len(xml_meas), len(pending)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--to", required=True, help="target key, e.g. Cm")
    ap.add_argument("--from", dest="src_key", help="source key (default: auto)")
    ap.add_argument("-o", "--out", help="output PDF path")
    ap.add_argument("--title")
    ap.add_argument("--engine", choices=["native", "omr"], default="native",
                    help="native = PDF geometry (default); omr = Clarity-OMR")
    ap.add_argument("--chords-only", action="store_true",
                    help="ignore melody; engrave a slash chord chart")
    ap.add_argument("--clarity-dir",
                    default=os.path.expanduser("~/.claude/skills/sheet-transpose/Clarity-OMR"))
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--workdir", default=None)
    a = ap.parse_args()

    out_pdf = a.out or re.sub(r"\.pdf$", "", a.pdf) + f"-{a.to}.pdf"
    workdir = a.workdir or os.path.join(os.path.dirname(os.path.abspath(out_pdf)),
                                        "_transpose_work")
    os.makedirs(workdir, exist_ok=True)

    # source key
    if a.src_key:
        _, _, src_sig, _ = parse_key(a.src_key)
        src_pc, src_minor = parse_key(a.src_key)[0], parse_key(a.src_key)[1]
    else:
        src_sig = detect_source_key(a.pdf)
        # infer tonic from the signature, assuming minor (lead sheets usually are)
        maj_from_sig = {0: "C", -1: "F", -2: "Bb", -3: "Eb", -4: "Ab", -5: "Db",
                        -6: "Gb", 1: "G", 2: "D", 3: "A", 4: "E", 5: "B", 6: "F#"}
        maj = maj_from_sig.get(src_sig, "C")
        src_pc = (SEMI[maj[0]] + (-1 if maj[1:] == "b" else 1 if maj[1:] == "#" else 0) - 3) % 12
        src_minor = True

    tgt_pc, tgt_minor, tgt_sig, tgt_flats = parse_key(a.to)
    semis = (tgt_pc - src_pc) % 12
    if semis > 6:
        semis -= 12

    src_name = a.src_key or (FLAT_N[src_pc] + "m")
    print(f"source key: {src_name} (sig {src_sig})  ->  {a.to} (sig {tgt_sig})"
          f"   shift {semis:+d} semitones")

    out_xml = re.sub(r"\.pdf$", "", out_pdf) + ".musicxml"

    # Decide the mode: a chart that is mostly rhythm slashes has no melody for
    # the OMR to read, so build the chord chart straight from the text layer.
    layout = source_layout(a.pdf)
    # Count measures, not glyphs: a few melody bars can carry more noteheads
    # than a long stretch of 4-slash chord bars, which the glyph ratio hides.
    slash_bars = sum(1 for m in layout if m.get("slashes"))
    chords_only = a.chords_only or slash_bars >= 0.3 * max(len(layout), 1)

    if chords_only:
        print(f"chord-chart mode ({slash_bars}/{len(layout)} bars are rhythm slashes)")
        nmeas, nch = build_chord_chart(a.pdf, semis, a.to, out_xml, a.title, src_sig)
    elif a.engine == "omr":
        xml = run_clarity(a.pdf, workdir, a.clarity_dir, a.python)
        nmeas, nch = build(a.pdf, xml, semis, a.to, out_xml, a.title)
    else:
        nmeas, nch, bad = build_native(a.pdf, semis, a.to, out_xml, a.title, src_sig)
        print(f"native mode: {nmeas - bad}/{nmeas} bars resolved cleanly")
        if bad:
            print(f"  {bad} bar(s) had uncertain rhythm - check those")
    pages = render_pdf(out_xml, out_pdf)
    print(f"measures={nmeas} chords={nch} pages={pages}")
    print("PDF     :", out_pdf)
    print("MusicXML:", out_xml)


if __name__ == "__main__":
    main()
