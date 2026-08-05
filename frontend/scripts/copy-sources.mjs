/**
 * Put the catalogue's source documents where the static export can serve them.
 *
 * The page offers each document for download so a reader can check the text a
 * citation came from. The files must be handed out by **nginx, from the export**
 * — never by the API. That is not tidiness: the API runs `--workers 1`, and
 * concurrent SSE streams are the bounded resource the whole of `app/limits.py`
 * exists to protect. A 982 KB response through that worker competes with the
 * one thing this demo is about.
 *
 * They are copied rather than committed twice. The files already live in
 * `backend/app/ingest/sources/`, which is where the parser reads them from, and
 * a second copy under `public/` would be a second answer to "which file did the
 * demo actually parse". `public/sources/` is git-ignored for that reason.
 *
 * Run from `prebuild`, so `npm run build` cannot forget it and produce an export
 * whose download links 404.
 */

import { copyFileSync, existsSync, mkdirSync, readdirSync, rmSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const FROM = join(dirname(ROOT), 'backend', 'app', 'ingest', 'sources');
const INTO = join(ROOT, 'public', 'sources');

/**
 * The basenames the catalogue names, spelled once.
 *
 * `nist-sp-800-63b.src.html` sits in the same directory and is deliberately not
 * here: it is the preprocessed input the `.docx` was built from, useful to a
 * test that needs ground truth from the publisher and not to a visitor. Copying
 * the directory wholesale would ship it, so the list is explicit.
 */
const FILES = ['gdpr-full.pdf', 'nist-sp-800-63b.docx'];

if (!existsSync(FROM)) {
  console.error(`No source documents at ${FROM}.`);
  process.exit(1);
}

// Rebuilt rather than merged, so a renamed source does not leave its old file
// behind to be served forever.
rmSync(INTO, { recursive: true, force: true });
mkdirSync(INTO, { recursive: true });

let total = 0;
for (const name of FILES) {
  const from = join(FROM, name);
  if (!existsSync(from)) {
    console.error(`Missing source document: ${from}`);
    process.exit(1);
  }
  copyFileSync(from, join(INTO, name));
  total += statSync(from).size;
}

const copied = readdirSync(INTO);
if (copied.length !== FILES.length) {
  console.error(`Expected ${FILES.length} files in ${INTO}, found ${copied.length}.`);
  process.exit(1);
}

console.log(`sources: ${copied.length} files, ${(total / 1024).toFixed(0)} KiB -> public/sources/`);
