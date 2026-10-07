"""
X -> Telegram (pictures only) - ONE-SHOT version for GitHub Actions.
Runs once, posts any new picture tweets, saves state_rss.json, then exits.
GitHub triggers it every 30 minutes (see .github/workflows/bot.yml).

Settings come from environment variables (GitHub Secrets), never from the code.
"""

import html
import json
import os
import re
import sys
from urllib.parse import unquote

import feedparser
import requests

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHANNEL = os.environ["TELEGRAM_CHANNEL"]
X_USERNAME = os.environ.get("X_USERNAME", "TheHateCentral2").lstrip("@")
STATE_FILE = "state_rss.json"

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
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}
STATUS_RE = re.compile(r"/status/(\d+)")


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(seen):
    with open(STATE_FILE, "w") as f:
        json.dump({"seen": sorted(seen, key=int)[-300:]}, f)


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


def html_to_text(raw):
    raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    raw = re.sub(r"</p>", "\n", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", "", raw)
    return html.unescape(raw).strip()


def get_photos(entry):
    """Photo URLs only; [] if the post has a video or GIF."""
    summary = entry.get("summary", "")
    if re.search(r"<video", summary, flags=re.I):
        return []
    photos = []
    for src in re.findall(r'<img[^>]+src="([^"]+)"', summary):
        decoded = unquote(src)
        if "video" in decoded.lower():
            return []
        if "/media/" in decoded:
            photos.append(src)
    return photos


def download(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=60)
        if r.status_code == 200 and r.content:
            return r.content
        print(f"[image] {url} -> HTTP {r.status_code}")
    except requests.RequestException as e:
        print(f"[image] {url} -> {type(e).__name__}")
    return None


def send_photos(photos, caption):
    caption = caption[:1000]
    files = [d for d in (download(u) for u in photos[:10]) if d]
    if not files:
        return False

    if len(files) == 1:
        payload = {"chat_id": TG_CHANNEL}
        if caption:
            payload.update(caption=caption, parse_mode="HTML")
        r = requests.post(f"{TG_API}/sendPhoto", data=payload,
                          files={"photo": ("photo.jpg", files[0])}, timeout=120)
    else:
        media, upload = [], {}
        for i, data in enumerate(files):
            item = {"type": "photo", "media": f"attach://p{i}"}
            if i == 0 and caption:
                item.update(caption=caption, parse_mode="HTML")
            media.append(item)
            upload[f"p{i}"] = (f"p{i}.jpg", data)
        r = requests.post(f"{TG_API}/sendMediaGroup",
                          data={"chat_id": TG_CHANNEL, "media": json.dumps(media)},
                          files=upload, timeout=180)

    if not r.ok:
        print(f"[telegram] error {r.status_code}: {r.text}")
    return r.ok


def main():
    state = load_state()
    seen = set(state.get("seen", []))
    entries = fetch_feed()
    if not entries:
        return

    # First ever run: remember existing posts, don't post old ones
    if "seen" not in state:
        for e in entries:
            pid = entry_id(e)
            if pid:
                seen.add(pid)
        save_state(seen)
        print("Initialised. Only new posts from now on will be forwarded.")
        return

    new = [(entry_id(e), e) for e in entries
           if entry_id(e) and entry_id(e) not in seen and not is_repost_or_reply(e)]
    new.sort(key=lambda x: int(x[0]))

    for pid, e in new:
        photos = get_photos(e)
        if not photos:
            print(f"Skipped {pid} (no photo, or has video/GIF)")
            ok = True
        else:
            caption = html.escape(html_to_text(e.get("summary", "") or e.get("title", "")))
            ok = send_photos(photos, caption)
            if ok:
                print(f"Forwarded {pid}")
        if ok:
            seen.add(pid)
            save_state(seen)
        else:
            break  # retry next run, keep order


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # don't mark the workflow red for temporary glitches
        print(f"Run failed: {exc}")
        sys.exit(0)
