import unicodedata
from typing import Annotated

from fastapi import APIRouter, Request
from pydantic import BaseModel, StringConstraints, field_validator

from app.auth.demo import DEMO_TOKEN_TTL_SECONDS, provision_demo_org

router = APIRouter(tags=["auth"])

# Categories with no appearance this API can count on: control, format (where
# U+200B ZERO WIDTH SPACE lives), unassigned, private use, the three separators,
# and the two mark classes — a label consisting only of combining marks has no
# base character to combine with, so it renders as nothing or as a lone dotted
# circle.
#
# `Cn` earns its place: it is where every invisible codepoint that Unicode has
# not assigned a character to lives, including the whole
# Default_Ignorable_Code_Point reserve — U+2065, U+FFF0..U+FFF8, U+E0002,
# U+E0080 — and the noncharacters U+FDD0..U+FDEF. Each of those was accepted
# before, producing an organisation whose name is one invisible character long.
# The cost is that `Cn` follows the Unicode version this interpreter ships: a
# codepoint assigned in a newer standard reads as unassigned here, so a label
# made *entirely* of brand-new characters is rejected until Python catches up.
# For a demo tenant label that is the right way round.
#
# `Co` is the weaker member and is here for a different reason: private-use
# codepoints have no assigned appearance at all, only whatever a private
# agreement between a font and its author gives them. A label made entirely of
# them is not a name any other reader can see, and most fonts draw nothing or a
# tofu box. This is the one category in the set whose members might render for
# somebody, and that is why the heading above says "no appearance this API can
# count on" rather than "cannot carry a glyph".
#
# Surrogates (`Cs`) are deliberately *not* here. A lone surrogate cannot be
# encoded as UTF-8, so no HTTP request body can carry one and the category could
# never fire; listing it would be decoration, and a rule that nothing can
# exercise is a rule nobody can trust.
_UNRENDERABLE_CATEGORIES = frozenset({"Cc", "Cf", "Cn", "Co", "Zs", "Zl", "Zp", "Mn", "Me"})

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

        Exactly what is tested: every character's general category is in
        `_UNRENDERABLE_CATEGORIES`, or the character is in `_BLANK_CODEPOINTS`.
        Nothing wider is claimed, and every entry in both is exercised by a
        fixture in `test_demo_login_rejects_a_label_with_nothing_visible_in_it`.

        **This is not a rendering oracle.** Unicode does name most of the
        deliberately invisible codepoints — the property is
        `Default_Ignorable_Code_Point` — but `unicodedata` does not expose it,
        and it would not settle the question on its own in either direction:
        U+3164 HANGUL FILLER carries the property, U+2800 BRAILLE PATTERN BLANK
        does not, and both draw nothing. Whether a codepoint shows up is finally
        a question about a font, and the API has no font.

        The named list exists because the category test alone is not enough,
        twice over. `strip_whitespace` strips what `str.isspace()` calls
        whitespace and U+200B ZERO WIDTH SPACE is not that, so it survives
        stripping and passes `min_length=1`; and U+3164 HANGUL FILLER is a
        perfectly ordinary `Lo` letter that happens to draw nothing, which is
        why it is the character people use for blank usernames. `Lo` and `So`
        hold both real characters and blanks, so no category rule separates
        them and the residue has to be named.
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
