"""Rewrite chord symbols in place, reusing the document's own font glyphs.

The CID for each character and its advance width are learned from the chord
symbols already on the page, so the replacements are typeset in exactly the
same font, size and spacing as the originals.
"""
import re, os, glob, collections

# Sibelius chord fonts remap ASCII: these are the glyphs we must read/write.
# 0xA9 is the sharp; 0xAB is the DOUBLE sharp. Mapping a plain sharp onto
# 0xAB prints C double-sharp minor where C sharp minor belongs.
DECODE = {"‹": "m", "¨": "b", "Œ": "ma", "„": "j",
          "Š": "", "©": "#", "«": "##"}
ENCODE = {"m": "‹", "b": "¨", "#": "©"}

SEMI = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
FLAT_N = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
SHARP_N = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
STEPS = "CDEFGAB"


FLATG, SHARPG = "\u00a8", "\u00a9"     # chord-font flat / sharp glyphs
DBLSHARPG = "\u00ab"                    # 0xAB is the DOUBLE sharp, not sharp
FLAT_VARIANTS = {FLATG, "b", "\u00ac"}   # 0xAC: the small flat some faces use


def _alter(g):
    if g in FLAT_VARIANTS:
        return -1
    if g in (SHARPG, "#"):
        return 1
    if g == DBLSHARPG:
        return 2
    return 0


def _shift(letter, accg, steps, semis, flatg=FLATG, sharpg=SHARPG,
           flats=True):
    """Move one root/bass name, returning (letter, accidental_glyph_or_None).

    `flats` says how the destination key spells its black notes. Moving
    every letter by the same step count is right for the diatonic notes,
    but a chromatic root then comes out as whatever the arithmetic lands
    on - Gb minor in a chart whose key signature says F sharp minor.
    """
    pc = (SEMI[letter] + _alter(accg)) % 12
    new_letter = STEPS[(STEPS.index(letter) + steps) % 7]
    alter = ((pc + semis) - SEMI[new_letter]) % 12
    if alter > 6:
        alter -= 12
    # A chord root is named the way its KEY names that pitch. Carrying the
    # letter across by step count alone spells the black notes whichever way
    # the arithmetic falls, so a chart whose signature says F sharp minor
    # ends up printing G flat minor over it.
    if alter:
        want = (pc + semis) % 12
        name = (FLAT_N if flats else SHARP_N)[want]
        new_letter = name[0]
        alter = -1 if "b" in name else 1 if "#" in name else 0
    if alter == 0:
        return new_letter, None
    if alter == -1:
        return new_letter, flatg
    if alter == 1:
        return new_letter, sharpg
    if alter == 2:
        return new_letter, DBLSHARPG
    return new_letter, None          # double flat: give up gracefully


def transpose_pairs(glyphs, steps, semis, flatg=FLATG, sharpg=SHARPG,
                    flats=True):
    """Like transpose_glyphs, but each output char remembers which input
    position produced it, so a symbol whose glyphs arrived as separate
    strings can be written back string by string."""
    out, i, n = [], 0, len(glyphs)
    at_name = True
    while i < n:
        g = glyphs[i]
        if at_name and g in STEPS:
            accg = glyphs[i + 1] if i + 1 < n and glyphs[i + 1] in (
                FLATG, SHARPG, DBLSHARPG, "b", "#", "\u00ac") else None
            letter, newacc = _shift(g, accg, steps, semis, flatg, sharpg,
                                    flats)
            if accg == "\u00ac" and newacc == flatg:
                newacc = accg              # keep the face's own small flat
            out.append((letter, i))
            if newacc:
                out.append((newacc, i + 1 if accg else i))
            i += 2 if accg else 1
            at_name = False
            continue
        out.append((g, i))
        if g == "/":
            at_name = True
        elif g.isspace():
            at_name = True
        i += 1
    return out


def transpose_glyphs(glyphs, steps, semis, flatg=FLATG, sharpg=SHARPG,
                     flats=True):
    """Transpose a chord symbol at the glyph level.

    Only the root and bass letters (and their accidental glyphs) are rewritten;
    every quality glyph is copied through untouched. Decoding to text and back
    would corrupt ligatures - the 'm' of a 'maj7' ligature is not a minor sign.
    """
    out, i, n = [], 0, len(glyphs)
    at_name = True                    # a root is expected here
    while i < n:
        g = glyphs[i]
        if at_name and g in STEPS:
            accg = glyphs[i + 1] if i + 1 < n and glyphs[i + 1] in (
                FLATG, SHARPG, DBLSHARPG, "b", "#", "\u00ac") else None
            letter, newacc = _shift(g, accg, steps, semis, flatg, sharpg,
                                    flats)
            if accg == "\u00ac" and newacc == flatg:
                newacc = accg
            out.append(letter)
            if newacc:
                out.append(newacc)
            i += 2 if accg else 1
            at_name = False
            continue
        out.append(g)
        if g == "/":
            at_name = True            # what follows a slash is the bass note
        elif g.isspace():
            at_name = True            # a run can hold several chords
        i += 1
    return out


def learn(runs, font_adv=None):
    """Build cid/advance tables from the chord runs already in the document.

    runs: [(chars, cids, td_advances)] where td_advances are text-space deltas.

    A width is only observed for a character that has another one after it, so
    anything that only ever ends a symbol - the flat in a chart whose chords
    are all plain "B flat" - is never measured. Falling back to a generic width
    then leaves a visible gap when that character lands mid-symbol, so the
    embedded font's own advance fills the gaps when it can be read.
    """
    cid = collections.defaultdict(dict)   # per font resource: two subset
    adv = collections.defaultdict(list)    # fonts NEVER share glyph codes
    for chars, cids, deltas, res in runs:
        for ch, c in zip(chars, cids):
            if c is not None:
                cid[res][ch] = c
        for ch, dx in zip(chars, deltas):
            if dx is not None:
                adv[ch].append(round(dx, 3))
    width = {ch: collections.Counter(v).most_common(1)[0][0]
             for ch, v in adv.items()}
    # The observed Td deltas and the font's advances are in different units,
    # and which unit the deltas use depends on the export. Calibrate on the
    # characters seen both ways before trusting the font for the rest; mixing
    # the two scales inserts a gap twenty times too wide.
    allch = {ch for t in cid.values() for ch in t}
    if font_adv:
        both = [width[ch] / font_adv[ch] for ch in width if ch in font_adv]
        if both:
            k = sorted(both)[len(both) // 2]
            for ch in allch:
                if ch not in width and ch in font_adv:
                    width[ch] = round(font_adv[ch] * k, 3)
    return dict(cid), width


HEAD = re.compile(rb"(/\w+)\s+([\d.]+)\s+Tf")
FIRST_TD = re.compile(rb"([-\d.]+)\s+([-\d.]+)\s+Td")


def natural_width(text, width, fallback_w):
    return sum(width.get(ch, fallback_w) for ch in text)


def rebuild(bt_bytes, text, cid, width, fallback_w, squeeze=1.0, hexw=4):
    """A replacement BT...ET run drawing `text` at the original origin.

    `squeeze` horizontally condenses the symbol (via Tz) when the transposed
    name is wider than the gap to the next chord - 'Eb/G' needs more room than
    the 'F/A' it replaced.
    """
    fm = HEAD.search(bt_bytes)
    td = FIRST_TD.search(bt_bytes)
    if not fm or not td:
        return None
    res, size = fm.group(1), fm.group(2)
    x0, y0 = td.group(1), td.group(2)
    parts = [b"BT ", res, b" ", size, b" Tf 1 0 0 -1  0 0 Tm "]
    if squeeze < 0.999:
        parts.append(b"%.2f Tz " % (squeeze * 100))
    parts += [x0, b" ", y0, b" Td "]
    prev = None
    for ch in text:
        if ch not in cid:
            return None
        if prev is not None:
            # Td offsets are in UNSCALED text space, so Tz does not shrink them;
            # scale them here or the condensed glyphs would leave gaps.
            parts += [b"%.5f 0 Td " % (width.get(prev, fallback_w) * squeeze)]
        # A one-byte (simple or Type3) font reads hex strings byte by byte,
        # so a four-digit code would draw a NUL glyph before every character.
        parts += [b"<%0*X> Tj " % (hexw, cid[ch])]
        prev = ch
    parts.append(b"ET")
    if squeeze < 0.999:
        parts.append(b" BT 100 Tz ET")     # leave Tz as we found it
    return b"".join(parts)


FONT_DIRS = ["/Library/Fonts", os.path.expanduser("~/Library/Fonts"),
             "/System/Library/Fonts"]


def find_full_font(embedded_name):
    """Locate the installed, non-subsetted version of an embedded font.

    Sibelius subsets its chord fonts to only the characters the score printed,
    so a chart in Bbm has no 'C' glyph. The complete faces ship with Sibelius
    and are normally installed system-wide; borrow one when the subset falls
    short.
    """
    stem = embedded_name.split("+")[-1]
    # Sibelius names the embedded subset without the "Std" the installed file
    # carries (Inkpen2Chords vs Inkpen2ChordsStd.otf), so try both spellings.
    names = [stem, stem + "Std", stem.replace("Std", "")]
    for d in FONT_DIRS:
        for nm in names:
            for ext in ("otf", "ttf", "OTF", "TTF"):
                hit = os.path.join(d, f"{nm}.{ext}")
                if os.path.exists(hit):
                    return hit
    return None
