"""Reading an invoice to fill the new-bill form: ``POST /bills/extract/`` (``bills/api.py``) and
``bills/services/invoice_reader.py``, on ``minty_shared.ai`` (owner, 2026-11-04). The engine's
own cases (retries, timeouts, 429/5xx, cost, the tier tripwire) are minty-shared-py's
``tests/test_ai.py``.

No network: the model client is a stand-in - a test that reached Google would test Google."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from minty_db.models import AiGrant, AiPolicy, AiRead, Entity, UserEntity, XeroContactSync
from minty_shared import ai, ai_prompt, fx
from minty_shared.django import ai_allowance

from bills.models import EntityBillAccountXero
from bills.services import invoice_reader

pytestmark = pytest.mark.django_db

PDF = b"%PDF-1.4 an invoice"
ORG = "org-current"
URL = "/api/v1/bills/extract/"
STATUS_URL = "/api/v1/bills/extract/status/"


def _reply(**over) -> str:
    """A reply to the owner's simple prompt plus the due date (2026-11-20), by the short ids sent
    (``lists``: acc_01 = 310 Cost of Goods Sold, acc_02 = 400 Advertising; con_01 = con-abc ABC
    Printing, con_02 = con-taxi Taxi Co)."""
    body = {
        "supplier_contact_id": "con_01", "supplier_name": "ABC Printing", "account_id": "acc_02",
        "amount": 1200.5, "currency": "HKD", "date": "2026-10-20", "description": "Flyer printing",
        "due_date": "2026-11-19",
    }
    body.update(over)
    return json.dumps(body)


class FakeUsage:
    prompt_token_count = 5000
    candidates_token_count = 300
    thoughts_token_count = 400
    cached_content_token_count = 0


class FakeResponse:
    """A ``generate_content`` reply: one candidate holding ``text``, ended by ``finish``."""

    def __init__(self, text=None, finish="STOP"):
        self.response_id = "response-1"
        self.usage_metadata = FakeUsage()
        self.prompt_feedback = None
        parts = [SimpleNamespace(text=text, thought=None)] if text is not None else []
        self.candidates = [SimpleNamespace(finish_reason=finish, content=SimpleNamespace(parts=parts))]


class FakeModel:
    """The Vertex AI client's ``models.generate_content``. ``by_model`` answers one model its own
    way (the failover); every other gets ``answer``."""

    def __init__(self):
        self.answer = FakeResponse(_reply())
        self.by_model = {}
        self.requests = []
        self.models = self

    def generate_content(self, **request):
        self.requests.append(request)
        answer = self.by_model.get(request["model"], self.answer)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _parts(request) -> list:
    """The request's parts in order: the company's lists, the invoice, the closing."""
    (content,) = request["contents"]
    return content.parts


@pytest.fixture
def model(monkeypatch, ai_policy) -> FakeModel:
    fake = FakeModel()
    monkeypatch.setattr(ai, "get_client", lambda timeout_s: fake)
    # no test reaches Frankfurter: another currency gets no rate unless a test stands one in
    monkeypatch.setattr(fx, "frankfurter_rate", lambda base, quote, on: None)
    monkeypatch.setattr(ai.time, "sleep", lambda _s: None)
    return fake


def _policy(**changes):
    """Change the policy as an operator's UPDATE would."""
    AiPolicy.objects.filter(id=1).update(**changes)


@pytest.fixture
def lists(test_entity):
    """Two bill accounts on (400 Advertising, 310 Cost of Goods), one off, one deleted, one of a
    type bills may not use; two suppliers of the current Xero org, one of another org."""
    # By query, not instance.save(): a save turns the fixture's id into a UUID, which the
    # auth token fixture then cannot encode.
    Entity.objects.filter(id=test_entity.id).update(xero_org_id=ORG)
    for code, name, kind, active, deleted in [
        ("400", "Advertising", "EXPENSE", True, False),
        ("310", "Cost of Goods Sold", "DIRECTCOSTS", True, False),
        ("410", "Cleaning", "EXPENSE", False, False),
        ("420", "Old account", "EXPENSE", True, True),
        ("200", "Sales", "REVENUE", True, False),
    ]:
        EntityBillAccountXero.objects.create(entity_id=test_entity.id, account_code=code, account_name=name,
                                             account_type=kind, is_active=active, is_deleted=deleted)
    for xid, name, org in [("con-abc", "ABC Printing", ORG), ("con-taxi", "Taxi Co", ""),
                           ("con-other", "Other Org Ltd", "org-previous")]:
        XeroContactSync.objects.create(entity_id=test_entity.id, xero_contact_id=xid, name=name, xero_org_id=org)


def _extract(api_client, auth_headers, *, body=PDF, name="invoice.pdf", files=None):
    data = {}
    if files is not None:
        data["invoice"] = files
    elif body is not None:
        data["invoice"] = SimpleUploadedFile(name, body, content_type="application/octet-stream")
    return api_client.post(URL, data=data, **auth_headers)


# --- the route ---------------------------------------------------------------------------------


def test_an_invoice_is_read_against_the_company_s_bill_lists(api_client, auth_headers, test_user_entity,
                                                             lists, model):
    resp = _extract(api_client, auth_headers)

    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["reason"] is None
    s = body["suggestions"]
    # no confidence is asked (owner, 2026-11-20): every value is filled and marked to check
    assert s["supplier"] == {"value": "ABC Printing", "confidence": 0.0, "band": "medium", "applied": True,
                             "contact_id": "con-abc", "read_name": "ABC Printing"}
    assert (s["account"]["code"], s["account"]["value"]) == ("400", "400 - Advertising")
    assert s["amount"]["value"] == "1200.50"
    assert (s["invoice_date"]["value"], s["due_date"]["value"]) == ("2026-10-20", "2026-11-19")
    assert s["description"]["value"] == "Flyer printing"
    assert (s["currency"], s["entity_currency"]) == ("HKD", "HKD")
    [request] = model.requests
    assert _parts(request)[1].inline_data.mime_type == "application/pdf"
    assert request["config"].system_instruction == ai_prompt.instruction("invoice")


def test_the_prefix_carries_only_what_the_bill_form_offers(api_client, auth_headers, test_user_entity,
                                                           lists, model):
    _extract(api_client, auth_headers)

    prefix = _parts(model.requests[0])[0].text
    assert "Name: Test Entity" in prefix and "Base currency: HKD" in prefix
    assert "acc_01 | 310 | Cost of Goods Sold" in prefix and "acc_02 | 400 | Advertising" in prefix
    for absent in ("410 |", "420 |", "200 |", "Other Org"):  # off, deleted, not a bill type, old org
        assert absent not in prefix
    assert "con_01 | ABC Printing" in prefix and "con_02 | Taxi Co" in prefix  # no org = legacy, kept
    assert "con-abc" not in prefix  # short ids only
    assert "acc_02" not in json.dumps(model.requests[0]["config"].response_json_schema)


def test_one_contact_list_minty_made_in_merged_and_retired_out(api_client, auth_headers, test_user_entity,
                                                              test_entity, lists, model):
    """C3: the reader offers the bill form's own list - a supplier made in Minty (no Xero id) by
    its row id; one merged away or retired never."""
    mine = XeroContactSync.objects.create(entity_id=test_entity.id, xero_contact_id=None, name="Corner Shop",
                                          xero_org_id="")
    XeroContactSync.objects.create(entity_id=test_entity.id, xero_contact_id="con-gone", name="Gone Ltd",
                                   xero_org_id=ORG, record_status="archived")
    abc = XeroContactSync.objects.get(entity_id=test_entity.id, xero_contact_id="con-abc")
    XeroContactSync.objects.create(entity_id=test_entity.id, xero_contact_id="con-dup", name="ABC Print",
                                   xero_org_id=ORG, record_status="archived", merged_into=abc)
    model.answer = FakeResponse(_reply(supplier_contact_id="con_02"))

    s = _extract(api_client, auth_headers).json()["suggestions"]

    prefix = _parts(model.requests[0])[0].text
    assert "con_02 | Corner Shop" in prefix and "Gone Ltd" not in prefix and "ABC Print\n" not in prefix
    assert s["supplier"]["contact_id"] == str(mine.id) and s["supplier"]["value"] == "Corner Shop"


#: A PDF just over 1 MB - ``ai_policy.max_file_mb`` is at least 1.
BIG_PDF = PDF + b"0" * (1024 * 1024)


@pytest.mark.parametrize("policy, invoice, reason", [
    ({"invoice_reader_on": False}, PDF, "disabled"),
    ({"max_file_mb": 1}, BIG_PDF, "file_too_large"),
], ids=["off", "too-large"])
def test_refusals_past_the_checks_are_a_silent_no_suggestion(api_client, auth_headers, test_user_entity,
                                                             lists, model, policy, invoice, reason):
    _policy(**policy)

    resp = _extract(api_client, auth_headers, body=invoice)

    body = resp.json()
    assert resp.status_code == 200 and (body["suggestions"], body["reason"]) == (None, reason)
    assert model.requests == [] and not AiRead.objects.exists()  # refused before a read: none used


def test_the_receipt_reader_s_switch_does_not_turn_on_the_invoice_reader(api_client, auth_headers,
                                                                         test_user_entity, lists, model):
    _policy(receipt_reader_on=True, invoice_reader_on=False)

    assert _extract(api_client, auth_headers).json() == {"suggestions": None, "reason": "disabled",
                                                         "reader": {"enabled": False}}


def test_a_company_with_nothing_to_choose_from_asks_no_question(api_client, auth_headers,
                                                                test_user_entity, model):
    resp = _extract(api_client, auth_headers)

    body = resp.json()
    assert resp.status_code == 200 and (body["suggestions"], body["reason"]) == (None, "no_context")
    assert model.requests == [] and body["reader"]["percent_left"] == 100


def test_a_failure_of_the_model_is_a_silent_no_suggestion(api_client, auth_headers, test_user_entity,
                                                          lists, model):
    model.answer = TimeoutError("deadline exceeded")

    resp = _extract(api_client, auth_headers)

    body = resp.json()
    assert resp.status_code == 200 and (body["suggestions"], body["reason"]) == (None, "timeout")
    assert body["reader"]["percent_left"] == 100  # a failed read uses none up


def test_one_log_line_with_numbers_and_never_the_values(api_client, auth_headers, test_user_entity,
                                                        lists, model, caplog):
    caplog.set_level("INFO", logger="minty-shared.ai")

    _extract(api_client, auth_headers)

    [line] = [r.getMessage() for r in caplog.records if "invoice_ai: extract" in r.getMessage()]
    assert "tokens_in=5000" in line and "status=ok" in line
    assert "model=gemini-3.8-flash attempts=gemini-3.8-flash:ok" in line
    assert "ABC" not in line and "1200" not in line and "Flyer" not in line


@pytest.mark.parametrize("body, message", [
    (None, "Attach the invoice to read it."),
    (b"PK\x03\x04 a word file", "Attach a PDF, JPEG or PNG file."),
    (b"", "That file is empty. Attach the invoice again."),
    (b"%PDF" + b"0" * (10 * 1024 * 1024), "Attach a file of 10 MB or less."),
], ids=["missing", "docx", "empty", "too-big"])
def test_a_file_the_reader_cannot_take_is_refused(api_client, auth_headers, test_user_entity, lists,
                                                  model, body, message):
    resp = _extract(api_client, auth_headers, body=body)

    assert resp.status_code == 422 and resp.json() == {"detail": message}
    assert model.requests == []


def test_two_files_are_refused(api_client, auth_headers, test_user_entity, lists, model):
    files = [SimpleUploadedFile("a.pdf", PDF), SimpleUploadedFile("b.pdf", PDF)]

    resp = _extract(api_client, auth_headers, files=files)

    assert resp.status_code == 422 and resp.json() == {"detail": "Attach one invoice at a time."}
    assert model.requests == []


def test_only_someone_who_may_create_a_bill_may_ask(api_client, auth_headers, test_user, test_entity,
                                                    lists, model):
    UserEntity.objects.create(user=test_user, entity=test_entity, role="entity_base", approved=True)

    resp = _extract(api_client, auth_headers)

    assert resp.status_code == 403
    assert model.requests == []
    no_token = api_client.post(URL, data={"invoice": SimpleUploadedFile("a.pdf", PDF)})
    assert no_token.status_code == 401


@pytest.mark.parametrize(("switch", "enabled"), [(True, True), (False, False), (None, False)])
def test_the_form_learns_whether_the_reader_is_on(api_client, auth_headers, test_user_entity, ai_policy,
                                                   switch, enabled):
    """The form offers "Read with AI" only when ``ai_policy.invoice_reader_on`` is on (owner,
    2026-11-20; off by the column's default), with the company's reads left; a switched-off reader
    sends no count."""
    if switch is None:
        AiPolicy.objects.filter(id=1).delete()
        AiPolicy.objects.create(id=1, primary_model="gemini-3.8-flash", default_grant={"tokens": 1000000},
                                limit_rules=[], receipt_reader_on=True)
    else:
        _policy(invoice_reader_on=switch)

    resp = api_client.get(STATUS_URL, **auth_headers)

    expected = {"enabled": False}
    if enabled:
        expected = {"enabled": True, "reads_used": None, "reads_limit": None, "reads_left": None,
                    "percent_left": 100, "blocked": False, "resumes_at": None}
    assert resp.status_code == 200 and resp.json() == expected


def test_the_reader_s_status_is_asked_by_those_who_may_read(api_client, auth_headers, test_user, test_entity,
                                                            ai_policy):
    UserEntity.objects.create(user=test_user, entity=test_entity, role="entity_base", approved=True)

    assert api_client.get(STATUS_URL, **auth_headers).status_code == 403
    assert api_client.get(STATUS_URL).status_code == 401


# --- the reply is untrusted input --------------------------------------------------------------


def _read(api_client, auth_headers, model, **reply):
    model.answer = FakeResponse(_reply(**reply))
    body = _extract(api_client, auth_headers).json()
    return body["suggestions"], body["reason"]


@pytest.mark.usefixtures("test_user_entity", "lists")
class TestTheReplyIsChecked:
    def test_a_supplier_not_sent_is_dropped_and_its_name_offered(self, api_client, auth_headers, model):
        s, _ = _read(api_client, auth_headers, model, supplier_contact_id="con_09",
                     supplier_name="Other Org Ltd")
        assert s["supplier"]["contact_id"] == "" and s["supplier"]["applied"] is False
        assert s["supplier"]["detected_name"] == "Other Org Ltd"
        assert s["amount"]["applied"] is True  # one bad field is not a bad reply

    def test_an_unmatched_supplier_comes_with_its_name_signed_for_this_company(
        self, api_client, auth_headers, model, test_entity
    ):
        """The user, 2026-11-23: saving the bill adds it (bill_service.take_reader_contact)."""
        from django.conf import settings
        from minty_shared import ai

        s, _ = _read(api_client, auth_headers, model, supplier_contact_id="", supplier_name=" Other   Org Ltd ")
        token = s["supplier"]["new_supplier_token"]
        assert ai.new_supplier_name(settings.SECRET_KEY, token, test_entity.id) == "Other Org Ltd"
        assert ai.new_supplier_name(settings.SECRET_KEY, token, "another-company") is None

    @pytest.mark.parametrize("answered", ["", "con_09"], ids=["no-id", "an-id-not-sent"])
    def test_the_same_name_in_the_list_is_that_supplier_however_it_is_typed(
        self, api_client, auth_headers, model, answered
    ):
        """The user, 2026-11-23: a supplier the list has is chosen, never offered as a new one."""
        s, _ = _read(api_client, auth_headers, model, supplier_contact_id=answered, supplier_name="abc-PRINTING")
        assert (s["supplier"]["contact_id"], s["supplier"]["value"]) == ("con-abc", "ABC Printing")
        assert "detected_name" not in s["supplier"] and "new_supplier_token" not in s["supplier"]

    def test_the_name_as_printed_comes_with_the_chosen_supplier(self, api_client, auth_headers, model):
        """The user, 2026-11-23: shown beside a "similar" pick ("滬江 -> Wu Kong")."""
        s, _ = _read(api_client, auth_headers, model, supplier_name=" ABC  Printing (HK) ")
        assert (s["supplier"]["contact_id"], s["supplier"]["read_name"]) == ("con-abc", "ABC Printing (HK)")

    def test_no_name_printed_means_no_read_name(self, api_client, auth_headers, model):
        s, _ = _read(api_client, auth_headers, model, supplier_name="  ")
        assert "read_name" not in s["supplier"]

    def test_a_matched_supplier_has_no_token(self, api_client, auth_headers, model):
        s, _ = _read(api_client, auth_headers, model)
        assert "new_supplier_token" not in s["supplier"]

    def test_a_short_id_matches_whatever_its_case(self, api_client, auth_headers, model):
        s, _ = _read(api_client, auth_headers, model, supplier_contact_id=" CON_01 ")
        assert s["supplier"]["contact_id"] == "con-abc"

    @pytest.mark.parametrize("account_id", ["acc_03", "400", "410", "con_01"])
    def test_an_account_the_form_does_not_offer_is_dropped(self, api_client, auth_headers, model, account_id):
        s, _ = _read(api_client, auth_headers, model, account_id=account_id)
        assert s["account"]["code"] == "" and s["account"]["applied"] is False

    @pytest.mark.parametrize("amount", [0, -50, 99_999_999_999])
    def test_an_out_of_range_amount_is_dropped(self, api_client, auth_headers, model, amount):
        s, _ = _read(api_client, auth_headers, model, amount=amount)
        assert s["amount"]["value"] == "" and s["amount"]["applied"] is False

    def test_the_amount_takes_the_currency_s_places(self, api_client, auth_headers, model, test_entity):
        from bills.models import CurrencyInfo

        jpy = CurrencyInfo.objects.create(id="22222222-2222-2222-2222-222222222222", currency_code="JPY",
                                          currency_name="Yen", symbol="¥", decimal_places=0, is_active=True)
        Entity.objects.filter(id=test_entity.id).update(currency_id=jpy.id)
        s, _ = _read(api_client, auth_headers, model, amount=1200.4, currency="JPY")
        assert s["amount"]["value"] == "1200"
        assert (s["currency"], s["entity_currency"]) == ("JPY", "JPY")

    def test_a_due_date_before_the_invoice_date_is_dropped(self, api_client, auth_headers, model):
        s, _ = _read(api_client, auth_headers, model, date="2026-10-20", due_date="2026-10-01")
        assert s["invoice_date"]["value"] == "2026-10-20"
        assert s["due_date"]["value"] == "" and s["due_date"]["applied"] is False

    @pytest.mark.parametrize("value", ["20/10/2026", "2026-13-40", "soon"])
    def test_a_date_that_is_not_a_date_is_dropped(self, api_client, auth_headers, model, value):
        s, _ = _read(api_client, auth_headers, model, date=value)
        assert s["invoice_date"]["value"] == "" and s["invoice_date"]["applied"] is False

    def test_another_currency_is_handed_on_for_a_note_only(self, api_client, auth_headers, model):
        s, _ = _read(api_client, auth_headers, model, currency="usd")
        assert (s["currency"], s["entity_currency"]) == ("USD", "HKD")
        s, _ = _read(api_client, auth_headers, model, currency="dollars")
        assert s["currency"] == ""

    def test_the_description_is_capped_and_the_supplier_name_too(self, api_client, auth_headers, model):
        s, _ = _read(api_client, auth_headers, model, description="x" * 5000,
                     supplier_contact_id="", supplier_name="z" * 400)
        assert len(s["description"]["value"]) == 200 and len(s["supplier"]["detected_name"]) == 100

    def test_every_answered_field_is_filled_and_marked_to_check(self, api_client, auth_headers, model):
        s, _ = _read(api_client, auth_headers, model)
        for key in ("supplier", "account", "amount", "description", "invoice_date", "due_date"):
            assert (s[key]["band"], s[key]["applied"]) == ("medium", True), key

    def test_nothing_usable_is_not_enough_evidence(self, api_client, auth_headers, model):
        # owner, 2026-11-23: nothing describes what was paid for, so the page says why
        empty = {"supplier_contact_id": "", "supplier_name": "", "account_id": "", "amount": 0,
                 "currency": "", "date": "", "description": "", "due_date": ""}
        assert _read(api_client, auth_headers, model, **empty) == (None, "no_description")


@pytest.mark.usefixtures("test_user_entity", "lists")
class TestTheEvidenceRule:
    """The owner's launch decisions (2026-11-23, after IRD's "A Guide to Keeping Business
    Records"): one document per file, and what was paid for described or inferred."""

    def test_an_invoice_with_no_description_is_not_enough_evidence(self, api_client, auth_headers, model):
        assert _read(api_client, auth_headers, model, description=" ") == (None, "no_description")

    def test_several_invoices_in_one_file_are_refused_before_anything_else(self, api_client, auth_headers,
                                                                          model):
        refused = _read(api_client, auth_headers, model, document_count=2, description="")
        assert refused == (None, "several_documents")

    def test_an_inferred_description_is_filled_and_marked_to_check_harder(self, api_client, auth_headers,
                                                                          model):
        s, reason = _read(api_client, auth_headers, model, description="Software subscription",
                          description_inferred=True)
        assert reason is None and s["description"]["applied"] is True
        assert (s["description"]["band"], s["description"]["inferred"]) == ("low", True)

    def test_a_printed_description_stays_in_the_usual_band(self, api_client, auth_headers, model):
        s, _ = _read(api_client, auth_headers, model)
        assert (s["description"]["band"], s["description"]["inferred"]) == ("medium", False)

    def test_a_refused_read_still_counts(self, api_client, auth_headers, model):
        model.answer = FakeResponse(_reply(document_count=3))
        body = _extract(api_client, auth_headers).json()
        assert body["reason"] == "several_documents"
        assert AiRead.objects.get().status == "ok"  # answered, so it counts (a failed read never does)


def test_the_instruction_is_the_owners_simple_prompt_with_the_due_date_and_no_bill_number():
    instruction = ai_prompt.instruction("invoice")
    assert "- due_date: ISO 8601 date (YYYY-MM-DD)" in instruction
    assert "bill number" not in instruction.lower() and "invoice number" not in instruction.lower()
    assert "bill_number" not in invoice_reader.Reading.model_fields


# --- the allowance: one per company, shared with Petty Cash (owner, 2026-11-18) ------------------


@pytest.mark.usefixtures("test_user_entity", "lists")
def test_the_first_read_gives_the_one_time_grant_and_a_used_up_grant_stops_reading(api_client, auth_headers,
                                                                                    test_entity, model):
    _policy(default_grant={"tokens": 5000})

    first = _extract(api_client, auth_headers).json()  # 5,700 tokens: the crossing read finishes
    refused = _extract(api_client, auth_headers).json()

    assert first["reason"] is None and first["reader"]["blocked"] and first["reader"]["percent_left"] == 0
    assert (refused["suggestions"], refused["reason"]) == (None, "limit_reached")
    assert len(model.requests) == 1
    [grant] = AiGrant.objects.filter(entity_id=test_entity.id)
    assert (grant.source, grant.tokens_total) == ("default", 5000)


@pytest.mark.usefixtures("test_user_entity", "lists")
def test_petty_cash_reads_use_the_same_grants(api_client, auth_headers, test_entity, model):
    _policy(default_grant=None)
    grant = ai_allowance.give_grant(test_entity.id, ai_allowance.GrantSpec(tokens=10000))
    AiRead.objects.create(entity_id=test_entity.id, grant_id=grant, module="petty_cash", status="ok",
                          tokens_in=10000)

    status = api_client.get(STATUS_URL, **auth_headers).json()
    assert (status["percent_left"], status["blocked"]) == (0, True)
    assert _extract(api_client, auth_headers).json()["reason"] == "limit_reached"
    assert model.requests == []


@pytest.mark.usefixtures("test_user_entity", "lists")
def test_a_read_is_recorded_as_payment_request_s_with_the_model_that_answered(api_client, auth_headers,
                                                                             test_entity, model):
    _policy(primary_model="gemini-3.5-flash-lite", backup_model="gemini-3.8-flash")
    model.by_model["gemini-3.5-flash-lite"] = FakeResponse("{not json")

    body = _extract(api_client, auth_headers).json()

    assert body["reason"] is None and body["reader"]["percent_left"] == 99
    [row] = AiRead.objects.filter(entity_id=test_entity.id)
    assert (row.module, row.status, row.model_answered) == ("payment_request", "ok", "gemini-3.8-flash")
    assert row.attempts == "gemini-3.5-flash-lite:invalid_reply>gemini-3.8-flash:ok"
    assert row.tokens_in == 10000  # both attempts were billed


@pytest.mark.usefixtures("test_user_entity", "lists")
def test_no_policy_turns_reading_off(api_client, auth_headers, model):
    AiPolicy.objects.all().delete()

    assert api_client.get(STATUS_URL, **auth_headers).json() == {"enabled": False}
    assert _extract(api_client, auth_headers).json()["reason"] == "not_configured"
