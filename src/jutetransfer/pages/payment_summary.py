"""Jute Supplier Dues: the Payment Tracker's bills summed up per JUTE SUPPLIER
(jute_supplier_mst, the broker), month-year wise, with who is on hold, how
long payments have been pending, a company-wise sheet and the bills.

Read-only on accounts and MRs. The only write is the owner's confirmation of
which party belongs to which jute supplier (jt_party_supplier_map, admin
only). Days pending are an indication worked oldest-first across all
companies; nothing is re-applied in accounts. Pure maths in
payment_summary_helpers; reads in payment_queries.
"""

import io
from datetime import date

import pandas as pd
import streamlit as st

from .. import auth
from .. import payment_summary_helpers as S
from .. import payment_tracker_helpers as H
from .. import supplier_map
from ..payment_queries import clear_cache, load_bills, load_summary_extras

EPS = 0.005
SHOW_DUES, SHOW_ALL = "With dues", "All"
HOLD_ALL, HOLD_ON, HOLD_OFF = "All", "On hold", "Not on hold"
ALL = "All"
NONE_PICKED = "-"


def _money(value) -> str:
    return f"₹{float(value or 0):,.0f}"


def _fy_text(fy: int) -> str:
    return f"{fy}-{(fy + 1) % 100:02d}"


def _fy_options(today: date, rows: pd.DataFrame) -> list:
    this_fy = today.year if today.month >= 4 else today.year - 1
    first = this_fy
    if not rows.empty:
        old = rows.loc[rows["Pending"] > EPS, "MR Date"].dropna()
        if not old.empty:
            d = min(old)
            first = min(first, d.year if d.month >= 4 else d.year - 1)
    return list(range(this_fy, first - 1, -1))


def _seed(key: str, value) -> None:
    if key not in st.session_state:
        st.session_state[key] = value


def _choose(key: str, options: list, default) -> None:
    if st.session_state.get(key) not in options:
        st.session_state[key] = default


def _date_cfg() -> st.column_config.DateColumn:
    return st.column_config.DateColumn(format="DD-MM-YYYY")


def _num_cfg(help_text=None) -> st.column_config.NumberColumn:
    return st.column_config.NumberColumn(format="%.0f", help=help_text)


def _supplier_config(period_cols: list) -> dict:
    cfg = {
        "skey": None,
        "Pending": _num_cfg(), "On Hold": _num_cfg(), "Free To Pay": _num_cfg(),
        "On Account": _num_cfg("Advance paid to the supplier that is not yet set against a bill"),
        "Days Pending": _num_cfg("Days since the oldest bill that is still unpaid, when the "
                                 "payments made are set against the oldest bills first. "
                                 "Bills on hold are left out. An indication only."),
        "Days Incl. Hold": _num_cfg("The same, with the bills on hold counted too"),
        "Pending From": st.column_config.DateColumn(
            format="DD-MM-YYYY", help="Date of the oldest bill not fully covered by payments, "
                                      "worked oldest-first"),
        "Cleared Till": st.column_config.DateColumn(
            format="DD-MM-YYYY", help="Bills up to this date are fully covered by payments, "
                                      "worked oldest-first"),
        "Oldest Unpaid Bill": _date_cfg(), "Last Payment": _date_cfg(),
        "Unpaid Bills": st.column_config.NumberColumn(format="%d"),
    }
    for c in period_cols:
        cfg[c] = _num_cfg()
    return cfg


SUPPLIER_ORDER = ["Jute Supplier", "Pending", "On Hold", "Free To Pay", "On Account", "Hold",
                  "Days Pending", "Days Incl. Hold", "Pending From", "Cleared Till",
                  "Oldest Unpaid Bill", "Last Payment", "Unpaid Bills", "Companies",
                  "Held Parties"]

COMPANY_CFG = {
    "Bills": st.column_config.NumberColumn(format="%d"),
    "Unpaid Bills": st.column_config.NumberColumn(format="%d"),
    "Bill Amount": _num_cfg(), "Paid": _num_cfg(), "Pending": _num_cfg(),
    "On Hold": _num_cfg(), "On Account": _num_cfg(),
    "Days Pending": _num_cfg(), "Days Incl. Hold": _num_cfg(),
    "Oldest Unpaid Bill": _date_cfg(), "Cleared Till": _date_cfg(),
    "Pending From": _date_cfg(), "Last Payment": _date_cfg(),
}
BILL_CFG = {
    "MR Date": _date_cfg(), "Due Date": _date_cfg(), "Last Payment": _date_cfg(),
    "Bill Amount": _num_cfg(), "Paid": _num_cfg(), "Pending": _num_cfg(),
    "FIFO Pending": st.column_config.NumberColumn(
        "Unpaid (oldest-first)", format="%.0f",
        help="Still unpaid when the payments made are set against the oldest bills first, "
             "all companies together (an indication). Blank for bills on hold."),
}


def _csv(df: pd.DataFrame, name: str, key: str) -> None:
    st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8"),
                       file_name=name, mime="text/csv", key=key)


def _workbook(summary, keyed, oa, today) -> bytes:
    """One Excel file: supplier dues, company-wise for every supplier, unpaid bills."""
    comp = []
    for skey, name in zip(summary["skey"], summary["Jute Supplier"]):
        cs = S.company_summary(keyed, oa, skey, today)
        cs = cs[cs["Company"] != "All companies"].copy()
        cs.insert(0, "Jute Supplier", name)
        comp.append(cs)
    bill_frames = []
    for skey, name in zip(summary["skey"], summary["Jute Supplier"]):
        b = S.supplier_bills(keyed, oa, skey)
        b = b[b["Pending"] > EPS].copy()
        b.insert(0, "Jute Supplier", name)
        bill_frames.append(b)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        summary.drop(columns=["skey"], errors="ignore").to_excel(
            xw, sheet_name="Supplier dues", index=False)
        (pd.concat(comp) if comp else pd.DataFrame()).to_excel(
            xw, sheet_name="Company-wise", index=False)
        (pd.concat(bill_frames) if bill_frames else pd.DataFrame()).to_excel(
            xw, sheet_name="Bills", index=False)
    return buf.getvalue()


def _tab_suppliers(shown: pd.DataFrame, period_cols: list) -> None:
    if shown.empty:
        st.info("No suppliers for this filter.")
        return
    cols = [c for c in SUPPLIER_ORDER if c in shown.columns] + period_cols
    event = st.dataframe(
        shown[["skey"] + cols], hide_index=True, width="stretch",
        on_select="rerun", selection_mode="single-row", key="ps_grid",
        column_config=_supplier_config(period_cols))
    st.caption(f"Total of the {len(shown)} suppliers shown: pending "
               f"{_money(shown['Pending'].sum())}, on hold {_money(shown['On Hold'].sum())}, "
               f"free to pay {_money(shown['Free To Pay'].sum())}, "
               f"on account {_money(shown['On Account'].sum())}. "
               "Tick a row to see that supplier company-wise.")
    _csv(shown[cols], "jute_supplier_dues.csv", "ps_csv_suppliers")
    picked = list(getattr(getattr(event, "selection", None), "rows", []) or [])
    if picked:
        skey = shown.iloc[picked[0]]["skey"]
        if skey != st.session_state.get("ps_last_pick"):
            st.session_state["ps_last_pick"] = skey
            st.session_state["ps_supplier"] = skey
    else:
        st.session_state["ps_last_pick"] = None


def _tab_company(summary, keyed, oa, months_from, today) -> None:
    if summary.empty:
        st.info("No suppliers.")
        return
    names = dict(zip(summary["skey"], summary["Jute Supplier"]))
    options = list(names)
    _choose("ps_supplier", options, options[0])
    skey = st.selectbox("Jute supplier", options=options, key="ps_supplier",
                        format_func=lambda k: names.get(k, str(k)))
    cs = S.company_summary(keyed, oa, skey, today)
    total = cs[cs["Company"] == "All companies"]
    if not total.empty:
        t = total.iloc[0]
        k = st.columns(6)
        k[0].metric("Pending", _money(t["Pending"]))
        k[1].metric("On hold", _money(t["On Hold"]))
        k[2].metric("On account", _money(t["On Account"]))
        k[3].metric("Days pending", "-" if pd.isna(t["Days Pending"]) else f"{t['Days Pending']:.0f}")
        k[4].metric("Days incl. hold",
                    "-" if pd.isna(t["Days Incl. Hold"]) else f"{t['Days Incl. Hold']:.0f}")
        cleared = t["Cleared Till"]
        k[5].metric("Cleared till", "-" if pd.isna(cleared) else cleared.strftime("%d-%m-%Y"))
    st.dataframe(cs, hide_index=True, width="stretch", column_config=COMPANY_CFG)
    _csv(cs, "jute_supplier_company_wise.csv", "ps_csv_company")

    mine = keyed[keyed["skey"] == skey]
    pm = S.period_matrix(mine, months_from, by="Step 1 Co")
    if not pm.empty:
        st.markdown("**Pending by company and month**")
        pm = (pm.replace(0.0, float("nan")).reset_index()
              .rename(columns={pm.index.name or "index": "Company"}))
        st.dataframe(pm, hide_index=True, width="stretch",
                     column_config={c: _num_cfg() for c in pm.columns if c != "Company"})

    st.markdown("**Bills**")
    bills = S.supplier_bills(keyed, oa, skey)
    companies = [ALL] + sorted(c for c in bills["Step 1 Co"].dropna().unique() if c)
    _choose("ps_bill_co", companies, ALL)
    _seed("ps_unpaid", True)
    c1, c2 = st.columns([2, 1])
    with c1:
        co = st.selectbox("Company", options=companies, key="ps_bill_co")
    with c2:
        unpaid = st.checkbox("Unpaid only", key="ps_unpaid")
    if co != ALL:
        bills = bills[bills["Step 1 Co"] == co]
    if unpaid:
        bills = bills[bills["Pending"] > EPS]
    if bills.empty:
        st.info("No bills for this filter.")
        return
    st.dataframe(bills, hide_index=True, width="stretch", column_config=BILL_CFG)
    _csv(bills, "jute_supplier_bills.csv", "ps_csv_bills")


def _tab_others(rows, master, extras, user_id) -> None:
    """The confirmation list. party_mst holds one record per company for the
    same party, so the list is one row per party NAME + sheet name
    (S.group_suggestions); a confirmation is saved for every party record of
    that row."""
    party_map = extras.get("party_map") or {}
    history = extras.get("party_history")
    if history is None:
        history = pd.DataFrame(columns=["party_id", "jute_supplier_id", "n"])
    sug = S.group_suggestions(S.suggest_party_suppliers(rows, master, history, party_map))
    st.caption("These parties have bills that do not name a real jute supplier ('others'). "
               "The suggestion comes from the name the payments sheet used for the party "
               "(else the party's own name or its earlier MRs). It is only a suggestion: a "
               "party moves out of 'others' once an admin confirms it here.")
    if sug.empty:
        st.info("Nothing to match: every bill already has a jute supplier.")
        return
    _seed("ps_map_dues", True)
    c1, c2 = st.columns([1, 3])
    with c1:
        only_dues = st.checkbox("Only parties with dues", key="ps_map_dues")
    if only_dues:
        sug = sug[sug["Pending"] > EPS].reset_index(drop=True)
    done = int((sug["Confirmed"] != "").sum())
    with c2:
        st.caption(f"{len(sug)} parties, {_money(sug['Pending'].sum())} pending; "
                   f"{done} confirmed, {len(sug) - done} waiting.")
    if sug.empty:
        st.info("No party under 'others' has dues.")
        return
    show_cols = ["Party", "Companies", "Bills", "Unpaid Bills", "Pending", "Sheet Names",
                 "Suggested", "Basis", "Confirmed"]
    cfg = {"Pending": _num_cfg(), "Bills": st.column_config.NumberColumn(format="%d"),
           "Unpaid Bills": st.column_config.NumberColumn(format="%d"),
           "Sheet Names": st.column_config.TextColumn(
               help="The name the payments sheet used for this party, with the number of bills"),
           "Confirmed": st.column_config.TextColumn(help="What is saved now")}
    _csv(sug[show_cols], "party_supplier_suggestions.csv", "ps_csv_sug")

    if not (auth.is_admin() and extras.get("map_ready")):
        st.dataframe(sug[show_cols], hide_index=True, width="stretch", column_config=cfg)
        if not extras.get("map_ready"):
            st.info("Saving the confirmed list is not switched on yet (the list table is not "
                    "created on this database).")
        else:
            st.caption("Only a transfer admin can confirm.")
        return

    real = {i: str(n).strip() for i, n in master.items() if i not in S.others_ids(master)}
    id_of = {n: i for i, n in real.items()}
    # a row is known by its party records, never by its position in the grid
    gids = [",".join(str(i) for i in ids) for ids in sug["party_ids"]]
    confirmed = dict(zip(gids, (str(c or "").strip() for c in sug["Confirmed"])))
    # the editor remembers edits by row position: forget them when the rows change
    sig = "|".join(gids)
    if st.session_state.get("ps_map_sig") not in (None, sig):
        st.session_state.pop("ps_map_editor", None)
    st.session_state["ps_map_sig"] = sig
    fills = st.session_state.setdefault("ps_fills", {})
    options = ["", "others"] + sorted((set(real.values()) | set(confirmed.values()))
                                      - {"", "others"})
    base = sug[show_cols].copy()
    base.insert(0, "gid", gids)
    base["Confirm as"] = [fills.get(g, confirmed[g]) for g in gids]
    b1, b2 = st.columns(2)
    with b1:
        if st.button("Fill blanks with suggestion", key="ps_fill",
                     help="Puts the suggestion in 'Confirm as' where nothing is confirmed "
                          "yet. Nothing is saved until Save."):
            for g, c, sgst in zip(gids, sug["Confirmed"], sug["Suggested"]):
                if not str(c or "").strip() and str(sgst or "").strip() in options:
                    fills[g] = str(sgst).strip()
            st.session_state.pop("ps_map_editor", None)
            st.rerun()
    edited = st.data_editor(
        base, hide_index=True, width="stretch", key="ps_map_editor",
        disabled=show_cols,
        column_config={**cfg, "gid": None,
                       "Confirm as": st.column_config.SelectboxColumn(
                           "Confirm as", options=options,
                           help="Pick the jute supplier this party belongs to, or 'others' "
                                "to keep it there")})
    changes = {}
    for g, new in zip(edited["gid"], edited["Confirm as"]):
        new = str(new or "").strip()
        if new == confirmed.get(g, ""):
            continue
        if new == "":
            value = supplier_map.CLEAR
        elif new == "others":
            value = None
        elif new in id_of:
            value = id_of[new]
        else:
            continue
        for party_id in g.split(","):
            changes[int(party_id)] = value
    rows_changed = len({g for g in gids if any(int(i) in changes for i in g.split(","))})
    with b2:
        pressed = st.button(f"Save ({rows_changed} changed)", type="primary", key="ps_save",
                            disabled=not changes)
    if pressed:
        try:
            supplier_map.save_map(changes, user_id)
        except Exception as e:
            st.error(f"Could not save the list: {e}")
            return
        st.session_state["ps_flash"] = f"Saved {rows_changed} party confirmation(s)."
        st.session_state.pop("ps_map_editor", None)
        st.session_state["ps_fills"] = {}
        clear_cache()
        st.rerun()


def payment_summary_page() -> None:
    st.title("Jute Supplier Dues")
    st.caption("What is pending to each jute supplier (the broker), month by month, who is on "
               "hold and how long payments have been waiting. Days are worked oldest-first "
               "across all companies, as an indication only; nothing is changed in accounts. "
               "Bills from before 1 Apr 2025 are known only where they were still unpaid on "
               "that day.")
    flash = st.session_state.pop("ps_flash", None)
    if flash:
        st.success(flash)

    today = date.today()
    try:
        data = load_bills(H.ACCOUNTS_START, date(today.year + 1, 12, 31))
        rows = H.tracker_rows(data, today, H.ACCOUNTS_START, drop_paid_opening=False)
    except Exception as e:
        st.error(f"Could not load the bills: {e}")
        return
    try:
        extras = load_summary_extras()
    except Exception as e:
        st.error(f"Could not load the supplier list: {e}")
        return
    mdf = extras.get("master")
    master = ({int(i): str(n or "") for i, n in zip(mdf["supplier_id"], mdf["supplier_name"])}
              if mdf is not None and not mdf.empty else {})
    party_map = extras.get("party_map") or {}

    fys = _fy_options(today, rows)
    _seed("ps_search", "")
    _seed("ps_split", False)
    _choose("ps_show", [SHOW_DUES, SHOW_ALL], SHOW_DUES)
    _choose("ps_hold", [HOLD_ALL, HOLD_ON, HOLD_OFF], HOLD_ALL)
    _choose("ps_fy", fys, fys[0])
    top = st.columns([2, 1, 1, 1, 1, 1])
    with top[0]:
        search = st.text_input("Search supplier", key="ps_search").strip().lower()
    with top[1]:
        show = st.selectbox("Show", [SHOW_DUES, SHOW_ALL], key="ps_show")
    with top[2]:
        hold = st.selectbox("Hold", [HOLD_ALL, HOLD_ON, HOLD_OFF], key="ps_hold")
    with top[3]:
        fy = st.selectbox("Month-wise from", fys, key="ps_fy", format_func=_fy_text,
                          help="Months from the start of this year; earlier years are one "
                               "column each.")
    with top[4]:
        split = st.checkbox("Split 'others' by party", key="ps_split")
    with top[5]:
        if st.button("Refresh", key="ps_refresh"):
            clear_cache()
            st.rerun()

    keyed = S.add_supplier_key(rows, master, party_map, split)
    holds = H.holds_map(data.get("holds"))
    oa_raw = extras.get("on_account")
    if oa_raw is None:
        oa_raw = pd.DataFrame(columns=["co_id", "co_prefix", "party_id", "supplier", "amount",
                                       "bill_date"])
    oa = S.assign_on_account(oa_raw, keyed, master, party_map, split, holds)
    summary = S.supplier_summary(keyed, oa, today)
    months_from = date(int(fy), 4, 1)
    pm = S.period_matrix(keyed, months_from)
    period_cols = list(pm.columns)
    # a month with nothing pending stays blank (not 0): easier to read on a phone
    full = summary.merge(pm.replace(0.0, float("nan")), left_on="skey", right_index=True,
                         how="left")

    shown = full
    if show == SHOW_DUES:
        shown = shown[(shown["Pending"] > EPS) | (shown["On Account"] > EPS)]
    if hold == HOLD_ON:
        shown = shown[shown["Hold"] != ""]
    elif hold == HOLD_OFF:
        shown = shown[shown["Hold"] == ""]
    if search:
        shown = shown[shown["Jute Supplier"].str.lower().str.contains(search, regex=False)]
    shown = shown.reset_index(drop=True)

    k = st.columns(5)
    k[0].metric("Suppliers with dues", f"{int((shown['Pending'] > EPS).sum()):,}")
    k[1].metric("Pending", _money(shown["Pending"].sum()))
    k[2].metric("On hold", _money(shown["On Hold"].sum()))
    k[3].metric("Free to pay", _money(shown["Free To Pay"].sum()))
    k[4].metric("On account", _money(shown["On Account"].sum()))

    tabs = st.tabs(["Supplier dues", "Company-wise", "Others → supplier"])
    with tabs[0]:
        _tab_suppliers(shown, period_cols)
        if st.button("Prepare Excel file", key="ps_xlsx_prepare"):
            try:
                st.session_state["ps_xlsx"] = _workbook(
                    shown[["skey"] + [c for c in SUPPLIER_ORDER if c in shown.columns]
                          + period_cols],
                    keyed, oa, today)
            except ImportError:
                st.info("The Excel download needs the 'openpyxl' package on this server.")
            except Exception as e:
                st.error(f"Could not build the Excel file: {e}")
        if st.session_state.get("ps_xlsx"):
            st.download_button(
                "Download Excel", st.session_state["ps_xlsx"],
                file_name="jute_supplier_dues.xlsx", key="ps_xlsx_download",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    with tabs[1]:
        _tab_company(summary.sort_values("Pending", ascending=False)
                     .reset_index(drop=True), keyed, oa, months_from, today)
    with tabs[2]:
        _tab_others(rows, master, extras, int(st.session_state.get("user_id") or 0))
