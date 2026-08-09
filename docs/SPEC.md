# Sheet-music PDF transposer — build spec

Transpose an engraved sheet-music PDF into a different key **by editing the
original PDF**, so the output is identical to the input except for the key.

This is NOT optical music recognition. Do not rasterise the page. Do not
re-engrave the score. Those approaches destroy the layout and every text
instruction, which is exactly what this must preserve.

---

## 1. The core insight

Engraved PDFs (exported from Sibelius, Finale, MuseScore) are **not pictures**:

- **Noteheads are font glyphs** placed at exact coordinates
- **Staff lines are vector graphics** (long thin horizontal strokes)
- **Chord symbols are real text** in a chord font
- Sibelius music fonts remap ASCII, e.g. `œ` = quarter notehead, `w` = whole

So pitch is **measured, not recognised**. Verified on real files: every
notehead lands on an **exact integer half-step** of the staff (residual
< 0.01 pt).

**And the key fact that makes the whole job tractable:**

> A diatonic transposition moves EVERY note by the SAME number of staff steps.

Dm → Cm is "down one diatonic step" for all 204 notes. So the geometry is a
single uniform vertical translation. Everything else on the page can be left
byte-for-byte untouched.

---

## 2. What changes vs what must not

| Changed | Left completely alone |
|---|---|
| noteheads | staff lines |
| stems, beams | barlines |
| ties, slurs | clefs, time signatures |
| articulations (accents, staccato) | rests |
| ledger lines (**recomputed**, not moved) | slash bars, repeat signs, D.S., segno |
| key signature (accidentals added/removed) | rehearsal marks, bar numbers |
| chord symbols (retypeset) | ALL text instructions, titles, page layout |

---

## 3. Algorithm

### 3.1 Parse the content stream

Tokenize the page content stream and simulate the graphics state so every
drawing operator's page position is known.

- Track the CTM through `q` / `Q` / `cm`
- Text position comes from `Tm` then the chain of `Td` offsets
- Record for each glyph: page (x, y), font resource, CID
- Record for each path operator: its operand **byte ranges**, its CTM, and its
  transformed points, grouped into subpaths (a new subpath starts at each `m`)

Coordinate note: the page-level matrix is typically
`0.06 0 0 -0.06 0 842 cm` (a scale + y-flip). PDF user space has y **up**;
most PDF libraries report page coords with y **down**. Convert with
`y_page = page_height - y_user`.

Validation checkpoint: your parsed glyph positions must match the library's own
text-extraction origins. On the reference file this was **365/401 exact**
(the 36 extras are spaces, which text extraction drops).

### 3.2 Find the staves

Staff lines = long horizontal strokes (width > 100 pt, height < 1.2 pt).
Group them into fives. Then:

```
interline = (line[4].y - line[0].y) / 4      # e.g. 4.960 pt
half_step = interline / 2                    # e.g. 2.480 pt  <- one diatonic step
```

### 3.3 Pitch from geometry

```
idx = round((glyph_y - staff_top) / half_step)   # 0 = top line, +1 per step down
# treble: top line = F5 -> diatonic 38;  bass: top line = A3 -> diatonic 26
dia = base - idx
step, octave = "CDEFGAB"[dia % 7], dia // 7
alter = key_signature_alteration(step)           # plus any in-bar accidental
```

**A notehead always lands on an integer `idx`.** Use that as a filter: reject
any glyph whose residual `|idx - round(idx)| > 0.28` — that removes rehearsal
marks and text set in the music font (this caught a phantom "Ab6" that was
actually the boxed letter "A").

### 3.4 Classify what moves

**Glyphs** — by character, and crucially by *positional variance*:

- Noteheads (`w` whole, `˙` half, `œ` quarter/eighth) → **MOVE**
- Articulations (`^` accent, `.` staccato) → **MOVE**
- Accidentals attached to a note → **MOVE**
- Clefs, rests, time signatures, slash marks, repeat dots → **STAY**
  (these sit at *fixed* staff positions; noteheads vary — that is the
  discriminator, not the glyph alone)
- Key-signature accidentals → **replaced separately** (see 3.6)
- Anything in a Chords / Script / Text font → **STAY**

**Paths** — by subpath geometry:

- horizontal, long, y matches a staff line → **staff line, STAY**
- vertical, spans exactly staff top→bottom, **line width ~0.78 pt** →
  **barline, STAY**
- vertical, thinner (**~0.47 pt**) → **stem, MOVE**
  (line width is the reliable discriminator: stems also cross the whole staff)
- filled paths (`f` / `f*`) → **beams, MOVE**
- bezier curves (`c`) → **ties / slurs, MOVE**
- short horizontal, outside the staff → **ledger line, RECOMPUTE** (see 3.5)

### 3.5 Ledger lines must be recomputed, not moved

Ledger lines only exist at **line positions** (even `idx` outside 0..8).
Translating one by a single step lands it in a space — visibly wrong.

For each ledger, find the note it serves, compute that note's new index, then
work out which slots the new stack needs:

```
above:  idx <= -2  ->  slots -2, -4, ... down to idx
below:  idx >= 10  ->  slots 10, 12, ... up to idx
```

Reassign surviving lines by rank; **delete surplus ones** by collapsing every
x-coordinate onto the subpath's start x (a zero-length butt-capped stroke paints
nothing). Example: a note on ledger −4 moving down one step becomes −3, which
sits in the space, so the −4 line must vanish while −2 stays.

### 3.6 Key signature

Compute source and target signatures, then add or remove accidentals.

Standard staff positions (treble, `idx` from top line; **add 2 for bass clef**):

```
flats:   Bb=4  Eb=1  Ab=5  Db=2  Gb=6  Cb=3  Fb=7
sharps:  F#=0  C#=3  G#=-1 D#=2  A#=5  E#=1  B#=4
```

Order: flats `B E A D G C F`, sharps `F C G D A E B`.
Horizontal spacing between accidentals ≈ **1.13 × interline** (5.6 pt when
interline is 4.96) — measured from real Sibelius output.

**To add one:** clone the existing accidental's `q…Q` block and prepend a
matrix that repositions it. Insert at the block's own start offset, where the
CTM is known:

```
A = target_matrix × inverse(ctm_at_that_point)
emit:  q <A> cm <cloned block bytes> Q
```

Cloning guarantees identical font, size and colour. **To remove one:** blank
its bytes.

Detecting which accidentals are the key signature: they sit far left of the
first note (`x < first_note_x - 12`); an accidental belonging to a note hugs it.

### 3.7 Making room for a wider key signature

Going from 1 flat to 3 needs ~11 pt that the engraving does not have. Shift the
header content (time signature, repeat sign) right — **but cap the shift** so
nothing is ever pushed on top of the first note:

```
cap = first_note_x - rightmost_header_element_right_edge - 1.0
shift = min(needed, cap)
```

Compute this **per system**, not globally — one tight system otherwise starves
every other. Never move the music itself.

### 3.8 Chord symbols

Transpose **at the glyph level**. Do not decode to text and re-encode:
chord fonts use ligatures, and the `m` in a `maj7` ligature is not a minor sign.
(This bug turned `Dbmaj7` into `D♭‹aj7`.)

Parse the raw glyph run as: `root [accidental] quality… [ / bass [accidental] ]`.
Rewrite only the root and bass letters plus their accidental glyphs; **copy
every quality glyph through untouched.**

Chord-font glyph mappings (Sibelius): `¨` = flat, `«` = sharp, `‹` = minor,
`Œ „ Š` = the `maj` ligature parts.

Retypeset by rewriting the `BT … ET` run with new CIDs and advances. Learn the
CID and advance width for each character **from the chord symbols already in
the document**, so the replacements match exactly.

**Condense when needed:** `Eb/G` is wider than the `F/A` it replaces. If the new
symbol would collide with the next chord, apply horizontal scaling `Tz`
(floor ~78%). Note `Td` offsets are in *unscaled* text space, so `Tz` does not
shrink them — multiply the advances by the squeeze factor yourself.

**Subset-font trap:** embedded fonts contain only the characters the score
actually printed. A chart in Bbm has **no `C` glyph**, so `Cm` cannot be typeset
from it. Detect this, and fall back to the full font installed on the system
(`OpusChordsStd.otf`, `Inkpen2ChordsStd.otf`) — blank the old symbol and redraw
with the complete face.

### 3.9 Write back

Apply all edits as **byte-range splices** on the original stream:

- moving glyph → insert ` 1 0 0 1 0 <dy> cm ` right after its `q`
- moving path → rewrite the y operand of each coordinate
- `dy_local = dy_user / ctm[3]`, and `dy_user = -dy_page` (y-up vs y-down)

Everything not spliced keeps its original bytes — that is what guarantees
fidelity. Save without cleaning or garbage-collecting the PDF.

---

## 4. Verification (do all four — do not claim success without them)

1. **Note count and shift** — every note moved by exactly the same number of
   staff steps, count unchanged.
2. **Annotations byte-identical** — extract all text-font glyphs with positions
   from both files; the sorted lists must be equal.
3. **Key signature** — correct number of accidentals at the correct staff
   indices on every staff.
4. **Render page 1 to PNG and actually look at it** — check for collisions,
   tofu boxes instead of ♭, stray sharps in a flat key, misplaced ledger lines.

Reference result (Madonna "Like A Prayer", piano grand staff, Dm → Cm):

```
204 notes            -> all moved exactly 1 diatonic step down
333 annotation glyphs -> byte-identical position AND character
key signature         -> 1 flat to 3 flats on all 17 staves
56 chord symbols      -> Dm->Cm, C/D->Bb/C, Gm/D->Fm/C, F/A->Eb/G, Bb->Ab ...
pitch check           -> D-F-A (Dm triad) -> C-Eb-G (Cm triad)
```

---

## 5. Interface

```
transpose.py IN.pdf --from Dm --to Cm -o OUT.pdf
```

- `--from` optional: auto-detect by counting key-signature accidentals between
  the clef and the first note
- Report per page: glyphs moved, subpaths moved, chords rewritten, key-signature
  accidentals added/removed, and any chords left untransposed

---

## 6. Dependencies and the iOS question

Reference implementation is **Python + PyMuPDF** (~1060 lines across 6 modules),
runtime ~1 s for a 2-page score. PyMuPDF is used for two things only:

1. reading/writing the raw content stream
2. resolved geometry (glyph origins, vector drawings with CTMs applied)

**PyMuPDF is a compiled C extension.** If it is unavailable (e.g. on iOS), the
algorithm still holds — but the read/write layer must be rebuilt on a
pure-Python PDF library (`pypdf` or `pikepdf`). Concretely you would need to:

- get each page's content stream bytes (both libraries can)
- **reimplement the geometry layer yourself**: the tokenizer and CTM tracking
  described in 3.1 are already hand-written and library-independent; what you
  lose is the cross-check against the library's text extraction, and font
  glyph-advance lookups for chord retypesetting
- decompress/recompress the stream (FlateDecode)

Rendering to PNG for the visual check needs a rasteriser; without one, rely on
the three numeric checks and inspect the output on a PDF viewer.

---

## 7. Known limits

- **Engraver-exported PDFs only.** A scan has no text layer — this cannot work
  on one; that genuinely needs OMR.
- Embedded fonts are subsetted (see 3.8).
- Very tight systems: a wider key signature may sit close to what follows. The
  music is never moved.
- Non-diatonic alterations are handled via the key signature; scores with many
  in-music accidentals need spot-checking.
- Assumes a treble/bass single- or grand-staff layout. Verified on both a
  single-staff lead sheet and a piano grand staff.
