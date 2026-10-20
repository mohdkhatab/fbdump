#!/usr/bin/env python3
"""
fbdump.py — Extract Facebook video CDN (MP4) links from any public Facebook URL.

Usage:
    python3 fbdump.py <facebook-url> [--out out.md] [--json]

How it works (see the companion MD doc for full rationale):
  Facebook login-walls normal fetches (curl/yt-dlp). The official *embed SDK*
  still renders logged-out and exposes the delivery fields in its DOM:
      dash_manifest  (DASH MPD XML -> multiple <Representation><BaseURL>)
      sd_src         (progressive single file, low)
      hd_src         (progressive single file, high)
  So we drive headless Chrome over CDP, load a local page containing the SDK
  <div class="fb-post">/fb-video>, let the SDK build its iframe, then load that
  iframe and scrape the fields.

Dependencies: google-chrome, python3 + websocket-client
"""
import argparse, functools, json, os, re, shutil, socket, subprocess, sys, tempfile, threading, time
import urllib.parse, urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import websocket

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _find_chrome():
    """Locate a Chrome/Chromium binary. Override with FBDUMP_CHROME."""
    cand = os.environ.get("FBDUMP_CHROME")
    if cand and os.path.isfile(cand):
        return cand
    for name in ("google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "chrome"):
        for d in os.environ.get("PATH", "").split(os.pathsep):
            p = os.path.join(d, name)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    shutil.which("google-chrome") or shutil.which("chromium") or "google-chrome"


CHROME = _find_chrome()
FB = "facebook.com"
TIMEOUT = 60

# Facebook serves the logged-out redirect chain (which carries the real
# permalink in ?next=) to some UAs and 400/403 to others. Try a spread.
RESOLVE_UAS = [
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Twitterbot/1.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    UA,
]


# ---------------------------------------------------------------- utilities
def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def curl_final(url, ua=UA, timeout=40):
    """Follow redirects, return final URL (may be a /login/?next=... wrapper)."""
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
    opener = urllib.request.build_opener()
    try:
        r = opener.open(req, timeout=timeout)
        return r.geturl()
    except Exception:
        return url


def _clean_permalink(cand):
    """Normalise a Facebook permalink, dropping tracking params."""
    if not cand:
        return None
    cand = cand.strip()
    cand = re.sub(r"[?&]rdid=[^&]*", "", cand)
    cand = re.sub(r"[?&]share_url=[^&]*", "", cand)
    cand = re.sub(r"[?&]refsrc=[^&]*", "", cand)
    cand = cand.rstrip("?&")
    if "facebook.com" not in cand:
        return None
    if ("story.php" in cand or "/videos/" in cand or "/reel/" in cand
            or "watch/" in cand or "/posts/" in cand or "/v=" in cand):
        return cand
    return None


def _permalink_from(next_raw):
    """Pull a usable permalink out of a /login/?next=... value."""
    if not next_raw:
        return None
    return _clean_permalink(urllib.parse.unquote(next_raw))


def resolve_permalink(url):
    """
    Turn any Facebook URL into a permalink we can embed.

    Handles the /share/v/ style short links, which resolve in one of two ways:
      1. 302 -> /login/?next=<encoded permalink>   (login-walled post)
      2. 302 -> /reel/<id>/?rdid=...&share_url=... (public reel, no login wall)
    Both shapes are handled.
    """
    url = url.strip()
    if "facebook.com" not in url and "fb.watch" not in url:
        die(f"not a Facebook URL: {url}")
    if "fb.watch/" not in url and "/share/" not in url:
        return _clean_permalink(url) or url

    for ua in RESOLVE_UAS:
        final = curl_final(url, ua=ua)
        # case 2: the redirect target itself is already a permalink
        p = _clean_permalink(final)
        if p and "share_url" not in p and "/share/" not in p:
            print(f"[*] resolved via {ua.split('/')[0][:28]} (direct)")
            return p
        # case 1: permalink is in the ?next= wrapper
        q = urllib.parse.parse_qs(urllib.parse.urlparse(final).query)
        p = _permalink_from((q.get("next") or [""])[0])
        if p:
            print(f"[*] resolved via {ua.split('/')[0][:28]} (next)")
            return p
        # fallback: some responses embed the permalink in the body
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            body = urllib.request.urlopen(req, timeout=40).read().decode("utf8", "ignore")
            m = re.search(r'story_fbid=(\d+)&(?:amp;)?id=(\d+)', body)
            if m:
                return (f"https://www.facebook.com/story.php?story_fbid={m.group(1)}"
                        f"&id={m.group(2)}")
            m = re.search(r'facebook\.com/reel/(\d+)', body)
            if m:
                return f"https://www.facebook.com/reel/{m.group(1)}"
        except Exception:
            pass
    die(f"could not resolve short link: {url}")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ------------------------------------------------------------------- chrome
class Chrome:
    def __init__(self):
        self.profile = tempfile.mkdtemp(prefix="fbdump-")
        self.port = free_port()
        self.proc = subprocess.Popen([
            CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
            "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
            "--autoplay-policy=no-user-gesture-required", "--window-size=1280,800",
            f"--user-data-dir={self.profile}",
            f"--remote-debugging-port={self.port}", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        info = self._wait_devtools()
        self.ws = websocket.create_connection(
            info["webSocketDebuggerUrl"], timeout=30, max_size=None, suppress_origin=True)
        self._id = 0
        self._sid = None

    def _wait_devtools(self):
        for _ in range(90):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=2) as r:
                    return json.load(r)
            except Exception:
                time.sleep(0.4)
        die("chrome devtools did not start")

    def cmd(self, method, params=None, session=None):
        self._id += 1
        mid = self._id
        m = {"id": mid, "method": method, "params": params or {}}
        if session:
            m["sessionId"] = session
        self.ws.send(json.dumps(m))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                return msg

    def open(self, url, settle=25):
        """Open url, return final DOM html + evaluated helpers."""
        tid = self.cmd("Target.createTarget", {"url": "about:blank"})["result"]["targetId"]
        time.sleep(0.4)
        sid = self.cmd("Target.attachToTarget", {"targetId": tid, "flatten": True})["result"]["sessionId"]
        self._sid = sid
        self.cmd("Page.enable", {}, sid)
        self.cmd("Runtime.enable", {}, sid)
        self.cmd("Network.enable", {}, sid)
        self.cmd("Page.navigate", {"url": url}, sid)
        time.sleep(settle)
        return self.eval("(document.documentElement?document.documentElement.outerHTML:'')||''"), sid

    def eval(self, expr, session=None):
        r = self.cmd("Runtime.evaluate",
                     {"expression": expr, "returnByValue": True, "awaitPromise": True},
                     session or self._sid)
        return r.get("result", {}).get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass
        self.proc.terminate()
        shutil.rmtree(self.profile, ignore_errors=True)


# ---------------------------------------------------------------- local host
def serve(dirpath, port):
    # Bind the handler to the directory explicitly. Do NOT chdir: the server
    # must keep serving from dirpath even if the process CWD changes later.
    handler = functools.partial(SimpleHTTPRequestHandler, directory=dirpath)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


# ------------------------------------------------------------------ extract
def unjson(s):
    try:
        return json.loads('"' + s + '"')
    except Exception:
        return s


def grab_fields(html):
    out = {}
    for key in ("dash_manifest", "sd_src", "hd_src"):
        i = html.find('"' + key + '"')
        if i < 0:
            continue
        m = re.match(r'"' + key + r'":"((?:[^"\\]|\\.)*)"', html[i:])
        if m:
            out[key] = unjson(m.group(1))
    return out


def dash_variants(mpd):
    """-> list of (height, url) from a DASH MPD."""
    import html as htmllib
    res = []
    for mm in re.finditer(r"<Representation[^>]*>(.*?)</Representation>", mpd, re.S):
        tag = mm.group(0)
        hh = re.search(r'height="(\d+)"', tag)
        b = re.search(r"<BaseURL>(.*?)</BaseURL>", mm.group(1), re.S)
        if hh and b:
            res.append((int(hh.group(1)), htmllib.unescape(b.group(1))))
    return sorted(res, key=lambda t: -t[0])


def probe(url, ua=UA):
    """Return (http_code, content_type, length) for a HEAD/range request."""
    import urllib.error
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ua, "Range": "bytes=0-99"})
        r = urllib.request.urlopen(req, timeout=30)
        return r.status, r.headers.get("Content-Type", ""), r.headers.get("Content-Range", "")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", "") if e.headers else "", ""
    except Exception as e:
        return 0, str(e)[:40], ""


# --------------------------------------------------------------------- main
def run(url, out_md=None, as_json=False, settle=22):
    permalink = resolve_permalink(url)
    print(f"[*] permalink : {permalink}")

    workdir = tempfile.mkdtemp(prefix="fbdump-page-")
    port = free_port()
    httpd = serve(workdir, port)
    origin = f"http://127.0.0.1:{port}"

    # two embed flavours; fb-post first (works for reels), fb-video as fallback
    # NOTE: the href MUST be HTML-escaped. A raw '&' in the attribute truncates
    # the permalink (story.php?story_fbid=...&id=... loses the &id= part) and
    # Facebook then answers "Video Unavailable".
    import html as _htmllib
    esc = _htmllib.escape(permalink, quote=True)
    variants = [
        ("fb-post", f'<div class="fb-post" data-href="{esc}" data-width="500" data-show-text="true"></div>'),
        ("fb-video", f'<div class="fb-video" data-href="{esc}"></div>'),
    ]

    chrome = Chrome()
    fields, used_variant, embed_url = {}, None, None
    try:
        for kind, div in variants:
            page = (f'<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>'
                    f'<div id="fb-root"></div>'
                    f'<script async="1" defer="1" '
                    f'src="https://connect.facebook.net/en_US/sdk.js#xfbml=1&version=v26.0"></script>'
                    f'{div}</body></html>')
            with open(os.path.join(workdir, "embed.html"), "w") as f:
                f.write(page)

            html, sid = chrome.open(f"{origin}/embed.html", settle=settle)
            iframes = re.findall(r'<iframe[^>]+src="([^"]*plugins/(?:post|video)\.php[^"]*)"', html)
            iframes = [i.replace("&amp;", "&") for i in iframes]
            if not iframes:
                print(f"[!] {kind}: no iframe produced")
                continue
            embed_url = iframes[0]
            print(f"[*] {kind}: iframe ok")

            html2, sid2 = chrome.open(embed_url, settle=settle)
            f = grab_fields(html2)
            if not f and os.environ.get("FBDUMP_DEBUG"):
                txt = chrome.eval("(document.body?document.body.innerText:'').slice(0,200)", sid2)
                print("    [dbg] iframe says:", repr(txt))
            if f:
                fields = f
                used_variant = kind
                break
            print(f"[!] {kind}: no delivery fields in iframe DOM")
    finally:
        chrome.close()
        try:
            httpd.shutdown()
        except Exception:
            pass
        shutil.rmtree(workdir, ignore_errors=True)

    if not fields:
        die("no dash_manifest/sd_src/hd_src found — the post may be private, "
            "removed, or login-gated")

    # ---------------------------------------------------------------- build
    result = {
        "source_url": url,
        "permalink": permalink,
        "embed_variant": used_variant,
        "progressive": [],
        "dash": [],
        "dash_manifest": fields.get("dash_manifest", ""),
    }
    if fields.get("hd_src"):
        result["progressive"].append({"label": "HD 720p (progressive)", "url": fields["hd_src"]})
    if fields.get("sd_src"):
        result["progressive"].append({"label": "SD 360p (progressive)", "url": fields["sd_src"]})
    for h, u in dash_variants(fields.get("dash_manifest", "")):
        result["dash"].append({"label": f"DASH {h}p (video only)", "height": h, "url": u})

    print("\n=== extracted ===")
    for item in result["progressive"] + result["dash"]:
        code, ctype, _ = probe(item["url"])
        item["http_status"] = code
        item["content_type"] = ctype
        print(f"  {code} {item['label']:<28} {item['url'][:88]}...")

    if as_json:
        print(json.dumps(result, indent=2))
    if out_md:
        write_md(result, out_md)
        print(f"\n[*] wrote {out_md}")
    return result


def write_md(r, path):
    L = []
    L.append("# Facebook Video — CDN Links\n")
    L.append(f"**Source URL:** {r['source_url']}")
    L.append(f"**Permalink:** {r['permalink']}")
    L.append(f"**Extracted:** {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n")
    L.append("> CDN links are signed (`oh=` / `oe=`) and expire in ~1-2 days.")
    L.append("> Re-resolve with `fbdump.py <url>` if a link returns 403.\n")
    L.append("---\n")
    L.append("## Progressive MP4 (video + audio)\n")
    for it in r["progressive"]:
        L.append(f"### {it['label']}\n")
        L.append(f"HTTP {it.get('http_status','?')} · {it.get('content_type','')}\n")
        L.append("```")
        L.append(it["url"])
        L.append("```\n")
    L.append("## DASH — video only\n")
    for it in r["dash"]:
        L.append(f"### {it['label']}\n")
        L.append(f"HTTP {it.get('http_status','?')} · {it.get('content_type','')}\n")
        L.append("```")
        L.append(it["url"])
        L.append("```\n")
    L.append("## DASH manifest (MPD)\n")
    L.append("> Raw XML: `&` appears as `&amp;`. Decode before using a `<BaseURL>` directly.\n")
    L.append("```")
    L.append(r["dash_manifest"])
    L.append("```")
    with open(path, "w") as f:
        f.write("\n".join(L))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--out")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    run(a.url, a.out, a.json, settle=int(os.environ.get("FBDUMP_SETTLE", "22")))
