# User acceptance test plan (pilot institution)

Run in a dedicated UAT environment with the institution's own (anonymised where policy requires) data, in parallel
with the existing process for at least one full month-end close.

| # | Area | Scenario | Evidence / pass criteria | Owner |
|---|---|---|---|---|
| 1 | Set-up | Load the institution's own chart of accounts; map regulatory codes | CoA reviewed and signed off by Financial Controller | Finance |
| 2 | Imports | Import 3 months of statements for every correspondent bank account (actual formats) | All files parse; invalid rows explained; no silent drops | Recon |
| 3 | Imports | Import core-banking GL extracts for the same accounts and period | Totals agree with core-banking trial balance | Finance |
| 4 | Reconciliation | Reconcile each account for each month | LedgerGuard statements agree with the existing manual reconciliations; differences explained | Recon + reviewer |
| 5 | Settlement | Import NIP/NIBSS, POS, ATM settlement reports for one month | Gross/fee/net validated; batches match settlement-account credits | Settlement |
| 5a | Settlement | Reconcile one week of the NIP settlement account (bulk session lines) against the individual GL transfers and charges, with the institution's session cut-offs configured | Times derived from GL references verified on a sample against switch records; each session's GL total agrees with its bulk line or the difference is explained; session summary agrees with the manual reconciliation | Settlement |
| 6 | Suspense | Load current suspense listing; run aging and escalations | Aging matches the institution's suspense report | Finance |
| 7 | Accounting | Post a month of manual journals (accruals, reclasses, reversals) through maker-checker | TB, IS and SoFP agree to an independent recomputation | Finance |
| 8 | Period close | Close the month; attempt a posting into it; reopen with reason | Postings rejected when closed; reopen recorded | CFO |
| 9 | Fixed assets | Load the asset register; run depreciation | Depreciation agrees with the current register to the naira; register ties to GL | Finance |
| 10 | Loans/deposits | Load month-end loan and deposit listings | Classification/provisioning agrees with the credit risk report; control totals agree with GL | Finance / Risk |
| 11 | Regulatory | Configure the institution's return lines; prepare one month | Values agree with the return actually filed for that month | Compliance |
| 12 | Access | Each role performs only its permitted actions; branch-restricted users see only their branch | No unauthorised action possible; activity log complete | IT security |
| 13 | Integrations | Core banking delivers files via API or drop folder | Files ingested, logged, rejected files reported | IT |
| 14 | Resilience | Backup, restore drill, worker restart during import | Restore within RTO; no data loss; integrity check passes | IT |
| 15 | Exports | Export each report to Excel/PDF | Figures identical to screen; files open cleanly | Finance |

Exit criteria: all scenarios passed or accepted with documented workarounds; sign-off by CFO, Head of Internal Audit,
Chief Information Security Officer and Chief Compliance Officer.
