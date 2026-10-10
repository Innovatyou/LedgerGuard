# Performance

Measured with `python manage.py load_test --rows N` (creates its own scratch institution; never run it against a
production institution). Environment: single container, 4 vCPU, PostgreSQL 16 on the same host, no tuning.

| Volume (each side) | Upload + validate | Commit | Reconciliation run | Statement |
|---|---|---|---|---|
| 20,000 rows (0.9 MB) | ~3–4 s | ~4.5 s | 5.7 s | 2.2 s |
| 100,000 rows (4.6 MB) | ~20–23 s | ~27–30 s | 37.6 s | 13.9 s |

The 100k run produced 87,266 auto-accepted matches, 9,739 proposals for review and 2,995 unmatched items, with a
final reconciliation difference of 0.00.

## Behaviour at volume

* Files over 2 MB are validated in the background worker; batches over 20,000 rows are committed in the background;
  reconciliations with more than 20,000 transactions in scope run in the background. The page asks the user to refresh.
* Gunicorn's request timeout is 300 s (`deploy/gunicorn.conf.py`); nginx's proxy timeout matches.
* The default row limit per file is 500,000 (`LEDGERGUARD_MAX_IMPORT_ROWS`).

## Optimisations made during testing

* Match groups and items are bulk-inserted per rule (reconciliation of 20k rows: 48.6 s → 5.7 s).
* Committed import rows are updated with one set-based statement rather than an ID list.

## Not yet measured

* Concurrent users (e.g. 50 users during month-end close). Run a concurrency test (e.g. Locust) against a
  production-sized environment before go-live.
* Reports over multi-year ledgers with millions of entries; add a materialised balance table if needed.
