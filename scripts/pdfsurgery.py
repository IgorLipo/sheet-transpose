"""In-place PDF transposition.

Re-engraving a score can never satisfy "identical except the key" - it relayouts
the page and drops text instructions, rehearsal marks and slash notation. So
instead of rebuilding the score, this edits the original PDF: every notehead
(with its stem, beam, accidental and ledger lines) moves vertically by the
transposition's diatonic step count, the key signature is redrawn, and chord
symbols are rewritten. Everything else is left exactly as it was.

A diatonic transposition moves EVERY note by the same number of staff steps, so
the geometry is one uniform translation - which is what makes this exact.
"""
import re, math

# PDF operators, longest first. Streams may run operators together with no
# whitespace ("lSQ", "TjET"), so a greedy [A-Za-z]+ would fuse them into one
# bogus token and the entire text layer would vanish.
_OPS = 'BDC|BMC|EMC|SCN|scn|B\\*|BI|BT|BX|CS|DP|Do|EI|ET|EX|ID|MP|RG|SC|T\\*|TD|TJ|TL|Tc|Td|Tf|Tj|Tm|Tr|Ts|Tw|Tz|W\\*|b\\*|cm|cs|d0|d1|f\\*|gs|re|ri|sc|sh|"|\'|B|F|G|J|K|M|Q|S|W|b|c|d|f|g|h|i|j|k|l|m|n|q|s|v|w|y'
TOKEN = re.compile((r"""
    (?P<hexstr><[0-9A-Fa-f\s]*>)
  | (?P<str>\((?:\\.|[^\\()])*\))
  | (?P<name>/[^\s/\[\]()<>{}]+)
  | (?P<num>[-+]?\d*\.?\d+)
  | (?P<arr>\[[^\]]*\])
  | (?P<op>""" + _OPS + r""")
""").encode(), re.VERBOSE)

# which operand slots of each path operator carry a y coordinate
Y_SLOTS = {"m": (1,), "l": (1,), "c": (1, 3, 5), "v": (1, 3), "y": (1, 3),
           "re": (1,)}
X_SLOTS = {"m": (0,), "l": (0,), "c": (0, 2, 4), "v": (0, 2), "y": (0, 2),
           "re": (0,)}
NOPERANDS = {"m": 2, "l": 2, "c": 6, "v": 4, "y": 4, "re": 4}


def tokenize(data):
    return [(m.lastgroup, m.group(0), m.start(), m.end())
            for m in TOKEN.finditer(data)]


def mat_mul(a, b):
    a0, a1, a2, a3, a4, a5 = a
    b0, b1, b2, b3, b4, b5 = b
    return (a0 * b0 + a1 * b2, a0 * b1 + a1 * b3,
            a2 * b0 + a3 * b2, a2 * b1 + a3 * b3,
            a4 * b0 + a5 * b2 + b4, a4 * b1 + a5 * b3 + b5)


def apply(m, x, y):
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


class Element:
    """Drawing done inside one innermost q...Q, in PDF user space."""
    __slots__ = ("inject_at", "qstart", "qend", "ctm", "ctm0", "kind",
                 "pts", "glyphs", "lw", "ops", "bt", "et", "standalone")

    def __init__(self, inject_at, ctm):
        self.inject_at = inject_at
        self.qstart = inject_at - 1   # the 'q' itself
        self.qend = None              # just after the matching 'Q'
        self.bt = self.et = None      # byte range of the BT...ET run, if any
        self.standalone = False       # text drawn outside any q...Q
        self.ctm = ctm
        self.ctm0 = ctm      # CTM at the 'q', before this block's own cm
        self.kind = None
        self.pts = []
        self.glyphs = []
        self.lw = 0.0
        self.ops = []

    def bbox(self):
        xs = [p[0] for p in self.pts]
        ys = [p[1] for p in self.pts]
        return (min(xs), min(ys), max(xs), max(ys)) if xs else None


class PathOp:
    """One path-construction operator, with byte ranges so it can be rewritten."""
    __slots__ = ("op", "slots", "ctm", "pts", "sub", "lw", "painted")

    def __init__(self, op, slots, ctm, pts, sub, lw):
        self.op = op
        self.slots = slots      # [(start, end, value)] per operand
        self.ctm = ctm
        self.pts = pts          # transformed points
        self.sub = sub          # subpath index
        self.lw = lw
        self.painted = ""


def parse(data, base_ctm=(1, 0, 0, 1, 0, 0)):
    """Return (elements, pathops)."""
    toks = tokenize(data)
    gstack = []
    ctm = base_ctm
    lw = 0.0
    cur = None
    elements, pathops = [], []
    operands = []                     # (value, start, end)
    tm = None
    font = None
    sub = -1
    standalone = False
    pend = []                         # path ops awaiting a paint operator

    for i, (kind, text, s, e) in enumerate(toks):
        if kind == "num":
            operands.append((float(text), s, e))
            continue
        if kind != "op":
            operands.append((text, s, e))
            continue
        op = text.decode("latin-1")
        vals = [o[0] for o in operands]

        if op == "q":
            gstack.append((ctm, lw, cur))
            cur = Element(e, ctm)
        elif op == "Q":
            if cur is not None and cur.kind:
                cur.qend = e
                elements.append(cur)
            if gstack:
                ctm, lw, cur = gstack.pop()
        elif op == "cm" and len(vals) >= 6:
            ctm = mat_mul(tuple(vals[-6:]), ctm)
            if cur is not None:
                cur.ctm = ctm
        elif op == "w" and vals:
            lw = vals[-1]
        elif op == "Tf":
            for j in range(i - 1, max(-1, i - 4), -1):
                if toks[j][0] == "name":
                    font = toks[j][1].decode("latin-1")
                    break
        elif op == "BT":
            if cur is None:
                # Text is not required to sit inside q...Q; plenty of writers
                # emit it at the top level. Treat the BT run itself as the
                # element so those glyphs are not silently dropped.
                cur = Element(s, ctm)
                cur.qstart = s
                cur.standalone = True
                standalone = True
            cur.bt = s
        elif op == "ET":
            if cur is not None:
                cur.et = e
                if standalone:
                    cur.qend = e
                    if cur.kind:
                        elements.append(cur)
                    cur = None
                    standalone = False
        elif op == "Tm" and len(vals) >= 6:
            tm = tuple(vals[-6:])
        elif op in ("Td", "TD") and len(vals) >= 2 and tm is not None:
            tm = mat_mul((1, 0, 0, 1, vals[-2], vals[-1]), tm)
        elif op == "Tj" and tm is not None and cur is not None:
            x, y = apply(mat_mul(tm, ctm), 0, 0)
            cid = None
            if i and toks[i - 1][0] == "hexstr":
                h = toks[i - 1][1][1:-1].replace(b" ", b"")
                cid = int(h, 16) if h else None
            elif i and toks[i - 1][0] == "str":
                # simple fonts show a literal string; the byte is the code
                body = toks[i - 1][1][1:-1]
                body = re.sub(rb"\\([0-7]{1,3})",
                              lambda m: bytes([int(m.group(1), 8) & 0xFF]), body)
                body = re.sub(rb"\\(.)", rb"\1", body)
                cid = body[0] if body else None
            cur.kind = cur.kind or "text"
            cur.glyphs.append((x, y, font, cid))
            cur.pts.append((x, y))
        elif op in NOPERANDS:
            n = NOPERANDS[op]
            if len(operands) >= n:
                slots = operands[-n:]
                if op == "m":
                    sub += 1
                pts = []
                if op == "re":
                    x, y, w, h = [v[0] for v in slots]
                    pts = [apply(ctm, x, y), apply(ctm, x + w, y),
                           apply(ctm, x, y + h), apply(ctm, x + w, y + h)]
                    sub += 1
                else:
                    vv = [v[0] for v in slots]
                    for k in range(0, len(vv), 2):
                        pts.append(apply(ctm, vv[k], vv[k + 1]))
                po = PathOp(op, slots, ctm, pts, sub, lw)
                pathops.append(po)
                pend.append(po)
                if cur is not None:
                    cur.kind = cur.kind or "path"
                    cur.pts.extend(pts)
        elif op in ("S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "n"):
            for po in pend:
                po.painted = op
            pend = []
            if cur is not None:
                cur.lw = abs(lw * math.hypot(ctm[0], ctm[1]))
                cur.ops.append(op)
        operands = []
    return elements, pathops


def edit(data, edits):
    """Apply {(start, end): replacement_bytes} and injections {offset: bytes}."""
    items = sorted(edits.items(), key=lambda kv: kv[0][0])
    out = bytearray()
    last = 0
    for (s, e), rep in items:
        if s < last:
            continue
        out += data[last:s]
        out += rep
        last = e
    out += data[last:]
    return bytes(out)


def fmt(v):
    return b"%.5f" % v
