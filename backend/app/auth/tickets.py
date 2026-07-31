from typing import Protocol

from app.auth.verify import Principal, verify_token

# Long enough for a browser to receive the ticket and open the EventSource,
# short enough that a URL captured from a proxy log, a screenshot or a shared
# link is already dead. It is also the number the purge's residual-safety
# argument is extended by: see
# test_a_stream_ticket_cannot_outlive_the_organisation_it_names.
TICKET_TTL_SECONDS = 60


class TicketIssuer(Protocol):
    """The signer of stream tickets.

    Structurally the dev IdP, but named as a role because the two are not the
    same thing: bearer tokens are meant to come from Clerk one day, while a
    ticket is minted by *this* service for its own stream endpoint and would
    need a signing key of its own. M1 does not ship one — the dev IdP refuses
    to start under ENV=production — so this endpoint is demo-only until it does.
    """

    def mint(
        self, subject: str, org_id: str, ttl: int = ..., audience: str | None = ...
    ) -> str: ...


def _stream_audience(message_id: str) -> str:
    """The one place the audience string is built, so mint and verify cannot drift."""
    return f"stream:{message_id}"


def mint_stream_ticket(
    idp: TicketIssuer, subject: str, org_id: str, message_id: str
) -> str:
    """Short-lived credential for an EventSource GET.

    EventSource is GET-only and cannot set an Authorization header, so the
    credential has to travel in the URL. Making it 60 seconds long and binding
    its audience to one message keeps a leaked query string from being a
    general-purpose token: it is not accepted by any other endpoint, because
    every other endpoint verifies against the API audience instead.
    """
    return idp.mint(
        subject=subject,
        org_id=org_id,
        ttl=TICKET_TTL_SECONDS,
        audience=_stream_audience(message_id),
    )


def verify_stream_ticket(ticket: str, jwks: dict, issuer: str, message_id: str) -> Principal:
    """Accept a ticket only for the message it was minted for.

    The message id is not a hint taken from the token, it is supplied by the
    caller's own URL and used as the expected audience — so a ticket for
    message A presented on message B's stream fails audience verification
    rather than being accepted with the wrong scope.
    """
    return verify_token(ticket, jwks, issuer, audience=_stream_audience(message_id))
