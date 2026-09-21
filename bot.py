#!/usr/bin/env python3
"""
TikTok profile hopper (Playwright).

Loop:
  For You feed -> open the creator's profile -> go back -> next video -> repeat

Runs headless, so it can live on a free cloud runner (GitHub Actions) and does
not need your own computer.

Login is done with your own browser cookies (see README_KU.md) because TikTok
blocks password logins from servers.
"""
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

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


RUN_MINUTES = env_float("RUN_MINUTES", 335)      # stop after this many minutes
MAX_VISITS = int(env_float("MAX_VISITS", 0))     # 0 = unlimited
WATCH_MIN = env_float("WATCH_MIN", 2)            # seconds "watching" a video
WATCH_MAX = env_float("WATCH_MAX", 6)
PROFILE_MIN = env_float("PROFILE_MIN", 3)        # seconds staying on a profile
PROFILE_MAX = env_float("PROFILE_MAX", 8)
BREAK_EVERY = int(env_float("BREAK_EVERY", 40))  # long pause every N visits
DEDUPE = os.getenv("DEDUPE", "1") == "1"         # skip creators already visited
HEADLESS = os.getenv("HEADLESS", "1") == "1"
MAX_ERRORS_IN_ROW = int(env_float("MAX_ERRORS_IN_ROW", 8))
SPEED = max(0.5, env_float("SPEED", 1))          # 1 = normal, 3 = 3x faster, ...
MODE = os.getenv("MODE", "tab")                  # "tab" = light (default), "back" = click + Back
RELOAD_EVERY = int(env_float("RELOAD_EVERY", 40))  # refresh the feed now and then (frees memory)
ORIGIN = re.match(r"https?://[^/]+", FEED_URL).group(0)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEBUG_DIR = Path("debug")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def pause(lo, hi, floor=0.25):
    """Random wait, shortened by SPEED (never below floor seconds)."""
    time.sleep(max(floor, random.uniform(lo, hi) / SPEED))


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


def save_debug(page, name):
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(DEBUG_DIR / f"{name}.png"))
        (DEBUG_DIR / f"{name}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Opening a profile
# --------------------------------------------------------------------------- #
def open_profile_tab(page, user):
    """Light mode: open the profile in a second tab, stay a moment, close it.
    The feed tab stays open, so the heavy feed + videos are NOT reloaded every time."""
    tab = page.context.new_page()
    try:
        # the profile tab needs no pictures/videos/fonts: block them to save CPU + data
        tab.route(
            "**/*",
            lambda r: r.abort()
            if r.request.resource_type in ("image", "media", "font")
            else r.continue_(),
        )
        tab.goto(f"{ORIGIN}/@{user}", wait_until="domcontentloaded", timeout=30000,
                 referer=FEED_URL)
        pause(1, 2)
        try:
            tab.mouse.wheel(0, random.randint(150, 500))
        except PWError:
            pass
        pause(PROFILE_MIN, PROFILE_MAX, floor=1.5)  # long enough for the visit to count
    finally:
        try:
            tab.close()
        except PWError:
            pass
    try:
        page.bring_to_front()
    except PWError:
        pass


def open_profile_and_back(page, el, user):
    """Classic mode: click the creator, wait, press Back (reloads the feed each time)."""
    try:
        el.click(timeout=5000)
        page.wait_for_url(re.compile(r".*/@[^/]+/?(\?.*)?$"), timeout=15000)
    except (PWTimeout, PWError):
        if user:
            page.goto(f"{ORIGIN}/@{user}", wait_until="domcontentloaded", timeout=30000)
        else:
            raise
    pause(1, 2)
    try:
        page.mouse.wheel(0, random.randint(150, 500))
    except PWError:
        pass
    pause(PROFILE_MIN, PROFILE_MAX, floor=1.5)
    page.go_back(wait_until="domcontentloaded", timeout=30000)
    try:
        wait_for_feed(page, 20000)
    except PWTimeout:
        open_feed(page)
    pause(1.5, 3)


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
# One iteration: video -> profile -> next video
# --------------------------------------------------------------------------- #
def visit_one(page, visited):
    """Returns 'visited', 'skipped' or raises on failure."""
    if "/@" in page.url or not page.url.startswith(ORIGIN):
        open_feed(page)

    wait_for_feed(page)
    pause(WATCH_MIN, WATCH_MAX)  # "watch" the video a bit

    el, user = find_author(page)
    if el is None:
        log("no creator link on screen, moving to the next video")
        go_next(page, None)
        return "skipped"

    if DEDUPE and user and user in visited:
        log(f"already visited @{user}, skipping")
        go_next(page, user)
        return "skipped"

    if MODE == "tab" and user:
        open_profile_tab(page, user)
    else:
        open_profile_and_back(page, el, user)

    if user:
        visited.add(user)
    log(f"opened profile @{user or '?'}")
    try:
        with open("visits.txt", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  @{user or '?'}\n")
    except OSError:
        pass
    go_next(page, user)
    calm_videos(page)
    return "visited"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    cookies = load_cookies()
    lower_priority()
    deadline = time.time() + RUN_MINUTES * 60
    visited, count, errors = set(), 0, 0

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
        log("logged in, starting the loop")

        while time.time() < deadline and (MAX_VISITS == 0 or count < MAX_VISITS):
            try:
                if captcha_present(page):
                    log("captcha detected, waiting a bit and reloading")
                    save_debug(page, f"captcha_{int(time.time())}")
                    time.sleep(random.uniform(60, 120))
                    open_feed(page)
                    errors += 1
                else:
                    result = visit_one(page, visited)
                    if result == "visited":
                        count += 1
                        errors = 0
                        left = max(0, int((deadline - time.time()) / 60))
                        log(f"TOTAL: {count} profiles visited (~{left} min left)")
                        if BREAK_EVERY and count % BREAK_EVERY == 0:
                            wait = random.uniform(30, 90)
                            log(f"short break {int(wait)}s")
                            time.sleep(wait)
                        if RELOAD_EVERY and count % RELOAD_EVERY == 0:
                            open_feed(page)  # fresh feed: frees the browser's memory
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
                browser.close()
                sys.exit(4)

        log(f"finished. total profiles visited: {count}")
        browser.close()


if __name__ == "__main__":
    main()
