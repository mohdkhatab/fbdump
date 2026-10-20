"""
api.py — HTTP wrapper around fbdump.py

Run:  uvicorn api:app --host 0.0.0.0 --port 8000

Endpoints:
  GET /health                  -> {"ok": true}
  GET /links?url=<fb-url>      -> {"url":..., "permalink":..., "variants":[...], "dash_manifest":...}
  GET /links.md?url=<fb-url>   -> the same as a markdown document
"""
import os
import sys

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fbdump  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(
    title="fbdump",
    description="Facebook video CDN link extractor",
    version="1.0.0",
)


def _extract(url: str):
    """Run the extraction; normalise errors into HTTPException."""
    try:
        return fbdump.run(url, out_md=None, as_json=False)
    except SystemExit as e:          # fbdump.die() raises SystemExit
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:            # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.get("/health")
def health():
    return {"ok": True, "tool": "fbdump"}


@app.get("/", response_class=HTMLResponse)
def index():
    """Web UI: paste a Facebook URL, get the CDN links."""
    path = os.path.join(HERE, "static", "index.html")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="UI not found")
    with open(path, encoding="utf-8") as f:
        return f.read()


@app.get("/links")
def links(url: str = Query(..., description="Facebook video/reel/post URL")):
    r = _extract(url)
    return JSONResponse({
        "url": r["source_url"],
        "permalink": r["permalink"],
        "embed_variant": r["embed_variant"],
        "variants": [
            {
                "label": v["label"],
                "url": v["url"],
                "http_status": v.get("http_status"),
                "content_type": v.get("content_type"),
            }
            for v in (r["progressive"] + r["dash"])
        ],
        "dash_manifest": r.get("dash_manifest", ""),
        "note": "Links are signed and expire in ~1-2 days. Re-run to refresh.",
    })


@app.get("/links.md", response_class=PlainTextResponse)
def links_md(url: str = Query(..., description="Facebook video/reel/post URL")):
    r = _extract(url)
    tmp = "/tmp/_fbdump_out.md"
    fbdump.write_md(r, tmp)
    with open(tmp) as f:
        return f.read()


@app.get("/download/fbdump.py")
def download_script():
    """The standalone extractor script, as a file download."""
    return FileResponse(
        os.path.join(HERE, "fbdump.py"),
        media_type="text/x-python",
        filename="fbdump.py",
    )


@app.get("/download/{filename}")
def download_file(filename: str):
    """Serve a packaged file (zip / md / api.py) by name."""
    allowed = {
        "fbdump-facebook-cdn-extractor.zip",
        "fbdump.py",
        "api.py",
        "README.md",
        "requirements.txt",
        "AGENT_FACEBOOK_CDN_PROCESS.md",
        "index.html",
    }
    if filename not in allowed:
        raise HTTPException(status_code=404, detail="unknown file")
    path = os.path.join(HERE, filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, filename=filename)


@app.get("/download/static/index.html")
def download_ui():
    """The web UI source, as a file download."""
    path = os.path.join(HERE, "static", "index.html")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type="text/html", filename="index.html")
