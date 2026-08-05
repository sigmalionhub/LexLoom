"""The committed schema is the schema this code serves.

`frontend/openapi.json` is a committed artefact, refreshed deliberately by
`npm run snapshot:openapi` and turned into `frontend/lib/api-types.ts` by
`npm run gen:types`. The reasoning is in `frontend/scripts/snapshot-openapi.mjs`
and it is sound: generating from a URL at build time would make a clean clone
and CI unbuildable, because neither has the stack up.

What that design does not defend against is the snapshot going **stale**, and
nothing did until this test. The refresh script fetches from
`http://127.0.0.1:8100/openapi.json` — a *running container*, which on a
developer's machine is whatever image was last built. Refresh after changing a
route and before rebuilding, and the script cheerfully writes a snapshot of the
old code over the new one, `gen:types` produces types for routes that no longer
exist, and `npx tsc --noEmit` passes because the types and the frontend agree
with each other while both disagree with the server.

That is not hypothetical: it happened while the second catalogue source was
being added, and the new route was silently missing from the snapshot. Hence
this test. The failure message says what to run, because the fix is mechanical
and the diff is 40 KB of JSON nobody should have to read to find out.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.main import create_app

SNAPSHOT = Path(__file__).resolve().parents[2] / "frontend" / "openapi.json"

_REFRESH = (
    "The committed frontend/openapi.json no longer matches this application.\n"
    "Regenerate it from THIS code, not from a running container:\n"
    "\n"
    "  cd backend && python -c 'import json;from app.main import create_app;"
    "print(json.dumps(create_app().openapi(), indent=2))' > ../frontend/openapi.json\n"
    "  cd ../frontend && npm run gen:types\n"
    "\n"
    "`npm run snapshot:openapi` fetches from port 8100 and will write a "
    "snapshot of whatever image is running there, which may be older than the "
    "code you just changed."
)


def test_the_committed_schema_matches_the_application() -> None:
    served = create_app().openapi()
    committed = json.loads(SNAPSHOT.read_text())
    assert committed == served, _REFRESH


def test_every_served_route_is_in_the_committed_schema() -> None:
    """The same thing, but with a message that names what moved.

    Comparing whole documents tells you *that* they differ; comparing the path
    sets tells you *which* route appeared or vanished, which is the first thing
    anyone wants to know and the last thing a 40 KB JSON diff makes obvious.
    """
    served = set(create_app().openapi()["paths"])
    committed = set(json.loads(SNAPSHOT.read_text())["paths"])
    assert served == committed, (
        f"only in the application: {sorted(served - committed)}; "
        f"only in the snapshot: {sorted(committed - served)}"
    )
