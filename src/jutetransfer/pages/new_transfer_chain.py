"""
New Vertical Transfer Chain Editor Page
Displays transfer chains in a 3-level hierarchy (filters → MR table → step cards with line items).
"""

import logging
from datetime import datetime, date
import pandas as pd
import streamlit as st

from .. import po_queries
from ..po_helpers import ROLE_FORWARD, format_po_no, parse_marker
from ..po_ops import transfer_po_enabled
from ..queries import (
    get_companies,
    get_branches_by_company,
    get_company_branch_options,
    get_jute_mr_with_line_items,
    get_original_po,
    get_transfer_chain,
    get_warehouses_by_branch,
    get_invoice_details_by_mr_id,
)
from ..jute_mr_chain_helpers import (
    _group_by_mr,
    _reconstruct_chain,
    _recalculate_chain,
    _empty_transfer_step,
    _cascade_rate,
    _calculate_line_item_amount,
    _stored_amount,
    is_root_eligible_for_new_chain,
)
from ..transfer import save_transfer_step, delete_chain_from_step, TransferStep

FLASH_KEY = "chain_flash"
# Every '% Rate Increase' box is a widget keyed 'pct_input_<root mr>_<step>'.
# (The plain 'pct_<root>_<step>' shadow keys the box used to be seeded from
# are gone: a shadow that outlived its step dict made the box show one %
# while the save posted another. A box is now only ever seeded from, and
# compared with, its step dict.)
PCT_PREFIXES = ("pct_input_", "pct_")

_log = logging.getLogger("jutetransfer.pages.transfer_chain")


def _flash(kind: str, message: str) -> None:
    """Queue a message for the next run: st.success() followed by st.rerun()
    is never seen, so save / delete results are shown from here instead."""
    st.session_state[FLASH_KEY] = (kind, message)


def _render_flash() -> None:
    flash = st.session_state.pop(FLASH_KEY, None)
    if not flash:
        return
    kind, message = flash
    {"success": st.success, "warning": st.warning}.get(kind, st.info)(message)
    # Also a toast: on a phone the page keeps its scroll position, so the
    # banner at the top is out of view after a save or delete lower down.
    # 'long', because the month reloads for 10-20 s right after a save.
    st.toast(message, icon="✅" if kind == "success" else "⚠️", duration="long")


def _drop_chain_state(filter_key: str, mr_id: int, keep_selection: bool = True) -> None:
    """Forget what is cached for this month (the rows and every chain are read
    again) and this lorry's editing state after a save or delete.

    The other lorries' typed steps are kept: a save here does not change
    them, and the editor rebuilds a kept chain by itself when the database
    shows it changed (_chain_changed). Every '% Rate Increase' box is
    forgotten: a box is re-seeded from its step dict, never the other way
    round, so it can never show one % while the next save posts another."""
    keys = [
        f"raw_df_{filter_key}",
        f"source_df_{filter_key}",
        f"line_items_{filter_key}",
        f"chains_map_{filter_key}",
        f"step_line_items_{filter_key}_{mr_id}",
        f"orig_po_{filter_key}",
    ]
    if not keep_selection:
        keys.append(f"selected_row_{filter_key}")
    keys += [k for k in st.session_state if isinstance(k, str) and k.startswith(PCT_PREFIXES)]
    # This lorry's own step widgets go too: its cards are rebuilt from the
    # database, and what was typed on the saved step is in the database now.
    keys += _lorry_widget_keys(mr_id)
    for key in keys:
        st.session_state.pop(key, None)
    transfers = st.session_state.get(f"transfers_{filter_key}")
    if isinstance(transfers, dict):
        transfers.pop(mr_id, None)


def _saved_ids(steps) -> set:
    """The MR ids of the saved steps of an in-memory chain (the root's own id
    stands for the finalized return step)."""
    return {int(s["saved_mr_id"]) for s in steps or [] if s.get("saved_mr_id")}


def _chain_changed(steps, chain_data, is_finalized: bool, mr_id: int) -> bool:
    """True when the chain kept in session state no longer matches the chain
    the database holds (a step saved or deleted elsewhere, a finalize or an
    un-finalize): the kept steps -- and anything typed on them -- are then
    stale and must be rebuilt from the database."""
    in_db = set()
    if chain_data is not None and not chain_data.empty:
        in_db = {int(x) for x in chain_data["jute_mr_id"]}
    if is_finalized:
        in_db.add(int(mr_id))
    return _saved_ids(steps) != in_db


STEP_WIDGETS = ("company", "date", "transport", "lc_ref", "lc_date", "po_lc", "od_lc",
                "pct_input", "pct", "confirm_delete")


def _step_widget_keys(mr_id: int, step_index: int) -> list:
    """The session-state keys of every widget on one step card."""
    exact = [f"{name}_{mr_id}_{step_index}" for name in STEP_WIDGETS]
    prefix = f"wh_{mr_id}_{step_index}_"            # keyed by branch as well
    return [k for k in st.session_state if isinstance(k, str)
            and (k in exact or k.startswith(prefix))]


def _lorry_widget_keys(mr_id: int) -> list:
    """The session-state keys of the step widgets of every card of one lorry."""
    prefixes = tuple(f"{name}_{mr_id}_" for name in STEP_WIDGETS + ("wh",))
    return [k for k in st.session_state if isinstance(k, str) and k.startswith(prefixes)]


def _source_mr_date(source_row):
    """The selected MR's date (the default date of a new step); today when
    the row has none."""
    raw = None
    if source_row is not None and "MR DATE" in getattr(source_row, "index", []):
        raw = source_row.get("MR DATE")
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return date.today()
    return raw


def _mr_label(step: dict) -> str:
    """'MR 50 (EJM-FACTORY)' for a saved step; the company alone when the
    number is not known."""
    mr_no = step.get("mr_no")
    company = step.get("company") or "?"
    if mr_no is None or (isinstance(mr_no, float) and pd.isna(mr_no)) or mr_no == "":
        return company
    try:
        mr_no = int(mr_no)
    except (TypeError, ValueError):
        pass
    return f"MR {mr_no} ({company})"


def _delete_confirm_label(step_index: int, step: dict, all_steps: list) -> str:
    """The tick-box text beside 'Delete from Step N onward': it names every
    MR the tap will delete and what else goes with it, so the owner confirms
    a thing, not a button."""
    if step.get("is_final_return"):
        return (f"Yes, un-finalize: remove the mill's {_mr_label(step)}, its invoice and "
                "the Final PO — the mill's MR goes back to Pending")
    doomed = [s for s in all_steps[step_index:] if s.get("saved_mr_id")]
    hops = [s for s in doomed if not s.get("is_final_return")]
    names = ", ".join(_mr_label(s) for s in hops)
    steps_text = (f"step {step_index + 1}" if len(doomed) == 1
                  else f"steps {step_index + 1}–{step_index + len(doomed)}")
    text = f"Yes, delete {steps_text}: {names}"
    if step_index == 0 and len(hops) == 1:
        text += " and its transfer PO"
    else:
        text += (", with the invoices and transfer POs" if len(hops) > 1
                 else ", with its invoice and transfer PO")
    if any(s.get("is_final_return") for s in doomed):
        text += "; the mill's MR goes back to Pending (invoice and Final PO removed)"
    return text


def _delete_result_message(summary: dict, step_index: int, step: dict) -> tuple:
    """(kind, text) of the flash after delete_chain_from_step, from what it
    reports as deleted -- never from what the page meant to delete."""
    removed_pos = summary.get("deleted_pos") or []
    po_text = ""
    if removed_pos:
        po_text = ("; transfer PO" + ("s " if len(removed_pos) > 1 else " ")
                   + ", ".join(removed_pos) + " removed")
    deleted = summary.get("deleted_mr_ids") or []
    if not deleted and not summary.get("reverted"):
        return ("warning", "Nothing was deleted: the chain had already changed "
                           "(another session?). The page now shows it as it is.")
    if step.get("is_final_return"):
        return ("success", "Un-finalized — the mill's MR is back to Pending and the "
                           f"invoice is removed{po_text}.")
    n = len(deleted)
    if step_index == 0:
        # The first hop carries no invoice: the mill sells nothing, it hands
        # the lorry over (the invoices start with the second hop).
        what = "MR removed" if n == 1 else f"{n} MRs removed with the invoices of the later steps"
    else:
        what = "MR and invoice removed" if n == 1 else f"{n} MRs and their invoices removed"
    msg = f"Deleted from step {step_index + 1} — {what}"
    if summary.get("reverted"):
        msg += ", the mill's MR is back to Pending"
    return ("success", msg + po_text + ".")


_SKIP_TEXT = {
    "switched off": "transfer PO creation is switched off",
    "no line with accepted weight": "the lorry has no accepted weight",
    "already has": "it already has one",
    "not finalized": "the chain is not finalized",
}


def _skip_text(reason: str) -> str:
    """A PO skip reason in the owner's words."""
    for needle, text in _SKIP_TEXT.items():
        if needle in (reason or ""):
            return text
    return reason or "no reason given"


@st.cache_data(ttl=60, show_spinner=False)
def _company_branch_map() -> dict:
    """{'PREFIX-Branch': (co_id, branch_id)} for the save captions, cached a
    minute so a caption does not cost two more queries on every tap."""
    return get_company_branch_options()[1]


def _clear_tracker_cache() -> None:
    """The PO Tracker caches its read for a minute; drop it after a save or
    delete so the new PO shows there straight away."""
    clear = getattr(po_queries, "clear_tracker_cache", None)
    if clear:
        clear()


def _stored_or_zero(value) -> float:
    """A stored money figure as a float; 0.0 for None / NaN."""
    amount = _stored_amount(value)
    return 0.0 if amount is None else amount


def _header_amount(row, key: str, fallback_key: str = None):
    """A money column of the selected MR row as a float: the header column
    `key` when the row has it, else `fallback_key` (the line-derived figure),
    else None."""
    for name in (key, fallback_key):
        if name is None or name not in row.index:
            continue
        value = row.get(name)
        if value is None or (isinstance(value, float) and pd.isna(value)):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _fmt_date(value) -> str:
    """dd-Mon-yyyy for a date / Timestamp; '' when missing."""
    if value is None or (isinstance(value, float) and pd.isna(value)) or value is pd.NaT:
        return ""
    try:
        return pd.Timestamp(value).strftime("%d-%b-%Y")
    except (ValueError, TypeError):
        return str(value)


def _po_line(label: str, po_no: str, po_date, weight, value) -> str:
    """'**Forwarding PO:** JTSPL/JPO/26-27/00021 · 31-Aug-2026 · 10,800 kg · ₹13,84,800'"""
    parts = [po_no]
    if _fmt_date(po_date):
        parts.append(_fmt_date(po_date))
    if weight is not None and not pd.isna(weight):
        parts.append(f"{float(weight):,.0f} kg")
    if value is not None and not pd.isna(value):
        parts.append(f"₹{float(value):,.0f}")
    return f"**{label}:** " + " · ".join(parts)


def _step_po_info(sc: dict) -> dict:
    """What a saved chain step's card says about its transfer PO, from the
    chain row as stored (never from the result of the save, so it is right
    after a rerun and for steps saved in another session)."""
    po_id = sc.get("transfer_po_id")
    if po_id is None or pd.isna(po_id):
        return {"po_state": "none"}
    po_no = format_po_no(sc.get("transfer_po_no"), sc.get("co_prefix"),
                         sc.get("branch_prefix"),
                         None if pd.isna(sc.get("transfer_po_date")) else sc.get("transfer_po_date"))
    marker = parse_marker(sc.get("transfer_po_note"))
    ours = bool(marker and marker["role"] == ROLE_FORWARD
                and marker["mr_id"] == int(sc["jute_mr_id"]))
    return {
        "po_state": "transfer" if ours else "erp",
        "po_no": po_no or f"PO #{int(po_id)}",
        "po_date": sc.get("transfer_po_date"),
        "po_weight": sc.get("transfer_po_weight"),
        "po_value": sc.get("transfer_po_value"),
    }

# Known Limitations:
# 1. pct_rate_increase is back-calculated from rounded totals (rounding errors possible)
#    Fix: Add pct_rate_increase DECIMAL(10,4) column to jute_mr table
# 2. Each step saves individually (no bulk save)
# 3. Saved steps are read-only (no unlock/edit feature)
# 4. Line items are non-editable (by design: transfers preserve original items)

# Constants
COMPACT_COLUMNS = ["Jute Gate Entry No", "Jute Gate Entry Date", "Jute Supplier", "Party Name", "Status", "Total Amount", "Claim Amount", "Net Total"]


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
    - Every saved step also gets a purchase order: a Forwarding PO at the
      forwarding company, and a Final PO at the mill when the chain returns
      (PO rate rounded to the nearest ₹50) — see the PO Tracker page
    """)

    # Result of the last save / delete (queued before st.rerun()).
    _render_flash()
    if not transfer_po_enabled():
        st.warning(
            "Transfer PO creation is switched off — steps saved now will have no PO."
        )

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


def _render_filters():
    """
    Render dropdown filters for company, branch, year, month.

    Populates session state keys:
    - selected_company_id
    - selected_branch_id
    - selected_year
    - selected_month
    """
    current_year = datetime.now().year
    current_month = datetime.now().month

    # Month name mapping for display
    month_names = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ]

    # Row 1: Company + Branch
    col1, col2 = st.columns(2)

    # Fetch companies for dropdown
    try:
        company_options = get_companies()
    except Exception as e:
        st.error(f"Failed to load companies: {str(e)}")
        company_options = {}

    # persist_state: the four filters survive a visit to another page (the
    # PO Tracker, say) instead of falling back to the first company and the
    # current month -- each fallback cost a 10-20 s reload of the month.
    with col1:
        selected_company_name = st.selectbox(
            "Select Company",
            options=list(company_options.keys()),
            index=0 if company_options else None,
            key="company_select",
            persist_state="session",
        )
        selected_company_id = (
            company_options.get(selected_company_name)
            if selected_company_name
            else None
        )

    # Store in session state
    st.session_state["selected_company_id"] = selected_company_id

    # Get branches for selected company
    try:
        branch_options = (
            get_branches_by_company(selected_company_id)
            if selected_company_id
            else {}
        )
    except Exception as e:
        st.error(f"Failed to load branches: {str(e)}")
        branch_options = {}

    with col2:
        selected_branch_name = st.selectbox(
            "Select Branch",
            options=list(branch_options.keys()),
            index=0 if branch_options else None,
            key="branch_select",
            persist_state="session",
        )
        selected_branch_id = (
            branch_options.get(selected_branch_name)
            if selected_branch_name
            else None
        )

    # Store in session state
    st.session_state["selected_branch_id"] = selected_branch_id

    # Row 2: Year + Month
    col3, col4 = st.columns(2)

    with col3:
        selected_year = st.selectbox(
            "Select Year",
            options=list(range(current_year, current_year - 10, -1)),
            index=0,
            key="year_select",
            persist_state="session",
        )

    # Store in session state
    st.session_state["selected_year"] = selected_year

    with col4:
        selected_month = st.selectbox(
            "Select Month",
            options=list(range(1, 13)),
            format_func=lambda x: month_names[x - 1],
            index=current_month - 1,
            key="month_select",
            persist_state="session",
        )

    # Store in session state
    st.session_state["selected_month"] = selected_month


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
    head_col, reload_col = st.columns([4, 1])
    with head_col:
        st.subheader("Monthly MR Overview")
    with reload_col:
        if st.button("Reload", key=f"reload_{filter_key}",
                     help="Read this month again from the database"):
            # Data caches only: the selected row and every widget stay --
            # company, date, LC fields AND the typed % (the box re-seeds its
            # fresh step dict on the next run), so the card the owner was
            # filling in looks the same after the reload as before it.
            data = (f"raw_df_{filter_key}", f"source_df_{filter_key}",
                    f"line_items_{filter_key}", f"transfers_{filter_key}",
                    f"chains_map_{filter_key}", f"orig_po_{filter_key}")
            for key in [k for k in st.session_state if isinstance(k, str)
                        and (k in data or k.startswith(f"step_line_items_{filter_key}_"))]:
                st.session_state.pop(key, None)
            _clear_tracker_cache()
            st.rerun()

    # Load data if not cached
    raw_df_key = f"raw_df_{filter_key}"
    if raw_df_key not in st.session_state:
        # Whatever the read below leaves behind, the editor must not find a
        # half-loaded month: the grouped rows are the last thing written.
        st.session_state.pop(f"source_df_{filter_key}", None)
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

            # Batch-load all chains
            all_mr_ids = grouped_df["jute_mr_id"].astype(int).tolist()
            chains_dict = {}
            for mr_id in all_mr_ids:
                chain_data = get_transfer_chain(mr_id)
                if chain_data is not None:
                    chains_dict[mr_id] = chain_data

            # Cache all data -- only once every read succeeded, so a failure
            # half-way never leaves rows without their chains.
            st.session_state[raw_df_key] = raw_df
            st.session_state[f"line_items_{filter_key}"] = line_items_map
            st.session_state[f"chains_map_{filter_key}"] = chains_dict
            st.session_state[f"source_df_{filter_key}"] = grouped_df

        except Exception as e:
            _log.exception("Loading the month failed (%s)", filter_key)
            st.error(f"Error loading MRs: {str(e)} — tap Reload to try again.")
            return

    # Get cached data
    grouped_df = st.session_state[f"source_df_{filter_key}"]

    # Display table with row selection
    if grouped_df.empty:
        st.info("No MR records to display")
        return

    st.write(f"**{len(grouped_df)} records found**")

    visible_cols = [c for c in COMPACT_COLUMNS if c in grouped_df.columns]
    event = st.dataframe(
        grouped_df[visible_cols] if visible_cols else grouped_df,
        use_container_width=True,
        on_select="rerun",
        selection_mode="single-row"
    )

    # Store selected row index — guard against missing .selection or .rows attribute
    if not hasattr(event, "selection") or not hasattr(event.selection, "rows"):
        return

    if event.selection.rows:
        st.session_state[f"selected_row_{filter_key}"] = event.selection.rows[0]
    else:
        # Clear selection if user deselects
        if f"selected_row_{filter_key}" in st.session_state:
            del st.session_state[f"selected_row_{filter_key}"]


def _fetch_step_line_items(jute_mr_id: int) -> list:
    """Fetch line items for a specific MR, joined with item_mst for quality names."""
    from ..database import DatabaseConnection
    df = DatabaseConnection.execute_query(
        """
        SELECT li.accepted_weight, li.rate, li.claim_rate, li.warehouse_id,
               COALESCE(im.item_name, CONCAT('Item-', li.actual_item_id)) AS item_quality
        FROM jute_mr_li li
        LEFT JOIN item_mst im ON li.actual_item_id = im.item_id
        WHERE li.jute_mr_id = :mr_id
          AND (li.active = 1 OR li.active IS NULL)
        """,
        {"mr_id": jute_mr_id},
    )
    if df is None or df.empty:
        return []
    return [
        {
            "weight": float(row.get("accepted_weight", 0) or 0),
            "original_rate": float(row.get("rate", 0) or 0),
            "original_claim": float(row.get("claim_rate", 0) or 0),
            "item_quality": row.get("item_quality", "Item"),
            "warehouse_id": row.get("warehouse_id"),
        }
        for _, row in df.iterrows()
    ]


def _render_chain_editor(filter_key):
    """
    Load transfer chain for selected MR, reconstruct order, render step cards.

    Session state keys:
    - transfers_{filter_key} — {mr_id: [steps]} for editing
    """
    selected_row_key = f"selected_row_{filter_key}"
    if selected_row_key not in st.session_state:
        return  # No row selected
    if f"source_df_{filter_key}" not in st.session_state:
        return  # the month did not load (error shown above); nothing to edit

    row_idx = st.session_state[selected_row_key]
    grouped_df = st.session_state[f"source_df_{filter_key}"]

    if row_idx >= len(grouped_df):
        return

    row = grouped_df.iloc[row_idx]
    mr_id = int(row["jute_mr_id"])
    line_items_map = st.session_state[f"line_items_{filter_key}"]
    chains_map = st.session_state[f"chains_map_{filter_key}"]

    # Safely read Total Amount (could be None/NaN)
    raw_total = row.get("Total Amount")
    if raw_total is None or (isinstance(raw_total, float) and pd.isna(raw_total)):
        raw_total = 0.0
    else:
        try:
            raw_total = float(raw_total)
        except (ValueError, TypeError):
            raw_total = 0.0

    # Safely read MR DATE
    raw_mr_date = row.get("MR DATE") if "MR DATE" in row.index else None
    if raw_mr_date is None or (isinstance(raw_mr_date, float) and pd.isna(raw_mr_date)):
        raw_mr_date = date.today()

    # Initialize transfers session state if needed
    transfers_key = f"transfers_{filter_key}"
    if transfers_key not in st.session_state:
        st.session_state[transfers_key] = {}

    transfers = st.session_state[transfers_key]

    # Detect finalization (chain completed back to origin). When finalized, the
    # return-to-origin step is the in-place updated source MR — there is no
    # separate jute_mr row for it, so we synthesize one. Always initialize this
    # so downstream code can rely on it.
    # NOTE: `branch_mr_no` is an Integer column; pandas surfaces DB NULL as
    # float('nan'), and bool(NaN) is True in Python — hence the pd.notna guard.
    _raw_ejm = row.get("EJM MR No.")
    is_finalized = pd.notna(_raw_ejm) and bool(_raw_ejm)

    # The saved chain as the database holds it (the month was just read).
    chain_data = chains_map.get(mr_id)
    step_line_items_key = f"step_line_items_{filter_key}_{mr_id}"

    # A chain kept from an earlier run (typed steps included) is only reused
    # while the database still shows the same saved steps; otherwise it is
    # rebuilt -- another session's save, or a delete, must never be edited
    # over.
    if mr_id in transfers and _chain_changed(transfers[mr_id], chain_data, is_finalized, mr_id):
        transfers.pop(mr_id, None)
        st.session_state.pop(step_line_items_key, None)

    # First time loading this MR: initialize from DB chain
    if mr_id not in transfers:
        # Decision D1 (2026-09-03): a NEW chain may only start from an
        # ERP root at status 13 (Pending). Existing chains (root already
        # has children) keep working regardless of root status. (Checked
        # before anything is kept for the MR, so the warning shows on every
        # run, not only the first.)
        is_new_chain = chain_data is None or chain_data.empty
        if is_new_chain and not is_finalized:
            root_status = row.get("status_id_raw")
            if not is_root_eligible_for_new_chain(root_status):
                st.warning(
                    f"This MR is '{row.get('Status')}' in the ERP. Only MRs set to "
                    "Pending (Transfer) can start a new chain; Approved rows are shown "
                    "for reference."
                )
                return

        transfers[mr_id] = []

        if chain_data is not None and not chain_data.empty:
            try:
                chain_mrs = chain_data.to_dict("records") if hasattr(chain_data, 'to_dict') else chain_data
                saved_chain = _reconstruct_chain(chain_mrs, st.session_state["selected_company_id"])

                # If finalized, append a synthetic return-to-origin step built
                # from the (in-place updated) source MR's row. saved_mr_id is
                # set to the root mr_id so delete_chain_from_step's
                # "from_mr_id == root_mr_id" branch handles unfinalize cleanly.
                if is_finalized:
                    _, _co_branch_mapping = get_company_branch_options()
                    sel_co = st.session_state.get("selected_company_id")
                    sel_br = st.session_state.get("selected_branch_id")
                    src_label = next(
                        (lbl for lbl, (cid, bid) in _co_branch_mapping.items()
                         if cid == sel_co and bid == sel_br),
                        "",
                    )
                    if src_label and "-" in src_label:
                        _src_co_prefix, _src_branch_name = src_label.split("-", 1)
                    else:
                        _src_co_prefix, _src_branch_name = src_label, ""
                    # Money as the ERP stores it on the root (finalize writes
                    # total / claim / 194Q TDS / roundoff / net the ERP way);
                    # the row's line-derived figures only when the header
                    # has none.
                    saved_chain.append({
                        "jute_mr_id": mr_id,
                        "co_prefix": _src_co_prefix,
                        "branch_name": _src_branch_name,
                        "branch_mr_no": row.get("EJM MR No."),
                        "jute_mr_date": row.get("MR DATE"),
                        "challan_date": row.get("Challan Date"),
                        "total_amount": _header_amount(row, "mr_total_amount", "Total Amount"),
                        "claim_amount": _header_amount(row, "mr_claim_amount", "Claim Amount"),
                        "tds_amount": _header_amount(row, "mr_tds_amount"),
                        "roundoff": _header_amount(row, "mr_roundoff"),
                        "net_total": _header_amount(row, "mr_net_total", "Net Total"),
                        "invoice_amount": _header_amount(row, "mr_invoice_amount"),
                        "is_final_return": True,
                    })

                # Populate steps from saved chain. Seed prev_total from Step 1
                # (saved_chain[0]) when present — Step 1 is a frozen snapshot
                # unaffected by finalization, so it's the true Source amount.
                if saved_chain and not saved_chain[0].get("is_final_return"):
                    prev_total = float(saved_chain[0].get("total_amount") or 0)
                else:
                    prev_total = raw_total
                for sc in saved_chain:
                    step = _empty_transfer_step()
                    step["company"] = f"{sc.get('co_prefix', 'N/A')}-{sc.get('branch_name', 'N/A')}"
                    step["mr_no"] = sc.get("branch_mr_no", "")
                    step["mr_date"] = sc.get("jute_mr_date", date.today())
                    # Stored header money; NaN / None are left to
                    # _recalculate_chain, which then computes the figure.
                    step["total_amount"] = float(sc.get("total_amount") or 0)
                    step["claim_amount"] = sc.get("claim_amount")
                    step["net_amount"] = sc.get("net_total")
                    step["tds_amount"] = _stored_or_zero(sc.get("tds_amount"))
                    step["invoice_amount"] = sc.get("invoice_amount")
                    step["saved_mr_id"] = sc.get("jute_mr_id")
                    step["is_final_return"] = bool(sc.get("is_final_return"))

                    # The step's transfer PO, as stored: the hop's linked PO,
                    # or (returned step) the Final PO found by its marker.
                    if step["is_final_return"]:
                        final_po = po_queries.get_final_transfer_po(mr_id)
                        if final_po:
                            step.update(
                                po_state="transfer",
                                po_no=(final_po.get("po_no_formatted")
                                       or f"PO #{int(final_po['jute_po_id'])}"),
                                po_date=final_po.get("po_date"),
                                po_weight=final_po.get("weight"),
                                po_value=final_po.get("jute_po_value"),
                            )
                        else:
                            step["po_state"] = "none"
                    else:
                        step.update(_step_po_info(sc))

                    # Back-calculate % rate increase (TODO: use DB column after migration)
                    current_total = float(sc.get("total_amount") or 0)
                    if prev_total > 0:
                        step["pct_rate_increase"] = ((current_total - prev_total) / prev_total) * 100
                    else:
                        step["pct_rate_increase"] = 0.0

                    # Load invoice details (LC/contract) for saved steps
                    saved_mr_id = sc.get("jute_mr_id")
                    if saved_mr_id:
                        inv_details = get_invoice_details_by_mr_id(saved_mr_id)
                        if inv_details:
                            step["lc_reference_no"] = inv_details.get("consignment_no") or ""
                            step["lc_date"] = inv_details.get("consignment_date")
                            step["po_no_for_lc"] = str(inv_details.get("contract_no") or "")
                            step["order_date_for_lc"] = inv_details.get("contract_date")

                    transfers[mr_id].append(step)
                    prev_total = current_total
            except Exception as e:
                st.error(f"Error reconstructing chain: {str(e)}")

        # Fetch line items for each saved step's MR (with item names)
        if step_line_items_key not in st.session_state:
            step_li_map = {}
            for idx, s in enumerate(transfers[mr_id]):
                saved_id = s.get("saved_mr_id")
                if saved_id:
                    step_li = _fetch_step_line_items(saved_id)
                    if step_li:
                        step_li_map[idx] = step_li
            st.session_state[step_line_items_key] = step_li_map

        # Populate warehouse_id on each saved step from its line items
        step_li_map_local = st.session_state.get(step_line_items_key, {})
        for idx, s in enumerate(transfers[mr_id]):
            if s.get("saved_mr_id") and not s.get("warehouse_id"):
                li_list = step_li_map_local.get(idx, [])
                if li_list:
                    s["warehouse_id"] = li_list[0].get("warehouse_id")

        # Add blank step for new entry — but NOT when the chain is finalized
        # (the synthetic return-to-origin step is the terminal state).
        if not is_finalized:
            new_step = _empty_transfer_step()
            new_step["mr_date"] = raw_mr_date
            transfers[mr_id].append(new_step)

    steps = transfers[mr_id]
    li_data = line_items_map.get(mr_id, [])
    step_li_map = st.session_state.get(f"step_line_items_{filter_key}_{mr_id}", {})

    # Source (Step 0) display total: prefer Step 1's frozen snapshot when it
    # exists (the source MR's row Total Amount is overwritten in-place during
    # finalization, so it can no longer be trusted for "original" display).
    if steps and steps[0].get("saved_mr_id") and not steps[0].get("is_final_return"):
        orig_total = float(steps[0].get("total_amount") or 0)
    else:
        orig_total = raw_total

    # Ensure all step totals are up-to-date before rendering.
    # _recalculate_chain skips saved steps (uses DB total) and fills in unsaved steps.
    _recalculate_chain(steps, li_data, orig_total, use_new_rounding=True)

    # Render chain header
    st.divider()
    st.subheader(f"Transfer Chain — {row.get('Jute Gate Entry No', 'N/A')} ({row.get('Jute Supplier', 'N/A')})")
    st.write(f"**Original Total:** ₹{orig_total:,.0f}")
    # The mill's own ERP purchase order for this lorry (one read per selected
    # MR, kept for the session): shown once here, not on every step card.
    orig_po_cache = st.session_state.setdefault(f"orig_po_{filter_key}", {})
    orig_po_failed = False
    if mr_id not in orig_po_cache:
        try:
            orig_po_cache[mr_id] = get_original_po(mr_id)
        except Exception:
            orig_po_failed = True       # not cached: the next run tries again
            _log.exception("Reading the original PO of MR %s failed", mr_id)
    orig_po = orig_po_cache.get(mr_id)
    if orig_po:
        orig_po_no = format_po_no(orig_po.get("po_no"), orig_po.get("co_prefix"),
                                  orig_po.get("branch_prefix"),
                                  None if pd.isna(orig_po.get("po_date")) else orig_po.get("po_date"))
        st.write(f"**Original PO:** {orig_po_no or '—'}"
                 + (f" · {_fmt_date(orig_po.get('po_date'))}" if _fmt_date(orig_po.get("po_date")) else ""))
    elif orig_po_failed:
        st.write("**Original PO:** could not be read just now — it is read again on the next tap")
    else:
        st.write("**Original PO:** none in the ERP")

    # Render all steps
    for i, step in enumerate(steps):
        if step.get("saved_mr_id") and i in step_li_map:
            # Saved step: use its own line items (rates are authoritative)
            step_line_items = step_li_map[i]
            base_step_index = 0  # not used for saved steps
        else:
            # Unsaved step: use the nearest preceding saved step's line items
            # so that displayed rates reflect the previous step's actual rates,
            # not the original source MR rates.
            step_line_items = li_data
            base_step_index = 0
            for j in range(i - 1, -1, -1):
                if steps[j].get("saved_mr_id") and j in step_li_map:
                    step_line_items = step_li_map[j]
                    base_step_index = j
                    break
        _render_step_card(
            step_index=i,
            step=step,
            all_steps=steps,
            line_items=step_line_items,
            original_total_amount=orig_total,
            mr_id=mr_id,
            filter_key=filter_key,
            source_row=row,
            base_step_index=base_step_index,
            root_line_items=li_data,
        )


# TODO: Extract to jute_mr_page_helpers.py (shared with jute_mr.py)
def _render_step_card(step_index, step, all_steps, line_items, original_total_amount, mr_id,
                      filter_key, source_row=None, base_step_index=0, root_line_items=None):
    """
    Render individual step card with company/date/% inputs, metrics, line items, and action buttons.

    Inputs:
    - step_index: position in chain (0 = source)
    - step: step dict
    - all_steps: full steps list (for recalculation context)
    - line_items: line items for rate display (may be from a saved predecessor)
    - original_total_amount: source MR total
    - mr_id: parent MR ID
    - filter_key: for session state
    - source_row: source MR row (for challan_date, PO no. display)
    - base_step_index: index of the step whose line items are being used as base
                       (for unsaved steps following a saved step)
    - root_line_items: the root MR's line items -- what _recalculate_chain
                       cascades the whole chain from (the card's own
                       line_items may already carry a saved step's mark-up)
    """
    is_saved = "saved_mr_id" in step and step.get("saved_mr_id") is not None
    is_empty = not step.get("company")
    if root_line_items is None:
        root_line_items = line_items

    def _recalculate():
        _recalculate_chain(all_steps, root_line_items, original_total_amount, use_new_rounding=True)

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
                # index= is read only when the box is created: a step kept in
                # session state while another lorry was shown gets its
                # company back (the widget itself was dropped meanwhile).
                kept = step.get("company", "")
                company_before = kept
                step["company"] = st.selectbox(
                    "Company",
                    options=co_options,
                    index=co_options.index(kept) if kept in co_options else 0,
                    key=f"company_{mr_id}_{step_index}",
                    disabled=is_saved
                )
                if step["company"] != company_before:
                    # The totals were computed before the pick: redo them
                    # now, so this card does not print 'Total: ₹0' once.
                    _recalculate()

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

        # Status badge + % rate increase
        with col3:
            if is_saved:
                pct = step.get("pct_rate_increase", 0)
                if pct and abs(pct) > 0.001:
                    st.write(f"✓ **Saved** — ↑ {pct:.2f}%")
                else:
                    st.write("✓ **Saved**")
            else:
                st.write("● **Editing**")

        # The step's transfer PO (the original PO is in the chain header).
        if is_saved:
            po_label = "Final PO" if step.get("is_final_return") else "Forwarding PO"
            po_state = step.get("po_state", "none")
            if po_state == "transfer":
                st.write(_po_line(po_label, step.get("po_no", ""), step.get("po_date"),
                                  step.get("po_weight"), step.get("po_value")))
                if step.get("is_final_return"):
                    # The invoice the Final PO is compared with is the hop's
                    # invoice as stored on the root (jute_mr.invoice_amount);
                    # it can differ from the MR lines' total by a few rupees.
                    invoice_amount = _stored_amount(step.get("invoice_amount"))
                    if invoice_amount is not None:
                        st.caption("PO rate rounded to the nearest ₹50 — invoice "
                                   f"₹{invoice_amount:,.0f}")
                    else:
                        st.caption("PO rate rounded to the nearest ₹50 — MR total "
                                   f"₹{float(step.get('total_amount') or 0):,.0f}")
            elif po_state == "erp":
                st.write(f"**PO:** {step.get('po_no', '')} (linked in the ERP, not a transfer PO)")
            elif any(float(li.get("weight") or 0) > 0 for li in (line_items or [])):
                st.write(f"**{po_label}:** not created")
                st.caption(
                    "Saved before transfer POs were introduced (or while they "
                    "were switched off). The one-time backfill creates it when "
                    "it is run; nothing to do here."
                )
            else:
                st.write(f"**{po_label}:** not applicable — no accepted weight on this lorry")

        # Row A: Challan Date, Warehouse, Transport toggle
        col_ch, col_wh, col_tr = st.columns(3)

        with col_ch:
            challan_dt = source_row.get("Challan Date") if source_row is not None else None
            if challan_dt is not None and not (isinstance(challan_dt, float) and pd.isna(challan_dt)):
                st.write(f"**Challan Date:** {challan_dt}")
            else:
                st.write("**Challan Date:** —")

        with col_wh:
            if is_saved:
                # Display warehouse name for saved steps (look up from warehouse_id)
                wh_id = step.get("warehouse_id")
                if wh_id:
                    # Resolve company label to branch_id for warehouse lookup
                    _, co_branch_mapping = get_company_branch_options()
                    company_label = step.get("company", "")
                    if company_label in co_branch_mapping:
                        _, step_branch_id = co_branch_mapping[company_label]
                        warehouses = get_warehouses_by_branch(step_branch_id)
                        st.write(f"**Warehouse:** {warehouses.get(int(wh_id), str(wh_id))}")
                    else:
                        st.write(f"**Warehouse:** ID {wh_id}")
                else:
                    st.write("**Warehouse:** —")
            else:
                # Editable warehouse selectbox: the godowns of the step's
                # company, by id (two godowns of a branch can share a name).
                company_label = step.get("company", "")
                _, co_branch_mapping = get_company_branch_options()
                if company_label and company_label in co_branch_mapping:
                    _, step_branch_id = co_branch_mapping[company_label]
                    warehouses = get_warehouses_by_branch(step_branch_id)   # {id: label}
                    # A godown belongs to one branch: a pick made under the
                    # previous company is dropped with the company (the box
                    # is keyed by branch, so it starts blank again).
                    if step.get("warehouse_branch_id") != step_branch_id:
                        step["warehouse_id"] = None
                        step["warehouse_branch_id"] = step_branch_id
                    wh_ids = [None] + list(warehouses)
                    current = step.get("warehouse_id")
                    new_wh = st.selectbox(
                        "Warehouse",
                        options=wh_ids,
                        index=wh_ids.index(current) if current in wh_ids else 0,
                        format_func=lambda wid: "" if wid is None else warehouses.get(wid, str(wid)),
                        key=f"wh_{mr_id}_{step_index}_{step_branch_id}",
                    )
                    # Always written: a blank box means no godown, never the
                    # last one picked.
                    step["warehouse_id"] = int(new_wh) if new_wh is not None else None
                else:
                    step["warehouse_id"] = None
                    step["warehouse_branch_id"] = None
                    st.write("**Warehouse:** —")

        with col_tr:
            if is_saved:
                st.write("**Transport:** Copied" if step.get("transfer_transport", True) else "**Transport:** Hand cart")
            else:
                transport_key = f"transport_{mr_id}_{step_index}"
                step["transfer_transport"] = st.checkbox(
                    "Transfer transport details",
                    value=step.get("transfer_transport", True),
                    key=transport_key,
                )

        # Row B: LC/Contract inputs (step 2+ only, since step 0 has no invoice)
        if step_index > 0:
            if is_saved:
                # Display read-only LC/contract values
                lc_ref = step.get("lc_reference_no", "")
                lc_dt = step.get("lc_date")
                po_lc = step.get("po_no_for_lc", "")
                od_lc = step.get("order_date_for_lc")
                col_lc1, col_lc2, col_lc3, col_lc4 = st.columns(4)
                with col_lc1:
                    st.write(f"**LC Ref No.:** {lc_ref or '—'}")
                with col_lc2:
                    st.write(f"**LC Date:** {lc_dt or '—'}")
                with col_lc3:
                    st.write(f"**LC order ref:** {po_lc or '—'}")
                with col_lc4:
                    st.write(f"**Order Date (LC):** {od_lc or '—'}")
            elif step.get("company"):
                # Editable LC/contract inputs
                col_lc1, col_lc2, col_lc3, col_lc4 = st.columns(4)
                with col_lc1:
                    step["lc_reference_no"] = st.text_input(
                        "LC Reference No.",
                        value=step.get("lc_reference_no", ""),
                        key=f"lc_ref_{mr_id}_{step_index}",
                    )
                with col_lc2:
                    lc_date_val = step.get("lc_date")
                    if isinstance(lc_date_val, str):
                        try:
                            lc_date_val = datetime.strptime(lc_date_val, "%Y-%m-%d").date()
                        except:
                            lc_date_val = None
                    step["lc_date"] = st.date_input(
                        "LC Date",
                        value=lc_date_val,
                        key=f"lc_date_{mr_id}_{step_index}",
                    )
                with col_lc3:
                    step["po_no_for_lc"] = st.text_input(
                        "LC order ref",
                        value=step.get("po_no_for_lc", ""),
                        key=f"po_lc_{mr_id}_{step_index}",
                    )
                with col_lc4:
                    od_val = step.get("order_date_for_lc")
                    if isinstance(od_val, str):
                        try:
                            od_val = datetime.strptime(od_val, "%Y-%m-%d").date()
                        except:
                            od_val = None
                    step["order_date_for_lc"] = st.date_input(
                        "Order Date for LC",
                        value=od_val,
                        key=f"od_lc_{mr_id}_{step_index}",
                    )

        # % Rate Increase input (for step 2+, unsaved only). The box is a
        # widget with a key and NO value=: it is seeded from the step dict
        # when it does not exist yet, and whenever it differs from the dict
        # the dict follows the box (the owner typed) and the chain is
        # recomputed in this same run -- so the % on screen, the Total under
        # it and the % a Save posts are always one figure, whatever happened
        # to other lorries or on a Reload in between.
        if step_index > 0 and not is_saved and step.get("company"):
            pct_input_key = f"pct_input_{mr_id}_{step_index}"
            dict_pct = float(step.get("pct_rate_increase", 0) or 0)
            if pct_input_key not in st.session_state:
                st.session_state[pct_input_key] = dict_pct

            col_pct, col_space = st.columns([1, 3])
            with col_pct:
                new_pct = float(st.number_input(
                    "% Rate Increase",
                    step=0.01,
                    min_value=-100.0,
                    max_value=100.0,
                    key=pct_input_key,
                ) or 0)
                if abs(new_pct - dict_pct) > 0.0001:
                    step["pct_rate_increase"] = new_pct
                    _recalculate()      # this card and the steps below it

        # Summary metrics — use step dict values (from _recalculate_chain for unsaved,
        # from DB header for saved). Recomputing from per-item rates diverges due to
        # intermediate rounding at each step.
        total = float(step.get("total_amount") or 0)
        claim = float(step.get("claim_amount") or 0)
        net = float(step.get("net_amount") or 0)
        tds = float(step.get("tds_amount") or 0) if is_saved else 0.0

        metrics = f"**Total:** ₹{total:,.0f} | **Claim:** ₹{claim:,.0f}"
        if abs(tds) >= 0.5:
            # The ERP's 194Q TDS on a finalized root: part of the stored net.
            metrics += f" | **TDS:** ₹{tds:,.0f}"
        st.markdown(metrics + f" | **Net:** ₹{net:,.0f}")

        # Line items table — saved steps with own line items use rates directly
        _render_step_line_items(step_index, line_items, all_steps, is_saved=is_saved, base_step_index=base_step_index)

        # Action buttons
        if is_saved:
            # Saved steps: "Delete from here" deletes this step and every
            # later one -- MRs, invoices and transfer POs, on production, so
            # it takes a confirming tick first (one mis-tap on a phone must
            # not be enough). The tick is a plain widget: unticked on the
            # next visit to the card.
            saved_mr_id = step.get("saved_mr_id")
            confirm_key = f"confirm_delete_{mr_id}_{step_index}"
            confirmed = st.checkbox(
                _delete_confirm_label(step_index, step, all_steps),
                key=confirm_key,
            )
            if st.button(
                f"Delete from Step {step_index + 1} onward",
                key=f"delete_saved_{mr_id}_{step_index}",
                disabled=not confirmed,
            ):
                if saved_mr_id:
                    with st.spinner("Deleting steps and refreshing..."):
                        try:
                            summary = delete_chain_from_step(
                                root_mr_id=mr_id,
                                from_mr_id=saved_mr_id,
                                updated_by=st.session_state.get("user_id", 1),
                            ) or {}
                            _flash(*_delete_result_message(summary, step_index, step))
                            _clear_tracker_cache()
                            _drop_chain_state(filter_key, mr_id, keep_selection=False)
                            st.rerun()
                        except Exception as e:
                            _log.exception("Delete from step %s of root %s failed",
                                           step_index + 1, mr_id)
                            st.error(f"Delete failed: {e}. Nothing was deleted.")
                else:
                    st.error("Could not find saved MR ID for this step.")
            st.caption("Tick the box, then tap Delete. Also deletes the transfer PO "
                       "of each deleted step.")

        elif step.get("company"):
            # Unsaved steps with company set: save, clear, or remove
            col_save, col_clear, col_delete = st.columns(3)

            with col_save:
                if st.button("Save Step", key=f"save_{mr_id}_{step_index}", type="primary"):
                    _save_step(step_index, step, all_steps, line_items, original_total_amount, mr_id, filter_key)

            with col_clear:
                if st.button("Clear", key=f"clear_{mr_id}_{step_index}",
                             help="Empty this step: company, date, godown, LC fields and %"):
                    all_steps[step_index] = _empty_transfer_step()
                    all_steps[step_index]["mr_date"] = _source_mr_date(source_row)
                    for k in _step_widget_keys(mr_id, step_index):
                        st.session_state.pop(k, None)
                    st.rerun()

            with col_delete:
                if st.button("Delete", key=f"delete_{mr_id}_{step_index}",
                             help="Remove this unsaved step from the chain"):
                    last_index = len(all_steps)
                    all_steps.pop(step_index)
                    # The steps below move up one place: their widgets are
                    # dropped too and come back from their step dicts.
                    for i in range(step_index, last_index + 1):
                        for k in _step_widget_keys(mr_id, i):
                            st.session_state.pop(k, None)
                    if not any(not s.get("saved_mr_id") for s in all_steps):
                        # Never leave the chain without a step to type into.
                        blank = _empty_transfer_step()
                        blank["mr_date"] = _source_mr_date(source_row)
                        all_steps.append(blank)
                    st.rerun()

            if transfer_po_enabled():
                _target = _company_branch_map().get(step.get("company", ""))
                _returns = (
                    step_index > 0 and _target is not None
                    and _target == (st.session_state.get("selected_company_id"),
                                    st.session_state.get("selected_branch_id"))
                )
                if _returns:
                    st.caption(
                        f"Saving also creates the Final PO at {step['company']} "
                        "(takes the next PO number of that branch)."
                    )
                else:
                    st.caption(f"Saving also creates the Forwarding PO at {step['company']}.")

        st.divider()


# TODO: Extract to jute_mr_page_helpers.py (shared with jute_mr.py)
def _render_step_line_items(step_index, line_items, all_steps, is_saved=False, base_step_index=0):
    """Render line items table showing items with rates for this step.

    Args:
        step_index: Position in chain (0 = source, 1+ = transfer steps)
        line_items: Line items for this step. For saved steps these come from
                    that step's own jute_mr_li (rates are the purchase price).
                    For unsaved steps these come from the nearest preceding
                    saved step (or source MR if no saved predecessor).
        all_steps: Full chain for cumulative multiplier calculation
        is_saved: True when line_items come from this step's own MR (use rates directly)
        base_step_index: Index of the step whose line items are being used as
                         base rates. The cumulative multiplier only accumulates
                         from steps AFTER this index.

    Display logic:
        - Saved steps or source step (0): amount = weight × rate / 100 (rate used as-is)
        - Unsaved transfer steps (1+): amount = weight × base_rate × multiplier / 100
          where multiplier only includes pcts from (base_step_index+1) to step_index
    """
    if not line_items:
        st.write("*(No line items)*")
        return

    # Build table rows
    rows = []
    total_amount = 0.0

    def _is_missing(v):
        """True for None or NaN."""
        return v is None or (isinstance(v, float) and v != v)

    for li in line_items:
        try:
            raw_weight = li.get("weight")
            raw_rate = li.get("original_rate")
            quality = li.get("item_quality", "Item")

            weight_missing = _is_missing(raw_weight)
            rate_missing = _is_missing(raw_rate)

            # Coerce to numbers for math (None/NaN -> 0)
            weight = 0.0 if weight_missing else round(float(raw_weight), 0)
            orig_rate = 0.0 if rate_missing else float(raw_rate)

            # Use shared _cascade_rate for round-then-cascade consistency
            if is_saved or step_index == 0:
                effective_rate = orig_rate
            else:
                # Build a sub-chain from base_step_index to step_index
                # _cascade_rate expects steps[1..up_to_index] to have pct_rate_increase
                sub_steps = [{}]  # dummy step 0
                start = max(1, base_step_index + 1)
                for i in range(start, step_index + 1):
                    if i < len(all_steps):
                        sub_steps.append(all_steps[i])
                effective_rate = _cascade_rate(orig_rate, sub_steps, len(sub_steps) - 1)

            # Calculate amount (only meaningful when both weight and rate are present)
            amount_known = not (weight_missing or rate_missing)
            amount = _calculate_line_item_amount(weight, effective_rate) if amount_known else 0.0

            warehouse_name = li.get("warehouse_name") or li.get("Warehouse") or "—"
            rows.append({
                "Quality": quality,
                "Weight (KG)": "—" if weight_missing else int(weight),
                "Warehouse": warehouse_name,
                "Rate (per quintal)": "—" if rate_missing else f"₹{effective_rate:,.0f}",
                "Amount": "—" if not amount_known else f"₹{amount:,.2f}",
            })
            if amount_known:
                total_amount += amount

        except (ValueError, TypeError) as e:
            st.warning(f"Error processing line item: {str(e)}")
            continue

    # Add total row (skip missing weights so '—' rows don't poison the sum)
    valid_weights = [
        int(float(li.get("weight"))) for li in line_items
        if not _is_missing(li.get("weight"))
    ]
    total_weight = sum(valid_weights) if valid_weights else 0

    # (plain text: st.dataframe does not render markdown, so no ** here)
    rows.append({
        "Quality": "TOTAL",
        "Weight (KG)": total_weight if valid_weights else "—",
        "Warehouse": "—",
        "Rate (per quintal)": "—",
        "Amount": f"₹{total_amount:,.2f}" if total_amount > 0 else "—",
    })

    # Display table
    st.markdown("**Line Items**")
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)


def _save_step(step_index, step, all_steps, line_items, original_total_amount, mr_id, filter_key):
    """
    Save step to database via save_transfer_step().
    Clear cache to force reload, then rerun.
    """
    # Validate required fields before saving
    if not step.get("company"):
        st.error("Company is required before saving.")
        return
    if not step.get("mr_date"):
        st.error("Date is required before saving.")
        return

    try:
        # Get source row for context
        source_df = st.session_state[f"source_df_{filter_key}"]
        row_idx = st.session_state[f"selected_row_{filter_key}"]
        source_row = source_df.iloc[row_idx]

        # Resolve company label to (co_id, branch_id) via mapping
        company_label = step.get("company", "")
        _, co_branch_mapping = get_company_branch_options()
        if company_label not in co_branch_mapping:
            st.error(f"Unknown company selection: {company_label}")
            return
        step_co_id, step_branch_id = co_branch_mapping[company_label]

        # The godown goes on every hop line (the ERP's stock view is per
        # godown): it must be one of the chosen company's own.
        warehouse_id = step.get("warehouse_id")
        if warehouse_id is not None:
            if int(warehouse_id) not in get_warehouses_by_branch(step_branch_id):
                st.error(f"The godown picked is not at {company_label}. Pick the "
                         "godown again (the box was reset when the company changed).")
                step["warehouse_id"] = None
                return
            warehouse_id = int(warehouse_id)

        # Determine previous step's co_id and branch_id
        if step_index > 0 and step_index - 1 < len(all_steps):
            prev_step = all_steps[step_index - 1]
            prev_label = prev_step.get("company", "")
            if prev_label in co_branch_mapping:
                prev_co_id, prev_branch_id = co_branch_mapping[prev_label]
            else:
                # Fallback to source company/branch
                prev_co_id = st.session_state.get("selected_company_id", 1)
                prev_branch_id = st.session_state.get("selected_branch_id", 1)
        else:
            prev_co_id = st.session_state.get("selected_company_id", 1)
            prev_branch_id = st.session_state.get("selected_branch_id", 1)

        source_co_id = st.session_state.get("selected_company_id", 1)
        source_branch_id = st.session_state.get("selected_branch_id", 1)

        # Single-step rate multiplier: only this step's pct increase.
        # The source MR (set below) already has the previous step's rates baked in.
        pct_rate = float(step.get("pct_rate_increase", 0) or 0)
        rate_multiplier = 1.0 + pct_rate / 100.0

        # Use previous step's saved MR as rate base (matches display path).
        # For step 0, use root MR (no previous step).
        effective_source_mr_id = mr_id
        if step_index > 0:
            prev_saved_id = all_steps[step_index - 1].get("saved_mr_id")
            if prev_saved_id:
                effective_source_mr_id = prev_saved_id

        # Parse step values
        pct_rate = float(step.get("pct_rate_increase", 0) or 0)
        total_amt = float(step.get("total_amount", 0) or 0)
        claim_amt = float(step.get("claim_amount", 0) or 0)
        net_amt = float(step.get("net_amount", 0) or 0)
        mr_rate = float(step.get("mr_rate", 0) or 0)
        mr_date_val = step.get("mr_date", date.today())

        # Prepare TransferStep dataclass
        transfer_step = TransferStep(
            co_id=step_co_id,
            branch_id=step_branch_id,
            mr_date=mr_date_val,
            mr_rate=mr_rate,
            pct_rate_increase=pct_rate,
            total_amount=total_amt,
            claim_amount=claim_amt,
            net_amount=net_amt,
            warehouse_id=warehouse_id,
            mr_no=0,  # Assigned inside save_transfer_step transaction
            lc_reference_no=step.get("lc_reference_no", ""),
            lc_date=step.get("lc_date"),
            po_no_for_lc=step.get("po_no_for_lc", ""),
            order_date_for_lc=step.get("order_date_for_lc"),
            transfer_transport=step.get("transfer_transport", True),
        )

        # Determine if this is the final step (returns to source company)
        is_final = (step_co_id == source_co_id and step_branch_id == source_branch_id)

        # Call save_transfer_step from transfer.py
        result = save_transfer_step(
            source_mr_id=effective_source_mr_id,
            step=transfer_step,
            prev_co_id=prev_co_id,
            prev_branch_id=prev_branch_id,
            source_co_id=source_co_id,
            source_branch_id=source_branch_id,
            root_mr_id=mr_id,
            updated_by=st.session_state.get("user_id", 1),
            rate_multiplier=rate_multiplier,
            is_first_step=(step_index == 0),
            is_final=is_final,
            original_source_mr_id=mr_id,
            use_new_rounding=True,
        )

        # Clear cache to force reload on next render
        _drop_chain_state(filter_key, mr_id)
        _clear_tracker_cache()

        # Shown on the next run (a message written just before st.rerun()
        # would never be seen).
        po = (result or {}).get("po") or {}
        if is_final and step_index > 0:
            msg = f"Step {step_index + 1} saved — returned to {company_label}"
            po_label = "Final PO"
        else:
            msg = f"Step {step_index + 1} saved — MR {transfer_step.mr_no} at {company_label}"
            po_label = "Forwarding PO"
        if po.get("po_id"):
            _flash("success", f"{msg}, {po_label} {po.get('po_no_formatted') or po['po_id']} created.")
        elif po.get("skipped"):
            _flash("warning", f"{msg}. No transfer PO was created: {_skip_text(po['skipped'])}.")
        else:
            _flash("success", f"{msg}.")
        st.rerun()

    except Exception as e:
        # The whole step is one transaction: a failure writes nothing. The
        # traceback goes to the server log (.logs/jt.log), not to the phone.
        _log.exception("Saving step %s of root MR %s failed", step_index + 1, mr_id)
        st.error(f"Step {step_index + 1} was not saved: {e}. Nothing was written. "
                 "Tap Reload above to read the chain again, then save once more.")
