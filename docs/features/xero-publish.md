# Publishing a bill to Xero

A submitted payment request becomes an **ACCPAY invoice** in the company's Xero
organisation, with its attachments. Code: `bills/services/xero_publish_service.py`,
`bills/api.py::publish_bill_endpoint` (`POST /api/bills/{id}/publish/`, elevated roles),
the sync tables `xero_bill_sync`, `xero_bill_sync_line`, `xero_bill_sync_payload`,
`xero_bill_response_line` (`GET /api/bills/{id}/xero-syncs[/{sync_id}]` reads them), the
token side in [authentication.md](authentication.md).

## The flow (`publish_bill_to_xero`)

1. **Token** — the connector's stored access token if unexpired, else Minty's internal
   token service; without one the endpoint returns *reconnect required*. A bill with no
   undeleted attachment is refused here too, before any Xero call: "Add the invoice
   attachment to publish this payment request." (owner, 2026-10-27).
2. **Which sync to build on** — `_select_successful_sync(bill, xero_org_id)` picks the
   last successful sync **for this organisation**: the org is read from the stored request
   headers (`xero_bill_sync_payload.request_headers["Xero-Tenant-Id"]`; an unrecorded org
   counts as a match on purpose). A sync that belongs to a *different* organisation — the
   company was reconnected elsewhere — is not republished against; the bill is reset for
   the new org (`_reset_for_new_org`) and published afresh. Otherwise a second publish
   into the same org would duplicate the invoice.
3. **Contact heal** — a bill saved without `xero_contact_id` gets it from the cached
   contact with the same name (case and extra spaces ignored); a Minty-only supplier is found in
   Xero by name or made there as a supplier first (`xero_contact_for`, owner's decisions doc C1,
   2026-11-19).
4. **Lock dates** — `fetch_lock_dates` reads the organisation's period and end-of-year
   locks; a bill dated on or before either is sent as `DRAFT` (Xero refuses to AUTHORISE
   into a locked period), otherwise `AUTHORISED`.
5. **First publish** — `PUT /Invoices` with the payload from `_build_xero_invoice_payload`
   (contact, dates, reference, the line with its account code, currency), each sync row
   carrying a fresh idempotency key; **republish** — `POST /Invoices/{InvoiceID}` to update
   in place, except when an AUTHORISED invoice must become DRAFT (the date moved into a
   locked period): then the old one is voided and a new one created. The request, headers
   (the `Authorization` stripped), response and response lines are stored on the sync row;
   `http_status_code`, `sync_status` (`success` / `failed`), `error_message`.
6. **Attachments** — every bill attachment goes to the invoice through the **Files API**
   as a full replace (the previous FileIds are deleted first, then re-uploaded and
   associated); a payment's bank slips the same way ([payments.md](payments.md)).
7. On success `bill.published = published`, an audit line `published_to_xero`
   ("Published…" on the first, "Republished…" after), and the endpoint **re-reads the
   sync row** before answering 200 — a service that returned without a success row is a
   422 with the sync's error.

The Minty side of the same organisation (the petty-cash report's bank transactions) is
`Minty/docs/features/xero-integration.md`; the two never touch each other's objects.

## Chart of accounts and contacts

Minty syncs `entity_bill_account_xero` (the account-code picker) and `xero_contact_sync`
when a company connects; this service asks for a refresh through accounts-api's one sync
(`xero_sync.py`, `POST /api/internal/xero/sync`; see entity-configuration.md - failures are
logged, never raised). `GET /api/entity-bill-contacts/` returns the dropdown's contacts
with the same matching rules Minty uses (`contact_service.py`), `POST` creates a contact
in Xero and caches it.

## Tests

`bills/tests/test_xero_publish.py`, `test_xero_publish_org_switch.py`,
`test_xero_publish_contact_fallback.py`, `test_xero_publish_bankslip.py`,
`test_xero_token_service.py`, `test_contact_*.py`; for real, against a Demo Company,
`minty-payment-request-web/e2e/04_xero_publish.spec.ts` with `E2E_XERO=1`.

## A supplier archived in Xero; an account gone from Xero (the owner's decisions doc C4, A3, 2026-11-19)

- Before the publish (`bills/api.py`, `contact_service.archived_bill_contact`), the bill's supplier when
  linked to Xero is asked of Xero: merged in Xero -> relinked through accounts-api (no prompt); archived with
  no merge -> 409 `{detail: "<Name> is archived in Xero. Restore it in Xero, or map it to another contact
  now.", archived_contacts: [{id, name}]}` (`PublishRefusedOut`), nothing sent. Minty never restores a
  contact in Xero ("+ Add" brings a deleted one back in Minty only).
- "Map it now": `GET /api/v1/entity-bill-contacts/xero/` and
  `POST /api/v1/entity-bill-contacts/{id}/xero-link/ {xero_contact_id}` (`check_publish_xero`).
- A3: `books.stale_matches` uses the doc's sentence, and a line saved by code alone after its account left
  Xero (no `account_id`) is held too.
- One contact list (C3): the picker's live Xero list leaves out a contact Minty deleted or merged away
  (`get_entity_bill_contacts`); Minty-made suppliers are appended as before.
- Tests: `bills/tests/test_archived_contact.py`, `test_minty_on_its_own.py`, `test_entity_bill_contacts_create.py`,
  `test_contact_no_duplicates.py`.

## The duplicate reminder (the owner's decisions doc DUP1, 2026-11-19)

Every bill save - `POST /bills/`, `/bills/submit/`, `/bills/draft/`, `PUT /bills/{id}/draft`, `PUT /bills/{id}` -
looks for another bill (not voided, not itself) with the same date (`invoice_date`), supplier and exactly
the same amount, once all three are filled in (`bill_service.ask_about_duplicates`). Found: 409
`{detail: "This looks like a bill you already have: the same date, supplier and amount. Save it anyway?",
duplicates: [...]}` (`DuplicateBillsOut`, `core/exceptions.py`), nothing saved; the same body with
`"confirm_duplicate": true` saves it. It never blocks. The invoice-number refusal is unchanged.
Tests: `bills/tests/test_duplicate_reminder.py`.
