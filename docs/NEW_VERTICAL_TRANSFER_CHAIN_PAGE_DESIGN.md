# New Vertical Transfer Chain Page — Design Specification

**Date:** 2026-04-03  
**Status:** Ready for Implementation  
**Implementation Approach:** Hybrid (Approach C) — Quick start with TODOs for future refactoring

---

## 1. EXECUTIVE SUMMARY

A new Streamlit page (`pages/new_transfer_chain.py`) that displays transfer chains in a **vertical 3-level hierarchy** instead of the current horizontal editor.

| Level | Component | Purpose |
|-------|-----------|---------|
| **1** | Filters + Monthly MR Table | User selects company/branch/year/month, then clicks an MR row |
| **2** | Transfer Chain Editor | Displays step cards (Step 1 = source, Step 2+ = transfers) |
| **3** | Line Items Table | Inside each card: shows original items (quality, weight, original rate, calculated amount) |

**Key improvements over current page:**
- ✓ Line items visible per step → easier verification of calculations
- ✓ Vertical layout → natural reading order (top to bottom vs. left to right)
- ✓ Self-contained step cards → clear boundaries between steps
- ✓ Original rates shown → trace calculation back to source

**Out of scope (for now):**
- Refactoring shared logic into reusable module (marked with TODOs for future)
- Deleting chains
- Editing saved steps (read-only once saved)
- Bulk save operations

---

## 2. ARCHITECTURE

### 2.1 Overview

```
Page Entry: transfer_chain_page()
    ↓
[Level 1] _render_filters()
    ↓ User selects company, branch, year, month
    ↓
[Level 1] _render_mr_table()
    ↓ Fetch + display all MRs for filter
    ↓ User clicks row → row_idx stored in session state
    ↓
[Levels 2-3] _render_chain_editor()
    ↓ For selected mr_id:
    ├─ Fetch chain from DB (or session cache)
    ├─ Reconstruct chain order via _reconstruct_chain()
    ├─ Loop through steps: _render_step_card()
    │  ├─ Display company, date, % input, summary metrics
    │  ├─ _render_step_line_items() — show items table
    │  └─ Action buttons (Save/Clear/Delete for unsaved only)
    └─ Auto-rerun on save
```

### 2.2 Code Reuse

**Functions reused from existing codebase (no changes required):**
- `jute_mr_chain_helpers._reconstruct_chain()`
- `jute_mr_chain_helpers._recalculate_chain()`
- `jute_mr_chain_helpers._calculate_step_total_amount()`
- `jute_mr_chain_helpers._empty_transfer_step()`
- `queries.get_jute_mr_with_line_items()`
- `queries.get_transfer_chain()` / `get_transfer_chains_batch()`
- `transfer.save_transfer_step()`

**Functions adapted (copy with modifications):**
- `jute_mr_editor._render_transfer_editor()` → Adapted to `_render_chain_editor()` (handles 3-level layout)
- `jute_mr_editor._render_step_calculation_chart()` → Adapted to `_render_step_line_items()` (shows original items table)

**New functions in `new_transfer_chain.py`:**
- `transfer_chain_page()` — entry point
- `_render_filters()` — filter controls
- `_render_mr_table()` — monthly MR table + row selection
- `_render_chain_editor()` — main editor logic
- `_render_step_card()` — individual step card
- `_render_step_line_items()` — line items table within card

---

## 3. FILE STRUCTURE

```
src/jutetransfer/
├── pages/
│   ├── jute_mr.py                 (existing, unchanged)
│   ├── new_transfer_chain.py       (NEW, ~450 lines)
│   └── __init__.py
├── jute_mr_chain_helpers.py        (existing, unchanged)
├── jute_mr_editor.py               (existing, unchanged)
└── transfer.py                     (existing, unchanged)
```

**Total new code:** ~450 lines in `new_transfer_chain.py`

---

## 4. DETAILED COMPONENT DESIGN

### 4.1 `transfer_chain_page()` — Entry Point (50 lines)

**Responsibility:** Top-level page structure, session state initialization.

**Pseudocode:**
```python
def transfer_chain_page():
    st.title("Vertical Transfer Chain Editor")
    st.markdown("[Help] How transfers work: ...")
    
    # Get current year/month for defaults
    current_year, current_month = datetime.now().year, datetime.now().month
    
    # Initialize session state keys (per filter)
    filter_key = None  # Will be set after filters are selected
    
    # Render three levels in order
    _render_filters()  # Updates filter_key in session
    _render_mr_table(filter_key)  # Shows table, handles row selection
    _render_chain_editor(filter_key)  # Shows editor for selected row
```

### 4.2 `_render_filters()` — Filter Controls (40 lines)

**Responsibility:** Dropdown selectors for company, branch, year, month.

**Outputs:**
- `st.session_state.selected_company_id`, `selected_company_name`
- `st.session_state.selected_branch_id`, `selected_branch_name`
- `st.session_state.selected_year`, `selected_month`
- Derived: `filter_key = f"{co_id}_{branch_id}_{year}_{month}"`

**Pseudocode:**
```python
def _render_filters():
    col1, col2 = st.columns(2)
    with col1:
        company_options = get_companies()
        st.session_state.selected_company_name = st.selectbox(...)
        st.session_state.selected_company_id = company_options.get(...)
    
    with col2:
        branch_options = get_branches_by_company(st.session_state.selected_company_id)
        st.session_state.selected_branch_name = st.selectbox(...)
        st.session_state.selected_branch_id = branch_options.get(...)
    
    col3, col4 = st.columns(2)
    with col3:
        st.session_state.selected_year = st.selectbox("Year", options=range(2020, 2031))
    with col4:
        st.session_state.selected_month = st.selectbox("Month", options=range(1, 13))
```

### 4.3 `_render_mr_table()` — Monthly MR Table (80 lines)

**Responsibility:** Load MRs via filters, display in interactive table, handle row selection.

**Input:** `filter_key` (derived from filters)

**Session state cache:**
- `raw_df_{filter_key}` — from `get_jute_mr_with_line_items()`
- `source_df_{filter_key}` — grouped by MR header
- `line_items_{filter_key}` — `{mr_id: [line_items]}`
- `chains_map_{filter_key}` — from `get_transfer_chains_batch()`

**Pseudocode:**
```python
def _render_mr_table(filter_key):
    # Load or retrieve from cache
    if f"raw_df_{filter_key}" not in st.session_state:
        raw_df = get_jute_mr_with_line_items(
            year=st.session_state.selected_year,
            month=st.session_state.selected_month,
            company_id=st.session_state.selected_company_id,
            branch_id=st.session_state.selected_branch_id
        )
        grouped_df, line_items_map = _group_by_mr(raw_df)
        
        st.session_state[f"raw_df_{filter_key}"] = raw_df
        st.session_state[f"source_df_{filter_key}"] = grouped_df
        st.session_state[f"line_items_{filter_key}"] = line_items_map
        
        # Batch-load all chains
        all_mr_ids = grouped_df["jute_mr_id"].astype(int).tolist()
        st.session_state[f"chains_map_{filter_key}"] = get_transfer_chains_batch(all_mr_ids)
    
    source_df = st.session_state[f"source_df_{filter_key}"]
    
    # Display table with row selection
    st.subheader(f"Monthly MR Overview — {len(source_df)} records")
    event = st.dataframe(
        source_df[COMPACT_COLUMNS],
        use_container_width=True,
        on_select="rerun",
        selection_mode="single-row"
    )
    
    # Store selected row index
    if event.selection.rows:
        st.session_state[f"selected_row_{filter_key}"] = event.selection.rows[0]
```

### 4.4 `_render_chain_editor()` — Main Editor (200 lines)

**Responsibility:** Load chain for selected MR, reconstruct, render step cards.

**Input:** `filter_key`

**Pseudocode:**
```python
def _render_chain_editor(filter_key):
    # Get selected row index
    selected_row_key = f"selected_row_{filter_key}"
    if selected_row_key not in st.session_state:
        st.info("Select a row from the table to edit its transfer chain")
        return
    
    row_idx = st.session_state[selected_row_key]
    source_df = st.session_state[f"source_df_{filter_key}"]
    
    if row_idx >= len(source_df):
        return
    
    row = source_df.iloc[row_idx]
    mr_id = int(row["jute_mr_id"])
    line_items_map = st.session_state[f"line_items_{filter_key}"]
    chains_map = st.session_state[f"chains_map_{filter_key}"]
    
    # Initialize transfers session state if needed
    transfers_key = f"transfers_{filter_key}"
    if transfers_key not in st.session_state:
        st.session_state[transfers_key] = {}
    
    transfers = st.session_state[transfers_key]
    
    if mr_id not in transfers:
        # First time loading this MR: initialize from DB chain
        step0 = _empty_transfer_step()
        step0["mr_date"] = row["MR DATE"]
        transfers[mr_id] = [step0]
        
        # Load saved chain if exists
        chain_df = chains_map.get(mr_id)
        if chain_df is not None and not chain_df.empty:
            chain_mrs = chain_df.to_dict("records")
            saved_chain = _reconstruct_chain(chain_mrs, selected_company_id=st.session_state.selected_company_id)
            
            # Populate steps from saved chain
            prev_total = row["Total Amount"]
            for sc in saved_chain:
                step = _empty_transfer_step()
                step["company"] = f"{sc['co_prefix']}-{sc['branch_name']}"
                step["mr_no"] = sc["branch_mr_no"]
                step["mr_date"] = sc["jute_mr_date"]
                step["total_amount"] = sc["total_amount"]
                step["claim_amount"] = sc["claim_amount"]
                step["net_amount"] = sc["net_total"]
                step["saved_mr_id"] = sc["jute_mr_id"]
                
                # Back-calculate % (TODO: fix via DB column after pct_rate_increase migration)
                current_total = sc["total_amount"]
                if prev_total > 0:
                    step["pct_rate_increase"] = ((current_total - prev_total) / prev_total) * 100
                
                transfers[mr_id].append(step)
                prev_total = current_total
        
        # Add blank step for editing
        transfers[mr_id].append(_empty_transfer_step())
    
    steps = transfers[mr_id]
    li_data = line_items_map.get(mr_id, [])
    orig_total = row["Total Amount"]
    
    # Render chain header
    st.subheader(f"Transfer Chain — {row['Jute Gate Entry No']} ({row['Jute Supplier']})")
    
    # Render all steps
    for i, step in enumerate(steps):
        _render_step_card(
            step_index=i,
            step=step,
            all_steps=steps,
            line_items=li_data,
            original_total_amount=orig_total,
            mr_id=mr_id,
            filter_key=filter_key
        )
```

### 4.5 `_render_step_card()` — Individual Step Card (120 lines)

**Responsibility:** Render company/date/% inputs, summary metrics, line items, action buttons.

**Inputs:**
- `step_index` — position in chain (0 = source)
- `step` — step dict
- `all_steps` — full steps list (for context)
- `line_items` — original line items
- `original_total_amount` — source MR total
- `mr_id` — parent MR ID
- `filter_key` — for session state

**Pseudocode:**
```python
def _render_step_card(step_index, step, all_steps, line_items, original_total_amount, mr_id, filter_key):
    is_saved = "saved_mr_id" in step and step["saved_mr_id"] is not None
    is_empty = not step.get("company")
    
    # Determine card color
    if is_empty:
        card_color = "#252540"  # gray
    elif is_saved:
        card_color = "#2a2520"  # orange tint
    else:
        card_color = "#1e3a2a"  # green tint (editing)
    
    with st.container():
        col1, col2, col3 = st.columns([2, 1, 1])
        
        # Company selection
        with col1:
            if is_saved:
                st.write(f"**{step['company']}**")
            else:
                co_branch_options, _ = get_company_branch_options()
                step["company"] = st.selectbox(
                    f"Step {step_index + 1} Company",
                    options=co_branch_options if not is_empty else [""],
                    key=f"company_{mr_id}_{step_index}"
                )
        
        # Date input
        with col2:
            if is_saved:
                st.write(step["mr_date"])
            else:
                step["mr_date"] = st.date_input(
                    f"Date",
                    value=step["mr_date"],
                    key=f"date_{mr_id}_{step_index}"
                )
        
        # Status badge
        with col3:
            if is_saved:
                st.write("✓ Saved")
            else:
                st.write("● Editing")
        
        # % Rate Increase input (for step 2+)
        if step_index > 0 and not is_saved and step.get("company"):
            pct_col, _ = st.columns([1, 2])
            with pct_col:
                pct_key = f"pct_{mr_id}_{step_index}"
                if pct_key not in st.session_state:
                    st.session_state[pct_key] = step.get("pct_rate_increase", 0.0)
                
                new_pct = st.number_input(
                    f"% Rate Increase",
                    value=st.session_state[pct_key],
                    step=0.01,
                    key=f"pct_input_{mr_id}_{step_index}"
                )
                
                # On Enter: trigger recalculation
                if new_pct != st.session_state[pct_key]:
                    all_steps[step_index]["pct_rate_increase"] = new_pct
                    _recalculate_chain(all_steps, line_items, original_total_amount)
                    st.session_state[pct_key] = new_pct
                    st.rerun()
        
        # Summary metrics
        st.markdown(f"""
        **Rate:** ₹{step.get('mr_rate', 0):.2f} | 
        **Total:** ₹{step.get('total_amount', 0):.0f} | 
        **Claim:** ₹{step.get('claim_amount', 0):.0f} | 
        **Net:** ₹{step.get('net_amount', 0):.0f}
        """)
        
        # Line items table
        _render_step_line_items(step_index, line_items, all_steps)
        
        # Action buttons (only for unsaved steps)
        if not is_saved and step.get("company"):
            col_save, col_clear, col_delete = st.columns(3)
            
            with col_save:
                if st.button("Save Step", key=f"save_{mr_id}_{step_index}"):
                    _save_step(step_index, step, all_steps, line_items, original_total_amount, mr_id, filter_key)
            
            with col_clear:
                if st.button("Clear", key=f"clear_{mr_id}_{step_index}"):
                    all_steps[step_index] = _empty_transfer_step()
                    st.rerun()
            
            with col_delete:
                if st.button("Delete", key=f"delete_{mr_id}_{step_index}"):
                    all_steps.pop(step_index)
                    st.rerun()
        
        st.divider()
```

### 4.6 `_render_step_line_items()` — Line Items Table (40 lines)

**Responsibility:** Display original line items with calculated amounts for this step.

**Pseudocode:**
```python
def _render_step_line_items(step_index, line_items, all_steps):
    if not line_items:
        st.write("(No line items)")
        return
    
    # Build table data
    rows = []
    for li in line_items:
        weight = float(li.get("weight", 0))
        orig_rate = float(li.get("original_rate", 0))
        quality = li.get("item_quality", "Item")
        
        # Calculate amount for this step (using cumulative multiplier)
        if step_index == 0:
            amount = weight * orig_rate / 100
        else:
            multiplier = 1.0
            for i in range(1, step_index):
                pct = float(all_steps[i].get("pct_rate_increase", 0) or 0)
                multiplier *= (1 + pct / 100)
            
            current_pct = float(all_steps[step_index].get("pct_rate_increase", 0) or 0)
            current_multiplier = multiplier * (1 + current_pct / 100)
            amount = weight * orig_rate * current_multiplier / 100
        
        rows.append({
            "Quality": quality,
            "Weight (KG)": int(weight),
            "Original Rate": f"₹{orig_rate:.2f}",
            "Amount": f"₹{amount:.0f}"
        })
    
    # Total row
    total_amount = sum(
        float(li.get("weight", 0)) * float(li.get("original_rate", 0)) / 100
        for li in line_items
    ) if step_index == 0 else sum(...)  # similar cumulative calc
    
    rows.append({
        "Quality": "TOTAL",
        "Weight (KG)": sum(int(float(li.get("weight", 0))) for li in line_items),
        "Original Rate": "—",
        "Amount": f"₹{total_amount:.0f}"
    })
    
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)
```

### 4.7 `_save_step()` — Step Persistence (50 lines)

**Responsibility:** Call `save_transfer_step()`, handle DB transaction, reload chain.

**Pseudocode:**
```python
def _save_step(step_index, step, all_steps, line_items, original_total_amount, mr_id, filter_key):
    # Gather context
    source_row = st.session_state[f"source_df_{filter_key}"].iloc[...]
    prev_co_id = all_steps[step_index - 1].get("owner_co_id") if step_index > 0 else ...
    
    # Determine if final (returns to source)
    is_final = (step.get("company") == source_co_branch)
    
    try:
        result = save_transfer_step(
            source_mr_id=mr_id,
            step=TransferStep(
                co_id=...,
                branch_id=...,
                mr_date=step["mr_date"],
                mr_rate=step.get("mr_rate", 0),
                pct_rate_increase=step.get("pct_rate_increase", 0),
                total_amount=step.get("total_amount", 0),
                claim_amount=step.get("claim_amount", 0),
                net_amount=step.get("net_amount", 0),
                warehouse_id=step.get("warehouse_id"),
                mr_no=0
            ),
            prev_co_id=prev_co_id,
            prev_branch_id=...,
            source_co_id=...,
            source_branch_id=...,
            root_mr_id=...,
            updated_by=1,
            rate_multiplier=...,
            is_first_step=(step_index == 0),
            is_final=is_final
        )
        
        # Clear session cache to force reload
        for key in [f"transfers_{filter_key}", f"chains_map_{filter_key}"]:
            if key in st.session_state:
                del st.session_state[key]
        
        st.success(f"Step {step_index + 1} saved!")
        st.rerun()
    
    except Exception as e:
        st.error(f"Error saving step: {str(e)}")
```

---

## 5. STATE MANAGEMENT

**Session State Keys** (per `filter_key`):

```python
filter_key = f"{co_id}_{branch_id}_{year}_{month}"

st.session_state[f"raw_df_{filter_key}"] = pd.DataFrame  # Original query result
st.session_state[f"source_df_{filter_key}"] = pd.DataFrame  # Grouped by MR header
st.session_state[f"line_items_{filter_key}"] = dict  # {mr_id: [line_items]}
st.session_state[f"transfers_{filter_key}"] = dict  # {mr_id: [steps]}
st.session_state[f"chains_map_{filter_key}"] = dict  # {mr_id: chain_df}
st.session_state[f"selected_row_{filter_key}"] = int  # Selected row index
```

**Widget State** (for % input persistence):
```python
st.session_state[f"pct_{mr_id}_{step_index}"] = float  # % Rate Increase value
```

**Rerun Triggers:**
- User selects filter → invalidate cache for that `filter_key`
- User selects table row → set `selected_row_{filter_key}`
- User enters % → `st.rerun()`
- User saves step → invalidate `transfers_{filter_key}` and `chains_map_{filter_key}`, then `st.rerun()`

---

## 6. ERROR HANDLING

| Scenario | Handling |
|----------|----------|
| No MRs found for filter | Display warning, show 0 records |
| User selects row, then changes filter | Clear `selected_row_{filter_key}`, show table only |
| Chain reconstruction fails | Show error message, log exception |
| Save fails (DB constraint) | Show error popup, keep step unsaved |
| Line items missing for MR | Display empty line items section gracefully |

---

## 7. TESTING STRATEGY

**Manual testing** (before handoff):

1. **Load page:**
   - [ ] Filters render correctly
   - [ ] MR table loads with correct data
   - [ ] Cache works (switching filters back/forth is fast)

2. **Select MR:**
   - [ ] Chain loads from DB if saved steps exist
   - [ ] Blank step appended for editing
   - [ ] Line items display correctly

3. **Edit step:**
   - [ ] % input accepts decimals (1.00, 2.50, 0.001)
   - [ ] Enter key triggers recalculation
   - [ ] Totals update in all downstream steps
   - [ ] Line items table updates with new amounts

4. **Save step:**
   - [ ] Step is marked as saved (read-only)
   - [ ] New step card appended for next edit
   - [ ] Page reloads, chain refetched from DB
   - [ ] Saved step persists across page refresh

5. **Edge cases:**
   - [ ] Empty chain (no saved transfers yet)
   - [ ] 1-step chain (only source, no buyers)
   - [ ] 5+ step chain (vertical scroll works)
   - [ ] MR with no line items

---

## 8. FUTURE REFACTORING (TODOs)

**Post-launch:** Extract shared components into `jute_mr_page_helpers.py`:

1. **Extract `_render_step_card()` → `render_step_card_component()` (shared)**
   - Used by: both old `jute_mr.py` and new `new_transfer_chain.py`
   - Benefit: Single source of truth for step rendering

2. **Extract `_render_step_line_items()` → `render_step_line_items_component()` (shared)**
   - Used by: both pages
   - Benefit: Consistent line items display

3. **Extract filter + table logic → `render_mr_table_component()` (shared)**
   - Used by: both pages
   - Benefit: Unified monthly MR table behavior

4. **Extract chain loading → `load_transfer_chain()` utility (shared)**
   - Encapsulate: DB query + reconstruction + step initialization
   - Benefit: DRY, easier to test

**Implementation note:** TODOs are marked with `# TODO: Extract to jute_mr_page_helpers.py` in code comments.

---

## 9. KNOWN LIMITATIONS & FUTURE IMPROVEMENTS

1. **Back-calculation of `pct_rate_increase`** (causes 6918.51 bug)
   - Workaround: None currently; rounding error accepted for now
   - Fix: Add `pct_rate_increase` column to `jute_mr` table (separate task)

2. **No bulk save** — Each step saves individually
   - Could batch multiple steps into single transaction (future enhancement)

3. **No edit of saved steps** — Fully read-only once saved
   - Could add "Unlock" feature to allow modifications (future enhancement)

4. **Line items non-editable** — Can't add/remove items per step
   - By design: transfer preserves original items, only rates change
   - Aligns with business logic

---

## 10. SUCCESS CRITERIA

- ✓ Page renders without errors
- ✓ Monthly MR table loads correctly
- ✓ Selecting MR loads its transfer chain
- ✓ Line items visible per step
- ✓ % input triggers recalculation
- ✓ Totals update correctly
- ✓ Save step persists to DB
- ✓ Saved step shows as read-only
- ✓ Auto-reload after save shows correct state
- ✓ Vertical layout is readable and usable

---

## Appendix A: Import Statements

```python
# Standard library
from datetime import datetime, date
import pandas as pd

# Streamlit
import streamlit as st

# Project imports
from ..queries import (
    get_companies,
    get_branches_by_company,
    get_company_branch_options,
    get_jute_mr_with_line_items,
    get_transfer_chains_batch,
    get_transfer_chain
)
from ..jute_mr_chain_helpers import (
    _group_by_mr,
    _reconstruct_chain,
    _recalculate_chain,
    _calculate_step_total_amount,
    _empty_transfer_step,
    _build_chain_summary,
    _get_chain_status,
    _find_source_co_branch,
    COMPACT_COLUMNS,
)
from ..transfer import save_transfer_step
from ..schemas import TransferStep
```

---

**End of Design Specification**
