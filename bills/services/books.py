"""Who keeps the books (Xero optional, minty-db v0.5's EXPAND, 2026-11-10; Minty
docs/modernisation/xero_optional_database.md). A company that chose "Use Minty independently" in
onboarding has ``entities.setup_mode = 'minty'`` and no Xero organisation: its suppliers are its own,
and its bills are never published. Connected later, Xero's rules apply again."""

import logging
from collections.abc import Callable

import requests
from django.utils import timezone
from minty_db.models import AccountInfo, Entity, XeroAccount

logger = logging.getLogger("minty-api")

NOT_IN_XERO = "This company keeps its books in Minty and isn't connected to Xero, so there's nowhere to publish to."
#: Connecting Xero later (Settings Phase 2, 2026-11-11): signed in, the accounts not matched yet.
STILL_MATCHING = "Finish matching your accounts to Xero first (Settings, under Integration), then publish."
#: Phase 3 of the matching (owner, 2026-11-12): an open matching for these holds every publish; a
#: change of the matches (``change``) doesn't - the company stays connected meanwhile.
HOLDING_MATCHES = ("first", "new_accounts", "new_org")
#: A stale match: an account the bill uses isn't in the company's current organisation, or Xero
#: archived it. (Suppliers aren't held: the publish re-creates one Xero doesn't know, as it always has.)
STALE_ACCOUNT = ("{name} ({code}) isn't matched to your Xero organisation. Match it in Settings, under "
                 "Integration, then publish again.")
#: The owner's decisions doc A3 (2026-11-19): an account Xero archived or removed is mapped again.
ARCHIVED_ACCOUNT = ("{name} ({code}) is archived in Xero. Please check it in Xero, then map this account to the one "
                    "you want to use (Settings, under General), and publish again.")
#: Asking Xero about an archived account holds the Publish click: short.
ACCOUNT_TIMEOUT = (5, 15)


def minty_on_its_own(entity_id) -> bool:
    """Minty keeps the books (``setup_mode = 'minty'``) - also while an organisation recorded
    mid-matching waits for Finish (Settings Phase 2, 2026-11-11), which sets ``'xero'``."""
    return Entity.objects.filter(id=entity_id, setup_mode="minty").exists()


def publish_refusal(entity_id, bill, token: Callable[[], str | None] | None = None) -> str | None:
    """Why this bill can't go to Xero, from who keeps the books - or None. A company still in Minty
    (no organisation, or one recorded but its accounts not matched yet) stays in Minty; once Xero is
    connected and matched, any bill can go, however old (owner, 2026-11-18). ``token`` gives a Xero
    access token, asked for only when an account reads archived (``_restored_in_xero``)."""
    entity = Entity.objects.filter(id=entity_id).values(
        "setup_mode", "xero_org_id", "xero_matching").first()
    if entity is None:
        return None
    if entity["setup_mode"] == "minty":
        return STILL_MATCHING if entity["xero_org_id"] else NOT_IN_XERO
    if entity["xero_matching"] in HOLDING_MATCHES:
        return STILL_MATCHING
    stale = stale_matches(entity_id, entity["xero_org_id"], bill, token)
    return "\n".join(stale) if stale else None


def _restored_in_xero(entity_id, org: str, token: Callable[[], str | None], account: AccountInfo) -> bool:
    """Ask Xero (``GET /Accounts/{id}``) whether an account the copies hold as archived is ACTIVE again.
    The copies change only when accounts-api's sync runs (opening Settings), so an account restored in
    Xero since then would hold every publish (2026-11-18). ACTIVE: the copy and ``account_info`` read
    ACTIVE again, as the next sync would make them. Any other answer, no token, or Xero not answering
    keeps the stored word (logged)."""
    from bills.services.xero_publish_service import XERO_API_BASE

    guid = account.xero_account_id
    try:
        access_token = token()
    except Exception as exc:  # the token service's own refusal; the stored word answers
        logger.warning("Publish: no Xero token to check archived account %s: %s", guid, exc)
        return False
    if not access_token:
        logger.warning("Publish: no Xero token to check archived account %s", guid)
        return False
    try:
        response = requests.get(f"{XERO_API_BASE}/Accounts/{guid}",
                                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json",
                                         "Xero-Tenant-Id": org}, timeout=ACCOUNT_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("Publish: Xero unreachable checking archived account %s: %s", guid, exc)
        return False
    if response.status_code != 200:
        logger.warning("Publish: Xero answered %s checking archived account %s", response.status_code, guid)
        return False
    try:
        found = (response.json() or {}).get("Accounts") or []
    except ValueError:
        logger.warning("Publish: Xero's answer for archived account %s was not JSON", guid)
        return False
    if not found or str(found[0].get("Status") or "").strip().upper() != "ACTIVE":
        return False
    XeroAccount.objects.filter(entity_id=entity_id, xero_org_id=org, xero_guid=guid).update(
        status="ACTIVE", synced_at=timezone.now())
    AccountInfo.objects.filter(id=account.id).exclude(status="ACTIVE").update(status="ACTIVE")
    logger.info("Publish: account %s (%s) is active in Xero again; its stored status updated", account.id, guid)
    return True


def stale_matches(entity_id, org, bill, token: Callable[[], str | None] | None = None) -> list[str]:
    """The bill's accounts that aren't in the company's current Xero organisation, or that Xero
    archived, one sentence each (Phase 3 of the matching, owner 2026-11-12). An account is stale with
    no Xero id, or with one the organisation's copies don't hold (when the company has copies at all:
    older data without them isn't held). The supplier isn't held: a bill whose last publish went to
    another organisation is published fresh into the current one, as it always has."""
    from bills.models import BillLine

    org = str(org or "")
    lines = list(BillLine.objects.filter(bill_id=bill.id).values_list("account_id", "account_code"))
    account_ids = {a for a, _code in lines if a}
    # a line saved with only a code while its account was already gone from Xero has no account_id
    # (``BillLine.save`` picks only active accounts) - it is held too (A3)
    codes = {c.strip() for a, c in lines if not a and c and c.strip()}
    if codes:
        account_ids |= set(AccountInfo.objects.filter(
            entity_id=entity_id, xero_code__in=codes, xero_account_id__isnull=False, merged_into__isnull=True)
            .exclude(status="ACTIVE").values_list("id", flat=True))
    copies = dict(XeroAccount.objects.filter(entity_id=entity_id, xero_org_id=org)
                  .values_list("xero_guid", "status"))
    out = []
    for account in sorted(AccountInfo.objects.filter(id__in=account_ids), key=lambda a: (a.xero_code or "", a.name)):
        named = {"name": account.name, "code": account.xero_code or account.code or "-"}
        if not account.xero_account_id or (copies and account.xero_account_id not in copies):
            out.append(STALE_ACCOUNT.format(**named))
        elif copies and copies[account.xero_account_id] != "ACTIVE" and not (
                token and _restored_in_xero(entity_id, org, token, account)):
            out.append(ARCHIVED_ACCOUNT.format(**named))
    return out
