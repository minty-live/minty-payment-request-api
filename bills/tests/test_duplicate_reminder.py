"""The duplicate reminder (the owner's decisions doc DUP1, 2026-11-19): just before a bill is saved,
another bill with the same date, supplier and exactly the same amount is asked about (409 with the
``duplicates``); ``confirm_duplicate`` saves it anyway - it never blocks. A voided bill, the bill
itself and a half-filled draft never count."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from minty_db.models import XeroContactSync

from bills.models import Bill

DAY = "2026-11-19"


@pytest.fixture
def acme(test_entity):
    return XeroContactSync.objects.create(entity_id=test_entity.id, name="Acme Ltd", xero_contact_id="cid-acme")


@pytest.fixture
def first(test_entity, test_user, acme):
    bill = Bill.objects.create(entity_id=test_entity.id, contact="Acme Ltd", amount=Decimal("120.00"),
                               invoice_date=date(2026, 11, 19), status="submitted", uploaded_by=test_user.id,
                               reference="INV-1")
    Bill.objects.filter(id=bill.id).update(contact_id=acme.id)
    return Bill.objects.get(id=bill.id)


def _draft(api_client, auth_headers, **body):
    return api_client.post("/api/v1/bills/draft/", data={"contact": "Acme Ltd", "xero_contact_id": "cid-acme",
                                                          "amount": "120.00", "invoice_date": DAY, **body},
                           content_type="application/json", **auth_headers)


@pytest.mark.django_db
def test_the_same_date_supplier_and_amount_is_asked_about_then_saved_when_told(
    api_client, auth_headers, test_user_entity, first
):
    asked = _draft(api_client, auth_headers)
    assert asked.status_code == 409, asked.content
    body = asked.json()
    assert body["detail"] == ("This looks like a bill you already have: the same date, supplier and amount. "
                              "Save it anyway?")
    assert [(d["reference"], d["contact"]) for d in body["duplicates"]] == [("INV-1", "Acme Ltd")]
    assert Bill.objects.count() == 1, "nothing saved yet"

    told = _draft(api_client, auth_headers, confirm_duplicate=True)
    assert told.status_code == 201, told.content
    assert Bill.objects.count() == 2


@pytest.mark.django_db
def test_another_amount_date_or_a_voided_bill_is_no_duplicate(api_client, auth_headers, test_user_entity, first):
    assert _draft(api_client, auth_headers, amount="120.01").status_code == 201
    assert _draft(api_client, auth_headers, invoice_date="2026-11-18").status_code == 201
    Bill.objects.filter(id=first.id).update(status="void")
    assert _draft(api_client, auth_headers).status_code == 201


@pytest.mark.django_db
def test_a_half_filled_draft_is_never_asked_about(api_client, auth_headers, test_user_entity, first):
    resp = api_client.post("/api/v1/bills/draft/", data={"contact": "Acme Ltd", "xero_contact_id": "cid-acme",
                                                         "amount": "120.00"},
                           content_type="application/json", **auth_headers)
    assert resp.status_code == 201, resp.content


@pytest.mark.django_db
def test_an_edit_that_makes_a_duplicate_is_asked_about_and_a_bill_is_never_its_own(
    api_client, auth_headers, test_user_entity, first, test_entity, test_user, acme
):
    other = Bill.objects.create(entity_id=test_entity.id, contact="Acme Ltd", amount=Decimal("5.00"),
                                invoice_date=date(2026, 11, 19), status="draft", uploaded_by=test_user.id)
    Bill.objects.filter(id=other.id).update(contact_id=acme.id)
    asked = api_client.put(f"/api/v1/bills/{other.id}/draft", data={"amount": "120.00"},
                           content_type="application/json", **auth_headers)
    assert asked.status_code == 409 and asked.json()["duplicates"]
    assert Bill.objects.get(id=other.id).amount == Decimal("5.00")
    itself = api_client.put(f"/api/v1/bills/{first.id}/draft", data={"amount": "120.00"},
                            content_type="application/json", **auth_headers)
    assert itself.status_code != 409


@pytest.mark.django_db
def test_a_status_only_change_is_never_asked_about(api_client, auth_headers, test_user_entity, first, test_entity,
                                                   test_user, acme):
    """The app moves a bill's status back by itself after a payment is deleted: not a save of its
    details, so never asked about, even when it matches another bill."""
    twin = Bill.objects.create(entity_id=test_entity.id, contact="Acme Ltd", amount=Decimal("120.00"),
                               invoice_date=date(2026, 11, 19), status="draft", uploaded_by=test_user.id)
    Bill.objects.filter(id=twin.id).update(contact_id=acme.id)
    resp = api_client.put(f"/api/v1/bills/{twin.id}/draft", data={"description": "Only words"},
                          content_type="application/json", **auth_headers)
    assert resp.status_code == 200, resp.content
