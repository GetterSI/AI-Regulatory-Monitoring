#!/usr/bin/env python3
"""Regression tests for the two false-positive classes found on 2026-09-08.

Run:  python tests/test_noise_filters.py
Exits 1 on any failure, so a workflow step running it fails loudly.

These import monitor.py and drive its real functions. They deliberately do
NOT re-implement the filter logic: on 2026-08-26 a correct model was hand
transcribed into a deliverable and a precondition was dropped in the copy,
so the tested thing and the shipped thing were different things. The consent
cases therefore go through extract_text_and_images(), the actual extraction
path, rather than through a local copy of the line filter.

Every consent string below is a REAL string that was reported as a
regulatory change by a production run — not an invented example:
  row 5   run #12   "Consent | Details | [#IABV2SETTINGS#]"
  row 145 run #12   "Accept | Deny Non-Essential"
  row 72  run #13   "This site uses cookies ... cookies policy page"
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import monitor  # noqa: E402

failures = []


def check(name, condition, detail=""):
    if condition:
        print("  ok    %s" % name)
    else:
        print("  FAIL  %s %s" % (name, detail))
        failures.append(name)


# ---------------------------------------------------------------------------
# 1. Consent-manager UI must not reach the diff, and lawful uses of the word
#    "consent" must survive untouched.
# ---------------------------------------------------------------------------

CONSENT_LINES = [
    "Consent | Details | [#IABV2SETTINGS#]",
    "Accept | Deny Non-Essential",
    "This site uses cookies. Visit our | cookies policy page | or click",
    "&copy; 2026 OneTrust, LLC. Cookie Settings",
    "We use cookies to improve your experience. Accept all cookies",
    "Manage my cookie preferences",
    "Strictly necessary cookies are always active",
    "Your Privacy Choices",
]

# Several of these use "consent" in its legal sense on purpose. If a future
# broadening of CONSENT_RE ever swallows them, this test is the tripwire.
SUBSTANCE_LINES = [
    "Regulation (EU) No 649/2012 establishes prior informed consent procedures",
    "Prior Informed Consent (PIC) notification required before export",
    "The Rotterdam Convention operates a prior informed consent procedure",
    "The competent authority shall give its consent within 30 days",
    "Minimum Amount 2026: 2,000 CZK excl. VAT",
    "Last updated 08 September 2026. Database contains 14 unique substances/entries.",
    "Industrial batteries above 2 kWh require a passport",
]

html = "<html><body>"
for line in CONSENT_LINES + SUBSTANCE_LINES:
    html += "<p>%s</p>" % line
html += "</body></html>"

text, _images = monitor.extract_text_and_images(
    html.encode("utf-8"), "text/html; charset=utf-8", "https://example.test/"
)

print("CONSENT FILTER (through extract_text_and_images)")
for line in CONSENT_LINES:
    probe = line.replace("&copy;", "©")
    check("dropped: %s" % probe[:52], probe not in text)
for line in SUBSTANCE_LINES:
    check("kept:    %s" % line[:52], line in text)

check(
    "CONSENT_RE never matches the bare word 'consent'",
    not monitor.CONSENT_RE.search("consent"),
    "- a bare-word match would delete PIC substance",
)

# ---------------------------------------------------------------------------
# 2. Image comparison must be keyed on content, not on URL.
#    Scenarios are the real observed rows. (prev, new, text_changed, alert?)
# ---------------------------------------------------------------------------

IMAGE_CASES = [
    ("cache-busted url, identical bytes",
     {"/hero.png?v=1": "aaa"}, {"/hero.png?v=2": "aaa"}, False, False),
    ("lazy-load path swap, identical bytes",
     {"/img/fees.png": "bbb"}, {"/assets/fees.png": "bbb"}, False, False),
    ("rows 21/22: whole image set vanished, text unchanged",
     {"/a.png": "1", "/b.png": "2", "/c.png": "3", "/d.png": "4", "/e.png": "5"},
     {}, False, False),
    ("row 20: slider re-adds the same images under new urls",
     {"/s1.jpg": "x", "/s2.jpg": "y"},
     {"/s1.jpg": "x", "/s2.jpg": "y", "/s3.jpg": "x", "/s4.jpg": "y"}, False, False),
    ("genuine new graphic published, text unchanged",
     {"/old.png": "old"}, {"/old.png": "old", "/fees-2027.png": "new"}, False, True),
    ("same url, genuinely different bytes",
     {"/fees.png": "v1"}, {"/fees.png": "v2"}, False, True),
    ("images lost but text also changed",
     {"/a.png": "1"}, {}, True, True),
    ("nothing changed at all",
     {"/a.png": "1"}, {"/a.png": "1"}, False, False),
]

print("IMAGE COMPARISON (assess_image_change)")
for label, prev, new, text_changed, should_alert in IMAGE_CASES:
    alert, _note = monitor.assess_image_change(prev, new, True, text_changed)
    check("%-52s -> %s" % (label[:52], "alert" if should_alert else "silent"),
          bool(alert) == should_alert)

check(
    "no baseline yet never alerts",
    monitor.assess_image_change({}, {"/a.png": "1"}, False, False)[0] is False,
)

# ---------------------------------------------------------------------------

print()
if failures:
    print("FAILED: %d check(s) -> %s" % (len(failures), "; ".join(failures)))
    sys.exit(1)
print("All checks passed.")
