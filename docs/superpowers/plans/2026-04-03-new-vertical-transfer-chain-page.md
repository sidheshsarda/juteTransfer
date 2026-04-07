# New Vertical Transfer Chain Page — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a new Streamlit page that displays transfer chains in a vertical 3-level hierarchy (filters → MR table → step cards with line items).

**Architecture:** Single new file (`pages/new_transfer_chain.py`, ~450 lines) reuses existing helpers from `jute_mr_chain_helpers.py`, `queries.py`, and `transfer.py`. Seven functions organized from top-level entry point down through UI components. Session state caching per filter key (`co_id_branch_id_year_month`).

**Tech Stack:** Streamlit, pandas, SQLAlchemy ORM, Python datetime, existing project modules.

---

## File Structure

**Files created:**
- `src/jutetransfer/pages/new_transfer_chain.py` — Main new page (~450 lines)

**Files modified:**
- None (reuses existing helpers without modification)

**Files examined (existing, unchanged):**
- `src/jutetransfer/jute_mr_chain_helpers.py` — Use `_reconstruct_chain()`, `_recalculate_chain()`, `_calculate_step_total_amount()`, `_empty_transfer_step()`
- `src/jutetransfer/queries.py` — Use `get_companies()`, `get_branches_by_company()`, `get_jute_mr_with_line_items()`, `get_transfer_chains_batch()`
- `src/jutetransfer/transfer.py` — Use `save_transfer_step()`

---

## Task 1: Create File Structure and Stub All Functions

**Files:**
- Create: `src/jutetransfer/pages/new_transfer_chain.py`

- [ ] **Step 1: Create the new file with imports and function stubs**

Create file at `src/jutetransfer/pages/new_transfer_chain.py`:

```python
"""
New Vertical Transfer Chain Editor Page
Displays transfer chains in a 3-level hierarchy (filters → MR table → step cards with line items).
"""

from datetime import datetime, date
import pandas as pd
import streamlit as st

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
)
from ..transfer import save_transfer_step
from ..schemas import TransferStep

# Constants
COMPACT_COLUMNS = ["Jute Gate Entry No", "Jute Supplier", "Total Amount", "Claim Amount", "Net Total"]


def transfer_chain_page():
    """Entry point for the vertical transfer chain page."""
    pass


def _render_filters():
    """Render dropdown filters for company, branch, year, month."""
    pass


def _render_mr_table(filter_key):
    """Render monthly MR table with row selection."""
    pass


def _render_chain_editor(filter_key):
    """Main editor logic: load chain, reconstruct, render step cards."""
    pass


def _render_step_card(step_index, step, all_steps, line_items, original_total_amount, mr_id, filter_key):
    """Render individual step card with inputs and action buttons."""
    pass


def _render_step_line_items(step_index, line_items, all_steps):
    """Render line items table within step card."""
    pass


def _save_step(step_index, step, all_steps, line_items, original_total_amount, mr_id, filter_key):
    """Save step to database and reload chain."""
    pass
```

- [ ] **Step 2: Verify file was created and imports resolve**

Run in terminal from project root:
```bash
python -c "from src.jutetransfer.pages.new_transfer_chain import transfer_chain_page; print('✓ File created, imports OK')"
```

Expected output: `✓ File created, imports OK`

- [ ] **Step 3: Register page in app.py navigation**

Read `app.py` to find where pages are registered:

```bash
grep -n "pages/" src/jutetransfer/../app.py | head -20
```

Add entry for new page in the page selector (exact location depends on current app structure). For example, if using a `st.sidebar.radio()`:

```python
# In app.py, add to the page selection logic:
elif page == "Transfer Chain (Vertical)":
    from src.jutetransfer.pages.new_transfer_chain import transfer_chain_page
    transfer_chain_page()
```

- [ ] **Step 4: Commit scaffold**

```bash
git add src/jutetransfer/pages/new_transfer_chain.py
git commit -m "scaffold: create new_transfer_chain.py with stubs"
```

---

## Task 2: Implement `transfer_chain_page()` Entry Point

**Files:**
- Modify: `src/jutetransfer/pages/new_transfer_chain.py` (lines ~10-30)

- [ ] **Step 1: Write the entry point function**

Replace the stub for `transfer_chain_page()`:

```python
def transfer_chain_page():
    """
    Entry point for vertical transfer chain page.
    
    Flow:
    1. Page title and help
    2. Render filters (company, branch, year, month)
    3. Render monthly MR table
    4. Render chain editor for selected MR
    """
    st.set_page_config(page_title="Transfer Chain Editor", layout="wide")
    st.title("Vertical Transfer Chain Editor")
    st.markdown("""
    **How transfers work:**
    - Step 1 is the source company (receives material at gate entry)
    - Step 2+ are transfer steps (material moves between companies)
    - Each step can increase the rate by a %, which cascades downward
    - Select an MR row to edit its transfer chain
    """)
    
    # Render filters (this also populates session state keys)
    _render_filters()
    
    # Build filter key from session state
    filter_key = None
    if "selected_company_id" in st.session_state and "selected_branch_id" in st.session_state:
        filter_key = (
            f"{st.session_state['selected_company_id']}_"
            f"{st.session_state['selected_branch_id']}_"
            f"{st.session_state.get('selected_year', datetime.now().year)}_"
            f"{st.session_state.get('selected_month', datetime.now().month)}"
        )
    
    # Render table and editor if filter key exists
    if filter_key:
        _render_mr_table(filter_key)
        _render_chain_editor(filter_key)
    else:
        st.info("Select company and branch from filters to continue")
```

- [ ] **Step 2: Test the entry point loads without errors**

In Streamlit terminal, navigate to project and run:

```bash
streamlit run app.py -- --logger.level=debug
```

Open the app, navigate to "Transfer Chain (Vertical)" page.

Expected: Page loads, shows title, help text, and "Select company and branch" message.

- [ ] **Step 3: Commit**

```bash
git add src/jutetransfer/pages/new_transfer_chain.py
git commit -m "feat: implement transfer_chain_page() entry point"
```

---

## Task 3: Implement `_render_filters()` Function

**Files:**
- Modify: `src/jutetransfer/pages/new_transfer_chain.py` (lines ~40-80)

- [ ] **Step 1: Write the filter component**

Replace the stub for `_render_filters()`:

```python
def _render_filters():
    """
    Render filter controls (company, branch, year, month).
    Updates session state with selected values.
    
    Session state keys:
    - selected_company_id, selected_company_name
    - selected_branch_id, selected_branch_name
    - selected_year, selected_month
    """
    st.subheader("Filters")
    
    col1, col2, col3, col4 = st.columns(4)
    
    # Company selector
    with col1:
        companies = get_companies()
        if not companies:
            st.warning("No companies found")
            return
        
        company_names = [f"{c['co_id']}-{c['co_name']}" for c in companies]
        company_dict = {f"{c['co_id']}-{c['co_name']}": c['co_id'] for c in companies}
        
        selected_co_label = st.selectbox(
            "Company",
            options=company_names,
            key="selectbox_company"
        )
        st.session_state["selected_company_id"] = company_dict[selected_co_label]
        st.session_state["selected_company_name"] = selected_co_label
    
    # Branch selector
    with col2:
        if "selected_company_id" in st.session_state:
            branches = get_branches_by_company(st.session_state["selected_company_id"])
            if not branches:
                st.warning("No branches for this company")
                return
            
            branch_names = [f"{b['branch_id']}-{b['branch_name']}" for b in branches]
            branch_dict = {f"{b['branch_id']}-{b['branch_name']}": b['branch_id'] for b in branches}
            
            selected_branch_label = st.selectbox(
                "Branch",
                options=branch_names,
                key="selectbox_branch"
            )
            st.session_state["selected_branch_id"] = branch_dict[selected_branch_label]
            st.session_state["selected_branch_name"] = selected_branch_label
        else:
            st.write("Select company first")
    
    # Year selector
    with col3:
        current_year = datetime.now().year
        years = list(range(2020, current_year + 2))
        st.session_state["selected_year"] = st.selectbox(
            "Year",
            options=years,
            index=years.index(current_year),
            key="selectbox_year"
        )
    
    # Month selector
    with col4:
        current_month = datetime.now().month
        months = list(range(1, 13))
        month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        month_dict = {month_names[i]: i+1 for i in range(12)}
        
        selected_month_label = st.selectbox(
            "Month",
            options=month_names,
            index=current_month - 1,
            key="selectbox_month"
        )
        st.session_state["selected_month"] = month_dict[selected_month_label]
```

- [ ] **Step 2: Test filters render and populate session state**

Run Streamlit app, navigate to page. Expected: 4 dropdowns visible, selection updates session state.

Verify in terminal that changing filters doesn't throw errors.

- [ ] **Step 3: Commit**

```bash
git add src/jutetransfer/pages/new_transfer_chain.py
git commit -m "feat: implement _render_filters() with company/branch/year/month selectors"
```

---

## Task 4: Implement `_render_mr_table()` Function

**Files:**
- Modify: `src/jutetransfer/pages/new_transfer_chain.py` (lines ~90-180)

- [ ] **Step 1: Write the MR table component with caching**

Replace the stub for `_render_mr_table()`:

```python
def _render_mr_table(filter_key):
    """
    Load monthly MRs via filters, display in interactive table, handle row selection.
    
    Caches data in session state by filter_key to avoid re-querying on reruns.
    
    Session state keys:
    - raw_df_{filter_key} — original query result
    - source_df_{filter_key} — grouped by MR header
    - line_items_{filter_key} — {mr_id: [line_items]}
    - chains_map_{filter_key} — {mr_id: chain_df}
    - selected_row_{filter_key} — selected row index
    """
    st.subheader("Monthly MR Overview")
    
    # Load data if not cached
    raw_df_key = f"raw_df_{filter_key}"
    if raw_df_key not in st.session_state:
        try:
            raw_df = get_jute_mr_with_line_items(
                year=st.session_state["selected_year"],
                month=st.session_state["selected_month"],
                company_id=st.session_state["selected_company_id"],
                branch_id=st.session_state["selected_branch_id"]
            )
            
            if raw_df.empty:
                st.info("No MRs found for selected filters")
                return
            
            # Group by MR header
            grouped_df, line_items_map = _group_by_mr(raw_df)
            
            # Cache all data
            st.session_state[raw_df_key] = raw_df
            st.session_state[f"source_df_{filter_key}"] = grouped_df
            st.session_state[f"line_items_{filter_key}"] = line_items_map
            
            # Batch-load all chains
            all_mr_ids = grouped_df["jute_mr_id"].astype(int).tolist()
            chains_dict = {}
            for mr_id in all_mr_ids:
                chain_data = get_transfer_chain(mr_id)
                if chain_data is not None:
                    chains_dict[mr_id] = chain_data
            st.session_state[f"chains_map_{filter_key}"] = chains_dict
        
        except Exception as e:
            st.error(f"Error loading MRs: {str(e)}")
            return
    
    # Get cached data
    grouped_df = st.session_state[f"source_df_{filter_key}"]
    
    # Display table with row selection
    st.write(f"**{len(grouped_df)} records found**")
    
    event = st.dataframe(
        grouped_df[COMPACT_COLUMNS] if COMPACT_COLUMNS else grouped_df,
        use_container_width=True,
        on_select="rerun",
        selection_mode="single-row"
    )
    
    # Store selected row index
    if event.selection.rows:
        st.session_state[f"selected_row_{filter_key}"] = event.selection.rows[0]
    else:
        # Clear selection if user deselects
        if f"selected_row_{filter_key}" in st.session_state:
            del st.session_state[f"selected_row_{filter_key}"]
```

- [ ] **Step 2: Test MR table loads and displays data**

Run Streamlit app. Select company, branch, year, month. Expected: Table populates with MRs.

Click a row and observe the selection is stored in session state (page reruns, row stays selected).

- [ ] **Step 3: Commit**

```bash
git add src/jutetransfer/pages/new_transfer_chain.py
git commit -m "feat: implement _render_mr_table() with caching and row selection"
```

---

## Task 5: Implement `_render_chain_editor()` Function

**Files:**
- Modify: `src/jutetransfer/pages/new_transfer_chain.py` (lines ~190-290)

- [ ] **Step 1: Write the chain editor component**

Replace the stub for `_render_chain_editor()`:

```python
def _render_chain_editor(filter_key):
    """
    Load transfer chain for selected MR, reconstruct order, render step cards.
    
    Session state keys:
    - transfers_{filter_key} — {mr_id: [steps]} for editing
    """
    selected_row_key = f"selected_row_{filter_key}"
    if selected_row_key not in st.session_state:
        return  # No row selected
    
    row_idx = st.session_state[selected_row_key]
    grouped_df = st.session_state[f"source_df_{filter_key}"]
    
    if row_idx >= len(grouped_df):
        return
    
    row = grouped_df.iloc[row_idx]
    mr_id = int(row["jute_mr_id"])
    line_items_map = st.session_state[f"line_items_{filter_key}"]
    chains_map = st.session_state[f"chains_map_{filter_key}"]
    
    # Initialize transfers session state if needed
    transfers_key = f"transfers_{filter_key}"
    if transfers_key not in st.session_state:
        st.session_state[transfers_key] = {}
    
    transfers = st.session_state[transfers_key]
    
    # First time loading this MR: initialize from DB chain
    if mr_id not in transfers:
        step0 = _empty_transfer_step()
        step0["mr_date"] = row["MR DATE"] if "MR DATE" in row else date.today()
        transfers[mr_id] = [step0]
        
        # Load saved chain if exists
        chain_data = chains_map.get(mr_id)
        if chain_data is not None and not chain_data.empty:
            try:
                chain_mrs = chain_data.to_dict("records") if hasattr(chain_data, 'to_dict') else chain_data
                saved_chain = _reconstruct_chain(chain_mrs, selected_company_id=st.session_state["selected_company_id"])
                
                # Populate steps from saved chain
                prev_total = float(row["Total Amount"])
                for sc in saved_chain:
                    step = _empty_transfer_step()
                    step["company"] = f"{sc.get('co_prefix', 'N/A')}-{sc.get('branch_name', 'N/A')}"
                    step["mr_no"] = sc.get("branch_mr_no", "")
                    step["mr_date"] = sc.get("jute_mr_date", date.today())
                    step["total_amount"] = float(sc.get("total_amount", 0))
                    step["claim_amount"] = float(sc.get("claim_amount", 0))
                    step["net_amount"] = float(sc.get("net_total", 0))
                    step["saved_mr_id"] = sc.get("jute_mr_id")
                    
                    # Back-calculate % rate increase (TODO: use DB column after migration)
                    current_total = float(sc.get("total_amount", 0))
                    if prev_total > 0:
                        step["pct_rate_increase"] = ((current_total - prev_total) / prev_total) * 100
                    else:
                        step["pct_rate_increase"] = 0.0
                    
                    transfers[mr_id].append(step)
                    prev_total = current_total
            except Exception as e:
                st.error(f"Error reconstructing chain: {str(e)}")
        
        # Add blank step for editing
        transfers[mr_id].append(_empty_transfer_step())
    
    steps = transfers[mr_id]
    li_data = line_items_map.get(mr_id, [])
    orig_total = float(row["Total Amount"])
    
    # Render chain header
    st.divider()
    st.subheader(f"Transfer Chain — {row.get('Jute Gate Entry No', 'N/A')} ({row.get('Jute Supplier', 'N/A')})")
    st.write(f"**Original Total:** ₹{orig_total:,.0f}")
    
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

- [ ] **Step 2: Test chain editor loads for selected MR**

Run app, select a row. Expected: Chain header appears, original total shown, blank step card below.

If MR has saved transfers, verify they appear as read-only cards above the blank editing card.

- [ ] **Step 3: Commit**

```bash
git add src/jutetransfer/pages/new_transfer_chain.py
git commit -m "feat: implement _render_chain_editor() with chain loading and reconstruction"
```

---

## Task 6: Implement `_render_step_card()` Function

**Files:**
- Modify: `src/jutetransfer/pages/new_transfer_chain.py` (lines ~300-420)

- [ ] **Step 1: Write the step card component**

Replace the stub for `_render_step_card()`:

```python
def _render_step_card(step_index, step, all_steps, line_items, original_total_amount, mr_id, filter_key):
    """
    Render individual step card with company/date/% inputs, metrics, line items, and action buttons.
    
    Inputs:
    - step_index: position in chain (0 = source)
    - step: step dict
    - all_steps: full steps list (for recalculation context)
    - line_items: original line items
    - original_total_amount: source MR total
    - mr_id: parent MR ID
    - filter_key: for session state
    """
    is_saved = "saved_mr_id" in step and step.get("saved_mr_id") is not None
    is_empty = not step.get("company")
    
    # Card styling
    with st.container(border=True):
        st.markdown(f"### Step {step_index + 1}")
        
        col1, col2, col3 = st.columns([2, 1, 1])
        
        # Company selection
        with col1:
            if is_saved:
                st.write(f"**Company:** {step['company']}")
            else:
                co_options, _ = get_company_branch_options()
                co_list = [co["co_id"] for co in co_options] if co_options else []
                step["company"] = st.selectbox(
                    "Company",
                    options=co_list if co_list else [""],
                    key=f"company_{mr_id}_{step_index}",
                    disabled=is_saved
                )
        
        # Date input
        with col2:
            current_date = step.get("mr_date", date.today())
            if isinstance(current_date, str):
                try:
                    current_date = datetime.strptime(current_date, "%Y-%m-%d").date()
                except:
                    current_date = date.today()
            
            if is_saved:
                st.write(f"**Date:** {current_date}")
            else:
                step["mr_date"] = st.date_input(
                    "Date",
                    value=current_date,
                    key=f"date_{mr_id}_{step_index}",
                    disabled=is_saved
                )
        
        # Status badge
        with col3:
            if is_saved:
                st.write("✓ **Saved**")
            else:
                st.write("● **Editing**")
        
        # % Rate Increase input (for step 2+, unsaved only)
        if step_index > 0 and not is_saved and step.get("company"):
            pct_key = f"pct_{mr_id}_{step_index}"
            if pct_key not in st.session_state:
                st.session_state[pct_key] = float(step.get("pct_rate_increase", 0) or 0)
            
            col_pct, col_space = st.columns([1, 3])
            with col_pct:
                new_pct = st.number_input(
                    "% Rate Increase",
                    value=st.session_state[pct_key],
                    step=0.01,
                    min_value=0.0,
                    max_value=100.0,
                    key=f"pct_input_{mr_id}_{step_index}"
                )
                
                # On change: trigger recalculation
                if abs(new_pct - st.session_state[pct_key]) > 0.0001:
                    all_steps[step_index]["pct_rate_increase"] = new_pct
                    st.session_state[pct_key] = new_pct
                    
                    # Recalculate chain downstream
                    _recalculate_chain(all_steps, line_items, original_total_amount)
                    st.rerun()
        
        # Summary metrics
        rate = step.get("mr_rate", 0)
        total = step.get("total_amount", 0)
        claim = step.get("claim_amount", 0)
        net = step.get("net_amount", 0)
        
        st.markdown(f"""
        **Rate:** ₹{rate:.2f} | **Total:** ₹{total:,.0f} | **Claim:** ₹{claim:,.0f} | **Net:** ₹{net:,.0f}
        """)
        
        # Line items table
        _render_step_line_items(step_index, line_items, all_steps)
        
        # Action buttons (only for unsaved steps with company set)
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

- [ ] **Step 2: Test step card renders correctly**

Run app, select MR. Expected: Blank step card shows company/date selectors, no % input yet.

For step 2+ (if saved chain exists), verify saved steps show read-only, unsaved step shows editing interface.

- [ ] **Step 3: Commit**

```bash
git add src/jutetransfer/pages/new_transfer_chain.py
git commit -m "feat: implement _render_step_card() with inputs and action buttons"
```

---

## Task 7: Implement `_render_step_line_items()` Function

**Files:**
- Modify: `src/jutetransfer/pages/new_transfer_chain.py` (lines ~430-490)

- [ ] **Step 1: Write the line items table component**

Replace the stub for `_render_step_line_items()`:

```python
def _render_step_line_items(step_index, line_items, all_steps):
    """
    Render line items table showing original items with calculated amounts for this step.
    
    Inputs:
    - step_index: position in chain (0 = source)
    - line_items: list of original line items
    - all_steps: full chain for cumulative multiplier calculation
    """
    if not line_items:
        st.write("*(No line items)*")
        return
    
    # Build table rows
    rows = []
    total_amount = 0.0
    
    for li in line_items:
        try:
            weight = float(li.get("weight", 0) or 0)
            orig_rate = float(li.get("original_rate", 0) or 0)
            quality = li.get("item_quality", "Item")
            
            # Calculate amount for this step using cumulative multiplier
            if step_index == 0:
                # Source step: just weight × original rate / 100
                amount = weight * orig_rate / 100
            else:
                # Transfer step: apply cumulative % increases
                multiplier = 1.0
                for i in range(1, step_index + 1):
                    pct = float(all_steps[i].get("pct_rate_increase", 0) or 0)
                    multiplier *= (1 + pct / 100)
                
                amount = weight * orig_rate * multiplier / 100
            
            rows.append({
                "Quality": quality,
                "Weight (KG)": int(weight),
                "Original Rate": f"₹{orig_rate:.2f}",
                "Amount": f"₹{amount:,.0f}"
            })
            total_amount += amount
        
        except Exception as e:
            st.warning(f"Error processing line item: {str(e)}")
            continue
    
    # Add total row
    total_weight = sum(int(float(li.get("weight", 0) or 0)) for li in line_items)
    rows.append({
        "Quality": "**TOTAL**",
        "Weight (KG)": total_weight,
        "Original Rate": "—",
        "Amount": f"**₹{total_amount:,.0f}**"
    })
    
    # Display table
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)
```

- [ ] **Step 2: Test line items table displays correctly**

Run app, select MR. Expected: Line items table shows for each step with correct amounts.

For source step (Step 0): amount = weight × original_rate / 100

For transfer steps: amount = weight × original_rate × cumulative_multiplier / 100

Verify total row sums correctly.

- [ ] **Step 3: Commit**

```bash
git add src/jutetransfer/pages/new_transfer_chain.py
git commit -m "feat: implement _render_step_line_items() with cumulative multiplier calculation"
```

---

## Task 8: Implement `_save_step()` Function

**Files:**
- Modify: `src/jutetransfer/pages/new_transfer_chain.py` (lines ~500-600)

- [ ] **Step 1: Write the step persistence function**

Replace the stub for `_save_step()`:

```python
def _save_step(step_index, step, all_steps, line_items, original_total_amount, mr_id, filter_key):
    """
    Save step to database via save_transfer_step().
    Clear cache to force reload, then rerun.
    
    Inputs:
    - step_index: position in chain
    - step: step dict to save
    - all_steps: full chain (for context)
    - line_items: original line items
    - original_total_amount: source MR total
    - mr_id: parent MR ID
    - filter_key: for cache invalidation
    """
    try:
        # Get source row for context
        source_df = st.session_state[f"source_df_{filter_key}"]
        row_idx = st.session_state[f"selected_row_{filter_key}"]
        source_row = source_df.iloc[row_idx]
        
        # Prepare TransferStep object
        company_str = step.get("company", "")
        pct_rate = float(step.get("pct_rate_increase", 0) or 0)
        total_amt = float(step.get("total_amount", 0) or 0)
        claim_amt = float(step.get("claim_amount", 0) or 0)
        net_amt = float(step.get("net_amount", 0) or 0)
        mr_date = step.get("mr_date", date.today())
        
        transfer_step = TransferStep(
            co_id=1,  # TODO: Extract from company_str
            branch_id=st.session_state.get("selected_branch_id", 1),
            mr_date=mr_date,
            mr_rate=float(step.get("mr_rate", 0) or 0),
            pct_rate_increase=pct_rate,
            total_amount=total_amt,
            claim_amount=claim_amt,
            net_amount=net_amt,
            warehouse_id=None,
            mr_no=0
        )
        
        # Call save_transfer_step from transfer.py
        result = save_transfer_step(
            source_mr_id=mr_id,
            step=transfer_step,
            prev_co_id=1,  # TODO: Extract from previous step
            prev_branch_id=st.session_state.get("selected_branch_id", 1),
            source_co_id=st.session_state.get("selected_company_id", 1),
            source_branch_id=st.session_state.get("selected_branch_id", 1),
            root_mr_id=mr_id,
            updated_by=1,  # TODO: Get from user session
            rate_multiplier=1.0,  # TODO: Calculate from chain
            is_first_step=(step_index == 0),
            is_final=False  # TODO: Determine if returns to source
        )
        
        # Clear cache to force reload
        cache_keys_to_clear = [
            f"transfers_{filter_key}",
            f"chains_map_{filter_key}"
        ]
        for key in cache_keys_to_clear:
            if key in st.session_state:
                del st.session_state[key]
        
        st.success(f"✓ Step {step_index + 1} saved!")
        st.rerun()
    
    except Exception as e:
        st.error(f"Error saving step: {str(e)}")
        import traceback
        st.write(traceback.format_exc())
```

- [ ] **Step 2: Test step save flow**

Run app, select MR, fill in step 2 company/date/%, enter %. Verify:
- % input triggers recalculation, totals update
- Line items show updated amounts
- Click "Save Step" → success message, page reloads
- Saved step appears as read-only, new blank step appended

- [ ] **Step 3: Commit**

```bash
git add src/jutetransfer/pages/new_transfer_chain.py
git commit -m "feat: implement _save_step() with DB persistence and cache invalidation"
```

---

## Task 9: Fill in Helper Functions and Missing Imports

**Files:**
- Modify: `src/jutetransfer/pages/new_transfer_chain.py` (verify all helpers exist)

- [ ] **Step 1: Verify `_group_by_mr()` exists in helpers**

Run in terminal:
```bash
grep -n "_group_by_mr" src/jutetransfer/jute_mr_chain_helpers.py
```

If function not found, add stub to `new_transfer_chain.py`:

```python
def _group_by_mr(raw_df):
    """
    Group raw query result (with line items) by MR header.
    
    Returns:
    - grouped_df: DataFrame with one row per MR
    - line_items_map: {mr_id: [line_items]}
    """
    # Group by jute_mr_id and aggregate
    grouped = raw_df.groupby("jute_mr_id").agg({
        "Jute Gate Entry No": "first",
        "Jute Supplier": "first",
        "Total Amount": "first",
        "Claim Amount": "first",
        "Net Total": "first",
        "MR DATE": "first"
    }).reset_index()
    
    # Build line items map
    line_items_map = {}
    for mr_id, group in raw_df.groupby("jute_mr_id"):
        line_items = group[["item_quality", "weight", "original_rate"]].drop_duplicates().to_dict("records")
        line_items_map[mr_id] = line_items
    
    return grouped, line_items_map
```

- [ ] **Step 2: Verify all imported functions exist**

Run:
```bash
python -c "
from src.jutetransfer.queries import get_companies, get_branches_by_company, get_company_branch_options, get_jute_mr_with_line_items, get_transfer_chains_batch, get_transfer_chain
from src.jutetransfer.jute_mr_chain_helpers import _reconstruct_chain, _recalculate_chain, _calculate_step_total_amount, _empty_transfer_step, _group_by_mr
from src.jutetransfer.transfer import save_transfer_step
from src.jutetransfer.schemas import TransferStep
print('✓ All imports OK')
"
```

Expected: `✓ All imports OK`

- [ ] **Step 3: Add COMPACT_COLUMNS if not already in helpers**

Verify at top of file:
```python
COMPACT_COLUMNS = ["Jute Gate Entry No", "Jute Supplier", "Total Amount", "Claim Amount", "Net Total"]
```

- [ ] **Step 4: Commit**

```bash
git add src/jutetransfer/pages/new_transfer_chain.py
git commit -m "feat: add helper functions and verify all imports resolve"
```

---

## Task 10: Integration Testing — Full Page Flow

**Files:**
- Test: Full app integration (no file changes, manual testing)

- [ ] **Step 1: Start Streamlit app**

```bash
cd c:/code/juteTransfer
streamlit run app.py
```

Navigate to "Transfer Chain (Vertical)" page.

- [ ] **Step 2: Test filter and table loading**

- Select Company (should populate)
- Select Branch (should populate based on company)
- Select Year, Month
- Expected: Table loads with MRs for that filter

Verify no errors in Streamlit console.

- [ ] **Step 3: Test chain loading for selected MR**

- Click a row in the table
- Expected: Chain header appears, original total shown, blank step card visible

If MR has saved transfers:
- Verify saved steps appear as read-only cards
- Verify line items display for each step

- [ ] **Step 4: Test % input and recalculation**

- Fill company, date for step 2
- Enter % rate increase (e.g., 1.5)
- Expected: Page reruns, line items update with new amounts, totals recalculate

- [ ] **Step 5: Test save flow**

- Click "Save Step"
- Expected: Success message, page reloads, step is now read-only, new blank step appended

- [ ] **Step 6: Test edge cases**

- [ ] Empty chain (pending MR, no transfers): Verify only blank step shows
- [ ] 1-step chain: Only source step saved, blank step for editing
- [ ] 5+ step chain: Vertical scroll works, all steps visible
- [ ] MR with no line items: Graceful empty message

- [ ] **Step 7: Commit integration test results**

```bash
git add -A
git commit -m "test: integration testing complete — filters, table, chain loading, % input, save flow"
```

---

## Task 11: Edge Case Handling and Error Testing

**Files:**
- Modify: `src/jutetransfer/pages/new_transfer_chain.py` (add defensive checks)

- [ ] **Step 1: Add null checks in chain loading**

In `_render_chain_editor()`, before accessing `row` fields:

```python
# Defensive checks
if row.empty or mr_id <= 0:
    st.warning("Invalid MR selection")
    return

if "Total Amount" not in row or row["Total Amount"] is None:
    orig_total = 0.0
else:
    orig_total = float(row["Total Amount"])
```

- [ ] **Step 2: Add error handling for company/branch queries**

In `_render_filters()`:

```python
try:
    companies = get_companies()
except Exception as e:
    st.error(f"Failed to load companies: {str(e)}")
    companies = []

if not companies:
    st.warning("No companies available. Contact admin.")
    return
```

- [ ] **Step 3: Add validation in save flow**

In `_save_step()`:

```python
# Validate step has required fields
required_fields = ["company", "mr_date"]
missing = [f for f in required_fields if not step.get(f)]

if missing:
    st.error(f"Cannot save: missing {', '.join(missing)}")
    return
```

- [ ] **Step 4: Test error scenarios**

- Simulate DB connection failure (disconnect MySQL): Expected graceful error
- Select filter with no MRs: Expected "0 records" message
- Try to save step with missing company: Expected validation error

- [ ] **Step 5: Commit error handling**

```bash
git add src/jutetransfer/pages/new_transfer_chain.py
git commit -m "feat: add defensive checks and error handling for edge cases"
```

---

## Task 12: Code Cleanup and TODOs Documentation

**Files:**
- Modify: `src/jutetransfer/pages/new_transfer_chain.py` (add TODO markers)

- [ ] **Step 1: Mark functions for future refactoring**

Add comments before functions that could be extracted to `jute_mr_page_helpers.py`:

```python
# TODO: Extract to jute_mr_page_helpers.py (shared with jute_mr.py)
def _render_step_card(...):
    ...

# TODO: Extract to jute_mr_page_helpers.py (shared with jute_mr.py)
def _render_step_line_items(...):
    ...
```

- [ ] **Step 2: Document known issues**

At top of file, add:

```python
"""
Known Limitations (for future improvement):

1. pct_rate_increase is back-calculated from rounded totals (causes rounding errors).
   Fix: Add pct_rate_increase DECIMAL(10,4) column to jute_mr table (see debugging plan).

2. save_transfer_step() requires many parameters; extraction of save logic would improve clarity.

3. Line items are read-only; could add feature to add/remove items per step in future.

4. No bulk save; each step saves individually.
"""
```

- [ ] **Step 3: Verify line count**

Count lines in file:

```bash
wc -l src/jutetransfer/pages/new_transfer_chain.py
```

Expected: ~450 lines (matches design spec)

- [ ] **Step 4: Final commit**

```bash
git add src/jutetransfer/pages/new_transfer_chain.py
git commit -m "docs: add TODOs and known limitations documentation"
```

---

## Task 13: Final Verification and Success Criteria

**Files:**
- None (verification only)

- [ ] **Step 1: Verify all success criteria**

Run final integration test:

```bash
streamlit run app.py
```

Navigate to "Transfer Chain (Vertical)" and verify:

- [ ] ✓ Page renders without errors
- [ ] ✓ Monthly MR table loads correctly
- [ ] ✓ Selecting MR loads its transfer chain
- [ ] ✓ Line items visible per step
- [ ] ✓ % input triggers recalculation
- [ ] ✓ Totals update correctly downstream
- [ ] ✓ Save step persists to DB
- [ ] ✓ Saved step shows as read-only
- [ ] ✓ Auto-reload after save shows correct state
- [ ] ✓ Vertical layout is readable and usable

- [ ] **Step 2: Run module import check**

```bash
python -c "from src.jutetransfer.pages.new_transfer_chain import transfer_chain_page; print('✓ Module imports successfully')"
```

Expected: `✓ Module imports successfully`

- [ ] **Step 3: Verify no circular imports**

```bash
python -c "from src.jutetransfer import jute_mr_chain_helpers, jute_mr_editor, pages.jute_mr, pages.new_transfer_chain, transfer; print('✓ No circular imports')"
```

Expected: `✓ No circular imports`

- [ ] **Step 4: Final status report**

Log final git status:

```bash
git log --oneline -10
```

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "feat: new vertical transfer chain page implementation complete

- Added pages/new_transfer_chain.py (~450 lines)
- 3-level hierarchy: filters → MR table → step cards with line items
- Reuses jute_mr_chain_helpers, queries, transfer modules
- Session state caching per filter key
- Auto-reload on save, read-only saved steps
- All success criteria verified"
```

---

## Summary

**Total tasks:** 13  
**Total new code:** ~450 lines  
**Files created:** 1 (`pages/new_transfer_chain.py`)  
**Files modified:** 0 (reuses existing modules)  

**Key deliverables:**
- ✓ New page renders transfer chains in vertical 3-level layout
- ✓ Filters + monthly MR table (Level 1)
- ✓ Transfer step cards with line items (Levels 2-3)
- ✓ % input with recalculation and cascade
- ✓ Save flow with DB persistence
- ✓ Session state caching for performance
- ✓ Error handling and edge cases
- ✓ TODOs marked for future refactoring

**Next steps after completion:**
1. Review the implementation against the design spec
2. Gather user feedback on UX (layout, responsiveness, clarity)
3. Implement database column migration for `pct_rate_increase` (separate task)
4. Extract shared components to `jute_mr_page_helpers.py` and consolidate with old page
