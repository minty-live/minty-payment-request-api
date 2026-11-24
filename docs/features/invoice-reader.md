# Invoice reader - reading an invoice into the new-bill form

When an invoice is attached in minty-payment-request-web's new-bill form and the person presses
**Read invoice** (never on attaching it - owner, 2026-11-18), a vision model reads it
and the form's empty fields are **filled as suggestions** the person checks (owner, 2026-11-04):
the supplier, the invoice and due dates, the amount, the description and the account. Nothing is
saved until Confirm or Save as Draft. A supplier the company does not have is added
automatically, but only when the bill is saved, never by the read (the user, 2026-11-23; see *A
supplier the company does not have* below). The form stays usable while the read runs (or fails). The bill number is not read: it is Minty's own
`MBI...` reference.

It is built the same way as Petty Cash's receipt reader (minty-petty-cash-api's
`docs/features/expense-ai.md`), on the same engine.

## Where it lives

- `GET /api/v1/bills/extract/status/` (`bills/api.py::extract_status_endpoint`, 2026-11-18) -
  a usable `ai_policy` with `invoice_reader_on`, read on the call, with the company's AI allowance
  left (below): the form offers "Read invoice" only when it is on. Asked by the same people as the read
  (403 otherwise).
- `POST /api/v1/bills/extract/` (`bills/api.py::extract_invoice_endpoint`) - multipart, one
  `invoice`. Who may ask: `check_create_bill` and not a system superuser outside their own
  company (403). The file: one PDF / JPEG / PNG of at most 10 MB, judged by its bytes
  (`invoice_reader.checked_invoice`; 422 `{"detail": sentence}`: "Attach the invoice to read
  it.", "Attach one invoice at a time.", "That file is empty. Attach the invoice again.",
  "Attach a file of 10 MB or less.", "Attach a PDF, JPEG or PNG file."). The attachment allowlist
  is wider (Word, Excel, HTML); the reader takes only what the model reads. Past those checks
  every outcome is 200 `{suggestions, reason, reader}`, `suggestions` null on any failure or
  refusal (below).
- `bills/services/invoice_reader.py` - the invoice's half: the lists sent with it, the prompt over
  them (`prompt`, below) and the check of the reply against them.
  - **Suppliers**: the bill form's own list as the database holds it (2026-11-20, the owner's
    decisions doc C3: one contact list) - active and not merged away, of the company's current Xero
    organisation (`contact_service.xero_org_scope`; rows with no org recorded count as legacy and
    are kept) or made in Minty (no Xero id: offered by its row id, `contact_service.bill_contact_id`).
    From the database only - never a live Xero call per read.
  - **Accounts**: `entity_bill_account_xero` rows that are active, not deleted and of
    `BILL_SETTINGS_ACCOUNT_TYPES` - the form's dropdown - sent and matched by **code** (bills store
    codes; an account added by hand has no Xero id).
  - **Checks**: a short id (supplier or account) is accepted only if it was sent; the amount is a
    positive decimal up to 10,000,000 in the company currency's own places (JPY 0); a date that is
    not `YYYY-MM-DD` is dropped, and so is a due date before the invoice date; the description is
    capped at 200 characters and an unmatched supplier name (`detected_name`, with its signed
    `new_supplier_token`) at 100. A bill always takes the company's currency: an invoice
    in another one has its amount converted (below), kept as read until then - never rounded to
    the company's places first.
- `minty_shared.ai` (minty-shared-py, extra `ai`) - the engine: the switch, the models and their
  failover, the call and its one retry, usage and cost, and the ONE log line (numbers only, never a
  value read off an invoice; `model=` the model that answered, `attempts=` the chain; logger
  `minty-shared`, into `logs/core.log`).
  `minty_shared.model_input` makes the small copy the model reads (an image capped at 2048 px and
  saved once as JPEG; a PDF whole, every page - minty-shared v0.16, the user 2026-11-24; the 10 MB
  limit stays). The stored attachment is never changed.

## The prompt (owner, 2026-11-20: "simple like this first")

The owner's simple prompt, shared with the receipt reader (`minty_shared.ai_prompt`, minty-shared-py
v0.10; minty-petty-cash-api's `docs/features/expense-ai.md` has the text): seven fields plus
`due_date` for an invoice ("if only terms such as Net 30 are printed, add them to the invoice
date"), the Entity, the accounts and the suppliers listed by short ids (`acc_01`, `con_01`) that
`validate` maps back to the code and the supplier, JSON only. The bill number is not asked for.
Since minty-shared-py v0.17 (the user's wording, 2026-11-24) the account is "" when nothing in
the list matches (v0.13's "closest, never empty" is undone); the prompt is the receipt's, plus the
due date.

## Evidence (owner's launch decisions, 2026-11-23)

The basis is IRD's "A Guide to Keeping Business Records": a record of a payment shows what was paid
for, and when the document doesn't list it, the description is the "additional notation"
(minty-petty-cash-api's `docs/features/expense-ai.md` has the full note). minty-shared-py v0.15:
- **`date`** (the invoice date) is a printed billing period's last day, otherwise the payment date
  (one line for receipts and invoices since v0.17, the user, 2026-11-24);
  the exchange rate is that same day's.
- **`description`** is inferred from the supplier and context when no items are listed, with
  `account_id` chosen to match; an inferred one comes in the `low` band with `inferred: true`, so
  the page asks for a closer check. `""` only when it can't be inferred.
- **Refusals** (`ai.evidence_refusal`, after the read, before any rate): more than one invoice in
  the file (`document_count`) answers `{suggestions: null, reason: "several_documents"}`, then an
  empty description `reason: "no_description"`. Either still counts as a read (a model answered).

## What the file may be instead (the user, 2026-11-24)

A warning, never a refusal: shop managers sometimes attach a bank payment confirmation, an
unrelated photo or an unreadable picture. minty-shared-py v0.18 asks for `document_type` (receipt,
invoice, bank_payment_confirmation, other or unreadable), and every answered read - refused ones
included - carries `document_warning`: `bank_payment_confirmation`, `other` or `unreadable`, else
null (`ai.document_warning`; a receipt and an invoice are both fine). The suggestions still fill;
the page shows the warning and saving goes on as before. It costs no second call (same read, same
allowance), and like every read it happens only when the person presses Read. The free photo
check (blurry, dark, small - no AI) is the web's, on every photo attached (`@minty/shared`'s
`photoQuality`).

A card payment slip (a card terminal's merchant or customer copy, e.g. a Bank of China VISA slip
with only a shop name and a total) attached as an invoice is warned about as a
`bank_payment_confirmation` (the user, 2026-11-24, "separate prompt"): from minty-shared-py v0.20
the invoice prompt alone defines that type as "a bank transfer confirmation or a card payment slip
... that lists no goods or services". Without it the model called such a slip a receipt and no
warning showed. Petty Cash's receipt prompt has no such line: a card slip stays a receipt there.

## The supplier (the user, 2026-11-23)

The prompt (minty-shared-py v0.14) asks for the same **or a similar** supplier from the list
(another spelling, language or branch of the same business), "" only if none is. A supplier the
model leaves out (no id, or an id not sent) whose printed name is a list supplier's name - case,
spaces and punctuation ignored (`minty_shared.names`) - is that supplier, never offered as a new
one. Every answer with a printed name carries it as `read_name` (spaces collapsed, at most 100
characters), so the form can show it beside a "similar" pick.

## A supplier the company does not have (the user, 2026-11-23)

An unmatched supplier comes back with `detected_name` and `new_supplier_token`, which is the name
signed with `SECRET_KEY` for this company (`minty_shared.ai.new_supplier_token`, good for 7 days, the
life of a local draft). The form sends it back as `new_contact_token` (on `BillCreateIn`,
`BillDraftIn` and `BillUpdateIn`) with an empty `xero_contact_id`, on any of draft, create, submit
or edit. `bill_service.take_reader_contact` then works like this:
- it runs after the save's submission check;
- it makes the name the bill's supplier through `contact_service.create_entity_bill_contact_in_xero`,
  which uses the company's supplier of that name if it has one, otherwise adds one the way
  `POST /entity-bill-contacts/` does (C5: made in Xero at once while live, the company's own row
  offline);
- there is no role check, by the user's rule. Adding a bill already needs Cashier, the same as
  adding a contact.

A token that has been altered, is for another company or has expired is 422 "The suggested supplier
has expired. Choose a supplier from the list or add it." and nothing is saved. A chosen
`xero_contact_id` wins over a token. A later refusal of the same save (a reference already used, a
DUP1 question) keeps the supplier, and the retry finds it. Tests: `bills/tests/test_reader_contact.py`.

**No confidence**: every value that passes the checks is filled into an empty field and marked for
the person to check (band `medium`). The answer's shape to the web is unchanged (`supplier`,
`account`, `amount`, `description`, `invoice_date`, `due_date`, `currency`, `entity_currency`); the
2026-11-19 prompt's `invoice_date_raw` (no screen used it) and its '$' rule went with it.

## An invoice in another currency (owner, 2026-11-19)

When the invoice's currency is not the company's, the amount comes back **converted** at
Frankfurter's rate (v1, the European Central Bank's reference rates, 30 currencies, no key) -
`minty_shared.fx.exchange_for` (minty-shared-py v0.8), after the read and outside the models'
failover: the invoice's own `invoice_date` (basis `receipt`), else the day the bill is entered - the
company's today in its country's zone (`report_date`) - when it is empty, in the future or more than a
year off; a weekend or holiday gets the previous business day's rate. Original x rate, half up, in
the company currency's places (JPY 0); the 10,000,000 cap is on the converted amount. The answer
carries `exchange` `{original_currency, original_amount, rate, rate_date, date_basis, source}`. No
rate (any error, 5 s timeout, a currency v1 doesn't have): the amount comes back empty with
`exchange_failed: {currency}`; nothing is blocked.

A saved bill keeps it: `POST /bills/`, `/bills/submit/`, `/bills/draft/` and `PUT /bills/{id}/draft`,
`PUT /bills/{id}` take an optional `exchange` - the six columns by name (`original_currency`,
`original_amount`, `exchange_rate`, `exchange_rate_date`, `exchange_rate_basis`,
`exchange_rate_source`) - checked by `fx.saved_exchange` (all six or none, another currency, a rate
above 0 for a day not after today, a known basis, Frankfurter, and original x rate giving the bill's
amount; else 422 "The exchange-rate details don't match this amount. Read the invoice again or enter
the amount yourself."). An update that changes the amount without them clears them. Xero is
unchanged: `amount` is the company-currency figure. `FX_API_URL` moves the address. Tests:
`bills/tests/test_invoice_exchange.py`.

## The company's AI allowance and the models (owner, 2026-11-18)

AI reading is a preview. **Each company reads against its own grants**, Petty Cash and Payment
Request together, and the models are settings - all in minty-db (v0.13), changed with an UPDATE or
minty-petty-cash-api's `python manage.py ai_policy`, never a release:

- `ai_grant`: an allowance given to one company - `tokens_total` and/or `reads_total`, an optional
  `expires_at`, `source` (`default` | `manual`), who gave it and why. A top-up is another grant. A
  used-up or expired grant is spent and stays as the record.
- `ai_policy` (one row): `primary_model`, `backup_model`, `default_grant` - the one-time grant every
  company is given automatically on its first read (`{"tokens": n, "reads": n, "days": n}`; NULL =
  only grants given by hand) - and `limit_rules`, optional extra caps over every company
  (`{"reads": n, "tokens": n, "period_days": n}`, all applying; a rolling window, e.g. a daily
  throttle). The preview: `default_grant {"tokens": 20000}` (about four to five receipts; a read is
  ~3,500-5,700 tokens), no extra caps. The default grant is given once per company, ever
  (`uq_ai_grant_one_default`): a used-up company stays used up until it is given another grant
  (`manage.py ai_policy --grant <company id> --tokens 50000 [--reads n] [--days n] [--note ...]`,
  or minty-web's Select Company in its **AI allowances** mode - every superuser; minty-accounts-api's
  `docs/features/ai-allowances.md`).
- `ai_read`: one row per read asked for - `pending`, then `ok` or `failed` - charged to one grant
  (the oldest with room), with the module, the model that answered, the chain tried
  (`gemini-3.5-flash-lite:timeout>gemini-3.8-flash:ok`) and every attempt's tokens.
- **Cost per company in Google's own figures (the user, 2026-11-24; minty-shared-py v0.20):** every
  Vertex call (each model, each retry) carries the labels `env` (`production`/`development`, from
  `APP_ENV`), `feature` (`payment_request`) and `entity_id` (the company's id, lowercase) -
  `ai.request_labels`. Nothing per invoice or person, and no company name. Cloud Billing > Reports >
  Group by label then shows Google's cost per company to set beside `ai_read.estimated_cost`
  (USD; the billing account is HKD). Reads before the release carry no labels.
- **Each model's price is `ai_policy.model_prices`** (the user, 2026-11-24; minty-db v0.19):
  `{"<model>": {"input": n, "output": n, "cached_input": n}}`, USD per 1M tokens, Vertex AI's
  global-endpoint list prices (gemini-3.5-flash-lite 0.30 / 2.50 / 0.03, gemini-3.5-flash 1.50 /
  9.00 / 0.15, checked 2026-11-24). A read's cost = uncached input at `input` + cached input at
  `cached_input` + output and thinking at `output`, every attempt summed. A policy that names a model
  without a price is refused by the database (`ck_ai_policy_prices`) and by `load_policy` (reading
  OFF, logged). Change a price with an UPDATE or `manage.py ai_policy --set-price MODEL IN OUT
  CACHED` (Petty Cash); a new model needs its price in the same change.
- **What counts:** a grant is charged the tokens of reads a model answered (even with nothing usable
  in them); a failed read uses none of it, though its billed tokens are recorded and count against
  the extra caps (our cost guard). A grant is used up once its tokens reach its total - the read
  that crosses it finishes. A refusal before the model is asked (switched off, file too large, no
  accounts or suppliers) uses nothing.
- **How:** `minty_shared.django.ai_allowance` (minty-shared-py v0.7) - `reserve` under a per-company
  Postgres advisory lock (it gives the default grant on the first read; two clicks with room for
  one: one passes), `finish`, `give_grant`, `reader_status`. A missing or unusable policy row turns
  reading OFF and logs an error - never a guessed limit.
- **Failover:** the engine tries `primary_model`, then `backup_model` when the first times out,
  errors, is rate-limited, truncates or replies unusably (not on a safety block). Only a provider in
  `minty_shared.ai.APPROVED_PROVIDERS` (Gemini today) is ever called; a model id names its provider
  (`gemini-...`, or `<provider>:<model>`), so another approved provider can be added later.
- **The answer** carries `reader` - `{enabled, percent_left, reads_used, reads_limit, reads_left,
  blocked, resumes_at}` - after every read, so the page's "80% of your AI allowance left" moves
  with no extra call. `percent_left` is measured over the grants that still have room (a top-up
  after a spent grant reads 100%). Used up, the route answers `limit_reached` without asking the
  model and the page disables the button.
- The in-process `RateLimiter` and `*_RATE_USER_PER_MIN`/`*_RATE_ENTITY_PER_HOUR` are gone (replaced).
- **Production:** Vertex AI on its `global` endpoint (minty-shared v0.12; below).

## Configuration

The route is Vertex AI only (owner, 2026-11-20; minty-shared v0.11): `models.generate_content` on
the `global` endpoint since minty-shared v0.12 (owner, 2026-11-23: no region setting; the policy's
`gemini-3.5-flash-lite` is not served in `asia-southeast1`) for project `minty-515207`,
authenticated by the `minty-ai` service account's key file. The direct Gemini API is gone.

| Where | Setting | Meaning |
|---|---|---|
| `ai_policy` (database) | `invoice_reader_on` | off unless on - the route answers `disabled`. Its own switch: the receipt reader's `receipt_reader_on` does not turn it on |
| `ai_policy` | `primary_model`, `backup_model` | the models tried, in order - above |
| `ai_policy` | `timeout_s` (25, per model tried), `max_output_tokens` (4096, thinking included), `thinking_level` (`low`) | the call - shared with the receipt reader |
| `ai_policy` | `max_file_mb` (10) | the model's size cap |
| environment | `GOOGLE_CLOUD_PROJECT` | Vertex AI's project (`minty-515207`); without it, no call is made (`not_configured`) |
| environment | `GOOGLE_APPLICATION_CREDENTIALS` | the service account's key file (on Render a secret file) |

The `ai_policy` settings are changed with minty-petty-cash-api's `python manage.py ai_policy`
(`--invoice-reader on|off`, ...). The old `BILL_AI_*`, `EXPENSE_AI_*` and `GEMINI_API_KEY`
variables are read by nothing.

## Tests

`bills/tests/test_invoice_reader.py` - the route (who may ask, the file refusals, the silent
reasons, its own switch), the allowance (the last read refused, Petty Cash's reads counted here,
the read recorded as Payment Request's with the model that answered after a failover, no policy =
off), the context sent (only the form's accounts and suppliers - Minty-made in, merged and retired
out - by short ids in a stable order), the reply checks, and the one log line; the model is a stand-in.
The engine's own cases are minty-shared-py's `tests/test_ai.py`.
