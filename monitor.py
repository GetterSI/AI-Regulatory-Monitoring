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
# Alternate identities used only as fallback retries when the primary
# request is blocked (403) or times out. Some sites' bot-management rules
# explicitly allowlist known search-engine crawlers even while blocking
# generic scripts, so a Googlebot-style UA occasionally gets through where
# a plain browser UA from a datacenter IP does not.
UA_FIREFOX = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0"
UA_GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
REQUEST_TIMEOUT = 20
PLAYWRIGHT_NAV_TIMEOUT_MS = 25000
PLAYWRIGHT_IDLE_TIMEOUT_MS = 8000
MAX_TEXT_CHARS = 6000

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
)


def is_bot_challenge(html_bytes, content_type):
    """True if raw looks like a bot-challenge interstitial rather than the
    real page. Only sniffs the first few KB (the challenge markup is always
    at the very top) and only for text responses — binaries (PDFs, etc.)
    never hit this path."""
    if not html_bytes or any(bt in (content_type or "") for bt in BINARY_CONTENT_TYPES):
        return False
    try:
        sample = html_bytes[:4000].decode("utf-8", errors="ignore").lower()
    except Exception:  # noqa: BLE001
        return False
    return any(marker in sample for marker in BOT_CHALLENGE_MARKERS)


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


def fetch_with_retry(url):
    """First attempt with the normal browser identity. A 404 means the URL
    itself is wrong — retrying won't fix that, so we stop immediately and
    report it (it should be corrected in watchlist.json). Anything else
    (403, timeout, empty body, connection error) gets up to three more
    tries with short backoff, rotating through alternate UAs in case the
    block is keyed on the browser identity rather than the source IP.
    If every urllib attempt still fails (and it wasn't a 404), a headless
    Chromium fetch (fetch_with_playwright) is tried once as a final
    fallback to obtain the full rendered page.

    A response that comes back HTTP 200 but is actually a bot-challenge
    interstitial (see is_bot_challenge) is treated exactly like any other
    failure here — it clears ok/raw so the retry loop keeps going and, if
    every attempt (including Playwright) hits the same wall, the page is
    correctly reported as a GAP with an error naming the block, instead of
    being stored as a snapshot that flips on every run."""

    def _reject_challenge(ok, ct, raw, err):
        if ok and raw and len(raw) >= 20 and is_bot_challenge(raw, ct):
            return False, ct, raw, "blocked by bot-challenge interstitial (e.g. Cloudflare) — page returned 200 but body is a verification page, not real content"
        return ok, ct, raw, err

    ok, ct, raw, err = _reject_challenge(*fetch(url, USER_AGENT))
    if ok and raw and len(raw) >= 20:
        return ok, ct, raw, err
    if err and "HTTP Error 404" in err:
        return ok, ct, raw, err

    got_404 = False
    for user_agent, delay in ((USER_AGENT, 2), (UA_FIREFOX, 3), (UA_GOOGLEBOT, 3)):
        time.sleep(delay)
        ok, ct, raw, err = _reject_challenge(*fetch(url, user_agent))
        if ok and raw and len(raw) >= 20:
            return ok, ct, raw, err
        if err and "HTTP Error 404" in err:
            got_404 = True
            break

    if got_404 or not PLAYWRIGHT_AVAILABLE:
        return ok, ct, raw, err

    pw_ok, pw_ct, pw_raw, pw_err = _reject_challenge(*fetch_with_playwright(url))
    if pw_ok and pw_raw and len(pw_raw) >= 20:
        return pw_ok, pw_ct, pw_raw, pw_err
    combined_err = f"{err} | playwright: {pw_err}" if err else pw_err
    return ok, ct, raw, combined_err


def extract_text(html_bytes, content_type):
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
    return text.strip()[:MAX_TEXT_CHARS]


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


def format_utc(iso_str):
    """Formats an ISO-8601 '...Z' timestamp (e.g. from started_at/finished_at)
    as 'YYYY-MM-DD HH:MM:SS UTC' for display — drops the sub-second precision,
    which is noise for a human reading the dashboard."""
    if not iso_str:
        return "—"
    s = iso_str.rstrip("Z")
    if "." in s:
        s = s.split(".", 1)[0]
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
    l.append(f"| Completed (UTC) | {format_utc(run_record['finished_at'])} |")
    l.append(f"| Started (UTC) | {format_utc(run_record.get('started_at'))} |")
    l.append(f"| Pages checked | {run_record['checked']} |")
    l.append(f"| New (baseline) | {run_record['new_baseline']} |")
    l.append(f"| Unchanged | {run_record['unchanged']} |")
    l.append(f"| Changed | {run_record['changed']} |")
    l.append(f"| Coverage gaps | {run_record['gaps']} |")
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

    l.append("## Run history (most recent first)")
    l.append("")
    l.append("| Date | Completed (UTC) | Checked | New | Unchanged | Changed | Gaps | Issue |")
    l.append("|---|---|---|---|---|---|---|---|")
    for r in list(reversed(runs))[:30]:
        r_issue = f"#{r['issue_number']}" if r.get("issue_number") else "—"
        finished = format_utc(r.get("finished_at"))
        l.append(f"| {r['date']} | {finished} | {r['checked']} | {r['new_baseline']} | {r['unchanged']} | "
                  f"{r['changed']} | {r['gaps']} | {r_issue} |")
    l.append("")

    l.append(f"## Current status — all {len(entries)} watched pages")
    l.append("")
    l.append("<details><summary>Show full list</summary>")
    l.append("")
    l.append("| Row | Description | Status | Last changed |")
    l.append("|---|---|---|---|")
    for entry in sorted(entries, key=lambda e: e["row"]):
        snap = snapshots.get(entry["slug"], {})
        status = "GAP" if snap.get("status") == "gap" else "OK"
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

    counts = {"checked": 0, "new": 0, "unchanged": 0, "changed": 0, "gap": 0}
    changes = []
    gaps = []

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
            new_text = extract_text(raw, content_type)
            if prev is None or prev.get("text") is None:
                status, note = "new", None
            else:
                is_changed, note = classify_change(prev.get("text", ""), new_text)
                status = "changed" if is_changed else "unchanged"

            snapshots[slug] = {
                **entry, "mode": "text", "text": new_text, "hash": None,
                "status": "ok",
                "last_checked": started_at,
                "last_changed": started_at if status == "changed" else (prev or {}).get("last_changed"),
                "last_change_note": note if status == "changed" else (prev or {}).get("last_change_note"),
            }

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
        "issue_number": issue_number,
        "issue_error": issue_error,
        "changes": [{"row": c["row"], "vp_id": c["vp_id"], "url": c["url"],
                     "description": c["description"], "note": c["note"]} for c in changes],
        "gaps_list": [{"row": g["row"], "vp_id": g["vp_id"], "url": g["url"],
                       "description": g["description"], "error": g["error"]} for g in gaps],
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
        f"gaps={counts['gap']} issue={issue_number}"
    )
    if changes and issue_error:
        log(f"{len(changes)} real change(s) detected but the GitHub issue FAILED to open — see runs.json.")


if __name__ == "__main__":
    main()
