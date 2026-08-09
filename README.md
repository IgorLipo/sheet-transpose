# sheet-transpose

Transpose engraved sheet-music PDFs into a different key **by editing the
original PDF**, so the result is identical to the input except for the key.

Not optical music recognition. The page is never rasterised and the score is
never re-engraved — those approaches relayout the page and throw away text
instructions, rehearsal marks and slash notation.

```bash
python -m sheet_transpose chart.pdf --from Dm --to Cm -o chart-Cm.pdf
```

## Why it works

Engraved PDFs (Sibelius, Finale, MuseScore) are not pictures. Noteheads are
font glyphs at exact coordinates and staff lines are vector graphics, so pitch
is **measured, not recognised** — every notehead lands on an exact integer
half-step of the staff. And a diatonic transposition moves *every* note by the
*same* number of steps, so the geometry is one uniform translation.

| Changed | Untouched (bytes never edited) |
|---|---|
| noteheads, stems, beams, ties, articulations | staff lines, barlines, clefs, rests |
| ledger lines (recomputed, not moved) | text instructions, rehearsal marks, titles |
| key signature | slash bars, repeats, D.S., segno, bar numbers |
| chord symbols (retypeset in the score's own font) | page layout, system breaks, spacing |

## Verified

Madonna, *Like A Prayer*, piano grand staff, Dm → Cm:

```
204 notes             -> all moved exactly 1 diatonic step down
333 annotation glyphs -> byte-identical position AND character
key signature         -> 1 flat to 3 flats on all 17 staves
56 chord symbols      -> Dm->Cm, C/D->Bb/C, Gm/D->Fm/C, F/A->Eb/G, Bb->Ab
pitch check           -> D-F-A (Dm triad) -> C-Eb-G (Cm triad)
```

Also verified on two Hebrew lead sheets (Bbm → Cm), including one that is
mostly rhythm-slash bars.

## Install

```bash
pip install -e .
```

Requires `pymupdf` (used for content-stream read/write and resolved geometry).
The algorithm itself is library-independent — see `docs/SPEC.md` §6 for what to
rebuild if PyMuPDF is unavailable (e.g. on iOS).

## Layout

| Path | What |
|---|---|
| `scripts/transpose_inplace.py` | CLI + orchestration |
| `scripts/pdfsurgery.py` | content-stream tokenizer, CTM tracking, byte splicing |
| `scripts/omr.py` | staff / notehead / chord-glyph extraction |
| `scripts/rhythm.py` | barline, stem and beam geometry |
| `scripts/keysig.py` | key-signature accidental placement |
| `scripts/chords.py` | glyph-level chord transposition |
| `server/app.py` | FastAPI service (upload → transpose → download) |
| `docs/SPEC.md` | full build spec, implementation-independent |
| `docs/SKILL.md` | Claude Code skill documentation |

## Fallback mode

`scripts/transpose.py` re-engraves the score from scratch (melody + chords) for
sources with no usable text layer. Layout and annotations are **not** preserved.

## Limits

- Engraver-exported PDFs only; a scan has no text layer.
- Embedded fonts are subsetted — a letter the score never printed has no glyph.
  Falls back to the full font if installed on the system.
- A wider key signature needs horizontal room; on tight systems it can sit close
  to what follows. The music itself is never moved.
- Assumes treble/bass single-staff or grand-staff layout.

## Licence

MIT for this code. Note that Sibelius music fonts (Opus, Inkpen2) are
proprietary Avid fonts and are **not** included here.
