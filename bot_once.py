"""
X -> Telegram - ONE-SHOT version for GitHub Actions (v2)
Posts new tweets that contain PICTURES or a PLAYABLE VIDEO.
- Caption = tweet text with all links removed
- No "View on X" link
- Text-only posts are skipped (and GIFs, unless INCLUDE_GIFS = True)
Runs once, saves state_rss.json, exits. GitHub triggers it on a schedule.
"""

import json
import os
import re
import sys
import html
from urllib.parse import unquote

import feedparser
import requests

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHANNEL = os.environ["TELEGRAM_CHANNEL"]
X_USERNAME = os.environ.get("X_USERNAME", "TheHateCentral2").lstrip("@")
STATE_FILE = "state_rss.json"

INCLUDE_GIFS = False                    # True = also post GIFs (they are short looping videos)
MAX_VIDEO_BYTES = 49 * 1024 * 1024      # Telegram bots can upload at most ~50 MB
MAX_ATTEMPTS = 3                        # give up on a post after this many failed runs

FEED_TEMPLATES = [
    "https://nitter.kareem.one/{user}/rss",
    "https://nitter.meowing.monster/{user}/rss",
    "https://shitter.thepixora.com/{user}/rss",
    "https://nitter.jaydenha.uk/{user}/rss",
    "https://xcancel.com/{user}/rss",
    "https://nitter.poast.org/{user}/rss",
    "https://nitter.privacyredirect.com/{user}/rss",
    "https://nitter.tiekoetter.com/{user}/rss",
]

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "*/*",
}
STATUS_RE = re.compile(r"/status/(\d+)")


# ---------- state ----------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(seen, fails):
    with open(STATE_FILE, "w") as f:
        json.dump({"seen": sorted(seen, key=int)[-300:], "fails": fails}, f)


# ---------- feed ----------
def fetch_feed():
    for template in FEED_TEMPLATES:
        url = template.format(user=X_USERNAME)
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code != 200:
                print(f"[feed] {url} -> HTTP {r.status_code}")
                continue
            parsed = feedparser.parse(r.content)
            if parsed.entries:
                print(f"[feed] using {url} ({len(parsed.entries)} entries)")
                return parsed.entries
            print(f"[feed] {url} -> 200 but no entries")
        except requests.RequestException as e:
            print(f"[feed] {url} -> {type(e).__name__}")
    print("No working feed this run. Will try again next run.")
    return []


def entry_id(entry):
    m = STATUS_RE.search(entry.get("link", "")) or STATUS_RE.search(entry.get("id", ""))
    return m.group(1) if m else None


def is_repost_or_reply(entry):
    return entry.get("title", "").startswith(("RT by", "R to", "RT @"))


# ---------- caption (links removed) ----------
def html_to_text(raw):
    raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    raw = re.sub(r"</p>", "\n", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", "", raw)
    return html.unescape(raw).strip()


def clean_caption(entry):
    text = html_to_text(entry.get("summary", "") or entry.get("title", ""))
    text = re.sub(r"(?:https?://|www\.)\S+", "", text)                 # full links
    text = re.sub(r"\bpic\.(?:twitter|x)\.com/\S+", "", text)           # pic.twitter.com/...
    text = re.sub(r"\b[\w-]+\.(?:com|net|org|io|co|me|ly|gg|tv|be)/\S*", "", text)  # bare domains
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.strip() for line in text.splitlines()).strip()
    return text[:1000]


# ---------- finding the media ----------
def fx_media(pid):
    """Ask the free FxTwitter API for the post's photos/video. Returns dict or None."""
    try:
        r = requests.get(f"https://api.fxtwitter.com/{X_USERNAME}/status/{pid}",
                         headers=HEADERS, timeout=30)
        if r.status_code != 200:
            print(f"[fxtwitter] {pid} -> HTTP {r.status_code}")
            return None
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[fxtwitter] {pid} -> {type(e).__name__}")
        return None

    tweet = data.get("tweet") or data.get("status") or {}
    media = tweet.get("media") or {}
    photos = [p["url"] for p in media.get("photos", []) if p.get("url")]
    video, has_gif = None, False
    for v in media.get("videos", []):
        if v.get("type") == "gif" and not INCLUDE_GIFS:
            has_gif = True
            continue
        if v.get("url"):
            video = v
            break
    return {"photos": photos, "video": video, "gif": has_gif}


def rss_media(entry):
    """Fallback: read photos / video links straight from the RSS item."""
    summary = entry.get("summary", "")
    photos, video_urls = [], []
    has_video = bool(re.search(r"<video", summary, flags=re.I))

    for src in re.findall(r'<img[^>]+src="([^"]+)"', summary):
        decoded = unquote(src)
        if "video" in decoded.lower():
            has_video = True
        elif "/media/" in decoded:
            photos.append(src)

    for src in re.findall(r'<(?:video|source)\b[^>]*?\bsrc="([^"]+)"', summary, flags=re.I):
        m = re.search(r"/video/[^/]+/(.+)$", src)
        if m:
            video_urls.append(unquote(m.group(1)))  # direct video.twimg.com link
        video_urls.append(src)

    video = {"url": video_urls[0], "fallbacks": video_urls[1:]} if video_urls else None
    return {"photos": [] if has_video else photos, "video": video,
            "gif": False, "video_seen": has_video}


def get_media(pid, entry):
    fx = fx_media(pid)
    if fx and (fx["photos"] or fx["video"] or fx["gif"]):
        return fx
    rss = rss_media(entry)  # FxTwitter failed or found nothing -> check the RSS item too
    if rss["video_seen"] and not rss["video"]:
        print(f"[media] {pid}: video detected but no download link available")
    return rss


# ---------- download / upload ----------
def download(url, limit=None):
    """Returns (bytes_or_None, status) where status is ok / big / fail."""
    try:
        with requests.get(url, headers=HEADERS, timeout=120, stream=True) as r:
            if r.status_code != 200:
                print(f"[download] {url[:80]} -> HTTP {r.status_code}")
                return None, "fail"
            size = int(r.headers.get("Content-Length") or 0)
            if limit and size > limit:
                return None, "big"
            chunks, total = [], 0
            for chunk in r.iter_content(1 << 20):
                chunks.append(chunk)
                total += len(chunk)
                if limit and total > limit:
                    return None, "big"
            return (b"".join(chunks), "ok") if total else (None, "fail")
    except requests.RequestException as e:
        print(f"[download] {url[:80]} -> {type(e).__name__}")
        return None, "fail"


def tg_post(method, data, files):
    r = requests.post(f"{TG_API}/{method}", data=data, files=files, timeout=300)
    if not r.ok:
        print(f"[telegram] {method} error {r.status_code}: {r.text}")
    return r.ok


def send_photos(photos, caption):
    files = [d for d in (download(u)[0] for u in photos[:10]) if d]
    if not files:
        return "fail"
    if len(files) == 1:
        data = {"chat_id": TG_CHANNEL}
        if caption:
            data["caption"] = caption
        ok = tg_post("sendPhoto", data, {"photo": ("photo.jpg", files[0])})
    else:
        media, upload = [], {}
        for i, blob in enumerate(files):
            item = {"type": "photo", "media": f"attach://p{i}"}
            if i == 0 and caption:
                item["caption"] = caption
            media.append(item)
            upload[f"p{i}"] = (f"p{i}.jpg", blob)
        ok = tg_post("sendMediaGroup",
                     {"chat_id": TG_CHANNEL, "media": json.dumps(media)}, upload)
    return "ok" if ok else "fail"


def send_video(video, caption):
    candidates = [video["url"]] + list(video.get("fallbacks", []))
    blob, status = None, "fail"
    for url in candidates:
        blob, status = download(url, MAX_VIDEO_BYTES)
        if blob or status == "big":
            break
    if status == "big":
        print("[video] larger than Telegram's 50 MB bot limit - skipped")
        return "skip"
    if not blob:
        return "fail"

    data = {"chat_id": TG_CHANNEL, "supports_streaming": "true"}
    if caption:
        data["caption"] = caption
    for key in ("width", "height", "duration"):
        if video.get(key):
            data[key] = str(int(video[key]))
    ok = tg_post("sendVideo", data, {"video": ("video.mp4", blob)})
    return "ok" if ok else "fail"


# ---------- main ----------
def main():
    state = load_state()
    seen = set(state.get("seen", []))
    fails = state.get("fails", {})

    entries = fetch_feed()
    if not entries:
        return

    # First ever run: remember existing posts, don't post old ones
    if "seen" not in state:
        for e in entries:
            pid = entry_id(e)
            if pid:
                seen.add(pid)
        save_state(seen, {})
        print("Initialised. Only new posts from now on will be forwarded.")
        return

    new = [(entry_id(e), e) for e in entries
           if entry_id(e) and entry_id(e) not in seen and not is_repost_or_reply(e)]
    new.sort(key=lambda x: int(x[0]))

    for pid, e in new:
        media = get_media(pid, e)
        n_photos = len(media["photos"])
        print(f"[media] {pid}: {n_photos} photo(s), video={'yes' if media['video'] else 'no'}, "
              f"gif={'yes' if media.get('gif') else 'no'}")

        caption = clean_caption(e)
        if media["video"]:
            result = send_video(media["video"], caption)
        elif media["photos"]:
            result = send_photos(media["photos"], caption)
        else:
            print(f"Skipped {pid} (no picture or video)")
            result = "skip"

        if result == "ok":
            print(f"Forwarded {pid}")

        if result in ("ok", "skip"):
            seen.add(pid)
            fails.pop(pid, None)
            save_state(seen, fails)
        else:
            fails[pid] = fails.get(pid, 0) + 1
            if fails[pid] >= MAX_ATTEMPTS:
                print(f"Giving up on {pid} after {MAX_ATTEMPTS} failed runs")
                seen.add(pid)
                fails.pop(pid, None)
                save_state(seen, fails)
                continue
            save_state(seen, fails)
            break  # retry next run, keep order


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # don't turn the workflow red for temporary glitches
        print(f"Run failed: {exc}")
        sys.exit(0)
