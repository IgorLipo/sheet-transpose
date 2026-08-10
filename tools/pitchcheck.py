"""Compare the sounding pitch of every note before and after a transposition.

Notes are paired by horizontal position, which a vertical transposition never
changes. Pairing by list index instead desynchronises the moment the two files
disagree on note count, which makes every later note look wrong.
"""
import sys, os, collections
sys.path.insert(0, os.path.expanduser(
    "~/Projects/il_projects/sheet-transpose/sheet_transpose"))
import pymupdf
from omr import staves, norm_glyph, is_music_font, is_chord_font, NOTEHEADS
from pdfsurgery import parse
import transpose_inplace as TI

STEPS = "CDEFGAB"
SEMI = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
FLAT_ORDER = ["B", "E", "A", "D", "G", "C", "F"]
SHARP_ORDER = ["F", "C", "G", "D", "A", "E", "B"]
ACCID = {"b": -1, "#": 1, "n": 0}


def key_alters(sig):
    if sig < 0:
        return {s: -1 for s in FLAT_ORDER[:abs(sig)]}
    return {s: 1 for s in SHARP_ORDER[:sig]}


def read_notes(path, sig, base=38):
    """base is the diatonic number of the top staff line: 38 = F5 (treble).

    Accidentals persist to the end of the bar on their own staff position,
    exactly as a player reads them.
    """
    alters = key_alters(sig)
    doc = pymupdf.open(path)
    out = []
    import bisect
    for pno, page in enumerate(doc):
        sts = sorted(staves(page), key=lambda t: t[0][0])
        # One authoritative glyph list: the low-level trace sees every
        # glyph, with its true origin, in every export dialect. rawdict
        # silently drops whole runs and the parser's decoded text only
        # exists for single-byte fonts, so mixing the two counted some
        # glyphs twice and missed others entirely.
        glyphs = []
        for sp in page.get_texttrace():
            if not sp.get("chars"):
                continue
            fn = sp["font"].split("+")[-1]
            if not is_music_font(fn) or is_chord_font(fn):
                continue
            for ucs, _g, org, _b in sp["chars"]:
                if ucs == 32:
                    continue
                glyphs.append((org[0], org[1], norm_glyph(chr(ucs))))
        # short horizontal segments = ledger lines, used to arbitrate staff
        # ownership for notes floating between two staves
        ledgers = []
        for d in page.get_drawings():
            for it in d["items"]:
                if it[0] == "l":
                    a2, b2 = it[1], it[2]
                    if abs(a2.y - b2.y) < 1.2 and 4 < abs(a2.x - b2.x) < 26:
                        ledgers.append(((a2.x + b2.x) / 2, a2.y))

        def ledger_count(x, y0, y1):
            # count LEVELS, not strokes: an engraver often draws one ledger
            # line as two overlapping segments
            lo, hi = min(y0, y1), max(y0, y1)
            return len({round(ly * 2) / 2 for lx, ly in ledgers
                        if abs(lx - x) < 8 and lo - 1 < ly < hi + 1})

        # Assign every glyph to ONE staff. Nearest-midpoint alone flips a
        # coin for a note floating midway between the staves of a grand
        # system; the ledger lines it would need settle the question - a
        # treble D3 without four ledger lines under the staff cannot exist.
        owner = {}
        for gi, g in enumerate(glyphs):
            gx, gy = g[0], g[1]
            best, bestscore = None, None
            for k, st_ in enumerate(sts):
                top_, bot_ = st_[0][0], st_[4][0]
                half_ = (bot_ - top_) / 8.0
                pos = (gy - top_) / half_
                idx_ = round(pos)
                if abs(pos - idx_) > 0.38 or not -14 <= idx_ <= 22:
                    continue
                if idx_ <= -2:
                    exp = len(range(-2, idx_ - 1, -2))
                    got = ledger_count(gx, top_ + (idx_ - 0.5) * half_,
                                       top_ - half_)
                elif idx_ >= 10:
                    exp = len(range(10, idx_ + 1, 2))
                    got = ledger_count(gx, bot_ + half_,
                                       top_ + (idx_ + 0.5) * half_)
                else:
                    exp = got = 0
                score = (abs(exp - got),
                         abs(gy - (top_ + bot_) / 2))
                if bestscore is None or score < bestscore:
                    best, bestscore = k, score
            if best is None:
                best = min(range(len(sts)), key=lambda k: abs(
                    gy - (sts[k][0][0] + sts[k][4][0]) / 2))
            owner.setdefault(best, []).append(g)
        # Barlines per staff, for accidental persistence - read with the
        # transposer's own finder so the two cannot disagree about where a
        # bar ends and an accidental stops applying.
        geom_ = TI.staff_geom(page)
        _els, _pops = parse(page.read_contents(),
                            simple_fonts=TI.simple_font_resources(page))
        allbars = TI.barlines(
            geom_, _pops, page.rect.height, geom_[0]["half"],
            heads=[(gx, gy) for gx, gy, gc in glyphs
                   if gc in NOTEHEADS]) if geom_ else []
        for si, st in enumerate(sts):
            top, bot = st[0][0], st[4][0]
            half = (bot - top) / 8.0
            mid = (top + bot) / 2
            band = sorted(owner.get(si, []))
            # a grand staff mixes clefs, so read each staff's own
            sbase = base
            for gx, gy, gc in band:
                if gx < st[0][1] + 30 and gc in ("&", "?"):
                    sbase = 38 if gc == "&" else 26
                    break
            # anything left of the first notehead minus an accidental's
            # width is key signature; a fixed clef-side offset disowns the
            # accidentals of a system's very first note
            first_head = min((x for x, y, ch in band if ch in NOTEHEADS),
                             default=st[0][1] + 60)
            accs = [(x, y, ch) for x, y, ch in band
                    if ch in ACCID and x > first_head - 14]
            barstate = {}
            claimed = set()
            for x, y, ch in band:
                if ch not in NOTEHEADS:
                    continue
                idx = round((y - top) / half)
                if not -14 <= idx <= 22:
                    continue
                dia = sbase - idx
                step, octv = STEPS[dia % 7], dia // 7
                bar = bisect.bisect(allbars[si], x)
                skey = (si, bar, idx)
                # A double flat is two flat glyphs SIDE BY SIDE, so they sum;
                # but two glyphs at the same x are one glyph seen twice, and
                # one glyph is one alteration however the key signature reads.
                # Walk LEFT from the notehead, taking only glyphs that touch
                # the one before: that is what makes a double accidental one
                # symbol, and what keeps the previous note's sign out of it.
                # Walk LEFT from the notehead, taking only glyphs that touch
                # the one before: that is what makes a double accidental one
                # symbol, and what keeps a neighbouring note's sign out of it.
                # Nothing may be counted twice - a chord's notes each claim
                # their own - so the nearest unclaimed glyph wins.
                # An accidental belongs to the NEXT notehead on its own
                # line, so take the nearest unclaimed one to the left and
                # let a touching neighbour join it (a double accidental is
                # two glyphs side by side). Anything further left belongs to
                # an earlier note, however close this note's chord sits.
                near = sorted(((ax, ay, ACCID[ac]) for ax, ay, ac in accs
                               if 0 < x - ax < 26
                               and abs(ay - y) < half * 0.55
                               and (ax, ay) not in claimed), reverse=True)
                uniq, edge = [], None
                for ax, ay, a in near:
                    if edge is not None and edge - ax > 8.5:
                        break
                    if not uniq or abs(uniq[-1][0] - ax) > 1.5:
                        uniq.append((ax, a))
                        claimed.add((ax, ay))
                    edge = ax
                if uniq:
                    alt = (sum(a for _x, a in uniq)
                           if len(uniq) > 1 and len({a for _x, a in uniq}) == 1
                           else uniq[0][1])
                    barstate[skey] = alt
                elif skey in barstate:
                    alt = barstate[skey]       # earlier sign still in force
                else:
                    alt = alters.get(step, 0)
                    barstate[skey] = alt
                out.append({"page": pno, "sys": si, "x": round(x, 2),
                            "y": round(y, 2), "idx": idx,
                            "name": step + ("b" * -alt if alt < 0 else "#" * alt) + str(octv),
                            "midi": (octv + 1) * 12 + SEMI[step] + alt})
    return out


def midi_name(m):
    n = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
    return n[m % 12] + str(m // 12 - 1)


def compare(src, dst, src_sig, dst_sig, semis, verbose=True):
    a = read_notes(src, src_sig)
    b = read_notes(dst, dst_sig)
    pool = collections.defaultdict(list)
    for n in b:
        pool[(n["page"], n["x"])].append(n)
    bad, unmatched = [], []
    for o in a:
        cand = pool.get((o["page"], o["x"]), [])
        if not cand:
            unmatched.append(o)
            continue
        # the partner is the one nearest the expected new staff position
        g = min(cand, key=lambda n: abs(n["midi"] - (o["midi"] + semis)))
        cand.remove(g)
        if g["midi"] != o["midi"] + semis:
            bad.append((o, g, o["midi"] + semis))
    extra = [n for v in pool.values() for n in v]
    if verbose:
        print("original notes: %d    transposed notes: %d" % (len(a), len(b)))
        print("WRONG PITCH : %d" % len(bad))
        print("UNMATCHED   : %d (original notes with no note at that x)" % len(unmatched))
        print("EXTRA       : %d (notes in the output with no original)" % len(extra))
        if bad:
            print()
            print("  page sys x        original   got      expected")
            for o, g, want in bad[:40]:
                print("  %-4d %-3d %-8.1f %-10s %-8s %s"
                      % (o["page"], o["sys"], o["x"], o["name"], g["name"],
                         midi_name(want)))
        if extra:
            print()
            print("  extra notes:", [(n["x"], n["y"], n["name"]) for n in extra[:8]])
    return bad, unmatched, extra


if __name__ == "__main__":
    src, dst = sys.argv[1], sys.argv[2]
    # Read both key signatures off the pages. Taking them from the command line
    # lets a wrong-key transposition pass: feed the checker the same mistaken
    # source key the transposer used and every note agrees with it.
    ssig = TI.source_key_signature(src)
    dsig = TI.source_key_signature(dst)
    semis = int(sys.argv[3]) if len(sys.argv) > 3 else None
    if len(sys.argv) > 4:
        want = int(sys.argv[4])
        if want != ssig:
            print("SOURCE KEY MISMATCH: page says %d accidentals, caller said %d"
                  % (ssig, want))
    print("source key sig %d -> output key sig %d" % (ssig, dsig))
    if semis is None:
        print("no semitone count given; pass it to check pitches")
        sys.exit(0)
    bad, un, extra = compare(src, dst, ssig, dsig, semis)
    sys.exit(1 if bad else 0)
