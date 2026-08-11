"""Sheet-transpose service: upload a chart, get it back in another key.

Runs on the Air and is reached over Tailscale. The heavy lifting is the
in-place PDF transposer; this only wraps it in an API and serves the UI.
"""
import os, re, io, json, uuid, shutil, subprocess, sys, sqlite3, time
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import (FileResponse, JSONResponse, HTMLResponse,
                               PlainTextResponse)
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

# Tailscale Funnel puts this service on the public internet so the phone can
# reach it from any network, which also means anyone who guesses the hostname
# can reach it. A shared secret - sent as a header, a query string or a cookie
# so it survives both the Shortcut and a tap on a link - keeps it ours.
TOKEN = os.environ.get("TRANSPOSE_TOKEN", "").strip()
OPEN_PATHS = ("/api/health", "/install.html", "/manifest.webmanifest",
              "/transpose-chart.shortcut")


@app.middleware("http")
async def gate(request: Request, call_next):
    if TOKEN and not request.url.path.startswith(OPEN_PATHS):
        given = (request.headers.get("x-transpose-token")
                 or request.query_params.get("k")
                 or request.cookies.get("tk"))
        if given != TOKEN:
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": "unauthorised"}, status_code=401)
            return HTMLResponse(
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<body style='background:#07070A;color:#F2F3F7;font:17px -apple-system;"
                "display:grid;place-items:center;height:100vh;margin:0'>"
                "<div style='text-align:center'><div style='font-size:34px'>&#9837;</div>"
                "<p>Open this from your own link.</p></div>", status_code=401)
    resp = await call_next(request)
    # a correct key in the URL is remembered, so the home-screen app and every
    # link it opens keep working without the key trailing behind them
    if TOKEN and request.query_params.get("k") == TOKEN:
        resp.set_cookie("tk", TOKEN, max_age=60 * 60 * 24 * 3650,
                        httponly=True, samesite="lax", secure=True)
    return resp


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
    """Which chord is this chart's tonic? Let the chord symbols decide.

    A key signature never names one key. Five flats fits D flat major, B flat
    minor AND F minor - "Golden Boy" is in F minor, opens on F minor and plays
    it more than any other chord, yet a test that only weighs the major
    against its relative minor cannot ever choose it.
    """
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
    if not seq:
        return maj
    ordered = [k for *_pos, k in sorted(seq)]
    # Candidates: the major the signature spells, its relative minor, and the
    # other minors that same signature can carry (a minor chart borrows a
    # raised leading note, so it prints its relative major's signature).
    cands = {(maj, False), (rel_minor, True)}
    for (r, mi), _n in roots.items():
        if mi:
            cands.add((r, mi))
    best, bestscore = None, ()
    for cand in cands:
        home = cand in ((maj, False), (rel_minor, True))
        if not home and ordered[0] != cand and ordered[-1] != cand:
            # a chart in a minor its signature does not spell has to earn it:
            # only an opening or closing chord makes that case
            continue
        # Raw counts alone are nearly a coin toss on a minor tune full of
        # relative-major colour chords. What actually names the tonic is
        # where the music starts and where it comes to rest, so the first
        # and last chords each count as heavily as several passing ones.
        score = (roots.get(cand, 0)
                 + 5 * (ordered[0] == cand) + 5 * (ordered[-1] == cand))
        # Ties go to the chord the music STARTS on, then to the key the
        # signature actually spells: "aluf haolam" opens on B flat minor and
        # ends on E flat minor with ten of each, and it is in B flat minor.
        rank = (score, ordered[0] == cand, home)
        if rank > bestscore:
            best, bestscore = cand, rank
    if not best:
        return maj
    return best[0] + "m" if best[1] else best[0]


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


def pick_page(token, info):
    """The key chooser, served when a request arrives without one.

    A Shortcut that fails to fill in the key is not a dead end: the chart is
    already on the server, so all that is missing is the tap that names the
    destination, and a page can ask for that where a share sheet cannot.
    """
    src = info.get("key") or "?"
    minor = src.endswith("m")
    names = [k + ("m" if minor else "") for k in KEYS]
    title = (info.get("title") or "chart").replace("<", "")
    keys = "".join(
        f'<a class=k href="/api/pick/{token}/{n}?k={TOKEN}">{n}</a>'
        for n in names if n != src)
    return f"""<!DOCTYPE html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Pick a key</title><style>
*{{box-sizing:border-box;-webkit-tap-highlight-color:transparent}}
body{{margin:0;background:#07070A;color:#F2F3F7;padding:26px 18px;
 font:17px/1.4 -apple-system,BlinkMacSystemFont,system-ui,sans-serif}}
h1{{font:600 22px/1.2 -apple-system;margin:0 0 4px;letter-spacing:-.02em}}
p{{color:rgba(242,243,247,.5);margin:0 0 22px;font-size:14px}}
.g{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}}
.k{{display:grid;place-items:center;height:64px;border-radius:16px;
 text-decoration:none;color:#fff;font:600 20px -apple-system;
 background:linear-gradient(160deg,#5E9BFF,#9C6BFF);
 box-shadow:0 6px 18px rgba(94,155,255,.34)}}
.k:active{{transform:scale(.95)}}
</style><h1>{title}</h1><p>Now in {src} &middot; tap the key you want</p>
<div class=g>{keys}</div>"""


@app.post("/api/quick/{dst}")
@app.post("/api/quick/")
@app.post("/api/quick")
async def quick(request: Request, dst: str = None):
    """One round trip for the iOS Shortcut: PDF in, transposed PDF out.

    The chart arrives either as a form upload or as the raw request body - a
    Shortcut posting a file straight from the share sheet sends the latter,
    and naming the parts of a multipart body is the fragile bit of the whole
    chain. The body is read here rather than through File/Form parameters,
    which consume the stream before the raw path can look at it.
    """
    ctype = request.headers.get("content-type", "")
    body, form = b"", {}
    if ctype.startswith("multipart/"):
        form = await request.form()
        up = form.get("file")
        if up is not None and hasattr(up, "read"):
            body = await up.read()
    else:
        # Anything else is taken as the chart itself. A Shortcut posting a
        # file, and curl with --data-binary, both label the body something
        # other than application/pdf, so the content decides, not the header.
        body = await request.body()
    q = request.query_params
    # The key may be the last path segment, which is the one place a Shortcut
    # cannot leave empty: an unresolved token in a query string still sends
    # dst= with nothing after it, and the request looks valid.
    dst = (dst or form.get("dst") or q.get("dst") or "").strip()
    src = form.get("src") or q.get("src")
    if not dst:
        # No key came through - a Shortcut token that did not resolve. Rather
        # than fail where the share sheet shows nothing, park the chart and
        # send back a page that asks for the key, which works in any browser.
        if not body:
            raise HTTPException(422, "no chart in the request")
        held = OUT / f"an-{uuid.uuid4().hex}.pdf"
        held.write_bytes(body)
        info = detect(str(held))
        # A Shortcut cannot preview a web page, so when one asks for the key
        # page it is told WHERE to find it and opens that in Safari instead.
        if request.query_params.get("as") == "url":
            return PlainTextResponse(
                f"{request.base_url}api/keys/{held.name}?k={TOKEN}")
        return HTMLResponse(pick_page(held.name, info))
    tmp = OUT / f"an-{uuid.uuid4().hex}.pdf"
    tmp.write_bytes(body)
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


@app.get("/api/keys/{token}")
def keys_page(token: str):
    """The key chooser as a normal page, reachable by tapping a link."""
    p = OUT / token
    if not p.exists():
        raise HTTPException(404, "that chart is no longer here")
    return HTMLResponse(pick_page(token, detect(str(p))))


@app.get("/api/pick/{token}/{dst}")
def pick(token: str, dst: str):
    """Finish a run that arrived without a key: transpose and hand back the PDF."""
    srcpdf = OUT / token
    if not srcpdf.exists():
        raise HTTPException(404, "that chart is no longer here")
    info = detect(str(srcpdf))
    src = info.get("key")
    if not src:
        raise HTTPException(422, "could not read the key")
    title = re.sub(r"[^\w\- ]", "", info.get("title") or "chart")[:60] or "chart"
    outpdf = OUT / f"{title} [{dst}] {uuid.uuid4().hex[:6]}.pdf"
    r = subprocess.run([PY, "-m", "sheet_transpose", str(srcpdf), "--from", src,
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
