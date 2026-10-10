# LedgerGuard

**Banking Accounting, Reconciliation, Audit & Financial Control Platform**
*Every Transaction Accounted For. Every Balance Verified.*

LedgerGuard is a Django application for Nigerian commercial banks, microfinance banks (MFBs), other financial
institutions (OFIs) and finance companies. It sits alongside the core banking system and provides a controlled
double-entry accounting engine, Excel/CSV import of bank statements, GL extracts and settlement reports, a
configurable reconciliation engine, suspense-account management, internal-audit workflow, maker-checker approvals,
an append-only activity trail and financial reporting with Excel, CSV and PDF export.

It does **not** replace the core banking system, hold live customer deposit ledgers, execute payments or submit
regulatory returns.

---

## What is implemented

Every item below is backed by working code and automated tests (`pytest`, 112 tests on PostgreSQL).

| Area | Implemented |
|---|---|
| **Multi-institution** | Institutions, branches, departments/cost centres; every record is institution-scoped and every query is filtered server-side; cross-tenant access tested for web views, API, session tampering and the `X-Institution` header. |
| **Users & access** | Custom user model; 13 built-in roles plus custom roles; 37 action-level permissions evaluated per institution; maker-checker enforcement; login throttling/lockout; TOTP multifactor authentication (RFC 6238, replay-protected) with per-institution enforcement; session idle timeout; API tokens. |
| **Chart of accounts** | Hierarchical accounts with category, subcategory, type, normal balance, currency, branch/department restriction, effective dating, reconciliation/control/suspense flags, cash-flow class, financial-statement and regulatory mapping. Starter templates for commercial banks, MFBs and OFIs (flagged as requiring validation). Accounts with postings cannot have code, category, normal balance, currency or type changed, and cannot be deleted. |
| **Double-entry engine** | Journal types (manual, adjustment, accrual, prepayment, depreciation, reclassification, reversal, imported, recurring, year-end); draft → submitted → approved → posted workflow; reject with reason; balanced in transaction **and** base currency; atomic posting with row locks on the journal and period; duplicate posting blocked in code and by a one-to-one DB constraint; immutable ledger (PostgreSQL trigger blocks UPDATE/DELETE); controlled reversal journals; closed-period rejection; backdating control; idempotency keys; gap-free journal numbering; period close/reopen with reasons and independent re-opener; year-end closing journal generator. |
| **Financial reports** | Trial balance, general ledger with running balance, income statement, statement of financial position (with A = L + E check), all from posted entries; Excel, CSV and PDF export with formula-injection protection; export permission and export logging. |
| **Imports** | CSV and XLSX (macro-enabled workbooks rejected, zip-bomb guard, read-only/values-only parsing); reusable import profiles with column mapping, header row, sheet, date formats, three amount layouts (separate DR/CR, signed, amount + indicator) and per-source sign convention; preview; row-level validation with explicit invalid rows; row correction with note and audit log; row exclusion; partial commit only on explicit choice; SHA-256 file checksum; the same file cannot be committed twice; row fingerprints make overlapping files import only new rows; identical rows within a file are flagged as suspected duplicates; original source text preserved; retention purge keeps checksums. Background validation via Celery for large files. |
| **Reconciliation** | Configurable rules with priority, date and amount tolerance, auto-accept flag and a rule snapshot stored on every match. Strategies: exact reference, external ID, amount + date window, narration + amount, composite key, one-to-many, many-to-one, gross/fee/net settlement, reversal pairs, and fuzzy narration (always manual review). Categories: exact, high-confidence, manual review, manual, unmatched, suspected duplicate, excluded-with-reason. Per-side sign conventions (a bank-statement credit and a GL debit are both inflows). Manual matching, unmatch, exclusions with reasons, adjustments, raise suspense item from exception. Workflow draft → processing → in review → submitted → approved → closed / reopened. Statement with opening/movements/closing check for both sides, outstanding deposits/payments, unrecorded credits/charges, duplicates, adjustments and final difference; submission and approval are blocked when balances don't reconcile to movements, reviews are pending, or the difference exceeds the institution's tolerance. Outstanding items carry forward; matched items cannot be reused. |
| **Settlements** | Settlement reports (NIP, NIBSS, POS, ATM, card, interbank, processor) with gross, fee, tax, net, settlement date, batch and reversal fields kept separately; per-record gross − fee − tax = net validation; batch summary with match status; default settlement matching rules. |
| **Suspense** | Suspense items on designated suspense accounts; assignment, investigation notes, evidence; configurable aging buckets; aging report by account; escalation rules with in-app (and optional e-mail) notifications; resolution proposed and approved by different users; clearing journals are prepared as drafts and must be posted through the normal workflow before an item can be resolved — nothing is cleared automatically. |
| **Internal audit** | Engagements (planning → fieldwork → reporting → closed), workpapers with preparer/reviewer sign-off, findings (condition/criteria/cause/effect/recommendation), management responses, corrective actions with owners and due dates, audit verification, and closure only after verification. External auditor role is read-only. |
| **Evidence & audit trail** | SHA-256 checksummed attachments on journals, reconciliations, suspense items, engagements and findings; approval history on every controlled record; append-only activity log (PostgreSQL trigger) with user, action, object, details and IP address. |
| **API** | Django REST Framework under `/api/v1/` (session or token auth, `X-Institution` header): branches, departments, accounts, periods, journals (submit/approve/reject/post/reverse), import profiles, import batches (upload/validate/commit/rows/correct), source transactions, matching rules, reconciliations (run/statement/matches/accept/reject/manual match/exclude/adjust/submit/approve/reopen/close), suspense items and aging, settlement summary, audit engagements and findings (read-only), and financial reports. |
| **Operations** | Environment-based settings; JSON structured logging with request IDs; `/health/` and `/health/ready/`; Dockerfile and docker-compose (web, worker, beat, PostgreSQL, Redis); GitHub Actions CI (ruff, migrations check, deploy check, tests). |

### Not yet implemented

The specification describes further modules that are **not** part of this build: loan and deposit control
schedules, fixed-asset register and depreciation runs, configurable regulatory (CBN/NDIC) workpapers, external
system connectors, backup administration, SSO, and filtering data by the branch recorded on a user's
membership (the branch is stored but not yet enforced). The specification text supplied for this build ended partway
through section 13 (settlement reconciliation), so requirements after that point have not been seen. See
[`docs/IMPLEMENTATION_STATUS.md`](docs/IMPLEMENTATION_STATUS.md).

---

## Quick start (local)

Requirements: Python 3.12+, PostgreSQL 14+ (Redis optional for local use).

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # then edit: secret key, DATABASE_URL, DJANGO_DEBUG=True, DJANGO_SECURE_COOKIES=False, CELERY_TASK_ALWAYS_EAGER=True
python manage.py migrate
python manage.py seed_demo    # demo institution with sample data; prints a one-time password
python manage.py runserver
```

Sign in at <http://localhost:8000/> as `recon`, `reviewer`, `accountant`, `finance`, `cfo`, `auditor`, `admin`,
etc. Platform superusers are created with `python manage.py createsuperuser` and can add institutions in
`/django-admin/`.

### Docker

```bash
cp .env.example .env   # set DJANGO_SECRET_KEY and DJANGO_ALLOWED_HOSTS
export POSTGRES_PASSWORD='choose-a-strong-password'
docker compose up --build
docker compose exec web python manage.py seed_demo
```

### Tests and checks

```bash
pytest --create-db         # 112 tests, PostgreSQL required (uses DATABASE_URL)
ruff check . && ruff format --check .
python manage.py makemigrations --check --dry-run
```

---

## Using it

1. **Set up**: Institution settings (currency, rounding, tolerance, backdating limit, MFA), branches, users and roles.
2. **Chart of accounts**: apply a starter template and adapt it; mark accounts that need reconciliation and suspense accounts.
3. **Periods**: generate the financial year. Journals can only be posted into open periods.
4. **Journals**: prepare → submit → independent approval → post. Corrections are reversals, never edits.
5. **Imports**: create an import profile per file layout, upload, review the preview and row errors, correct or exclude, commit.
6. **Reconcile**: define matching rules, create a reconciliation (left = external source, right = internal records, with opening/closing balances), run matching, review candidates, investigate exceptions (exclude, adjust, raise suspense, manual match), submit, and have a reviewer approve. Roll forward to the next period from the approved reconciliation.
7. **Suspense**: assign, investigate, prepare a clearing journal, propose a resolution, approve.
8. **Report**: trial balance, general ledger, income statement, financial position, reconciliation statement and suspense aging, exportable to Excel/CSV/PDF.

Sample bank statement, GL and settlement layouts are created by `seed_demo` (see the import profiles it creates).

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) – modules, dependencies, data model, accounting and reconciliation rules
- [`docs/SECURITY_AND_TENANCY.md`](docs/SECURITY_AND_TENANCY.md) – tenant-isolation model, access control, MFA/SSO guidance, production hardening
- [`docs/IMPLEMENTATION_STATUS.md`](docs/IMPLEMENTATION_STATUS.md) – implementation plan, status by requirement, and next steps

## Supported runtime

Python 3.12–3.13, Django 5.2 LTS, PostgreSQL 14–16 (production and tests), Redis 7 for Celery and caching.
Dependency versions are pinned in `requirements.txt`.
