"""A supplier archived in Xero, met at publish (the owner's decisions doc C4, 2026-11-19): one Xero
merged into another follows it with no prompt; one archived with no merge is asked about (409 with
the supplier) and nothing is sent; the person maps it to another Xero contact now
(``POST /api/v1/entity-bill-contacts/<id>/xero-link/``), through accounts-api's relink. Minty never
restores anything in the customer's Xero."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from minty_db.models import Entity, XeroContact, XeroContactSync

from bills.models import Bill

ORG = "org-1"


@pytest.fixture
def live(test_entity):
    Entity.objects.filter(id=test_entity.id).update(xero_org_id=ORG, status="connected", setup_mode="xero")
    return test_entity


def _supplier(entity, name="Acme Ltd", guid="cid-acme"):
    return XeroContactSync.objects.create(entity_id=entity.id, name=name, xero_contact_id=guid, xero_org_id=ORG)


def _xero_says(contact):
    answer = MagicMock(status_code=200)
    answer.json.return_value = {"Contacts": [contact]}
    return answer


def _publish(api_client, auth_headers, bill):
    return api_client.post(f"/api/v1/bills/{bill.id}/publish/", **auth_headers)


@pytest.mark.django_db
def test_a_supplier_archived_with_no_merge_is_asked_about_and_nothing_is_sent(
    api_client, auth_headers, test_user_entity, draft_bill, live
):
    acme = _supplier(live)
    Bill.objects.filter(id=draft_bill.id).update(contact_id=acme.id)
    archived = _xero_says({"ContactID": "cid-acme", "ContactStatus": "ARCHIVED"})
    with (
        patch("bills.api.resolve_xero_access_token_for_entity", return_value="tok"),
        patch("bills.services.contact_service.requests.get", return_value=archived),
        patch("bills.api.publish_bill_to_xero", side_effect=AssertionError("sent")),
        patch("bills.services.contact_service.requests.post", side_effect=AssertionError("restored")),
    ):
        resp = _publish(api_client, auth_headers, draft_bill)

    assert resp.status_code == 409, resp.content
    assert resp.json() == {
        "detail": "Acme Ltd is archived in Xero. Restore it in Xero, or map it to another contact now.",
        "archived_contacts": [{"id": str(acme.id), "name": "Acme Ltd"}],
    }


@pytest.mark.django_db
def test_a_supplier_xero_merged_follows_it_with_no_prompt(api_client, auth_headers, test_user_entity, draft_bill,
                                                           live):
    acme = _supplier(live)
    Bill.objects.filter(id=draft_bill.id).update(contact_id=acme.id)
    merged = _xero_says({"ContactID": "cid-acme", "ContactStatus": "ARCHIVED", "MergedToContactID": "cid-kept"})
    relinked = []
    with (
        patch("bills.api.resolve_xero_access_token_for_entity", return_value="tok"),
        patch("bills.services.contact_service.requests.get", return_value=merged),
        patch("bills.services.contact_service.relink", side_effect=lambda *a, **k: relinked.append(a) or {}),
        patch("bills.api.publish_bill_to_xero",
              return_value={"bill": Bill.objects.get(id=draft_bill.id), "sync_id": None}) as sent,
    ):
        resp = _publish(api_client, auth_headers, draft_bill)

    assert resp.status_code == 200, resp.content
    assert relinked == [(str(live.id), acme.id, "cid-kept", "merged_in_xero")]
    sent.assert_called_once()  # the publish went on


@pytest.mark.django_db
def test_mapping_a_supplier_now_uses_one_of_the_companys_xero_contacts(
    api_client, auth_headers, test_user_entity, live
):
    acme = _supplier(live)
    XeroContact.objects.create(entity_id=live.id, xero_org_id=ORG, xero_guid="cid-beta", name="Beta Ltd",
                               status="ACTIVE")
    XeroContact.objects.create(entity_id=live.id, xero_org_id=ORG, xero_guid="cid-old", name="Old Ltd",
                               status="ARCHIVED")

    listed = api_client.get("/api/v1/entity-bill-contacts/xero/", **auth_headers)
    assert listed.json() == [{"xero_contact_id": "cid-beta", "name": "Beta Ltd"}]

    def link(guid):
        return api_client.post(f"/api/v1/entity-bill-contacts/{acme.id}/xero-link/",
                               data={"xero_contact_id": guid}, content_type="application/json", **auth_headers)

    assert link("cid-old").status_code == 422, "only an active Xero contact of the company's"
    asked = []
    with patch("bills.services.contact_service.relink",
               side_effect=lambda *a, **k: asked.append(a) or {"merged": False, "contact_id": str(acme.id)}):
        resp = link("cid-beta")
    assert (resp.status_code, resp.json()["message"]) == (200, "Acme Ltd now uses Beta Ltd in Xero.")
    assert asked[0][:4] == (str(live.id), acme.id, "cid-beta", "user")
