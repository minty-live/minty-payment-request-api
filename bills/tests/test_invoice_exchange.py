# ruff: noqa: F811 - pytest fixtures imported from the reader suite are used as parameters
"""An invoice in another currency (owner, 2026-11-19): the reader converts its amount at
Frankfurter's rate (``minty_shared.fx``; the rule's own cases are minty-shared-py's
``tests/test_fx.py``) - not rounded to the company's places first - and a saved bill keeps how it
was converted: all six details or none, and only while its amount is the converted one. No
network: the rate is a stand-in."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from minty_db.models import CurrencyInfo, Entity
from minty_shared import fx

from bills.models import Bill
from bills.tests.test_invoice_reader import FakeResponse, _extract, _reply, lists, model  # noqa: F401

pytestmark = pytest.mark.django_db

RATE_DAY = date(2026, 10, 19)


@pytest.fixture
def rates(monkeypatch):
    """Frankfurter, stood in: 7.846 a unit for whatever day is asked."""
    asked: list[tuple[str, str, date]] = []
    answer = {"rate": fx.Rate(rate=Decimal("7.846"), date=RATE_DAY)}

    def fake(base, quote, on):
        asked.append((base, quote, on))
        return answer["rate"]

    monkeypatch.setattr(fx, "frankfurter_rate", fake)
    return {"asked": asked, "answer": answer}


def _read(api_client, auth_headers, model, **reply):
    model.answer = FakeResponse(_reply(**reply))
    return _extract(api_client, auth_headers).json()["suggestions"]


@pytest.mark.usefixtures("test_user_entity", "lists")
class TestTheRead:
    def test_an_invoice_in_another_currency_comes_back_converted_on_its_own_day(self, api_client, auth_headers,
                                                                                 model, rates):
        s = _read(api_client, auth_headers, model, currency="USD", amount=12.34, date="2026-10-20")
        assert rates["asked"] == [("USD", "HKD", date(2026, 10, 20))]
        assert s["amount"]["value"] == "96.82" and s["amount"]["applied"] is True
        assert s["exchange"] == {
            "original_currency": "USD", "original_amount": "12.34", "rate": "7.846",
            "rate_date": RATE_DAY.isoformat(), "date_basis": "receipt", "source": "Frankfurter",
            "asked_date": "2026-10-20",  # the invoice's day; the stand-in answers for the 19th
        }

    def test_with_no_invoice_date_the_day_it_is_entered_is_used(self, api_client, auth_headers, model, rates):
        s = _read(api_client, auth_headers, model, currency="USD", amount=12.34, date="")
        [(_, _, on)] = rates["asked"]
        assert on >= date(2026, 11, 19) and s["exchange"]["date_basis"] == "report_date"

    def test_no_rate_leaves_the_amount_empty_and_names_the_currency(self, api_client, auth_headers, model, rates):
        rates["answer"]["rate"] = None
        s = _read(api_client, auth_headers, model, currency="TWD", amount=300)
        assert s["amount"]["value"] == "" and s["exchange_failed"] == {"currency": "TWD"}

    def test_the_company_s_own_currency_asks_for_no_rate(self, api_client, auth_headers, model, rates):
        s = _read(api_client, auth_headers, model)
        assert rates["asked"] == [] and "exchange" not in s and s["amount"]["value"] == "1200.50"

    def test_a_foreign_amount_is_converted_before_any_rounding(self, api_client, auth_headers, model, rates,
                                                               test_entity):
        jpy = CurrencyInfo.objects.create(id="33333333-3333-3333-3333-333333333333", currency_code="JPY",
                                          currency_name="Yen", symbol="¥", decimal_places=0, is_active=True)
        Entity.objects.filter(id=test_entity.id).update(currency_id=jpy.id)
        rates["answer"]["rate"] = fx.Rate(rate=Decimal("156.5"), date=RATE_DAY)
        s = _read(api_client, auth_headers, model, currency="USD", amount=12.34)
        assert s["exchange"]["original_amount"] == "12.34"  # not 12: the yen's places are the result's
        assert s["amount"]["value"] == "1931"


# --- the saved bill --------------------------------------------------------------------------------

SIX = {
    "original_currency": "USD", "original_amount": "12.34", "exchange_rate": "7.846",
    "exchange_rate_date": RATE_DAY.isoformat(), "exchange_rate_basis": "receipt",
    "exchange_rate_source": "Frankfurter",
}


def _post(api_client, auth_headers, path, body):
    return api_client.post(path, data=json.dumps(body), content_type="application/json", **auth_headers)


def _kept(bill_id):
    row = Bill.objects.get(id=bill_id)
    return {name: getattr(row, name) for name in fx.COLUMNS}


@pytest.mark.usefixtures("test_user_entity")
class TestTheSavedBill:
    def test_a_draft_keeps_how_its_amount_was_converted(self, api_client, auth_headers):
        resp = _post(api_client, auth_headers, "/api/v1/bills/draft/",
                     {"contact": "Vendor", "amount": "96.82", "exchange": SIX})
        assert resp.status_code == 201, resp.content
        assert _kept(resp.json()["id"]) == {
            "original_currency": "USD", "original_amount": Decimal("12.34"), "exchange_rate": Decimal("7.846"),
            "exchange_rate_date": RATE_DAY, "exchange_rate_basis": "receipt", "exchange_rate_source": "Frankfurter",
        }

    def test_without_them_a_bill_keeps_none(self, api_client, auth_headers):
        resp = _post(api_client, auth_headers, "/api/v1/bills/draft/", {"contact": "Vendor", "amount": "96.82"})
        assert set(_kept(resp.json()["id"]).values()) == {None}

    @pytest.mark.parametrize("path", ["/api/v1/bills/draft/", "/api/v1/bills/", "/api/v1/bills/submit/"])
    def test_details_that_do_not_give_the_amount_are_refused(self, api_client, auth_headers, path):
        resp = _post(api_client, auth_headers, path,
                     {"contact": "Vendor", "amount": "100.00", "exchange": SIX, "invoice_date": "2026-11-01",
                      "due_date": "2026-11-30", "description": "Chairs",
                      "line_items": [{"description": "Chairs", "quantity": "1", "unit_amount": "100.00",
                                      "line_amount": "100.00", "account_code": "400"}]})
        assert resp.status_code == 422
        assert fx.exchange_refused("invoice") in json.dumps(resp.json())
        assert not Bill.objects.exists()

    def test_changing_the_amount_drops_the_conversion_and_a_new_one_replaces_it(self, api_client, auth_headers):
        bill_id = _post(api_client, auth_headers, "/api/v1/bills/draft/",
                        {"contact": "Vendor", "amount": "96.82", "exchange": SIX}).json()["id"]

        def put(body):
            return api_client.put(f"/api/v1/bills/{bill_id}/draft", data=json.dumps(body),
                                  content_type="application/json", **auth_headers)

        assert put({"description": "Chairs"}).status_code == 200
        assert _kept(bill_id)["original_currency"] == "USD"  # the amount didn't change: it stays
        assert put({"amount": "100.00"}).status_code == 200
        assert set(_kept(bill_id).values()) == {None}
        assert put({"amount": "96.82", "exchange": SIX}).status_code == 200
        assert _kept(bill_id)["exchange_rate"] == Decimal("7.846")
