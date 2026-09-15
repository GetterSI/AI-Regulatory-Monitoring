#!/usr/bin/env python3
"""Drive monitor.py's real fetch chain against ONE url, in about 3 minutes.

Why this exists: a full daily-watch run takes 40-75 minutes, so diagnosing a
single row by running the monitor is far too slow a loop -- and on 2026-09-15
two fixes for row 113 were shipped and neither worked, because the evidence
needed to choose between the possible causes was buried in a step log that
GitHub's viewer would not render.

This prints, per fetch layer: whether it succeeded, how many bytes came back,
what _visible_text_len thinks of it, and -- the number that actually matters --
how much text survives extract_text_and_images, because that is what gets
stored and compared. Then it runs the real fetch_with_retry chain so the
answer includes the escalation and best-so-far logic, not just the layers.

Usage:  python tools/url_check.py <url>
Run it from probe.yml, which passes its extra_url input straight through.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import monitor  # noqa: E402


def describe(label, ok, ct, raw, err):
    size = len(raw) if raw else 0
    print("\n--- %s" % label)
    print("    ok=%s  bytes=%d  content_type=%r" % (ok, size, ct))
    if err:
        print("    error: %s" % str(err)[:300])
    if not raw:
        return
    print("    _visible_text_len (pre chrome-strip) = %d"
          % monitor._visible_text_len(raw, ct))
    try:
        text, images = monitor.extract_text_and_images(raw, ct, URL)
    except Exception as exc:  # noqa: BLE001
        print("    extraction raised: %r" % (exc,))
        return
    stripped = (text or "").strip()
    print("    EXTRACTED+CHROME-STRIPPED = %d   (MIN_VISIBLE_TEXT_CHARS=%d)"
          % (len(stripped), monitor.MIN_VISIBLE_TEXT_CHARS))
    print("    thin by that measure? %s" % (len(stripped) < monitor.MIN_VISIBLE_TEXT_CHARS))
    print("    images found: %d" % len(images or []))
    print("    first 400 chars: %r" % stripped[:400])


if len(sys.argv) < 2 or not sys.argv[1].strip():
    raise SystemExit("usage: python tools/url_check.py <url>")
URL = sys.argv[1].strip()
print("=" * 70)
print("url_check: %s" % URL)
print("playwright available: %s" % getattr(monitor, "PLAYWRIGHT_AVAILABLE", "?"))
print("=" * 70)

try:
    describe("plain urllib (fetch)", *monitor.fetch(URL))
except Exception as exc:  # noqa: BLE001
    print("\n--- plain urllib raised: %r" % (exc,))

try:
    describe("playwright (fetch_with_playwright)", *monitor.fetch_with_playwright(URL))
except Exception as exc:  # noqa: BLE001
    print("\n--- playwright raised: %r" % (exc,))

try:
    describe("flaresolverr (fetch_with_flaresolverr)", *monitor.fetch_with_flaresolverr(URL))
except Exception as exc:  # noqa: BLE001
    print("\n--- flaresolverr raised: %r" % (exc,))

print("\n" + "=" * 70)
print("THROUGH THE REAL CHAIN (fetch_with_retry, incl. escalation + best-so-far)")
print("=" * 70)
try:
    describe("fetch_with_retry", *monitor.fetch_with_retry(URL))
except Exception as exc:  # noqa: BLE001
    print("fetch_with_retry raised: %r" % (exc,))

print("\nDone. The EXTRACTED+CHROME-STRIPPED figure on the last block is what",
      "would be stored for this row.")
