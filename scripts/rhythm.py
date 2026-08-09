"""Extract note durations: beams, stems, flags, dots, barlines, rests."""
import fitz

BEAM_H = (1.8, 3.2)     # beam fill thickness range (pt)


def features(page, st):
    """Collect beams / stems / barlines for one staff system."""
    y0, y1 = st[0][0], st[4][0]
    gap = (y1 - y0) / 4
    lo, hi = y0 - 7 * gap, y1 + 7 * gap
    beams, stems, bars = [], [], []
    for it in page.get_drawings():
        r = it["rect"]
        if not (lo < (r.y0 + r.y1) / 2 < hi):
            continue
        lw = it.get("width") or 0
        if it["type"] in ("f", "fs"):
            h = r.y1 - r.y0
            if BEAM_H[0] <= h <= BEAM_H[1] and r.x1 - r.x0 > 4:
                beams.append((r.x0, r.x1, r.y0, r.y1))
        for i in it["items"]:
            if i[0] == "l":
                a, b = i[1], i[2]
                if abs(a.x - b.x) < 0.7 and abs(a.y - b.y) > 3:
                    ylo, yhi = min(a.y, b.y), max(a.y, b.y)
                    # Barlines span the staff exactly and are drawn thicker
                    # (0.78pt) than stems (0.47pt); that width is the reliable
                    # discriminator since stems can also cross the whole staff.
                    if lw > 0.6 and abs(ylo - y0) < 1.0 and abs(yhi - y1) < 1.0:
                        bars.append(a.x)
                    elif abs(a.y - b.y) > 3:
                        stems.append((a.x, ylo, yhi))
    return {"beams": beams, "stems": stems, "bars": sorted(bars), "gap": gap}


def beams_at(x, feat):
    """How many beams cross this stem x -> beam count."""
    n = 0
    for (bx0, bx1, by0, by1) in feat["beams"]:
        if bx0 - 1.0 <= x <= bx1 + 1.0:
            n += 1
    return n


def stem_for(note, feat):
    """Find the stem attached to a notehead.

    The glyph bbox is the full em-box, not the notehead. The notehead centre is
    at the glyph origin; the stem touches its left (down) or right (up) edge.
    """
    nx0 = note["bb"][0]
    nx1 = nx0 + (note["bb"][2] - nx0)
    cy = note["y"]
    best = None
    for (sx, ylo, yhi) in feat["stems"]:
        if nx0 - 1.5 <= sx <= nx1 + 1.5 and ylo - 3 <= cy <= yhi + 3:
            d = min(abs(sx - nx0), abs(sx - nx1))
            if best is None or d < best[1]:
                best = ((sx, ylo, yhi), d)
    return best[0] if best else None
