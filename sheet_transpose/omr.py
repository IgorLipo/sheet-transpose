"""Extract notes + chords from Sibelius-exported PDF sheet music (text layer, no OMR)."""
import fitz, sys, json

NOTEHEADS = {'œ', 'ú', 'w', 'h', 'q', 'e', 'x'}  # notehead-bearing glyphs

# Sibelius ships several music-font families (Opus, Inkpen2, Reprise, ...).
# They share the same ASCII remapping, so match the family suffix, not a name.
MUSIC_FONT = ("Std", "MT", "Opus", "Inkpen", "Reprise", "Maestro", "Petrucci")


def is_music_font(f):
    return any(k in f for k in MUSIC_FONT) and "Arial" not in f and "Times" not in f


def is_chord_font(f):
    return "Chords" in f
STEPS = "CDEFGAB"


def staves(page):
    """Group the 5-line staff systems by y."""
    lines = []
    for it in page.get_drawings():
        for i in it["items"]:
            if i[0] == "l":
                a, b = i[1], i[2]
                if abs(a.y - b.y) < 0.5 and abs(a.x - b.x) > 100:
                    lines.append((a.y, min(a.x, b.x), max(a.x, b.x)))
            elif i[0] == "re":
                r = i[1]
                if r.height < 1.2 and r.width > 100:
                    lines.append(((r.y0 + r.y1) / 2, r.x0, r.x1))
    lines.sort()
    out, cur = [], []
    for ln in lines:
        if cur and ln[0] - cur[-1][0] > 8:
            if len(cur) == 5:
                out.append(cur)
            cur = []
        cur.append(ln)
    if len(cur) == 5:
        out.append(cur)
    return out


def glyphs(page):
    g = []
    for b in page.get_text("rawdict")["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                for c in s["chars"]:
                    if c["c"] == " ":
                        continue
                    g.append({"f": s["font"], "c": c["c"],
                              "x": c["origin"][0], "y": c["origin"][1],
                              "bb": c["bbox"], "size": s["size"]})
    return g


def pitch(y, st, key_alt):
    """y -> (step, octave, alter). st = the 5 staff-line y values, top first."""
    top = st[0][0]
    gap = (st[4][0] - st[0][0]) / 4.0
    half = gap / 2.0
    # top line F5 = diatonic index 0, increasing downward
    idx = round((y - top) / half)
    dia = 4 + 7 * 5 - idx          # F5 -> absolute diatonic number
    step = STEPS[dia % 7]
    octv = dia // 7
    return step, octv, key_alt.get(step, 0)


def run(path):
    doc = fitz.open(path)
    systems = []
    for pno in range(len(doc)):
        page = doc[pno]
        sts = staves(page)
        gs = glyphs(page)
        for si, st in enumerate(sts):
            y0, y1 = st[0][0], st[4][0]
            gap = (y1 - y0) / 4
            # notes: within +-6 staff-spaces of the staff
            band = [g for g in gs if y0 - 6 * gap < g["y"] < y1 + 6 * gap]
            # A real notehead sits on an exact half-step of the staff. Rehearsal
            # marks and text set in the music font drift off that grid, so the
            # residual is a reliable way to reject them.
            top, half = st[0][0], (st[4][0] - st[0][0]) / 8.0
            notes = []
            for g in band:
                if not is_music_font(g["f"]) or is_chord_font(g["f"]):
                    continue
                if g["c"] not in NOTEHEADS:
                    continue
                pos = (g["y"] - top) / half
                if abs(pos - round(pos)) > 0.28:      # off the staff grid
                    continue
                if not -7 <= round(pos) <= 15:        # implausible ledger range
                    continue
                notes.append(g)
            # Rhythm-slash bars ("play the chord") carry no noteheads; the
            # slashes are beat positions and are what chords anchor to.
            slashes = [g for g in band
                       if "Special" in g["f"] and g["c"] == "V"]
            chords = [g for g in band if is_chord_font(g["f"]) and g["y"] < y0]
            systems.append({"page": pno + 1, "staff": si, "st": st,
                            "notes": sorted(notes, key=lambda g: g["x"]),
                            "slashes": sorted(slashes, key=lambda g: g["x"]),
                            "chords": sorted(chords, key=lambda g: g["x"])})
    return systems


if __name__ == "__main__":
    for s in run(sys.argv[1]):
        print(f"--- p{s['page']} staff{s['staff']} notes={len(s['notes'])} chordglyphs={len(s['chords'])}")
