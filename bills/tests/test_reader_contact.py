"""A supplier the invoice reader read and the company does not have (the user, 2026-11-23): added
automatically when the bill is saved - draft, create, submit or edit - never by the read
(``bill_service.take_reader_contact``). Only the reader's signed name counts (``new_contact_token``);
a chosen ``xero_contact_id`` wins."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest
from django.conf import settings
from minty_db.models import XeroContactSync
from minty_shared import ai

from bills.models import Bill
from bills.tests.conftest import give_xero_token

GONE = "The suggested supplier has expired. Choose a supplier from the list or add it."


def _token(entity_id, name="Corner Bakery"):
    return ai.new_supplier_token(settings.SECRET_KEY, entity_id, name)


def _body(token, **over):
    body = {"contact": "Corner Bakery", "xero_contact_id": "", "new_contact_token": token,
            "amount": "120.00", "invoice_date": "2026-11-20", "due_date": "2026-12-20",
            "description": "Bread",
            "line_items": [{"description": "Bread", "quantity": 1, "unit_amount": "120.00",
                            "line_amount": "120.00", "account_code": "429"}]}
    body.update(over)
    return body


def _post(api_client, auth_headers, path, body):
    return api_client.post(f"/api/v1/bills/{path}", data=body, content_type="application/json", **auth_headers)


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["draft/", "", "submit/"], ids=["draft", "create", "submit"])
def test_a_read_supplier_is_added_with_the_bill_offline(api_client, auth_headers, test_user_entity, test_entity,
                                                        path):
    resp = _post(api_client, auth_headers, path, _body(_token(test_entity.id)))

    assert resp.status_code == 201, resp.content
    row = XeroContactSync.objects.get(entity_id=test_entity.id, name="Corner Bakery")
    assert (row.xero_contact_id, row.source) == (None, "user")
    bill = Bill.objects.get(id=resp.json()["id"])
    assert (bill.xero_contact_id, bill.contact) == (str(row.id), "Corner Bakery")


@pytest.mark.django_db
def test_a_read_supplier_is_made_in_xero_when_live(api_client, auth_headers, test_user, test_user_entity,
                                                   test_entity):
    test_entity.status, test_entity.xero_org_id = "connected", "org-abc"
    test_entity.save()
    give_xero_token(test_user, "fake-xero-token")
    search = MagicMock(status_code=200)
    search.json.return_value = {"Contacts": []}
    made = MagicMock(status_code=200)
    made.json.return_value = {"Contacts": [{"ContactID": "new-cid-001", "Name": "Corner Bakery"}]}

    with patch("bills.services.contact_service.requests.get", return_value=search), \
            patch("bills.services.contact_service.requests.post", return_value=made) as post:
        resp = _post(api_client, auth_headers, "draft/", _body(_token(test_entity.id)))

    assert resp.status_code == 201, resp.content
    assert post.call_args[1]["json"] == {
        "Contacts": [{"Name": "Corner Bakery", "IsCustomer": False, "IsSupplier": True}]
    }
    assert Bill.objects.get(id=resp.json()["id"]).xero_contact_id == "new-cid-001"
    assert XeroContactSync.objects.filter(entity_id=test_entity.id, xero_contact_id="new-cid-001").exists()


@pytest.mark.django_db
def test_a_read_supplier_the_company_has_is_that_one(api_client, auth_headers, test_user_entity, test_entity):
    held = XeroContactSync.objects.create(entity_id=test_entity.id, name="Corner Bakery", xero_contact_id="cid-cb")

    resp = _post(api_client, auth_headers, "draft/", _body(_token(test_entity.id, "corner  bakery")))

    assert resp.status_code == 201, resp.content
    assert XeroContactSync.objects.filter(entity_id=test_entity.id).count() == 1
    assert Bill.objects.get(id=resp.json()["id"]).xero_contact_id == held.xero_contact_id


@pytest.mark.django_db
def test_a_cashier_adds_a_read_supplier_too(api_client, auth_headers, test_user_entity, test_entity):
    test_user_entity.role = "cashier"
    test_user_entity.save(update_fields=["role"])

    resp = _post(api_client, auth_headers, "draft/", _body(_token(test_entity.id)))

    assert resp.status_code == 201, resp.content
    assert XeroContactSync.objects.filter(entity_id=test_entity.id, name="Corner Bakery").exists()


@pytest.mark.django_db
@pytest.mark.parametrize("which", ["altered", "another-company"])
def test_a_bad_reader_token_is_a_422_and_nothing_is_saved(api_client, auth_headers, test_user_entity, test_entity,
                                                          which):
    token = "forged.token" if which == "altered" else _token(uuid.uuid4())

    resp = _post(api_client, auth_headers, "draft/", _body(token))

    assert resp.status_code == 422
    assert resp.json()["detail"] == GONE
    assert not XeroContactSync.objects.exists() and not Bill.objects.exists()


@pytest.mark.django_db
def test_a_submit_refused_for_another_reason_adds_no_supplier(api_client, auth_headers, test_user_entity,
                                                              test_entity):
    resp = _post(api_client, auth_headers, "submit/", _body(_token(test_entity.id), amount="0"))

    assert resp.status_code == 422
    assert not XeroContactSync.objects.exists()


@pytest.mark.django_db
def test_a_chosen_supplier_wins_over_a_reader_token(api_client, auth_headers, test_user_entity, test_entity):
    XeroContactSync.objects.create(entity_id=test_entity.id, name="Acme Ltd", xero_contact_id="cid-acme")

    resp = _post(api_client, auth_headers, "draft/",
                 _body(_token(test_entity.id), contact="Acme Ltd", xero_contact_id="cid-acme"))

    assert resp.status_code == 201, resp.content
    assert not XeroContactSync.objects.filter(name="Corner Bakery").exists()
    assert Bill.objects.get(id=resp.json()["id"]).xero_contact_id == "cid-acme"


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["draft", ""], ids=["edit-draft", "edit"])
def test_an_edit_takes_a_read_supplier_too(api_client, auth_headers, test_user, test_user_entity, test_entity,
                                           path):
    bill = Bill.objects.create(entity_id=test_entity.id, contact="", amount="10.00", status="draft",
                               uploaded_by=test_user.id)
    url = f"/api/v1/bills/{bill.id}/{path}" if path else f"/api/v1/bills/{bill.id}"

    resp = api_client.put(url, data={"contact": "Corner Bakery", "new_contact_token": _token(test_entity.id)},
                          content_type="application/json", **auth_headers)

    assert resp.status_code == 200, resp.content
    row = XeroContactSync.objects.get(entity_id=test_entity.id, name="Corner Bakery")
    bill.refresh_from_db()
    assert (bill.xero_contact_id, bill.contact) == (str(row.id), "Corner Bakery")
