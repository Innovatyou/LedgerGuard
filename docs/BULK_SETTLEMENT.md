# Bulk (session) settlement reconciliation

## The problem

The core banking GL posts every NIP / NIBSS transfer individually, for example on the NIP settlement GL:

| Time | Reference | Narration | Debit | Credit |
|---|---|---|---|---|
| 07:10 | 000013261017071000… | NIP inward – Chike | 20,000.00 | |
| 08:30 | 000013261017083000… | NIP inward – Bola | 100,000.00 | |
| 11:45 | 000013261017114500… | NIP inward – Acme Ltd | 200,000.00 | |
| 13:00 | 000013261017130000… | NIP outward – Ada | | 50,000.00 |
| 13:00 | 000013261017130000… | NIP transfer charge | | 10.75 |
| 13:00 | 000013261017130000… | VAT on NIP charge | | 0.81 |

The settlement account statement shows one bulk line for each settlement session (often 8 or more a day):

| Date | Narration | Debit | Credit |
|---|---|---|---|
| 17-Oct | NIP inward settlement, session 2 | | 320,000.00 |
| 17-Oct | NIP outward settlement, session 3 | 50,011.56 | |

The GL rows have no session ID and no shared batch reference, so reference matching cannot pair them.

## How LedgerGuard matches them

### 1. Find the time of each GL item

A session is a window of time, so each GL item needs a time of day. LedgerGuard reads it from the first source
available:

1. a **Transaction / posting time** column mapped in the import profile;
2. a time inside the date cell (`17/10/2026 14:32:05`);
3. the **reference**, when the import profile's *Derive time from reference* option is set:
   * **NIP session ID**: the 30-digit NIP session ID is commonly built as a 6-digit institution code, the
     date and time `YYMMDDHHMMSS`, and a 12-digit number. LedgerGuard searches the transaction ID, the external
     reference and the narration for such a number. It accepts the time only when the embedded date is within
     three days of the transaction date, so an unrelated 30-digit number is not misread.
   * **Custom pattern**: for an internal reference that embeds the time, a regular expression with named groups
     `HH` and `MM` (optionally `SS`, `YY`, `MO`, `DD`).

Check a sample of your GL references against your switch records before relying on the NIP option. The import
row keeps the source of the time (`derived from reference`) for audit.

### 2. Session bulk rule (`SESSION_BULK`)

Configure a matching rule with:

* **Session cut-off times**: the institution's settlement session cut-offs, for example `03:00, 06:00, …`. An item
  belongs to the first session whose cut-off is at or after its time; items after the last cut-off belong to
  session 1 of the next day. Use the cut-offs published to your bank by NIBSS; LedgerGuard ships no default.
* **Bulk side**: the side of the reconciliation that shows the bulk lines (normally the left, the statement).
* **Max group size**: set it above the largest number of items in one session (e.g. 5,000 or more).
* **Date tolerance**: days allowed between the session date and the bulk line date.
* **Fee keywords** (optional): narration words of the charge lines, e.g. `CHARGE, FEE, VAT`.
* **Boundary minutes** (optional): how close to a cut-off an item may be and still have settled in the
  neighbouring session.

For each session and direction (inward / outward) the rule tries, in this order:

| Step | What it compares | Result |
|---|---|---|
| 1 | All items in the session (principal **and** charges) against one bulk line | Auto-accepted when unique and exact (if the rule allows it) |
| 2 | Principal items against one bulk line, and charge items against another bulk line (fee keywords set) | As step 1 |
| 3 | The session after moving items close to a cut-off into or out of the neighbouring session | Manual review |
| 4 | Inward minus outward items of a session against one net line | Manual review |

### 3. Bulk amount finder (`BULK_SUBSET`)

Use this rule for GL items that have no time and no NIP session ID. For each unmatched bulk line it considers
the unmatched items in the same direction, dated from the line date minus the date tolerance up to the line date,
in file order (date, time, row):

1. **Consecutive run.** It looks for a run of consecutive items whose total equals the bulk amount. A
   chronological GL export lists a session's items together, so a single such run is usually the session. The
   result is a high-confidence candidate that a person accepts.
2. **Free search.** If no run fits, it searches for any combination of items that adds up exactly. The search
   is bounded to the nearest 60 items and a fixed number of steps. Results are always **manual review**, and
   are flagged when another combination also fits or the search limit was reached.

The amount finder is never auto-accepted. A combination that adds up is evidence for a person to confirm, not
proof.

### Recommended rule order

1. Exact reference (one-to-one items that do carry the same reference)
2. Session bulk
3. Bulk amount finder
4. Reversal pairs

The default settlement rules (*Settlements → Install default settlement rules*) include the amount finder. Add
the session bulk rule with your institution's cut-off times. Rule changes follow the configuration
maker-checker.

## Session summary report

*Reconciliation → Session summary* lists, for each session date, session number and direction:

* the number and total of GL items;
* how many items are matched (accepted) and proposed;
* the bulk lines matched and their total, and the difference;
* the status: matched, proposed, partly matched or unmatched.

It also lists bulk lines not yet matched, and counts items that have no time and so cannot be placed in a
session. The report exports to Excel, CSV and PDF.

## Performance

Measured with in-memory engine data: 60,000 GL items in 16 session groups. The session bulk rule matched all 16
bulk lines in about 0.6 s, and the amount finder (consecutive-run path) in about 0.3 s.
