"""Load the public seed corpus into its permanent organisation.

Run it from `backend/`:

    python scripts/seed_corpus.py

or, against the Compose stack:

    docker compose exec -T api python scripts/seed_corpus.py

Idempotent — re-running replaces the organisation's chunks rather than adding a
second copy — so it is safe on every deployment.

Deliberately thin. The work is `app.chat.corpus.seed_public_corpus`, in the
application package, because the same corpus is copied into every demo tenant at
login and a second loader would be a second answer to "what is in the corpus".
The sources are open — the GDPR as published in the Official Journal — because a
public repository in the legal domain must not ship someone else's contracts;
`backend/seed/gdpr_excerpts.json` carries the citation and the terms of reuse.
"""

import asyncio
import pathlib
import sys

# `backend/` on the path so `app` imports when this is run as a script from
# anywhere. The package is imported from the source tree in every environment
# this project has — the image copies it, development installs it editable —
# so there is no installed copy to fall back on.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.chat.corpus import PUBLIC_CORPUS_ORG_ID, seed_public_corpus  # noqa: E402
from app.ingest.preload import seed_public_catalogue_corpus  # noqa: E402


async def main() -> None:
    # Order matters and is not incidental: `seed_public_corpus` deletes every
    # chunk this organisation holds before writing the excerpts, so the
    # catalogue load has to follow it. The reverse order leaves the template
    # holding the seed alone, every tenant born without a document, and
    # `provision_demo_org` raising on the first visitor.
    seeded = await seed_public_corpus()
    print(f"Seeded {seeded} excerpt chunks into organisation {PUBLIC_CORPUS_ORG_ID}.")

    # Minutes, not seconds: each source is parsed and embedded here so that no
    # visitor's login ever pays for it.
    for source_key, chunks in (await seed_public_catalogue_corpus()).items():
        print(f"Seeded {chunks} chunks of {source_key}.")


if __name__ == "__main__":
    asyncio.run(main())
