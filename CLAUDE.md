# JuteTransfer Development Guide

## Project Overview

**JuteTransfer** is a Streamlit-based application for managing Material Receipt (MR) records in jute trading. It replaces an Excel-based workflow with a structured digital system that tracks circular transfer chains across companies.

**Tech Stack:**
- Frontend: Streamlit + streamlit-aggrid
- Backend: Python 3.12+ with SQLAlchemy ORM
- Database: MySQL
- Auth: Custom authentication layer in `src.jutetransfer.auth`

## Core Business Logic

### Transfer Chain Pattern

The fundamental pattern is a **circular transfer chain**:
- Gate entry occurs at Company A (creates MR in "pending" state)
- First purchaser is Company B → B sells to C → C to D → ... → jute returns to Company A
- User **manually finalizes** the MR when it returns to Company A (not auto-detected)
- Each transfer step has rates that cascade downward (compound effect)

**Key requirement:** The vertical transfer chain page is the single UI surface for viewing and editing an MR's chain from root through final return.

## Architecture

### Module Organization

```
src/jutetransfer/
├── pages/
│   ├── new_transfer_chain.py         # Vertical transfer chain page (sole editing UI)
│   ├── company_pl_dashboard.py       # Company P&L dashboard page
│   ├── schema_viewer.py              # Schema browser page
│   └── __init__.py
├── jute_mr_chain_helpers.py          # Pure Python chain logic
├── transfer.py                       # Transfer data operations & finalization
├── queries.py                        # Database queries (get_companies, get_jute_mr_with_line_items, etc)
├── models.py                         # SQLAlchemy ORM models
├── database.py                       # DB connection & session management
├── config.py                         # Configuration (env vars)
├── auth.py                           # Authentication helpers
├── schemas.py                        # Pydantic schemas
├── data.py                           # Data utilities
└── __init__.py

app.py                                # Streamlit entry point
```

### Critical Dependencies

**jute_mr_chain_helpers.py** → **No circular imports allowed**
- This is the pure Python core; imports nothing from pages
- Contains: grouping, chain reconstruction, status checks, rate recalculation math
- Used by: `transfer.py`, `pages/new_transfer_chain.py`

**transfer.py** → **Public API for transfer operations**
- Imports: `jute_mr_chain_helpers`, `queries`, `database`
- Used by: `pages/new_transfer_chain.py` for save/delete/finalize operations

**pages/new_transfer_chain.py** → **Transfer Chain (Vertical) page**
- Imports: `jute_mr_chain_helpers`, `transfer`, `queries`
- Renders the vertical 3-level chain editor used for all transfer editing

## Key Implementation Patterns

### 1. The % Rate Increase Widget Bug (SOLVED)

**Historical Context:** Users couldn't enter % rate increases—the input appeared broken.

**Root Causes:**
1. `nonlocal` doesn't cross Streamlit reruns (callbacks execute in different frames)
2. `value=current_pct` parameter reset widget state every rerun
3. Closures captured stale loop variables

**Current Solution (in `pages/new_transfer_chain.py`):**
```python
# Initialize widget state if missing
if pct_key not in st.session_state:
    st.session_state[pct_key] = current_pct

# Let Streamlit manage widget, don't pass value= param
new_pct = float(st.session_state[pct_key])

# Trigger recalculation if changed
if new_pct != current_pct:
    changed = True
```

**Lesson:** Streamlit's session state is the source of truth; never override it with `value=` parameters when you need persistent input state.

### 2. Chain Recalculation

**jute_mr_chain_helpers._recalculate_chain()** is the math engine:
- Takes a step with modified rate/factor
- Propagates changes downward through all subsequent steps
- Respects claim propagation rules (don't cascade past claim breaks)
- Returns updated chain with all dependent steps recalculated

**Used by:** the vertical transfer chain page when the user modifies % rate increase or other step properties.

### 3. Session State Management

Use `st.session_state` for:
- Widget values that persist across reruns
- Cached data that should survive a single user action
- Intermediate state during complex workflows

Do NOT use:
- `nonlocal` for widget callbacks
- Closure captures for loop variables
- Module-level globals

## Testing Strategy

### Current Status

- [x] Module imports (no circular dependencies)
- [ ] Integration: App start, page loads
- [ ] Widget behavior: % Rate Increase form flow
- [ ] Chain propagation: Rate changes cascade correctly
- [ ] Persistence: Save/reload preserves changes

### How to Run Tests

```bash
# Module import validation
python -c "from src.jutetransfer import jute_mr_chain_helpers, transfer; from src.jutetransfer.pages import new_transfer_chain, schema_viewer, company_pl_dashboard; print('✓ All imports OK')"

# Full app (requires MySQL, .env with credentials)
streamlit run app.py
```

### Integration Test Checklist

When modifying chain logic or the transfer editor:

1. **Rate Cascade**
   - Enter 10% in Step 2 → Verify Step 3's rate updates
   - Enter -5% → Verify decrease propagates

2. **Save/Reload**
   - Save a modified step
   - Reload the page
   - Verify changes persisted and display is correct

3. **Edge Cases**
   - Empty chains (pending MR, no transfers yet)
   - Single-step chains
   - Chains with claim breaks (should stop cascading at that point)

## Development Workflow

### Before Committing

1. **Verify imports are correct**
   - No circular dependencies between modules
   - Chain helpers imports only stdlib/third-party, never pages/editor

2. **Test the affected feature**
   - If modifying chain logic: test rate cascade
   - If modifying editor UI: test widget state persistence
   - If modifying queries: verify data structure unchanged

3. **Keep modules focused**
   - jute_mr_chain_helpers: pure Python chain math
   - pages/new_transfer_chain: Streamlit UI rendering
   - transfer.py: data operations
   - queries.py: database reads

### File Size Targets

- **pages/new_transfer_chain.py**: Keep focused; extract helpers if it grows past ~700 lines
- **jute_mr_chain_helpers.py**: Keep under 350 lines

If a module exceeds these targets, consider extracting helper functions into separate files (e.g., `jute_mr_calculations.py`).

## Common Tasks

### Adding a New Transfer Step Property

1. Update `_empty_transfer_step()` in `jute_mr_chain_helpers.py`
2. Add field to the editor input section in `pages/new_transfer_chain.py`
3. Update `_recalculate_chain()` logic if the property affects downstream calculations
4. Update `transfer.save_transfer_step()` to persist the new field
5. Test: Verify new field displays, saves, and cascades (if applicable)

### Fixing a Widget State Bug

- Check if you're using `value=` parameter (usually the culprit)
- Use `st.session_state` for initialization instead
- Read value from `st.session_state` after widget renders
- Do NOT use `nonlocal` in callbacks

### Adding a New Database Query

1. Write function in `queries.py` (keep it simple, single SELECT/JOIN)
2. Import in the page or editor that needs it
3. Test: Verify query returns expected shape and handles edge cases

## References

- **Business Logic Detail**: See memory file `project_jute_transfer.md`
- **Refactoring History**: See memory file `refactoring_session.md`
- **% Rate Increase Deep Dive**: See memory file `rate_increase_execution_flow.md`
- **Widget State Fix**: See memory file `rate_increase_enter_key_fix.md`

---

**Last Updated:** 2026-04-18  
**Maintained By:** Development Team  
**Key Constraint:** Circular transfer chains must be preserved; the vertical transfer chain page is the sole editing UI
