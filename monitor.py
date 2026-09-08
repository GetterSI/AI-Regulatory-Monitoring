#!/usr/bin/env python3
"""
Cloud Regulatory Watch — standalone daily monitor.

Runs entirely on its own (GitHub Actions, cron, or any machine with Python) —
no Claude, no Cowork, no desktop app required at runtime. It:

  1. Loads watchlist.json (the list of pages to watch).
  2. Loads snapshots.json (what each page looked like last time).
  3. Fetches every URL, extracts the substantive text (or hashes it, for
     binaries like PDFs), and filters out obvious site chrome.
  4. Classifies each page as new / unchanged / changed / gap.
  5. If any real (non-cosmetic) changes were found, opens ONE GitHub Issue
     summarizing them all — GitHub emails the repo owner automatically when
     an issue is opened, so this needs no external notification service.
  6. Writes snapshots.json, runs.json and DASHBOARD.md back out so the next
     run — and a GitHub Actions commit step — can pick up where this one left
     off. DASHBOARD.md is a Markdown dashboard GitHub renders natively at its
     normal blob URL, gated by GitHub's own login, so a private repo stays
     private with no extra hosting.

Environment variables (both provided automatically by GitHub Actions —
nothing to configure):
  GITHUB_TOKEN        auto-injected token, used to open the issue
  GITHUB_REPOSITORY   "owner/repo", used to target the right repo's API
"""
import os
import re
import sys
import json
import time
import gzip
import zlib
import hashlib
import difflib
import datetime
import urllib.request
import urllib.error
import urllib.parse
from zoneinfo import ZoneInfo

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependency: pip install -r requirements.txt", file=sys.stderr)
    raise

# Playwright (headless Chromium) is an OPTIONAL last-resort fallback for
# pages that block plain urllib requests (IP-reputation/bot-management
# blocks that no header or User-Agent tweak can get past). It is not a
# hard dependency: if it isn't installed, the monitor still runs fine on
# urllib alone, it just won't have this extra fallback layer.
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_PATH = os.path.join(BASE_DIR, "watchlist.json")
SNAPSHOTS_PATH = os.path.join(BASE_DIR, "snapshots.json")
RUNS_PATH = os.path.join(BASE_DIR, "runs.json")
DASHBOARD_PATH = os.path.join(BASE_DIR, "DASHBOARD.md")

GITHUB_API_URL = "https://api.github.com"
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 CloudRegulatoryWatch/1.0"
)
# FlareSolverr (see daily-watch.yml) is a free, self-hosted, open-source
# service — no account or API key needed — run as a Docker container
# alongside this job. It drives a stealth-patched Chromium purpose-built to
# solve Cloudflare-style JS challenges ("just a moment", "one moment,
# please", etc.), which is a step up from a generic Playwright fetch for
# exactly that failure class. It is tried as the LAST resort, after both a
# plain urllib fetch and a generic Playwright fetch have failed, so it never
# spends time on pages the free methods already handle. It cannot get past a
# hard IP-reputation block — this job's outbound IP is still GitHub's — only
# a software challenge that a convincing-enough browser can clear.
FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://localhost:8191/v1")
FLARESOLVERR_TIMEOUT_MS = 60000
# Alternate identities used only as fallback retries when the primary
# request is blocked (403) or times out. Some sites' bot-management rules
# explicitly allowlist known search-engine crawlers even while blocking
# generic scripts, so a Googlebot-style UA occasionally gets through where
# a plain browser UA from a datacenter IP does not.
UA_FIREFOX = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0"
UA_GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
REQUEST_TIMEOUT = 30
PLAYWRIGHT_NAV_TIMEOUT_MS = 35000
PLAYWRIGHT_IDLE_TIMEOUT_MS = 12000
# How many times to retry FlareSolverr itself before giving up on a page.
# Solving a Cloudflare-style challenge is inherently non-deterministic — the
# same URL that gets a clean solve on one run can come back "flaresolverr:
# HTTP Error 500: Internal Server Error" on the next (observed 2026-09-07 for
# rows 52/57/76/131, the exact rows a previous run had fetched cleanly). A
# single extra attempt costs little and recovers most of these.
FLARESOLVERR_ATTEMPTS = 2
FLARESOLVERR_RETRY_DELAY = 5
# No text-length cap. Charles, 2026-09-07: "We need to analyse the entire
# contents of the page. Otherwise this is pointless." Every character of
# extracted, chrome-stripped text is compared, however long the page is —
# at the cost of a larger snapshots.json and (for the rare huge page) a
# slower classify_change() diff. That tradeoff is intentional: a change
# past whatever cutoff we'd otherwise pick is exactly the kind of change
# this tool exists to catch.

# --- Image comparison ----------------------------------------------------
# Some regulatory pages publish substantive content as an image (a fee
# table rendered as a graphic, a scanned notice, a chart) that a text-only
# diff can never see. This does a best-effort second channel: find images
# that look like real page content (not logos, icons, nav chrome, or
# tracking pixels), fetch each one, and hash it. A changed hash — or an
# image added/removed — is treated as a real page change exactly like a
# text change, even when the surrounding text is byte-identical.
MAX_IMAGES_PER_PAGE = 6  # cap per page — keeps run time and byte-hashing
    # bounded even on an image-heavy page; content images are rare enough
    # per page that this ceiling should never bind in practice
IMAGE_FETCH_TIMEOUT = 10
MIN_CONTENT_IMAGE_DIMENSION = 100  # px; an <img> with an explicit width or
    # height below this is almost always a logo/icon/badge, not content
SKIP_IMAGE_EXTENSIONS = (".svg", ".ico", ".gif")
    # .svg/.ico are logos and favicons; .gif on these sites is essentially
    # always a spinner, tracking pixel, or decorative flourish — never
    # observed to be the actual regulatory content
SKIP_IMAGE_PATTERN = re.compile(
    r"logo|icon|sprite|badge|avatar|spinner|loading|placeholder|"
    r"pixel|1x1|spacer|banner-ad|flag-|arrow|chevron|social|share",
    re.IGNORECASE,
)

# Lines matched by any of these (case-insensitive) are treated as site chrome,
# not substance, and dropped before comparing snapshots.
CHROME_PATTERNS = [
    r"^\s*(home|menu|search|login|log in|sign in|sign up|subscribe|newsletter)\s*$",
    r"^\s*(contact us|about us|careers|sitemap|follow us)\s*$",
    r"cookie (policy|settings|consent|banner)",
    r"privacy policy|terms of (use|service)|all rights reserved",
    r"^\s*©?\s*\d{4}\s",  # bare copyright lines
    r"facebook\.com|twitter\.com|x\.com/|linkedin\.com|instagram\.com|youtube\.com|tiktok\.com",
    r"^\s*(skip to (main )?content)\s*$",
    r"^\s*(select (a )?language|choose (your )?country)\s*$",
]
CHROME_RE = re.compile("|".join(CHROME_PATTERNS), re.IGNORECASE)

# Consent-manager and cookie-wall UI. Checked UNCONDITIONALLY, and before the
# CHROME_RE/KEEP_HINTS_RE pair below, because a consent line that happens to
# carry a year ("(c) 2026 OneTrust") escapes the chrome filter via
# KEEP_HINTS_RE and lands in the diff as though it were page content. Runs #12
# and #13 each reported consent banners as regulatory changes: row 5
# ("Consent | Details | [#IABV2SETTINGS#]"), row 145 ("Accept | Deny
# Non-Essential") and row 72 ("This site uses cookies ... cookies policy
# page"). The old CHROME_PATTERNS entry only covered "cookie policy/settings/
# consent/banner" and caught none of the three.
#
# Compound phrases ONLY. Never match the bare word "consent": prior informed
# consent, and Regulation (EU) 649/2012 (PIC), are real regulatory substance,
# so a filter that broad would silently delete the very thing being watched
# for. tests/test_filters.py asserts PIC lines survive.
CONSENT_PATTERNS = [
    r"\[#IABV2SETTINGS#\]",
    r"\bIAB\s*(TCF|Europe)\b",
    r"deny non-?essential",
    r"accept (all|only necessary|essential)? ?cookies?\b",
    r"\ballow all cookies\b",
    r"manage (my )?(cookie|consent|privacy) (preferences|settings|choices)",
    r"cookie (policy|settings|consent|banner|preferences|notice|statement)",
    r"cookies? policy page",
    r"(we|this (site|website)) uses? cookies",
    r"\b(strictly )?necessary cookies\b",
    r"\bnon-essential cookies\b",
    r"consent management (platform|provider)",
    r"\b(onetrust|cookiebot|usercentrics|trustarc|quantcast choice)\b",
    r"your privacy choices|do not sell my personal information",
    r"^\s*(accept|reject|decline)( all)?\s*[|.-]?\s*(details|settings|preferences|more info)\s*$",
]
CONSENT_RE = re.compile("|".join(CONSENT_PATTERNS), re.IGNORECASE)

# If a line contains any of these, it is almost always substance — never
# drop it even if it superficially resembles chrome.
KEEP_HINTS_RE = re.compile(
    r"[€$£]|%|\bfee\b|\btariff\b|\bthreshold\b|\bdeadline\b|\bversion\b|"
    r"\bamend|\beffective\b|\bkg\b|\bton(ne)?s?\b|\d{4}-\d{2}-\d{2}|"
    r"\b(19|20)\d{2}\b",
    re.IGNORECASE,
)

BINARY_CONTENT_TYPES = ("application/pdf", "application/octet-stream", "application/zip")

# Cloudflare/Akamai/Incapsula-style bot-challenge interstitials return a
# normal HTTP 200 with real HTML, so they pass every check in fetch() and
# fetch_with_retry() as a "successful" response. But the body is just a
# challenge page containing a unique per-request token (Cloudflare calls it
# a "Ray ID") that is different on every single load. If that token gets
# scraped as the page's "text", classify_change() sees a different value
# every run and reports a fake "changed" page forever — this is exactly
# what happened to rows 52/57/76/131 (confirmed by opening each URL
# directly: the runner's IP gets challenged even though a normal browser
# session does not). Any response matching one of these markers must be
# treated as a failed fetch, never stored as a snapshot.
BOT_CHALLENGE_MARKERS = (
    "just a moment",
    "performing security verification",
    "checking your browser before accessing",
    "enable javascript and cookies to continue",
    "attention required! | cloudflare",
    "ddos protection by cloudflare",
    "cf-browser-verification",
    "cf_chl_",
    "__cf_chl_rt_tk",
    "sorry, you have been blocked",
    "request unsuccessful. incapsula",
    "distil_r_captcha",
    "please verify you are a human",
    "pardon our interruption",
    "captcha-delivery.com",
    # New template found 2026-09-06: 6 pages (LBMA, ReSimple, Hellenic
    # Copyright Organization, Paristokierrätys x2, RLG WEEE Romania,
    # El-Kretsen) all flipped to this exact challenge wording and back to
    # real content the next day, firing false "changed" alerts each time.
    "one moment, please",
    "please wait while your request is being verified",
    # Found 2026-09-07 for rows 9/125 (Alberta Recycling, both pages of the
    # same site): a different WAF vendor's interstitial that was being
    # stored as if it were real 80-character page content instead of being
    # caught and escalated -- not in any of the markers above, so it
    # slipped through silently rather than triggering a retry.
    "robot challenge screen",
    "checking the site connection security",
)


def is_bot_challenge(html_bytes, content_type):
    """True if raw looks like a bot-challenge interstitial rather than the
    real page. Sniffs a generous prefix of the raw response (the challenge
    markup is always near the top, but FlareSolverr's rendered-Chrome output
    for a Turnstile-style challenge often carries several KB of injected
    analytics/JS before the visible "Just a moment..." text — a 4000-byte
    window missed that variant in production on 2026-09-07 for rows
    52/57/76/131, letting Cloudflare's challenge page through as if it were
    real content. 20000 bytes comfortably covers that case while still being
    cheap for the rare huge page) and only for text responses — binaries
    (PDFs, etc.) never hit this path."""
    if not html_bytes or any(bt in (content_type or "") for bt in BINARY_CONTENT_TYPES):
        return False
    try:
        sample = html_bytes[:20000].decode("utf-8", errors="ignore").lower()
    except Exception:  # noqa: BLE001
        return False
    return any(marker in sample for marker in BOT_CHALLENGE_MARKERS)


def text_is_bot_challenge(text):
    """Second, size-independent safety net: checks the same markers against
    the already-extracted, script/style-stripped visible text rather than
    raw HTML bytes. Catches a challenge page that slipped past
    is_bot_challenge()'s byte-window (e.g. a fetch layer whose raw response
    structure pushes the challenge markup further down than expected)
    before it can ever be compared against the previous snapshot or stored
    as if it were real content. Only samples the first 4000 characters —
    independent of whether the full extracted text is capped — since a
    genuine challenge page's banner is always right at the top; there is
    no need to scan a whole (possibly very long) page looking for it."""
    if not text:
        return False
    sample = text[:4000].lower()
    return any(marker in sample for marker in BOT_CHALLENGE_MARKERS)


# Charles, 2026-09-07: "add a section for links that are expired or no
# longer working." A different failure class from BOT_CHALLENGE_MARKERS
# above: these pages fetch completely successfully -- normal HTTP 200, no
# security interstitial, every fetch layer agrees -- but the content itself
# says the specific resource is gone. Confirmed 2026-09-07 for 2 CIRCABC
# documents (appearing as 3 watchlist rows -- 65/161/187, with 65 and 187
# being a literal duplicate URL): circabc.europa.eu/ui/no-content says "The
# file or folder does not exists. It is maybe deleted or removed." This is
# a soft-404 -- the kind of client-side-rendered SPA response that never
# raises a real HTTP 404 status for urllib to catch, so it looks fetchable
# forever. No fetch layer can ever recover it: the document really is gone
# from the source, not a bot-management or rendering-timing problem. This
# is the "genuinely broken" case, distinct from a "gap" (couldn't fetch
# anything at all) -- tracked as its own status so it gets a dedicated,
# honest list instead of silently sitting as an unremarkable "OK" row just
# because the fetch technically succeeded.
DEAD_LINK_MARKERS = (
    "the file or folder does not exists",
    "it is maybe deleted or removed",
    "woops! nothing found here",
    "the link you followed seems valid, but the content is missing",
    "the page you are looking for does not exist",
    "the page you requested was not found",
    "the page you were looking for doesn't exist",
    "we can't find the page you're looking for",
    "we couldn't find the page you were looking for",
    "sorry, this page isn't available",
    "sorry, we couldn't find that page",
    "oops! that page can't be found",
    "the requested url was not found on this server",
    "404 - page not found",
    "404 not found",
    "http error 404",
)


def is_dead_link(text):
    """Returns the matched line of already-extracted visible text if it
    reads like the target resource itself is gone (see DEAD_LINK_MARKERS
    above), or None otherwise -- not a bot-challenge
    (text_is_bot_challenge) and not a fetch failure (gaps). Only samples
    the first 3000 characters: every known not-found template states it
    right at the top, so this can't miss on a long, real page that happens
    to mention "404" somewhere deep in unrelated content. Deliberately
    conservative phrase list (full compound sentences, not bare words like
    "404") to keep false positives on genuine content rare; if a real page
    is ever misflagged, the fix is to remove or tighten the specific
    marker that matched, not to abandon the check. Returning the matched
    line (not just True/False) lets the caller show Charles exactly what
    the source page says, instead of a generic "this looks dead" note."""
    if not text:
        return None
    sample = text[:3000].lower()
    for marker in DEAD_LINK_MARKERS:
        if marker in sample:
            for line in text[:3000].splitlines():
                if marker in line.lower():
                    return line.strip()
            return marker  # matched but somehow not on its own line -- fall back to the marker text itself
    return None


def log(msg):
    print(f"[{datetime.datetime.utcnow().isoformat()}Z] {msg}", flush=True)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
        if not content:
            return default
        return json.loads(content)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)


def _headers_for(user_agent):
    """A realistic browser-style header set. Many sites' bot-management
    rules (Cloudflare, Akamai, etc.) key off more than just User-Agent —
    a request missing Accept/Accept-Language/Sec-Fetch-* headers reads as
    a script even with a browser UA string."""
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    if "Googlebot" not in user_agent:
        headers.update({
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        })
    return headers


def _decompress(raw, resp_headers):
    """urllib does not auto-decompress — do it ourselves since we now ask
    for gzip/deflate (some sites only serve compressed bodies)."""
    encoding = (resp_headers.get("Content-Encoding") or "").lower()
    try:
        if "gzip" in encoding:
            return gzip.decompress(raw)
        if "deflate" in encoding:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:  # noqa: BLE001 — fall back to the raw bytes
        return raw
    return raw


def fetch(url, user_agent=USER_AGENT):
    """Returns (ok, content_type, raw_bytes_or_none, error_or_none)."""
    req = urllib.request.Request(url, headers=_headers_for(user_agent))
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read()
            raw = _decompress(raw, resp.headers)
            return True, content_type, raw, None
    except Exception as e:  # noqa: BLE001 — deliberately broad, this is a monitor
        return False, "", None, str(e)


_playwright_ctx = None
_playwright_browser = None


def _get_playwright_browser():
    """Lazily launches a single shared headless Chromium instance, reused
    across every fallback fetch this run so the ~1-2s browser startup cost
    is paid once, not per page."""
    global _playwright_ctx, _playwright_browser
    if not PLAYWRIGHT_AVAILABLE:
        return None
    if _playwright_browser is None:
        _playwright_ctx = sync_playwright().start()
        _playwright_browser = _playwright_ctx.chromium.launch(headless=True)
    return _playwright_browser


def close_playwright():
    """Shuts down the shared browser/driver. Safe to call even if it was
    never started (e.g. Playwright isn't installed, or no page ever needed
    the fallback)."""
    global _playwright_ctx, _playwright_browser
    if _playwright_browser is not None:
        try:
            _playwright_browser.close()
        except Exception:  # noqa: BLE001
            pass
        _playwright_browser = None
    if _playwright_ctx is not None:
        try:
            _playwright_ctx.stop()
        except Exception:  # noqa: BLE001
            pass
        _playwright_ctx = None


def fetch_with_playwright(url):
    """Last-resort fetch via real headless Chromium. This is used only for
    pages that fail every urllib attempt in fetch_with_retry — it renders
    the full page with real JavaScript execution and a genuine browser
    TLS/canvas fingerprint, which passes lighter bot-management checks
    that a plain urllib request (no matter the headers or User-Agent)
    cannot. It does NOT help against a hard IP-reputation block on the
    runner's datacenter IP range — that would need a different exit IP
    entirely. Returns (ok, content_type, raw_bytes_or_none, error_or_none),
    matching fetch()'s signature so callers can treat it interchangeably."""
    browser = _get_playwright_browser()
    if browser is None:
        return False, "", None, "playwright not available"
    page = None
    try:
        page = browser.new_page(user_agent=USER_AGENT)
        page.set_default_navigation_timeout(PLAYWRIGHT_NAV_TIMEOUT_MS)
        page.goto(url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_NAV_TIMEOUT_MS)
        try:
            # Best-effort: let late JS-rendered content settle. Many pages
            # never truly go idle (ads/trackers keep polling), so this is
            # allowed to time out without failing the fetch.
            page.wait_for_load_state("networkidle", timeout=PLAYWRIGHT_IDLE_TIMEOUT_MS)
        except Exception:  # noqa: BLE001
            pass
        try:
            # Charles, 2026-09-07: found via CIRCABC row 86 (a live, real
            # document page, not a dead link) still only capturing a cookie
            # banner + "Loading..." after networkidle resolved — Angular's
            # own render/zone.js cycle can lag a beat behind the network
            # settling, so "no more network requests" doesn't mean "DOM
            # finished painting" for every SPA. A flat extra 3s here costs
            # nothing on ordinary pages (they already have their content)
            # and gives slow-rendering apps a chance to finish painting
            # before we capture and scroll.
            page.wait_for_timeout(3000)
        except Exception:  # noqa: BLE001
            pass
        try:
            # Charles, 2026-09-07: "we need to review the entire page ...
            # otherwise we will miss important content." Some pages only
            # populate real content (infinite-scroll lists, intersection-
            # observer-triggered images/text) once the viewport actually
            # reaches them — a page that never scrolls never renders it,
            # no matter how long we wait at the top. Walk down the page in
            # steps so anything gated on scroll position gets a chance to
            # load, then return to the top before capturing content() (some
            # sites lazy-unload text above the fold once you've scrolled
            # past it, so ending at the top is the safer place to capture
            # from). Best-effort: never fails the fetch if it errors.
            prev_height = 0
            for _ in range(8):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(400)
                height = page.evaluate("document.body.scrollHeight")
                if height == prev_height:
                    break
                prev_height = height
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(200)
        except Exception:  # noqa: BLE001
            pass
        html = page.content()
        return True, "text/html; charset=utf-8", html.encode("utf-8"), None
    except Exception as e:  # noqa: BLE001 — deliberately broad, this is a monitor
        return False, "", None, f"playwright: {str(e)[:200]}"
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass


def fetch_with_flaresolverr(url):
    """Last-of-last-resort fetch via a locally-running FlareSolverr instance
    (see daily-watch.yml — it runs as a Docker service alongside this job,
    nothing to sign up for). Sends a plain 'solve this URL' request over
    FlareSolverr's own HTTP API and gets back the already-rendered page.
    Returns (ok, content_type, raw_bytes_or_none, error_or_none), matching
    fetch()'s signature. If the service isn't running or unreachable (e.g.
    running this file outside the GitHub Actions workflow), this fails soft —
    same optional-dependency philosophy as PLAYWRIGHT_AVAILABLE — so a
    missing FlareSolverr never breaks the run, the page just falls through
    to being reported as a gap same as before this existed."""
    payload = json.dumps({
        "cmd": "request.get",
        "url": url,
        "maxTimeout": FLARESOLVERR_TIMEOUT_MS,
    }).encode()
    req = urllib.request.Request(
        FLARESOLVERR_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=(FLARESOLVERR_TIMEOUT_MS / 1000) + 10) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:  # noqa: BLE001 — service not running, timed out, etc.
        return False, "", None, f"flaresolverr unreachable: {str(e)[:200]}"

    if result.get("status") != "ok":
        return False, "", None, f"flaresolverr: {str(result.get('message', 'unknown error'))[:200]}"

    solution = result.get("solution") or {}
    html = solution.get("response") or ""
    if not html:
        return False, "", None, "flaresolverr: empty response"
    return True, "text/html; charset=utf-8", html.encode("utf-8"), None


MIN_VISIBLE_TEXT_CHARS = 400  # below this, a "successful" fetch is treated
    # as suspect — probably a JS-shell page that returned 200 with almost no
    # server-rendered content, not a real gap, but not full content either.
    # Raised from 200 to 400 on 2026-09-07 after finding CIRCABC's 4 rows all
    # cleared 200 (206 chars each) while capturing only a cookie-consent
    # banner plus a literal "Loading..." placeholder — Playwright's own
    # networkidle/scroll wait wasn't enough for CIRCABC's slow Angular API
    # calls, and 200 was just under that specific stuck-state's length. 400
    # comfortably clears it while a genuinely tiny real page (several exist
    # in this watchlist, some under 60 chars) still gets returned via the
    # best-so-far fallback if nothing longer turns up on any layer — this
    # only costs extra escalation attempts, it can never lose real content.


def _visible_text_len(raw, content_type):
    """Cheap, best-effort estimate of how much real visible text a fetched
    response contains — used only to decide whether a nominally-successful
    fetch is actually worth trusting, not the real extraction (see
    extract_text_and_images, which does the full chrome-stripping compare
    later). Binary responses (PDFs etc.) are exempt by the caller — sniffing
    a PDF as if it were HTML text always looks empty and would wrongly
    trigger escalation for something that was never a text page."""
    try:
        html = raw.decode("utf-8", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "nav", "header", "footer", "form"]):
            tag.decompose()
        return len(soup.get_text(strip=True))
    except Exception:  # noqa: BLE001
        return 0


def fetch_with_retry(url):
    """Tries every layer before giving up on a page. Charles, 2026-09-07:
    "we can't have gaps (unless they are genuinely broken)." A plain HTTP
    404 used to be treated as terminal — the reasoning was "the URL itself
    is wrong, retrying won't fix that" — but that reasoning doesn't hold in
    general: some sites 404 a plain script request on a path that resolves
    fine through client-side routing or a bot-management rule, and only a
    real rendered browser can tell the difference. Verified directly
    2026-09-07: most of a sample of 404 rows were confirmed genuinely dead
    by opening them in a real browser, but that confirmation came from
    trying a real browser, not from trusting the first 404. So a 404 no
    longer short-circuits anything — it gets exactly the same escalation
    as a 403, timeout, or empty body: alternate-UA retries, then Playwright,
    then FlareSolverr. Only a page that fails *every* layer, including a
    fully rendered browser, gets reported as a gap.

    First attempt uses the normal browser identity; up to three more tries
    follow with short backoff, rotating through alternate UAs in case the
    block is keyed on the browser identity rather than the source IP. If
    every urllib attempt still fails, a headless Chromium fetch
    (fetch_with_playwright) is tried next, and if that also fails,
    FlareSolverr (fetch_with_flaresolverr) is tried as a final fallback —
    it runs a stealth-patched Chromium specifically built to solve
    Cloudflare-style JS challenges, a step up from Playwright's plain fetch
    for that one failure class. Both are free and require no signup;
    they're ordered cheapest-and-most-general-first so no attempt is wasted
    on a page a simpler method could already handle.

    A response that comes back HTTP 200 but is actually a bot-challenge
    interstitial (see is_bot_challenge) is treated exactly like any other
    failure here — it clears ok/raw so the retry loop keeps going and, if
    every attempt (including Playwright and FlareSolverr) hits the same
    wall, the page is correctly reported as a GAP with an error naming the
    block, instead of being stored as a snapshot that flips on every run.

    A response that is technically "ok" but suspiciously thin (see
    MIN_VISIBLE_TEXT_CHARS) — a JS-rendered page that a plain script
    request only ever sees as an empty shell, not a Cloudflare-style
    challenge, just genuinely no server-rendered text — does NOT stop the
    escalation either: it's kept as the best-so-far candidate, but every
    remaining layer still gets tried in case a real browser renders more.
    Only if nothing does better is the thin result finally accepted,
    on the theory that a page that's thin everywhere really is just short
    (some regulatory notices genuinely are one line) rather than treating
    every short page as a failure."""

    def _reject_challenge(ok, ct, raw, err):
        if ok and raw and len(raw) >= 20 and is_bot_challenge(raw, ct):
            return False, ct, raw, "blocked by bot-challenge interstitial (e.g. Cloudflare) — page returned 200 but body is a verification page, not real content"
        return ok, ct, raw, err

    def _is_thin(ok, ct, raw):
        if not ok or not raw or any(bt in (ct or "") for bt in BINARY_CONTENT_TYPES):
            return False
        return _visible_text_len(raw, ct) < MIN_VISIBLE_TEXT_CHARS

    best = None  # best-so-far (ok, ct, raw, err) among thin-but-technically-ok results

    def _consider(ok, ct, raw, err):
        nonlocal best
        if not (ok and raw and len(raw) >= 20):
            return None
        if not _is_thin(ok, ct, raw):
            return (ok, ct, raw, err)
        if best is None or len(raw) > len(best[2]):
            best = (ok, ct, raw, err)
        return None

    ok, ct, raw, err = _reject_challenge(*fetch(url, USER_AGENT))
    result = _consider(ok, ct, raw, err)
    if result:
        return result

    for user_agent, delay in ((USER_AGENT, 2), (UA_FIREFOX, 3), (UA_GOOGLEBOT, 3)):
        time.sleep(delay)
        ok, ct, raw, err = _reject_challenge(*fetch(url, user_agent))
        result = _consider(ok, ct, raw, err)
        if result:
            return result

    if PLAYWRIGHT_AVAILABLE:
        pw_ok, pw_ct, pw_raw, pw_err = _reject_challenge(*fetch_with_playwright(url))
        result = _consider(pw_ok, pw_ct, pw_raw, pw_err)
        if result:
            return result
        if pw_ok:
            err = None  # a thin-but-ok Playwright fetch supersedes the earlier error text
        else:
            err = f"{err} | playwright: {pw_err}" if err else pw_err

    fs_ok, fs_ct, fs_raw, fs_err = False, "", None, None
    for attempt in range(FLARESOLVERR_ATTEMPTS):
        fs_ok, fs_ct, fs_raw, fs_err = _reject_challenge(*fetch_with_flaresolverr(url))
        result = _consider(fs_ok, fs_ct, fs_raw, fs_err)
        if result:
            return result
        if attempt < FLARESOLVERR_ATTEMPTS - 1:
            time.sleep(FLARESOLVERR_RETRY_DELAY)

    if best is not None:
        return best

    combined_err = f"{err} | flaresolverr: {fs_err}" if err else fs_err
    return ok, ct, raw, combined_err


def _looks_like_content_image(img_tag, resolved_url):
    """Heuristic filter: True if this <img> looks like real page content
    worth hashing and comparing, False if it looks like chrome (a logo,
    icon, tracking pixel, decorative flourish). Deliberately conservative —
    missing a genuine content image is much cheaper than drowning every
    page in false "changed" alerts from a rotating ad banner or a
    per-request tracking pixel with a random query string."""
    path = urllib.parse.urlparse(resolved_url).path.lower()
    if path.endswith(SKIP_IMAGE_EXTENSIONS):
        return False
    class_attr = img_tag.get("class") or ""
    if not isinstance(class_attr, str):
        class_attr = " ".join(class_attr)
    haystack = " ".join(filter(None, [
        resolved_url, img_tag.get("alt", ""), class_attr, img_tag.get("id", ""),
    ])).lower()
    if SKIP_IMAGE_PATTERN.search(haystack):
        return False
    for attr in ("width", "height"):
        val = img_tag.get(attr)
        if val:
            digits = re.sub(r"\D", "", str(val))
            if digits and int(digits) < MIN_CONTENT_IMAGE_DIMENSION:
                return False
    return True


def extract_images(soup, base_url):
    """Finds up to MAX_IMAGES_PER_PAGE candidate content images in an
    already-parsed, already-chrome-stripped soup — nav/header/footer/script
    tags are already gone by the time this runs (extract_text_and_images
    decomposes them first), so logos and icons living in those regions are
    excluded for free, before the heuristic filter below even runs.
    Returns resolved absolute URLs in page order."""
    found = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if not src or src.startswith("data:"):
            continue
        resolved = urllib.parse.urljoin(base_url, src)
        if not resolved.startswith(("http://", "https://")):
            continue
        if _looks_like_content_image(img, resolved):
            found.append(resolved)
        if len(found) >= MAX_IMAGES_PER_PAGE:
            break
    return found


def fetch_image_hash(url):
    """Fetches one candidate content image and returns its SHA-256 hash, or
    None if it can't be fetched or doesn't actually look like an image
    (some sites respond to an image request with an HTML error/challenge
    page — that must never get hashed as if it were the image). Failures
    here are silent by design: this is a best-effort enrichment on top of
    the text diff, not a hard requirement, so one unreachable image never
    turns a page's own fetch into a gap."""
    req = urllib.request.Request(url, headers=_headers_for(USER_AGENT))
    try:
        with urllib.request.urlopen(req, timeout=IMAGE_FETCH_TIMEOUT) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if not content_type.startswith("image/"):
                return None
            raw = resp.read()
            if not raw:
                return None
            return hashlib.sha256(raw).hexdigest()
    except Exception:  # noqa: BLE001 — best-effort only, see docstring
        return None


def describe_image_change(old_hashes, new_hashes):
    """Turns an old {url: hash} vs. new {url: hash} comparison into a short
    human-readable note, the same role classify_change()'s note plays for
    text — e.g. "1 image(s) changed; 1 image(s) added"."""
    added = [u for u in new_hashes if u not in old_hashes]
    removed = [u for u in old_hashes if u not in new_hashes]
    changed = [u for u in new_hashes if u in old_hashes and old_hashes[u] != new_hashes[u]]
    parts = []
    if changed:
        parts.append(f"{len(changed)} image(s) changed")
    if added:
        parts.append(f"{len(added)} image(s) added")
    if removed:
        parts.append(f"{len(removed)} image(s) removed")
    return "; ".join(parts) if parts else "image content changed"


def assess_image_change(prev_hashes, new_hashes, had_baseline, text_changed):
    """Decides whether an image delta deserves an alert, and describes it.

    Supersedes the url-keyed test this replaced. describe_image_change() above
    compares {url: hash} maps, so ANY url churn read as a change even when the
    bytes were identical - a cache-busted filename, a versioned CDN path, a
    lazy-loader swapping data-src for src, or a slider picking different
    images per request. That is what produced run #12's rows 21 and 22
    ("5 image(s) removed", no additions and no text change - the extractor
    finding no qualifying images that run, not five images being deleted),
    row 20 on a page whose content is from 2018, and the Living Future pair.

    Two rules:
      1. Compare the multiset of CONTENT hashes, not the url->hash map, so the
         same bytes served from a new url is a non-event.
      2. Where the text did not change, require genuinely new image content
         before alerting. Images merely disappearing, with nothing new and no
         text change, is extraction instability - logged, never alerted.

    Image detection is deliberately kept, not removed: a real regulatory
    change is sometimes published as a graphic (a fee table, a scanned
    notice). The fix for the noise was content-keying, not dropping the
    capability.

    Returns (alertworthy, note). Covered by tests/test_images.py.
    """
    if not had_baseline:
        return False, None
    prev_set, new_set = set(prev_hashes.values()), set(new_hashes.values())
    if prev_set == new_set:
        return False, None
    gained = new_set - prev_set
    lost = prev_set - new_set
    if not text_changed and not gained:
        return False, ("images lost with no new image content and no text "
                       "change - treated as extraction instability")
    parts = []
    if gained:
        parts.append(f"{len(gained)} new image(s) by content")
    if lost:
        parts.append(f"{len(lost)} image(s) no longer present")
    return True, "; ".join(parts)


def extract_text_and_images(html_bytes, content_type, base_url):
    """Returns (text, image_urls) from a single HTML parse. text is the
    full extracted, chrome-stripped visible text — no length cap (see the
    comment by the removed MAX_TEXT_CHARS above): every character gets
    compared, however long the page is. image_urls is up to
    MAX_IMAGES_PER_PAGE candidate content images (see extract_images) for
    the caller to hash and compare separately, since a genuine regulatory
    change is sometimes published as a graphic (a fee table, a scanned
    notice) rather than as text."""
    charset = "utf-8"
    m = re.search(r"charset=([\w-]+)", content_type or "", re.IGNORECASE)
    if m:
        charset = m.group(1)
    try:
        html = html_bytes.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        html = html_bytes.decode("utf-8", errors="replace")

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "header", "footer", "form"]):
        tag.decompose()

    images = extract_images(soup, base_url)

    raw_lines = soup.get_text("\n").splitlines()
    kept = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        if CONSENT_RE.search(line):
            continue
        if CHROME_RE.search(line) and not KEEP_HINTS_RE.search(line):
            continue
        kept.append(line)

    text = "\n".join(kept)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(), images


def normalize_for_compare(text):
    return re.sub(r"\s+", " ", text.lower()).strip()


def classify_change(old_text, new_text):
    """Returns (is_changed, note). Applies cosmetic-noise filtering."""
    old_n, new_n = normalize_for_compare(old_text), normalize_for_compare(new_text)
    if old_n == new_n:
        return False, None

    ratio = difflib.SequenceMatcher(None, old_n, new_n).ratio()
    if ratio >= 0.995:
        return False, "near-identical (>99.5% match) — treated as cosmetic"

    diff_lines = list(
        difflib.unified_diff(old_text.splitlines(), new_text.splitlines(), lineterm="", n=0)
    )
    changed_lines = [l for l in diff_lines if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
    changed_lines = [l[1:].strip() for l in changed_lines if l[1:].strip()]

    if not changed_lines:
        return False, "whitespace-only difference"

    note = " | ".join(changed_lines[:3])[:400]
    return True, note


def load_watchlist():
    data = load_json(WATCHLIST_PATH, {"entries": []})
    return data.get("entries", [])


def create_github_issue(date_str, changes, gaps):
    """Opens one issue summarizing this run's changes. GitHub emails the repo
    owner automatically whenever an issue is opened — that email IS the
    notification. Returns (success, issue_number_or_None, error_or_None)."""
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        log("No GITHUB_TOKEN/GITHUB_REPOSITORY set — skipping issue creation "
            f"(would have reported {len(changes)} change(s)).")
        return False, None, "no token/repo"

    lines = [f"**{len(changes)} page(s) changed** on {date_str}.\n"]
    for c in changes:
        lines.append(f"- [{c['description'] or c['url']}]({c['url']})\n  {c['note']}")
    if gaps:
        lines.append(f"\n_{len(gaps)} page(s) could not be fetched this run — see runs.json for details._")
    body = "\n".join(lines)[:60000]  # GitHub issue body size guard

    payload = json.dumps({
        "title": f"Regulatory changes detected — {date_str} ({len(changes)} page{'s' if len(changes) != 1 else ''})",
        "body": body,
        "labels": ["regulatory-change"],
    }).encode()
    req = urllib.request.Request(
        f"{GITHUB_API_URL}/repos/{GITHUB_REPOSITORY}/issues",
        data=payload,
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            result = json.loads(resp.read().decode())
            return True, result.get("number"), None
    except urllib.error.HTTPError as e:
        return False, None, f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}"
    except Exception as e:  # noqa: BLE001
        return False, None, str(e)[:300]


def escape_md(text):
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


UK_TZ = ZoneInfo("Europe/London")


def format_utc(iso_str):
    """Formats an ISO-8601 '...Z' timestamp (e.g. from started_at/finished_at,
    always stored/recorded in UTC) as UK local time for display —
    'YYYY-MM-DD HH:MM:SS GMT' or '... BST' depending on the time of year,
    since Europe/London observes British Summer Time. Drops sub-second
    precision, which is noise for a human reading the dashboard. Falls back
    to plain UTC text if the system has no tzdata (shouldn't happen on the
    GitHub Actions runner, but this keeps the dashboard readable either way)."""
    if not iso_str:
        return "—"
    s = iso_str.rstrip("Z")
    if "." in s:
        s = s.split(".", 1)[0]
    try:
        dt_utc = datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=datetime.timezone.utc
        )
        dt_uk = dt_utc.astimezone(UK_TZ)
        tz_label = dt_uk.tzname() or "UK"
        return dt_uk.strftime("%Y-%m-%d %H:%M:%S") + f" {tz_label}"
    except Exception:  # noqa: BLE001 — no tzdata or unexpected format
        return s.replace("T", " ") + " UTC"


def format_dashboard(entries, snapshots, runs, run_record):
    """Builds DASHBOARD.md — a GitHub-rendered Markdown page that always shows
    the latest state. Viewed at the repo's normal blob URL, gated by GitHub's
    own login, so a private repo stays private with no extra hosting needed."""
    l = []
    l.append("# Cloud Regulatory Watch — Dashboard")
    l.append("")
    l.append(f"_Last updated: {format_utc(run_record['finished_at'])} (run date {run_record['date']})_")
    l.append("")
    l.append("Auto-generated by `monitor.py` on every run. Do not edit by hand — it gets overwritten.")
    l.append("")

    l.append("## Latest run")
    l.append("")
    l.append("| Metric | Value |")
    l.append("|---|---|")
    l.append(f"| Completed (UK time) | {format_utc(run_record['finished_at'])} |")
    l.append(f"| Started (UK time) | {format_utc(run_record.get('started_at'))} |")
    l.append(f"| Pages checked | {run_record['checked']} |")
    l.append(f"| New (baseline) | {run_record['new_baseline']} |")
    l.append(f"| Unchanged | {run_record['unchanged']} |")
    l.append(f"| Changed | {run_record['changed']} |")
    l.append(f"| Coverage gaps | {run_record['gaps']} |")
    l.append(f"| Dead / expired links | {run_record.get('dead', 0)} |")
    issue_txt = f"#{run_record['issue_number']}" if run_record.get("issue_number") else "none opened"
    l.append(f"| GitHub issue | {issue_txt} |")
    l.append("")

    if run_record["changes"]:
        l.append(f"## Changes detected this run ({len(run_record['changes'])})")
        l.append("")
        for c in run_record["changes"]:
            l.append(f"- **row {c['row']}** [{escape_md(c['description']) or c['url']}]({c['url']})")
            l.append(f"  {escape_md(c['note'])}")
        l.append("")

    if run_record["gaps_list"]:
        l.append(f"## Coverage gaps this run ({len(run_record['gaps_list'])})")
        l.append("")
        l.append("<details><summary>Show gap list</summary>")
        l.append("")
        l.append("| Row | Page | Error |")
        l.append("|---|---|---|")
        for g in run_record["gaps_list"]:
            l.append(f"| {g['row']} | [{escape_md(g['description']) or g['url']}]({g['url']}) | {escape_md(g['error'])} |")
        l.append("")
        l.append("</details>")
        l.append("")

    if run_record.get("dead_links"):
        # Charles, 2026-09-07: "add a section for links that are expired
        # or no longer working." Distinct from Coverage gaps above -- a gap
        # means the fetch itself failed (blocked, timed out, network
        # error); a dead link means the fetch succeeded and the source
        # itself says the resource is gone (a soft-404, confirmed by
        # opening the page directly). These need a different fix: a gap
        # might resolve itself next run, a dead link needs the URL
        # updated or removed from watchlist.json.
        l.append(f"## Dead / expired links this run ({len(run_record['dead_links'])})")
        l.append("")
        l.append("<details><summary>Show dead-link list</summary>")
        l.append("")
        l.append("| Row | Page | What the source page says |")
        l.append("|---|---|---|")
        for d in run_record["dead_links"]:
            l.append(f"| {d['row']} | [{escape_md(d['description']) or d['url']}]({d['url']}) | {escape_md(d['note'])} |")
        l.append("")
        l.append("</details>")
        l.append("")

    l.append("## Run history (most recent first)")
    l.append("")
    l.append("| Date | Completed (UK time) | Checked | New | Unchanged | Changed | Gaps | Dead | Issue |")
    l.append("|---|---|---|---|---|---|---|---|---|")
    for r in list(reversed(runs))[:30]:
        r_issue = f"#{r['issue_number']}" if r.get("issue_number") else "—"
        finished = format_utc(r.get("finished_at"))
        l.append(f"| {r['date']} | {finished} | {r['checked']} | {r['new_baseline']} | {r['unchanged']} | "
                  f"{r['changed']} | {r['gaps']} | {r.get('dead', 0)} | {r_issue} |")
    l.append("")

    l.append(f"## Current status — all {len(entries)} watched pages")
    l.append("")
    l.append("<details><summary>Show full list</summary>")
    l.append("")
    l.append("| Row | Description | Status | Last changed |")
    l.append("|---|---|---|---|")
    for entry in sorted(entries, key=lambda e: e["row"]):
        snap = snapshots.get(entry["slug"], {})
        snap_status = snap.get("status")
        status = "GAP" if snap_status == "gap" else ("DEAD" if snap_status == "dead" else "OK")
        last_changed = snap.get("last_changed") or "—"
        desc = escape_md(entry.get("description")) or entry["url"]
        l.append(f"| {entry['row']} | [{desc}]({entry['url']}) | {status} | {last_changed} |")
    l.append("")
    l.append("</details>")
    l.append("")

    return "\n".join(l)


def main():
    started_at = datetime.datetime.utcnow().isoformat() + "Z"
    entries = load_watchlist()
    snapshots = load_json(SNAPSHOTS_PATH, {})
    runs = load_json(RUNS_PATH, [])

    counts = {"checked": 0, "new": 0, "unchanged": 0, "changed": 0, "gap": 0, "dead": 0}
    changes = []
    gaps = []
    dead_links = []

    log(f"Loaded {len(entries)} watchlist entries, {len(snapshots)} existing snapshots.")

    for entry in entries:
        slug = entry["slug"]
        url = entry["url"]
        counts["checked"] += 1

        ok, content_type, raw, err = fetch_with_retry(url)
        prev = snapshots.get(slug)

        if not ok or not raw or len(raw) < 20:
            counts["gap"] += 1
            gaps.append({**entry, "error": err or "empty response"})
            snapshots[slug] = {
                **entry,
                "mode": (prev or {}).get("mode", "text"),
                "text": (prev or {}).get("text"),
                "hash": (prev or {}).get("hash"),
                "image_hashes": (prev or {}).get("image_hashes"),
                "status": "gap",
                "last_checked": started_at,
                "last_changed": (prev or {}).get("last_changed"),
                "last_change_note": (prev or {}).get("last_change_note"),
            }
            log(f"GAP  row {entry['row']:>3}  {url}  ({err or 'empty'})")
            continue

        is_binary = any(bt in (content_type or "") for bt in BINARY_CONTENT_TYPES)

        if is_binary:
            new_hash = hashlib.sha256(raw).hexdigest()
            if prev is None:
                status, note = "new", None
            elif prev.get("hash") == new_hash:
                status, note = "unchanged", None
            else:
                status, note = "changed", "binary/PDF content hash changed since last check"

            snapshots[slug] = {
                **entry, "mode": "binary", "hash": new_hash, "text": None,
                "status": "ok",
                "last_checked": started_at,
                "last_changed": started_at if status == "changed" else (prev or {}).get("last_changed"),
                "last_change_note": note if status == "changed" else (prev or {}).get("last_change_note"),
            }
        else:
            new_text, new_image_urls = extract_text_and_images(raw, content_type, url)

            if text_is_bot_challenge(new_text):
                # The raw-bytes check (is_bot_challenge, inside
                # fetch_with_retry) already missed this once for this exact
                # response — every fetch layer's raw HTML is only sniffed up
                # to a byte limit, and FlareSolverr in particular can return
                # a challenge page whose markup pushes "Just a moment..."
                # further down than that window covers (rows 52/57/76/131,
                # 2026-09-07). This is the safety net: it checks the final,
                # already-extracted visible text instead, which is small
                # and mostly IS the challenge message when a challenge slips
                # through, so it can't miss on size. Treat exactly like a
                # fetch failure — never let a solved-looking-but-not-really
                # challenge page overwrite a real snapshot.
                counts["gap"] += 1
                gaps.append({**entry, "error": "blocked by bot-challenge interstitial (caught post-extraction) — page returned 200 but body is a verification page, not real content"})
                snapshots[slug] = {
                    **entry,
                    "mode": (prev or {}).get("mode", "text"),
                    "text": (prev or {}).get("text"),
                    "hash": (prev or {}).get("hash"),
                    "image_hashes": (prev or {}).get("image_hashes"),
                    "status": "gap",
                    "last_checked": started_at,
                    "last_changed": (prev or {}).get("last_changed"),
                    "last_change_note": (prev or {}).get("last_change_note"),
                }
                log(f"GAP  row {entry['row']:>3}  {url}  (bot-challenge caught post-extraction)")
                continue

            dead_reason = is_dead_link(new_text)
            is_dead = dead_reason is not None
            had_dead_before = (prev or {}).get("status") == "dead"

            # Hash each candidate content image found on the page (see
            # extract_images/fetch_image_hash above). A page whose text is
            # byte-identical to last time can still have genuinely changed
            # if a fee table or notice published as a graphic was swapped
            # out — this is how that gets caught.
            new_image_hashes = {}
            for img_url in new_image_urls:
                h = fetch_image_hash(img_url)
                if h:
                    new_image_hashes[img_url] = h
            prev_image_hashes = (prev or {}).get("image_hashes") or {}
            # Distinguish "never tracked images for this page before" from
            # "tracked them and they're the same" — an existing page whose
            # snapshot predates this feature has no "image_hashes" key at
            # all, and establishing that first baseline must not itself
            # count as a change (same principle as prev is None for text).
            # Without this, the rollout run would flag every page with at
            # least one qualifying image as "changed" purely from having
            # nothing to compare against yet.
            had_image_baseline = prev is not None and "image_hashes" in prev

            if prev is None or prev.get("text") is None:
                status, note = "new", None
            else:
                text_changed, text_note = classify_change(prev.get("text", ""), new_text)
                if is_dead and had_dead_before:
                    # Both before and after are "this resource is gone"
                    # states -- some dead-page templates embed a dynamic
                    # element (a request id, a random "related pages"
                    # list) that would otherwise diff as a false "changed"
                    # every run. A page that's dead on both sides has
                    # nothing new to report; only a genuine live<->dead
                    # transition (handled below via prev.get("status"))
                    # should ever surface as a real change.
                    text_changed = False
                images_changed, image_note = assess_image_change(
                    prev_image_hashes, new_image_hashes, had_image_baseline, text_changed
                )
                if text_changed or images_changed:
                    status = "changed"
                    note_parts = []
                    if text_changed and text_note:
                        note_parts.append(text_note)
                    if images_changed:
                        note_parts.append(image_note)
                    note = " | ".join(note_parts) if note_parts else "image content changed"
                else:
                    status, note = "unchanged", None

            snapshots[slug] = {
                **entry, "mode": "text", "text": new_text, "hash": None,
                "image_hashes": new_image_hashes,
                "status": "dead" if is_dead else "ok",
                "last_checked": started_at,
                "last_changed": started_at if status == "changed" else (prev or {}).get("last_changed"),
                "last_change_note": note if status == "changed" else (prev or {}).get("last_change_note"),
            }

            if is_dead:
                # Charles, 2026-09-07: "add a section for links that are
                # expired or no longer working." Reported every run the
                # page still reads as dead (not just the run it first
                # flips) so the dashboard's dead-link list always reflects
                # current reality, the same way gaps_list already does.
                counts["dead"] += 1
                dead_links.append({**entry, "note": dead_reason[:200]})
                log(f"DEAD row {entry['row']:>3}  {url}  ({dead_reason[:100]})")

        counts[status] += 1
        if status == "changed":
            changes.append({**entry, "note": note})
            log(f"CHANGED row {entry['row']:>3}  {url}  -- {note}")
        elif status == "new":
            log(f"NEW   row {entry['row']:>3}  {url}  (baseline)")
        else:
            log(f"OK    row {entry['row']:>3}  {url}")

    log(f"Fetch pass done. Counts: {counts}")
    close_playwright()

    issue_number, issue_error = None, None
    if changes:
        success, issue_number, issue_error = create_github_issue(started_at[:10], changes, gaps)
        if success:
            log(f"Opened GitHub issue #{issue_number} for {len(changes)} change(s).")
        else:
            log(f"FAILED to open GitHub issue: {issue_error}")

    finished_at = datetime.datetime.utcnow().isoformat() + "Z"
    run_record = {
        "date": started_at[:10],
        "started_at": started_at,
        "finished_at": finished_at,
        "checked": counts["checked"],
        "new_baseline": counts["new"],
        "unchanged": counts["unchanged"],
        "changed": counts["changed"],
        "gaps": counts["gap"],
        "dead": counts["dead"],
        "issue_number": issue_number,
        "issue_error": issue_error,
        "changes": [{"row": c["row"], "vp_id": c["vp_id"], "url": c["url"],
                     "description": c["description"], "note": c["note"]} for c in changes],
        "gaps_list": [{"row": g["row"], "vp_id": g["vp_id"], "url": g["url"],
                       "description": g["description"], "error": g["error"]} for g in gaps],
        "dead_links": [{"row": d["row"], "vp_id": d["vp_id"], "url": d["url"],
                        "description": d["description"], "note": d["note"]} for d in dead_links],
    }
    runs.append(run_record)
    runs = runs[-90:]  # keep the most recent ~90 days, no unbounded growth

    save_json(SNAPSHOTS_PATH, snapshots)
    save_json(RUNS_PATH, runs)

    dashboard_md = format_dashboard(entries, snapshots, runs, run_record)
    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(dashboard_md)

    log(
        f"DONE. checked={counts['checked']} new={counts['new']} "
        f"unchanged={counts['unchanged']} changed={counts['changed']} "
        f"gaps={counts['gap']} dead={counts['dead']} issue={issue_number}"
    )
    if changes and issue_error:
        log(f"{len(changes)} real change(s) detected but the GitHub issue FAILED to open — see runs.json.")


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
Cloud Regulatory Watch — standalone daily monitor.

Runs entirely on its own (GitHub Actions, cron, or any machine with Python) —
no Claude, no Cowork, no desktop app required at runtime. It:

  1. Loads watchlist.json (the list of pages to watch).
  2. Loads snapshots.json (what each page looked like last time).
  3. Fetches every URL, extracts the substantive text (or hashes it, for
     binaries like PDFs), and filters out obvious site chrome.
  4. Classifies each page as new / unchanged / changed / gap.
  5. If any real (non-cosmetic) changes were found, opens ONE GitHub Issue
     summarizing them all — GitHub emails the repo owner automatically when
     an issue is opened, so this needs no external notification service.
  6. Writes snapshots.json, runs.json and DASHBOARD.md back out so the next
     run — and a GitHub Actions commit step — can pick up where this one left
     off. DASHBOARD.md is a Markdown dashboard GitHub renders natively at its
     normal blob URL, gated by GitHub's own login, so a private repo stays
     private with no extra hosting.

Environment variables (both provided automatically by GitHub Actions —
nothing to configure):
  GITHUB_TOKEN        auto-injected token, used to open the issue
  GITHUB_REPOSITORY   "owner/repo", used to target the right repo's API
"""
import os
import re
import sys
import json
import time
import gzip
import zlib
import hashlib
import difflib
import datetime
import urllib.request
import urllib.error
import urllib.parse
from zoneinfo import ZoneInfo

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependency: pip install -r requirements.txt", file=sys.stderr)
    raise

# Playwright (headless Chromium) is an OPTIONAL last-resort fallback for
# pages that block plain urllib requests (IP-reputation/bot-management
# blocks that no header or User-Agent tweak can get past). It is not a
# hard dependency: if it isn't installed, the monitor still runs fine on
# urllib alone, it just won't have this extra fallback layer.
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_PATH = os.path.join(BASE_DIR, "watchlist.json")
SNAPSHOTS_PATH = os.path.join(BASE_DIR, "snapshots.json")
RUNS_PATH = os.path.join(BASE_DIR, "runs.json")
DASHBOARD_PATH = os.path.join(BASE_DIR, "DASHBOARD.md")

GITHUB_API_URL = "https://api.github.com"
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 CloudRegulatoryWatch/1.0"
)
# FlareSolverr (see daily-watch.yml) is a free, self-hosted, open-source
# service — no account or API key needed — run as a Docker container
# alongside this job. It drives a stealth-patched Chromium purpose-built to
# solve Cloudflare-style JS challenges ("just a moment", "one moment,
# please", etc.), which is a step up from a generic Playwright fetch for
# exactly that failure class. It is tried as the LAST resort, after both a
# plain urllib fetch and a generic Playwright fetch have failed, so it never
# spends time on pages the free methods already handle. It cannot get past a
# hard IP-reputation block — this job's outbound IP is still GitHub's — only
# a software challenge that a convincing-enough browser can clear.
FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://localhost:8191/v1")
FLARESOLVERR_TIMEOUT_MS = 60000
# Alternate identities used only as fallback retries when the primary
# request is blocked (403) or times out. Some sites' bot-management rules
# explicitly allowlist known search-engine crawlers even while blocking
# generic scripts, so a Googlebot-style UA occasionally gets through where
# a plain browser UA from a datacenter IP does not.
UA_FIREFOX = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0"
UA_GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
REQUEST_TIMEOUT = 30
PLAYWRIGHT_NAV_TIMEOUT_MS = 35000
PLAYWRIGHT_IDLE_TIMEOUT_MS = 12000
# How many times to retry FlareSolverr itself before giving up on a page.
# Solving a Cloudflare-style challenge is inherently non-deterministic — the
# same URL that gets a clean solve on one run can come back "flaresolverr:
# HTTP Error 500: Internal Server Error" on the next (observed 2026-09-07 for
# rows 52/57/76/131, the exact rows a previous run had fetched cleanly). A
# single extra attempt costs little and recovers most of these.
FLARESOLVERR_ATTEMPTS = 2
FLARESOLVERR_RETRY_DELAY = 5
# No text-length cap. Charles, 2026-09-07: "We need to analyse the entire
# contents of the page. Otherwise this is pointless." Every character of
# extracted, chrome-stripped text is compared, however long the page is —
# at the cost of a larger snapshots.json and (for the rare huge page) a
# slower classify_change() diff. That tradeoff is intentional: a change
# past whatever cutoff we'd otherwise pick is exactly the kind of change
# this tool exists to catch.

# --- Image comparison ----------------------------------------------------
# Some regulatory pages publish substantive content as an image (a fee
# table rendered as a graphic, a scanned notice, a chart) that a text-only
# diff can never see. This does a best-effort second channel: find images
# that look like real page content (not logos, icons, nav chrome, or
# tracking pixels), fetch each one, and hash it. A changed hash — or an
# image added/removed — is treated as a real page change exactly like a
# text change, even when the surrounding text is byte-identical.
MAX_IMAGES_PER_PAGE = 6  # cap per page — keeps run time and byte-hashing
    # bounded even on an image-heavy page; content images are rare enough
    # per page that this ceiling should never bind in practice
IMAGE_FETCH_TIMEOUT = 10
MIN_CONTENT_IMAGE_DIMENSION = 100  # px; an <img> with an explicit width or
    # height below this is almost always a logo/icon/badge, not content
SKIP_IMAGE_EXTENSIONS = (".svg", ".ico", ".gif")
    # .svg/.ico are logos and favicons; .gif on these sites is essentially
    # always a spinner, tracking pixel, or decorative flourish — never
    # observed to be the actual regulatory content
SKIP_IMAGE_PATTERN = re.compile(
    r"logo|icon|sprite|badge|avatar|spinner|loading|placeholder|"
    r"pixel|1x1|spacer|banner-ad|flag-|arrow|chevron|social|share",
    re.IGNORECASE,
)

# Lines matched by any of these (case-insensitive) are treated as site chrome,
# not substance, and dropped before comparing snapshots.
CHROME_PATTERNS = [
    r"^\s*(home|menu|search|login|log in|sign in|sign up|subscribe|newsletter)\s*$",
    r"^\s*(contact us|about us|careers|sitemap|follow us)\s*$",
    r"cookie (policy|settings|consent|banner)",
    r"privacy policy|terms of (use|service)|all rights reserved",
    r"^\s*©?\s*\d{4}\s",  # bare copyright lines
    r"facebook\.com|twitter\.com|x\.com/|linkedin\.com|instagram\.com|youtube\.com|tiktok\.com",
    r"^\s*(skip to (main )?content)\s*$",
    r"^\s*(select (a )?language|choose (your )?country)\s*$",
]
CHROME_RE = re.compile("|".join(CHROME_PATTERNS), re.IGNORECASE)

# If a line contains any of these, it is almost always substance — never
# drop it even if it superficially resembles chrome.
KEEP_HINTS_RE = re.compile(
    r"[€$£]|%|\bfee\b|\btariff\b|\bthreshold\b|\bdeadline\b|\bversion\b|"
    r"\bamend|\beffective\b|\bkg\b|\bton(ne)?s?\b|\d{4}-\d{2}-\d{2}|"
    r"\b(19|20)\d{2}\b",
    re.IGNORECASE,
)

BINARY_CONTENT_TYPES = ("application/pdf", "application/octet-stream", "application/zip")

# Cloudflare/Akamai/Incapsula-style bot-challenge interstitials return a
# normal HTTP 200 with real HTML, so they pass every check in fetch() and
# fetch_with_retry() as a "successful" response. But the body is just a
# challenge page containing a unique per-request token (Cloudflare calls it
# a "Ray ID") that is different on every single load. If that token gets
# scraped as the page's "text", classify_change() sees a different value
# every run and reports a fake "changed" page forever — this is exactly
# what happened to rows 52/57/76/131 (confirmed by opening each URL
# directly: the runner's IP gets challenged even though a normal browser
# session does not). Any response matching one of these markers must be
# treated as a failed fetch, never stored as a snapshot.
BOT_CHALLENGE_MARKERS = (
    "just a moment",
    "performing security verification",
    "checking your browser before accessing",
    "enable javascript and cookies to continue",
    "attention required! | cloudflare",
    "ddos protection by cloudflare",
    "cf-browser-verification",
    "cf_chl_",
    "__cf_chl_rt_tk",
    "sorry, you have been blocked",
    "request unsuccessful. incapsula",
    "distil_r_captcha",
    "please verify you are a human",
    "pardon our interruption",
    "captcha-delivery.com",
    # New template found 2026-09-06: 6 pages (LBMA, ReSimple, Hellenic
    # Copyright Organization, Paristokierrätys x2, RLG WEEE Romania,
    # El-Kretsen) all flipped to this exact challenge wording and back to
    # real content the next day, firing false "changed" alerts each time.
    "one moment, please",
    "please wait while your request is being verified",
    # Found 2026-09-07 for rows 9/125 (Alberta Recycling, both pages of the
    # same site): a different WAF vendor's interstitial that was being
    # stored as if it were real 80-character page content instead of being
    # caught and escalated -- not in any of the markers above, so it
    # slipped through silently rather than triggering a retry.
    "robot challenge screen",
    "checking the site connection security",
)


def is_bot_challenge(html_bytes, content_type):
    """True if raw looks like a bot-challenge interstitial rather than the
    real page. Sniffs a generous prefix of the raw response (the challenge
    markup is always near the top, but FlareSolverr's rendered-Chrome output
    for a Turnstile-style challenge often carries several KB of injected
    analytics/JS before the visible "Just a moment..." text — a 4000-byte
    window missed that variant in production on 2026-09-07 for rows
    52/57/76/131, letting Cloudflare's challenge page through as if it were
    real content. 20000 bytes comfortably covers that case while still being
    cheap for the rare huge page) and only for text responses — binaries
    (PDFs, etc.) never hit this path."""
    if not html_bytes or any(bt in (content_type or "") for bt in BINARY_CONTENT_TYPES):
        return False
    try:
        sample = html_bytes[:20000].decode("utf-8", errors="ignore").lower()
    except Exception:  # noqa: BLE001
        return False
    return any(marker in sample for marker in BOT_CHALLENGE_MARKERS)


def text_is_bot_challenge(text):
    """Second, size-independent safety net: checks the same markers against
    the already-extracted, script/style-stripped visible text rather than
    raw HTML bytes. Catches a challenge page that slipped past
    is_bot_challenge()'s byte-window (e.g. a fetch layer whose raw response
    structure pushes the challenge markup further down than expected)
    before it can ever be compared against the previous snapshot or stored
    as if it were real content. Only samples the first 4000 characters —
    independent of whether the full extracted text is capped — since a
    genuine challenge page's banner is always right at the top; there is
    no need to scan a whole (possibly very long) page looking for it."""
    if not text:
        return False
    sample = text[:4000].lower()
    return any(marker in sample for marker in BOT_CHALLENGE_MARKERS)


# Charles, 2026-09-07: "add a section for links that are expired or no
# longer working." A different failure class from BOT_CHALLENGE_MARKERS
# above: these pages fetch completely successfully -- normal HTTP 200, no
# security interstitial, every fetch layer agrees -- but the content itself
# says the specific resource is gone. Confirmed 2026-09-07 for 2 CIRCABC
# documents (appearing as 3 watchlist rows -- 65/161/187, with 65 and 187
# being a literal duplicate URL): circabc.europa.eu/ui/no-content says "The
# file or folder does not exists. It is maybe deleted or removed." This is
# a soft-404 -- the kind of client-side-rendered SPA response that never
# raises a real HTTP 404 status for urllib to catch, so it looks fetchable
# forever. No fetch layer can ever recover it: the document really is gone
# from the source, not a bot-management or rendering-timing problem. This
# is the "genuinely broken" case, distinct from a "gap" (couldn't fetch
# anything at all) -- tracked as its own status so it gets a dedicated,
# honest list instead of silently sitting as an unremarkable "OK" row just
# because the fetch technically succeeded.
DEAD_LINK_MARKERS = (
    "the file or folder does not exists",
    "it is maybe deleted or removed",
    "woops! nothing found here",
    "the link you followed seems valid, but the content is missing",
    "the page you are looking for does not exist",
    "the page you requested was not found",
    "the page you were looking for doesn't exist",
    "we can't find the page you're looking for",
    "we couldn't find the page you were looking for",
    "sorry, this page isn't available",
    "sorry, we couldn't find that page",
    "oops! that page can't be found",
    "the requested url was not found on this server",
    "404 - page not found",
    "404 not found",
    "http error 404",
)


def is_dead_link(text):
    """Returns the matched line of already-extracted visible text if it
    reads like the target resource itself is gone (see DEAD_LINK_MARKERS
    above), or None otherwise -- not a bot-challenge
    (text_is_bot_challenge) and not a fetch failure (gaps). Only samples
    the first 3000 characters: every known not-found template states it
    right at the top, so this can't miss on a long, real page that happens
    to mention "404" somewhere deep in unrelated content. Deliberately
    conservative phrase list (full compound sentences, not bare words like
    "404") to keep false positives on genuine content rare; if a real page
    is ever misflagged, the fix is to remove or tighten the specific
    marker that matched, not to abandon the check. Returning the matched
    line (not just True/False) lets the caller show Charles exactly what
    the source page says, instead of a generic "this looks dead" note."""
    if not text:
        return None
    sample = text[:3000].lower()
    for marker in DEAD_LINK_MARKERS:
        if marker in sample:
            for line in text[:3000].splitlines():
                if marker in line.lower():
                    return line.strip()
            return marker  # matched but somehow not on its own line -- fall back to the marker text itself
    return None


def log(msg):
    print(f"[{datetime.datetime.utcnow().isoformat()}Z] {msg}", flush=True)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
        if not content:
            return default
        return json.loads(content)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)


def _headers_for(user_agent):
    """A realistic browser-style header set. Many sites' bot-management
    rules (Cloudflare, Akamai, etc.) key off more than just User-Agent —
    a request missing Accept/Accept-Language/Sec-Fetch-* headers reads as
    a script even with a browser UA string."""
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    if "Googlebot" not in user_agent:
        headers.update({
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        })
    return headers


def _decompress(raw, resp_headers):
    """urllib does not auto-decompress — do it ourselves since we now ask
    for gzip/deflate (some sites only serve compressed bodies)."""
    encoding = (resp_headers.get("Content-Encoding") or "").lower()
    try:
        if "gzip" in encoding:
            return gzip.decompress(raw)
        if "deflate" in encoding:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:  # noqa: BLE001 — fall back to the raw bytes
        return raw
    return raw


def fetch(url, user_agent=USER_AGENT):
    """Returns (ok, content_type, raw_bytes_or_none, error_or_none)."""
    req = urllib.request.Request(url, headers=_headers_for(user_agent))
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read()
            raw = _decompress(raw, resp.headers)
            return True, content_type, raw, None
    except Exception as e:  # noqa: BLE001 — deliberately broad, this is a monitor
        return False, "", None, str(e)


_playwright_ctx = None
_playwright_browser = None


def _get_playwright_browser():
    """Lazily launches a single shared headless Chromium instance, reused
    across every fallback fetch this run so the ~1-2s browser startup cost
    is paid once, not per page."""
    global _playwright_ctx, _playwright_browser
    if not PLAYWRIGHT_AVAILABLE:
        return None
    if _playwright_browser is None:
        _playwright_ctx = sync_playwright().start()
        _playwright_browser = _playwright_ctx.chromium.launch(headless=True)
    return _playwright_browser


def close_playwright():
    """Shuts down the shared browser/driver. Safe to call even if it was
    never started (e.g. Playwright isn't installed, or no page ever needed
    the fallback)."""
    global _playwright_ctx, _playwright_browser
    if _playwright_browser is not None:
        try:
            _playwright_browser.close()
        except Exception:  # noqa: BLE001
            pass
        _playwright_browser = None
    if _playwright_ctx is not None:
        try:
            _playwright_ctx.stop()
        except Exception:  # noqa: BLE001
            pass
        _playwright_ctx = None


def fetch_with_playwright(url):
    """Last-resort fetch via real headless Chromium. This is used only for
    pages that fail every urllib attempt in fetch_with_retry — it renders
    the full page with real JavaScript execution and a genuine browser
    TLS/canvas fingerprint, which passes lighter bot-management checks
    that a plain urllib request (no matter the headers or User-Agent)
    cannot. It does NOT help against a hard IP-reputation block on the
    runner's datacenter IP range — that would need a different exit IP
    entirely. Returns (ok, content_type, raw_bytes_or_none, error_or_none),
    matching fetch()'s signature so callers can treat it interchangeably."""
    browser = _get_playwright_browser()
    if browser is None:
        return False, "", None, "playwright not available"
    page = None
    try:
        page = browser.new_page(user_agent=USER_AGENT)
        page.set_default_navigation_timeout(PLAYWRIGHT_NAV_TIMEOUT_MS)
        page.goto(url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_NAV_TIMEOUT_MS)
        try:
            # Best-effort: let late JS-rendered content settle. Many pages
            # never truly go idle (ads/trackers keep polling), so this is
            # allowed to time out without failing the fetch.
            page.wait_for_load_state("networkidle", timeout=PLAYWRIGHT_IDLE_TIMEOUT_MS)
        except Exception:  # noqa: BLE001
            pass
        try:
            # Charles, 2026-09-07: found via CIRCABC row 86 (a live, real
            # document page, not a dead link) still only capturing a cookie
            # banner + "Loading..." after networkidle resolved — Angular's
            # own render/zone.js cycle can lag a beat behind the network
            # settling, so "no more network requests" doesn't mean "DOM
            # finished painting" for every SPA. A flat extra 3s here costs
            # nothing on ordinary pages (they already have their content)
            # and gives slow-rendering apps a chance to finish painting
            # before we capture and scroll.
            page.wait_for_timeout(3000)
        except Exception:  # noqa: BLE001
            pass
        try:
            # Charles, 2026-09-07: "we need to review the entire page ...
            # otherwise we will miss important content." Some pages only
            # populate real content (infinite-scroll lists, intersection-
            # observer-triggered images/text) once the viewport actually
            # reaches them — a page that never scrolls never renders it,
            # no matter how long we wait at the top. Walk down the page in
            # steps so anything gated on scroll position gets a chance to
            # load, then return to the top before capturing content() (some
            # sites lazy-unload text above the fold once you've scrolled
            # past it, so ending at the top is the safer place to capture
            # from). Best-effort: never fails the fetch if it errors.
            prev_height = 0
            for _ in range(8):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(400)
                height = page.evaluate("document.body.scrollHeight")
                if height == prev_height:
                    break
                prev_height = height
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(200)
        except Exception:  # noqa: BLE001
            pass
        html = page.content()
        return True, "text/html; charset=utf-8", html.encode("utf-8"), None
    except Exception as e:  # noqa: BLE001 — deliberately broad, this is a monitor
        return False, "", None, f"playwright: {str(e)[:200]}"
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass


def fetch_with_flaresolverr(url):
    """Last-of-last-resort fetch via a locally-running FlareSolverr instance
    (see daily-watch.yml — it runs as a Docker service alongside this job,
    nothing to sign up for). Sends a plain 'solve this URL' request over
    FlareSolverr's own HTTP API and gets back the already-rendered page.
    Returns (ok, content_type, raw_bytes_or_none, error_or_none), matching
    fetch()'s signature. If the service isn't running or unreachable (e.g.
    running this file outside the GitHub Actions workflow), this fails soft —
    same optional-dependency philosophy as PLAYWRIGHT_AVAILABLE — so a
    missing FlareSolverr never breaks the run, the page just falls through
    to being reported as a gap same as before this existed."""
    payload = json.dumps({
        "cmd": "request.get",
        "url": url,
        "maxTimeout": FLARESOLVERR_TIMEOUT_MS,
    }).encode()
    req = urllib.request.Request(
        FLARESOLVERR_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=(FLARESOLVERR_TIMEOUT_MS / 1000) + 10) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:  # noqa: BLE001 — service not running, timed out, etc.
        return False, "", None, f"flaresolverr unreachable: {str(e)[:200]}"

    if result.get("status") != "ok":
        return False, "", None, f"flaresolverr: {str(result.get('message', 'unknown error'))[:200]}"

    solution = result.get("solution") or {}
    html = solution.get("response") or ""
    if not html:
        return False, "", None, "flaresolverr: empty response"
    return True, "text/html; charset=utf-8", html.encode("utf-8"), None


MIN_VISIBLE_TEXT_CHARS = 400  # below this, a "successful" fetch is treated
    # as suspect — probably a JS-shell page that returned 200 with almost no
    # server-rendered content, not a real gap, but not full content either.
    # Raised from 200 to 400 on 2026-09-07 after finding CIRCABC's 4 rows all
    # cleared 200 (206 chars each) while capturing only a cookie-consent
    # banner plus a literal "Loading..." placeholder — Playwright's own
    # networkidle/scroll wait wasn't enough for CIRCABC's slow Angular API
    # calls, and 200 was just under that specific stuck-state's length. 400
    # comfortably clears it while a genuinely tiny real page (several exist
    # in this watchlist, some under 60 chars) still gets returned via the
    # best-so-far fallback if nothing longer turns up on any layer — this
    # only costs extra escalation attempts, it can never lose real content.


def _visible_text_len(raw, content_type):
    """Cheap, best-effort estimate of how much real visible text a fetched
    response contains — used only to decide whether a nominally-successful
    fetch is actually worth trusting, not the real extraction (see
    extract_text_and_images, which does the full chrome-stripping compare
    later). Binary responses (PDFs etc.) are exempt by the caller — sniffing
    a PDF as if it were HTML text always looks empty and would wrongly
    trigger escalation for something that was never a text page."""
    try:
        html = raw.decode("utf-8", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "nav", "header", "footer", "form"]):
            tag.decompose()
        return len(soup.get_text(strip=True))
    except Exception:  # noqa: BLE001
        return 0


def fetch_with_retry(url):
    """Tries every layer before giving up on a page. Charles, 2026-09-07:
    "we can't have gaps (unless they are genuinely broken)." A plain HTTP
    404 used to be treated as terminal — the reasoning was "the URL itself
    is wrong, retrying won't fix that" — but that reasoning doesn't hold in
    general: some sites 404 a plain script request on a path that resolves
    fine through client-side routing or a bot-management rule, and only a
    real rendered browser can tell the difference. Verified directly
    2026-09-07: most of a sample of 404 rows were confirmed genuinely dead
    by opening them in a real browser, but that confirmation came from
    trying a real browser, not from trusting the first 404. So a 404 no
    longer short-circuits anything — it gets exactly the same escalation
    as a 403, timeout, or empty body: alternate-UA retries, then Playwright,
    then FlareSolverr. Only a page that fails *every* layer, including a
    fully rendered browser, gets reported as a gap.

    First attempt uses the normal browser identity; up to three more tries
    follow with short backoff, rotating through alternate UAs in case the
    block is keyed on the browser identity rather than the source IP. If
    every urllib attempt still fails, a headless Chromium fetch
    (fetch_with_playwright) is tried next, and if that also fails,
    FlareSolverr (fetch_with_flaresolverr) is tried as a final fallback —
    it runs a stealth-patched Chromium specifically built to solve
    Cloudflare-style JS challenges, a step up from Playwright's plain fetch
    for that one failure class. Both are free and require no signup;
    they're ordered cheapest-and-most-general-first so no attempt is wasted
    on a page a simpler method could already handle.

    A response that comes back HTTP 200 but is actually a bot-challenge
    interstitial (see is_bot_challenge) is treated exactly like any other
    failure here — it clears ok/raw so the retry loop keeps going and, if
    every attempt (including Playwright and FlareSolverr) hits the same
    wall, the page is correctly reported as a GAP with an error naming the
    block, instead of being stored as a snapshot that flips on every run.

    A response that is technically "ok" but suspiciously thin (see
    MIN_VISIBLE_TEXT_CHARS) — a JS-rendered page that a plain script
    request only ever sees as an empty shell, not a Cloudflare-style
    challenge, just genuinely no server-rendered text — does NOT stop the
    escalation either: it's kept as the best-so-far candidate, but every
    remaining layer still gets tried in case a real browser renders more.
    Only if nothing does better is the thin result finally accepted,
    on the theory that a page that's thin everywhere really is just short
    (some regulatory notices genuinely are one line) rather than treating
    every short page as a failure."""

    def _reject_challenge(ok, ct, raw, err):
        if ok and raw and len(raw) >= 20 and is_bot_challenge(raw, ct):
            return False, ct, raw, "blocked by bot-challenge interstitial (e.g. Cloudflare) — page returned 200 but body is a verification page, not real content"
        return ok, ct, raw, err

    def _is_thin(ok, ct, raw):
        if not ok or not raw or any(bt in (ct or "") for bt in BINARY_CONTENT_TYPES):
            return False
        return _visible_text_len(raw, ct) < MIN_VISIBLE_TEXT_CHARS

    best = None  # best-so-far (ok, ct, raw, err) among thin-but-technically-ok results

    def _consider(ok, ct, raw, err):
        nonlocal best
        if not (ok and raw and len(raw) >= 20):
            return None
        if not _is_thin(ok, ct, raw):
            return (ok, ct, raw, err)
        if best is None or len(raw) > len(best[2]):
            best = (ok, ct, raw, err)
        return None

    ok, ct, raw, err = _reject_challenge(*fetch(url, USER_AGENT))
    result = _consider(ok, ct, raw, err)
    if result:
        return result

    for user_agent, delay in ((USER_AGENT, 2), (UA_FIREFOX, 3), (UA_GOOGLEBOT, 3)):
        time.sleep(delay)
        ok, ct, raw, err = _reject_challenge(*fetch(url, user_agent))
        result = _consider(ok, ct, raw, err)
        if result:
            return result

    if PLAYWRIGHT_AVAILABLE:
        pw_ok, pw_ct, pw_raw, pw_err = _reject_challenge(*fetch_with_playwright(url))
        result = _consider(pw_ok, pw_ct, pw_raw, pw_err)
        if result:
            return result
        if pw_ok:
            err = None  # a thin-but-ok Playwright fetch supersedes the earlier error text
        else:
            err = f"{err} | playwright: {pw_err}" if err else pw_err

    fs_ok, fs_ct, fs_raw, fs_err = False, "", None, None
    for attempt in range(FLARESOLVERR_ATTEMPTS):
        fs_ok, fs_ct, fs_raw, fs_err = _reject_challenge(*fetch_with_flaresolverr(url))
        result = _consider(fs_ok, fs_ct, fs_raw, fs_err)
        if result:
            return result
        if attempt < FLARESOLVERR_ATTEMPTS - 1:
            time.sleep(FLARESOLVERR_RETRY_DELAY)

    if best is not None:
        return best

    combined_err = f"{err} | flaresolverr: {fs_err}" if err else fs_err
    return ok, ct, raw, combined_err


def _looks_like_content_image(img_tag, resolved_url):
    """Heuristic filter: True if this <img> looks like real page content
    worth hashing and comparing, False if it looks like chrome (a logo,
    icon, tracking pixel, decorative flourish). Deliberately conservative —
    missing a genuine content image is much cheaper than drowning every
    page in false "changed" alerts from a rotating ad banner or a
    per-request tracking pixel with a random query string."""
    path = urllib.parse.urlparse(resolved_url).path.lower()
    if path.endswith(SKIP_IMAGE_EXTENSIONS):
        return False
    class_attr = img_tag.get("class") or ""
    if not isinstance(class_attr, str):
        class_attr = " ".join(class_attr)
    haystack = " ".join(filter(None, [
        resolved_url, img_tag.get("alt", ""), class_attr, img_tag.get("id", ""),
    ])).lower()
    if SKIP_IMAGE_PATTERN.search(haystack):
        return False
    for attr in ("width", "height"):
        val = img_tag.get(attr)
        if val:
            digits = re.sub(r"\D", "", str(val))
            if digits and int(digits) < MIN_CONTENT_IMAGE_DIMENSION:
                return False
    return True


def extract_images(soup, base_url):
    """Finds up to MAX_IMAGES_PER_PAGE candidate content images in an
    already-parsed, already-chrome-stripped soup — nav/header/footer/script
    tags are already gone by the time this runs (extract_text_and_images
    decomposes them first), so logos and icons living in those regions are
    excluded for free, before the heuristic filter below even runs.
    Returns resolved absolute URLs in page order."""
    found = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if not src or src.startswith("data:"):
            continue
        resolved = urllib.parse.urljoin(base_url, src)
        if not resolved.startswith(("http://", "https://")):
            continue
        if _looks_like_content_image(img, resolved):
            found.append(resolved)
        if len(found) >= MAX_IMAGES_PER_PAGE:
            break
    return found


def fetch_image_hash(url):
    """Fetches one candidate content image and returns its SHA-256 hash, or
    None if it can't be fetched or doesn't actually look like an image
    (some sites respond to an image request with an HTML error/challenge
    page — that must never get hashed as if it were the image). Failures
    here are silent by design: this is a best-effort enrichment on top of
    the text diff, not a hard requirement, so one unreachable image never
    turns a page's own fetch into a gap."""
    req = urllib.request.Request(url, headers=_headers_for(USER_AGENT))
    try:
        with urllib.request.urlopen(req, timeout=IMAGE_FETCH_TIMEOUT) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if not content_type.startswith("image/"):
                return None
            raw = resp.read()
            if not raw:
                return None
            return hashlib.sha256(raw).hexdigest()
    except Exception:  # noqa: BLE001 — best-effort only, see docstring
        return None


def describe_image_change(old_hashes, new_hashes):
    """Turns an old {url: hash} vs. new {url: hash} comparison into a short
    human-readable note, the same role classify_change()'s note plays for
    text — e.g. "1 image(s) changed; 1 image(s) added"."""
    added = [u for u in new_hashes if u not in old_hashes]
    removed = [u for u in old_hashes if u not in new_hashes]
    changed = [u for u in new_hashes if u in old_hashes and old_hashes[u] != new_hashes[u]]
    parts = []
    if changed:
        parts.append(f"{len(changed)} image(s) changed")
    if added:
        parts.append(f"{len(added)} image(s) added")
    if removed:
        parts.append(f"{len(removed)} image(s) removed")
    return "; ".join(parts) if parts else "image content changed"


def extract_text_and_images(html_bytes, content_type, base_url):
    """Returns (text, image_urls) from a single HTML parse. text is the
    full extracted, chrome-stripped visible text — no length cap (see the
    comment by the removed MAX_TEXT_CHARS above): every character gets
    compared, however long the page is. image_urls is up to
    MAX_IMAGES_PER_PAGE candidate content images (see extract_images) for
    the caller to hash and compare separately, since a genuine regulatory
    change is sometimes published as a graphic (a fee table, a scanned
    notice) rather than as text."""
    charset = "utf-8"
    m = re.search(r"charset=([\w-]+)", content_type or "", re.IGNORECASE)
    if m:
        charset = m.group(1)
    try:
        html = html_bytes.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        html = html_bytes.decode("utf-8", errors="replace")

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "header", "footer", "form"]):
        tag.decompose()

    images = extract_images(soup, base_url)

    raw_lines = soup.get_text("\n").splitlines()
    kept = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        if CHROME_RE.search(line) and not KEEP_HINTS_RE.search(line):
            continue
        kept.append(line)

    text = "\n".join(kept)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(), images


def normalize_for_compare(text):
    return re.sub(r"\s+", " ", text.lower()).strip()


def classify_change(old_text, new_text):
    """Returns (is_changed, note). Applies cosmetic-noise filtering."""
    old_n, new_n = normalize_for_compare(old_text), normalize_for_compare(new_text)
    if old_n == new_n:
        return False, None

    ratio = difflib.SequenceMatcher(None, old_n, new_n).ratio()
    if ratio >= 0.995:
        return False, "near-identical (>99.5% match) — treated as cosmetic"

    diff_lines = list(
        difflib.unified_diff(old_text.splitlines(), new_text.splitlines(), lineterm="", n=0)
    )
    changed_lines = [l for l in diff_lines if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
    changed_lines = [l[1:].strip() for l in changed_lines if l[1:].strip()]

    if not changed_lines:
        return False, "whitespace-only difference"

    note = " | ".join(changed_lines[:3])[:400]
    return True, note


def load_watchlist():
    data = load_json(WATCHLIST_PATH, {"entries": []})
    return data.get("entries", [])


def create_github_issue(date_str, changes, gaps):
    """Opens one issue summarizing this run's changes. GitHub emails the repo
    owner automatically whenever an issue is opened — that email IS the
    notification. Returns (success, issue_number_or_None, error_or_None)."""
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        log("No GITHUB_TOKEN/GITHUB_REPOSITORY set — skipping issue creation "
            f"(would have reported {len(changes)} change(s)).")
        return False, None, "no token/repo"

    lines = [f"**{len(changes)} page(s) changed** on {date_str}.\n"]
    for c in changes:
        lines.append(f"- [{c['description'] or c['url']}]({c['url']})\n  {c['note']}")
    if gaps:
        lines.append(f"\n_{len(gaps)} page(s) could not be fetched this run — see runs.json for details._")
    body = "\n".join(lines)[:60000]  # GitHub issue body size guard

    payload = json.dumps({
        "title": f"Regulatory changes detected — {date_str} ({len(changes)} page{'s' if len(changes) != 1 else ''})",
        "body": body,
        "labels": ["regulatory-change"],
    }).encode()
    req = urllib.request.Request(
        f"{GITHUB_API_URL}/repos/{GITHUB_REPOSITORY}/issues",
        data=payload,
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            result = json.loads(resp.read().decode())
            return True, result.get("number"), None
    except urllib.error.HTTPError as e:
        return False, None, f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}"
    except Exception as e:  # noqa: BLE001
        return False, None, str(e)[:300]


def escape_md(text):
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


UK_TZ = ZoneInfo("Europe/London")


def format_utc(iso_str):
    """Formats an ISO-8601 '...Z' timestamp (e.g. from started_at/finished_at,
    always stored/recorded in UTC) as UK local time for display —
    'YYYY-MM-DD HH:MM:SS GMT' or '... BST' depending on the time of year,
    since Europe/London observes British Summer Time. Drops sub-second
    precision, which is noise for a human reading the dashboard. Falls back
    to plain UTC text if the system has no tzdata (shouldn't happen on the
    GitHub Actions runner, but this keeps the dashboard readable either way)."""
    if not iso_str:
        return "—"
    s = iso_str.rstrip("Z")
    if "." in s:
        s = s.split(".", 1)[0]
    try:
        dt_utc = datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=datetime.timezone.utc
        )
        dt_uk = dt_utc.astimezone(UK_TZ)
        tz_label = dt_uk.tzname() or "UK"
        return dt_uk.strftime("%Y-%m-%d %H:%M:%S") + f" {tz_label}"
    except Exception:  # noqa: BLE001 — no tzdata or unexpected format
        return s.replace("T", " ") + " UTC"


def format_dashboard(entries, snapshots, runs, run_record):
    """Builds DASHBOARD.md — a GitHub-rendered Markdown page that always shows
    the latest state. Viewed at the repo's normal blob URL, gated by GitHub's
    own login, so a private repo stays private with no extra hosting needed."""
    l = []
    l.append("# Cloud Regulatory Watch — Dashboard")
    l.append("")
    l.append(f"_Last updated: {format_utc(run_record['finished_at'])} (run date {run_record['date']})_")
    l.append("")
    l.append("Auto-generated by `monitor.py` on every run. Do not edit by hand — it gets overwritten.")
    l.append("")

    l.append("## Latest run")
    l.append("")
    l.append("| Metric | Value |")
    l.append("|---|---|")
    l.append(f"| Completed (UK time) | {format_utc(run_record['finished_at'])} |")
    l.append(f"| Started (UK time) | {format_utc(run_record.get('started_at'))} |")
    l.append(f"| Pages checked | {run_record['checked']} |")
    l.append(f"| New (baseline) | {run_record['new_baseline']} |")
    l.append(f"| Unchanged | {run_record['unchanged']} |")
    l.append(f"| Changed | {run_record['changed']} |")
    l.append(f"| Coverage gaps | {run_record['gaps']} |")
    l.append(f"| Dead / expired links | {run_record.get('dead', 0)} |")
    issue_txt = f"#{run_record['issue_number']}" if run_record.get("issue_number") else "none opened"
    l.append(f"| GitHub issue | {issue_txt} |")
    l.append("")

    if run_record["changes"]:
        l.append(f"## Changes detected this run ({len(run_record['changes'])})")
        l.append("")
        for c in run_record["changes"]:
            l.append(f"- **row {c['row']}** [{escape_md(c['description']) or c['url']}]({c['url']})")
            l.append(f"  {escape_md(c['note'])}")
        l.append("")

    if run_record["gaps_list"]:
        l.append(f"## Coverage gaps this run ({len(run_record['gaps_list'])})")
        l.append("")
        l.append("<details><summary>Show gap list</summary>")
        l.append("")
        l.append("| Row | Page | Error |")
        l.append("|---|---|---|")
        for g in run_record["gaps_list"]:
            l.append(f"| {g['row']} | [{escape_md(g['description']) or g['url']}]({g['url']}) | {escape_md(g['error'])} |")
        l.append("")
        l.append("</details>")
        l.append("")

    if run_record.get("dead_links"):
        # Charles, 2026-09-07: "add a section for links that are expired
        # or no longer working." Distinct from Coverage gaps above -- a gap
        # means the fetch itself failed (blocked, timed out, network
        # error); a dead link means the fetch succeeded and the source
        # itself says the resource is gone (a soft-404, confirmed by
        # opening the page directly). These need a different fix: a gap
        # might resolve itself next run, a dead link needs the URL
        # updated or removed from watchlist.json.
        l.append(f"## Dead / expired links this run ({len(run_record['dead_links'])})")
        l.append("")
        l.append("<details><summary>Show dead-link list</summary>")
        l.append("")
        l.append("| Row | Page | What the source page says |")
        l.append("|---|---|---|")
        for d in run_record["dead_links"]:
            l.append(f"| {d['row']} | [{escape_md(d['description']) or d['url']}]({d['url']}) | {escape_md(d['note'])} |")
        l.append("")
        l.append("</details>")
        l.append("")

    l.append("## Run history (most recent first)")
    l.append("")
    l.append("| Date | Completed (UK time) | Checked | New | Unchanged | Changed | Gaps | Dead | Issue |")
    l.append("|---|---|---|---|---|---|---|---|---|")
    for r in list(reversed(runs))[:30]:
        r_issue = f"#{r['issue_number']}" if r.get("issue_number") else "—"
        finished = format_utc(r.get("finished_at"))
        l.append(f"| {r['date']} | {finished} | {r['checked']} | {r['new_baseline']} | {r['unchanged']} | "
                  f"{r['changed']} | {r['gaps']} | {r.get('dead', 0)} | {r_issue} |")
    l.append("")

    l.append(f"## Current status — all {len(entries)} watched pages")
    l.append("")
    l.append("<details><summary>Show full list</summary>")
    l.append("")
    l.append("| Row | Description | Status | Last changed |")
    l.append("|---|---|---|---|")
    for entry in sorted(entries, key=lambda e: e["row"]):
        snap = snapshots.get(entry["slug"], {})
        snap_status = snap.get("status")
        status = "GAP" if snap_status == "gap" else ("DEAD" if snap_status == "dead" else "OK")
        last_changed = snap.get("last_changed") or "—"
        desc = escape_md(entry.get("description")) or entry["url"]
        l.append(f"| {entry['row']} | [{desc}]({entry['url']}) | {status} | {last_changed} |")
    l.append("")
    l.append("</details>")
    l.append("")

    return "\n".join(l)


def main():
    started_at = datetime.datetime.utcnow().isoformat() + "Z"
    entries = load_watchlist()
    snapshots = load_json(SNAPSHOTS_PATH, {})
    runs = load_json(RUNS_PATH, [])

    counts = {"checked": 0, "new": 0, "unchanged": 0, "changed": 0, "gap": 0, "dead": 0}
    changes = []
    gaps = []
    dead_links = []

    log(f"Loaded {len(entries)} watchlist entries, {len(snapshots)} existing snapshots.")

    for entry in entries:
        slug = entry["slug"]
        url = entry["url"]
        counts["checked"] += 1

        ok, content_type, raw, err = fetch_with_retry(url)
        prev = snapshots.get(slug)

        if not ok or not raw or len(raw) < 20:
            counts["gap"] += 1
            gaps.append({**entry, "error": err or "empty response"})
            snapshots[slug] = {
                **entry,
                "mode": (prev or {}).get("mode", "text"),
                "text": (prev or {}).get("text"),
                "hash": (prev or {}).get("hash"),
                "image_hashes": (prev or {}).get("image_hashes"),
                "status": "gap",
                "last_checked": started_at,
                "last_changed": (prev or {}).get("last_changed"),
                "last_change_note": (prev or {}).get("last_change_note"),
            }
            log(f"GAP  row {entry['row']:>3}  {url}  ({err or 'empty'})")
            continue

        is_binary = any(bt in (content_type or "") for bt in BINARY_CONTENT_TYPES)

        if is_binary:
            new_hash = hashlib.sha256(raw).hexdigest()
            if prev is None:
                status, note = "new", None
            elif prev.get("hash") == new_hash:
                status, note = "unchanged", None
            else:
                status, note = "changed", "binary/PDF content hash changed since last check"

            snapshots[slug] = {
                **entry, "mode": "binary", "hash": new_hash, "text": None,
                "status": "ok",
                "last_checked": started_at,
                "last_changed": started_at if status == "changed" else (prev or {}).get("last_changed"),
                "last_change_note": note if status == "changed" else (prev or {}).get("last_change_note"),
            }
        else:
            new_text, new_image_urls = extract_text_and_images(raw, content_type, url)

            if text_is_bot_challenge(new_text):
                # The raw-bytes check (is_bot_challenge, inside
                # fetch_with_retry) already missed this once for this exact
                # response — every fetch layer's raw HTML is only sniffed up
                # to a byte limit, and FlareSolverr in particular can return
                # a challenge page whose markup pushes "Just a moment..."
                # further down than that window covers (rows 52/57/76/131,
                # 2026-09-07). This is the safety net: it checks the final,
                # already-extracted visible text instead, which is small
                # and mostly IS the challenge message when a challenge slips
                # through, so it can't miss on size. Treat exactly like a
                # fetch failure — never let a solved-looking-but-not-really
                # challenge page overwrite a real snapshot.
                counts["gap"] += 1
                gaps.append({**entry, "error": "blocked by bot-challenge interstitial (caught post-extraction) — page returned 200 but body is a verification page, not real content"})
                snapshots[slug] = {
                    **entry,
                    "mode": (prev or {}).get("mode", "text"),
                    "text": (prev or {}).get("text"),
                    "hash": (prev or {}).get("hash"),
                    "image_hashes": (prev or {}).get("image_hashes"),
                    "status": "gap",
                    "last_checked": started_at,
                    "last_changed": (prev or {}).get("last_changed"),
                    "last_change_note": (prev or {}).get("last_change_note"),
                }
                log(f"GAP  row {entry['row']:>3}  {url}  (bot-challenge caught post-extraction)")
                continue

            dead_reason = is_dead_link(new_text)
            is_dead = dead_reason is not None
            had_dead_before = (prev or {}).get("status") == "dead"

            # Hash each candidate content image found on the page (see
            # extract_images/fetch_image_hash above). A page whose text is
            # byte-identical to last time can still have genuinely changed
            # if a fee table or notice published as a graphic was swapped
            # out — this is how that gets caught.
            new_image_hashes = {}
            for img_url in new_image_urls:
                h = fetch_image_hash(img_url)
                if h:
                    new_image_hashes[img_url] = h
            prev_image_hashes = (prev or {}).get("image_hashes") or {}
            # Distinguish "never tracked images for this page before" from
            # "tracked them and they're the same" — an existing page whose
            # snapshot predates this feature has no "image_hashes" key at
            # all, and establishing that first baseline must not itself
            # count as a change (same principle as prev is None for text).
            # Without this, the rollout run would flag every page with at
            # least one qualifying image as "changed" purely from having
            # nothing to compare against yet.
            had_image_baseline = prev is not None and "image_hashes" in prev

            if prev is None or prev.get("text") is None:
                status, note = "new", None
            else:
                text_changed, text_note = classify_change(prev.get("text", ""), new_text)
                if is_dead and had_dead_before:
                    # Both before and after are "this resource is gone"
                    # states -- some dead-page templates embed a dynamic
                    # element (a request id, a random "related pages"
                    # list) that would otherwise diff as a false "changed"
                    # every run. A page that's dead on both sides has
                    # nothing new to report; only a genuine live<->dead
                    # transition (handled below via prev.get("status"))
                    # should ever surface as a real change.
                    text_changed = False
                images_changed = had_image_baseline and new_image_hashes != prev_image_hashes
                if text_changed or images_changed:
                    status = "changed"
                    note_parts = []
                    if text_changed and text_note:
                        note_parts.append(text_note)
                    if images_changed:
                        note_parts.append(describe_image_change(prev_image_hashes, new_image_hashes))
                    note = " | ".join(note_parts) if note_parts else "image content changed"
                else:
                    status, note = "unchanged", None

            snapshots[slug] = {
                **entry, "mode": "text", "text": new_text, "hash": None,
                "image_hashes": new_image_hashes,
                "status": "dead" if is_dead else "ok",
                "last_checked": started_at,
                "last_changed": started_at if status == "changed" else (prev or {}).get("last_changed"),
                "last_change_note": note if status == "changed" else (prev or {}).get("last_change_note"),
            }

            if is_dead:
                # Charles, 2026-09-07: "add a section for links that are
                # expired or no longer working." Reported every run the
                # page still reads as dead (not just the run it first
                # flips) so the dashboard's dead-link list always reflects
                # current reality, the same way gaps_list already does.
                counts["dead"] += 1
                dead_links.append({**entry, "note": dead_reason[:200]})
                log(f"DEAD row {entry['row']:>3}  {url}  ({dead_reason[:100]})")

        counts[status] += 1
        if status == "changed":
            changes.append({**entry, "note": note})
            log(f"CHANGED row {entry['row']:>3}  {url}  -- {note}")
        elif status == "new":
            log(f"NEW   row {entry['row']:>3}  {url}  (baseline)")
        else:
            log(f"OK    row {entry['row']:>3}  {url}")

    log(f"Fetch pass done. Counts: {counts}")
    close_playwright()

    issue_number, issue_error = None, None
    if changes:
        success, issue_number, issue_error = create_github_issue(started_at[:10], changes, gaps)
        if success:
            log(f"Opened GitHub issue #{issue_number} for {len(changes)} change(s).")
        else:
            log(f"FAILED to open GitHub issue: {issue_error}")

    finished_at = datetime.datetime.utcnow().isoformat() + "Z"
    run_record = {
        "date": started_at[:10],
        "started_at": started_at,
        "finished_at": finished_at,
        "checked": counts["checked"],
        "new_baseline": counts["new"],
        "unchanged": counts["unchanged"],
        "changed": counts["changed"],
        "gaps": counts["gap"],
        "dead": counts["dead"],
        "issue_number": issue_number,
        "issue_error": issue_error,
        "changes": [{"row": c["row"], "vp_id": c["vp_id"], "url": c["url"],
                     "description": c["description"], "note": c["note"]} for c in changes],
        "gaps_list": [{"row": g["row"], "vp_id": g["vp_id"], "url": g["url"],
                       "description": g["description"], "error": g["error"]} for g in gaps],
        "dead_links": [{"row": d["row"], "vp_id": d["vp_id"], "url": d["url"],
                        "description": d["description"], "note": d["note"]} for d in dead_links],
    }
    runs.append(run_record)
    runs = runs[-90:]  # keep the most recent ~90 days, no unbounded growth

    save_json(SNAPSHOTS_PATH, snapshots)
    save_json(RUNS_PATH, runs)

    dashboard_md = format_dashboard(entries, snapshots, runs, run_record)
    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(dashboard_md)

    log(
        f"DONE. checked={counts['checked']} new={counts['new']} "
        f"unchanged={counts['unchanged']} changed={counts['changed']} "
        f"gaps={counts['gap']} dead={counts['dead']} issue={issue_number}"
    )
    if changes and issue_error:
        log(f"{len(changes)} real change(s) detected but the GitHub issue FAILED to open — see runs.json.")


if __name__ == "__main__":
    main()
