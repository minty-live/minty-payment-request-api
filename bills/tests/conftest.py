import uuid

import jwt as pyjwt
import pytest
import requests
from django.conf import settings
from django.test import Client
from django.utils import timezone as django_tz
from minty_db.models import CountryInfo, Entity, User, UserEntity, UserToken

from bills.models import Attachment, Bill, BillAttachment, CurrencyInfo


@pytest.fixture(autouse=True)
def _block_real_token_service_calls(monkeypatch):
    """Stop the suite reaching a live Flask app over HTTP.

    `resolve_xero_access_token_for_entity` POSTs to the Flask token service whenever the
    stored token is expired or absent. Unblocked, the suite talks to whatever is
    listening on ACCOUNTS_API_URL — on a developer machine that is a running Minty against
    a real database, and that endpoint can spend a single-use Xero refresh token.

    Tests that exercise the token service patch `requests.post` themselves; those
    patches are applied inside the test and take precedence over this one.
    """

    def _blocked(*args, **kwargs):
        raise requests.ConnectionError("real HTTP blocked in tests")

    monkeypatch.setattr("bills.services.xero_token_service.requests.post", _blocked)
    # The list and settings screens start accounts-api's sync on a thread (bills/services/xero_sync.py).
    monkeypatch.setattr("bills.services.xero_sync.requests.post", _blocked)


@pytest.fixture(autouse=True)
def xero_organisation(monkeypatch):
    """Xero's ``GET /Organisation`` for the publish (its lock dates and base currency, 2026-11-24),
    answered here so no test reaches Xero: ``{"BaseCurrency": "HKD"}`` by default - set a key to
    change the answer, ``fail`` to an exception or a status code to make it fail. Any other GET goes
    to whatever ``requests.get`` is (a test's own patch inside the test still wins)."""
    from unittest.mock import MagicMock

    state = {"Name": "Acme Ltd", "BaseCurrency": "HKD", "fail": None}
    real_get = requests.get

    def answer(url, *args, **kwargs):
        if not str(url).endswith("/Organisation"):
            return real_get(url, *args, **kwargs)
        if isinstance(state["fail"], Exception):
            raise state["fail"]
        resp = MagicMock()
        resp.status_code = state["fail"] or 200
        org = {k: v for k, v in state.items() if k != "fail" and v is not None}
        org["OrganisationID"] = (kwargs.get("headers") or {}).get("Xero-Tenant-Id")
        resp.json.return_value = {"Organisations": [org]}
        resp.text = "{}"
        return resp

    monkeypatch.setattr("bills.services.xero_publish_service.requests.get", answer)
    return state


@pytest.fixture(autouse=True)
def _registry_rows(request):
    """The country / currency rows the entity fixtures point at.

    ``entities.country_code`` and ``entities.currency_id`` are real FKs on Postgres; SQLite
    (tables from the models) never enforced them, which is how fixtures got away with
    ``country_code="HK"`` and a made-up currency id for so long. Runs only for tests that
    touch the database.
    """
    if "db" not in request.fixturenames and not request.node.get_closest_marker("django_db"):
        return
    request.getfixturevalue("db")
    hkd, _ = CurrencyInfo.objects.get_or_create(
        id="11111111-1111-1111-1111-111111111111",
        defaults={"currency_code": "HKD", "currency_name": "Hong Kong Dollar", "symbol": "HK$",
                  "decimal_places": 2, "is_active": True},
    )
    CountryInfo.objects.get_or_create(
        country_code="HK",
        defaults={"alpha3_code": "HKG", "country_name_en": "Hong Kong", "currency_id": hkd.id,
                  "is_active": True, "display_order": 1},
    )


@pytest.fixture
def api_client():
    return Client()


@pytest.fixture
def test_user(db):
    return User.objects.create(
        id="96dc83ad-f6b8-5b7d-aa0a-26f5f76f8197",
        email="test@minty.com",
        password="hashed_pw",
        first_name="Test",
        last_name="User",
        username="testuser",
        system_role="normal",
    )


def give_xero_token(user, access_token="access-token", *, expires_in=1800, obtained_at=None,
                    refresh_token="refresh-token", id_token=None):
    """Store a Xero bundle for ``user`` the way Minty does: one ``user_token`` row.

    Minty always writes the access token together with its expiry pair, so a token with
    unknown expiry never occurs in practice; ``_token_expired`` treats one as expired.
    """
    row, _ = UserToken.objects.get_or_create(
        user=user, defaults={"id": uuid.uuid4()}
    )
    row.access_token = access_token
    row.access_token_expires_in = expires_in
    row.access_token_obtained_at = obtained_at or django_tz.now()
    row.refresh_token = refresh_token
    row.id_token = id_token
    row.save()
    return row


def tiny_image(fmt: str = "JPEG") -> bytes:
    """A real 8x8 image: photos are decoded when stored (the user, 2026-11-26). This small, a PNG
    and a quality-50 JPEG are kept byte for byte - re-saving would not make them smaller."""
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (8, 8), (30, 140, 200)).save(buf, format=fmt, **({"quality": 50} if fmt == "JPEG" else {}))
    return buf.getvalue()


@pytest.fixture
def test_entity(db):
    return Entity.objects.create(
        id="9df620d9-a0f3-5f42-a200-04f17c73b009",
        name="Test Entity",
        country_code="HK",
        currency_id="11111111-1111-1111-1111-111111111111",
        status="disconnected",
    )


@pytest.fixture
def test_user_entity(db, test_user, test_entity):
    return UserEntity.objects.create(
        user=test_user,
        entity=test_entity,
        role="admin",
        approved=True,
    )


@pytest.fixture
def auth_token(test_user, test_entity):
    return pyjwt.encode(
        {"user_id": test_user.id, "entity_id": test_entity.id},
        settings.SECRET_KEY,
        algorithm="HS256",
    )


@pytest.fixture
def auth_headers(auth_token, test_entity):
    return {
        "HTTP_AUTHORIZATION": f"Bearer {auth_token}",
        "HTTP_X_ENTITY_ID": test_entity.id,
    }


@pytest.fixture
def draft_bill(db, test_user, test_entity):
    return Bill.objects.create(
        entity_id=test_entity.id,
        contact="Vendor A",
        status="draft",
        amount=100.00,
        description="Test bill",
        uploaded_by=test_user.id,
    )


def attach_invoice(bill, name="invoice.pdf"):
    """Give ``bill`` one invoice attachment - a bill can't be published without one."""
    attachment = Attachment.objects.create(
        original_name=name,
        stored_name="stored.pdf",
        file_path="attachments/test/stored.pdf",
        mime_type="application/pdf",
        file_size=1024,
        file_extension="pdf",
        storage_provider="s3",
        uploaded_by=bill.uploaded_by,
    )
    return BillAttachment.objects.create(
        bill=bill,
        attachment=attachment,
        attachment_role="invoice",
        created_by=bill.uploaded_by,
    )


@pytest.fixture
def draft_bill_with_attachment(db, draft_bill):
    attach_invoice(draft_bill)
    return draft_bill


#: ai_policy.model_prices (the user, 2026-11-24): USD per 1M tokens, every model the tests name.
MODEL_PRICES = {
    "gemini-3.8-flash": {"input": 0.75, "output": 3.75, "cached_input": 0.075},
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50, "cached_input": 0.03},
}


@pytest.fixture
def ai_policy(db, test_entity):
    """minty-db's one ``ai_policy`` row (seed_catalogue.sql writes it in a real database): one
    model, no backup, no extra caps, an AI trial large enough for a test's many reads (the grant
    rules themselves are minty-petty-cash-api's tests), and the invoice reader on with the columns'
    own limits (owner, 2026-11-20: the switch is the row's). A test changes it as an operator would.
    The company started its Payment Request trial yesterday, so its AI trial runs (the user,
    2026-11-27: the AI trial starts with the company's first module trial)."""
    from datetime import timedelta

    from django.utils import timezone
    from minty_db.models import AiPolicy, EntityModuleSubscription

    started = timezone.now() - timedelta(days=1)
    row = EntityModuleSubscription.objects.create(
        entity_id=test_entity.id, function_code="PAYMENT_REQUEST", phase="trial",
        trial_end=started + timedelta(days=30), app_access_until=started + timedelta(days=30))
    EntityModuleSubscription.objects.filter(id=row.id).update(created_at=started)
    return AiPolicy.objects.create(id=1, primary_model="gemini-3.8-flash", backup_model=None,
                                   default_grant={"tokens": 1000000}, limit_rules=[], invoice_reader_on=True,
                                   model_prices=MODEL_PRICES)
