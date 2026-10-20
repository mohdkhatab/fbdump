# fbdump — Facebook Video CDN Link Extractor

Paste a Facebook video / reel / post URL → get the direct signed **CDN MP4 links**.
Does **not** download the video.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/mohdkhatab/fbdump)

## Live API

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Web UI |
| `GET` | `/links?url=<fb-url>` | JSON — all CDN variants + HTTP status |
| `GET` | `/links.md?url=<fb-url>` | Markdown output |
| `GET` | `/health` | Health check |
| `GET` | `/docs` | Swagger UI |

## CLI

```bash
pip install -r requirements.txt
python3 fbdump.py "https://www.facebook.com/share/v/1CANGjWWeN/" --out links.md
python3 fbdump.py "<fb-url>" --json
```

Output — one line per variant with an HTTP status (`206` = live):

```
  206 HD 720p (progressive)        https://video-iad6-1.xx.fbcdn.net/...
  206 SD 360p (progressive)        https://video-iad3-1.xx.fbcdn.net/...
  206 DASH 720p (video only)       https://video-iad3-2.xx.fbcdn.net/...
```

- `HD 720p` / `SD 360p` (progressive) include an **AAC audio track**.
- `DASH` variants are **video-only**.

## How it works

Facebook login-walls normal fetches (`curl`, `yt-dlp`). The official **embed SDK**
still renders logged-out and leaks the delivery fields into its DOM:

| Field | Contents |
|---|---|
| `dash_manifest` | DASH MPD XML → `<Representation><BaseURL>` per quality |
| `sd_src` | progressive single-file MP4, low |
| `hd_src` | progressive single-file MP4, high |

So the tool drives headless Chrome over CDP, renders a local page containing the SDK
`<div class="fb-post">`, lets the SDK build its iframe, loads that iframe and scrapes
the fields.

Full rationale, pipeline details, the bugs to avoid and a troubleshooting table live in
[`AGENT_FACEBOOK_CDN_PROCESS.md`](AGENT_FACEBOOK_CDN_PROCESS.md).

## Deploy to Render

**One-click:** click the badge above → Render reads `render.yaml` → done.

**Manual:**
1. New → Web Service → connect this repo
2. Environment: `Python`, Region: `Oregon`, Plan: `Free`
3. Build command:
   ```
   pip install --no-cache-dir -r requirements.txt && (which google-chrome || (apt-get update -qq && apt-get install -y -qq google-chrome-stable))
   ```
4. Start command:
   ```
   uvicorn api:app --host 0.0.0.0 --port $PORT
   ```
5. Health check path: `/health`

A [`Dockerfile`](Dockerfile) is also included if you prefer container deploys.

> Chrome is required — the tool drives a real headless browser. The build command above
> installs it; the Dockerfile bundles it.

## Notes

- Links are signed (`oh=` / `oe=`) and **expire in ~1–2 days**. After that they return
  `403` — just re-run the extraction to refresh.
- Only public videos work. Private / removed posts legitimately return no links.
- First request takes ~45s while the embed player renders; later ones are similar.
  Tune with the `FBDUMP_SETTLE` env var (default `22`).
- Set `FBDUMP_CHROME` to point at a specific Chrome/Chromium binary if needed.
