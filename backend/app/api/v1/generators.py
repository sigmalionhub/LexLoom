"""`GET /api/v1/generators` — public, unauthenticated, and deliberately thin.

What lets the frontend disable the Gemini options without knowing in advance
whether `GEMINI_API_KEY` is configured: a clone with no key still runs, and
this is how it tells the selector which choices actually work right now — the
response lists only the generators that would succeed if picked, not all
three with a flag. With no key that is one element, `deterministic`, and that
is the whole point: a clone with zero API keys still lists a working chat, it
just does not offer a Gemini option to click.

Unauthenticated on purpose, for the same reason `GET /api/v1/status` is: it
takes no token, sees no organisation and reads no org-scoped table, so it
carries none of the residual-purge argument migration 0004 makes for a
credential — see the comment beside its entry in `EXPECTED_ROUTES`
(`backend/tests/test_demo_orgs.py`).

Labels live here rather than only in the frontend so a caller that never
loads the bundle (`curl`, `/docs`) still gets a human-readable answer, and so
there is exactly one place that decides what each choice is called.

A choice missing from this list is not the only defence against picking it
anyway (a stale bundle, a hand-written request): `POST .../messages` still
accepts it — it has no way to know a key is missing at the moment the
question is asked rather than answered — and `GET .../stream` refuses it with
`422 generator_unavailable` rather than a raw 500. See
`app/api/v1/conversations.py`.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.chat.providers import GeneratorChoice
from app.config import get_settings

router = APIRouter(prefix="/api/v1", tags=["generators"])


class GeneratorOption(BaseModel):
    value: GeneratorChoice
    label: str


class GeneratorsResponse(BaseModel):
    generators: list[GeneratorOption]


# Latency and "what kind of answer this is" captions belong to
# `frontend/components/panel/generator-selector.tsx`, which is what the
# visitor actually reads and which changes with each fresh measurement
# without a backend deploy. This label is only the name.
_LABELS: dict[GeneratorChoice, str] = {
    GeneratorChoice.DETERMINISTIC: "Deterministic",
    GeneratorChoice.FLASH_LITE: "Flash-Lite",
    GeneratorChoice.FLASH: "Flash",
}


@router.get("/generators", response_model=GeneratorsResponse)
async def list_generators() -> GeneratorsResponse:
    has_key = bool(get_settings().gemini_api_key)
    # `DETERMINISTIC` needs no key and is always first; the two Gemini choices
    # need the same key, so they are both present or both absent together.
    choices = [GeneratorChoice.DETERMINISTIC]
    if has_key:
        choices += [GeneratorChoice.FLASH_LITE, GeneratorChoice.FLASH]
    return GeneratorsResponse(
        generators=[GeneratorOption(value=choice, label=_LABELS[choice]) for choice in choices]
    )
