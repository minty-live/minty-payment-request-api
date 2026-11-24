"""Accounts and suppliers as the company's chart shows them NOW (the user's rule, 2026-11-24).

A bill keeps ids (``bill_line.account_id``, ``bill.contact_id``); every screen and download reads the
code, name and type here, so a matching, a merge or a rename in Xero shows everywhere with nothing
copied. An account or contact merged away (archived, ``merged_into_id`` set) shows the one it was
merged into. The text columns (``bill_line.account_code``/``account_name``, ``bill.xero_account_code``,
``bill.contact_name``) are kept in step but only shown when a row has no id (lines loaded before
``account_id`` existed and no account of the company has their code); the ``published_*`` columns are
the record of what went to Xero and are never shown.
"""

from __future__ import annotations

from dataclasses import dataclass

from minty_db.models import AccountInfo, XeroContactSync

#: How far a merge chain is followed (merges refuse a target that was itself merged away, so it is one).
_MAX_HOPS = 5


@dataclass(frozen=True)
class AccountView:
    id: object
    code: str
    name: str
    type: str


def _follow(ids, model, fields):
    """``{asked id: row}``, each row the end of its ``merged_into`` chain."""
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    rows = {r.id: r for r in model.objects.filter(id__in=wanted).only("id", "merged_into_id", *fields)}
    for _ in range(_MAX_HOPS):
        missing = {r.merged_into_id for r in rows.values() if r.merged_into_id} - rows.keys()
        if not missing:
            break
        rows.update({r.id: r for r in model.objects.filter(id__in=missing).only("id", "merged_into_id", *fields)})
    out = {}
    for i in wanted:
        row = rows.get(i)
        for _ in range(_MAX_HOPS):
            if row is None or not row.merged_into_id or row.merged_into_id not in rows:
                break
            row = rows[row.merged_into_id]
        if row is not None:
            out[i] = row
    return out


def accounts(ids) -> dict:
    """``{account id: AccountView}`` for the ids that exist; the code is Xero's, else Minty's own."""
    return {
        i: AccountView(id=r.id, code=(r.xero_code or r.code or "").strip(), name=r.name or "", type=r.type or "")
        for i, r in _follow(ids, AccountInfo, ("xero_code", "code", "name", "type")).items()
    }


def contacts(ids) -> dict:
    """``{contact id: name}`` for the ids that exist."""
    return {i: r.name or "" for i, r in _follow(ids, XeroContactSync, ("name",)).items()}


def kept_account_id(account_id):
    """The id an account now stands for (itself, or the account it was merged into)."""
    view = accounts([account_id]).get(account_id)
    return view.id if view else None


def line_account(line, views: dict) -> tuple[str, str]:
    """A bill line's (code, name): its account's now, else the stored text."""
    view = views.get(line.account_id) if line.account_id else None
    if view is not None:
        return view.code, view.name
    return (line.account_code or "").strip(), line.account_name or ""


def bill_account_code(bill, lines, views: dict) -> str:
    """The bill's header code: its first line's account now, else the stored header text."""
    for line in lines:
        code, _ = line_account(line, views)
        if code:
            return code
    return (bill.xero_account_code or "").strip()


def bill_contact(bill, names: dict) -> str:
    """The bill's supplier name: its contact's now, else the stored text."""
    if bill.contact_id and bill.contact_id in names:
        return names[bill.contact_id]
    return bill.contact_name or ""


def refresh_bill_text(bill_id) -> None:
    """Rewrite one bill's text copies from its ids (lines' code and name, the header code, the
    supplier's name), so what a publish sends is the chart's now. A merged id moves to the kept one.
    No Xero call; the ``published_*`` record is untouched."""
    from bills.models import Bill, BillLine

    bill = Bill.objects.filter(id=bill_id).only("id", "contact_id", "contact_name", "xero_account_code").first()
    if bill is None:
        return
    lines = list(BillLine.objects.filter(bill_id=bill_id).order_by("sort_order", "created_at")
                 .only("id", "account_id", "account_code", "account_name"))
    views = accounts(line.account_id for line in lines)
    for line in lines:
        view = views.get(line.account_id) if line.account_id else None
        if view is None:
            continue
        code = view.code if len(view.code) <= 20 else line.account_code  # a bill line holds 20
        if (line.account_id, line.account_code, line.account_name) != (view.id, code, view.name[:150]):
            BillLine.objects.filter(id=line.id).update(account_id=view.id, account_code=code,
                                                       account_name=view.name[:150])
            line.account_id, line.account_code, line.account_name = view.id, code, view.name[:150]
    changes = {}
    header = bill_account_code(bill, lines, views)
    if header and header != (bill.xero_account_code or "") and len(header) <= 20:
        changes["xero_account_code"] = header
    if bill.contact_id:
        kept = _follow([bill.contact_id], XeroContactSync, ("name",)).get(bill.contact_id)
        if kept is not None:
            if kept.id != bill.contact_id:
                changes["contact_id"] = kept.id
            if kept.name and kept.name[:100] != (bill.contact_name or ""):
                changes["contact_name"] = kept.name[:100]
    if changes:
        Bill.objects.filter(id=bill.id).update(**changes)
