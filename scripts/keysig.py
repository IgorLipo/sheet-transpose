"""Key-signature surgery: add or remove accidentals on an engraved staff.

The existing accidental glyph is cloned rather than drawn from scratch, so the
added flats are the same font, size and colour as the ones already on the page.
"""
FLAT_ORDER = ["B", "E", "A", "D", "G", "C", "F"]
SHARP_ORDER = ["F", "C", "G", "D", "A", "E", "B"]
# staff position (0 = top line) of each accidental in a treble key signature
FLAT_POS = {"B": 4, "E": 1, "A": 5, "D": 2, "G": 6, "C": 3, "F": 7}
SHARP_POS = {"F": 0, "C": 3, "G": -1, "D": 2, "A": 5, "E": 1, "B": 4}
BASS_OFFSET = 2          # bass clef sits two diatonic steps lower


def inverse(m):
    a, b, c, d, e, f = m
    det = a * d - b * c
    if abs(det) < 1e-12:
        return None
    return (d / det, -b / det, -c / det, a / det,
            (c * f - d * e) / det, (b * e - a * f) / det)


def mat_mul(a, b):
    a0, a1, a2, a3, a4, a5 = a
    b0, b1, b2, b3, b4, b5 = b
    return (a0 * b0 + a1 * b2, a0 * b1 + a1 * b3,
            a2 * b0 + a3 * b2, a2 * b1 + a3 * b3,
            a4 * b0 + a5 * b2 + b4, a4 * b1 + a5 * b3 + b5)


def positions(sig, bass):
    """Staff indices for the accidentals of a key signature, in written order."""
    if sig == 0:
        return []
    order = FLAT_ORDER if sig < 0 else SHARP_ORDER
    table = FLAT_POS if sig < 0 else SHARP_POS
    off = BASS_OFFSET if bass else 0
    return [table[n] + off for n in order[:abs(sig)]]


def clone(data, el, dx_user, dy_user):
    """Bytes that redraw element `el` displaced by (dx_user, dy_user).

    Inserted at el.qstart, where the CTM is el.ctm0, so the clone's matrix is
    chosen to land the glyph exactly where we want regardless of nesting.
    """
    inv = inverse(el.ctm0)
    if inv is None:
        return b""
    target = (el.ctm[0], el.ctm[1], el.ctm[2], el.ctm[3],
              el.ctm[4] + dx_user, el.ctm[5] + dy_user)
    a = mat_mul(target, inv)
    inner = data[el.qstart:el.qend]
    return (b" q %.6f %.6f %.6f %.6f %.6f %.6f cm " % a) + inner + b" Q "
