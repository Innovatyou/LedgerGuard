# LedgerGuard

**Banking Accounting, Reconciliation, Audit & Financial Control Platform**
*Every Transaction Accounted For. Every Balance Verified.*

LedgerGuard is a Django application for Nigerian commercial banks, microfinance banks (MFBs), other financial
institutions (OFIs) and finance companies. It sits alongside the core banking system and provides a controlled
double-entry accounting engine, Excel/CSV import of bank statements, GL extracts and settlement reports, a
configurable reconciliation engine, settlement and suspense management, fixed assets, loan and deposit control
schedules, regulatory workpapers, internal-audit workflow, maker-checker approvals, an append-only activity trail,
controlled integrations and financial reporting with Excel, CSV and PDF export.

It does **not** replace the core banking system, hold live customer deposit ledgers, execute payments or submit
regulatory returns.

---

## What is implemented

Every item below is backed by working code and automated tests (`pytest`, 138 tests on PostgreSQL).

| Area | Implemented |
|---|---|
| **Multi-institution** | Institutions, branches, departments/cost centres; every record is institution-scoped and every query is filtered server-side; cross-tenant access tested for web views, API, session tampering and the `X-Institution` header. |
| **Users & access** | Custom user model; 13 built-in roles plus custom roles; 49 action-level permissions evaluated per institution; branch-restricted memberships enforced in journals, reports, assets, portfolio schedules and the API; maker-checker enforcement; login throttling/lockout; TOTP multifactor authentication (RFC 6238, replay-protected) with per-institution enforcement; session idle timeout; forced password change on first sign-in and after an administrator reset; API tokens. |
| **Chart of accounts** | Hierarchical accounts with category, subcategory, type, normal balance, currency, branch/department restriction, effective dating, reconciliation/control/suspense flags, cash-flow class, financial-statement and regulatory mapping. Starter templates for commercial banks, MFBs and OFIs (flagged as requiring validation). Accounts with postings cannot have code, category, normal balance, currency or type changed, and cannot be deleted. Account, matching-rule and institution-setting changes are applied only after independent approval (change requests). |
| **Double-entry engine** | Journal types (manual, adjustment, accrual, prepayment, depreciation, reclassification, reversal, imported, recurring, year-end); draft → submitted → approved → posted workflow; reject with reason; balanced in transaction **and** base currency; atomic posting with row locks on the journal and period; duplicate posting blocked in code and by a one-to-one DB constraint; immutable ledger (PostgreSQL trigger blocks UPDATE/DELETE); controlled reversal journals; closed-period rejection; backdating control; idempotency keys; gap-free journal numbering; period close/reopen with reasons and independent re-opener; year-end closing journal generator; recurring journal templates (approved before activation, generated on schedule, idempotent). |
| **Financial reports** | Trial balance, general ledger with running balance, income statement, statement of financial position (with A = L + E check), all from posted entries; Excel, CSV and PDF export with formula-injection protection; export permission and export logging. |
| **Imports** | CSV and XLSX (macro-enabled workbooks rejected, zip-bomb guard, read-only/values-only parsing); reusable import profiles with column mapping, header row, sheet, date formats, three amount layouts (separate DR/CR, signed, amount + indicator) and per-source sign convention; preview; row-level validation with explicit invalid rows; row correction with note and audit log; row exclusion; partial commit only on explicit choice; SHA-256 file checksum; the same file cannot be committed twice; row fingerprints make overlapping files import only new rows; identical rows within a file are flagged as suspected duplicates; original source text preserved; retention purge keeps checksums. Background validation via Celery for large files. |
| **Reconciliation** | Configurable rules with priority, date and amount tolerance, auto-accept flag and a rule snapshot stored on every match. Strategies: exact reference, external ID, amount + date window, narration + amount, composite key, one-to-many, many-to-one, gross/fee/net settlement, reversal pairs, and fuzzy narration (always manual review). Categories: exact, high-confidence, manual review, manual, unmatched, suspected duplicate, excluded-with-reason. Per-side sign conventions (a bank-statement credit and a GL debit are both inflows). Manual matching, unmatch, exclusions with reasons, adjustments, raise suspense item from exception. Workflow draft → processing → in review → submitted → approved → closed / reopened. Statement with opening/movements/closing check for both sides, outstanding deposits/payments, unrecorded credits/charges, duplicates, adjustments and final difference; submission and approval are blocked when balances don't reconcile to movements, reviews are pending, or the difference exceeds the institution's tolerance. Outstanding items carry forward; matched items cannot be reused. |
| **Settlements** | Settlement reports (NIP, NIBSS, POS, ATM, card, interbank, processor) with gross, fee, tax, net, settlement date, batch and reversal fields kept separately; per-record gross − fee − tax = net validation; batch summary with match status; default settlement matching rules. |
| **Suspense** | Suspense items on designated suspense accounts; assignment, investigation notes, evidence; configurable aging buckets; aging report by account; escalation rules with in-app (and optional e-mail) notifications; resolution proposed and approved by different users; clearing journals are prepared as drafts and must be posted through the normal workflow before an item can be resolved — nothing is cleared automatically. |
| **Internal audit** | Engagements (planning → fieldwork → reporting → closed), workpapers with preparer/reviewer sign-off, findings (condition/criteria/cause/effect/recommendation), management responses, corrective actions with owners and due dates, audit verification, and closure only after verification. External auditor role is read-only. |
| **Fixed assets** | Asset categories mapped to cost, accumulated-depreciation, expense and gain/loss accounts; register with cost, residual value, useful life, straight-line or reducing-balance method and migrated opening depreciation; optional capitalisation journal; monthly depreciation runs prepared as journals (full-month convention, never below residual value, one run per period); disposals with gain/loss; register-to-GL control by account. Depreciation counts only once its journal is posted. |
| **Loan portfolio control** | Upload a core-banking loan listing (CSV/XLSX, strict all-or-nothing validation); configurable days-past-due bands with provisioning rates (defaults by institution type, flagged for validation against CBN prudential guidelines and IFRS 9); classification, provisioning, NPL ratio, analysis by band/product/branch; comparison of principal, interest and allowance with GL control accounts; provision adjustment journal; independent sign-off. |
| **Deposit control** | Upload a deposit listing; product-to-GL mapping (changes approval-controlled); listing vs GL by product (accounts shared by several products compared once); unmapped products, overdrawn deposit accounts and dormant balances surfaced; independent sign-off. |
| **Regulatory workpapers** | Configurable templates (lines from ledger balances by regulatory code, subcategory, account code or category; closing balance or movement; formulas; manual inputs); safe formula evaluator (no `eval`); validation rules with tolerance; preparer/reviewer sign-off; Excel/CSV/PDF export. An illustrative monthly statement-of-financial-position template is included. Returns are never submitted by LedgerGuard. |
| **Integrations** | Inbound HTTPS file API (hashed keys shown once, key rotation, IP allow-lists, rate limiting) and server drop folders (e.g. SFTP targets) feeding import profiles through the same validation and duplicate controls; optional auto-commit of clean files; every call logged. |
| **Administration** | Verified backups (`pg_dump` + media, SHA-256, `pg_restore --list` check, retention); ledger integrity checks; system health page for platform operators; scheduled jobs via Celery beat or `run_scheduled_jobs` for cron. |
| **Evidence & audit trail** | SHA-256 checksummed attachments on journals, reconciliations, suspense items, engagements and findings; approval history on every controlled record; append-only activity log with user, action, object, details and IP address. Ledger entries, approval records and the activity log are protected by PostgreSQL triggers and by the application database role having no UPDATE/DELETE/TRUNCATE/DDL rights on them. |
| **API** | Django REST Framework under `/api/v1/` (session or token auth, `X-Institution` header): branches, departments, accounts and matching rules (approval-controlled), periods, journals (submit/approve/reject/post/reverse), import profiles, import batches (upload/validate/commit/rows/correct), source transactions, reconciliations (run/statement/matches/accept/reject/manual match/exclude/adjust/submit/approve/reopen/close), suspense items and aging, settlement summary, fixed assets, audit engagements and findings (read-only), financial reports, and the keyed inbound integration endpoint. |
| **Security & operations** | Content-Security-Policy with no inline scripts, HSTS, frame denial; secrets from environment; separate migration (owner) and application database roles; JSON logging with request IDs; health endpoints; Dockerfile, gunicorn and nginx configuration; docker-compose stack (web, worker, beat, PostgreSQL with role split, Redis) verified end to end; CI with pip-audit, ruff, migration and deploy checks, and tests. Performance measured at 100,000 rows per side (`docs/PERFORMANCE.md`). |

### Before go-live

Engineering work is complete, but production use still requires institution-side steps that cannot be done in code:
UAT with the institution's real files, accounting sign-off of the chart of accounts, provisioning bands and
regulatory mappings, an independent penetration test, production infrastructure (managed PostgreSQL with PITR,
TLS, off-site backups, monitoring) and a restore drill. See [`docs/PRODUCTION_READINESS.md`](docs/PRODUCTION_READINESS.md)
and [`docs/UAT_PLAN.md`](docs/UAT_PLAN.md). The specification text supplied ended partway through section 13; the
modules named in section 5 but not specified in detail were built to standard practice and should be reviewed
against any later sections.

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
cp .env.example .env   # set DJANGO_SECRET_KEY, DJANGO_ALLOWED_HOSTS and the three database passwords
docker compose --env-file .env up -d --build
docker compose exec web python manage.py seed_demo   # evaluation only – never in production
```

The database container creates a schema-owner role (used only for migrations) and a restricted application role.
For production deployment, backups, upgrades and monitoring see [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

### Tests and checks

```bash
pytest --create-db         # 138 tests, PostgreSQL required (uses DATABASE_URL)
pip-audit -r requirements.txt
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
8. **Month-end controls**: run depreciation, load loan and deposit listings and agree them to the GL, prepare regulatory workpapers and have them reviewed.
9. **Report**: trial balance, general ledger, income statement, financial position, reconciliation statement and suspense aging, exportable to Excel/CSV/PDF.

Sample bank statement, GL and settlement layouts are created by `seed_demo` (see the import profiles it creates).

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) – modules, dependencies, data model, accounting and reconciliation rules
- [`docs/SECURITY_AND_TENANCY.md`](docs/SECURITY_AND_TENANCY.md) – tenant-isolation model, access control, MFA/SSO guidance, production hardening
- [`docs/IMPLEMENTATION_STATUS.md`](docs/IMPLEMENTATION_STATUS.md) – implementation plan, status by requirement, and next steps
- [`docs/PRODUCTION_READINESS.md`](docs/PRODUCTION_READINESS.md) – what is done, what the institution must still do, known limitations
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) – deployment, upgrades, scheduled jobs, backups and restore, monitoring, incidents
- [`docs/UAT_PLAN.md`](docs/UAT_PLAN.md) – pilot acceptance scenarios and exit criteria
- [`docs/BULK_SETTLEMENT.md`](docs/BULK_SETTLEMENT.md) – reconciling NIP/NIBSS bulk session settlements against individual GL items
- [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md) – measured volumes and timings

## Supported runtime

Python 3.12–3.13, Django 5.2 LTS, PostgreSQL 14–16 (production and tests), Redis 7 for Celery and caching.
Dependency versions are pinned in `requirements.txt`.
