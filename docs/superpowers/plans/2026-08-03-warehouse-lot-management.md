# Warehouse Lot Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the warehouse-marked page into 3 tabs (Lots / Transfer / Marked Stock) with lot split/merge via app-created lot MRs, multi-lot batch transfer with a common % rate change, and auto-only sold detection.

**Architecture:** A lot is a `jute_mr_li` row. Re-lotting creates a new "lot MR" (`transfer_mode=0`, `status_id=3`, `src_jute_mr_id` NULL) at the source company; the new sls-only table `jute_lot_src` records line-level provenance. Batch transfer generalises `save_marked_move` to many lines: one mode-1 child MR per source MR, whole-lot moves only. Sold detection is a `sales_invoice_jute.mr_id` join — no status flips, no buttons.

**Tech Stack:** Streamlit + st_aggrid, SQLAlchemy raw SQL (`text()`), MySQL (sls tenant), pytest.

**Spec:** `docs/superpowers/specs/2026-08-03-warehouse-lot-management-design.md`

## Global Constraints

- sls tenant DB only; never touch other tenants, vowconsole3, vowerp3be, vowerp3ui.
- Zero column changes to existing tables; the ONLY schema change is the new `jute_lot_src` table.
- All multi-statement writes inside `DatabaseConnection.get_transaction()`; lock rows `FOR UPDATE` in ascending `jute_mr_li_id` order.
- Transfers move whole lots only (partial = split first in Lots tab).
- Eligibility everywhere: `transfer_mode = 0 AND status_id = 3 AND accepted_weight > 0` and the MR has no mode-0 children (chain guard).
- `status_id` semantics: 1 Open, 3 Approved, 13 Pending. Never use the 0/1/2 labels from `get_jute_mr_with_line_items`.
- Rates are per quintal: value = kg × rate / 100. Kg rounds to 3 dp, money to 2 dp.
- Stock truth is the ERP view `vw_jute_stock_outstanding` (`bal_weight = actual_weight − SUM(jute_issue.weight WHERE status_id <> 4)` per `jute_mr_li_id`, MRs status 3/13). Available kg = `LEAST(COALESCE(v.bal_weight, li.accepted_weight), li.accepted_weight)`; consumed = balance ≤ 0. Never detect consumption via `sales_invoice_jute.mr_id` (owner ruling 2026-08-03 — the ERP UI never populates it).
- Every line this app creates must set `actual_weight` (= moved kg), `actual_rate` (= rate), and `actual_qty` (source `actual_qty` × take-fraction, 3 dp); every source reduction/restore adjusts `actual_weight`/`actual_qty` alongside `accepted_weight`. Exact deltas live in `jute_lot_src.actual_qty_delta`/`actual_weight_delta`.
- Pure math modules import nothing from pages/DB (pattern: `jute_mr_chain_helpers.py`).
- Streamlit widgets: key-based session state, never pass `value=` for persistent inputs (CLAUDE.md widget lesson).
- Run pytest from repo root: `pytest tests/ -v`. Import check: `python -c "from src.jutetransfer import ..."`.

---

### Task 1: Sold-detection verification script (STOP GATE)

Spec §7: before anything else, verify sls data supports auto-only sold detection.

**Files:**
- Create: `scripts/verify_sold_detection.py`

**Interfaces:**
- Consumes: `DatabaseConnection.execute_query` (`src/jutetransfer/database.py:69`)
- Produces: printed report; a go/no-go decision recorded in the task commit message.

- [ ] **Step 1: Write the script**

```python
"""Read-only verification: can sold marked stock be auto-detected? (spec section 7)

Run: python scripts/verify_sold_detection.py
"""
from src.jutetransfer.database import DatabaseConnection

CHECKS = {
    "mode1_total": (
        "count of marked (transfer_mode=1) MRs",
        "SELECT COUNT(*) AS c FROM jute_mr WHERE transfer_mode = 1",
    ),
    "mode1_sold_linked": (
        "marked MRs referenced by an active raw-jute invoice via sales_invoice_jute.mr_id",
        """
        SELECT COUNT(DISTINCT mr.jute_mr_id) AS c
        FROM jute_mr mr
        JOIN sales_invoice_jute sij ON sij.mr_id = mr.jute_mr_id
        JOIN sales_invoice si ON si.invoice_id = sij.invoice_id
        WHERE mr.transfer_mode = 1 AND si.active = 1 AND si.invoice_type = 5
        """,
    ),
    "type5_mr_id_population": (
        "active raw-jute invoice rows: how many carry a non-null mr_id",
        """
        SELECT SUM(sij.mr_id IS NOT NULL) AS linked, COUNT(*) AS total
        FROM sales_invoice_jute sij
        JOIN sales_invoice si ON si.invoice_id = sij.invoice_id
        WHERE si.active = 1 AND si.invoice_type = 5
        """,
    ),
    "type5_mr_id_valid": (
        "non-null mr_id values that resolve to a real jute_mr row",
        """
        SELECT COUNT(*) AS c
        FROM sales_invoice_jute sij
        JOIN sales_invoice si ON si.invoice_id = sij.invoice_id
        JOIN jute_mr mr ON mr.jute_mr_id = sij.mr_id
        WHERE si.active = 1 AND si.invoice_type = 5
        """,
    ),
    "jute_issue_mr_no_link": (
        "informational: jute_issue rows joinable to jute_mr on (mr_no, branch)",
        """
        SELECT COUNT(*) AS c
        FROM jute_issue ji
        JOIN jute_mr mr ON mr.branch_mr_no = ji.mr_no AND mr.branch_id = ji.branch_id
        WHERE COALESCE(ji.is_active, 1) = 1
        """,
    ),
}


def main() -> None:
    for key, (label, sql) in CHECKS.items():
        df = DatabaseConnection.execute_query(sql)
        print(f"{key}: {label}")
        print(df.to_string(index=False))
        print("-" * 60)
    print(
        "DECISION RULE: proceed iff type5_mr_id_population shows mr_id populated "
        "on (nearly) all rows AND type5_mr_id_valid matches the linked count. "
        "mode1_sold_linked may legitimately be 0 if no marked lot was sold yet. "
        "If mr_id is broadly NULL: STOP and report to owner (spec section 7)."
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it**

Run: `python scripts/verify_sold_detection.py`
Expected: five result blocks, no exceptions (read-only; needs `.env`).

- [ ] **Step 3: Apply the decision rule**

If mr_id is broadly NULL on active type-5 invoices: **STOP the plan here and report the numbers to the owner.** Otherwise record the counts in the commit message and continue.

- [ ] **Step 4: Commit**

```bash
git add scripts/verify_sold_detection.py
git commit -m "chore: sold-detection verification script (spec 7) - results: <paste counts>"
```

---

### Task 2: `jute_lot_src` migration

**Files:**
- Create: `scripts/migrate_jute_lot_src.py`

**Interfaces:**
- Consumes: `DatabaseConnection.execute_non_query`, `get_table_schema` (`src/jutetransfer/database.py`)
- Produces: table `jute_lot_src` in the sls DB (columns exactly as below) — used by Tasks 4-10.

- [ ] **Step 1: Write the idempotent migration script**

```python
"""Create the jute_lot_src provenance table (sls only). Idempotent.

Run: python scripts/migrate_jute_lot_src.py
"""
from src.jutetransfer.database import DatabaseConnection, get_table_schema

DDL = """
CREATE TABLE IF NOT EXISTS jute_lot_src (
    lot_src_id         BIGINT PRIMARY KEY AUTO_INCREMENT,
    new_jute_mr_li_id  BIGINT NOT NULL,
    src_jute_mr_li_id  BIGINT NOT NULL,
    qty_kg             DECIMAL(12,3) NOT NULL,
    actual_qty_delta   DECIMAL(12,3) NULL,
    actual_weight_delta DECIMAL(12,3) NULL,
    created_by         INT NULL,
    created_date_time  DATETIME NULL,
    KEY idx_lot_src_new (new_jute_mr_li_id),
    KEY idx_lot_src_src (src_jute_mr_li_id)
)
"""
```

Rows are written for every app-created line — lot-MR lines AND marked child
lines — recording exactly what was taken from which source line
(`qty_kg` = accepted kg moved, `actual_qty_delta`/`actual_weight_delta` = the
amounts subtracted from the source's `actual_qty`/`actual_weight`), so deletes
restore sources exactly with no quality matching.

```python

if __name__ == "__main__":
    DatabaseConnection.execute_non_query(DDL)
    print(get_table_schema("jute_lot_src").to_string(index=False))
```

- [ ] **Step 2: Run it**

Run: `python scripts/migrate_jute_lot_src.py`
Expected: DESCRIBE output listing lot_src_id, new_jute_mr_li_id, src_jute_mr_li_id, qty_kg, created_by, created_date_time.

- [ ] **Step 3: Run it again (idempotency check)**

Run: `python scripts/migrate_jute_lot_src.py`
Expected: same output, no error.

- [ ] **Step 4: Commit**

```bash
git add scripts/migrate_jute_lot_src.py
git commit -m "feat(db): jute_lot_src provenance table (sls-only)"
```

---

### Task 3: Pure lot math — `lot_helpers.py`

**Files:**
- Create: `src/jutetransfer/lot_helpers.py`
- Test: `tests/test_lot_helpers.py`

**Interfaces:**
- Consumes: nothing (stdlib only — like `jute_mr_chain_helpers.py`).
- Produces (used by Tasks 6, 7, 9):
  - `validate_takes(takes: list[tuple[int, float]], available: dict[int, float]) -> list[tuple[int, float]]` — raises `ValueError` on empty list, duplicate line id, qty ≤ 0, qty > available (1e-9 tolerance); returns `(int, round(qty, 3))` pairs.
  - `apply_pct(rate: float, pct: float) -> float` — `round(rate * (1 + pct/100), 2)`.
  - `line_price(weight_kg: float, rate: float) -> float` — `round(weight_kg * rate / 100, 2)`.
  - `primary_source_mr(mr_take_totals: dict[int, float]) -> int` — MR with largest take total, ties → lowest MR id; `ValueError` on empty dict.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for pure lot math (no DB, no Streamlit)."""
import pytest
from src.jutetransfer.lot_helpers import (
    validate_takes, apply_pct, line_price, primary_source_mr,
)


def test_validate_takes_ok_and_rounding():
    out = validate_takes([(11, 4000.0), (12, 1999.9995)], {11: 6000.0, 12: 2000.0})
    assert out == [(11, 4000.0), (12, 2000.0)]  # 3dp rounding


def test_validate_takes_full_line_allowed():
    assert validate_takes([(11, 6000.0)], {11: 6000.0}) == [(11, 6000.0)]


def test_validate_takes_empty():
    with pytest.raises(ValueError):
        validate_takes([], {})


def test_validate_takes_duplicate_line():
    with pytest.raises(ValueError):
        validate_takes([(11, 100.0), (11, 200.0)], {11: 6000.0})


def test_validate_takes_zero_and_negative():
    for bad in (0.0, -5.0):
        with pytest.raises(ValueError):
            validate_takes([(11, bad)], {11: 6000.0})


def test_validate_takes_over_available():
    with pytest.raises(ValueError):
        validate_takes([(11, 6000.001)], {11: 6000.0})


def test_apply_pct_up_down_zero():
    assert apply_pct(2500.0, 10.0) == 2750.0
    assert apply_pct(2500.0, -5.0) == 2375.0
    assert apply_pct(2500.0, 0.0) == 2500.0
    assert apply_pct(2551.33, 2.5) == 2615.11  # verify against REPL; round half-even


def test_line_price():
    assert line_price(4000.0, 2750.0) == 110000.0
    assert line_price(123.456, 2500.0) == 3086.4


def test_primary_source_mr_largest_then_lowest_id():
    assert primary_source_mr({5: 100.0, 9: 300.0}) == 9
    assert primary_source_mr({9: 300.0, 5: 300.0}) == 5
    with pytest.raises(ValueError):
        primary_source_mr({})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_lot_helpers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.jutetransfer.lot_helpers'`

- [ ] **Step 3: Implement**

```python
"""Pure lot math for warehouse lot management.

No Streamlit/DB imports (same rule as jute_mr_chain_helpers.py).
"""

_EPS = 1e-9


def validate_takes(takes, available):
    """Validate (jute_mr_li_id, qty_kg) takes against available kg per line.

    Returns normalised [(int_id, qty_rounded_3dp)]. Raises ValueError on empty
    input, duplicate line, qty <= 0, or qty > available.
    """
    if not takes:
        raise ValueError("no lots selected")
    seen, out = set(), []
    for li_id, qty in takes:
        li_id = int(li_id)
        qty = round(float(qty), 3)
        if li_id in seen:
            raise ValueError(f"duplicate source line {li_id}")
        seen.add(li_id)
        avail = float(available.get(li_id, 0.0))
        if qty <= 0:
            raise ValueError(f"take qty must be > 0 for line {li_id}, got {qty}")
        if qty > avail + _EPS:
            raise ValueError(f"take {qty} exceeds available {avail} for line {li_id}")
        out.append((li_id, qty))
    return out


def apply_pct(rate, pct):
    """Common % rate change (positive or negative), money-rounded."""
    return round(float(rate) * (1.0 + float(pct) / 100.0), 2)


def line_price(weight_kg, rate):
    """Line value: rate is per quintal."""
    return round(float(weight_kg) * float(rate) / 100.0, 2)


def primary_source_mr(mr_take_totals):
    """MR contributing the largest take qty; ties -> lowest jute_mr_id."""
    if not mr_take_totals:
        raise ValueError("no source MRs")
    return min(mr_take_totals.items(), key=lambda kv: (-kv[1], kv[0]))[0]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_lot_helpers.py -v`
Expected: all PASS. Note: if `test_apply_pct_up_down_zero` fails on `2615.11`, check Python banker's rounding — compute `round(2551.33 * 1.025, 2)` in a REPL and fix the *expected value in the test* to the actual half-even result; the implementation stays `round(..., 2)`.

- [ ] **Step 5: Commit**

```bash
git add src/jutetransfer/lot_helpers.py tests/test_lot_helpers.py
git commit -m "feat: pure lot math helpers (validate_takes, apply_pct, line_price, primary_source_mr)"
```

---

### Task 4: Availability queries

**Files:**
- Modify: `src/jutetransfer/queries.py` (append after `get_jute_mr_with_line_items`, ~line 221)

**Interfaces:**
- Consumes: `DatabaseConnection.execute_query`; table `jute_lot_src` (Task 2).
- Produces (used by Tasks 8, 9):
  - `get_available_lots(co_id: int, branch_id: int, year: int, month: int) -> pd.DataFrame` — columns: `jute_mr_id, jute_mr_li_id, mr_no, mr_date, quality, remaining_kg, rate, warehouse, party, is_lot`.
  - `get_quality_availability_summary(co_id: int, branch_id: int, year: int, month: int) -> pd.DataFrame` — columns: `quality, lots, total_kg, avg_rate`.

- [ ] **Step 1: Implement both queries**

Write the FROM/WHERE block twice, fully inlined in each function (no string splicing — correctness over DRY):

```python
def get_available_lots(co_id: int, branch_id: int, year: int, month: int) -> pd.DataFrame:
    """Transferable lots: mode 0, Approved (status 3), available kg > 0, not
    feeding a live vertical chain. One row per jute_mr_li line. Available kg is
    balance-aware: LEAST(view balance, accepted_weight) — weight already issued
    to production is not movable. is_lot=1 marks app-created lines (provenance
    exists in jute_lot_src)."""
    query = """
        SELECT
            mr.jute_mr_id,
            li.jute_mr_li_id,
            mr.branch_mr_no AS mr_no,
            mr.jute_mr_date AS mr_date,
            im.item_name AS quality,
            ROUND(LEAST(COALESCE(v.bal_weight, li.accepted_weight),
                        li.accepted_weight), 3) AS remaining_kg,
            li.rate AS rate,
            wh.warehouse_name AS warehouse,
            COALESCE(s.supplier_name, pm.supp_name) AS party,
            EXISTS(
                SELECT 1 FROM jute_lot_src ls
                WHERE ls.new_jute_mr_li_id = li.jute_mr_li_id
            ) AS is_lot
        FROM jute_mr mr
        JOIN branch_mst bm ON bm.branch_id = mr.branch_id
        JOIN jute_mr_li li ON li.jute_mr_id = mr.jute_mr_id
        LEFT JOIN vw_jute_stock_outstanding v ON v.jute_mr_li_id = li.jute_mr_li_id
        LEFT JOIN item_mst im ON im.item_id = li.actual_item_id
        LEFT JOIN warehouse_mst wh ON wh.warehouse_id = li.warehouse_id
        LEFT JOIN jute_supplier_mst s ON s.supplier_id = mr.jute_supplier_id
        LEFT JOIN party_mst pm ON pm.party_id = mr.party_id AND pm.co_id = bm.co_id
        WHERE mr.transfer_mode = 0
          AND mr.status_id = 3
          AND LEAST(COALESCE(v.bal_weight, li.accepted_weight),
                    COALESCE(li.accepted_weight, 0)) > 0
          AND bm.co_id = :co_id
          AND mr.branch_id = :branch_id
          AND YEAR(mr.jute_gate_entry_date) = :year
          AND MONTH(mr.jute_gate_entry_date) = :month
          AND NOT EXISTS (
              SELECT 1 FROM jute_mr c
              WHERE c.src_jute_mr_id = mr.jute_mr_id
                AND c.transfer_mode = 0
                AND c.jute_mr_id <> mr.jute_mr_id
          )
        ORDER BY mr.jute_mr_date, mr.jute_mr_id, li.jute_mr_li_id
    """
    return DatabaseConnection.execute_query(
        query, {"co_id": co_id, "branch_id": branch_id, "year": year, "month": month}
    )


def get_quality_availability_summary(co_id: int, branch_id: int, year: int, month: int) -> pd.DataFrame:
    """Quality-wise availability: lot count, total kg, weighted-avg rate."""
    query = """
        SELECT
            im.item_name AS quality,
            COUNT(*) AS lots,
            ROUND(SUM(LEAST(COALESCE(v.bal_weight, li.accepted_weight),
                            li.accepted_weight)), 2) AS total_kg,
            ROUND(SUM(LEAST(COALESCE(v.bal_weight, li.accepted_weight),
                            li.accepted_weight) * li.rate)
                  / NULLIF(SUM(LEAST(COALESCE(v.bal_weight, li.accepted_weight),
                                     li.accepted_weight)), 0), 2) AS avg_rate
        FROM jute_mr mr
        JOIN branch_mst bm ON bm.branch_id = mr.branch_id
        JOIN jute_mr_li li ON li.jute_mr_id = mr.jute_mr_id
        LEFT JOIN vw_jute_stock_outstanding v ON v.jute_mr_li_id = li.jute_mr_li_id
        LEFT JOIN item_mst im ON im.item_id = li.actual_item_id
        WHERE mr.transfer_mode = 0
          AND mr.status_id = 3
          AND LEAST(COALESCE(v.bal_weight, li.accepted_weight),
                    COALESCE(li.accepted_weight, 0)) > 0
          AND bm.co_id = :co_id
          AND mr.branch_id = :branch_id
          AND YEAR(mr.jute_gate_entry_date) = :year
          AND MONTH(mr.jute_gate_entry_date) = :month
          AND NOT EXISTS (
              SELECT 1 FROM jute_mr c
              WHERE c.src_jute_mr_id = mr.jute_mr_id
                AND c.transfer_mode = 0
                AND c.jute_mr_id <> mr.jute_mr_id
          )
        GROUP BY im.item_name
        ORDER BY im.item_name
    """
    return DatabaseConnection.execute_query(
        query, {"co_id": co_id, "branch_id": branch_id, "year": year, "month": month}
    )
```

- [ ] **Step 2: Smoke-test against sls (read-only)**

Run:
```bash
python -c "from src.jutetransfer.queries import get_available_lots, get_quality_availability_summary, get_companies; import datetime as d; co=list(get_companies().values())[0]; n=d.datetime.now(); a=get_available_lots(co,1,n.year,n.month); s=get_quality_availability_summary(co,1,n.year,n.month); print(list(a.columns)); print(list(s.columns))"
```
Expected: `['jute_mr_id', 'jute_mr_li_id', 'mr_no', 'mr_date', 'quality', 'remaining_kg', 'rate', 'warehouse', 'party', 'is_lot']` and `['quality', 'lots', 'total_kg', 'avg_rate']`, no exception. (If branch 1 doesn't exist for that company, pick one via `get_branches_by_company(co)`.)

- [ ] **Step 3: Commit**

```bash
git add src/jutetransfer/queries.py
git commit -m "feat(queries): available-lots and quality availability summary (status-3 filtered)"
```

---

### Task 5: Marked-stock, provenance, and P&L queries

**Files:**
- Modify: `src/jutetransfer/queries.py` — append two functions; edit `get_company_wise_marked_stock` (~line 484)

**Interfaces:**
- Consumes: `jute_lot_src` (Task 2); ERP view `vw_jute_stock_outstanding` (exists at sls; `bal_weight` per `jute_mr_li_id`).
- Produces (used by Tasks 7, 8, 10):
  - `get_marked_stock_with_balance(co_id: int, branch_id: int, year: int, month: int) -> pd.DataFrame` — columns: `jute_mr_id, jute_mr_li_id, mr_no, mr_date, quality, kg, balance_kg, rate, value, godown, src_jute_mr_id, consumed`.
  - `get_lot_provenance(jute_mr_id: int) -> pd.DataFrame` — columns: `lot_li, src_jute_mr_li_id, qty_kg, depth, src_mr_id, src_mr_no, quality`.
  - `get_company_wise_marked_stock` — same signature/columns as today, revalued from remaining balance.

- [ ] **Step 1: Implement `get_marked_stock_with_balance`**

```python
def get_marked_stock_with_balance(co_id: int, branch_id: int, year: int, month: int) -> pd.DataFrame:
    """Marked (mode-1) stock lines with remaining balance from the ERP stock
    view (bal_weight = actual_weight - issued). consumed=1 when balance <= 0.
    One row per line; value prices the REMAINING balance."""
    query = """
        SELECT
            mr.jute_mr_id,
            li.jute_mr_li_id,
            mr.branch_mr_no AS mr_no,
            mr.jute_mr_date AS mr_date,
            im.item_name AS quality,
            li.accepted_weight AS kg,
            ROUND(GREATEST(COALESCE(v.bal_weight, li.accepted_weight), 0), 3) AS balance_kg,
            li.rate AS rate,
            ROUND(GREATEST(COALESCE(v.bal_weight, li.accepted_weight), 0)
                  * COALESCE(li.rate, 0) / 100, 2) AS value,
            wh.warehouse_name AS godown,
            mr.src_jute_mr_id,
            (COALESCE(v.bal_weight, li.accepted_weight) <= 0) AS consumed
        FROM jute_mr mr
        JOIN branch_mst bm ON bm.branch_id = mr.branch_id
        JOIN jute_mr_li li ON li.jute_mr_id = mr.jute_mr_id
        LEFT JOIN vw_jute_stock_outstanding v ON v.jute_mr_li_id = li.jute_mr_li_id
        LEFT JOIN item_mst im ON im.item_id = li.actual_item_id
        LEFT JOIN warehouse_mst wh ON wh.warehouse_id = li.warehouse_id
        WHERE mr.transfer_mode = 1
          AND mr.status_id = 3
          AND bm.co_id = :co_id
          AND mr.branch_id = :branch_id
          AND YEAR(mr.jute_gate_entry_date) = :year
          AND MONTH(mr.jute_gate_entry_date) = :month
        ORDER BY mr.jute_mr_date, mr.jute_mr_id, li.jute_mr_li_id
    """
    return DatabaseConnection.execute_query(
        query, {"co_id": co_id, "branch_id": branch_id, "year": year, "month": month}
    )
```

- [ ] **Step 2: Implement `get_lot_provenance`** (MySQL 8 recursive CTE)

```python
def get_lot_provenance(jute_mr_id: int) -> pd.DataFrame:
    """Walk jute_lot_src from a lot MR's lines back to gate-entry origins.

    depth 1 = direct source; deeper = re-lotted lots. Empty DataFrame if the
    MR is not a lot MR."""
    query = """
        WITH RECURSIVE prov AS (
            SELECT ls.new_jute_mr_li_id AS lot_li,
                   ls.src_jute_mr_li_id,
                   ls.qty_kg,
                   1 AS depth
            FROM jute_lot_src ls
            JOIN jute_mr_li li ON li.jute_mr_li_id = ls.new_jute_mr_li_id
            WHERE li.jute_mr_id = :mr_id
            UNION ALL
            SELECT p.lot_li, ls2.src_jute_mr_li_id, ls2.qty_kg, p.depth + 1
            FROM prov p
            JOIN jute_lot_src ls2 ON ls2.new_jute_mr_li_id = p.src_jute_mr_li_id
        )
        SELECT p.lot_li,
               p.src_jute_mr_li_id,
               p.qty_kg,
               p.depth,
               smr.jute_mr_id AS src_mr_id,
               smr.branch_mr_no AS src_mr_no,
               im.item_name AS quality
        FROM prov p
        JOIN jute_mr_li sli ON sli.jute_mr_li_id = p.src_jute_mr_li_id
        JOIN jute_mr smr ON smr.jute_mr_id = sli.jute_mr_id
        LEFT JOIN item_mst im ON im.item_id = sli.actual_item_id
        ORDER BY p.lot_li, p.depth, p.src_jute_mr_li_id
    """
    return DatabaseConnection.execute_query(query, {"mr_id": jute_mr_id})
```

- [ ] **Step 3: Revalue `get_company_wise_marked_stock` from remaining balance**

Replace the existing function's SQL (`queries.py` ~line 487) with:

```python
    return DatabaseConnection.execute_query(
        """
        SELECT
            cm.co_id   AS co_id,
            cm.co_name AS co_name,
            COALESCE(SUM(
                GREATEST(COALESCE(v.bal_weight, li.accepted_weight), 0)
                * COALESCE(li.rate, 0) / 100
            ), 0) AS stock_value
        FROM jute_mr mr
        JOIN branch_mst bm ON mr.branch_id = bm.branch_id
        JOIN co_mst    cm ON bm.co_id = cm.co_id
        JOIN jute_mr_li li ON li.jute_mr_id = mr.jute_mr_id
        LEFT JOIN vw_jute_stock_outstanding v ON v.jute_mr_li_id = li.jute_mr_li_id
        WHERE mr.jute_mr_date BETWEEN :fy_start AND :fy_end
          AND mr.status_id = 3
          AND mr.transfer_mode = 1
        GROUP BY cm.co_id, cm.co_name
        """,
        {
            "fy_start": fy_start.strftime("%Y-%m-%d"),
            "fy_end": fy_end.strftime("%Y-%m-%d"),
        },
    )
```

(Docstring: update to say value = remaining balance × rate; consumed stock
drops out naturally.)

- [ ] **Step 4: Smoke-test (read-only)**

Run:
```bash
python -c "from src.jutetransfer.queries import get_marked_stock_with_balance, get_lot_provenance, get_company_wise_marked_stock, get_companies, _get_financial_year_bounds; import datetime as d; co=list(get_companies().values())[0]; n=d.datetime.now(); m=get_marked_stock_with_balance(co,1,n.year,n.month); print(list(m.columns)); print(list(get_lot_provenance(1).columns)); fs,fe=_get_financial_year_bounds(); print(get_company_wise_marked_stock(fs,fe).columns.tolist())"
```
Expected: the three column lists as specified in Interfaces, no exception.

- [ ] **Step 5: Commit**

```bash
git add src/jutetransfer/queries.py
git commit -m "feat(queries): marked stock with sold flag, lot provenance CTE, sold-excluded P&L marked stock"
```

---

### Task 6: `lot_ops.py` — create_lot / delete_lot (+ shared helpers in warehouse_stock_ops)

**Files:**
- Create: `src/jutetransfer/lot_ops.py`
- Modify: `src/jutetransfer/warehouse_stock_ops.py` (append shared helpers)

**Interfaces:**
- Consumes: `lot_helpers.validate_takes/line_price/primary_source_mr` (Task 3); `DatabaseConnection.get_transaction/execute_insert_returning_id`; from `transfer.py`: `_get_next_gate_entry_no(conn, branch_id)`, `_get_next_mr_number_in_txn(conn, branch_id, mr_date)`, `_get_next_bill_pass_no_in_txn(conn, branch_id)`; from `warehouse_stock_ops.py`: `_recompute_mr_header(conn, jute_mr_id, updated_by)`.
- Produces (used by Tasks 7, 8):
  - `create_lot(takes: list[tuple[int, float]], mr_date: date, updated_by: int) -> int` — returns new lot MR id.
  - `delete_lot(lot_mr_id: int, updated_by: int) -> None`.
  - In `warehouse_stock_ops.py`: `_BALANCE_SQL`, `_available_kg(conn, li_id, accepted) -> float`, `_reduce_source_line(conn, r, qty, available) -> tuple[aq_delta, aw_delta]`, `_LI_INSERT_SQL` — Task 7's batch transfer reuses these.

**File placement:** the block below defines `_BALANCE_SQL`, `_available_kg`,
`_reduce_source_line`, and `_LI_INSERT_SQL` — those four go into
`warehouse_stock_ops.py`, appended after `_recompute_mr_header`, together with
a new top-of-file import `from .lot_helpers import apply_pct, line_price`
(apply_pct is used by Task 7). Everything else goes in the new `lot_ops.py`,
which imports them:
`from .warehouse_stock_ops import _recompute_mr_header, _available_kg, _reduce_source_line, _LI_INSERT_SQL`
(replacing the plain `_recompute_mr_header` import shown in the block).

- [ ] **Step 1: Write the module**

```python
"""Lot restructuring ops: create/delete app-created lot MRs.

A lot MR re-allocates quantities from existing mode-0 lines at the SAME
company/branch. Line-level provenance lives in jute_lot_src; source lines are
reduced in place (weight conserved). Lot MRs have transfer_mode=0, status 3,
src_jute_mr_id NULL — downstream transfer logic treats them as ordinary MRs.

Kept separate from warehouse_stock_ops (marked moves) and transfer (chains).
"""

from datetime import date

from sqlalchemy import bindparam, text

from .database import DatabaseConnection
from .lot_helpers import validate_takes, line_price, primary_source_mr
from .transfer import (
    _get_next_gate_entry_no,
    _get_next_mr_number_in_txn,
    _get_next_bill_pass_no_in_txn,
)
from .warehouse_stock_ops import _recompute_mr_header

_LOCK_LINE_SQL = """
    SELECT li.jute_mr_li_id, li.accepted_weight, li.rate, li.actual_item_id,
           li.actual_quality, li.challan_quality_id, li.marka, li.crop_year,
           li.unit_conversion, li.warehouse_id, li.jute_mr_id,
           li.actual_qty, li.actual_weight, li.actual_rate,
           mr.branch_id, mr.transfer_mode, mr.status_id,
           mr.party_id, mr.party_branch_id, bm.co_id
    FROM jute_mr_li li
    JOIN jute_mr mr ON mr.jute_mr_id = li.jute_mr_id
    JOIN branch_mst bm ON bm.branch_id = mr.branch_id
    WHERE li.jute_mr_li_id = :id
    FOR UPDATE
"""

_BALANCE_SQL = """
    SELECT bal_weight FROM vw_jute_stock_outstanding WHERE jute_mr_li_id = :id
"""


def _available_kg(conn, li_id: int, accepted: float) -> float:
    """Balance-aware available kg: LEAST(view balance, accepted_weight).

    The view row can be missing (e.g. freshly created line inside this
    transaction) — fall back to accepted_weight."""
    row = conn.execute(text(_BALANCE_SQL), {"id": li_id}).fetchone()
    bal = row._mapping["bal_weight"] if row else None
    return round(min(float(bal), accepted), 3) if bal is not None else accepted


def _reduce_source_line(conn, r: dict, qty: float, available: float) -> tuple:
    """Reduce a locked source line by qty kg across accepted AND actual fields.

    Returns (actual_qty_delta, actual_weight_delta) for provenance storage."""
    accepted = float(r["accepted_weight"] or 0)
    actual_w = float(r["actual_weight"] or 0)
    actual_q = float(r["actual_qty"] or 0)
    frac = qty / available if available > 0 else 1.0
    aw_delta = round(min(qty, actual_w), 3)
    aq_delta = round(actual_q * frac, 3)
    new_accepted = round(accepted - qty, 3)
    conn.execute(text("""
        UPDATE jute_mr_li
        SET accepted_weight = :w, total_price = :p,
            actual_weight = :aw, actual_qty = :aq,
            updated_date_time = NOW()
        WHERE jute_mr_li_id = :id
    """), {
        "w": new_accepted,
        "p": line_price(new_accepted, float(r["rate"] or 0)),
        "aw": round(max(0.0, actual_w - aw_delta), 3),
        "aq": round(max(0.0, actual_q - aq_delta), 3),
        "id": int(r["jute_mr_li_id"]),
    })
    return aq_delta, aw_delta

_CHAIN_CHILD_SQL = """
    SELECT 1 FROM jute_mr
    WHERE src_jute_mr_id = :sid AND transfer_mode = 0 AND jute_mr_id <> :sid
    LIMIT 1
"""

_LI_INSERT_SQL = """
    INSERT INTO jute_mr_li (
        jute_mr_id, actual_item_id, actual_quality, challan_quality_id,
        accepted_weight, rate, claim_rate, total_price, warehouse_id,
        actual_qty, actual_weight, actual_rate,
        marka, crop_year, active, updated_date_time, unit_conversion
    ) VALUES (
        :mr_id, :actual_item_id, :actual_quality, :challan_quality_id,
        :w, :rate, 0, :price, :warehouse_id,
        :actual_qty, :w, :rate,
        :marka, :crop_year, 1, NOW(), :unit_conversion
    )
"""
# actual_weight = accepted kg and actual_rate = rate on app-created lines, so
# the ERP stock view computes balances for them (bal = actual_weight - issued).


def create_lot(takes, mr_date: date, updated_by: int) -> int:
    """Create one lot MR from (src_jute_mr_li_id, qty_kg) takes.

    All sources must be mode-0, status-3, same-branch lines not feeding a live
    chain. Returns the new lot MR id.
    """
    if not takes:
        raise ValueError("no lots selected")
    with DatabaseConnection.get_transaction() as conn:
        rows = {}
        for li_id in sorted(int(t[0]) for t in takes):  # ascending lock order
            row = conn.execute(text(_LOCK_LINE_SQL), {"id": li_id}).fetchone()
            if not row:
                raise ValueError(f"Source line {li_id} not found")
            rows[li_id] = dict(row._mapping)

        available = {
            i: _available_kg(conn, i, float(r["accepted_weight"] or 0))
            for i, r in rows.items()
        }
        norm = validate_takes(takes, available)

        branches = {int(r["branch_id"]) for r in rows.values()}
        if len(branches) != 1:
            raise ValueError("All source lines must belong to the same branch")
        branch_id = branches.pop()

        checked = set()
        for r in rows.values():
            if int(r["transfer_mode"] or 0) != 0:
                raise ValueError("Can only re-lot normal (transfer_mode=0) stock")
            if int(r["status_id"] or 0) != 3:
                raise ValueError("Can only re-lot Approved (status 3) MRs")
            mr_id = int(r["jute_mr_id"])
            if mr_id not in checked:
                if conn.execute(text(_CHAIN_CHILD_SQL), {"sid": mr_id}).fetchone():
                    raise ValueError(
                        f"MR {mr_id} feeds a vertical transfer chain; re-lot disabled"
                    )
                checked.add(mr_id)

        # Reduce each source line (accepted + actual fields); collect per-MR
        # take totals and the per-take actual deltas for provenance.
        mr_totals, deltas = {}, {}
        for li_id, qty in norm:
            r = rows[li_id]
            deltas[li_id] = _reduce_source_line(conn, r, qty, available[li_id])
            mr_totals[int(r["jute_mr_id"])] = (
                mr_totals.get(int(r["jute_mr_id"]), 0.0) + qty
            )
        for mr_id in sorted(mr_totals):
            _recompute_mr_header(conn, mr_id, updated_by)

        # Lot MR header: party copied from the primary source MR (same company,
        # no remap needed); src_com_id / src_jute_mr_id NULL — this is a
        # re-allocation at the same company, not a transfer.
        primary_mr_id = primary_source_mr(mr_totals)
        primary = next(
            rows[li] for li, _ in norm
            if int(rows[li]["jute_mr_id"]) == primary_mr_id
        )
        lot_mr_id = DatabaseConnection.execute_insert_returning_id(conn, """
            INSERT INTO jute_mr (
                jute_gate_entry_no, branch_mr_no, jute_gate_entry_date, jute_mr_date,
                status_id, transfer_mode, updated_by, updated_date_time,
                branch_id, party_id, party_branch_id, src_com_id, src_jute_mr_id,
                total_amount, claim_amount, roundoff, net_total,
                bill_pass_no, bill_pass_date
            ) VALUES (
                :gate_no, :mr_no, :mr_date, :mr_date,
                3, 0, :updated_by, NOW(),
                :branch_id, :party_id, :party_branch_id, NULL, NULL,
                0, 0, 0, 0,
                :bill_pass_no, :mr_date
            )
        """, {
            "gate_no": _get_next_gate_entry_no(conn, branch_id),
            "mr_no": _get_next_mr_number_in_txn(conn, branch_id, mr_date),
            "mr_date": mr_date,
            "updated_by": updated_by,
            "branch_id": branch_id,
            "party_id": primary["party_id"],
            "party_branch_id": primary["party_branch_id"],
            "bill_pass_no": _get_next_bill_pass_no_in_txn(conn, branch_id),
        })

        # One lot line + one provenance row per take. Same company: item ids
        # copy over unchanged.
        for li_id, qty in norm:
            r = rows[li_id]
            aq_delta, aw_delta = deltas[li_id]
            new_li_id = DatabaseConnection.execute_insert_returning_id(
                conn, _LI_INSERT_SQL, {
                    "mr_id": lot_mr_id,
                    "actual_item_id": r["actual_item_id"],
                    "actual_quality": r["actual_quality"],
                    "challan_quality_id": r["challan_quality_id"],
                    "w": qty,
                    "rate": float(r["rate"] or 0),
                    "price": line_price(qty, float(r["rate"] or 0)),
                    "warehouse_id": r["warehouse_id"],
                    "actual_qty": aq_delta,
                    "marka": r["marka"],
                    "crop_year": r["crop_year"],
                    "unit_conversion": r["unit_conversion"],
                })
            conn.execute(text("""
                INSERT INTO jute_lot_src
                    (new_jute_mr_li_id, src_jute_mr_li_id, qty_kg,
                     actual_qty_delta, actual_weight_delta,
                     created_by, created_date_time)
                VALUES (:new_li, :src_li, :qty, :aq, :aw, :by, NOW())
            """), {"new_li": new_li_id, "src_li": li_id, "qty": qty,
                   "aq": aq_delta, "aw": aw_delta, "by": updated_by})

        _recompute_mr_header(conn, lot_mr_id, updated_by)
        return lot_mr_id


def delete_lot(lot_mr_id: int, updated_by: int) -> None:
    """Undo a lot MR: restore every source line exactly from jute_lot_src,
    then delete provenance rows, lines, and header.

    Blocks if any MR references the lot as source (marked move or chain), or
    if any lot line feeds a newer lot."""
    with DatabaseConnection.get_transaction() as conn:
        header = conn.execute(text("""
            SELECT jute_mr_id, transfer_mode FROM jute_mr
            WHERE jute_mr_id = :id FOR UPDATE
        """), {"id": lot_mr_id}).fetchone()
        if not header:
            raise ValueError(f"MR {lot_mr_id} not found")
        if int(header._mapping["transfer_mode"] or 0) != 0:
            raise ValueError("Not a lot MR (transfer_mode != 0)")

        prov = conn.execute(text("""
            SELECT ls.lot_src_id, ls.new_jute_mr_li_id, ls.src_jute_mr_li_id,
                   ls.qty_kg, ls.actual_qty_delta, ls.actual_weight_delta
            FROM jute_lot_src ls
            JOIN jute_mr_li li ON li.jute_mr_li_id = ls.new_jute_mr_li_id
            WHERE li.jute_mr_id = :id
            FOR UPDATE
        """), {"id": lot_mr_id}).fetchall()
        if not prov:
            raise ValueError(f"MR {lot_mr_id} is not an app-created lot MR")

        issued = conn.execute(text("""
            SELECT 1 FROM jute_issue ji
            JOIN jute_mr_li li ON li.jute_mr_li_id = ji.jute_mr_li_id
            WHERE li.jute_mr_id = :id AND COALESCE(ji.status_id, 0) <> 4
            LIMIT 1
        """), {"id": lot_mr_id}).fetchone()
        if issued:
            raise ValueError(
                "Lot has issue entries against it in the ERP; cannot delete"
            )

        if conn.execute(text("""
            SELECT 1 FROM jute_mr
            WHERE src_jute_mr_id = :id AND jute_mr_id <> :id LIMIT 1
        """), {"id": lot_mr_id}).fetchone():
            raise ValueError("Lot has dependent MRs (transfer exists); delete those first")

        lot_line_ids = sorted({int(p._mapping["new_jute_mr_li_id"]) for p in prov})
        feeds_stmt = text("""
            SELECT 1 FROM jute_lot_src
            WHERE src_jute_mr_li_id IN :ids LIMIT 1
        """).bindparams(bindparam("ids", expanding=True))
        if conn.execute(feeds_stmt, {"ids": lot_line_ids}).fetchone():
            raise ValueError("Lot lines feed a newer lot; delete that lot first")

        # Restore sources exactly from provenance (no quality matching), in
        # ascending line order; put back accepted AND actual amounts.
        restore = sorted(
            ((int(p._mapping["src_jute_mr_li_id"]), float(p._mapping["qty_kg"]),
              float(p._mapping["actual_qty_delta"] or 0),
              float(p._mapping["actual_weight_delta"] or 0))
             for p in prov),
            key=lambda t: t[0],
        )
        src_mr_ids = set()
        for src_li_id, qty, aq_delta, aw_delta in restore:
            src = conn.execute(text("""
                SELECT jute_mr_li_id, jute_mr_id, accepted_weight, rate,
                       actual_qty, actual_weight
                FROM jute_mr_li WHERE jute_mr_li_id = :id FOR UPDATE
            """), {"id": src_li_id}).fetchone()
            if not src:
                raise ValueError(f"Source line {src_li_id} vanished; cannot restore")
            s = src._mapping
            new_w = round(float(s["accepted_weight"] or 0) + qty, 3)
            conn.execute(text("""
                UPDATE jute_mr_li
                SET accepted_weight = :w, total_price = :p,
                    actual_weight = :aw, actual_qty = :aq,
                    updated_date_time = NOW()
                WHERE jute_mr_li_id = :id
            """), {"w": new_w, "p": line_price(new_w, float(s["rate"] or 0)),
                   "aw": round(float(s["actual_weight"] or 0) + aw_delta, 3),
                   "aq": round(float(s["actual_qty"] or 0) + aq_delta, 3),
                   "id": src_li_id})
            src_mr_ids.add(int(s["jute_mr_id"]))
        for mr_id in sorted(src_mr_ids):
            _recompute_mr_header(conn, mr_id, updated_by)

        del_prov = text(
            "DELETE FROM jute_lot_src WHERE new_jute_mr_li_id IN :ids"
        ).bindparams(bindparam("ids", expanding=True))
        conn.execute(del_prov, {"ids": lot_line_ids})
        conn.execute(text("DELETE FROM jute_mr_li WHERE jute_mr_id = :id"),
                     {"id": lot_mr_id})
        conn.execute(text("DELETE FROM jute_mr WHERE jute_mr_id = :id"),
                     {"id": lot_mr_id})
```

- [ ] **Step 2: Import validation**

Run: `python -c "from src.jutetransfer import lot_ops; print('OK')"`
Expected: `OK` (also proves no circular import: lot_ops → warehouse_stock_ops → transfer → queries/database).

- [ ] **Step 3: Run existing test suite (no regressions)**

Run: `pytest tests/ -v`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add src/jutetransfer/lot_ops.py
git commit -m "feat: lot_ops - create_lot/delete_lot with jute_lot_src provenance"
```

---

### Task 7: Batch marked transfer + consumption-block on delete

**Files:**
- Modify: `src/jutetransfer/warehouse_stock_ops.py` (append `save_marked_batch`; edit `delete_marked_move` ~line 209)

**Interfaces:**
- Consumes: `lot_helpers.apply_pct/line_price` (Task 3); existing `_recompute_mr_header`, `_ensure_company_as_party`, `_ensure_item`, `_get_next_*` helpers (already imported at top of file).
- Produces (used by Task 9):
  - `save_marked_batch(lot_li_ids: list[int], pct_change: float, target_co_id: int, target_branch_id: int, warehouse_id: int, mr_date: date, updated_by: int) -> list[int]` — returns created child MR ids, one per source MR.
  - `delete_marked_move` — unchanged signature, now raises `ValueError` if ERP issue entries exist against the child (consumption started), and restores sources exactly via `jute_lot_src` provenance when present.

- [ ] **Step 1: Verify shared helpers exist**

Task 6 added `from .lot_helpers import apply_pct, line_price` plus
`_BALANCE_SQL` / `_available_kg` / `_reduce_source_line` / `_LI_INSERT_SQL` to
`warehouse_stock_ops.py`. Verify they are present before starting.

- [ ] **Step 2: Implement `save_marked_batch`**

```python
def save_marked_batch(
    lot_li_ids: list,
    pct_change: float,
    target_co_id: int,
    target_branch_id: int,
    warehouse_id: int,
    mr_date: date,
    updated_by: int,
) -> list:
    """Move N whole lots into a marked godown in one transaction.

    Selected lines are grouped by source MR; each source MR gets ONE child MR
    (transfer_mode=1) holding its selected lines at rate * (1 + pct/100).
    The moved amount per line is the balance-aware available kg
    (LEAST(view balance, accepted_weight)) — weight already issued to
    production stays behind. Provenance rows are written to jute_lot_src so
    deletion restores sources exactly. Returns the created child MR ids.
    """
    if not lot_li_ids:
        raise ValueError("no lots selected")
    ids = sorted({int(x) for x in lot_li_ids})

    with DatabaseConnection.get_transaction() as conn:
        rows = []
        for li_id in ids:  # ascending lock order
            row = conn.execute(text("""
                SELECT li.jute_mr_li_id, li.accepted_weight, li.rate,
                       li.actual_item_id, li.actual_quality, li.challan_quality_id,
                       li.marka, li.crop_year, li.unit_conversion,
                       li.actual_qty, li.actual_weight, li.actual_rate,
                       li.jute_mr_id, mr.branch_id AS src_branch_id,
                       mr.transfer_mode, mr.status_id, bm.co_id AS src_co_id
                FROM jute_mr_li li
                JOIN jute_mr mr ON mr.jute_mr_id = li.jute_mr_id
                JOIN branch_mst bm ON bm.branch_id = mr.branch_id
                WHERE li.jute_mr_li_id = :id
                FOR UPDATE
            """), {"id": li_id}).fetchone()
            if not row:
                raise ValueError(f"Source line {li_id} not found")
            r = dict(row._mapping)
            if int(r["transfer_mode"] or 0) != 0:
                raise ValueError("Can only mark-move from normal (transfer_mode=0) stock")
            if int(r["status_id"] or 0) != 3:
                raise ValueError("Can only mark-move Approved (status 3) MRs")
            r["moved_kg"] = _available_kg(
                conn, li_id, float(r["accepted_weight"] or 0)
            )
            if r["moved_kg"] <= 0:
                raise ValueError(f"Line {li_id} has no available weight")
            rows.append(r)

        checked = set()
        for r in rows:
            mr_id = int(r["jute_mr_id"])
            if mr_id in checked:
                continue
            chain_child = conn.execute(text("""
                SELECT 1 FROM jute_mr
                WHERE src_jute_mr_id = :sid AND transfer_mode = 0
                  AND jute_mr_id <> :sid
                LIMIT 1
            """), {"sid": mr_id}).fetchone()
            if chain_child:
                raise ValueError(
                    f"MR {mr_id} is part of a vertical transfer chain; "
                    "mark-move is disabled to avoid corrupting it"
                )
            checked.add(mr_id)

        by_mr = {}
        for r in rows:
            by_mr.setdefault(int(r["jute_mr_id"]), []).append(r)

        child_ids = []
        for src_mr_id in sorted(by_mr):
            grp = by_mr[src_mr_id]
            src_co_id = int(grp[0]["src_co_id"])
            src_branch_id = int(grp[0]["src_branch_id"])
            party_id, party_branch_id = _ensure_company_as_party(
                conn, src_co_id, src_branch_id, target_co_id, updated_by
            )
            child_mr_id = DatabaseConnection.execute_insert_returning_id(conn, """
                INSERT INTO jute_mr (
                    jute_gate_entry_no, branch_mr_no, jute_gate_entry_date, jute_mr_date,
                    status_id, transfer_mode, updated_by, updated_date_time,
                    branch_id, party_id, party_branch_id, src_com_id, src_jute_mr_id,
                    total_amount, claim_amount, roundoff, net_total,
                    bill_pass_no, bill_pass_date
                ) VALUES (
                    :gate_no, :mr_no, :mr_date, :mr_date,
                    3, 1, :updated_by, NOW(),
                    :branch_id, :party_id, :party_branch_id, :src_com_id, :src_jute_mr_id,
                    0, 0, 0, 0,
                    :bill_pass_no, :mr_date
                )
            """, {
                "gate_no": _get_next_gate_entry_no(conn, target_branch_id),
                "mr_no": _get_next_mr_number_in_txn(conn, target_branch_id, mr_date),
                "mr_date": mr_date,
                "updated_by": updated_by,
                "branch_id": target_branch_id,
                "party_id": party_id,
                "party_branch_id": party_branch_id,
                "src_com_id": src_co_id,
                "src_jute_mr_id": src_mr_id,  # direct parent (mode-1 semantics)
                "bill_pass_no": _get_next_bill_pass_no_in_txn(conn, target_branch_id),
            })

            for r in grp:
                moved = float(r["moved_kg"])
                new_rate = apply_pct(float(r["rate"] or 0), pct_change)
                src_item_id = r["actual_item_id"]
                target_item_id = (
                    _ensure_item(conn, int(src_item_id), target_co_id, updated_by)
                    if src_item_id else None
                )
                aq_delta, aw_delta = _reduce_source_line(conn, r, moved, moved)
                child_li_id = DatabaseConnection.execute_insert_returning_id(
                    conn, _LI_INSERT_SQL, {
                        "mr_id": child_mr_id,
                        "actual_item_id": target_item_id,
                        "actual_quality": r["actual_quality"],
                        "challan_quality_id": r["challan_quality_id"],
                        "w": moved,
                        "rate": new_rate,
                        "price": line_price(moved, new_rate),
                        "warehouse_id": warehouse_id,
                        "actual_qty": aq_delta,
                        "marka": r["marka"],
                        "crop_year": r["crop_year"],
                        "unit_conversion": r["unit_conversion"],
                    })
                conn.execute(text("""
                    INSERT INTO jute_lot_src
                        (new_jute_mr_li_id, src_jute_mr_li_id, qty_kg,
                         actual_qty_delta, actual_weight_delta,
                         created_by, created_date_time)
                    VALUES (:new_li, :src_li, :qty, :aq, :aw, :by, NOW())
                """), {"new_li": child_li_id,
                       "src_li": int(r["jute_mr_li_id"]),
                       "qty": moved, "aq": aq_delta, "aw": aw_delta,
                       "by": updated_by})

            _recompute_mr_header(conn, src_mr_id, updated_by)
            _recompute_mr_header(conn, child_mr_id, updated_by)
            child_ids.append(child_mr_id)

        return child_ids
```

- [ ] **Step 3: Consumption-block + exact restore in `delete_marked_move`**

Two changes to `delete_marked_move`:

**(a)** Right after the `grandchild` guard (~line 226), add a consumption
block — deletion is forbidden once ERP issue entries exist against the child:

```python
        issued = conn.execute(text("""
            SELECT 1 FROM jute_issue ji
            JOIN jute_mr_li li ON li.jute_mr_li_id = ji.jute_mr_li_id
            WHERE li.jute_mr_id = :id AND COALESCE(ji.status_id, 0) <> 4
            LIMIT 1
        """), {"id": child_mr_id}).fetchone()
        if issued:
            raise ValueError(
                "This marked MR has ERP issue entries (consumption started); "
                "cannot delete"
            )
```

**(b)** Before the legacy quality-match restore block, add a provenance-based
exact restore: if `jute_lot_src` rows exist for the child's lines (all batches
created by `save_marked_batch` write them), restore each source line exactly
and skip the quality-match path entirely. The legacy path remains only for
pre-provenance marked MRs (none exist in production — mode-1 count is 0).

```python
        prov = conn.execute(text("""
            SELECT ls.src_jute_mr_li_id, ls.qty_kg,
                   ls.actual_qty_delta, ls.actual_weight_delta,
                   ls.new_jute_mr_li_id
            FROM jute_lot_src ls
            JOIN jute_mr_li li ON li.jute_mr_li_id = ls.new_jute_mr_li_id
            WHERE li.jute_mr_id = :id
            FOR UPDATE
        """), {"id": child_mr_id}).fetchall()
        if prov:
            src_mr_ids = set()
            for p in sorted(prov, key=lambda p: int(p._mapping["src_jute_mr_li_id"])):
                m = p._mapping
                src = conn.execute(text("""
                    SELECT jute_mr_li_id, jute_mr_id, accepted_weight, rate,
                           actual_qty, actual_weight
                    FROM jute_mr_li WHERE jute_mr_li_id = :id FOR UPDATE
                """), {"id": int(m["src_jute_mr_li_id"])}).fetchone()
                if not src:
                    raise ValueError(
                        f"Source line {m['src_jute_mr_li_id']} vanished; cannot restore"
                    )
                s = src._mapping
                qty = float(m["qty_kg"] or 0)
                new_w = round(float(s["accepted_weight"] or 0) + qty, 3)
                conn.execute(text("""
                    UPDATE jute_mr_li
                    SET accepted_weight = :w, total_price = :p,
                        actual_weight = :aw, actual_qty = :aq,
                        updated_date_time = NOW()
                    WHERE jute_mr_li_id = :id
                """), {"w": new_w,
                       "p": _round2(new_w * float(s["rate"] or 0) / 100.0),
                       "aw": round(float(s["actual_weight"] or 0)
                                   + float(m["actual_weight_delta"] or 0), 3),
                       "aq": round(float(s["actual_qty"] or 0)
                                   + float(m["actual_qty_delta"] or 0), 3),
                       "id": int(s["jute_mr_li_id"])})
                src_mr_ids.add(int(s["jute_mr_id"]))
            for mr_id in sorted(src_mr_ids):
                _recompute_mr_header(conn, mr_id, updated_by)
            conn.execute(text("""
                DELETE ls FROM jute_lot_src ls
                JOIN jute_mr_li li ON li.jute_mr_li_id = ls.new_jute_mr_li_id
                WHERE li.jute_mr_id = :id
            """), {"id": child_mr_id})
            conn.execute(text("DELETE FROM jute_mr_li WHERE jute_mr_id = :id"),
                         {"id": child_mr_id})
            conn.execute(text("DELETE FROM jute_mr WHERE jute_mr_id = :id"),
                         {"id": child_mr_id})
            return
```

(The existing quality-match code then runs only when `prov` is empty.)

- [ ] **Step 4: Validate**

Run:
```bash
python -m src.jutetransfer.warehouse_stock_ops
python -c "from src.jutetransfer.warehouse_stock_ops import save_marked_batch, delete_marked_move; print('OK')"
pytest tests/ -v
```
Expected: `warehouse_stock_ops self-check OK`, `OK`, all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/jutetransfer/warehouse_stock_ops.py
git commit -m "feat: save_marked_batch (one child MR per source MR, common pct) + sold-block on delete"
```

---

### Task 8: Page rewrite — tab scaffold + Lots tab

**Files:**
- Modify: `src/jutetransfer/pages/warehouse_stock.py` (full rewrite)

**Interfaces:**
- Consumes: `get_available_lots`, `get_quality_availability_summary` (Task 4), `get_lot_provenance`, `get_marked_stock_with_sold` (Task 5), `lot_ops.create_lot/delete_lot` (Task 6), `save_marked_batch/delete_marked_move` (Task 7), existing `get_companies/get_branches_by_company/get_company_branch_options/get_warehouses_by_branch/get_marked_warehouses_by_branch/set_warehouse_marked`.
- Produces: `warehouse_stock_page()` (same export — `app.py:107` keeps working); `_lot_grid(df, key) -> pd.DataFrame` (selected rows) reused by Task 9; `_render_transfer_tab` / `_render_marked_tab` placeholders replaced by Tasks 9-10.

- [ ] **Step 1: Rewrite the file with tabs + Lots tab; Transfer/Marked tabs as placeholders**

```python
"""Warehouse-marked stock page: Lots / Transfer / Marked Stock tabs.

Lots: quality-wise availability + split/merge into app-created lot MRs
(jute_lot_src provenance). Transfer: multi-lot whole-lot batch move into a
marked godown with a common % rate change. Marked Stock: mode-1 stock with
auto sold detection (raw-jute invoice join).
"""

from datetime import date, datetime

import pandas as pd
import streamlit as st
from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode

from ..queries import (
    get_companies,
    get_branches_by_company,
    get_company_branch_options,
    get_available_lots,
    get_quality_availability_summary,
    get_marked_stock_with_balance,
    get_lot_provenance,
    get_warehouses_by_branch,
    get_marked_warehouses_by_branch,
    set_warehouse_marked,
)
from ..lot_ops import create_lot, delete_lot
from ..warehouse_stock_ops import save_marked_batch, delete_marked_move


def _lot_grid(df: pd.DataFrame, key: str) -> pd.DataFrame:
    """Multi-select grid over available lots. Returns selected rows as a
    DataFrame (empty if none)."""
    gb = GridOptionsBuilder.from_dataframe(df)
    gb.configure_default_column(resizable=True, filterable=True, sortable=True)
    gb.configure_selection("multiple", use_checkbox=True)
    gb.configure_column("jute_mr_id", hide=True)
    gb.configure_column("jute_mr_li_id", hide=True)
    resp = AgGrid(
        df,
        gridOptions=gb.build(),
        height=320,
        theme="streamlit",
        update_mode=GridUpdateMode.SELECTION_CHANGED,
        key=key,
    )
    sel = resp.get("selected_rows")
    if sel is None:
        return pd.DataFrame()
    return pd.DataFrame(sel)  # handles list[dict] and DataFrame variants


def _render_lots_tab(co_id: int, branch_id: int, year: int, month: int,
                     user_id: int) -> None:
    summary = get_quality_availability_summary(co_id, branch_id, year, month)
    st.subheader("Available by quality")
    if summary is None or summary.empty:
        st.info("No available stock for this filter.")
        return
    st.dataframe(summary, use_container_width=True, hide_index=True)

    lots = get_available_lots(co_id, branch_id, year, month)
    st.subheader("Lots")
    sel = _lot_grid(lots, key="lots_grid")

    st.markdown("**Create a new lot** — take quantities from the selected lines")
    if sel.empty:
        st.caption("Select one or more lines above.")
    else:
        takes = []
        for _, row in sel.iterrows():
            li_id = int(row["jute_mr_li_id"])
            avail = float(row["remaining_kg"])
            k = f"take_{li_id}"
            if k not in st.session_state:
                st.session_state[k] = avail
            qty = st.number_input(
                f"{row['quality']} — MR {row['mr_no']} — available {avail:,.2f} kg",
                min_value=0.0, max_value=avail, step=100.0, key=k,
            )
            takes.append((li_id, float(qty)))
        lot_date = st.date_input("Lot date", value=date.today(), key="lot_date")
        if st.button("Create lot", type="primary", key="btn_create_lot"):
            try:
                new_id = create_lot(takes, lot_date, user_id)
                for li_id, _ in takes:
                    st.session_state.pop(f"take_{li_id}", None)
                st.success(f"Lot MR {new_id} created.")
                st.rerun()
            except Exception as e:
                st.error(str(e))

    lot_mrs = (
        lots[lots["is_lot"] == 1][["jute_mr_id", "mr_no", "mr_date"]]
        .drop_duplicates("jute_mr_id")
        if not lots.empty else pd.DataFrame()
    )
    with st.expander(f"App-created lot MRs ({len(lot_mrs)})"):
        if lot_mrs.empty:
            st.caption("None in this filter.")
        for _, lr in lot_mrs.iterrows():
            mr_id = int(lr["jute_mr_id"])
            c1, c2 = st.columns([5, 1])
            with c1:
                st.markdown(f"Lot MR **{mr_id}** (no. {lr['mr_no']}, {lr['mr_date']})")
                prov = get_lot_provenance(mr_id)
                if not prov.empty:
                    st.dataframe(prov, use_container_width=True, hide_index=True)
            with c2:
                if st.button("Delete", key=f"del_lot_{mr_id}"):
                    try:
                        delete_lot(mr_id, user_id)
                        st.success("Lot deleted; sources restored.")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))


def _render_transfer_tab(co_id: int, branch_id: int, year: int, month: int,
                         user_id: int) -> None:
    st.info("Transfer tab — implemented in the next task.")


def _render_marked_tab(co_id: int, branch_id: int, year: int, month: int,
                       user_id: int) -> None:
    st.info("Marked Stock tab — implemented in a later task.")


def warehouse_stock_page() -> None:
    st.title("Warehouse-Marked Stock")
    st.caption(
        "Restructure lots, batch-move them into marked godowns at other "
        "companies, and track marked stock until the ERP sells it."
    )

    companies = get_companies()
    if not companies:
        st.info("No companies found.")
        return
    user_id = st.session_state.get("user_id", 1)

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        co_name = st.selectbox("Company", options=list(companies.keys()))
        co_id = companies[co_name]
    with c2:
        branches = get_branches_by_company(co_id)
        br_names = list(branches.keys())
        br_name = st.selectbox("Branch", options=br_names) if br_names else None
        branch_id = branches.get(br_name) if br_name else None
    with c3:
        this_year = datetime.now().year
        year = st.selectbox("Year", options=list(range(this_year, this_year - 6, -1)))
    with c4:
        month = st.selectbox("Month", options=list(range(1, 13)),
                             index=datetime.now().month - 1)
    if not branch_id:
        st.info("Select a branch.")
        return

    tab_lots, tab_transfer, tab_marked = st.tabs(
        ["Lots", "Transfer", "Marked Stock"]
    )
    with tab_lots:
        _render_lots_tab(co_id, branch_id, year, month, user_id)
    with tab_transfer:
        _render_transfer_tab(co_id, branch_id, year, month, user_id)
    with tab_marked:
        _render_marked_tab(co_id, branch_id, year, month, user_id)
```

Notes:
- This rewrite drops the old per-line move form and old marked list — Tasks 9 and 10 replace them. `get_jute_mr_with_line_items` and `save_marked_move` are no longer imported by the page but remain in the codebase.
- The old `this_year = datetime.now().month and datetime.now().year` oddity is fixed to `datetime.now().year`.
- Keep the unused-at-this-point imports (`get_marked_stock_with_sold`, `save_marked_batch`, `delete_marked_move`, `get_company_branch_options`, warehouse helpers) — Tasks 9-10 use them.

- [ ] **Step 2: Import validation**

Run: `python -c "from src.jutetransfer.pages import warehouse_stock; print('OK')"`
Expected: `OK`.

- [ ] **Step 3: Manual check in the app**

Run: `streamlit run app.py` → Warehouse Stock page.
Expected: 3 tabs render; Lots tab shows quality summary + grid; selecting lines shows take inputs; creating a lot from a partial take splits the line (summary totals unchanged); deleting the lot restores it.

- [ ] **Step 4: Commit**

```bash
git add src/jutetransfer/pages/warehouse_stock.py
git commit -m "feat(page): 3-tab warehouse page with Lots tab (split/merge, provenance, undo)"
```

---

### Task 9: Transfer tab

**Files:**
- Modify: `src/jutetransfer/pages/warehouse_stock.py` (replace `_render_transfer_tab`)

**Interfaces:**
- Consumes: `_lot_grid` (Task 8), `save_marked_batch` (Task 7), `apply_pct`/`line_price` from `lot_helpers` (Task 3), `get_company_branch_options`, `get_marked_warehouses_by_branch`.
- Produces: working Transfer tab.

- [ ] **Step 1: Implement**

Add import at top of the page file:

```python
from ..lot_helpers import apply_pct, line_price
```

Replace `_render_transfer_tab`:

```python
def _render_transfer_tab(co_id: int, branch_id: int, year: int, month: int,
                         user_id: int) -> None:
    lots = get_available_lots(co_id, branch_id, year, month)
    if lots is None or lots.empty:
        st.info("No available lots for this filter.")
        return
    st.subheader("Select lots to transfer (whole lots — split first for partials)")
    sel = _lot_grid(lots, key="transfer_grid")
    if sel.empty:
        st.caption("Select one or more lots above.")
        return

    cb_options, cb_map = get_company_branch_options()
    t1, t2, t3, t4 = st.columns(4)
    with t1:
        cb_label = st.selectbox("To company/branch", options=cb_options,
                                key="xfer_cb")
        tgt = cb_map.get(cb_label)
    with t2:
        if tgt:
            tgt_co, tgt_br = tgt
            mwh = get_marked_warehouses_by_branch(tgt_br)
            wh_name = st.selectbox("Marked godown",
                                   options=list(mwh.keys()) or ["(none tagged)"],
                                   key="xfer_wh")
            wh_id = mwh.get(wh_name)
        else:
            tgt_co = tgt_br = wh_id = None
            st.selectbox("Marked godown", options=["(select company)"],
                         key="xfer_wh")
    with t3:
        move_date = st.date_input("Date", value=date.today(), key="xfer_dt")
    with t4:
        if "xfer_pct" not in st.session_state:
            st.session_state["xfer_pct"] = 0.0
        pct = st.number_input("% rate change (+/-)", step=0.5, key="xfer_pct")

    prev = sel.copy()
    prev["new_rate"] = prev["rate"].map(lambda r: apply_pct(float(r or 0), pct))
    prev["value"] = prev.apply(
        lambda r: line_price(float(r["remaining_kg"]), float(r["rate"] or 0)), axis=1)
    prev["new_value"] = prev.apply(
        lambda r: line_price(float(r["remaining_kg"]), float(r["new_rate"])), axis=1)
    show = prev[["mr_no", "quality", "remaining_kg", "rate", "new_rate",
                 "value", "new_value"]]
    st.subheader("Preview")
    st.dataframe(show, use_container_width=True, hide_index=True)
    st.markdown(
        f"**Total: {prev['remaining_kg'].sum():,.2f} kg — "
        f"value {prev['value'].sum():,.2f} → {prev['new_value'].sum():,.2f}**"
    )

    n_src_mrs = prev["jute_mr_id"].nunique()
    st.caption(f"Will create {n_src_mrs} MR(s) at the target (one per source MR).")
    can_save = bool(tgt) and bool(wh_id)
    if st.button("Transfer selected lots", type="primary",
                 disabled=not can_save, key="btn_batch_move"):
        try:
            li_ids = [int(x) for x in prev["jute_mr_li_id"]]
            child_ids = save_marked_batch(
                li_ids, float(pct), int(tgt_co), int(tgt_br), int(wh_id),
                move_date, user_id,
            )
            st.success(f"Transferred. Created MR(s): {', '.join(map(str, child_ids))}")
            st.rerun()
        except Exception as e:
            st.error(str(e))
```

- [ ] **Step 2: Import validation**

Run: `python -c "from src.jutetransfer.pages import warehouse_stock; print('OK')"`
Expected: `OK`.

- [ ] **Step 3: Manual check**

Run: `streamlit run app.py` → Transfer tab: select 2+ lots from different MRs, set target + %, verify preview math (rate × (1+pct/100), 2 dp), save, confirm one child MR per source MR reported.

- [ ] **Step 4: Commit**

```bash
git add src/jutetransfer/pages/warehouse_stock.py
git commit -m "feat(page): batch transfer tab - multi-lot, common pct, preview, one child MR per source"
```

---

### Task 10: Marked Stock tab

**Files:**
- Modify: `src/jutetransfer/pages/warehouse_stock.py` (replace `_render_marked_tab`)

**Interfaces:**
- Consumes: `get_marked_stock_with_sold` (Task 5), `delete_marked_move` (Task 7), `get_lot_provenance` (Task 5), `get_warehouses_by_branch`, `get_marked_warehouses_by_branch`, `set_warehouse_marked`.
- Produces: working Marked Stock tab; completes the page.

- [ ] **Step 1: Implement**

```python
def _render_marked_tab(co_id: int, branch_id: int, year: int, month: int,
                       user_id: int) -> None:
    with st.expander("Tag godowns as marked"):
        all_wh = get_warehouses_by_branch(branch_id)
        marked_wh = get_marked_warehouses_by_branch(branch_id)
        if not all_wh:
            st.write("No godowns for this branch.")
        else:
            chosen = st.multiselect(
                "Marked godowns (this branch)",
                options=list(all_wh.keys()),
                default=list(marked_wh.keys()),
            )
            if st.button("Save godown tags"):
                chosen_ids = {int(all_wh[n]) for n in chosen}
                for _name, wid in all_wh.items():
                    set_warehouse_marked(int(wid), int(wid) in chosen_ids)
                st.success("Godown tags updated.")
                st.rerun()

    mk = get_marked_stock_with_balance(co_id, branch_id, year, month)
    st.subheader("Marked stock here")
    if mk is None or mk.empty:
        st.info("No marked stock for this filter.")
        return

    in_stock_val = mk.loc[mk["consumed"] == 0, "value"].sum()
    st.markdown(
        f"**Remaining marked value: {in_stock_val:,.2f}** "
        f"(balance-priced; consumed lots drop out of P&L automatically)"
    )

    for mr_id, grp in mk.groupby("jute_mr_id", sort=True):
        mr_id = int(mr_id)
        consumed = bool((grp["consumed"] == 1).all())
        partially = bool((grp["balance_kg"] < grp["kg"]).any()) and not consumed
        src_raw = grp["src_jute_mr_id"].iloc[0]
        src_label = int(src_raw) if pd.notna(src_raw) else "-"
        head = (
            f"MR {mr_id} (no. {grp['mr_no'].iloc[0]}, {grp['mr_date'].iloc[0]}) "
            f"— source MR {src_label}"
        )
        if partially:
            head += " — **partially consumed**"
        c1, c2 = st.columns([5, 1])
        with c1:
            st.markdown(("~~" + head + "~~ **CONSUMED**") if consumed else head)
            st.dataframe(
                grp[["quality", "kg", "balance_kg", "rate", "value", "godown"]],
                use_container_width=True, hide_index=True,
            )
            prov = get_lot_provenance(mr_id)
            if not prov.empty:
                with st.expander("Source provenance"):
                    st.dataframe(prov, use_container_width=True, hide_index=True)
        with c2:
            can_delete = bool((grp["balance_kg"] >= grp["kg"]).all())
            if st.button("Delete", key=f"del_mk_{mr_id}", disabled=not can_delete):
                try:
                    delete_marked_move(mr_id, user_id)
                    st.success("Deleted; source restored.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
```

(Marked children now carry provenance rows themselves, so
`get_lot_provenance(mr_id)` works directly on the marked MR. Delete is
disabled the moment any weight has been issued — matching the ops-level
consumption block.)

- [ ] **Step 2: Import validation + full import sweep**

Run:
```bash
python -c "from src.jutetransfer import jute_mr_chain_helpers, transfer, warehouse_stock_ops, lot_ops, lot_helpers; from src.jutetransfer.pages import new_transfer_chain, warehouse_stock, schema_viewer, company_pl_dashboard; print('OK')"
```
Expected: `OK`.

- [ ] **Step 3: Manual check**

Run: `streamlit run app.py` → Marked Stock tab: godown tagging works; marked MRs grouped with lines showing kg vs balance_kg; consumed MRs struck through with Delete disabled; deleting an untouched marked MR restores the source lot exactly.

- [ ] **Step 4: Commit**

```bash
git add src/jutetransfer/pages/warehouse_stock.py
git commit -m "feat(page): marked stock tab - balance-based consumption, provenance drill-down, guarded delete"
```

---

### Task 11: Documentation updates

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/TRANSFER_PROCESS_UNDERSTANDING.md`

- [ ] **Step 1: CLAUDE.md edits**

1. In **Database integration facts**, extend the juteTransfer-only bullet: add `jute_lot_src` as an app-owned sls-only *table* (line-level lot provenance).
2. Replace the constraint sentence "Root MRs are created by VoWERP gate entry, never by this app." with: "Gate-entry root MRs are created by VoWERP, never by this app. App-created **lot MRs** (re-allocations at the same company) are the one exception: `transfer_mode=0`, `status_id=3`, `src_jute_mr_id` NULL, always traceable via `jute_lot_src`."
3. In the **Type 2** column of the two-transfer-types table, update Status to "Built: lot management (split/merge via lot MRs), batch transfer with common % change, balance-based consumption via `vw_jute_stock_outstanding` (ERP issue entries reduce balance; consumed = balance ≤ 0)" and the Ops row to `warehouse_stock_ops.py` + `lot_ops.py`.
4. In **Module Organization**, add `lot_helpers.py` (pure lot math) and `lot_ops.py` (lot MR create/delete) lines.
5. Update the **Key Constraints** footer line to the amended root-MR rule, and bump **Last Updated**.

- [ ] **Step 2: TRANSFER_PROCESS_UNDERSTANDING.md edit**

Append a section "2026-08-03: Lot management & batch transfer" summarising: lot MR concept, `jute_lot_src` (incl. actual-weight deltas), whole-lot batch transfer (one child per source MR), the stop-gate result (`sales_invoice_jute.mr_id` unusable — 0/2,980 in 2025-26) and the owner's balance-based consumption ruling (`vw_jute_stock_outstanding`), closing the doc's open question 2.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/TRANSFER_PROCESS_UNDERSTANDING.md
git commit -m "docs: lot management concepts, amended root-MR constraint, sold-detection decision"
```

---

### Task 12: Integration verification (spec §9 checklist)

**Files:** none new — manual pass against the running app.

- [ ] **Step 1: Full automated sweep**

Run:
```bash
pytest tests/ -v
python -m src.jutetransfer.warehouse_stock_ops
python -c "from src.jutetransfer import jute_mr_chain_helpers, transfer, warehouse_stock_ops, lot_ops, lot_helpers; from src.jutetransfer.pages import new_transfer_chain, warehouse_stock, schema_viewer, company_pl_dashboard; print('OK')"
```
Expected: all PASS / OK.

- [ ] **Step 2: Manual checklist in `streamlit run app.py`** (tick each)

- [ ] Split: create lot taking 4000 of a 6000 kg line → source shows 2000, lot MR shows 4000; quality summary total unchanged.
- [ ] Cross-MR merge: create one lot from lines of two different MRs → one lot MR, two lines, provenance shows both sources.
- [ ] Batch transfer: select lots from ≥2 source MRs → one child MR per source MR; rates = old × (1+pct/100); source lines at 0 kg vanish from availability.
- [ ] Negative %: preview and saved rates go down.
- [ ] Undo lot: delete unused lot MR → sources restored to exact weights (accepted AND actual).
- [ ] Undo marked move: delete untouched marked MR → source lots restored exactly via provenance.
- [ ] Delete blocked: lot with transferred lines; marked MR (or lot) with ERP issue entries.
- [ ] Chain guard: line of an MR feeding a live chain is absent from availability.
- [ ] Balance: a jute_issue entry against a marked line reduces its balance_kg on the Marked Stock tab and its value in P&L marked stock (view-driven, no app write needed).
- [ ] P&L: `Company P&L` page loads; marked stock values remaining balance only.

- [ ] **Step 3: Final commit if fixes were needed, then push**

```bash
git push origin stock-transfer
```
