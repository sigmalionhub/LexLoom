/**
 * Refreshes the committed `openapi.json` snapshot from a running API.
 *
 * The types in `lib/api-types.ts` are generated from that file, never from a
 * URL. Generating from `http://localhost:8100/openapi.json` as part of the
 * build would mean a clean clone cannot build and CI cannot build, because
 * neither has the stack up — the schema would become a network dependency of
 * `npm run build`. So the schema is a committed artefact, refreshed
 * deliberately by running this script against a live stack, and the refresh
 * shows up in a diff where it can be reviewed.
 *
 * Usage: docker compose up -d --build api && npm run snapshot:openapi && npm run gen:types
 *
 * `--build` is not decoration. This fetches from a RUNNING container, so a
 * refresh against a stale image writes a snapshot of the old code over the new
 * one, and `gen:types` then produces types that agree with the frontend while
 * both disagree with the server. `backend/tests/test_openapi_snapshot.py` is
 * what catches that; it exists because the mistake was made rather than
 * imagined.
 */
import { writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const source = process.env.OPENAPI_URL ?? 'http://127.0.0.1:8100/openapi.json';
const target = join(dirname(fileURLToPath(import.meta.url)), '..', 'openapi.json');

const response = await fetch(source);
if (!response.ok) {
  console.error(`GET ${source} -> ${response.status}. Is the stack up?`);
  process.exit(1);
}

// Re-serialised with a stable 2-space indent so that a refresh produces a
// readable diff rather than one 40 KB line.
const schema = await response.json();
writeFileSync(target, `${JSON.stringify(schema, null, 2)}\n`);
console.log(`wrote ${target} from ${source} (openapi ${schema.openapi})`);
