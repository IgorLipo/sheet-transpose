"""Sheet-transpose service: upload a chart, get it back in another key.

Runs on the Air and is reached over Tailscale. The heavy lifting is the
in-place PDF transposer; this only wraps it in an API and serves the UI.
"""
import os, re, io, json, uuid, shutil, subprocess, sys, sqlite3, time
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
LIB = ROOT / "library"          # saved charts
OUT = ROOT / "out"              # transposed results
for d in (LIB, OUT):
    d.mkdir(exist_ok=True)
DB = ROOT / "charts.db"
PY = str(ROOT / "venv" / "bin" / "python")

KEYS = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]

app = FastAPI(title="Sheet Transpose")


def db():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS charts(
        id TEXT PRIMARY KEY, title TEXT, src_key TEXT,
        pages INT, added REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS outputs(
        name TEXT PRIMARY KEY, title TEXT, src_key TEXT,
        dst_key TEXT, added REAL)""")
    return c


def remember_output(name, title, src, dst):
    c = db()
    c.execute("INSERT OR REPLACE INTO outputs VALUES(?,?,?,?,?)",
              (name, title, src, dst, time.time()))
    c.commit()


def pick_tonic(doc, maj, rel_minor):
    """Major or relative minor? Let the chord symbols decide."""
    import collections, re as _re
    from sheet_transpose.omr import is_chord_font
    roots = collections.Counter()
    seq = []
    for pno, page in enumerate(doc):
        for b in page.get_text("rawdict")["blocks"]:
            for l in b.get("lines", []):
                for sp in l["spans"]:
                    if not is_chord_font(sp["font"]):
                        continue
                    t = "".join(c["c"] for c in sp["chars"]).strip()
                    m = _re.match(r"^([A-G][\u00a8\u00ab#b]?)", t)
                    if m:
                        minor = "\u2039" in t or _re.search(r"m(?!aj)", t)
                        key = (m.group(1).replace("\u00a8", "b")
                                         .replace("\u00ab", "#"), bool(minor))
                        roots[key] += 1
                        seq.append((pno, round(sp["bbox"][1], 1),
                                    sp["bbox"][0], key))
    ordered = [k for *_pos, k in sorted(seq)]
    maj_hits = roots.get((maj, False), 0)
    min_hits = roots.get((rel_minor, True), 0)
    # Raw counts alone are nearly a coin toss on a minor tune full of
    # relative-major colour chords. What actually names the tonic is where
    # the music starts and where it comes to rest, so the first and final
    # chords each count as heavily as several passing ones.
    if ordered:
        for probe, w in ((ordered[0], 5), (ordered[-1], 5)):
            if probe == (maj, False):
                maj_hits += w
            elif probe == (rel_minor, True):
                min_hits += w
    return rel_minor + "m" if min_hits > maj_hits else maj


# ----------------------------------------------------------------- detection
def detect(pdf_path):
    """Read the source key signature and title straight from the PDF."""
    import pymupdf
    from sheet_transpose.transpose_inplace import source_key_signature, activate_roles
    from sheet_transpose.omr import is_music_font, is_chord_font
    info = {"key": None, "sig": None, "pages": 0, "title": None}
    try:
        doc = pymupdf.open(pdf_path)
        activate_roles(doc)
        info["pages"] = doc.page_count
        # Title = the visually largest TEXT on page 1. Judged by the drawn
        # bbox, not the reported point size: Type3 fonts report a size from
        # their own FontMatrix, so a 2pt title can tower over a 20pt one. And
        # music fonts are excluded, or the tallest clef "wins" as tofu boxes.
        best = (0, None)
        for b in doc[0].get_text("rawdict")["blocks"]:
            for l in b.get("lines", []):
                for s in l["spans"]:
                    if is_music_font(s["font"]) or is_chord_font(s["font"]):
                        continue
                    t = "".join(c["c"] for c in s["chars"]).strip()
                    h = s["bbox"][3] - s["bbox"][1]
                    if t and len(t) > 2 and t.isprintable() and h > best[0] \
                       and not any(0xF000 <= ord(c) <= 0xF0FF for c in t):
                        best = (h, t)
        info["title"] = best[1]
        sig = source_key_signature(pdf_path)
        info["sig"] = sig
        maj = {0: "C", -1: "F", -2: "Bb", -3: "Eb", -4: "Ab", -5: "Db", -6: "Gb",
               1: "G", 2: "D", 3: "A", 4: "E", 5: "B", 6: "F#"}.get(sig, "C")
        SHARPK = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A",
                  "A#", "B"]
        # sharp keys name their relative minor with a sharp: E goes with
        # C sharp minor, never D flat minor
        table = SHARPK if sig > 0 else KEYS
        rel_minor = table[(KEYS.index(maj) - 3) % 12] if maj in KEYS \
            else ("F#" if maj == "F#" else "A")
        info["major"] = maj
        info["minor"] = rel_minor + "m"
        # Pick whichever tonic the chart actually leans on: compare how often
        # the major tonic and the relative minor appear as chord roots.
        info["key"] = pick_tonic(doc, maj, rel_minor)
    except Exception as e:
        info["error"] = str(e)[:200]
    return info


@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...)):
    tmp = OUT / f"an-{uuid.uuid4().hex}.pdf"
    tmp.write_bytes(await file.read())
    info = detect(str(tmp))
    info["token"] = tmp.name
    return info


@app.post("/api/transpose")
async def transpose(token: str = Form(None), chart_id: str = Form(None),
                    src: str = Form(...), dst: str = Form(...),
                    title: str = Form(None)):
    if chart_id:
        srcpdf = LIB / f"{chart_id}.pdf"
    else:
        srcpdf = OUT / token
    if not srcpdf.exists():
        raise HTTPException(404, "source not found")
    stem = re.sub(r"[^\w\- ]", "", (title or srcpdf.stem))[:60] or "chart"
    outpdf = OUT / f"{stem} [{dst}] {uuid.uuid4().hex[:6]}.pdf"
    r = subprocess.run([PY, "-m", "sheet_transpose", str(srcpdf), "--from", src,
                        "--to", dst, "-o", str(outpdf), "-v"],
                       capture_output=True, text=True, cwd=str(ROOT))
    log = (r.stdout + r.stderr)
    if not outpdf.exists():
        raise HTTPException(500, log[-800:] or "transpose failed")
    warn = []
    if "chords_unavailable" in log:
        m = re.search(r"chords_unavailable': '([^']*)'", log)
        if m:
            warn.append("Some chords kept their original spelling: " + m.group(1))
    remember_output(outpdf.name, stem, src, dst)
    return {"url": f"/api/file/{outpdf.name}", "name": outpdf.name,
            "warnings": warn}


@app.post("/api/quick")
async def quick(file: UploadFile = File(...), dst: str = Form(...),
                src: str = Form(None)):
    """One round trip for the iOS Shortcut: PDF in, transposed PDF out.

    The source key is detected from the chart itself unless given, and the
    result lands in the transposed library like any other run.
    """
    tmp = OUT / f"an-{uuid.uuid4().hex}.pdf"
    tmp.write_bytes(await file.read())
    info = detect(str(tmp))
    src = src or info.get("key")
    if not src:
        raise HTTPException(422, info.get("error") or "could not read the key")
    # match the tonic mode: a minor chart goes to a minor destination
    if src.endswith("m") != dst.endswith("m"):
        dst = dst + "m" if src.endswith("m") else dst.rstrip("m")
    title = re.sub(r"[^\w\- ]", "", info.get("title")
                   or (file.filename or "chart").rsplit(".", 1)[0])[:60] or "chart"
    outpdf = OUT / f"{title} [{dst}] {uuid.uuid4().hex[:6]}.pdf"
    r = subprocess.run([PY, "-m", "sheet_transpose", str(tmp), "--from", src,
                        "--to", dst, "-o", str(outpdf)],
                       capture_output=True, text=True, cwd=str(ROOT))
    if not outpdf.exists():
        raise HTTPException(500, (r.stdout + r.stderr)[-800:] or "transpose failed")
    remember_output(outpdf.name, title, src, dst)
    return FileResponse(outpdf, media_type="application/pdf",
                        filename=outpdf.name)


@app.get("/api/outputs")
def outputs():
    c = db()
    rows = c.execute("SELECT name,title,src_key,dst_key,added FROM outputs "
                     "ORDER BY added DESC").fetchall()
    return [{"name": r[0], "title": r[1], "src": r[2], "dst": r[3],
             "url": f"/api/file/{r[0]}"}
            for r in rows if (OUT / r[0]).exists()]


@app.delete("/api/outputs/{name}")
def del_output(name: str):
    c = db()
    c.execute("DELETE FROM outputs WHERE name=?", (name,))
    c.commit()
    (OUT / name).unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/file/{name}")
def getfile(name: str):
    p = OUT / name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="application/pdf", filename=name)


# ------------------------------------------------------------------- library
@app.get("/api/charts")
def charts():
    c = db()
    rows = c.execute("SELECT id,title,src_key,pages FROM charts ORDER BY added DESC").fetchall()
    return [{"id": r[0], "title": r[1], "key": r[2], "pages": r[3]} for r in rows]


@app.post("/api/charts")
async def add_chart(file: UploadFile = File(...), title: str = Form(None)):
    cid = uuid.uuid4().hex[:10]
    p = LIB / f"{cid}.pdf"
    p.write_bytes(await file.read())
    info = detect(str(p))
    t = title or info.get("title") or file.filename.rsplit(".", 1)[0]
    c = db()
    c.execute("INSERT INTO charts VALUES(?,?,?,?,?)",
              (cid, t, info.get("key") or "?", info.get("pages") or 0, time.time()))
    c.commit()
    return {"id": cid, "title": t, "key": info.get("key"), "pages": info.get("pages")}


@app.delete("/api/charts/{cid}")
def del_chart(cid: str):
    c = db()
    c.execute("DELETE FROM charts WHERE id=?", (cid,))
    c.commit()
    (LIB / f"{cid}.pdf").unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"ok": True, "charts": len(charts())}


app.mount("/", StaticFiles(directory=str(ROOT / "web"), html=True), name="web")
