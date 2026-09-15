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
    # The three shapes that escaped the first pass and were still reported as
    # regulatory changes in issue #15. Exact strings from that issue.
    "Consent | Details | About",
    "or click the link in any footer for more information and to change your preferences. | Accept only essential cookies | show/hide",
    "or click the link in any footer for more information and to change your preferences. | Accept only essential cookies | ECHA",
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
    # CHANGED 2026-09-08. Image-only deltas no longer alert at all - see the
    # comment in assess_image_change. These two cases previously expected an
    # alert and now expect silence. That is the accepted loss, asserted here
    # so it stays visible and deliberate rather than drifting.
    ("ACCEPTED LOSS: graphic-only publication, no text change",
     {"/old.png": "old"}, {"/old.png": "old", "/fees-2027.png": "new"}, False, False),
    ("same url, different bytes, no text change (CDN re-encode)",
     {"/fees.png": "v1"}, {"/fees.png": "v2"}, False, False),
    ("images corroborating a real text change still reported",
     {"/a.png": "1"}, {"/a.png": "1", "/new.png": "2"}, True, True),
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
    "image-only change is silent even when content genuinely differs",
    monitor.assess_image_change({"/a.png": "1"}, {"/a.png": "2"}, True, False)[0] is False,
)

check(
    "no baseline yet never alerts",
    monitor.assess_image_change({}, {"/a.png": "1"}, False, False)[0] is False,
)

# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 3. Failure classification. Every error string below is one this system has
#    actually produced, taken from run logs, not invented.
# ---------------------------------------------------------------------------

FAILURE_CASES = [
    # rows 42 and 193: nothing answers at any layer
    ("<urlopen error timed out> | playwright: playwright: Page.goto: Timeout "
     "35000ms exceeded | flaresolverr: flaresolverr unreachable: timed out", "connect"),
    ("<urlopen error timed out>", "connect"),
    ("HTTP Error 403: Forbidden", "http_client"),
    ("HTTP Error 404: Not Found", "http_client"),
    ("HTTP Error 500: Internal Server Error", "http_server"),
    ("empty response", "empty"),
    # a challenge served instead of the page. Checked before the HTTP status,
    # deliberately: a 403 carrying an interstitial is a challenge, not a
    # plain refusal, and calling it a challenge is the more useful truth.
    ("HTTP Error 403: Forbidden | playwright: blocked by bot-challenge "
     "interstitial (e.g. Cloudflare)", "challenge"),
    ("Robot Challenge Screen - checking the site connection security", "challenge"),
    # an unknown failure must look unknown rather than be filed under the
    # nearest available label
    ("some entirely novel failure nobody has seen", "other"),
    (None, "other"),
]

print("FAILURE CLASSIFICATION (classify_failure)")
for err, expected in FAILURE_CASES:
    got = monitor.classify_failure(err)
    label = (err or "(no error string)")[:46]
    check("%-48s -> %s" % (label, expected), got == expected, "got %r" % got)

check(
    "connect failures promote only after UNREACHABLE_AFTER_RUNS",
    monitor.UNREACHABLE_AFTER_RUNS >= 3,
    "- a lower threshold would promote a single flaky run",
)

print()

# ---------------------------------------------------------------------------
# 4. Order-insensitive comparison (2026-09-15).
#
#    ECHA row 98 returns the SAME rows in a DIFFERENT ORDER on every request
#    -- proven by loading it twice, seconds apart, and hashing the extracted
#    rows: identical total text length, 10 of 15 positions different. The old
#    concatenated-text comparison therefore reported a change every run, and
#    that is what got the Art. 13(5) battery list muted as volatile.
#
#    The strings below are the real first two rows of that page.
# ---------------------------------------------------------------------------
print("ORDER-INSENSITIVE COMPARISON (classify_change)")

_ECHA_A = "\n".join([
    "cadmium|231-152-8|7440-43-9|All batteries||As cadmium||View Details",
    "cadmium|231-152-8|7440-43-9|Portable batteries, whether or not incorporated",
    "Cadmium and cadmium compounds show/hide Cadmium lithopone yellow",
    "Mercury|231-106-7|7439-97-6|Batteries, whether or not incorporated",
])
_ECHA_B = "\n".join([
    "cadmium|231-152-8|7440-43-9|Portable batteries, whether or not incorporated",
    "Mercury|231-106-7|7439-97-6|Batteries, whether or not incorporated",
    "cadmium|231-152-8|7440-43-9|All batteries||As cadmium||View Details",
    "Cadmium and cadmium compounds show/hide Cadmium lithopone yellow",
])

_changed, _note = monitor.classify_change(_ECHA_A, _ECHA_B)
check("ECHA reshuffle is not a change", _changed is False,
      "got %r / %r" % (_changed, _note))

_changed, _note = monitor.classify_change(
    _ECHA_A,
    _ECHA_B + "\nLead|231-100-4|7439-92-1|All batteries||As lead||View Details",
)
check("a substance ADDED inside a reshuffle still alerts", _changed is True,
      "got %r" % (_changed,))
check("the note names what was added", "added:" in (_note or "")
      and "Lead" in (_note or ""), "note=%r" % (_note,))
check("the note does not blame a line that merely moved",
      "removed:" not in (_note or ""), "note=%r" % (_note,))

_changed, _note = monitor.classify_change(
    _ECHA_A, "\n".join(_ECHA_B.split("\n")[:-1])
)
check("a substance REMOVED still alerts", _changed is True, "got %r" % (_changed,))
check("the note says removed", "removed:" in (_note or ""), "note=%r" % (_note,))

_changed, _note = monitor.classify_change(_ECHA_A, _ECHA_A)
check("identical text is unchanged", _changed is False and _note is None,
      "got %r / %r" % (_changed, _note))

_changed, _note = monitor.classify_change(
    "Annex I entry\nAnnex I entry\nother line", "Annex I entry\nother line"
)
check("losing one of two identical lines still alerts", _changed is True,
      "got %r / %r" % (_changed, _note))

_changed, _note = monitor.classify_change(
    "Tariff 2026\nPaper 120 EUR/t\nGlass 95 EUR/t",
    "Tariff 2026\nPaper 135 EUR/t\nGlass 95 EUR/t",
)
check("a fee change alerts", _changed is True, "got %r" % (_changed,))
check("the note carries both the old and new figure",
      "135" in (_note or "") and "120" in (_note or ""), "note=%r" % (_note,))

# ACCEPTED LOSS -- asserted so it cannot drift silently. On a page where the
# ORDER ITSELF is the information, a pure reordering will not alert. This is
# the deliberate price of killing reshuffle noise on 213 sources. Reverse it
# only with a per-row opt-in, never by switching the whole comparison back.
_changed, _note = monitor.classify_change(
    "1. Alpha Refinery\n2. Beta Refinery\n3. Gamma Refinery",
    "1. Alpha Refinery\n3. Gamma Refinery\n2. Beta Refinery",
)
check("ACCEPTED LOSS: order-only change on a ranked list is silent",
      _changed is False, "got %r / %r" % (_changed, _note))


# ---------------------------------------------------------------------------
# 5. Non-English consent UI must not reach the diff (2026-09-15), and it must
#    go through the REAL extraction path, not a copy of the filter.
#    Strings are real: rows 105/148 (ECHA German locale) and row 14 (CONAI).
# ---------------------------------------------------------------------------
print("NON-ENGLISH CONSENT FILTER (extract_text_and_images)")

_DE_HTML = (
    "<html><body>"
    "<p>Diese Website verwendet Cookies. Mehr erfahren Sie auf unserer</p>"
    "<p>Cookies-Seite</p>"
    "<p>Alle Cookies akzeptieren</p>"
    "<p>Nur unbedingt notwendige Cookies akzeptieren</p>"
    "<p>Datenschutzerkl\u00e4rung</p>"
    "<p>Consent Selection</p>"
    "<p>Learn more about this provider</p>"
    "<p>Eintrag 23 des Anhangs XVII wurde 2026 ge\u00e4ndert</p>"
    "<p>prior informed consent procedure under Regulation (EU) No 649/2012</p>"
    "</body></html>"
).encode("utf-8")
_de_text, _ = monitor.extract_text_and_images(
    _DE_HTML, "text/html; charset=utf-8", "https://echa.europa.eu/de/test"
)
for _s in ("verwendet Cookies", "Cookies-Seite", "Alle Cookies akzeptieren",
           "notwendige Cookies", "Datenschutzerkl", "Consent Selection",
           "Learn more about this provider"):
    check("German/Cookiebot line dropped: %s" % _s, _s not in _de_text,
          "still present")
check("German regulatory substance survives the consent filter",
      "Anhangs XVII" in _de_text, "text=%r" % (_de_text[:200],))
check("PIC tripwire still holds (lawful use of 'consent')",
      "649/2012" in _de_text, "text=%r" % (_de_text[:200],))


# ---------------------------------------------------------------------------
# 6. Dead-link markers, non-English and bare "page not found" (2026-09-15).
#    Four dead pages were sitting in the OK bucket reporting "unchanged"
#    every run. Rows 172 and 14 were confirmed dead in a real browser.
# ---------------------------------------------------------------------------
print("DEAD-LINK MARKERS")

for _label, _txt in (
    ("row 172 Commission", "Page not found | Environment An official website"),
    ("row 14 CONAI", "Page Not Found - Conai\nConsent\nDetails"),
    ("row 35 Swedish", "Sidan hittades inte - Batteriretur"),
    ("row 11 German", "404-Fehler | stiftung elektro-altger\u00e4te register"),
):
    check("flags %s" % _label, monitor.is_dead_link(_txt) is not None,
          "not flagged")

# Tripwire: the conservative discipline must hold. A bare "404" in an address
# or a page that merely mentions not finding records is NOT a dead link.
for _alive in (
    "404 Main Street, Sacramento CA -- office address",
    "Restriction list under REACH Annex XVII, last updated 08 September 2026",
    "Search results: no records found for that CAS number",
    "The page you are viewing lists all notified bodies",
):
    check("does not flag healthy text: %s" % _alive[:40],
          monitor.is_dead_link(_alive) is None, "wrongly flagged")


if failures:
    print("FAILED: %d check(s) -> %s" % (len(failures), "; ".join(failures)))
    sys.exit(1)
print("All checks passed.")
