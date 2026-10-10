# Bookkeeping, Accounting, and Auditing Clerks — Task 1


This task asks the agent to act as the bookkeeper supporting Flower Rose's April 2012 month-end close, reconciling the operating bank account as of April 30, 2012 from the bank export and the cash ledger, classifying each reconciling item, and writing a controller memo that names the corrections to post now and the items that should remain open for follow-up.

---

## 1. Capability Professionals Want Agents to Handle

**ONET Task Summary:** *Reconcile records of bank transactions.*

**Expert Reported Automation Desire:** 3.75 (scale: 0–5, from WorkBank)

In practice this means tying the April 30 bank statement ending balance of $2,441.50 and the $1,621.50 book balance to a single adjusted figure of $1,881.50 by walking line by line through `bank-flower-rose.csv` and `ledger-flower-rose.csv`, matching the 9-Apr bank card $102.50 to the $82.50 fuel plus $20.00 mobile top-up entries, the 8-Apr City Express $220 charge to the 9-Apr ledger entry, and the 30-Apr AirlineOnline $280 to the 30-Apr Airfare entry, then isolating the 27-Apr check 0004 for $350 as an outstanding check and the 29-Apr Hotel Grande $210 as an unresolved exception, while posting book-side corrections for the duplicate 8-Apr Office Supplies check 003 ($350), the misposted 8-Apr Local Grocers $55 Kitchen Supplies and 21-Apr Great Glass Cleaners $90 Cleaning cash entries, the 10-Apr $10 overdraft fee, the three 30-Apr fees totaling $65, the 30-Apr AMB Loans $110 payment, and the 25-Apr deposit 10001 correction of $50.

**Why desired:** Month-end bank reconciliation on a small operating account is bookkeeping-heavy and unforgiving — cross-system matching with imperfect descriptions, distinguishing duplicate postings from legitimate timing differences, separating cash-handling side effects (the $200 ATM withdrawal that already captured the bank-side cash reduction behind the $55 Local Grocers entry) from book-side errors, and assembling the bank-side $560 of open items and the book-side net $260 of corrections so both halves arrive at the adjusted $1,881.50 without a plug. An agent that handles the matching and arithmetic frees the bookkeeper to focus on the controller narrative, account-name discipline for the correcting entries (Office Supplies, Kitchen Supplies, Cleaning, Bank Loan), and the conservative judgment about which items must remain open after April close.

---

## 2. Main Reasoning Challenges by Design

- **Cross-system matching with imperfect descriptions**
  Several legitimate matches require comparing amounts, dates, and merchant wording across the bank export and ledger rather than relying on identical descriptions. The agent must pair the 9-Apr bank card $102.50 with the $82.50 fuel and $20.00 mobile top-up entries, the 8-Apr City Express $220 charge with the 9-Apr ledger advertising line, and the 30-Apr AirlineOnline $280 with the 30-Apr Airfare entry, and must not flag those as exceptions even though the descriptions do not match exactly.

- **Duplicate postings versus genuine timing differences**
  The 27-Apr check 0004 for $350 to Marketing has no matching clearance on the April statement and belongs in bank-side outstanding checks, while the 8-Apr ledger contains two Office Supplies check 003 entries at $350 even though only one $350 check cleared on 8-Apr. The agent must keep check 0004 open as a timing item but reverse the duplicate $350 Office Supplies posting on the books, classifying each correctly rather than treating both as the same kind of difference.

- **Cash-handling side effects and incorrect book entries**
  The 8-Apr Local Grocers $55 Kitchen Supplies and 21-Apr Great Glass Cleaners $90 Cleaning entries do not have separate matching bank charges because the bank-side cash reduction was already captured through the $200 ATM withdrawal. The agent must add $55 and $90 back to the bank-account books with reversing entries that use the existing Kitchen Supplies and Cleaning account names rather than treating the items as bank errors.

- **Conservative handling of unsupported items**
  The 29-Apr Hotel Grande $210 entry does not have any matching $210 transaction on the April bank statement, and the file does not support a definitive resolution by April 30. The agent must leave it open for follow-up rather than posting an unsupported reversing entry, and must avoid plug adjustments anywhere else in the schedule by tying every booked correction to a specific bank or ledger line.

- **Arithmetic completeness on both sides of the schedule**
  The bank-side open items (the $350 outstanding check and the $210 unresolved Hotel Grande charge) total $560 against the $2,441.50 statement balance, and the book-side corrections (the $350 duplicate add-back, the $55 and $90 cash add-backs, less the $10 overdraft fee, $65 of month-end fees, $110 loan payment, and $50 deposit-correction reduction) net to a $260 increase against the $1,621.50 book balance, both landing at $1,881.50. The agent must produce a schedule whose itemized lines reach those adjusted balances without a forced residual.

- **Memo discipline on what posts now versus what stays open**
  The controller memo must clearly separate items that should be posted now (the duplicate $350, the $55 and $90 cash entries, the $10 overdraft fee, the $65 month-end fees, the $110 AMB Loans payment to a Bank Loan or equivalent liability account, and the $50 deposit-correction reduction) from items that must remain open after the April close (check 0004 and the $210 Hotel Grande charge), and must explain the main causes of the mismatch in plain terms suitable for the controller.
