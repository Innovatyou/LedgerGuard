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

* **Session cut-off times**: the institution's settlement session cut-offs, for example `06:00, 09:00, …, 24:00`.
  An item belongs to the first session whose cut-off is at or after its time; `24:00` means the end of the day.
  Items after the last cut-off belong to session 1 of the next day. Use the cut-offs published to your bank by
  NIBSS, or work them out from a month of matched data as shown in the worked example below. LedgerGuard ships no
  default.
* **Session label**: when the statement narration names the session (`31.08.2026 11th SESSION NIP SETT`),
  choose *NIBSS narration*. Each bulk line then settles exactly the session it names, even when the bank posts it
  the next day. Lines naming the same session with equal and opposite amounts (a bank posting and its
  *REVERSAL OF ENTRY*) are paired with each other first. A custom pattern is available for other formats.
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

## Worked example: a correspondent-bank settlement account and a core banking interbank GL

This example uses one month of a microfinance bank's real data (September 2026): 832 GL lines and 716 statement
lines.

**What the files showed**

* **GL**: every transfer is one line, and the *Transaction Date* column has the time to the second. Outward
  transfers ("Mob trans to …") include the ₦5.38 charge in one amount, with a 12-digit instrument number. A
  failed transfer is reversed as "Reversal of (Mob trans to …)", with the narration truncated.
* **Statement**: inward transfers arrive as one credit per NIBSS session, labelled
  `DD.MM.YYYY Nth SESSION NIP SETT`, with up to 12 sessions a day. Each outward transfer appears as two debits,
  the principal and the commission, and both carry `PRF,T,<4 digits><instrument number>` in the narration.
* **Session windows**: grouping the GL inward items by time showed clock windows that match the NIBSS session
  numbers: 1 up to 06:00, 2 to 09:00, then one per hour to 15:00, 9 to 17:00, 10 to 19:00, 11 to 21:00 and 12 to
  midnight. Some items were posted a few minutes after the cut-off of the session that settled them.

**Configuration used**

| Setting | Value |
|---|---|
| GL import profile | Header row 11; *Transaction Date* → transaction date (date and time); *Financial Date* → posting date; *Reference No.* → transaction ID; *Instrument No.* → external reference; DR / CR columns |
| Statement import profile | Header row 17; *Post Date*, *Narration*, *Debit*, *Credit*; narration reference pattern `PRF,T,\d{4}(\d{12})` |
| Rule 1 – many-to-one | External reference ↔ external reference, date tolerance 3, auto-accept: the transfer and commission debits on the statement equal the GL line |
| Rule 2 – reversal pairs | Right side by *Narration*, date tolerance 7: a GL failed transfer and its "Reversal of (…)" entry |
| Rule 3 – session bulk | Cut-offs `06:00, 09:00, 10:00, 11:00, 12:00, 13:00, 14:00, 15:00, 17:00, 19:00, 21:00, 24:00`; label *NIBSS narration*; boundary 15 minutes; date tolerance 2; max group 5,000; auto-accept |
| Rule 4 – bulk amount finder | Date tolerance 2 |
| Rule 5 – amount and date | Date tolerance 2: single late-posted items that equal one session credit |

**Result** (one run, about 1 second):

| | Groups |
|---|---|
| Auto-accepted (exact) | 362: 175 outward transfers with their commission, 18 GL reversals, 169 inward sessions |
| High-confidence candidates | 16 |
| Manual review | 29, mostly items posted after the cut-off and sessions settled together |

769 of the 832 GL lines were matched or proposed. The rest are real exceptions, which the reconciliation and
the session summary make visible:

* Sessions of 30 September not settled until October: outstanding at month end.
* 31 August sessions credited on 1 September whose GL items are in the August GL.
* Days where the bank settled more than the GL recorded, e.g. 27 September session 4: GL items missing or
  posted later.
* Commission-refund credits on the statement with no reference.
* Card transactions, investments and own-account transfers that have no counterpart on this GL account.

A statement export that declares a wrong sheet size (only cell `A1`) is read in full.

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
