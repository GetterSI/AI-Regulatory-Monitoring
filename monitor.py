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
  5. Posts one Monday.com update per real (non-cosmetic) change.
  6. Writes snapshots.json and runs.json back out so the next run — and a
     GitHub Actions commit step — can pick up where this one left off.

Environment variables required:
  MONDAY_API_TOKEN   personal API token for the monday.com GraphQL API
  MONDAY_ITEM_ID      the item to post updates to (defaults to the one below)
"""
import os
import re
import sys
import json
import time
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_PATH = os.path.join(BASE_DIR, "watchlist.json")
SNAPSHOTS_PATH = os.path.join(BASE_DIR, "snapshots.json")
RUNS_PATH = os.path.join(BASE_DIR, "runs.json")

MONDAY_API_URL = "https://api.monday.com/v2"
MONDAY_ITEM_ID = os.environ.get("MONDAY_ITEM_ID", "12967259713")
MONDAY_TOKEN = os.environ.get("MONDAY_API_TOKEN", "")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 CloudRegulatoryWatch/1.0"
)
REQUEST_TIMEOUT = 20
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


def fetch(url):
    """Returns (ok, content_type, raw_bytes_or_none, error_or_none)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read()
            return True, content_type, raw, None
    except Exception as e:  # noqa: BLE001 — deliberately broad, this is a monitor
        return False, "", None, str(e)


def fetch_with_retry(url):
    ok, ct, raw, err = fetch(url)
    if not ok or not raw:
        time.sleep(2)
        ok, ct, raw, err = fetch(url)
    return ok, ct, raw, err


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


def post_monday_update(url, description, note):
    if not MONDAY_TOKEN:
        log("No MONDAY_API_TOKEN set — skipping Monday post (would have posted): "
            f"{description} — {note}")
        return False, "no token"

    safe_desc = (description or url).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    safe_note = (note or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    body = f'<div><strong><a href="{url}">{safe_desc}</a></strong></div><div>{safe_note}</div>'

    query = """
    mutation ($itemId: ID!, $body: String!) {
      create_update(item_id: $itemId, body: $body) { id }
    }
    """
    payload = json.dumps({"query": query, "variables": {"itemId": MONDAY_ITEM_ID, "body": body}}).encode()
    req = urllib.request.Request(
        MONDAY_API_URL,
        data=payload,
        headers={
            "Authorization": MONDAY_TOKEN,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            result = json.loads(resp.read().decode())
            if result.get("errors"):
                return False, json.dumps(result["errors"])[:300]
            return True, None
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:300]


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

    posted, suppressed = [], []
    for change in changes:
        success, err = post_monday_update(change["url"], change["description"], change["note"])
        if success:
            posted.append(change)
        else:
            suppressed.append({**change, "post_error": err})
            log(f"Monday post FAILED for row {change['row']}: {err}")
        time.sleep(0.5)

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
        "posted_to_monday": len(posted),
        "changes": [{"row": c["row"], "vp_id": c["vp_id"], "url": c["url"],
                     "description": c["description"], "note": c["note"]} for c in changes],
        "post_failures": suppressed,
        "gaps_list": [{"row": g["row"], "vp_id": g["vp_id"], "url": g["url"],
                       "description": g["description"], "error": g["error"]} for g in gaps],
    }
    runs.append(run_record)
    runs = runs[-90:]  # keep the most recent ~90 days, no unbounded growth

    save_json(SNAPSHOTS_PATH, snapshots)
    save_json(RUNS_PATH, runs)

    log(
        f"DONE. checked={counts['checked']} new={counts['new']} "
        f"unchanged={counts['unchanged']} changed={counts['changed']} "
        f"gaps={counts['gap']} posted_to_monday={len(posted)}"
    )
    if suppressed:
        log(f"{len(suppressed)} real change(s) detected but FAILED to post to Monday — see runs.json.")


if __name__ == "__main__":
    main()
