#!/usr/bin/env bash
#
# Rebuilds the committed NIST SP 800-63B .docx from the publisher's own HTML.
#
# Run this offline, by a human, when the source needs refreshing. LibreOffice is
# a BUILD TOOL, not a dependency of this service: nothing at runtime and nothing
# in CI invokes it, and it must not be installed on the deploy box.
#
# The committed .docx is OUR artefact, not NIST's — NIST publishes this document
# as HTML and PDF, never as .docx. That is why `catalogue.py` carries a
# `conversion` field and two digests, and why this script exists rather than a
# line in a README: the artefact has to be reproducible by someone who does not
# trust us.
#
# Three stages, each answering a defect measured on the raw conversion:
#
#   preprocess  The page is not the document. It carries one <nav>, one
#               <footer>, a `page-switch` div ("View this document as: …"), and
#               one <p> holding the page BUILD TIMESTAMP — and that timestamp is
#               body text, so leaving it in means every republish shifts every
#               chunk_ordinal the catalogue promises to hold still. Anchors are
#               unwrapped rather than removed: that keeps every word of the text
#               while producing a .docx with ZERO external relationships (the
#               raw conversion had 67, three of them dead file:/// image
#               targets). <img> and <script> go for the same reason.
#
#               The bibliography STAYS. It sits mid-document — <h1
#               id="references"> is followed by appendices A–E — and removing it
#               would leave a hundred-odd dangling [FIPS140]-style references
#               and make the catalogue title, which names the full publication,
#               an overstatement. Its retrieval noise is ~5% of the text and is
#               recorded in the catalogue rather than silently accepted.
#
#               Nothing is inlined into headings. NIST's sub-section numbers
#               ("3.1.1") exist nowhere in this HTML: `data-section` carries
#               only the top-level chapter (all three AAL "Reauthentication"
#               headings share '2'), and <nav> carries 13 chapter markers. The
#               locator is built from the heading HIERARCHY instead, which the
#               format gives us and which needs no inference.
#
#   convert     LibreOffice maps <h1>..<h4> onto real Word Heading styles. That
#               is the whole reason a .docx is worth having beside a PDF: the
#               structure is given by the format instead of inferred by the
#               GDPR-specific boundary regexes in parse.py.
#
#   sanitise    Drops word/comments.xml (LibreOffice injects the scraped <head>
#               into it), docProps/custom.xml (harvested page metadata), and the
#               build fingerprints. Then rezips with fixed entry timestamps, so
#               the FILE digest is reproducible too — an earlier version of this
#               work claimed byte-reproducibility was impossible; it is not, we
#               simply had to pay for it with `touch`.
#
# Usage: backend/scripts/build_docx_source.sh [output-dir]
set -euo pipefail

SRC_URL="https://pages.nist.gov/800-63-4/sp800-63b.html"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${1:-$HERE/../app/ingest/sources}"
OUT_DOCX="$OUT_DIR/nist-sp-800-63b.docx"
OUT_HTML="$OUT_DIR/nist-sp-800-63b.src.html"

command -v soffice >/dev/null || { echo "soffice not found; this is a build-time tool" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
export HOME="$WORK"

echo "==> fetching $SRC_URL"
curl -fsSL "$SRC_URL" -o "$WORK/raw.html"

echo "==> preprocessing"
python3 - "$WORK/raw.html" "$WORK/pre.html" <<'PY'
import re, sys
raw, out = sys.argv[1], sys.argv[2]
h = open(raw, encoding="utf-8").read()

def drop(pattern, label):
    global h
    h, n = re.subn(pattern, "", h, flags=re.S | re.I)
    print(f"    {label:<24} {n}")

drop(r"<nav\b.*?</nav>", "nav")
drop(r"<footer\b.*?</footer>", "footer")
drop(r'<div[^>]*class="[^"]*page-switch[^"]*"[^>]*>.*?</div>', "div.page-switch")
drop(r"<p>\s*(Mon|Tue|Wed|Thu|Fri|Sat|Sun),[^<]*</p>", "build timestamp <p>")
drop(r"<script\b.*?</script>", "script")
drop(r"<style\b.*?</style>", "style")
drop(r"<img\b[^>]*>", "img")

# Unwrap, do not delete: every word of link text is part of the document.
h, n = re.subn(r"<a\b[^>]*>(.*?)</a>", r"\1", h, flags=re.S | re.I)
print(f"    {'anchors unwrapped':<24} {n}")

# Invariants the rest of the pipeline depends on. Fail loudly here rather than
# produce a subtly wrong corpus: chunk_docx.py assumes every chunk sits under a
# heading, which is only true because nothing survives ahead of the first one.
headings = re.findall(r"<h[1-6][^>]*\bid=\"([^\"]+)\"", h)
assert len(headings) == 145, f"expected 145 headings, found {len(headings)}"
body = re.search(r"<body\b[^>]*>(.*)</body>", h, re.S)
assert body, "no <body>"
lead = re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", body.group(1).split("<h1", 1)[0])).strip()
assert lead == "", f"text survives before the first heading: {lead[:80]!r}"
assert '<h1 id="references"' in h, "bibliography was removed; it must stay"

open(out, "w", encoding="utf-8").write(h)
print(f"    {'bytes':<24} {len(h)}")
PY

cp "$WORK/pre.html" "$OUT_HTML"

echo "==> converting"
soffice --headless --norestore --nolockcheck --nodefault \
  -env:UserInstallation="file://$WORK/lo" \
  --convert-to 'docx:MS Word 2007 XML' --outdir "$WORK" "$WORK/pre.html" >/dev/null 2>&1

echo "==> sanitising"
rm -rf "$WORK/x" && mkdir "$WORK/x"
( cd "$WORK/x" && unzip -qo "$WORK/pre.docx" )
python3 - "$WORK/x" <<'PY'
import os, re, sys
root = sys.argv[1]

def edit(rel, fn):
    p = os.path.join(root, rel)
    if not os.path.exists(p):
        return
    text = open(p, encoding="utf-8").read()
    open(p, "w", encoding="utf-8").write(fn(text))

# Comment markers, then the comments part itself and every reference to it.
def strip_comments(d):
    for pat in (r"<w:commentRangeStart[^>]*/>", r"<w:commentRangeEnd[^>]*/>",
                r"<w:r>(?:(?!</w:r>).)*?<w:commentReference[^>]*/>.*?</w:r>",
                r"<w:commentReference[^>]*/>"):
        d = re.sub(pat, "", d, flags=re.S)
    return d

edit("word/document.xml", strip_comments)
for junk in ("word/comments.xml", "docProps/custom.xml"):
    p = os.path.join(root, junk)
    if os.path.exists(p):
        os.remove(p)
        name = os.path.basename(junk)
        edit("word/_rels/document.xml.rels",
             lambda t, n=name: re.sub(rf"<Relationship[^>]*{n}[^>]*/>", "", t))
        edit("[Content_Types].xml",
             lambda t, n=name: re.sub(rf"<Override[^>]*{n}[^>]*/>", "", t))
        edit("_rels/.rels", lambda t, n=name: re.sub(rf"<Relationship[^>]*{n}[^>]*/>", "", t))

edit("docProps/app.xml",
     lambda t: re.sub(r"<Application>[^<]*</Application>",
                      "<Application>LexLoom build script</Application>", t))
edit("word/theme/theme1.xml",
     lambda t: re.sub(r'name="LibreOffice"', 'name="Office Theme"', t))

leftovers = []
for dirpath, _, names in os.walk(root):
    for n in names:
        blob = open(os.path.join(dirpath, n), encoding="utf-8", errors="ignore").read()
        for bad in ("vbaProject", "oleObject", "DDEAUTO", "INCLUDETEXT",
                    "<!DOCTYPE", "<!ENTITY", 'TargetMode="External"', "LibreOffice"):
            if bad in blob:
                leftovers.append(f"{n}: {bad}")
assert not leftovers, f"sanitise missed: {leftovers}"
print("    package clean")
PY

# Fixed timestamps: a zip records an mtime per entry, and without this the file
# digest differs between two runs over byte-identical input.
find "$WORK/x" -exec touch -t 202001010000.00 {} +
rm -f "$OUT_DOCX"
( cd "$WORK/x" && zip -q -X -D -r "$OUT_DOCX" '[Content_Types].xml' _rels docProps word )

echo
echo "wrote $OUT_HTML"
echo "wrote $OUT_DOCX"
echo "sha256(file)         $(sha256sum "$OUT_DOCX" | cut -d' ' -f1)"
echo "sha256(document.xml) $(sha256sum "$WORK/x/word/document.xml" | cut -d' ' -f1)"
soffice --version | head -1
