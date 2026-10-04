"""PO Tracker: every transferred lorry next to its three purchase orders.

One row per transferred lorry (a vertical-chain root MR) with its Original
PO, Forwarding PO and Final PO; a second view rolls the same lorries up to
one row per Original PO; selecting a row reconciles each transfer PO against
the MR lines it was built from.

STRICTLY READ-ONLY. The page issues SELECTs only (po_queries) and has no
button that writes anything -- the only buttons are Refresh and the two CSV
downloads. Missing transfer POs are created by the Transfer Chain page (new
steps) or the one-off backfill script, never from here.

Everything is loaded once per financial year and cached for a minute; every
filter, search and row selection is plain Python on that load, so selecting
a row runs no query. Shaping and maths live in po_tracker_helpers (pure).
"""

import pandas as pd
import streamlit as st

from .. import po_tracker_helpers as H
from ..po_ops import transfer_po_enabled
from ..po_queries import clear_tracker_cache, get_tracker_index, load_tracker_data

# Widget state. Each key is initialised once in st.session_state and the
# widgets are created with key= only (no value= / index=), so a rerun never
# overrides what the user chose. They are created with
# persist_state="session" too: Streamlit drops a widget's key when the widget
# is not rendered for a run, so without it the designed loop Transfer Chain
# -> PO Tracker -> Transfer Chain came back to All mills / newest month / no
# search every time.
K_VIEW = "pot_view"
K_MILL = "pot_mill"            # co_id of the mill, 0 = all mills
K_PERIOD = "pot_period"        # 'M:2026-09' or 'FY:2026'
K_SHOW = "pot_show"
K_SEARCH = "pot_search"
K_FULL = "pot_full"
K_FWD = "pot_fwd"              # co_id of the forwarding company, 0 = all
# Row selection is remembered as an id (root MR / Original PO), never as a
# row position: positions change with every filter.
K_SEL_ROOT = "pot_sel_root"
K_SEL_PO = "pot_sel_po"

_DEFAULTS = {
    K_VIEW: H.VIEW_LORRIES, K_MILL: 0, K_PERIOD: None, K_SHOW: H.SHOW_ALL,
    K_SEARCH: "", K_FULL: False, K_FWD: 0, K_SEL_ROOT: None, K_SEL_PO: None,
}

_MONEY_LORRY_COLUMNS = ["MR Kg", "MR Amt", "Fwd Kg", "Fwd Value", "Final Kg",
                        "Final Value", "Inv Amt"]
_SIGNED_LORRY_COLUMNS = ["Fwd Diff", "Final Diff"]


def _init_state() -> None:
    for key, value in _DEFAULTS.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _whole(value) -> str:
    return f"{value:,.0f}"


# ---------------------------------------------------------------------------
# Tables with one selectable row
# ---------------------------------------------------------------------------

def _select_grid(frame: pd.DataFrame, row_ids: list, state_key: str, prefix: str,
                 filter_state: tuple, column_config: dict,
                 money_columns: list, signed_columns: list):
    """Show `frame` with single-row selection and return the selected id.

    The widget key carries a hash of the filter state and the row ids: a
    selection is stored by position, so under a fixed key it would survive a
    filter change and point at another lorry. A new key starts unselected;
    when the remembered id is still among the rows it is selected again.
    """
    key = H.grid_key(prefix, filter_state, row_ids)
    stored = st.session_state.get(state_key)
    default = None
    if stored in row_ids:
        default = {"selection": {"rows": [row_ids.index(stored)]}}
    # Styler text is what a number cell shows; sorting still uses the number.
    money = [c for c in money_columns if c in frame.columns]
    signed = [c for c in signed_columns if c in frame.columns]
    styled = frame.style
    if money:
        styled = styled.format(_whole, subset=money, na_rep=H.CELL_NONE)
    if signed:
        styled = styled.format(H.fmt_signed, subset=signed, na_rep=H.CELL_NONE)
    event = st.dataframe(
        styled,
        key=key,
        on_select="rerun",
        selection_mode="single-row",
        selection_default=default,
        hide_index=True,
        column_config=column_config,
        width="stretch",
        placeholder=H.CELL_NONE,
    )
    rows = []
    if event is not None and hasattr(event, "selection"):
        rows = list(event.selection.rows)
    selected = row_ids[rows[0]] if rows and 0 <= rows[0] < len(row_ids) else None
    st.session_state[state_key] = selected
    return selected


def _lorry_column_config() -> dict:
    text, number, day = (st.column_config.TextColumn, st.column_config.NumberColumn,
                         st.column_config.DateColumn)
    config = {
        # The first four columns must fit a 390 px phone next to the ~32 px
        # selection box: 100 + 70 + 78 + 78 = 326 px (the earlier 362 px cut
        # the Final PO column, and the pilot's 'EJM 63' read 'EJM 6' -- the
        # same text as its Orig PO cell).
        "Lorry": text("Lorry", pinned=True, width=100),
        "Orig PO": text("Orig PO", width=70, help="Original PO: the mill's ERP purchase order on the outside supplier"),
        "Fwd PO": text("Fwd PO", width=78, help="Forwarding PO: the forwarding company's transfer PO"),
        "Final PO": text("Final PO", width=78, help="Final PO: the mill's transfer PO on the forwarding company, made when the lorry is back at the mill"),
        "Status": text("Status", width=124),
        "GE No": number("GE No", format="%d"),
        "Days": number("Days", format="%d", help="Days since the lorry was transferred, while it is not back at the mill"),
        "PO Lorries": text("PO Lorries", help="This lorry's place among the lorries received on its Original PO"),
        "MR Kg": number("MR Kg", help="Accepted weight of the lorry"),
        "MR Amt": number("MR Amt", help="Accepted kg x original rate, at the forwarding company"),
        "Fwd Diff": number("Fwd Diff", help="Forwarding PO value minus MR amount"),
        "Final Diff": number("Final Diff", help="Final PO value minus invoice amount"),
    }
    for col in H.LORRY_DATE_COLUMNS:
        config[col] = day(col, format="DD MMM YYYY")
    return config


def _po_column_config() -> dict:
    text, number, day = (st.column_config.TextColumn, st.column_config.NumberColumn,
                         st.column_config.DateColumn)
    return {
        "Orig PO": text("Orig PO", pinned=True, width=80),
        "Date": day("Date", format="DD MMM YYYY", width=98),
        "Lorries": text("Lorries", width=74, help="transferred / received / ordered"),
        "Fwd POs": text("Fwd POs", width=118),
        "Final POs": text("Final POs", width=130),
        "Status": text("Status", width=124),
        "Ordered Kg": number("Ordered Kg", help="Weight ordered on the Original PO"),
        "Ordered Value": number("Ordered Value"),
        "MR Kg": number("MR Kg", help="Accepted weight of the transferred lorries"),
    }


def _static_table(frame: pd.DataFrame) -> None:
    st.dataframe(frame, hide_index=True, width="stretch", height="content")


# ---------------------------------------------------------------------------
# Drill-down blocks
# ---------------------------------------------------------------------------

def _render_original_po(orig, siblings, lorries_by_root, other_year_roots,
                        selected_root_id=None) -> None:
    with st.container(border=True):
        if orig is None:
            st.markdown("**Original PO**")
            st.write("This lorry has no purchase order in the ERP.")
            return
        st.markdown(f"**Original PO** · {orig['full']} · {H.fmt_date(orig['date'])} · {orig['status']}")
        who = " / ".join(H.md_escape(x) for x in (orig["supplier_name"], orig["party_name"]) if x)
        headline = H.original_po_headline(orig)
        st.markdown("  \n".join(x for x in (who, headline) if x))
        _static_table(H.original_po_lines_table(orig))
        st.markdown("Lorries received on this PO")
        _static_table(H.sibling_table(siblings, lorries_by_root, selected_root_id,
                                      other_year_roots))


def _render_forwarding_po(hop: dict, full: bool) -> None:
    po = hop["po"]
    with st.container(border=True):
        title = "**Forwarding PO**"
        if po is not None:
            title += f" · {po['full']} · {H.fmt_date(po['date'])}"
            if po["status_id"] != H.PO_STATUS_CLOSED:
                title += f" · {po['status']}"
        st.markdown(title)
        mr_no = hop["mr_no"] if hop["mr_no"] is not None else "?"
        st.markdown(
            f"{H.md_escape(hop['co_name'] or hop['co_prefix'])} buys from "
            f"{H.md_escape(hop['party_name'] or 'the supplier')} · MR {mr_no} at "
            f"{hop['co_prefix']} dated {H.fmt_date(hop['mr_date'])}"
        )
        if po is None:
            if hop["cell"] == H.CELL_NA:
                st.write("Not applicable — no accepted weight on this lorry.")
                return
            if hop["cell"] == H.CELL_NOT_YET:
                st.write(
                    f"Not created yet. Transferred on {H.fmt_date(hop['mr_date'])}, before "
                    "transfer POs existed (or while they were switched off); the one-time "
                    "backfill creates it."
                )
            else:
                st.write(f"The MR is linked to PO id {hop['cell'].lstrip('#')}, which no longer exists.")
        elif not hop["is_transfer_po"]:
            # A hand-linked ERP PO (or another MR's transfer PO) is written with
            # other items: pairing its lines with this MR's would mean nothing.
            st.write("This is not the MR's own transfer PO, so its lines are not "
                     "compared with the MR.")
            return
        if hop["recon"]:              # the MR side alone while there is no PO
            _static_table(H.recon_table(hop["recon"], full))
        if hop["is_transfer_po"] and hop["recon"]:
            st.markdown(H.difference_sentence(
                po["value"], hop["mr_amount"], H.recon_totals(hop["recon"]), "MR", po["uom"]))


def _render_final_po(lorry: dict, full: bool) -> None:
    final = lorry["final"]
    pos = final["pos"]
    with st.container(border=True):
        if not pos:
            st.markdown("**Final PO**")
            if final["cell"] == H.CELL_AWAITING:
                st.write(
                    "Not yet. The Final PO is created when the step back to "
                    f"{lorry['mill_name'] or lorry['mill']} is saved on the "
                    "Transfer Chain page."
                )
                return
            if not lorry["returned"]:
                st.write(f"The lorry's MR at the mill is {lorry['root_status']}; "
                         "there is no Final PO.")
                return
        po = pos[0] if pos else None
        if po is not None:
            title = f"**Final PO** · {po['full']} · {H.fmt_date(po['date'])}"
            if po["status_id"] != H.PO_STATUS_CLOSED:
                title += f" · {po['status']}"
            st.markdown(title)
        seller = (po["party_name"] if po is not None and po["party_name"]
                  else lorry["root_party_name"])
        line = (f"{H.md_escape(lorry['mill_name'] or lorry['mill'])} buys from "
                f"{H.md_escape(seller or 'the forwarding company')}")
        if lorry["invoice_no"]:
            line += f" · invoice {H.md_escape(lorry['invoice_no'])} · {H.fmt_num(lorry['inv_amt'])}"
        if lorry["mill_mr_no"] is not None and lorry["returned"]:
            line += f" · MR {lorry['mill_mr_no']} at {lorry['mill']}"
        st.markdown(line)
        if po is None:
            if final["cell"] == H.CELL_NA:
                st.write("Not applicable — no accepted weight on the lorry's MR at the mill.")
                return
            st.write(f"Back at the mill on {H.fmt_date(lorry['returned_date'])}; Final PO not "
                     "created yet — the one-time backfill creates it.")
        if final["recon"]:
            _static_table(H.recon_table(final["recon"], full))
        if po is not None and lorry["returned"] and final["recon"]:
            totals = H.recon_totals(final["recon"])
            if lorry["inv_amt"] is not None:
                base, label = lorry["inv_amt"], "invoice"
            else:
                base, label = totals["mr_amount"], "MR"
            st.markdown(H.difference_sentence(po["value"], base, totals, label, po["uom"]))
        if len(pos) > 1:
            st.write("Other Final POs for this lorry: "
                     + ", ".join(p["full"] for p in pos[1:]))


def _render_lorry_detail(lorry: dict, lorries_by_root: dict, other_year_roots,
                         full: bool) -> None:
    st.divider()
    head = [f"GE {lorry['ge_no'] if lorry['ge_no'] is not None else '?'}",
            H.fmt_date(lorry["ge_date"])]
    if lorry["lorry_no"]:
        head.append(H.md_escape(lorry["lorry_no"]))
    names = " / ".join(H.md_escape(x) for x in (lorry["supplier"], lorry["party"]) if x)
    if names:
        head.append(names)
    route = [lorry["route"], lorry["status"]]
    if lorry["markup_pct"] is not None:
        route.append(f"{lorry['markup_pct']:+.2f} %")
    st.markdown(f"**{' · '.join(head)}**  \n{' · '.join(route)}")

    _render_original_po(lorry["orig"], lorry["siblings"], lorries_by_root,
                        other_year_roots, lorry["root_mr_id"])
    for hop in lorry["hops"]:
        _render_forwarding_po(hop, full)
    _render_final_po(lorry, full)
    st.caption(H.ROUNDING_CAPTION)
    if lorry["status"] == H.STATUS_CHECK:
        st.warning(f"Check — {lorry['status_reason']}")
    elif lorry["status"].startswith(H.STATUS_OTHER):
        st.warning(lorry["status_reason"])


def _render_po_detail(po_row: dict, lorries_by_root: dict, other_year_roots) -> None:
    st.divider()
    orig = po_row["orig"]
    siblings = lorries_by_root[po_row["root_mr_ids"][0]]["siblings"]
    _render_original_po(orig, siblings, lorries_by_root, other_year_roots)
    st.caption(f"Switch to Lorries and search {orig['full']} for the line detail.")


def _render_orphans(data: dict, chains: list) -> None:
    orphans = H.orphan_transfer_pos(
        H.records(data["transfer_pos"]),
        [c["root_mr_id"] for c in chains],
        [c["fwd_po_id"] for c in chains],
    )
    if not orphans:
        return
    with st.expander(f"Transfer POs without a chain ({len(orphans)})"):
        st.caption("Created by Jute Transfer for a step that no longer exists.")
        _static_table(pd.DataFrame(orphans))


def _refresh_button() -> None:
    if st.button("Refresh", key="pot_refresh", help="Reload from the database (data is kept for one minute)"):
        clear_tracker_cache()
        st.rerun()


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def po_tracker_page() -> None:
    """Entry point of the PO Tracker page (read-only)."""
    st.title("PO Tracker")
    if not transfer_po_enabled():
        st.warning("Transfer PO creation is switched off — steps saved now will have no PO.")

    _init_state()
    ss = st.session_state

    try:
        index = get_tracker_index()
        chains = H.records(index["chains"])
        today = H.as_date(index["today"])

        # Settle the stored filter values BEFORE any widget is created: a
        # value that is no longer among the options is reset here.
        mill_ids = [co for co, _label in H.mill_options(chains)]
        if ss[K_MILL] not in mill_ids:
            ss[K_MILL] = 0
        periods = H.period_options(chains, ss[K_MILL] or None)
        period_labels = dict(periods)
        if ss[K_PERIOD] not in period_labels:
            ss[K_PERIOD] = H.default_period(periods, today)
        if ss[K_VIEW] not in H.VIEW_OPTIONS:
            ss[K_VIEW] = H.VIEW_LORRIES
        if ss[K_SHOW] not in H.SHOW_OPTIONS:
            ss[K_SHOW] = H.SHOW_ALL
        fy = H.period_fy(ss[K_PERIOD])
        if fy is None:                       # no chain at all: nothing to pick
            fy = H.fy_start_year(today)

        data = load_tracker_data(fy)
        lorries = H.build_lorries(
            H.records(data["hops"]), H.records(data["transfer_pos"]),
            H.records(data["mr_lines"]), H.records(data["po_lines"]),
            H.records(data["siblings"]), today,
        )
    except Exception as exc:  # query error: say so, show no partial table
        st.error(f"Could not load the PO tracker: {exc}")
        return

    if not chains:
        st.info("No transferred lorries yet. They appear here after the first "
                "step is saved on the Transfer Chain page.")
        _refresh_button()
        _render_orphans(data, chains)
        return

    mill_labels = dict(H.mill_options(chains, fy))
    in_mill = [l for l in lorries if not ss[K_MILL] or l["mill_co_id"] == ss[K_MILL]]
    fwd_options = H.forwarder_options(in_mill)
    fwd_labels = dict(fwd_options)
    if ss[K_FWD] not in fwd_labels:
        ss[K_FWD] = 0

    view, period, show = ss[K_VIEW], ss[K_PERIOD], ss[K_SHOW]
    full = bool(ss[K_FULL])
    search = (ss[K_SEARCH] or "").strip()
    scope = [l for l in in_mill if not ss[K_FWD] or ss[K_FWD] in l["fwd_co_ids"]]
    if search:
        # A search looks through the whole financial year: the PO or lorry
        # asked for is rarely in the month that happens to be selected.
        shown = [l for l in scope if H.search_matches(l, search)]
    else:
        shown = [l for l in scope
                 if H.in_period(l["lorry_date"], period) and H.matches_show(l, show)]

    # -- filters, one per line (a phone stacks columns anyway) ---------------
    keep = {"persist_state": "session"}
    st.segmented_control("View", H.VIEW_OPTIONS, key=K_VIEW, required=True,
                         label_visibility="collapsed", **keep)
    st.selectbox("Mill", mill_ids, format_func=lambda co: mill_labels.get(co, str(co)),
                 key=K_MILL, label_visibility="collapsed", **keep)
    st.selectbox("Period", list(period_labels),
                 format_func=lambda code: period_labels.get(code, str(code)),
                 key=K_PERIOD, label_visibility="collapsed", **keep)
    st.pills("Show", H.SHOW_OPTIONS, key=K_SHOW, required=True,
             label_visibility="collapsed", **keep)
    st.text_input("Search", key=K_SEARCH, label_visibility="collapsed",
                  placeholder="Search PO no, GE no, supplier, lorry no", **keep)
    with st.expander("More filters"):
        st.selectbox("Forwarding company", list(fwd_labels),
                     format_func=lambda co: fwd_labels.get(co, str(co)), key=K_FWD, **keep)

    mill_prefix = next((l["mill"] for l in in_mill), "") if ss[K_MILL] else ""
    fwd_prefix = ""
    if ss[K_FWD]:
        fwd_prefix = next((h["co_prefix"] for l in in_mill for h in l["hops"]
                           if h["co_id"] == ss[K_FWD] and h["co_prefix"]),
                          f"co{ss[K_FWD]}")
    # The file is named after the rows it holds: mill, period (the financial
    # year for a search), Show chip, forwarder and search text.
    file_filters = {"show": show, "fwd_prefix": fwd_prefix, "search": search}
    with st.container(horizontal=True, vertical_alignment="center"):
        st.toggle("All columns", key=K_FULL, persist_state="session")
        _refresh_button()
        st.download_button(
            "Download CSV",
            data=H.lorries_csv_frame(shown).to_csv(index=False).encode("utf-8-sig"),
            file_name=H.csv_file_name(mill_prefix, period, **file_filters),
            mime="text/csv", key="pot_csv", on_click="ignore", disabled=not shown,
            help="One row per lorry in the current filter",
        )
        st.download_button(
            "Line detail CSV",
            data=H.line_detail_frame(shown).to_csv(index=False).encode("utf-8-sig"),
            file_name=H.csv_file_name(mill_prefix, period, "lines", **file_filters),
            mime="text/csv", key="pot_csv_lines", on_click="ignore", disabled=not shown,
            help="One row per PO line with the MR line beside it",
        )

    if search:
        where = f"FY {H.fy_text(fy)}"
        if ss[K_MILL]:
            where += f", {mill_prefix or 'selected mill'} only"
        if ss[K_FWD]:
            where += ", selected forwarding company only"
        st.caption(f"Searching all of {where} — Period and Show are ignored.")

    lorries_by_root = {l["root_mr_id"]: l for l in lorries}
    other_year_roots = {c["root_mr_id"] for c in chains} - set(lorries_by_root)
    filter_state = (view, ss[K_MILL], ss[K_FWD], period, show, search)

    if view == H.VIEW_POS:
        listed = {l["orig_po_id"] for l in shown if l["orig"] is not None}
        po_lorries = [l for l in in_mill if l["orig_po_id"] in listed]
        po_rows = H.original_po_rows(po_lorries)
        totals = H.lorry_totals(po_lorries)
        st.markdown(H.po_summary_line(po_rows, totals))
    else:
        po_rows = []
        totals = H.lorry_totals(shown)
        st.markdown(H.summary_line(totals))
    # A month with '0 forwarding POs · 0 final POs' while the pilot's POs sit
    # in another month read as 'the POs are missing': say where they are.
    note = H.fy_po_note(totals, H.lorry_totals(scope), period, fy, bool(search))
    if note:
        st.caption(note)

    # One banner for the whole financial year instead of a warning per lorry.
    for kind, text in H.backfill_banners(lorries):
        (st.info if kind == "info" else st.warning)(text)

    if not shown:
        st.info(H.nothing_found_text(search, fy) if search
                else "No lorries for this filter.")
        _render_orphans(data, chains)
        return

    if view == H.VIEW_POS:
        st.caption("Original POs of the lorries in this filter; each row covers "
                   "all of that PO's transferred lorries.")
        without_po = sum(1 for l in shown if l["orig"] is None)
        if without_po:
            st.caption(f"{without_po} lorries have no Original PO and are listed "
                       "only in the Lorries view.")
        selected_po = None
        if po_rows:
            selected_po = _select_grid(
                H.original_pos_frame(po_rows, full),
                [r["orig_po_id"] for r in po_rows], K_SEL_PO, "pot_pos", filter_state,
                _po_column_config(),
                ["Ordered Kg", "Ordered Value", "MR Kg", "Fwd Value", "Final Value"], [],
            )
            st.markdown(H.po_totals_line(po_rows, totals))
        row = next((r for r in po_rows if r["orig_po_id"] == selected_po), None)
        if row is not None:
            _render_po_detail(row, lorries_by_root, other_year_roots)
        elif po_rows:
            st.caption("Select a row to see that PO's lorries.")
    else:
        selected_root = _select_grid(
            H.lorries_frame(shown, full),
            [l["root_mr_id"] for l in shown], K_SEL_ROOT, "pot_lorries", filter_state,
            _lorry_column_config(), _MONEY_LORRY_COLUMNS, _SIGNED_LORRY_COLUMNS,
        )
        st.markdown(H.totals_line(totals))
        lorry = lorries_by_root.get(selected_root)
        if lorry is not None:
            _render_lorry_detail(lorry, lorries_by_root, other_year_roots, full)
        else:
            st.caption("Select a row to see the three POs of that lorry.")

    _render_orphans(data, chains)
