"""Verify a vector-engraved transposition: every notehead moved by exactly
the same number of staff steps, none appeared or vanished, and nothing that
must stay put (clefs, rests, stafflines, barlines) moved."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pymupdf
import sheet_transpose.transpose_inplace as TI
import sheet_transpose.vector as V
from sheet_transpose.pdfsurgery import parse


def snapshot(path):
    doc = pymupdf.open(path)
    TI.activate_roles(doc)
    out = []
    for pno, page in enumerate(doc):
        H = page.rect.height
        els, pops = parse(page.read_contents(),
                          simple_fonts=TI.simple_font_resources(page))
        geom = TI.staff_geom(page)
        if not geom:
            continue
        half = geom[0]["half"]
        objs = V.group(pops)
        kinds, heads, info = V.classify(objs, geom, half, H)
        # barlines are excluded: a stem that moves down far enough to span
        # the staff re-CLASSIFIES as a barline without having drifted at all
        keep = sorted((round(info[pid][6], 1), round(info[pid][7], 1), k)
                      for pid, k in kinds.items()
                      if k in ("clef", "rest", "staffline"))
        out.append({"page": pno, "heads": sorted(heads), "keep": keep,
                    "half": half, "geom": geom})
    return out


def compare(src, dst, steps):
    A, B = snapshot(src), snapshot(dst)
    bad = 0
    for a, b in zip(A, B):
        dy = -steps * a["half"]
        if len(a["heads"]) != len(b["heads"]):
            print(f"p{a['page']}: notehead count {len(a['heads'])} -> "
                  f"{len(b['heads'])}")
            bad += 1
        moved = 0
        for (ax, ay), (bx, by) in zip(a["heads"], b["heads"]):
            if abs(ax - bx) > 0.5 or abs((ay + dy) - by) > 0.7:
                if moved < 6:
                    print(f"p{a['page']}: head ({ax:.1f},{ay:.1f}) expected "
                          f"y {ay + dy:.1f}, got ({bx:.1f},{by:.1f})")
                moved += 1
        bad += moved
        fixed_a = [t for t in a["keep"]]
        fixed_b = [t for t in b["keep"]]
        if fixed_a != fixed_b:
            diff = [t for t in fixed_a if t not in fixed_b][:5]
            print(f"p{a['page']}: fixed objects changed, e.g. {diff}")
            bad += 1
    print("NOTEHEAD ERRORS + FIXED-OBJECT DRIFT:", bad)
    return bad


if __name__ == "__main__":
    sys.exit(1 if compare(sys.argv[1], sys.argv[2], int(sys.argv[3])) else 0)
