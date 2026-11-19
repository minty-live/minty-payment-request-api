"""Session endpoints for the billing module.

There is no token refresh (Phase 9, owner's S2, 2026-11-19): the web keeps the 30-minute hand-off
token from minty-accounts-api and, on a 401, hands the person over again - so Payment Request
signs out when Minty does (``core/auth.py``, the sign-in check).
"""

import logging

from ninja import Router, Schema

from core.auth import SelfBearerAuth

logger = logging.getLogger("minty-api")

session_router = Router(tags=["Session"])


class BillingSessionOut(Schema):
    role: str
    entity_id: str


class XeroStatusOut(Schema):
    connected: bool
    #: The company keeps its books in Minty with no Xero (Xero optional, 2026-11-10): nothing to
    #: publish, and its bill codes are its own. The web hides Publish and shows the codes.
    minty_only: bool = False


class EntitlementsOut(Schema):
    petty_cash_enabled: bool
    billing_enabled: bool


class LogoutOut(Schema):
    detail: str


class EntityCurrencyOut(Schema):
    # ISO 4217 code of the entity's selected currency ("" when unset).
    currency_code: str
    # Its currency_info.decimal_places (JPY 0); 2 when no currency is set.
    decimal_places: int


@session_router.get(
    "/session",
    response={200: BillingSessionOut},
    summary="Current billing session (entity role from DB)",
)
def billing_session(request):
    """Return the caller's role for X-Entity-Id / token entity — same source as permission checks."""
    logger.info(
        "Billing session context user_id=%s entity_id=%s role=%s",
        getattr(request.auth_user, "id", None),
        getattr(request, "entity_id", None),
        getattr(request, "entity_role", None),
    )
    return BillingSessionOut(
        role=request.entity_role,
        entity_id=request.entity_id,
    )


@session_router.get(
    "/entity-currency",
    response={200: EntityCurrencyOut},
    summary="ISO currency code and decimal places of the current entity (entities.currency_id)",
)
def entity_currency(request):
    """Resolve the JWT entity's selected currency to its ISO code and decimal places.

    entities.currency_id is a uuid FK into pettycashv3.currency_info(id); the
    UI renders money amounts with the currency_code and sizes amount inputs to
    decimal_places; "" means none is set (decimal_places is then 2).
    """
    from core.entity_currency import entity_currency as resolve

    entity_id = str(getattr(request, "entity_id", "") or "")
    code, places = resolve(entity_id)
    return EntityCurrencyOut(currency_code=code, decimal_places=places)


@session_router.get(
    "/entitlements",
    auth=SelfBearerAuth(),
    response={200: EntitlementsOut},
    summary="Live module entitlements for the current entity (DB-fresh, not JWT)",
)
def entitlements(request):
    """Return the current module entitlements for the JWT's entity.

    Reads straight from ``entity_function_map`` (via core.entitlements) so the
    Module 2 frontend can refresh its module-visibility state without going
    back through Module 1 to re-mint a JWT. The Bearer auth gives us
    ``request.entity_id`` — we don't take it from a query string because that
    would let a caller probe entitlements for entities they don't belong to.
    """
    from core.entitlements import get_module_claims

    claims = get_module_claims(str(request.entity_id or ""))
    return EntitlementsOut(
        petty_cash_enabled=claims["petty_cash_enabled"],
        billing_enabled=claims["billing_enabled"],
    )


@session_router.get(
    "/xero-status",
    response={200: XeroStatusOut},
    summary="Whether the current entity has a live Xero connection",
)
def xero_status(request):
    """Return whether the token's company is live on Xero (``xero_connection_live``).

    The company's connection, not the person's: the default ``BearerAuth`` requires a role
    on the entity. The frontend hides the Xero-fed settings (the account codes) when it is
    not live. Read from the database - no live call to the Xero API.
    """
    from bills.services.books import minty_on_its_own
    from bills.services.xero_token_service import xero_connection_live

    connected = xero_connection_live(str(request.entity_id), str(request.auth_user.id))
    return XeroStatusOut(connected=connected, minty_only=minty_on_its_own(request.entity_id))


@session_router.post(
    "/logout",
    response={200: LogoutOut},
    summary="Invalidate the current billing session",
)
def logout(request):
    """Acknowledge a logout request and drop the user's sign-in presence.

    Billing auth cookies are set client-side (not HttpOnly) and are cleared
    by the frontend via clearAuth() in lib/auth.ts. This endpoint exists so
    the frontend has a uniform logout call and can confirm the server received
    the intent. Calling it twice is safe.

    Clearing ``signed_in_at`` is what takes the user off Minty's Settings > Users
    list — that list shows who is signed in, and the billing profile's Log out is
    one of the ways to stop being.

    ``last_seen_at`` is stamped rather than left alone, and that is load-bearing
    rather than cosmetic. Minty's Flask session survives this call on purpose (Log
    out returns the browser to the entity list, not the login page), so the user
    keeps making authenticated requests afterwards. Minty reads a cleared
    ``signed_in_at`` beside a SET ``last_seen_at`` as "signed out, leave them off",
    but beside a BLANK one as "never stamped, adopt them" — so writing both columns
    is what stops the very next Minty page from undoing this logout. See
    ``refresh_presence`` in Minty's services/user_presence.py.
    """
    user_id = getattr(request.auth_user, "id", None)
    logger.info("Logout requested user_id=%s", user_id)
    if user_id:
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo

            from minty_db.models import User

            # Naive Hong Kong local, NOT django.utils.timezone.now(). These columns
            # are TIMESTAMP WITHOUT TIME ZONE and Minty writes HK local into them
            # (models/db.py `tz`), while this project runs USE_TZ=True on UTC —
            # so timezone.now() would land eight hours behind everything Minty
            # wrote, in the same column. Mirrors user_presence.now() exactly.
            stamp = datetime.now(ZoneInfo("Asia/Hong_Kong")).replace(tzinfo=None)
            User.objects.filter(id=str(user_id)).update(
                signed_in_at=None, last_seen_at=stamp
            )
        except Exception:
            # Presence is decoration on a user list. A logout that reached us is
            # a logout, whether or not we managed to record it.
            logger.exception("Failed to clear sign-in presence user_id=%s", user_id)
    return LogoutOut(detail="logged out")
