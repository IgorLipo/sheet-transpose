---
name: sheet-transpose
description: Transpose sheet-music PDFs to a different key. The default mode edits the original PDF in place so the output is identical to the input except for the key - every text instruction, rehearsal mark, slash bar, repeat and D.S. survives. Use when the user gives a music PDF and asks to change/transpose the key, get it "in Cm", make it easier to sing/play, or extract the notes and chords from a score. Handles Sibelius/Finale-exported PDFs (lead sheets and piano grand staves).
---

# Sheet music transposer

## Default: in-place transposition (`transpose_inplace.py`)

```bash
SK=~/.claude/skills/sheet-transpose
$SK/venv/bin/python $SK/scripts/transpose_inplace.py IN.pdf --from Dm --to Cm -o OUT.pdf -v
```

Edits the original PDF rather than re-engraving it. A diatonic transposition
moves every note by the same number of staff steps, so the whole job is one
uniform translation of the noteheads plus a new key signature:

| Changed | Untouched (bytes never edited) |
|---|---|
| noteheads, stems, beams, ties, articulations | staff lines, barlines, clefs, rests |
| ledger lines (recomputed, not moved) | text instructions, rehearsal marks, titles |
| key signature (accidentals added/removed) | slash bars, repeats, D.S., segno, bar numbers |
| chord symbols (retypeset in the score's own font) | page layout, system breaks, spacing |

**This is the only mode that can satisfy "identical except the key".** Every OMR
tool re-engraves, which relayouts the page and discards annotations.

Verified on `Like A Prayer` (Madonna, piano grand staff, Dm to Cm):

```
204 notes  -> all moved exactly 1 diatonic step down
333 annotation glyphs -> byte-identical position and character
key signature -> 1 flat to 3 flats on all 17 staves
56 chord symbols -> Dm/C/D/Gm/D/F/A/Bb... -> Cm/Bb/C/Fm/C/Eb/G/Ab...
```

### How it works

The PDF content stream is tokenized and the graphics state simulated, so every
drawing operator's page position is known. Noteheads are font glyphs at exact
coordinates and staff lines are vector graphics, so pitch is *measured*: every
notehead lands on an exact integer half-step of the staff. Moving elements get a
translation spliced in; everything else keeps its original bytes.

- `scripts/pdfsurgery.py` - content-stream tokenizer, CTM tracking, byte-level splicing
- `scripts/keysig.py` - key-signature accidental placement; clones the score's own glyph
- `scripts/chords.py` - glyph-level chord transposition (preserves `maj7` ligatures)

### Limits

- **Engraver-exported PDFs only** (Sibelius/Finale/MuseScore). A scan has no
  text layer; use the re-engraving mode below.
- **Embedded fonts are subsetted.** If the score never printed a letter, that
  glyph does not exist and the affected chord is left alone - the run prints
  `chords_unavailable` listing them. (Not an issue on scores that use all seven
  letters.)
- **A wider key signature needs room.** Going to more accidentals shifts the
  time signature / repeat sign right, capped so nothing is pushed onto a note.
  On tightly engraved systems the key signature can end up close to what
  follows; the music itself is never moved.
- Non-diatonic transposition of individual accidentals is handled by the key
  signature only; scores with many in-music accidentals need spot-checking.

---

## Fallback: re-engraving (`transpose.py`)

Rebuilds the score from scratch (melody + chords) when the source has no usable
text layer. Layout and annotations are NOT preserved.

## Usage


```bash
SK=~/.claude/skills/sheet-transpose
$SK/venv/bin/python $SK/scripts/transpose.py INPUT.pdf --to Cm
```

Options:
- `--to Cm` target key (required). `Cm`, `Bbm`, `Eb`, `F#m`, …
- `--engine native|omr` extraction engine (default `native`)
- `--from Bbm` source key; omit to auto-detect from the key signature
- `-o OUT.pdf` output path (default `INPUT-Cm.pdf`)
- `--title "Song name"`

Writes `OUT.pdf` and `OUT.musicxml`.

## Verify before reporting success

Always check these; report any that fail rather than claiming it worked:

1. **Measure count matches** — the script prints `measures=N`. The PDF-derived
   measure count and the MusicXML count must agree, or chords land in the wrong bars.
2. **Every bar is full** — no measure should differ from the time signature.
3. **Chord set is sensible** — transposed chords should be diatonic to the new key.
4. **Render the output and look at it.** Convert page 1 to PNG and read it.
   Check for: overlapping chord symbols, tofu boxes (□) instead of ♭, repeated
   clefs/time signatures mid-score, stray sharps in a flat key.

```bash
$SK/venv/bin/python -c "
import pymupdf; d=pymupdf.open('OUT.pdf')
d[0].get_pixmap(matrix=pymupdf.Matrix(1.5,1.5)).save('check.png')"
```

## Two modes (auto-selected)

- **Lead-sheet mode** — melody + chords. Uses Clarity-OMR for rhythm.
- **Chord-chart mode** — triggered when ≥30% of bars are rhythm slashes
  ("STATIC PIANO CHORDS" style). OMR has no noteheads to read on those bars and
  returns empty measures, so the chart is built straight from the text layer:
  every bar becomes a slash bar with its chords. Force with `--chords-only`.

The mode chosen is printed. If a melody chart is wrongly detected as a chord
chart (or the reverse), pass `--chords-only` or edit the threshold.

## Known limits

- **Text-layer PDFs only.** A scan or photo has no text layer at all, so the
  native engine cannot work; use `--engine omr`, and expect no chord symbols.
- Only the Sibelius Opus/Inkpen2 glyph mappings are tabulated. A different
  engraver's font may need entries adding to `GLYPH_DUR` / `ACCIDENTAL` in
  `native.py`; unknown glyphs fall back to a quarter note.
- Lyrics and Hebrew/section markings (פתיחה/בית/פזמון) are not carried over.
- Chord beat placement is anchored to the nearest notehead; in bars that are
  mostly rests the anchor is approximate and chords are spread evenly.
- Assumes a single melody staff (lead sheet). Piano grand staves untested.
- Durations are the softer half of the native engine. Beams are Bezier fills
  whose edges can sit inside the stem, so a bar that does not add up falls back
  to spacing-derived values; the script prints how many bars needed that.
- Ties, slurs, triplets and tuplets are not modelled; a tuplet bar will be
  approximated by the spacing fallback.
- Repeat structure (D.S., segno, rehearsal marks A/B) is not carried over —
  the output is written out linearly, bar for bar.
- Source key auto-detection assumes minor when reading a flat signature; pass
  `--from` for major keys.

## Files

- `scripts/transpose.py` — CLI pipeline
- `scripts/omr.py` — staff/notehead/chord-glyph extraction from the PDF text layer
- `scripts/rhythm.py` — barline / stem / beam geometry
- `venv/` — dependencies (torch, music21, verovio, cairosvg, pymupdf)
- `Clarity-OMR/` — vendored OMR engine (GPL-3.0, https://github.com/clquwu/Clarity-OMR)

Requires the **Bravura Text** font for ♭/♯ in chord symbols
(`~/Library/Fonts/BravuraText.otf`); without it they render as boxes.
