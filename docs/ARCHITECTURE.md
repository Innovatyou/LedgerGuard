# LedgerGuard architecture

## Layers

```
templates/ (Django templates, one design system: static/css/ledgerguard.css)
        │
views.py / api.py  ── thin: parse input, resolve institution, call services, render
        │
services.py / engine.py / reports.py  ── all business rules, permission checks, workflow transitions
        │
models.py  ── data, constraints, invariants that must hold regardless of caller
        │
PostgreSQL ── NUMERIC money columns, unique/check constraints, append-only triggers
```

Business rules live in service functions, never in templates or views. Every service that changes state checks
the actor's permission (`accounts.services.require_perm`) and records an `ActivityLog` entry; workflow decisions
are also recorded as `ApprovalRecord`s.

## Modules

| App | Responsibility | Depends on |
|---|---|---|
| `core` | Base models (`InstitutionScopedModel`), money parsing/rounding, tenancy resolution, request context, activity log, evidence attachments, document numbering, view/API base classes, structured logging, health checks | – |
| `organizations` | Institutions, branches, departments/cost centres, institution settings, `seed_demo` | core |
| `accounts` | User model, roles, memberships, permission catalogue, login throttling, TOTP MFA | core, organizations |
| `approvals` | Maker-checker helpers and immutable approval records | core |
| `notifications` | In-app (and optional e-mail) notifications | accounts |
| `accounting` | Chart of accounts, periods, journals, posting engine, ledger, financial reports, COA templates | core, accounts, approvals |
| `imports` | Import profiles, file parsing, normalisation, validation, duplicate fingerprints, source transactions | core, accounts |
| `reconciliation` | Matching rules, matching engine (pure functions), match groups, exclusions, adjustments, statement, workflow | imports, approvals |
| `settlements` | Settlement-record validation, batch summaries, default settlement rules | imports, reconciliation |
| `suspense` | Suspense items, aging, escalation, resolution workflow, clearing-journal preparation | accounting, reconciliation, notifications |
| `audit` | Engagements, workpapers, findings, corrective actions | core, accounts |
| `fixed_assets` | Asset register, depreciation runs, disposals, register-to-GL control | accounting |
| `loans` | Loan listing upload, DPD classification, provisioning, GL control, provision journals | accounting |
| `deposits` | Deposit listing upload, product mapping, control totals vs GL | accounting |
| `regulatory` | Workpaper templates, formula evaluation, validation rules, sign-off | accounting |
| `integrations` | Keyed inbound file API, drop folders, integration logs | imports |
| `administration` | Backups, integrity checks, health, scheduled-job runner, load test | accounting, imports, integrations |
| `reporting` | Dashboard, report views, Excel/CSV/PDF export | all of the above (read-only) |

There are no circular imports: dependencies flow downwards in the table. Celery tasks (`imports.tasks`,
`reconciliation.tasks`, `suspense.tasks`, `accounting.tasks`, `administration.tasks`) wrap the same service functions used by the web and API layers.

## Money

* All amounts are `DecimalField(max_digits=20, decimal_places=2)` (PostgreSQL `NUMERIC`).
* `core.money.parse_amount` converts source text without floats; openpyxl numeric cells are converted via
  `repr()` → `Decimal`. Source values with more decimal places than configured are rejected, not rounded.
* Journal lines store transaction-currency and base-currency amounts; both must balance. Rounding uses the
  institution's configured mode (`HALF_UP` or `HALF_EVEN`).

## Accounting engine

* `Journal` → `JournalLine` (one-sided, positive amounts, enforced by a DB check constraint).
* `post_journal` runs in one transaction: lock journal row → validate (balanced, accounts active, posting-type,
  currency, effective dates, branch/department restrictions) → lock the financial period row (so a concurrent
  close waits) → check open period and backdating → bulk-insert `LedgerEntry` rows → mark posted.
* `LedgerEntry.line` is one-to-one with `JournalLine`, so a line can never be posted twice even if application
  checks were bypassed. Ledger entries and the activity log are protected by PostgreSQL triggers that reject
  `UPDATE`, `DELETE` and `TRUNCATE`.
* Reports are computed from `LedgerEntry` only. Imported core-banking GL data lives in `imports.SourceTransaction`
  and is never treated as LedgerGuard's own ledger.

## Imports

```
upload ─► inspect (type, size, macros, zip size) ─► store file + SHA-256
        ─► read_table (csv dialect sniffing / openpyxl read-only values)
        ─► normalize_row per row (dates, amounts, side, references, narration) ─► ImportRow(VALID|INVALID|WARNING)
        ─► fingerprints (content key + occurrence index) ─► DUPLICATE if already committed
        ─► optional corrections / exclusions (logged)
        ─► commit valid rows → SourceTransaction (unique (institution, fingerprint))
```

`signed_amount = credit − debit` in the source's own convention. Reconciliations decide which side is an inflow.

## Reconciliation

Each reconciliation has a *left* (external, e.g. bank statement) and *right* (internal, e.g. GL extract) side,
defined by source type, optional source system, account reference and currency, and an *inflow side* for each
(bank statement: credit; GL asset account: debit). All matching uses inflow-positive **flow** amounts.

**Scope**: transactions dated up to the period end that are not actively matched in another reconciliation —
so outstanding items carry forward automatically and settled items are never reused.

**Engine**: rules run in priority order; each strategy is a pure function returning proposals; a transaction is
used by at most one proposal per run. Classification:

* difference ≠ 0, ambiguous candidates, inconsistent settlement figures, suspected duplicates or fuzzy strategy
  → *manual review*;
* unique, zero difference, rule allows auto-accept, strategy is deterministic → *exact* (auto-accepted);
* otherwise → *high-confidence candidate* (needs a person to accept).

Amount-plus-date and fuzzy narration matches are never auto-accepted. Rejected pairings are remembered and not
proposed again. A partial unique index guarantees a transaction is in at most one active match.

**Statement equations** (all inflow-positive):

```
computed closing (each side) = opening + inflows in period − outflows in period
balance check                = reported closing − computed closing            (must be 0)

adjusted left  = left closing  + outstanding deposits − outstanding payments + excluded right items + left adjustments
adjusted right = right closing + unrecorded credits   − unrecorded charges   + excluded left items  + right adjustments
final difference = adjusted left − adjusted right                              (must be within tolerance)
```

where *outstanding deposits/payments* are unmatched right-side inflows/outflows and *unrecorded
credits/charges* are unmatched left-side inflows/outflows. Submission and approval require: zero balance-check
differences, no pending proposals, and `|final difference| ≤ institution.reconciliation_tolerance`; approval also
requires an approver other than the preparer.

## Settlements

Settlement reports are imported as `SETTLEMENT_REPORT` source transactions with gross, fee, tax, net,
settlement date, batch reference and reversal flag. The `FEE_NET` strategy groups records by batch and matches
the batch's net settlement against the bank credit (by reference or narration). The match difference is
computed from flow amounts, so if a profile maps the gross amount as the record amount, fees appear as a
difference that must be explained rather than being silently absorbed.

**Bulk sessions**: when the settlement account shows one bulk line per NIP/NIBSS session while the GL shows
individual transfers and charges, `SESSION_BULK` groups GL items by session (from a configured cut-off calendar
and each item's time of day, read from a column or derived from the NIP session ID) and direction, and matches the
group total to one bulk line. `BULK_SUBSET` finds consecutive runs or bounded combinations of items adding up to a
bulk line when no time is available (never auto-accepted). See `BULK_SETTLEMENT.md`.

## Month-end controls

* **Fixed assets** – depreciation runs and disposals create journals that follow the normal approval workflow; the
  register counts depreciation only after its journal is posted (`sync_statuses` aligns run/disposal status with
  journal outcomes). `register_control` compares register cost and accumulated depreciation with the GL accounts.
* **Loans / deposits** – listings are parsed strictly (any invalid row rejects the file), stored as snapshots per
  date (a signed-off snapshot cannot be replaced) and compared with GL balances as at the snapshot date.
* **Regulatory** – each line sums posted ledger entries selected by regulatory code, subcategory, account code
  and/or category, or evaluates a formula over other lines; validation rules compare expressions with a tolerance.
