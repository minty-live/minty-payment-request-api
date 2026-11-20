"""Reading a supplier's invoice with the model, to fill the new-bill form (owner, 2026-11-04).

The Payment Request half of the document reader; the engine (the call, its retries, the switches,
cost and the one log line) is ``minty_shared.ai``, and the prompt is the owner's simple one
(2026-11-20; ``minty_shared.ai_prompt``, shared with minty-petty-cash-api's receipt reader): seven
fields plus the due date, the company's lists with short ids (``acc_01``, ``con_01``), JSON only,
no confidence - every value that passes the check is filled and marked for the person to check.
Minty supplies the company's bill accounts and suppliers; the person is the final authority -
nothing here writes any record, and every failure is "no suggestion" with a reason.

What is read: the supplier (from the company's own list - the bill form's, Minty-made suppliers
included), the invoice and due dates, the total, a short description and the bill account (by
code - bills store codes). Not read: the bill number, which is Minty's own ``MBI...`` reference.
A bill always takes the company's currency: an invoice in another one has its amount converted at
Frankfurter's rate (``minty_shared.fx``, owner 2026-11-19), and the bill keeps how
(``exchange``) while its amount is the converted one.

Configuration: ``BILL_AI_*`` (``minty_shared.ai.ReaderConfig``), its own switch and limits; the
route to the model (``GEMINI_API_KEY``, ``EXPENSE_AI_ALLOW_DIRECT_API``, ``EXPENSE_AI_DIRECT_TIER``)
is shared with the receipt reader.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal, InvalidOperation

from minty_db.models import Entity, XeroContactSync
from minty_shared import ai, ai_prompt, fx
from minty_shared.django import ai_allowance
from minty_shared.model_input import prepare_for_model
from minty_shared.time import DEFAULT_ENTITY_TIMEZONE, country_timezone, today_in

from bills.models import EntityBillAccountXero
from bills.services.contact_service import bill_contact_id, xero_org_scope
from core.entity_currency import entity_currency
from core.exceptions import BillValidationError

logger = logging.getLogger("minty-api")

CONFIG = ai.ReaderConfig("BILL_AI")

_MAX_AMOUNT = Decimal("10000000")
_MAX_FOREIGN_AMOUNT = Decimal("999999999999")
_MAX_DESCRIPTION = 200


#: The reply's model: the owner's simple prompt plus the due date.
Reading = ai_prompt.InvoiceReading


def build_context(entity_id) -> dict | None:
    """The company's facts and the lists the new-bill form offers. Suppliers: the form's own list
    as the database holds it (``contact_service``'s fallback - never a live Xero call per read):
    active and not merged away, of the company's current Xero organisation or made in Minty (one
    contact list, owner's decisions doc C3), each by the id the form uses. Accounts: active, not
    deleted, of the bill types, by code. None when there is nothing to choose from. Nothing else
    is sent; ``ai_prompt`` puts both lists in a byte-stable order."""
    from bills.api_config import BILL_SETTINGS_ACCOUNT_TYPES

    entity = Entity.objects.filter(id=entity_id).first()
    if entity is None:
        return None
    accounts: dict[str, str] = {}
    for row in EntityBillAccountXero.objects.filter(
        entity_id=entity_id, is_deleted=False, is_active=True,
        account_type__in=BILL_SETTINGS_ACCOUNT_TYPES,
    ):
        code = (row.account_code or "").strip()
        if code and code not in accounts:
            accounts[code] = (row.account_name or "").strip()
    contacts: dict[str, str] = {}
    seen: set[str] = set()
    for row in XeroContactSync.objects.filter(
        entity_id=entity_id, record_status="active", merged_into__isnull=True
    ).filter(xero_org_scope(entity.xero_org_id)):
        contact_id = (bill_contact_id(row) or "").strip()
        if contact_id and contact_id.upper() not in seen:
            seen.add(contact_id.upper())
            contacts[contact_id] = (row.name or "").strip()
    if not accounts and not contacts:
        return None
    code, places = entity_currency(entity_id)
    return {
        "entity": {"id": str(entity.id), "name": entity.name or "",
                   "country": entity.country_code or "", "currency": code, "places": places},
        "accounts": sorted(({"code": c, "name": n} for c, n in accounts.items()),
                           key=lambda a: (a["code"], a["name"])),
        "contacts": sorted(({"id": i, "name": n} for i, n in contacts.items()),
                           key=lambda c: (c["name"].lower(), c["id"])),
    }


def prompt(context: dict) -> tuple[str, str, ai_prompt.Aliases]:
    """``(instruction, prefix, aliases)``: the owner's simple prompt (with the due date) over this
    company's lists, with short ids that ``aliases`` maps back to account codes and supplier ids."""
    entity = context["entity"]
    return ai_prompt.build(
        ai_prompt.INVOICE,
        {"name": entity["name"], "country": entity["country"], "currency": entity["currency"]},
        [(a["code"], a["code"], a["name"]) for a in context["accounts"]],
        [(c["id"], c["name"]) for c in context["contacts"]],
    )


def _iso_date(value: str) -> str:
    text = (value or "").strip()[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def validate(parsed: Reading, context: dict, aliases: ai_prompt.Aliases) -> dict:
    """The reply is untrusted input: a short id is accepted only if it was in the lists sent with
    THIS request, and a value that fails a check is dropped, never repaired. There is no confidence
    (owner, 2026-11-20): every value that passes is filled and marked for the person to check."""
    accounts = {a["code"]: a for a in context["accounts"]}
    contacts = {c["id"]: c for c in context["contacts"]}

    supplier_id = aliases.supplier(parsed.supplier_contact_id) or ""
    if supplier_id in contacts:
        contact = contacts[supplier_id]
        supplier = ai.checked(contact["name"], {"contact_id": contact["id"]})
    else:
        if (parsed.supplier_contact_id or "").strip():
            logger.info("invoice reader: dropped a supplier id not in the sent list")
        # The name read off the invoice, for the form's "+ Add ... as a new supplier" - never a
        # contact (creating one stays the person's click). Untrusted text: stripped and capped
        # at the form's own 100-character supplier name limit.
        supplier = ai.checked("", {"contact_id": "",
                                   "detected_name": (parsed.supplier_name or "").strip()[:100]})

    code = aliases.account(parsed.account_id) or ""
    if code in accounts:
        account = accounts[code]
        account_field = ai.checked(f"{code} - {account['name']}" if account["name"] else code,
                                   {"code": code, "name": account["name"]})
    else:
        if (parsed.account_id or "").strip():
            logger.info("invoice reader: dropped an account id not in the sent list")
        account_field = ai.checked("", {"code": "", "name": ""})

    currency = (parsed.currency or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        currency = ""
    # another currency: kept as read (2 places) and capped only once converted (fx.exchange_for);
    # rounding it to the company's places first would convert the wrong number
    foreign = bool(currency) and currency != context["entity"]["currency"]
    places = 2 if foreign else int(context["entity"].get("places", 2))
    amount_value = ""
    try:
        amount = Decimal(str(parsed.amount or 0)).quantize(Decimal(1).scaleb(-places))
        if Decimal("0") < amount <= (_MAX_FOREIGN_AMOUNT if foreign else _MAX_AMOUNT):
            amount_value = format(amount, "f")
    except (InvalidOperation, ValueError, TypeError):
        amount_value = ""

    invoice_date = _iso_date(parsed.date)
    due_date = _iso_date(parsed.due_date)
    if invoice_date and due_date and due_date < invoice_date:
        logger.info("invoice reader: dropped a due date before the invoice date")
        due_date = ""

    return {
        "supplier": supplier,
        "account": account_field,
        "amount": ai.checked(amount_value),
        "description": ai.checked((parsed.description or "").strip()[:_MAX_DESCRIPTION]),
        "invoice_date": ai.checked(invoice_date),
        "due_date": ai.checked(due_date),
        "currency": currency,
        "entity_currency": context["entity"]["currency"],
    }


MAX_INVOICE_BYTES = 10 * 1024 * 1024


def checked_invoice(uploads) -> tuple[bytes, str]:
    """The one invoice to read, as bytes and the type its bytes say - or BillValidationError (422)
    with the sentence to show. The attachment allowlist is wider (Word, Excel, HTML...); the reader
    takes only what the model reads."""
    files = [f for f in uploads or [] if f is not None and (f.name or "").strip()]
    if not files:
        raise BillValidationError("Attach the invoice to read it.")
    if len(files) > 1:
        raise BillValidationError("Attach one invoice at a time.")
    upload = files[0]
    if upload.size == 0:
        raise BillValidationError("That file is empty. Attach the invoice again.")
    if upload.size > MAX_INVOICE_BYTES:
        raise BillValidationError("Attach a file of 10 MB or less.")
    upload.seek(0)
    data = upload.read()
    mime = ai.sniff_mime(data[:16])
    if mime is None:
        raise BillValidationError("Attach a PDF, JPEG or PNG file.")
    return data, mime


def reader_status(entity_id) -> dict:
    """The form's reader: whether "Read with AI" is offered, and the company's reads left
    (shared with Petty Cash; owner, 2026-11-18)."""
    return ai_allowance.reader_status(CONFIG.enabled(), entity_id)


def read_invoice(entity_id, user_id, data: bytes, mime: str) -> dict:
    """``{suggestions, reason, reader}`` for an invoice the route has checked (who asks, one
    PDF/JPEG/PNG by its bytes, at most 10 MB). ``suggestions`` is null on every failure; ``reader``
    is the allowance after this read. A read is reserved before the model is called and counts
    only if a model answered (``ai_allowance``); a refusal before that point uses nothing."""
    if not CONFIG.enabled():
        return {"suggestions": None, "reason": ai.REASON_DISABLED, "reader": {"enabled": False}}
    policy = ai_allowance.load_policy()
    if policy is None:
        return {"suggestions": None, "reason": ai.REASON_NOT_CONFIGURED, "reader": {"enabled": False}}
    if len(data) > CONFIG.max_file_bytes():
        return _no_read(ai.REASON_FILE_TOO_LARGE, entity_id, policy)
    context = build_context(entity_id)
    if context is None:
        logger.info("invoice reader: no bill accounts or suppliers for entity=%s", entity_id)
        return _no_read(ai.REASON_NO_CONTEXT, entity_id, policy)
    read_id, allowance = ai_allowance.reserve(entity_id, user_id, ai_allowance.MODULE_PAYMENT_REQUEST, policy)
    if read_id is None:
        logger.info("invoice reader: allowance used up entity=%s", entity_id)
        return {"suggestions": None, "reason": ai.REASON_LIMIT_REACHED,
                "reader": {"enabled": True, **allowance.as_dict()}}
    result = ai.Extraction(reason=ai.REASON_PROVIDER_ERROR)
    try:
        document, document_mime = prepare_for_model(data, mime)
        instruction, prefix, aliases = prompt(context)
        result = ai.read(document, document_mime, config=CONFIG, instruction=instruction,
                         prefix_text=prefix, closing_text=ai_prompt.CLOSING, reply_model=Reading,
                         validate=lambda parsed: validate(parsed, context, aliases),
                         models=policy.models)
    finally:
        # Even if something here raised, the reserved read is closed (failed, so never counted).
        ai_allowance.finish(read_id, result)
    ai.log_read(CONFIG, result, entity_id=entity_id, user_id=user_id)
    answer = result.answer()
    if answer["suggestions"]:
        # another currency: converted at Frankfurter's rate (owner, 2026-11-19) on the invoice's
        # day, else the day the bill is entered - after the read, outside the model's failover
        today = today_in(country_timezone(context["entity"]["country"]) or DEFAULT_ENTITY_TIMEZONE)
        answer["suggestions"] = fx.exchange_for(
            answer["suggestions"], report_date=today, today=today, places=int(context["entity"].get("places", 2)),
            max_amount=_MAX_AMOUNT, fetch=fx.frankfurter_rate, date_field="invoice_date",
        )
    return {**answer, "reader": {"enabled": True, **ai_allowance.status(entity_id, policy).as_dict()}}


def _no_read(reason: str, entity_id, policy: ai_allowance.Policy) -> dict:
    return {"suggestions": None, "reason": reason,
            "reader": {"enabled": True, **ai_allowance.status(entity_id, policy).as_dict()}}
