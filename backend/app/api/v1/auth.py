import unicodedata
from typing import Annotated

from fastapi import APIRouter, Request
from pydantic import BaseModel, StringConstraints, field_validator

from app.auth.demo import DEMO_TOKEN_TTL_SECONDS, provision_demo_org

router = APIRouter(tags=["auth"])

# Categories that cannot carry a glyph on their own: control, format (where
# U+200B ZERO WIDTH SPACE lives), the three separators, and the two mark classes
# — a label consisting only of combining marks has no base character to combine
# with, so it renders as nothing or as a lone dotted circle.
_UNRENDERABLE_CATEGORIES = frozenset({"Cc", "Cf", "Zs", "Zl", "Zp", "Mn", "Me"})

# Codepoints in otherwise ordinary categories that still render blank. `Lo`
# holds both real letters and these fillers, and `So` holds both symbols and the
# empty Braille cell, so no category test separates them — they have to be named.
# This list is the known offenders, not a complete one; see the docstring below.
_BLANK_CODEPOINTS = frozenset(
    {
        "\u115f",  # HANGUL CHOSEONG FILLER (Lo)
        "\u1160",  # HANGUL JUNGSEONG FILLER (Lo)
        "\u3164",  # HANGUL FILLER (Lo) - the canonical blank-name character
        "\uffa0",  # HALFWIDTH HANGUL FILLER (Lo)
        "\u2800",  # BRAILLE PATTERN BLANK (So)
    }
)


class DemoLoginRequest(BaseModel):
    # Stripped before the length check, not after: `max_length` alone accepts
    # "" and "   ", and an organisation whose name is blank is indistinguishable
    # in the UI from one whose name failed to load.
    label: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
    ]

    @field_validator("label")
    @classmethod
    def _must_not_be_entirely_blank(cls, value: str) -> str:
        """Reject labels made only of characters that cannot show anything.

        Precisely: characters in the control, format, separator and mark
        categories, plus a named list of codepoints that sit in ordinary
        categories and still render blank. **This is not a rendering oracle**, and
        cannot be one — Unicode has no "does this show up" property, and `Lo` and
        `So` each contain both real characters and blanks, so no positive
        category set decides it either. Codepoints outside the list can still
        produce an empty-looking name; what this rules out is the known ways.

        The list exists because the category test alone is not enough, twice
        over. `strip_whitespace` strips what `str.isspace()` calls whitespace and
        U+200B ZERO WIDTH SPACE is not that, so it survives stripping and passes
        `min_length=1`; and U+3164 HANGUL FILLER is a perfectly ordinary `Lo`
        letter that happens to draw nothing, which is why it is the character
        people use for blank usernames.
        """
        if all(
            unicodedata.category(char) in _UNRENDERABLE_CATEGORIES
            or char in _BLANK_CODEPOINTS
            for char in value
        ):
            raise ValueError("label must contain at least one character that renders")
        return value


@router.get("/.well-known/jwks.json")
async def jwks(request: Request) -> dict:
    return request.app.state.dev_idp.jwks()


@router.post("/api/v1/auth/demo-login")
async def demo_login(payload: DemoLoginRequest, request: Request) -> dict[str, object]:
    """Mint a throwaway organisation for this visitor.

    Shared demo orgs would put one visitor's upload in front of the next, and a
    project whose headline claim is per-org isolation cannot seat every visitor
    in the same tenant. An ephemeral org makes the demo demonstrate the thesis:
    two browsers, two orgs, mutually invisible data.

    `expires_in` describes the token, so it is the same constant the token was
    minted with — not a second literal that agrees with `mint()`'s default by
    coincidence and stops agreeing the moment either is tuned.
    """
    org = await provision_demo_org(payload.label)
    token = request.app.state.dev_idp.mint(
        subject=org.external_id, org_id=org.id, ttl=DEMO_TOKEN_TTL_SECONDS
    )
    return {
        "access_token": token,
        "org_id": org.id,
        "org_name": org.name,
        "expires_in": DEMO_TOKEN_TTL_SECONDS,
    }
