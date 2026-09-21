#!/usr/bin/env python3
"""
TikTok Kurdish-creator visitor (Playwright).

What it does (slowly and with human-like randomness):
  * finds NEW Kurdish creators (from Kurdish hashtag pages, or the For You feed)
  * opens their profile, stays a few seconds, moves on
  * rarely likes a video, and very rarely leaves a short Kurdish comment
  * never visits the same creator twice, respects daily limits and "active hours"

Runs headless, so it can live on a free cloud runner (GitHub Actions) or on your PC.
Login is done with your own browser cookies (TIKTOK_COOKIES / cookies.json).
"""
import json
import os
import random
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

# --------------------------------------------------------------------------- #
# Settings (can be overridden with environment variables)
# --------------------------------------------------------------------------- #
FEED_URL = os.getenv("FEED_URL", "https://www.tiktok.com/foryou")


def env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


RUN_MINUTES = env_float("RUN_MINUTES", 110)      # stop after this many minutes
MAX_VISITS = int(env_float("MAX_VISITS", 0))     # 0 = unlimited
WATCH_MIN = env_float("WATCH_MIN", 2)            # seconds "watching" a video
WATCH_MAX = env_float("WATCH_MAX", 6)
PROFILE_MIN = env_float("PROFILE_MIN", 3)        # seconds staying on a profile
PROFILE_MAX = env_float("PROFILE_MAX", 8)
PROFILE_HARD_MIN = env_float("PROFILE_HARD_MIN", 5)  # always stay on a profile at least this long (real seconds, not sped up)
DEDUPE = os.getenv("DEDUPE", "1") == "1"         # never visit the same creator twice
HEADLESS = os.getenv("HEADLESS", "1") == "1"
MAX_ERRORS_IN_ROW = int(env_float("MAX_ERRORS_IN_ROW", 8))
STALL_MINUTES = env_float("STALL_MINUTES", 0)      # stop if nothing is logged for this long (0 = off)
SPEED = max(0.5, env_float("SPEED", 1))          # 1 = normal, 2 = twice as fast, ...
ORIGIN = re.match(r"https?://[^/]+", FEED_URL).group(0)

SOURCE = os.getenv("SOURCE", "tags")             # "tags" = Kurdish hashtag pages, "feed" = For You
TAGS = [t.strip() for t in os.getenv(
    "TAGS",
    "kurdish,kurdistan,kurd,kurdm,kurdishtiktok,کوردی,کوردستان,کورد,هەولێر,سلێمانی,دهۆک",
).split(",") if t.strip()]
PER_TAG = int(env_float("PER_TAG", 12))          # profiles per hashtag page before reloading
RELOAD_EVERY = int(env_float("RELOAD_EVERY", 12))  # feed mode: reload the feed every N videos
KURDISH_ONLY = os.getenv("KURDISH_ONLY", "1") == "1"

# --- natural behaviour + safety limits ---
ACTIVE_HOURS = os.getenv("ACTIVE_HOURS", "10-23")  # only work in these local hours ("" = always)
OUTSIDE = os.getenv("OUTSIDE", "exit")           # outside hours / limit reached: "exit" or "wait"
PROFILE_CAP = int(env_float("PROFILE_CAP", 400))   # max profile visits per day
LIKE_CAP = int(env_float("LIKE_CAP", 50))          # max likes per day
COMMENT_CAP = int(env_float("COMMENT_CAP", 5))     # max comments per day
LIKE_CHANCE = env_float("LIKE_CHANCE", 0.10)       # chance to like a video
COMMENT_CHANCE = env_float("COMMENT_CHANCE", 0.03) # chance to comment on a video
COMMENT_STYLE = os.getenv("COMMENT_STYLE", "text")   # "text" (short Kurdish phrases) or "hearts" (only hearts)
SKIP_CHANCE = env_float("SKIP_CHANCE", 0.2)        # skip a creator now and then
SESSION_CAP = int(env_float("SESSION_CAP", 0))     # profiles per session (0 = no limit)
START_JITTER_MIN = env_float("START_JITTER_MIN", 0)  # random delay before starting (minutes)
STATS_FILE = Path("stats.json")
SEEN_MAX = 5000

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEBUG_DIR = Path("debug")

COMMENTS_CKB = [  # Sorani
    "زۆر جوانە 👏",
    "دەستت خۆش 🔥",
    "زۆر خۆشە ❤️",
    "بەڕاستی جوانە",
    "سوپاس 🙏",
    "ماشەاڵڵا 👌",
    "❤️❤️",
    "🔥🔥",
]
COMMENTS_KMR = [  # Kurmanji
    "Pir xweş e 👏",
    "Destê te sax 🔥",
    "Spas ❤️",
    "Pir baş e 👌",
    "❤️❤️",
    "🔥🔥",
]

SORANI_LETTERS = set("ێۆڕڵەڤ")  # letters used only in Sorani Kurdish
KURDISH_WORDS = (
    "kurd", "کورد", "هەولێر",
    "سلێمانی", "دهۆک",
    "kurmanc", "sorani", "hewler", "hawler", "erbil", "slemani", "sulaimani",
    "sulaymani", "duhok", "zakho", "rojava", "bashur", "rojhelat",
)


LAST_LOG = [time.time()]


def log(msg):
    LAST_LOG[0] = time.time()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def start_watchdog(deadline):
    """Safety net: if the browser freezes, end the run instead of hanging until GitHub kills it."""
    def run():
        while True:
            time.sleep(20)
            now = time.time()
            if now > deadline + 300:
                log("time is over but the bot did not stop by itself, stopping now")
                break
            if STALL_MINUTES and now - LAST_LOG[0] > STALL_MINUTES * 60:
                log(f"nothing happened for {int(STALL_MINUTES)} minutes (frozen browser), stopping now")
                break
        try:
            save_stats()
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=run, daemon=True).start()


def pause(lo, hi, floor=0.25):
    """Random wait, shortened by SPEED (never below floor seconds)."""
    time.sleep(max(floor, random.uniform(lo, hi) / SPEED))


# --------------------------------------------------------------------------- #
# Kurdish detection
# --------------------------------------------------------------------------- #
def is_kurdish(text):
    """True if the text has Sorani-only letters or Kurdish keywords/hashtags."""
    t = (text or "").lower()
    return any(ch in SORANI_LETTERS for ch in t) or any(w in t for w in KURDISH_WORDS)


def is_arabic_script(text):
    return any("؀" <= ch <= "ۿ" for ch in (text or ""))


COMMENTS_CUSTOM = []


def load_comments():
    global COMMENTS_CUSTOM
    try:
        lines = [x.strip() for x in Path("comments.txt").read_text(encoding="utf-8").splitlines()]
        COMMENTS_CUSTOM = [x for x in lines if x]
    except OSError:
        COMMENTS_CUSTOM = []


def pick_comment(context_text):
    if COMMENT_STYLE == "hearts":
        heart = chr(0x2764) + chr(0xFE0F)
        return random.choice([heart, heart, heart * 2, heart * 3, chr(0x1F60D) + heart,
                              chr(0x1F497), chr(0x1F49A) + heart, chr(0x1F495)])
    if COMMENTS_CUSTOM:
        return random.choice(COMMENTS_CUSTOM)
    return random.choice(COMMENTS_CKB if is_arabic_script(context_text) else COMMENTS_KMR)


# --------------------------------------------------------------------------- #
# Daily limits, active hours, stats (saved in stats.json)
# --------------------------------------------------------------------------- #
STATS = {}
SEEN = set()


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def load_stats():
    global STATS
    try:
        data = json.loads(STATS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    seen = data.get("seen", [])
    if not isinstance(seen, list):
        seen = []
    if data.get("date") != _today():  # new day: counters restart, the "seen" list is kept
        data = {"date": _today(), "profiles": 0, "likes": 0, "comments": 0}
    data["seen"] = seen[-SEEN_MAX:]
    STATS = data
    SEEN.clear()
    SEEN.update(STATS["seen"])
    save_stats()


def save_stats():
    try:
        STATS_FILE.write_text(json.dumps(STATS), encoding="utf-8")
    except OSError:
        pass


def bump(key):
    if STATS.get("date") != _today():
        load_stats()
    STATS[key] = STATS.get(key, 0) + 1
    save_stats()


def mark_seen(user):
    if user and user not in SEEN:
        SEEN.add(user)
        STATS.setdefault("seen", []).append(user)
        if len(STATS["seen"]) > SEEN_MAX:
            STATS["seen"] = STATS["seen"][-SEEN_MAX:]


def in_active_hours():
    if not ACTIVE_HOURS:
        return True
    try:
        a, b = (int(x) for x in ACTIVE_HOURS.split("-"))
    except ValueError:
        return True
    h = datetime.now().hour
    return (a <= h < b) if a <= b else (h >= a or h < b)


def allowed_now():
    if STATS.get("date") != _today():
        load_stats()
    if not in_active_hours():
        return False, f"outside active hours ({ACTIVE_HOURS})"
    if STATS.get("profiles", 0) >= PROFILE_CAP:
        return False, f"daily profile limit reached ({PROFILE_CAP})"
    return True, ""


def wait_until_allowed(deadline):
    """True when we may work now. 'wait' mode sleeps until we may; 'exit' mode gives up."""
    announced = False
    while True:
        ok, why = allowed_now()
        if ok:
            return True
        if OUTSIDE != "wait" or time.time() > deadline:
            log(f"not working now: {why}")
            return False
        if not announced:
            log(f"waiting: {why}")
            announced = True
        time.sleep(300)


def want(chance, key, cap):
    return chance > 0 and STATS.get(key, 0) < cap and random.random() < chance


class Pacer:
    """Counts profiles, takes random breaks, ends the session when needed."""

    def __init__(self):
        self.count = 0
        self.session = 0
        self.next_break = random.randint(20, 45)

    def done_one(self, deadline):
        """Call after every profile visit. Returns False when this run must end."""
        self.count += 1
        self.session += 1
        left = max(0, int((deadline - time.time()) / 60))
        log(f"TOTAL: {self.count} profiles this run, {STATS.get('profiles', 0)} today "
            f"(limit {PROFILE_CAP}), ~{left} min left")
        if MAX_VISITS and self.count >= MAX_VISITS:
            return False
        if SESSION_CAP and self.session >= SESSION_CAP:
            if OUTSIDE == "wait":
                mins = random.uniform(30, 90)
                log(f"session finished, resting {int(mins)} minutes")
                time.sleep(max(0, min(mins * 60, deadline - time.time())))
                self.session = 0
            else:
                log("session limit reached, ending this run")
                return False
        if self.count >= self.next_break:
            wait = random.uniform(60, 240)
            log(f"short break {int(wait)}s")
            time.sleep(wait)
            self.next_break = self.count + random.randint(20, 45)
        elif random.random() < 0.04:
            wait = random.uniform(20, 60)
            log(f"distracted for {int(wait)}s")
            time.sleep(wait)
        return True


# --------------------------------------------------------------------------- #
# Cookies
# --------------------------------------------------------------------------- #
def _fix_cookie(c):
    """Convert a cookie exported by Cookie-Editor / EditThisCookie to Playwright format."""
    out = {
        "name": c["name"],
        "value": c["value"],
        "domain": c.get("domain", ".tiktok.com"),
        "path": c.get("path", "/"),
        "secure": bool(c.get("secure", True)),
        "httpOnly": bool(c.get("httpOnly", False)),
    }
    exp = c.get("expirationDate", c.get("expires"))
    if isinstance(exp, (int, float)) and exp > 0:
        out["expires"] = float(exp)
    same = str(c.get("sameSite", "")).lower()
    if same in ("no_restriction", "none"):
        out["sameSite"] = "None"
    elif same == "strict":
        out["sameSite"] = "Strict"
    elif same == "lax":
        out["sameSite"] = "Lax"
    return out


def load_cookies():
    raw = os.getenv("TIKTOK_COOKIES", "").strip()
    if not raw and Path("cookies.json").exists():
        raw = Path("cookies.json").read_text(encoding="utf-8").strip()
    if not raw:
        log("ERROR: no cookies found. Set TIKTOK_COOKIES or create cookies.json")
        sys.exit(2)

    # 1) JSON (list or {"cookies": [...]})
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            data = data.get("cookies", [])
        return [_fix_cookie(c) for c in data if "name" in c and "value" in c]
    except (json.JSONDecodeError, TypeError):
        pass

    # 2) "name=value; name2=value2" header string
    cookies = []
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            cookies.append(
                {"name": k, "value": v, "domain": ".tiktok.com", "path": "/",
                 "secure": True, "httpOnly": False}
            )
    if not cookies:
        log("ERROR: could not understand the cookies format")
        sys.exit(2)
    return cookies


# --------------------------------------------------------------------------- #
# Page helpers
# --------------------------------------------------------------------------- #
# Finds the creator link of the video that is currently centred on screen.
FIND_AUTHOR_JS = r"""
() => {
  const vh = window.innerHeight, mid = vh / 2;
  const dist = el => {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return null;
    const cy = r.top + r.height / 2;
    if (cy < 0 || cy > vh) return null;
    return Math.abs(cy - mid);
  };
  const toLink = el => el.tagName === 'A' ? el : (el.closest('a') || el.querySelector('a') || el);
  const pick = sel => {
    let best = null, bestD = Infinity;
    for (const el of document.querySelectorAll(sel)) {
      const a = toLink(el);
      const d = dist(a);
      if (d === null) continue;
      const href = (a.getAttribute && a.getAttribute('href')) || '';
      if (/\/video\//.test(href)) continue;
      if (d < bestD) { best = a; bestD = d; }
    }
    return best;
  };
  return pick('[data-e2e="video-author-avatar"], [data-e2e="video-author-uniqueid"]')
      || pick('article a[href^="/@"]');
}
"""

# Returns the element (matching a selector) that is closest to the middle of the screen.
PICK_JS = r"""
(sel) => {
  const vh = window.innerHeight, mid = vh / 2;
  let best = null, bd = Infinity;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    const cy = r.top + r.height / 2;
    if (cy < 0 || cy > vh) continue;
    const d = Math.abs(cy - mid);
    if (d < bd) { best = el; bd = d; }
  }
  return best;
}
"""

# Text of the video (caption, hashtags, names) that is currently on screen in the feed.
ARTICLE_TEXT_JS = r"""
() => {
  const vh = window.innerHeight, mid = vh / 2;
  let best = null, bd = Infinity;
  for (const el of document.querySelectorAll('article')) {
    const r = el.getBoundingClientRect();
    const cy = r.top + r.height / 2;
    if (r.height < 4) continue;
    const d = Math.abs(cy - mid);
    if (d < bd) { best = el; bd = d; }
  }
  return best ? (best.innerText || '').slice(0, 800) : '';
}
"""

# On a hashtag page: every video (creator, id, caption + names).
HARVEST_JS = r"""
() => {
  const map = new Map();
  for (const a of document.querySelectorAll('a[href*="/video/"]')) {
    const m = (a.href || '').match(/@([^\/?#]+)\/video\/(\d+)/);
    if (!m) continue;
    const img = a.querySelector('img');
    const txt = ((a.innerText || '') + ' ' + (img ? (img.alt || '') : '')).trim();
    const cur = map.get(m[2]) || {user: m[1], vid: m[2], text: ''};
    cur.text = (cur.text + ' ' + txt).trim();
    map.set(m[2], cur);
  }
  return [...map.values()];
}
"""


def find_author(page):
    """Return (element, username) for the current video, or (None, None)."""
    try:
        handle = page.evaluate_handle(FIND_AUTHOR_JS)
        el = handle.as_element()
    except PWError:
        return None, None
    if el is None:
        return None, None
    user = None
    try:
        href = el.get_attribute("href") or ""
        m = re.search(r"/@([^/?#]+)", href)
        if m:
            user = m.group(1)
        else:
            txt = (el.inner_text() or "").strip().lstrip("@")
            user = txt or None
    except PWError:
        pass
    return el, user


def pick_center(page, selector):
    try:
        return page.evaluate_handle(PICK_JS, selector).as_element()
    except PWError:
        return None


def article_text(page):
    try:
        return page.evaluate(ARTICLE_TEXT_JS) or ""
    except PWError:
        return ""


def captcha_present(page):
    try:
        return page.locator(
            "#captcha_container, .captcha_verify_container, iframe[src*='captcha']"
        ).count() > 0
    except PWError:
        return False


def wait_for_feed(page, timeout=20000):
    page.wait_for_selector("article, [data-e2e='recommend-list-item-container']",
                           timeout=timeout)


def open_feed(page):
    page.goto(FEED_URL, wait_until="domcontentloaded", timeout=60000)
    wait_for_feed(page, 30000)
    pause(2, 4)


def is_logged_in(page):
    try:
        if page.locator("[data-e2e='top-login-button']").count() > 0:
            return False
    except PWError:
        pass
    return any(c["name"] in ("sessionid", "sessionid_ss") for c in page.context.cookies())


def go_next(page, prev_user):
    """Move to the next video. Tries keyboard, then the arrow button, then mouse wheel."""
    attempts = [
        lambda: page.keyboard.press("ArrowDown"),
        lambda: page.locator("[data-e2e='feed-navigation-next']").first.click(timeout=2000),
        lambda: page.mouse.wheel(0, 700),
    ]
    for act in attempts:
        try:
            act()
        except PWError:
            continue
        # wait (up to ~2.5s) until the next video is really on screen
        end = time.time() + 2.5
        while time.time() < end:
            time.sleep(0.25)
            _, user = find_author(page)
            if user and user != prev_user:
                return True
    return False


def advance(page, prev_user):
    """Next video. If the feed is stuck on the same video, reload it (no endless loops)."""
    if not go_next(page, prev_user):
        log("stuck on the same video, reloading the feed")
        open_feed(page)


def save_debug(page, name):
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(DEBUG_DIR / f"{name}.png"))
        (DEBUG_DIR / f"{name}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


def calm_videos(page):
    """Mute and pause the playing video: less CPU/RAM/sound, nothing else changes."""
    try:
        page.evaluate(
            "document.querySelectorAll('video').forEach(v => { v.muted = true; v.pause(); })"
        )
    except PWError:
        pass


def lower_priority():
    """Windows: run at 'below normal' priority so the PC stays responsive.
    The browser started afterwards inherits it."""
    if sys.platform == "win32":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetPriorityClass(k.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Likes and comments (rare, capped per day)
# --------------------------------------------------------------------------- #
COMMENT_FAILS = 0


def like_current(page):
    """Like the video that is on screen (if not liked yet)."""
    el = pick_center(page, '[data-e2e="like-icon"]')
    if el is None:
        return False
    try:
        if el.get_attribute("aria-pressed") == "true":
            return False
        pause(0.8, 2.0)
        el.click(timeout=4000)
        bump("likes")
        log(f"liked a video (today: {STATS['likes']}/{LIKE_CAP})")
        pause(0.5, 1.2)
        return True
    except (PWTimeout, PWError):
        return False


def close_comments(page):
    try:
        page.keyboard.press("Escape")
        page.evaluate("document.activeElement && document.activeElement.blur && "
                      "document.activeElement.blur()")
    except PWError:
        pass


def comment_current(page, context_text):
    """Write a short comment on the video that is on screen."""
    global COMMENT_FAILS
    icon = pick_center(page, '[data-e2e="comment-icon"]')
    if icon is None:
        return False
    text = pick_comment(context_text)
    try:
        icon.click(timeout=4000)
        box = None
        for sel in ('[data-e2e="comment-input"] [contenteditable="true"]',
                    '[data-e2e="comment-input"]', 'div[contenteditable="true"]',
                    '[role="textbox"]'):
            try:
                page.wait_for_selector(sel, state="visible", timeout=5000)
                box = page.locator(sel).first
                break
            except PWTimeout:
                continue
        if box is None:
            COMMENT_FAILS += 1
            log("comment box not found, skipping")
            return False
        pause(0.8, 2)
        box.click(timeout=3000)
        page.keyboard.type(text, delay=random.randint(70, 170))
        pause(0.8, 2)
        post = page.locator('[data-e2e="comment-post"]')
        posted = False
        if post.count() > 0:
            try:
                post.first.click(timeout=3000)
                posted = True
            except (PWTimeout, PWError):
                pass  # button not clickable right now, fall back to Enter below
        if not posted:
            page.keyboard.press("Enter")
        COMMENT_FAILS = 0
        bump("comments")
        log(f"commented (today: {STATS['comments']}/{COMMENT_CAP})")
        pause(1.5, 3)
        return True
    except (PWTimeout, PWError) as e:
        COMMENT_FAILS += 1
        log(f"comment failed, skipping ({type(e).__name__})")
        return False
    finally:
        close_comments(page)


# --------------------------------------------------------------------------- #
# Profile visit
# --------------------------------------------------------------------------- #
def block_heavy(tab):
    """A profile tab needs no pictures/videos/fonts: block them to save CPU + data."""
    tab.route(
        "**/*",
        lambda r: r.abort()
        if r.request.resource_type in ("image", "media", "font")
        else r.continue_(),
    )


def dwell_on_profile(tab, arrived_at=None):
    if arrived_at is not None:
        left = PROFILE_HARD_MIN - (time.time() - arrived_at)
        if left > 0:
            time.sleep(left)  # never leave a profile before this many real seconds have passed
    pause(1, 2)
    try:
        tab.mouse.wheel(0, random.randint(150, 500))
    except PWError:
        pass
    pause(PROFILE_MIN, PROFILE_MAX, floor=1.5)  # a little extra, natural variation


def act_on_profile(tab, do_like, do_comment, context_text, target_vid=None):
    """Open a video from the CREATOR'S OWN profile grid and like/comment it there
    (not on the main feed / hashtag page). Verified against the real TikTok profile
    layout: [data-e2e="user-post-item"] opens an in-page overlay with the usual
    like-icon / comment-icon, closed with the [aria-label="Close"] button."""
    try:
        tab.wait_for_selector('[data-e2e="user-post-item"]', timeout=8000)
    except PWTimeout:
        return False  # empty or private profile: nothing to like/comment on
    try:
        posts = tab.locator('[data-e2e="user-post-item"]')
        count = posts.count()
        if count == 0:
            return False
        opened = False
        if target_vid:
            match = tab.locator(f'[data-e2e="user-post-item"]:has(a[href*="/video/{target_vid}"])')
            if match.count() > 0:
                match.first.click(timeout=4000)
                opened = True
        if not opened:
            posts.nth(random.randint(0, min(2, count - 1))).click(timeout=4000)
        tab.wait_for_selector('[data-e2e="like-icon"]', timeout=8000)
        pause(1.5, 3)
        if do_like:
            like_current(tab)
        if do_comment:
            comment_current(tab, context_text)
        pause(1, 2.5)
        return True
    except (PWTimeout, PWError):
        return False
    finally:
        try:
            close_btn = tab.locator('[aria-label="Close"]').first
            if close_btn.count() > 0:
                close_btn.click(timeout=3000)
        except PWError:
            pass


def record_visit(user):
    bump("profiles")
    mark_seen(user)
    save_stats()
    log(f"opened profile @{user or '?'}")
    try:
        with open("visits.txt", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  @{user or '?'}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# SOURCE = feed : For You feed, only Kurdish videos
# --------------------------------------------------------------------------- #
def visit_feed_video(page):
    """Returns 'visited', 'skipped' or raises on failure."""
    if "/@" in page.url or not page.url.startswith(ORIGIN):
        open_feed(page)

    wait_for_feed(page)
    pause(0.5, 1)  # let the video settle

    el, user = find_author(page)
    if el is None:
        advance(page, None)
        return "skipped"

    text = article_text(page) + " " + (user or "")
    if KURDISH_ONLY and not is_kurdish(text):
        advance(page, user)  # not Kurdish: swipe on quickly
        return "skipped"
    if DEDUPE and user and user in SEEN:
        advance(page, user)
        return "skipped"

    pause(WATCH_MIN, WATCH_MAX)  # "watch" the video a bit
    if random.random() < 0.12:
        pause(8, 20)  # sometimes really watch it

    do_like = want(LIKE_CHANCE, "likes", LIKE_CAP)
    do_comment = COMMENT_FAILS < 3 and want(COMMENT_CHANCE, "comments", COMMENT_CAP)

    if random.random() < SKIP_CHANCE or not user:
        advance(page, user)
        return "skipped"

    tab = page.context.new_page()
    try:
        if not (do_like or do_comment):
            block_heavy(tab)
        arrived = time.time()
        tab.goto(f"{ORIGIN}/@{user}", wait_until="domcontentloaded", timeout=30000,
                 referer=FEED_URL)
        if do_like or do_comment:
            # like/comment on a video from THEIR profile, not on the main feed
            act_on_profile(tab, do_like, do_comment, text)
        dwell_on_profile(tab, arrived)
    finally:
        try:
            tab.close()
        except PWError:
            pass
    try:
        page.bring_to_front()
    except PWError:
        pass

    record_visit(user)
    advance(page, user)
    calm_videos(page)
    return "visited"


def run_feed(page, deadline, pacer):
    errors, checked = 0, 0
    while time.time() < deadline:
        if not wait_until_allowed(deadline):
            break
        try:
            if captcha_present(page):
                log("captcha detected, waiting a bit and reloading")
                save_debug(page, f"captcha_{int(time.time())}")
                time.sleep(random.uniform(60, 120))
                open_feed(page)
                errors += 1
            else:
                result = visit_feed_video(page)
                checked += 1
                if result != "visited":
                    pause(1.0, 3.0)
                if result == "visited":
                    errors = 0
                    if not pacer.done_one(deadline):
                        break
                if RELOAD_EVERY and checked % RELOAD_EVERY == 0:
                    open_feed(page)  # quick reload: brings new creators, frees memory
        except KeyboardInterrupt:
            break
        except Exception as e:  # keep running, recover by reloading the feed
            errors += 1
            log(f"error ({errors}/{MAX_ERRORS_IN_ROW}): {type(e).__name__}: {str(e)[:150]}")
            save_debug(page, f"error_{errors}")
            try:
                time.sleep(random.uniform(3, 6))
                open_feed(page)
            except Exception:
                pass
        if errors >= MAX_ERRORS_IN_ROW:
            log("too many errors in a row, stopping")
            save_debug(page, "too_many_errors")
            return 4
    return 0


# --------------------------------------------------------------------------- #
# SOURCE = tags : Kurdish hashtag pages -> new Kurdish creators
# --------------------------------------------------------------------------- #
def diag(page):
    """Log what the page really shows (helps when TikTok serves an empty/blocked page)."""
    try:
        info = page.evaluate(
            "() => ({u: location.href, t: document.title, "
            "v: document.querySelectorAll('a[href*=\"/video/\"]').length, "
            "b: (document.body.innerText || '').replace(/\\s+/g, ' ').slice(0, 160)})")
        log(f"page shows: url={info.get('u')} title={info.get('t')!r} video_links={info.get('v')} "
            f"text={info.get('b')!r}")
    except Exception:
        pass


def collect_candidates(page, tag):
    """Open a hashtag page and return NEW Kurdish creators found there."""
    page.goto(f"{ORIGIN}/tag/{quote(tag)}", wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_selector('a[href*="/video/"]', timeout=25000)
    except PWTimeout:
        diag(page)
        raise
    pause(2, 4)
    for _ in range(random.randint(2, 4)):  # scroll to load more videos
        try:
            page.mouse.wheel(0, random.randint(1200, 2400))
        except PWError:
            pass
        pause(1.2, 2.5)
    items = page.evaluate(HARVEST_JS) or []
    cands, users = [], set()
    for it in items:
        user = it.get("user")
        if not user or user in users:
            continue
        if DEDUPE and user in SEEN:
            continue
        if KURDISH_ONLY and not is_kurdish(it.get("text", "") + " " + user):
            continue
        users.add(user)
        cands.append(it)
    random.shuffle(cands)
    return cands


def visit_candidate(ctx, cand):
    """Visit the creator's profile; (rarely) like/comment happens ON the profile
    (opening one of their own videos there), never on the hashtag page."""
    user = cand["user"]
    do_like = want(LIKE_CHANCE, "likes", LIKE_CAP)
    do_comment = COMMENT_FAILS < 3 and want(COMMENT_CHANCE, "comments", COMMENT_CAP)
    tab = ctx.new_page()
    try:
        if not (do_like or do_comment):
            block_heavy(tab)
        arrived = time.time()
        tab.goto(f"{ORIGIN}/@{user}", wait_until="domcontentloaded", timeout=30000,
                 referer=ORIGIN + "/")
        if do_like or do_comment:
            act_on_profile(tab, do_like, do_comment, cand.get("text", ""), target_vid=cand.get("vid"))
        dwell_on_profile(tab, arrived)
    finally:
        try:
            tab.close()
        except PWError:
            pass
    record_visit(user)


def run_tags(ctx, page, deadline, pacer):
    errors, empty_rounds, last_tag, tag_fail = 0, 0, None, 0
    while time.time() < deadline:
        if not wait_until_allowed(deadline):
            break
        tag = random.choice([t for t in TAGS if t != last_tag] or TAGS)
        last_tag = tag
        try:
            if captcha_present(page):
                log("captcha detected, waiting a bit")
                save_debug(page, f"captcha_{int(time.time())}")
                time.sleep(random.uniform(60, 120))
                errors += 1
                continue
            cands = collect_candidates(page, tag)
            tag_fail = 0
            log(f"#{tag}: {len(cands)} new Kurdish creators found")
            if not cands:
                empty_rounds += 1
                if empty_rounds >= max(3, len(TAGS)):
                    log("no new Kurdish creators found on any hashtag, stopping")
                    return 0
                continue
            empty_rounds = 0
            done_here = 0
            for cand in cands:
                if time.time() >= deadline or done_here >= PER_TAG:
                    break
                ok, _ = allowed_now()
                if not ok:
                    break
                if random.random() < SKIP_CHANCE:
                    continue
                visit_candidate(ctx, cand)
                done_here += 1
                errors = 0
                if not pacer.done_one(deadline):
                    return 0
        except KeyboardInterrupt:
            break
        except Exception as e:
            errors += 1
            log(f"error ({errors}/{MAX_ERRORS_IN_ROW}): {type(e).__name__}: {str(e)[:150]}")
            save_debug(page, f"error_{errors}")
            if isinstance(e, PWTimeout) and "/video/" in str(e):
                tag_fail += 1
            time.sleep(random.uniform(3, 6))
            if tag_fail >= 2:
                # hashtag pages come back empty from this server: use the For You feed instead
                log("hashtag pages are empty here, switching to the For You feed (Kurdish only)")
                try:
                    open_feed(page)
                except Exception as e2:
                    log(f"could not open the feed: {type(e2).__name__}")
                    return 4
                return run_feed(page, deadline, pacer)
        if errors >= MAX_ERRORS_IN_ROW:
            log("too many errors in a row, stopping")
            save_debug(page, "too_many_errors")
            return 4
    return 0


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    cookies = load_cookies()
    load_stats()
    load_comments()
    lower_priority()
    deadline = time.time() + RUN_MINUTES * 60

    if not wait_until_allowed(deadline):
        return
    if START_JITTER_MIN > 0 and OUTSIDE != "wait":
        delay = random.uniform(0, START_JITTER_MIN * 60)
        log(f"random start delay: {int(delay / 60)} min")
        time.sleep(delay)
        if not wait_until_allowed(deadline):
            return

    code = 0
    start_watchdog(deadline)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                  "--mute-audio", "--disable-extensions"],
        )
        ctx = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 768},
            locale="en-US",
        )
        ctx.add_cookies(cookies)
        page = ctx.new_page()
        page.set_default_timeout(20000)

        try:
            open_feed(page)
        except Exception as e:
            log(f"could not open TikTok: {e}")
            save_debug(page, "open_failed")
            sys.exit(3)

        if not is_logged_in(page):
            log("NOT LOGGED IN - the cookies are missing or expired. Export them again.")
            save_debug(page, "not_logged_in")
            sys.exit(2)
        log(f"logged in, starting ({SOURCE} mode, Kurdish only: {KURDISH_ONLY})")

        pacer = Pacer()
        if SOURCE == "feed":
            code = run_feed(page, deadline, pacer)
        else:
            code = run_tags(ctx, page, deadline, pacer)

        log(f"finished. this run: {pacer.count} profiles. today: {STATS.get('profiles', 0)} "
            f"profiles, {STATS.get('likes', 0)} likes, {STATS.get('comments', 0)} comments")
        save_stats()
        browser.close()
    if code:
        sys.exit(code)


if __name__ == "__main__":
    main()
