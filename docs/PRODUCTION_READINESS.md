# Production readiness

Status as of this release. "Done" items are implemented and covered by automated tests or were verified as
described. Items in the second table cannot be completed in code; they need the institution.

## Engineering – done

| Area | Status | Evidence |
|---|---|---|
| Functional scope (spec sections 1–13 + modules listed in section 5) | Done | 138 automated tests; `docs/IMPLEMENTATION_STATUS.md` |
| Maker-checker on transactions, reconciliations, suspense, periods, workpapers, configuration changes | Done | `tests/test_accounting.py`, `test_reconciliation.py`, `test_controls.py`, `test_modules.py` |
| Tenant isolation (views, API, header, session tampering) and branch scoping | Done | `tests/test_security.py`, `tests/test_controls.py` |
| Immutable ledger and audit trail | Done | DB triggers + app role without UPDATE/DELETE/TRUNCATE/DDL on those tables; verified against PostgreSQL |
| Least-privilege database roles | Done | `deploy/postgres/create_roles.sql`, `harden_database`; verified the app role cannot alter/drop triggers |
| Authentication hardening | Done | Lockout, TOTP MFA, forced password change, 12-char policy, session timeout |
| Web security headers | Done | CSP without inline scripts, HSTS (configurable), X-Frame-Options DENY, nosniff, no-store on auth/API |
| Dependency vulnerabilities | Done | `pip-audit` clean (DRF upgraded to 3.17.2); runs in CI |
| Container build and stack | Done | Image built; compose stack (web, worker, beat, PostgreSQL with role split, Redis) started, migrated, seeded, healthy; login and exports verified over HTTP |
| Backups | Done | `backup` command: pg_dump + media, SHA-256, verification, retention; tested against PostgreSQL |
| Integrity monitoring | Done | `check_integrity` (scheduled daily) |
| Performance | Done for single-user volume | `docs/PERFORMANCE.md` (100k rows per side) |
| Operations documentation | Done | `docs/OPERATIONS.md`, `deploy/nginx.conf`, `deploy/gunicorn.conf.py` |

## Required before go-live – institution / third parties

| Item | Why it cannot be done in code | Suggested owner |
|---|---|---|
| UAT with real bank statements, core-banking extracts and settlement files (`docs/UAT_PLAN.md`) | Real file layouts and data are needed to validate parsing and matching | Finance + Recon |
| Accounting sign-off: chart of accounts, provisioning bands, regulatory line mappings | Must reflect the institution's IFRS policies and current CBN/NDIC formats | CFO / Financial Controller |
| Independent penetration test and code review | Required by bank IT risk policies; must be independent | CISO |
| Infrastructure: managed PostgreSQL with PITR, Redis, TLS certificates, off-site backups, monitoring/SIEM | Environment-specific | IT Infrastructure |
| Restore drill and RTO/RPO confirmation | Needs the production-like environment | IT Infrastructure |
| Concurrency/load test at expected month-end user numbers | Needs the target environment | IT |
| Data-protection assessment (Nigeria Data Protection Act 2023) and hosting approval | Legal/compliance determination | DPO / Compliance |
| SSO with the institution's identity provider (optional) | Needs IdP configuration; local accounts + TOTP work without it | IT Security |
| Change the demo credentials / do not load demo data in production | Operational | IT |

## Known limitations

* Reconciliation matching for one-to-many/many-to-one relies on shared references; arbitrary subset-sum matching is
  not attempted (by design – it is not verifiable).
* Regulatory templates are workpapers; the bundled template is illustrative and must be replaced with the
  institution's validated return definitions. No submission to regulators.
* PostgreSQL row-level security is not enabled (tenant isolation is enforced in application queries and tested);
  dedicated deployments per bank are recommended for commercial banks.
* Reports are computed from ledger entries on demand; very large multi-year ledgers may need a balance snapshot table.
