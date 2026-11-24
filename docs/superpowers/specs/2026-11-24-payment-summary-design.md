# Jute Supplier Dues (Payment Summary page) — design

Owner ask 2026-11-24. A new juteTransfer page summarising the Payment Tracker:
dues per **jute supplier** (`jute_supplier_mst`, the broker — NOT `party_mst`),
month-year wise, which suppliers are on hold, how many days payments are
pending, and a company-wise sheet per supplier with the bills below.

Read-only on accounts and MRs. The only write is the party → supplier
confirmation list (new table `jt_party_supplier_map`, admin only).

## Owner rulings (2026-11-24)

1. Group by the jute supplier master. Many MRs carry jute supplier `others`
   because names were changed at import (341 unpaid bills, ₹30 cr of ₹54 cr).
   The page shows a **matching list** party → suggested supplier; the owner
   confirms which party belongs to which supplier and which stay in `others`.
   Nothing is guessed silently: a suggestion counts only once confirmed.
2. FIFO is **notional** — an indication to get a date. No payment is
   re-applied and nothing is saved from it. It pools **all companies together**
   per supplier.
3. Bills of parties on payment hold: **show both** — days pending worked
   without the held bills, and days including them.
4. Month-year columns show the dues **as in accounts** (each bill's own pending
   in its bill month). FIFO only produces the dates/days.

## Data

Rows = the Payment Tracker's rows for "All years" (same bills, same paid /
pending, same hold flag). Terms used below are the tracker's column names:
`Party` = jute supplier name of the MR, `Supplier` = party (party_mst) name,
`Step 1 Co` = the company that owes the bill, `MR Date` = bill date.

### Changes to existing files

`payment_queries.py`
- `_MRS_SQL`: also select `h.jute_supplier_id`.
- new `load_summary_extras() -> dict` (`st.cache_data`, same TTL), each part in
  its own try/except falling back to an empty frame with the right columns:
  - `master`: frame `supplier_id, supplier_name` (all of `jute_supplier_mst`).
  - `on_account`: frame `co_id, co_prefix, party_id, supplier, amount,
    bill_date` — `acc_bill_ref` rows with `ref_type='ON_ACCOUNT'`, active,
    `pending_amount > 0` (amount = pending_amount), on a voucher with
    `status_id = 3`, not reversed, active. `supplier` = `party_mst.supp_name`.
  - `party_history`: frame `party_id, jute_supplier_id, n` — approved
    (`status_id = 3`) `jute_mr` rows grouped by party and jute supplier
    (supplier not null).
  - `party_map`: `supplier_map.load_map()` (`{}` when the table is absent),
    `map_ready`: `supplier_map.table_exists()`.
- `clear_cache()` clears it too.

`payment_tracker_helpers.py`
- `build_rows` / `build_opening_rows`: two more hidden columns (before
  `COLUMNS`, next to `on_hold`): `jute_supplier_id` (the MR's, `None` when the
  row has no MR or the MR has none) and `sheet_party` (opening rows: the
  middle `|` segment of `origin_note`, stripped — `"LCPL | kishan k"` →
  `"kishan k"`, `"EJM old | others | no MR in system"` → `"others"`; `""` for
  current rows or a note without `|`). `COLUMNS` itself is unchanged.
- new `tracker_rows(data, today, accounts_start, drop_paid_opening=False) ->
  DataFrame`: exactly what `pages/payment_tracker.py` does today between
  `load_bills` and the filters (pick_bills → merge acc → build_rows →
  build_opening_rows → optional drop of paid opening rows → merge_rows). The
  tracker page calls it; the new page calls it with `drop_paid_opening=False`.
- `ACCOUNTS_START = date(2026, 4, 1)` moves here (the page imports it).

### New `supplier_map.py` (table `jt_party_supplier_map`, sls only)

```
party_id INT PRIMARY KEY, jute_supplier_id INT NULL, note VARCHAR(200) NULL,
updated_by INT NOT NULL, updated_date_time DATETIME NOT NULL
```
`jute_supplier_id NULL` = "confirmed: stays in others". Follow `freed.py` /
`access.py`: `DDL`, `table_exists()`, `load_map() -> {party_id: supplier_id |
None}`, `save_map(changes: dict, user_id)` (`{party_id: supplier_id | None}`
upserts; value `supplier_map.CLEAR` deletes the row = un-confirm) inside
`DatabaseConnection.get_transaction()`, `access.require_admin(user_id)` first,
one `audit.log` line if `audit.log` fits (read it), else none.
`scripts/migrate_jt_party_supplier_map.py` copies `migrate_jt_freed_no.py`
(dry run by default, `--apply`). **Do not run it with `--apply`.**

### New `payment_summary_helpers.py` (pure: no Streamlit / DB imports)

Tolerance everywhere `EPS = 0.005`. `OTHERS = "others"`.

```python
def norm_name(name) -> str
    # lower, strip, punctuation (. , & / - ') to space, collapse spaces
def others_ids(master: dict) -> set
    # ids whose norm_name is in {"", "others", "other", "na", "n a"}
def match_master(name, master: dict) -> Optional[int]
    # sheet short name -> ONE real (non-others) master id, else None.
    # 1 exact norm_name match; 2 every token of `name` is a prefix of a
    # distinct token of the master name, in order ("kishan k" -> "Kishan
    # Kothari", "r poddar" -> "Ramobatar  Poddar", "agarpara" -> "agarparaaa");
    # 3 a token of `name` may also be the initials of consecutive master tokens
    # ("sk pugalia" -> "Sushil Kumar Pugalia", "GCB" -> "Gopal Chand Bajaj &
    # Sons"). Exact beats 2 beats 3; two candidates at the best level -> None.
def resolve_supplier(row: dict, master: dict, party_map: dict) -> tuple
    # -> (supplier_id | None, source). Order:
    # 1 row jute_supplier_id is a real master id            -> (id, "mr")
    # 2 row has no jute_supplier_id (None/NaN) and sheet_party exactly
    #   (norm_name) equals a real master name               -> (id, "sheet")
    # 3 party_map has the row's party_id with a real id     -> (id, "confirmed")
    # 4 else                                                -> (None, "others")
def add_supplier_key(rows, master, party_map, split_others=False) -> DataFrame
    # copy of rows + columns `skey`, `Jute Supplier`, `ssource`.
    # real supplier: skey = "S<id>", name = master name stripped.
    # others: skey = "O", name = "others"; with split_others: skey =
    # "O<party_id>" (party_id None -> "O"), name = f"{Supplier} (others)".
def assign_on_account(on_account, keyed_rows, master, party_map,
                      split_others=False, holds=None) -> DataFrame
    # on_account + `skey`, `Jute Supplier`, `on_hold`. The group of a ref =
    # the skey with the largest total Pending among keyed_rows of that
    # party_id (any company); none pending -> the skey of that party's latest
    # MR Date row; no rows at all -> via party_map else others. on_hold =
    # (co_id, party_id) in holds (holds = payment_tracker_helpers.holds_map).
def fifo_position(bills, extra_paid=0.0, today=None) -> dict
    # bills: iterable of (bill_date, amount, pending). NOTIONAL.
    # paid = sum(amount - pending) + extra_paid; pending = max(sum(amount) -
    # paid, 0). Walk the bills oldest first (date, then given order; a None
    # date sorts first) giving `paid` to each in turn.
    # -> {"billed", "paid", "pending",
    #     "actual_oldest_unpaid": min date with pending > EPS (None if none),
    #     "pending_from": date of the first bill not fully covered,
    #     "cleared_till": the last bill DATE before pending_from (all its
    #                     bills fully covered), None when there is none,
    #     "days_pending": (today - pending_from).days,
    #     "fifo_pending": list aligned with the INPUT order, each bill's
    #                     notional pending}
    # pending <= EPS: pending_from = days_pending = None, cleared_till = the
    # last bill's date.
def supplier_summary(keyed_rows, on_account_keyed, today) -> DataFrame
    # one row per skey that has any row. Columns:
    # skey, "Jute Supplier", "Unpaid Bills", "Pending", "On Hold" (pending of
    # on_hold rows), "Free To Pay" (Pending - On Hold), "On Account",
    # "Hold" ("" / "Part" / "All": share of PENDING that is held),
    # "Held Parties" (distinct Supplier names of held pending rows, sorted,
    # first 3 + " +n"), "Companies" (sorted distinct Step 1 Co with pending,
    # joined ", "), "Oldest Unpaid Bill" (actual, all rows),
    # "Cleared Till", "Pending From", "Days Pending"  <- fifo_position over
    #   rows NOT on hold, extra_paid = on-account refs not on hold,
    # "Days Incl. Hold"  <- fifo_position over ALL rows + all on-account,
    # "Last Payment" (max). Sorted by Pending desc. Money rounded to 2.
def period_label(d: date, months_from: date) -> str
    # d >= months_from -> "Apr-26"; earlier -> "FY 25-26" (Apr-Mar years).
def period_matrix(keyed_rows, months_from, by="skey") -> DataFrame
    # index = `by` values, columns = period labels in time order (FY columns
    # oldest first, then months), values = sum of actual Pending by MR Date;
    # only periods with a non-zero total. `by` may be "Step 1 Co".
def company_summary(keyed_rows, on_account_keyed, skey, today) -> DataFrame
    # the supplier's rows per "Step 1 Co": "Company", "Bills", "Unpaid Bills",
    # "Bill Amount", "Paid", "Pending", "On Hold", "On Account",
    # "Oldest Unpaid Bill", "Cleared Till", "Pending From", "Days Pending",
    # "Days Incl. Hold" (fifo inside that company), "Last Payment"; last row
    # Company = "All companies" with the pooled figures (= supplier_summary).
def supplier_bills(keyed_rows, on_account_keyed, skey) -> DataFrame
    # the supplier's bills oldest first: "Step 1 Co", "Gate Entry Co", "MR No",
    # "MR Date", "Supplier", "Bill", "Bill Amount", "Paid", "Pending",
    # "FIFO Pending" (notional, pooled all companies, bills not on hold; None
    # on held bills), "On hold", "Status", "Due Date", "Last Payment".
def suggest_party_suppliers(rows, master, party_history, party_map) -> DataFrame
    # one row per party_id that has rows which resolve to others WITHOUT the
    # party_map (rules 1-2 fail), biggest pending first. Columns: party_id,
    # "Party" (Supplier name), "Companies", "Bills", "Unpaid Bills", "Pending",
    # "Sheet Names" ("sukumar (363), rungta (2)" — counts of non-empty
    # sheet_party, most frequent first), "Suggested" (master name or ""),
    # suggested_id, "Basis", "Confirmed" (master name / "others" / "").
    # Suggestion: the most frequent sheet name that is not others ->
    # match_master; if none, the party's most frequent real supplier in
    # party_history. Basis in words: "Sheet name 'kishan k' on 219 of 225
    # bills" / "Jute supplier on 12 of 14 MRs of this party" / "Sheet name
    # 'cheena' has no match in the master" / "".
```

### New page `pages/payment_summary.py` → `payment_summary_page()`

Sidebar entry **"Payment Summary"** right after "Payment Tracker" (`app.py`,
`pages/__init__.py`). Title "Jute Supplier Dues". Widget keys prefixed `ps_`,
state in `st.session_state`, `key=` only (see `po_tracker.py`).

Top row: search (supplier name), Show (`With dues` default / `All`), Hold
(`All` / `On hold` / `Not on hold`), checkbox "Split 'others' by party",
"Month-wise from" (FY list from the current FY back to the earliest FY with a
pending bill, default current FY; earlier years show one column per FY),
Refresh. Metrics: suppliers with dues, Pending, On hold, Free to pay, On
account. Caption in plain words: days are a notional oldest-first working
across all companies; nothing is re-applied in accounts; bills from before
1 Apr 2025 are known only where they were still unpaid on that day.

Tabs (`st.tabs`):
1. **Supplier dues** — `supplier_summary` joined with `period_matrix`;
   `st.dataframe(on_select="rerun", selection_mode="single-row")`; the picked
   supplier becomes the Company-wise tab's supplier (remember the `skey`, not
   the row position). Totals line under the grid.
2. **Company-wise** — supplier selectbox (default: the one picked in tab 1,
   else the biggest due); metrics (Pending, On hold, On account, Days pending,
   Days incl. hold, Cleared till); `company_summary` table; company × period
   matrix; then "Bills" — `supplier_bills` with a company filter and an
   "Unpaid only" checkbox (default on).
3. **Others → supplier** — `suggest_party_suppliers`. Everyone sees the list
   and a CSV download. Admin + `map_ready`: `st.data_editor` with one editable
   column "Confirm as" (options: "", "others", every real master name),
   prefilled from Confirmed; button "Fill blanks with suggestion" (fills only
   rows with no confirmation, not saved); button "Save" → `supplier_map.
   save_map` with only the changed rows ("" on a previously confirmed row =
   CLEAR), then `clear_cache()` + rerun. Not `map_ready`: `st.info` that saving
   is not switched on yet.
   **Review change:** `party_mst` has one record per company for the same
   party, so the tab lists `group_suggestions(...)` — one row per party name +
   sheet name(s) + suggestion + confirmation, carrying `party_ids`; one answer
   is saved for every party record of the row. A suggestion falls back from
   the sheet name to the party's own name being in the master (Shema Jute
   Agency, M/s. Shree Shyam Agency), then to the party's MR history.

Download: one Excel workbook (sheets "Supplier dues", "Company-wise" = every
supplier with dues × company via `company_summary` without the total rows,
"Bills" = unpaid bills with Jute Supplier first) when `openpyxl` is importable
(add it to `pyproject.toml` dependencies if it is not there), plus per-table
CSV.

## Tests

`tests/test_payment_summary_helpers.py` (pure, table-driven: match_master on
the real name pairs above incl. ambiguous → None; resolve order; fifo_position
— no payment, paid out of turn, exact cover, part cover, advance, all paid,
same-day bills; hold both ways; on-account assignment; period labels across
Mar/Apr; company summary total = supplier summary; suggestions),
`tests/test_supplier_map.py` (on `tests/fake_mysql.py` if it can hold the
table, else with a recorder), page test with `AppTest` and patched loaders
(page renders, tab 1 totals, tab 3 read-only without the table, Save sends
only changed rows). Existing tests stay green.
