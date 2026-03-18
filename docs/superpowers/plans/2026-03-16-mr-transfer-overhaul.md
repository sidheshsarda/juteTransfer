# MR Transfer Overhaul Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Overhaul the jute transfer system to group MRs by header, apply per-line-item rate cascading with proper rounding, track chains via `src_com_id`, and support partial saves with edit/delete.

**Architecture:** The changes span three files: `queries.py` (ownership queries via `branch_id`), `transfer.py` (per-step save/delete, field fixes, per-item rates), and `jute_mr.py` (grouping, recalculation, UI). A new `tests/` directory is added for pure-function unit tests on calculation and chain reconstruction logic.

**Tech Stack:** Python 3.12, Streamlit, SQLAlchemy, MySQL, pandas, pytest

**Spec:** `docs/superpowers/specs/2026-03-16-mr-grouping-per-item-rate-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `src/jutetransfer/queries.py` | Modify | Update ownership queries to use `branch_id`; add `get_transfer_chain`, `get_warehouses_by_branch` |
| `src/jutetransfer/transfer.py` | Modify | Field fixes, per-item rates via `rate_multiplier`, `save_transfer_step`, `delete_transfer_step`, `revert_original_mr` |
| `src/jutetransfer/pages/jute_mr.py` | Modify | Group by `jute_mr_id`, recalculation with rounding, chain hydration from DB, partial save UI |
| `tests/conftest.py` | Create | Pytest fixtures for mock DB connections |
| `tests/test_recalculation.py` | Create | Unit tests for `_recalculate_chain` (rounding, claim passthrough, cumulative %) |
| `tests/test_chain_reconstruction.py` | Create | Unit tests for chain reconstruction algorithm |
| `tests/test_grouping.py` | Create | Unit tests for MR grouping logic |
| `pyproject.toml` | Modify | Add pytest dependency |

---

## Chunk 1: Test Setup + Pure Function Tests

### Task 1: Set up pytest

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Add pytest to dev dependencies in pyproject.toml**

Add under `[project.optional-dependencies]`:
```toml
[project.optional-dependencies]
dev = ["pytest>=8.0.0"]
```

- [ ] **Step 2: Create test files**

Create `tests/__init__.py` (empty) and `tests/conftest.py`:
```python
"""Shared test fixtures."""
```

- [ ] **Step 3: Verify pytest runs**

Run: `cd c:/code/juteTransfer && uv run pytest tests/ -v`
Expected: "no tests ran" (0 collected), no errors

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml tests/
git commit -m "feat: add pytest test infrastructure"
```

---

### Task 2: Recalculation unit tests

**Files:**
- Create: `tests/test_recalculation.py`

These tests define the expected behavior of `_recalculate_chain` BEFORE we rewrite it. The function will be extracted as a pure function that takes line items and steps, returns updated steps.

- [ ] **Step 1: Write tests for single line item, no rate increase**

```python
"""Tests for _recalculate_chain with rounding rules."""
from src.jutetransfer.pages.jute_mr import _recalculate_chain


def _make_line_items(items):
    """Helper: create line_items list from (weight, rate, claim) tuples."""
    return [{"weight": w, "original_rate": r, "original_claim": c} for w, r, c in items]


def _make_step(company="CoB", pct=0.0):
    """Helper: create a minimal step dict."""
    return {
        "company": company,
        "mr_date": None,
        "pct_rate_increase": pct,
        "mr_no": None,
        "warehouse_id": None,
    }


def test_single_item_no_increase():
    """Single line item, one step, no % increase."""
    line_items = _make_line_items([(1000, 2500, 50)])  # 1000kg, 2500 Rs/q, 50 claim
    steps = [_make_step()]
    steps = _recalculate_chain(steps, line_items)

    assert steps[0]["total_amount"] == 25000  # round(1000 * 2500/100, 0) = 25000
    assert steps[0]["claim_amount"] == 50     # round(50, 0)
    assert steps[0]["net_amount"] == 24950
    assert steps[0]["roundoff"] == 0.0


def test_single_item_with_increase():
    """Single line item, two steps, 10% increase on step 2."""
    line_items = _make_line_items([(1000, 2500, 50)])
    steps = [_make_step("CoB"), _make_step("CoC", pct=10.0)]
    steps = _recalculate_chain(steps, line_items)

    # Step 0: original rate
    assert steps[0]["total_amount"] == 25000

    # Step 1: 2500 * 1.10 = 2750 → 1000 * 2750/100 = 27500
    assert steps[1]["total_amount"] == 27500
    assert steps[1]["claim_amount"] == 50  # claim unaffected by %
    assert steps[1]["net_amount"] == 27450
```

- [ ] **Step 2: Write tests for multiple line items with rounding**

```python
def test_multiple_items_rounding():
    """Multiple line items with different rates; verify rounding rules."""
    line_items = _make_line_items([
        (500, 2450, 30),   # li1: 500 * 2450/100 = 12250.00
        (750, 2800, 45),   # li2: 750 * 2800/100 = 21000.00
    ])
    steps = [_make_step()]
    steps = _recalculate_chain(steps, line_items)

    # Line items: round to 2 → 12250.00 + 21000.00 = 33250.00
    # Header: round to 0 → 33250
    assert steps[0]["total_amount"] == 33250
    assert steps[0]["claim_amount"] == 75  # round(30 + 45, 0)
    assert steps[0]["roundoff"] == 0.0     # no rounding difference here


def test_rounding_creates_roundoff():
    """Verify roundoff captures the rounding difference."""
    # Construct values where line item sum has fractional part
    line_items = _make_line_items([
        (333, 2501, 10),   # li1: round(333 * 2501/100, 2) = round(8328.33, 2) = 8328.33
        (667, 2499, 20),   # li2: round(667 * 2499/100, 2) = round(16668.33, 2) = 16668.33
    ])
    steps = [_make_step()]
    steps = _recalculate_chain(steps, line_items)

    raw_total = 8328.33 + 16668.33  # 24996.66
    assert steps[0]["total_amount"] == 24997  # round(24996.66, 0)
    assert abs(steps[0]["roundoff"] - (24996.66 - 24997)) < 0.01


def test_claim_unaffected_by_pct():
    """Claim amount must NOT change when % rate increase is applied."""
    line_items = _make_line_items([(1000, 2500, 100)])
    steps = [_make_step("CoB"), _make_step("CoC", pct=20.0), _make_step("CoD", pct=15.0)]
    steps = _recalculate_chain(steps, line_items)

    # Claim stays 100 at every step
    for s in steps:
        assert s["claim_amount"] == 100
```

- [ ] **Step 3: Write test for cumulative rate cascading**

```python
def test_cumulative_cascading():
    """Step 2's rates are based on Step 1's output, not originals."""
    line_items = _make_line_items([(1000, 2000, 0)])
    steps = [
        _make_step("CoB"),
        _make_step("CoC", pct=10.0),  # 2000 * 1.10 = 2200
        _make_step("CoD", pct=5.0),   # 2200 * 1.05 = 2310
    ]
    steps = _recalculate_chain(steps, line_items)

    assert steps[0]["total_amount"] == 20000  # 1000 * 2000/100
    assert steps[1]["total_amount"] == 22000  # 1000 * 2200/100
    assert steps[2]["total_amount"] == 23100  # 1000 * 2310/100


def test_zero_weight():
    """Zero weight should not cause division errors."""
    line_items = _make_line_items([(0, 2500, 0)])
    steps = [_make_step()]
    steps = _recalculate_chain(steps, line_items)

    assert steps[0]["total_amount"] == 0
    assert steps[0]["weighted_avg_rate"] == 0.0


def test_empty_company_clears_values():
    """Step with no company selected should have zero values."""
    line_items = _make_line_items([(1000, 2500, 50)])
    steps = [_make_step("CoB"), {"company": "", "pct_rate_increase": 0}]
    steps = _recalculate_chain(steps, line_items)

    assert steps[1]["total_amount"] == 0
    assert steps[1]["claim_amount"] == 0
    assert steps[1]["net_amount"] == 0
```

- [ ] **Step 4: Run tests to verify they fail (function not yet rewritten)**

Run: `cd c:/code/juteTransfer && uv run pytest tests/test_recalculation.py -v`
Expected: FAIL (signature mismatch or import error — the current `_recalculate_chain` has a different signature)

- [ ] **Step 5: Commit test file**

```bash
git add tests/test_recalculation.py
git commit -m "test: add recalculation unit tests with rounding rules"
```

---

### Task 3: Chain reconstruction unit tests

**Files:**
- Create: `tests/test_chain_reconstruction.py`

- [ ] **Step 1: Write chain reconstruction tests**

```python
"""Tests for chain reconstruction algorithm."""
from src.jutetransfer.pages.jute_mr import _reconstruct_chain


def _make_chain_mrs(entries):
    """Helper: create list of MR dicts from (jute_mr_id, src_com_id, owner_co_id, branch_id) tuples."""
    return [
        {"jute_mr_id": mid, "src_com_id": src, "owner_co_id": owner, "branch_id": bid}
        for mid, src, owner, bid in entries
    ]


def test_simple_chain_a_b_a():
    """A→B→A: one transferred MR."""
    mrs = _make_chain_mrs([
        (222, 1, 2, 20),  # MR at B, received from A (co_id=1)
    ])
    root_co_id = 1  # Company A
    ordered = _reconstruct_chain(mrs, root_co_id)
    assert [m["jute_mr_id"] for m in ordered] == [222]


def test_chain_a_b_c_a():
    """A→B→C→A: two transferred MRs."""
    mrs = _make_chain_mrs([
        (222, 1, 2, 20),  # MR at B, received from A
        (333, 2, 3, 30),  # MR at C, received from B
    ])
    ordered = _reconstruct_chain(mrs, root_co_id=1)
    assert [m["jute_mr_id"] for m in ordered] == [222, 333]


def test_chain_a_b_c_b_a():
    """A→B→C→B→A: repeated company, disambiguated by jute_mr_id."""
    mrs = _make_chain_mrs([
        (222, 1, 2, 20),  # B, from A
        (333, 2, 3, 30),  # C, from B
        (444, 3, 2, 21),  # B again, from C (different branch)
    ])
    ordered = _reconstruct_chain(mrs, root_co_id=1)
    assert [m["jute_mr_id"] for m in ordered] == [222, 333, 444]


def test_chain_with_ambiguous_sender():
    """A→B→C→B→D→A: B sends twice (to C and to D)."""
    mrs = _make_chain_mrs([
        (222, 1, 2, 20),  # B, from A
        (333, 2, 3, 30),  # C, from B (first send)
        (444, 3, 2, 21),  # B again, from C
        (555, 2, 4, 40),  # D, from B (second send)
    ])
    ordered = _reconstruct_chain(mrs, root_co_id=1)
    assert [m["jute_mr_id"] for m in ordered] == [222, 333, 444, 555]


def test_empty_chain():
    """No transferred MRs."""
    ordered = _reconstruct_chain([], root_co_id=1)
    assert ordered == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd c:/code/juteTransfer && uv run pytest tests/test_chain_reconstruction.py -v`
Expected: FAIL (function doesn't exist yet)

- [ ] **Step 3: Commit**

```bash
git add tests/test_chain_reconstruction.py
git commit -m "test: add chain reconstruction unit tests"
```

---

### Task 4: Grouping unit tests

**Files:**
- Create: `tests/test_grouping.py`

- [ ] **Step 1: Write grouping tests**

```python
"""Tests for MR grouping logic."""
import pandas as pd
from src.jutetransfer.pages.jute_mr import _group_by_mr


def test_single_mr_single_li():
    """One MR with one line item — passthrough."""
    df = pd.DataFrame([{
        "jute_mr_id": 111,
        "Jute Gate Entry No": 1,
        "MR DATE": "2026-03-01",
        "Party Name": "Supplier A",
        "Item Quality": "TD5",
        "Weight (KG)": 1000,
        "MR Rate": 2500,
        "Total Amount": 25000.0,
        "Claim Amount": 50.0,
        "Net Total": 24950.0,
    }])
    grouped, line_items_map = _group_by_mr(df)
    assert len(grouped) == 1
    assert grouped.iloc[0]["jute_mr_id"] == 111
    assert grouped.iloc[0]["Weight (KG)"] == 1000
    assert len(line_items_map[111]) == 1


def test_single_mr_multiple_li():
    """One MR with two line items — aggregated."""
    df = pd.DataFrame([
        {
            "jute_mr_id": 111, "Jute Gate Entry No": 1, "MR DATE": "2026-03-01",
            "Party Name": "Supplier A", "Item Quality": "TD5",
            "Weight (KG)": 500, "MR Rate": 2450,
            "Total Amount": 12250.0, "Claim Amount": 30.0, "Net Total": 12220.0,
        },
        {
            "jute_mr_id": 111, "Jute Gate Entry No": 1, "MR DATE": "2026-03-01",
            "Party Name": "Supplier A", "Item Quality": "TD4",
            "Weight (KG)": 750, "MR Rate": 2800,
            "Total Amount": 21000.0, "Claim Amount": 45.0, "Net Total": 20955.0,
        },
    ])
    grouped, line_items_map = _group_by_mr(df)
    assert len(grouped) == 1
    row = grouped.iloc[0]
    assert row["Weight (KG)"] == 1250       # 500 + 750
    assert row["Total Amount"] == 33250.0   # 12250 + 21000
    assert row["Claim Amount"] == 75.0      # 30 + 45
    assert "TD5 / TD4" in row["Item Quality"] or "TD4 / TD5" in row["Item Quality"]
    assert len(line_items_map[111]) == 2


def test_multiple_mrs():
    """Two different MRs — two grouped rows."""
    df = pd.DataFrame([
        {"jute_mr_id": 111, "Jute Gate Entry No": 1, "MR DATE": "2026-03-01",
         "Party Name": "A", "Item Quality": "TD5",
         "Weight (KG)": 1000, "MR Rate": 2500,
         "Total Amount": 25000.0, "Claim Amount": 50.0, "Net Total": 24950.0},
        {"jute_mr_id": 222, "Jute Gate Entry No": 2, "MR DATE": "2026-03-02",
         "Party Name": "B", "Item Quality": "TD4",
         "Weight (KG)": 800, "MR Rate": 2600,
         "Total Amount": 20800.0, "Claim Amount": 40.0, "Net Total": 20760.0},
    ])
    grouped, line_items_map = _group_by_mr(df)
    assert len(grouped) == 2
    assert set(grouped["jute_mr_id"]) == {111, 222}


def test_weighted_avg_rate():
    """Weighted average rate = Total Amount / Weight * 100."""
    df = pd.DataFrame([
        {"jute_mr_id": 111, "Jute Gate Entry No": 1, "MR DATE": "2026-03-01",
         "Party Name": "A", "Item Quality": "TD5",
         "Weight (KG)": 500, "MR Rate": 2000,
         "Total Amount": 10000.0, "Claim Amount": 0.0, "Net Total": 10000.0},
        {"jute_mr_id": 111, "Jute Gate Entry No": 1, "MR DATE": "2026-03-01",
         "Party Name": "A", "Item Quality": "TD4",
         "Weight (KG)": 500, "MR Rate": 3000,
         "Total Amount": 15000.0, "Claim Amount": 0.0, "Net Total": 15000.0},
    ])
    grouped, _ = _group_by_mr(df)
    row = grouped.iloc[0]
    # Total = 25000, Weight = 1000 → avg rate = 25000/1000*100 = 2500
    assert row["MR Rate"] == 2500.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd c:/code/juteTransfer && uv run pytest tests/test_grouping.py -v`
Expected: FAIL (function doesn't exist yet)

- [ ] **Step 3: Commit**

```bash
git add tests/test_grouping.py
git commit -m "test: add MR grouping unit tests"
```

---

## Chunk 2: Query Updates + Transfer.py Backend

### Task 5: Update queries.py — ownership via branch_id

**Files:**
- Modify: `src/jutetransfer/queries.py`

Reference: Spec Part 3 "Query updates" section.

- [ ] **Step 1: Update `get_next_mr_number` — remove `src_com_id` filter**

In `get_next_mr_number` (line ~92), change:
```python
# Before:
WHERE src_com_id = :co_id
AND branch_id = :branch_id
```
To:
```python
# After:
WHERE branch_id = :branch_id
```
Remove `"co_id": co_id` from params dict. The function signature keeps `co_id` param for now (callers still pass it) but it's unused in the query.

- [ ] **Step 2: Update `get_next_mr_numbers_batch` — same change**

Same pattern: remove `src_com_id = :co_id` from the WHERE clause (line ~129). Remove `"co_id"` from params.

- [ ] **Step 3: Update `get_jute_mr_with_line_items` — join via branch_mst**

Add `branch_mst` join and update party join + company filter:
```python
query = """
    SELECT
        mr.jute_mr_id AS `jute_mr_id`,
        mr.jute_gate_entry_no AS `Jute Gate Entry No`,
        -- ... (all existing SELECT columns stay the same)
    FROM jute_mr mr
    INNER JOIN branch_mst bm ON mr.branch_id = bm.branch_id
    INNER JOIN jute_mr_li li ON mr.jute_mr_id = li.jute_mr_id
    LEFT JOIN jute_po p ON mr.po_id = p.jute_po_id
    LEFT JOIN jute_supplier_mst s ON mr.jute_supplier_id = s.supplier_id
    LEFT JOIN party_mst pm ON pm.party_id = mr.party_id AND pm.co_id = bm.co_id
    LEFT JOIN party_branch_mst pb ON pb.party_id = pm.party_id AND pb.party_mst_branch_id = mr.party_branch_id
    LEFT JOIN jute_quality_mst q ON li.actual_quality = q.jute_qlty_id
    WHERE YEAR(mr.jute_gate_entry_date) = :year
    AND MONTH(mr.jute_gate_entry_date) = :month
"""

# Company filter:
if company_id:
    query += " AND bm.co_id = :co_id"
    params["co_id"] = company_id
```

- [ ] **Step 4: Add `get_transfer_chain` query**

Add new function at end of `queries.py`:
```python
def get_transfer_chain(root_mr_id: int) -> pd.DataFrame:
    """Fetch all transferred MRs for a given root MR, with company info.

    Returns DataFrame with columns: jute_mr_id, src_com_id, branch_id,
    jute_mr_date, branch_mr_no, total_amount, claim_amount, net_total,
    owner_co_id, branch_name, co_name, co_prefix.
    Ordered by jute_mr_id ASC for chain reconstruction.
    """
    return DatabaseConnection.execute_query(
        """
        SELECT mr.jute_mr_id, mr.src_com_id, mr.branch_id, mr.jute_mr_date,
               mr.branch_mr_no, mr.total_amount, mr.claim_amount, mr.net_total,
               bm.co_id AS owner_co_id, bm.branch_name,
               cm.co_name, cm.co_prefix
        FROM jute_mr mr
        JOIN branch_mst bm ON mr.branch_id = bm.branch_id
        JOIN co_mst cm ON bm.co_id = cm.co_id
        WHERE mr.src_jute_mr_id = :root_id
        ORDER BY mr.jute_mr_id ASC
        """,
        {"root_id": root_mr_id},
    )
```

- [ ] **Step 5: Add `get_warehouses_by_branch` query**

```python
def get_warehouses_by_branch(branch_id: int) -> dict:
    """Fetch warehouses for a branch.

    Returns:
        dict: {warehouse_name: warehouse_id}
    """
    df = DatabaseConnection.execute_query(
        "SELECT warehouse_id, warehouse_name FROM warehouse_mst WHERE branch_id = :bid AND active = 1 ORDER BY warehouse_name",
        {"bid": branch_id},
    )
    if df is not None and not df.empty:
        return {row["warehouse_name"]: row["warehouse_id"] for _, row in df.iterrows()}
    return {}
```

- [ ] **Step 6: Commit**

```bash
git add src/jutetransfer/queries.py
git commit -m "feat: update queries to derive ownership from branch_id, add chain and warehouse queries"
```

---

### Task 6: Update transfer.py — TransferStep + field fixes

**Files:**
- Modify: `src/jutetransfer/transfer.py`

- [ ] **Step 1: Update TransferStep dataclass**

```python
@dataclass
class TransferStep:
    """A single step in the transfer chain."""
    co_id: int
    branch_id: int
    mr_date: date
    mr_rate: float              # weighted avg rate (display/header)
    total_amount: float         # aggregate, rounded to 0
    claim_amount: float         # aggregate, rounded to 0 (unaffected by %)
    net_amount: float           # total_amount - claim_amount
    mr_no: int
    pct_rate_increase: float = 0.0
    roundoff: float = 0.0
    warehouse_id: Optional[int] = None
    gate_entry_no: Optional[int] = None
```

- [ ] **Step 2: Add `_get_next_gate_entry_no` helper**

Add after the existing helpers:
```python
def _get_next_gate_entry_no(conn, branch_id: int) -> int:
    """Get the next gate entry number for a branch in the current FY."""
    fy_start, fy_end = _get_financial_year_bounds()
    result = conn.execute(
        text("""SELECT COALESCE(MAX(jute_gate_entry_no), 0) AS max_no
                FROM jute_mr
                WHERE branch_id = :bid
                AND jute_gate_entry_date BETWEEN :fy_start AND :fy_end"""),
        {"bid": branch_id, "fy_start": fy_start.strftime("%Y-%m-%d"),
         "fy_end": fy_end.strftime("%Y-%m-%d")},
    )
    return int(result.scalar() or 0) + 1


def _get_next_mr_number_in_txn(conn, branch_id: int) -> int:
    """Get next branch_mr_no inside an existing transaction."""
    fy_start, fy_end = _get_financial_year_bounds()
    result = conn.execute(
        text("""SELECT COALESCE(MAX(branch_mr_no), 0) AS max_no
                FROM jute_mr
                WHERE branch_id = :bid
                AND jute_mr_date BETWEEN :fy_start AND :fy_end"""),
        {"bid": branch_id, "fy_start": fy_start.strftime("%Y-%m-%d"),
         "fy_end": fy_end.strftime("%Y-%m-%d")},
    )
    return int(result.scalar() or 0) + 1
```

- [ ] **Step 3: Update `_create_mr` — field fixes + rate_multiplier**

Update the function signature:
```python
def _create_mr(conn, source_mr: dict, step: TransferStep,
               party_id: int, party_branch_id: Optional[int],
               updated_by: int, rate_multiplier: float,
               prev_co_id: int, root_mr_id: int) -> int:
```

In the INSERT params, change:
```python
"status_id": 3,  # Approved (was 0)
"po_id": None,   # PO is company-specific (was source_mr.get("po_id"))
"gate_entry_no": _get_next_gate_entry_no(conn, step.branch_id),
"gate_entry_date": step.mr_date,  # was source_mr.get("jute_gate_entry_date")
"src_com_id": prev_co_id,  # received-from company (was step.co_id)
"src_jute_mr_id": root_mr_id,  # always root (was source_mr.get("jute_mr_id"))
"roundoff": step.roundoff,
```

In the line item loop, change:
```python
for li in source_mr.get("line_items", []):
    accepted_weight = round(float(li.get("accepted_weight") or 0), 0)
    original_rate = float(li.get("rate") or 0)
    new_rate = original_rate * rate_multiplier
    total_price = round(accepted_weight * new_rate / 100, 2)

    conn.execute(text("""..."""), {
        # ... existing fields ...
        "jute_mr_id": new_mr_id,
        "jute_po_li_id": None,      # was li.get("jute_po_li_id")
        "rate": new_rate,            # was step.mr_rate
        "total_price": total_price,  # was accepted_weight * step.mr_rate / 100
        "warehouse_id": step.warehouse_id,  # was li.get("warehouse_id")
        "actual_rate": None,         # was li.get("actual_rate")
    })
```

- [ ] **Step 4: Update `_create_sales_invoice` — status_id + rate_multiplier**

Update signature:
```python
def _create_sales_invoice(conn, seller_step: TransferStep,
                           buyer_party_id: int, buyer_party_branch_id: Optional[int],
                           mr_id: int, source_mr: dict,
                           updated_by: int, rate_multiplier: float) -> int:
```

In the INSERT SQL, change `status_id` from `0` to `3`.

In the line item loop:
```python
for li in source_mr.get("line_items", []):
    accepted_weight = round(float(li.get("accepted_weight") or 0), 0)
    original_rate = float(li.get("rate") or 0)
    new_rate = original_rate * rate_multiplier
    amount = round(accepted_weight * new_rate / 100, 2)
```

- [ ] **Step 5: Update `_ensure_supplier_party` — derive co_id from branch_id**

Change:
```python
source_co_id = int(source_mr.get("src_com_id") or 0)
```
To:
```python
# Derive owner co_id from branch_id
source_branch_id = int(source_mr.get("branch_id") or 0)
result = conn.execute(
    text("SELECT co_id FROM branch_mst WHERE branch_id = :bid"),
    {"bid": source_branch_id},
)
row = result.fetchone()
source_co_id = row[0] if row else 0
```

- [ ] **Step 6: Commit**

```bash
git add src/jutetransfer/transfer.py
git commit -m "feat: update _create_mr/_create_sales_invoice with field fixes and rate_multiplier"
```

---

### Task 7: Update transfer.py — _update_original_mr + revert

**Files:**
- Modify: `src/jutetransfer/transfer.py`

- [ ] **Step 1: Rewrite `_update_original_mr` for per-item rates + branch_mr_no**

```python
def _update_original_mr(conn, jute_mr_id: int, rate_multiplier: float,
                         final_party_id: int, final_party_branch_id: Optional[int],
                         source_mr: dict, branch_id: int,
                         mr_date: date, updated_by: int) -> None:
    """Update the original MR with final rate/party and assign branch_mr_no."""
    # Assign branch_mr_no
    new_mr_no = _get_next_mr_number_in_txn(conn, branch_id)

    # Update each line item with its computed absolute rate
    for li in source_mr.get("line_items", []):
        li_id = li["jute_mr_li_id"]
        original_rate = float(li.get("rate") or 0)
        new_rate = original_rate * rate_multiplier
        accepted_weight = round(float(li.get("accepted_weight") or 0), 0)
        new_total_price = round(accepted_weight * new_rate / 100, 2)

        conn.execute(text("""
            UPDATE jute_mr_li SET
                rate = :rate,
                total_price = :total_price,
                updated_date_time = NOW()
            WHERE jute_mr_li_id = :li_id
        """), {"rate": new_rate, "total_price": new_total_price, "li_id": li_id})

    # Recompute header totals from line items
    conn.execute(text("""
        UPDATE jute_mr SET
            party_id = :party_id,
            party_branch_id = :party_branch_id,
            branch_mr_no = :mr_no,
            jute_mr_date = :mr_date,
            total_amount = (SELECT ROUND(COALESCE(SUM(total_price), 0), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id),
            roundoff = (SELECT COALESCE(SUM(total_price), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id) -
                       (SELECT ROUND(COALESCE(SUM(total_price), 0), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id),
            net_total = (SELECT ROUND(COALESCE(SUM(total_price), 0), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id) - claim_amount,
            updated_by = :updated_by,
            updated_date_time = NOW()
        WHERE jute_mr_id = :mr_id
    """), {
        "party_id": str(final_party_id),
        "party_branch_id": final_party_branch_id,
        "mr_no": new_mr_no,
        "mr_date": mr_date,
        "updated_by": updated_by,
        "mr_id": jute_mr_id,
    })
```

- [ ] **Step 2: Add `revert_original_mr` function**

```python
def revert_original_mr(conn, jute_mr_id: int, source_mr: dict, updated_by: int) -> None:
    """Revert the original MR to its pre-finalization state.

    Restores original party, rates, branch_mr_no=NULL, and line item rates.
    Called when deleting a completed chain's final step.
    """
    # Restore each line item to original rate
    for li in source_mr.get("line_items", []):
        li_id = li["jute_mr_li_id"]
        original_rate = float(li.get("rate") or 0)
        accepted_weight = round(float(li.get("accepted_weight") or 0), 0)
        original_total = round(accepted_weight * original_rate / 100, 2)

        conn.execute(text("""
            UPDATE jute_mr_li SET
                rate = :rate, total_price = :total_price, updated_date_time = NOW()
            WHERE jute_mr_li_id = :li_id
        """), {"rate": original_rate, "total_price": original_total, "li_id": li_id})

    # Restore header: original party, NULL branch_mr_no, recompute totals
    conn.execute(text("""
        UPDATE jute_mr SET
            party_id = :party_id,
            party_branch_id = :party_branch_id,
            branch_mr_no = NULL,
            total_amount = (SELECT ROUND(COALESCE(SUM(total_price), 0), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id),
            roundoff = (SELECT COALESCE(SUM(total_price), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id) -
                       (SELECT ROUND(COALESCE(SUM(total_price), 0), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id),
            net_total = (SELECT ROUND(COALESCE(SUM(total_price), 0), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id) - claim_amount,
            updated_by = :updated_by,
            updated_date_time = NOW()
        WHERE jute_mr_id = :mr_id
    """), {
        "party_id": str(source_mr.get("party_id", "")),
        "party_branch_id": source_mr.get("party_branch_id"),
        "updated_by": updated_by,
        "mr_id": jute_mr_id,
    })
```

- [ ] **Step 3: Commit**

```bash
git add src/jutetransfer/transfer.py
git commit -m "feat: rewrite _update_original_mr with per-item rates, add revert_original_mr"
```

---

### Task 8: Add save_transfer_step and delete_transfer_step

**Files:**
- Modify: `src/jutetransfer/transfer.py`

- [ ] **Step 1: Add `save_transfer_step` function**

Replace the monolithic `finalize_transfer_chain` orchestration with a per-step function. Add before `finalize_transfer_chain`:

```python
def save_transfer_step(
    source_mr_id: int,
    step: TransferStep,
    prev_co_id: int,
    prev_branch_id: int,
    source_co_id: int,
    source_branch_id: int,
    root_mr_id: int,
    updated_by: int,
    rate_multiplier: float,
    is_first_step: bool = False,
    is_final: bool = False,
) -> dict:
    """Save a single transfer step: create MR + invoice.

    Args:
        source_mr_id: Root MR ID (for fetching source data)
        step: The transfer step being saved
        prev_co_id: Company from which this step receives
        prev_branch_id: Branch of the previous step (for invoice)
        source_co_id: Original source company co_id
        source_branch_id: Original source branch_id
        root_mr_id: Root MR ID (for src_jute_mr_id)
        updated_by: User ID
        rate_multiplier: Cumulative rate multiplier for this step
        is_first_step: True if this is step[0] (supplier party, no invoice from prev)
        is_final: True if chain returns to source

    Returns:
        dict with keys: mr_id (int or None), invoice_id (int or None)
    """
    mr_id = None
    invoice_id = None

    with DatabaseConnection.get_transaction() as conn:
        source_mr = get_source_mr_full(source_mr_id, conn=conn)
        if not source_mr:
            raise ValueError(f"Source MR {source_mr_id} not found")

        # Assign MR number inside transaction
        step.mr_no = _get_next_mr_number_in_txn(conn, step.branch_id)
        step.gate_entry_no = _get_next_gate_entry_no(conn, step.branch_id)

        if is_first_step:
            # Step[0]: first receiver gets MR from original supplier
            party_id, party_branch_id = _ensure_supplier_party(
                conn, source_mr, step.co_id, updated_by
            )
            mr_id = _create_mr(
                conn, source_mr, step, party_id, party_branch_id,
                updated_by, rate_multiplier, prev_co_id, root_mr_id
            )
        else:
            # Intermediate or final: create invoice from seller, then MR for buyer
            # 1. Ensure buyer exists as party in seller's company
            buyer_party_id, buyer_party_branch_id = _ensure_company_as_party(
                conn, step.co_id, step.branch_id, prev_co_id, updated_by
            )
            # 2. Create sales invoice from seller
            prev_step_for_invoice = TransferStep(
                co_id=prev_co_id, branch_id=prev_branch_id,
                mr_date=step.mr_date, mr_rate=0, total_amount=step.total_amount,
                claim_amount=step.claim_amount, net_amount=step.net_amount,
                mr_no=0, roundoff=step.roundoff,
            )
            # Find the previous MR ID for invoice linkage
            prev_mr_result = conn.execute(
                text("""SELECT jute_mr_id FROM jute_mr
                        WHERE src_jute_mr_id = :root AND branch_id = :bid
                        ORDER BY jute_mr_id DESC LIMIT 1"""),
                {"root": root_mr_id, "bid": prev_branch_id},
            )
            prev_mr_row = prev_mr_result.fetchone()
            prev_mr_id = prev_mr_row[0] if prev_mr_row else source_mr_id

            invoice_id = _create_sales_invoice(
                conn, prev_step_for_invoice, buyer_party_id,
                buyer_party_branch_id, prev_mr_id, source_mr,
                updated_by, rate_multiplier
            )

            if is_final:
                # Final step: update original MR, don't create new MR
                last_seller_party_id, last_seller_party_branch_id = _ensure_company_as_party(
                    conn, prev_co_id, prev_branch_id, source_co_id, updated_by
                )
                _update_original_mr(
                    conn, source_mr_id, rate_multiplier,
                    last_seller_party_id, last_seller_party_branch_id,
                    source_mr, source_branch_id, step.mr_date, updated_by
                )
            else:
                # Create MR for buyer
                seller_party_id, seller_party_branch_id = _ensure_company_as_party(
                    conn, prev_co_id, prev_branch_id, step.co_id, updated_by
                )
                jute_supplier_id = int(source_mr.get("jute_supplier_id") or 0)
                _ensure_supplier_party_map(
                    conn, jute_supplier_id, step.co_id, seller_party_id, updated_by
                )
                mr_id = _create_mr(
                    conn, source_mr, step, seller_party_id, seller_party_branch_id,
                    updated_by, rate_multiplier, prev_co_id, root_mr_id
                )

    logger.info(f"Transfer step saved for MR {source_mr_id}: mr_id={mr_id}, invoice_id={invoice_id}")
    return {"mr_id": mr_id, "invoice_id": invoice_id}
```

- [ ] **Step 2: Add `delete_transfer_step` function**

```python
def delete_transfer_step(jute_mr_id: int, updated_by: int) -> None:
    """Delete a transfer MR and its associated invoice.

    Finds the invoice via sales_invoice_jute.mr_id linkage,
    then deletes invoice records and the MR + line items.
    """
    with DatabaseConnection.get_transaction() as conn:
        # Find linked invoice(s) via sales_invoice_jute
        inv_rows = conn.execute(
            text("SELECT invoice_id FROM sales_invoice_jute WHERE mr_id = :mr_id"),
            {"mr_id": jute_mr_id},
        ).fetchall()

        for inv_row in inv_rows:
            inv_id = inv_row[0]
            conn.execute(text("DELETE FROM sales_invoice_jute WHERE invoice_id = :id"), {"id": inv_id})
            conn.execute(text("DELETE FROM sales_invoice_dtl WHERE invoice_id = :id"), {"id": inv_id})
            conn.execute(text("DELETE FROM sales_invoice WHERE invoice_id = :id"), {"id": inv_id})

        # Delete MR line items and MR
        conn.execute(text("DELETE FROM jute_mr_li WHERE jute_mr_id = :id"), {"id": jute_mr_id})
        conn.execute(text("DELETE FROM jute_mr WHERE jute_mr_id = :id"), {"id": jute_mr_id})

    logger.info(f"Deleted transfer MR {jute_mr_id} and linked invoices")


def delete_chain_from_step(root_mr_id: int, from_mr_id: int,
                            source_mr: dict, updated_by: int) -> None:
    """Delete all chain steps from a given MR onward (cascade).

    Uses chain reconstruction to find the order, then deletes in reverse.
    If the chain was complete (original MR finalized), reverts the original.
    """
    from .queries import get_transfer_chain
    chain_df = get_transfer_chain(root_mr_id)
    if chain_df is None or chain_df.empty:
        return

    # Reconstruct ordered chain
    from .pages.jute_mr import _reconstruct_chain
    chain_mrs = chain_df.to_dict("records")
    # Derive root co_id
    root_mr = get_source_mr_full(root_mr_id)
    if not root_mr:
        return

    with DatabaseConnection.get_transaction() as conn:
        root_branch = int(root_mr.get("branch_id") or 0)
        root_co_row = conn.execute(
            text("SELECT co_id FROM branch_mst WHERE branch_id = :bid"),
            {"bid": root_branch},
        ).fetchone()
        root_co_id = root_co_row[0] if root_co_row else 0

    ordered = _reconstruct_chain(chain_mrs, root_co_id)

    # Find index of from_mr_id
    from_idx = next((i for i, m in enumerate(ordered) if m["jute_mr_id"] == from_mr_id), None)
    if from_idx is None:
        return

    # Check if chain was complete (original MR has branch_mr_no)
    was_complete = root_mr.get("branch_mr_no") is not None

    # Delete in reverse order from the end back to from_idx
    to_delete = ordered[from_idx:]
    for mr in reversed(to_delete):
        delete_transfer_step(mr["jute_mr_id"], updated_by)

    # Revert original MR if chain was complete
    if was_complete:
        with DatabaseConnection.get_transaction() as conn:
            source_mr_full = get_source_mr_full(root_mr_id, conn=conn)
            revert_original_mr(conn, root_mr_id, source_mr, updated_by)
```

- [ ] **Step 3: Update `finalize_transfer_chain` to use `save_transfer_step`**

Rewrite the orchestrator as a convenience wrapper:
```python
def finalize_transfer_chain(
    source_mr_id: int,
    steps: list[TransferStep],
    source_co_id: int,
    source_branch_id: int,
    updated_by: int,
) -> dict:
    """Execute full transfer chain in sequence using save_transfer_step.

    Convenience wrapper for when the full chain is known upfront.
    """
    if len(steps) < 2:
        raise ValueError("Transfer chain must have at least 2 steps")

    mr_ids = []
    invoice_ids = []
    cumulative_multiplier = 1.0

    for i, step in enumerate(steps):
        if i > 0:
            cumulative_multiplier *= (1 + step.pct_rate_increase / 100)

        prev_co_id = source_co_id if i == 0 else steps[i - 1].co_id
        prev_branch_id = source_branch_id if i == 0 else steps[i - 1].branch_id
        is_final = (i == len(steps) - 1)

        result = save_transfer_step(
            source_mr_id=source_mr_id,
            step=step,
            prev_co_id=prev_co_id,
            prev_branch_id=prev_branch_id,
            source_co_id=source_co_id,
            source_branch_id=source_branch_id,
            root_mr_id=source_mr_id,
            updated_by=updated_by,
            rate_multiplier=cumulative_multiplier,
            is_first_step=(i == 0),
            is_final=is_final,
        )

        if result.get("mr_id"):
            mr_ids.append(result["mr_id"])
        if result.get("invoice_id"):
            invoice_ids.append(result["invoice_id"])

    logger.info(
        f"Transfer chain finalized for MR {source_mr_id}: "
        f"created {len(mr_ids)} MRs, {len(invoice_ids)} invoices"
    )
    return {"mr_ids": mr_ids, "invoice_ids": invoice_ids}
```

- [ ] **Step 4: Commit**

```bash
git add src/jutetransfer/transfer.py
git commit -m "feat: add save_transfer_step, delete_transfer_step, rewrite finalize_transfer_chain"
```

---

## Chunk 3: UI — Grouping, Recalculation, Chain Hydration, Partial Save

### Task 9: Implement grouping + recalculation + chain reconstruction in jute_mr.py

**Files:**
- Modify: `src/jutetransfer/pages/jute_mr.py`

- [ ] **Step 1: Add imports and update existing imports**

At top of file, add:
```python
from ..queries import (
    get_companies,
    get_branches_by_company,
    get_company_branch_options,
    get_jute_mr_with_line_items,
    get_next_mr_numbers_batch,
    get_transfer_chain,
    get_warehouses_by_branch,
)
from ..transfer import (
    finalize_transfer_chain, save_transfer_step, delete_chain_from_step,
    TransferStep,
)
```

- [ ] **Step 2: Implement `_group_by_mr` function**

Replace the existing `_init_transfer_data` with a grouping function. Add after the constants:

```python
def _group_by_mr(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Group raw per-line-item dataframe into one row per jute_mr_id.

    Returns:
        (grouped_df, line_items_map) where line_items_map is
        {jute_mr_id: [{weight, original_rate, original_claim}, ...]}
    """
    if df is None or df.empty:
        return df, {}

    line_items_map = {}
    grouped_rows = []

    for mr_id, group in df.groupby("jute_mr_id"):
        # Extract per-line-item details
        items = []
        for _, row in group.iterrows():
            items.append({
                "weight": round(float(row.get("Weight (KG)") or 0), 0),
                "original_rate": float(row.get("MR Rate") or 0),
                "original_claim": float(row.get("Claim Amount") or 0),
            })
        line_items_map[int(mr_id)] = items

        # Aggregated row
        first = group.iloc[0].to_dict()
        total_weight = sum(li["weight"] for li in items)
        total_amount = sum(float(r.get("Total Amount") or 0) for _, r in group.iterrows())
        claim_amount = sum(float(r.get("Claim Amount") or 0) for _, r in group.iterrows())

        first["Weight (KG)"] = total_weight
        first["Total Amount"] = total_amount
        first["Claim Amount"] = claim_amount
        first["Net Total"] = total_amount - claim_amount
        first["MR Rate"] = (total_amount / total_weight * 100) if total_weight > 0 else 0.0

        # Combine qualities
        qualities = group["Item Quality"].dropna().unique()
        if len(qualities) > 1:
            first["Item Quality"] = " / ".join(str(q) for q in qualities)

        grouped_rows.append(first)

    grouped_df = pd.DataFrame(grouped_rows)
    return grouped_df, line_items_map
```

- [ ] **Step 3: Implement `_reconstruct_chain` function**

```python
def _reconstruct_chain(chain_mrs: list[dict], root_co_id: int) -> list[dict]:
    """Reconstruct transfer chain order from MR records.

    Args:
        chain_mrs: List of MR dicts with jute_mr_id, src_com_id, owner_co_id, branch_id.
                   Must be sorted by jute_mr_id ASC.
        root_co_id: The root/source company's co_id.

    Returns:
        Ordered list of MR dicts representing the chain.
    """
    if not chain_mrs:
        return []

    # Ensure sorted by jute_mr_id for tie-breaking
    sorted_mrs = sorted(chain_mrs, key=lambda m: m["jute_mr_id"])

    visited = set()
    ordered = []
    current_co = root_co_id

    while True:
        # Find unvisited MR where src_com_id == current_co, lowest jute_mr_id first
        candidate = None
        for mr in sorted_mrs:
            if mr["jute_mr_id"] not in visited and mr.get("src_com_id") == current_co:
                candidate = mr
                break

        if candidate is None:
            break

        visited.add(candidate["jute_mr_id"])
        ordered.append(candidate)
        # Derive next current_co from this MR's owner
        current_co = candidate.get("owner_co_id") or candidate.get("co_id")

    return ordered
```

- [ ] **Step 4: Rewrite `_recalculate_chain` for per-line-item rates**

```python
def _recalculate_chain(steps: list[dict], line_items: list[dict]) -> list[dict]:
    """Recalculate all derived values in a transfer chain.

    Args:
        steps: List of transfer step dicts (mutated in place and returned).
        line_items: List of {weight, original_rate, original_claim} per line item.

    Returns:
        The steps list with computed aggregates filled in.
    """
    weights = [round(float(li.get("weight") or 0), 0) for li in line_items]
    original_rates = [float(li.get("original_rate") or 0) for li in line_items]
    claims = [float(li.get("original_claim") or 0) for li in line_items]
    total_weight = sum(weights)

    prev_rates = list(original_rates)

    for i, step in enumerate(steps):
        if not step.get("company"):
            step["total_amount"] = 0
            step["claim_amount"] = 0
            step["net_amount"] = 0
            step["roundoff"] = 0.0
            step["weighted_avg_rate"] = 0.0
            continue

        if i == 0:
            rates_i = list(prev_rates)
        else:
            pct = float(step.get("pct_rate_increase", 0) or 0)
            rates_i = [r * (1 + pct / 100) for r in prev_rates]

        # Line item level: round to 2
        li_totals = [round(weights[j] * rates_i[j] / 100, 2) for j in range(len(line_items))]
        li_claims = [round(claims[j], 2) for j in range(len(line_items))]

        # Header level: round to 0
        raw_total = sum(li_totals)
        step["total_amount"] = round(raw_total, 0)
        step["roundoff"] = round(raw_total - step["total_amount"], 2)
        step["claim_amount"] = round(sum(li_claims), 0)
        step["net_amount"] = step["total_amount"] - step["claim_amount"]
        step["weighted_avg_rate"] = (step["total_amount"] / total_weight * 100) if total_weight > 0 else 0.0

        prev_rates = rates_i

    return steps
```

- [ ] **Step 5: Run the recalculation and grouping tests**

Run: `cd c:/code/juteTransfer && uv run pytest tests/test_recalculation.py tests/test_grouping.py tests/test_chain_reconstruction.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add src/jutetransfer/pages/jute_mr.py tests/
git commit -m "feat: implement _group_by_mr, _reconstruct_chain, rewrite _recalculate_chain"
```

---

### Task 10: Update jute_mr_table_page — grouping + chain hydration

**Files:**
- Modify: `src/jutetransfer/pages/jute_mr.py`

- [ ] **Step 1: Update `jute_mr_table_page` to use grouped data**

After the `df = get_jute_mr_with_line_items(...)` call, replace the transfer init block with:

```python
if df is None or df.empty:
    # ... existing empty warning ...
    return

# Group by MR header
grouped_df, line_items_map = _group_by_mr(df)

# Session keys
transfers_key = f"transfers_{filter_key}"
source_df_key = f"source_df_{filter_key}"
line_items_key = f"line_items_{filter_key}"

if transfers_key not in st.session_state:
    # Initialize transfer state keyed by jute_mr_id
    transfers = {}
    for _, row in grouped_df.iterrows():
        mr_id = int(row["jute_mr_id"])
        step0 = _empty_transfer_step()
        step0["mr_date"] = row["MR DATE"]
        transfers[mr_id] = [step0]
    st.session_state[transfers_key] = transfers
    st.session_state[source_df_key] = grouped_df.copy()
    st.session_state[line_items_key] = line_items_map

source_df = st.session_state[source_df_key]
transfers = st.session_state[transfers_key]
line_items_map = st.session_state[line_items_key]
```

- [ ] **Step 2: Update chain summary to check DB for existing chains**

```python
chain_summaries = []
chain_statuses = []
for _, row in source_df.iterrows():
    mr_id = int(row["jute_mr_id"])
    # Check DB for existing chain
    chain_df = get_transfer_chain(mr_id)
    if chain_df is not None and not chain_df.empty:
        chain_cos = chain_df["co_prefix"].tolist()
        chain_summaries.append(" -> ".join(chain_cos))
        has_mr_no = row.get("EJM MR No.") is not None  # branch_mr_no
        chain_statuses.append("Complete" if has_mr_no else f"{len(chain_cos)} step(s)")
    else:
        steps = transfers.get(mr_id, [])
        chain_summaries.append(_build_chain_summary(steps))
        chain_statuses.append(_get_chain_status(steps, source_co_branch))
```

- [ ] **Step 3: Update row selection to use jute_mr_id**

Replace the row selection + editor call block:
```python
if row_idx is not None and row_idx < len(source_df):
    row = source_df.iloc[row_idx]
    mr_id = int(row["jute_mr_id"])
    steps = transfers.get(mr_id, [_empty_transfer_step()])
    li_data = line_items_map.get(mr_id, [])

    _render_transfer_editor(
        mr_id=mr_id,
        row=row,
        steps=steps,
        line_items=li_data,
        co_branch_options=co_branch_options,
        source_co_branch=source_co_branch,
        co_branch_mapping=co_branch_mapping,
        filter_key=filter_key,
        selected_company_id=selected_company_id,
        selected_branch_id=selected_branch_id,
    )
```

- [ ] **Step 4: Commit**

```bash
git add src/jutetransfer/pages/jute_mr.py
git commit -m "feat: update jute_mr_table_page to use grouped data and chain hydration"
```

---

### Task 11: Rewrite _render_transfer_editor with partial save UI

**Files:**
- Modify: `src/jutetransfer/pages/jute_mr.py`

- [ ] **Step 1: Update `_render_transfer_editor` signature and add warehouse/save buttons**

Rewrite the function. Key changes:
- Accept `line_items` parameter instead of reading weight/rate from row.
- Add warehouse selector per step.
- Recalculation uses new `_recalculate_chain(steps, line_items)`.
- Add "Save Step" button for partial saves.
- Add "Finalize MR" button only when chain returns to source.
- Add "Edit Step" button for saved steps.
- Load existing chain from DB and show completed steps as read-only.

This is the largest single change. The full implementation should:

1. Check DB for existing chain via `get_transfer_chain(mr_id)` + `_reconstruct_chain`.
2. Show existing steps as read-only metric cards.
3. Show editable steps for new/unsaved entries.
4. For each editable step: company selector, date, warehouse selector (via `get_warehouses_by_branch`), % increase.
5. "Save Step" calls `save_transfer_step` with computed `rate_multiplier`.
6. "Finalize MR" appears when last step returns to source.
7. "Edit from Step X" calls `delete_chain_from_step` then refreshes.

The `_empty_transfer_step` dict gains `warehouse_id: None`.

The recalculation call changes from:
```python
_recalculate_chain(steps, weight, original_mr_rate, original_claim)
```
To:
```python
_recalculate_chain(steps, line_items)
```

The `_assign_mr_numbers_batch` call is REMOVED — MR numbers are now assigned inside `save_transfer_step`.

- [ ] **Step 2: Commit**

```bash
git add src/jutetransfer/pages/jute_mr.py
git commit -m "feat: rewrite transfer editor with partial save, warehouse selector, chain hydration"
```

---

### Task 12: Final integration test + cleanup

**Files:**
- All modified files

- [ ] **Step 1: Run all tests**

Run: `cd c:/code/juteTransfer && uv run pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 2: Run the Streamlit app and verify no import errors**

Run: `cd c:/code/juteTransfer && uv run streamlit run app.py --server.headless true` (start, check for errors, then Ctrl+C)
Expected: App starts without import errors

- [ ] **Step 3: Remove old unused functions**

In `jute_mr.py`, remove:
- `_init_transfer_data` (replaced by grouping + session init in page)
- Old `_recalculate_chain` signature (replaced)
- `_assign_mr_numbers_batch` (MR numbers now assigned in transaction)

In `transfer.py`, clean up any dead code from the old `finalize_transfer_chain` that is no longer needed.

- [ ] **Step 4: Run tests again after cleanup**

Run: `cd c:/code/juteTransfer && uv run pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "chore: remove old unused functions, final cleanup"
```
