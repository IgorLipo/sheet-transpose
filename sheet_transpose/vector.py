"""Classification of fully-vector engraved music.

Some exports draw EVERYTHING as filled paths: noteheads, clefs, rests, even
the key signature. There is no text layer to read, so each painted path
object is classified by its geometry - and by its neighbours, because a flag
looks like a rest until you notice the stem it hangs from.

Bounding boxes here come from the paths' control points, which overshoot the
drawn curve; the thresholds below are calibrated for that.
"""
import collections

MOVING = {"notehead", "stem", "beam", "flag", "dot", "tie", "accidental"}


def group(pops):
    """Painted path objects: {pid: [PathOp]}."""
    objs = collections.defaultdict(list)
    for o in pops:
        objs[o.pid].append(o)
    return objs


def _bbox(ops, H):
    xs = [p[0] for o in ops for p in o.pts]
    ys = [H - p[1] for o in ops for p in o.pts]
    return min(xs), min(ys), max(xs), max(ys)


def classify(objs, geom, half, H):
    """{pid: kind} plus the notehead centres the rest of the pipeline needs.

    Pass 1 decides the self-evident shapes; pass 2 uses adjacency: a small
    curved shape beside a notehead is its accidental, a curl at the free end
    of a stem is a flag, and a similar curl with no stem anywhere near it is
    a rest and must never move.
    """
    kinds = {}
    heads, stems = [], []
    info = {}
    for pid, ops in objs.items():
        x0, y0, x1, y1 = _bbox(ops, H)
        w, h = x1 - x0, y1 - y0
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        curved = any(o.op in ("c", "v", "y") for o in ops)
        st = min(geom, key=lambda g: abs(cy - (g["top"] + g["bot"]) / 2))
        info[pid] = (x0, y0, x1, y1, w, h, cx, cy, curved, st)
        if h < 1.6 and w > 100:
            kinds[pid] = "staffline"
        elif h < 1.6 and w < 40:
            kinds[pid] = "ledger"
        elif w < 1.8 and h > 3 * half:
            # spans the staff = barline; shorter = stem
            if y0 <= st["top"] + 1.5 and y1 >= st["bot"] - 1.5 \
               and (y1 - y0) < st["half"] * 8.8:
                kinds[pid] = "barline"
            else:
                kinds[pid] = "stem"
                stems.append((cx, y0, y1))
        elif curved and 4.5 < w < 9.5 and 3 < h < 6.5:
            # notehead (control points overshoot the drawn ellipse)
            pos = (cy - st["top"]) / half
            if abs(pos - round(pos)) <= 0.35 and -12 <= round(pos) <= 20:
                kinds[pid] = "notehead"
                heads.append((cx, cy))
            else:
                kinds[pid] = "unknown"
        elif curved and w < 3.2 and h < 3.2:
            kinds[pid] = "dot"
        elif curved and 10 < w and h < 7:
            kinds[pid] = "tie"
        elif curved and w > 9 and h > 20:
            kinds[pid] = "clef"
        elif w > 3.5 and 1.6 <= h < 9 and not curved:
            kinds[pid] = "beam"
        else:
            kinds[pid] = "unknown"

    for pid, kind in list(kinds.items()):
        if kind != "unknown":
            continue
        x0, y0, x1, y1, w, h, cx, cy, curved, st = info[pid]
        near_stem = any(abs(sx - x0) < 2.5 or abs(sx - x1) < 2.5
                        for sx, sy0, sy1 in stems
                        if sy0 - 2 < cy < sy1 + 2)
        head_right = any(0 < hx - cx < 9 and abs(hy - cy) < half * 1.6
                         for hx, hy in heads)
        if curved and near_stem and 3 < h < 12:
            kinds[pid] = "flag"
        elif 1.8 < w < 6 and 4 < h < 12 and head_right:
            kinds[pid] = "accidental"
        elif 3 < h < 14 and not near_stem:
            kinds[pid] = "rest"
        else:
            kinds[pid] = "other"

    # key-signature accidentals: accidental-shaped objects in a staff header,
    # left of that staff's first notehead
    notex = {}
    for g in geom:
        gx = [hx for hx, hy in heads
              if abs(hy - (g["top"] + g["bot"]) / 2) < 40]
        notex[id(g)] = min(gx) if gx else g["x0"] + 60
    for pid, kind in list(kinds.items()):
        if kind not in ("accidental", "unknown", "other", "rest", "flag"):
            continue
        x0, y0, x1, y1, w, h, cx, cy, curved, st = info[pid]
        if curved and 1.8 < w < 7 and 4 < h < 14 \
           and abs(cy - (st["top"] + st["bot"]) / 2) < 24 \
           and st["x0"] + 5 < cx < min(notex[id(st)] - 8, st["x0"] + 95):
            kinds[pid] = "keysig"
    return kinds, heads, info
