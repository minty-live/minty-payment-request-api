"""Bills show the chart of accounts as it is NOW (the user's rule, 2026-11-24; ``services/chart_view.py``).

A line keeps its account by id and a bill its supplier by id; the detail, the list, the CSV and the
duplicate warning read the code, name and supplier from the chart - a rename, a matching or a merge
shows on every bill, published ones included. A merged-away account or contact shows the kept one.
Only a line with no id shows its stored text. The ``published_*`` record is never shown. A publish
sends the chart's now (the text copies are refreshed first); nothing is re-sent by a change.
"""

import json
from datetime import date
from decimal import Decimal

import pytest
from minty_db.models import AccountInfo, Entity, XeroContactSync

from bills.models import Bill, BillLine
from bills.services import chart_view
from bills.services.xero_publish_service import _build_xero_invoice_payload

pytestmark = pytest.mark.django_db


def _account(entity, code, name, **fields):
    return AccountInfo.objects.create(entity_id=entity.id, name=name, type="EXPENSE", xero_code=code, code=code,
                                      account_type="expense", status="ACTIVE", record_status="active",
                                      source="user", **fields)


def _contact(entity, name):
    return XeroContactSync.objects.create(entity_id=entity.id, name=name, source="user")


def _merge(gone, kept):
    AccountInfo.objects.filter(id=gone.id).update(merged_into_id=kept.id, record_status="archived",
                                                  status="ARCHIVED")


@pytest.fixture
def bill(test_entity, test_user_entity):
    rent = _account(test_entity, "469", "Rent")
    bakery = _contact(test_entity, "Corner Bakery")
    bill = Bill.objects.create(entity_id=test_entity.id, contact_name="Corner Bakery", contact_id=bakery.id,
                               status="draft", amount=Decimal("100"), reference="MBI-1",
                               invoice_date=date(2026, 11, 2), xero_account_code="469", currency_code="HKD")
    BillLine.objects.create(bill=bill, description="May rent", account_code="469", account_name="Rent",
                            line_amount=100)
    return {"bill": bill, "rent": rent, "bakery": bakery}


def _detail(client, headers, bill):
    resp = client.get(f"/api/v1/bills/{bill.id}", **headers)
    assert resp.status_code == 200, resp.content
    return resp.json()


def _list(client, headers, **query):
    resp = client.get("/api/v1/bills/", query, **headers)
    assert resp.status_code == 200, resp.content
    return resp.json()


def test_a_renamed_account_and_supplier_show_everywhere(api_client, auth_headers, bill):
    AccountInfo.objects.filter(id=bill["rent"].id).update(name="Office Rent", xero_code="470", code="470")
    XeroContactSync.objects.filter(id=bill["bakery"].id).update(name="Corner Bakery Ltd")
    Bill.objects.filter(id=bill["bill"].id).update(published="published")  # published ones too

    body = _detail(api_client, auth_headers, bill["bill"])
    line = body["line_items"][0]
    assert (line["account_id"], line["account_code"], line["account_name"]) == (
        str(bill["rent"].id), "470", "Office Rent")
    assert (body["xero_account_code"], body["contact"]) == ("470", "Corner Bakery Ltd")

    (row,) = _list(api_client, auth_headers)
    assert (row["xero_account_code"], row["contact"]) == ("470", "Corner Bakery Ltd")
    assert _list(api_client, auth_headers, search="Bakery Ltd"), "searched by the name shown"
    assert _list(api_client, auth_headers, contact="Bakery Ltd")


def test_a_merged_account_shows_the_kept_one(api_client, auth_headers, bill, test_entity):
    kept = _account(test_entity, "480", "Premises")
    _merge(bill["rent"], kept)

    line = _detail(api_client, auth_headers, bill["bill"])["line_items"][0]
    assert (line["account_id"], line["account_code"], line["account_name"]) == (str(kept.id), "480", "Premises")


def test_a_line_with_no_account_shows_its_text(api_client, auth_headers, bill):
    BillLine.objects.filter(bill=bill["bill"]).update(account_id=None, account_code="999", account_name="Old")
    body = _detail(api_client, auth_headers, bill["bill"])
    line = body["line_items"][0]
    assert (line["account_id"], line["account_code"], line["account_name"]) == (None, "999", "Old")
    assert body["xero_account_code"] == "999"


def test_the_published_record_is_never_shown(api_client, auth_headers, bill):
    BillLine.objects.filter(bill=bill["bill"]).update(published_account_code="111", published_account_name="Was")
    Bill.objects.filter(id=bill["bill"].id).update(published_contact_name="Was Bakery", published="published")
    body = _detail(api_client, auth_headers, bill["bill"])
    assert body["line_items"][0]["account_code"] == "469"
    assert body["contact"] == "Corner Bakery"


def test_the_csv_shows_the_chart_now(api_client, auth_headers, bill):
    AccountInfo.objects.filter(id=bill["rent"].id).update(xero_code="470", code="470")
    XeroContactSync.objects.filter(id=bill["bakery"].id).update(name="Corner Bakery Ltd")
    resp = api_client.post("/api/v1/bills/export.csv", data=json.dumps({"ids": [str(bill["bill"].id)]}),
                           content_type="application/json",
                           **auth_headers)
    assert resp.status_code == 200, resp.content
    text = b"".join(resp.streaming_content).decode() if hasattr(resp, "streaming_content") else resp.content.decode()
    row = text.splitlines()[1].split(",")
    assert row[1] == "Corner Bakery Ltd"
    assert row[7] == "470"


def test_a_line_saved_with_a_merged_code_keeps_its_account(bill, test_entity):
    kept = _account(test_entity, "480", "Premises")
    _merge(bill["rent"], kept)
    line = BillLine.objects.create(bill=bill["bill"], description="Again", account_code="469", line_amount=1)
    assert line.account_id == kept.id
    assert line.account_name == "Premises", "the name follows the account"


def test_a_publish_sends_the_chart_now(bill, test_entity):
    kept = _account(test_entity, "480", "Premises")
    _merge(bill["rent"], kept)
    XeroContactSync.objects.filter(id=bill["bakery"].id).update(name="Corner Bakery Ltd")

    chart_view.refresh_bill_text(bill["bill"].id)

    fresh = Bill.objects.get(id=bill["bill"].id)
    assert (fresh.xero_account_code, fresh.contact_name) == ("480", "Corner Bakery Ltd")
    line = fresh.line_items.get()
    assert (line.account_id, line.account_code, line.account_name) == (kept.id, "480", "Premises")
    entity = Entity.objects.get(id=test_entity.id)
    payload = _build_xero_invoice_payload(fresh, entity)
    assert payload["Invoices"][0]["LineItems"][0]["AccountCode"] == "480"


def test_the_duplicate_warning_names_the_supplier_now(api_client, auth_headers, bill):
    XeroContactSync.objects.filter(id=bill["bakery"].id).update(name="Corner Bakery Ltd")
    from bills.services.bill_service import DuplicateBill, ask_about_duplicates

    class _Data:
        confirm_duplicate = False
        xero_contact_id = None
        invoice_date = date(2026, 11, 2)
        amount = Decimal("100")

    other = Bill.objects.create(entity_id=bill["bill"].entity_id, contact_id=bill["bakery"].id, status="draft",
                                amount=Decimal("100"), invoice_date=date(2026, 11, 2))
    with pytest.raises(DuplicateBill) as caught:
        ask_about_duplicates(bill["bill"].entity_id, _Data(), other)
    assert caught.value.found[0]["contact"] == "Corner Bakery Ltd"
