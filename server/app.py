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
    return c


def pick_tonic(doc, maj, rel_minor):
    """Major or relative minor? Let the chord symbols decide."""
    import collections, re as _re
    roots = collections.Counter()
    for page in doc:
        for b in page.get_text("rawdict")["blocks"]:
            for l in b.get("lines", []):
                for sp in l["spans"]:
                    if "Chords" not in sp["font"]:
                        continue
                    t = "".join(c["c"] for c in sp["chars"]).strip()
                    m = _re.match(r"^([A-G][\u00a8\u00ab#b]?)", t)
                    if m:
                        minor = "\u2039" in t or _re.search(r"m(?!aj)", t)
                        roots[(m.group(1).replace("\u00a8", "b"), bool(minor))] += 1
    maj_hits = roots.get((maj, False), 0)
    min_hits = roots.get((rel_minor, True), 0)
    return rel_minor + "m" if min_hits > maj_hits else maj


# ----------------------------------------------------------------- detection
def detect(pdf_path):
    """Read the source key signature and title straight from the PDF."""
    import pymupdf
    from sheet_transpose.transpose_inplace import source_key_signature
    info = {"key": None, "sig": None, "pages": 0, "title": None}
    try:
        doc = pymupdf.open(pdf_path)
        info["pages"] = doc.page_count
        # title = the largest text on page 1
        best = (0, None)
        for b in doc[0].get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                for s in l["spans"]:
                    t = s["text"].strip()
                    if t and s["size"] > best[0] and len(t) > 2:
                        best = (s["size"], t)
        info["title"] = best[1]
        sig = source_key_signature(pdf_path)
        info["sig"] = sig
        maj = {0: "C", -1: "F", -2: "Bb", -3: "Eb", -4: "Ab", -5: "Db", -6: "Gb",
               1: "G", 2: "D", 3: "A", 4: "E", 5: "B", 6: "F#"}.get(sig, "C")
        rel_minor = KEYS[(KEYS.index(maj) - 3) % 12] if maj in KEYS else "A"
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
    return {"url": f"/api/file/{outpdf.name}", "name": outpdf.name,
            "warnings": warn}


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
