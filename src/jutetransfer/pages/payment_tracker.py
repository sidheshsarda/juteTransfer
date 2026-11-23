"""Jute Payment Tracker: every supplier bill with the mill where the lorry's
gate entry was done, the step-1 company that bought it, and what is paid and
pending. Replaces the 'Entry sheet' of the payments workbook.

Bills come from jute_mr, paid / pending from the accounts module (reads only);
opening bills and suppliers on payment hold come from the accounts tables too.
A payment is NOT written by this app: an admin ticks bills of one company and
one supplier, fills the popup, and VoW creates the payment voucher through
its own function (vow_api -> VoW's jute-payments integration API). The
voucher is a Draft that accounts approves in VoW; until then the bills show
as 'Payment proposed'. Shaping is in payment_tracker_helpers (pure), reads in
payment_queries.
"""

from datetime import date

import pandas as pd
import streamlit as st

from .. import payment_tracker_helpers as H
from .. import vow_api
from ..auth import is_admin
from ..payment_queries import clear_cache, load_bills

ALL = "All"
ALL_YEARS = 0      # the period box's "every year" choice
# Bills dated before this are not read from the MRs: what was still owed on
# them is the opening bills entered in VoW (otherwise every old MR, paid long
# ago outside the system, would show as pending).
ACCOUNTS_START = date(2026, 4, 1)


def _money(value: float) -> str:
    return f"₹{value:,.0f}"


def _fy_options(today: date) -> list:
    this_fy = today.year if today.month >= 4 else today.year - 1
    return [ALL_YEARS] + [y for y in range(this_fy, ACCOUNTS_START.year - 1, -1)]


def _fy_text(fy: int) -> str:
    return "All years" if fy == ALL_YEARS else f"{fy}-{(fy + 1) % 100:02d}"


def _pick(label: str, values, key: str) -> str:
    options = [ALL] + sorted(v for v in set(values) if v)
    if st.session_state.get(key) not in options:
        st.session_state[key] = ALL
    return st.selectbox(label, options=options, key=key)


@st.dialog("Pay bills", width="large")
def _pay_dialog(picked: pd.DataFrame, user_id: int) -> None:
    """The payment popup: the fields VoW's own Pay Bills form asks for. Saving
    sends them to VoW, which creates the Draft payment voucher."""
    first = picked.iloc[0]
    co_id, party_id = int(first["step1_co_id"]), int(first["party_id"])
    st.markdown(f"**{first['Step 1 Co']}** pays **{first['Supplier']}**")
    try:
        options = vow_api.payment_options(co_id)
        live = {int(b["acc_bill_ref_id"]): b for b in vow_api.open_bills(co_id, party_id)}
    except vow_api.VowApiError as e:
        st.error(str(e))
        return
    if options.get("activated") is False:
        st.error(f"Accounting is not switched on for {first['Step 1 Co']} in VoW.")
        return
    branches = {int(b["branch_id"]): b["branch_name"] for b in options.get("branches", [])}
    ledgers = {int(l["acc_ledger_id"]): l["ledger_name"] for l in options.get("bank_ledgers", [])}
    if not ledgers:
        st.error(f"{first['Step 1 Co']} has no bank or cash account in VoW accounts. Add one "
                 "there first.")
        return

    c1, c2 = st.columns(2)
    with c1:
        branch_id = st.selectbox("Branch", options=list(branches), key="pay_branch",
                                 format_func=lambda b: branches.get(b, str(b)))
        bank_ledger_id = st.selectbox("Paid from (bank / cash account)", options=list(ledgers),
                                      key="pay_ledger",
                                      format_func=lambda l: ledgers.get(l, str(l)))
        instrument_no = st.text_input("UTR / cheque no.", key="pay_instr", max_chars=50)
    with c2:
        if "pay_date" not in st.session_state:
            st.session_state["pay_date"] = date.today()
        voucher_date = st.date_input("Payment date", key="pay_date")
        pay_mode = st.selectbox("Pay mode", key="pay_mode",
                                options=options.get("pay_modes") or
                                ["NEFT", "RTGS", "CHEQUE", "UPI", "CASH", "OTHER"])
        has_instr_date = st.checkbox("Cheque / UTR has its own date", key="pay_has_instr_date")
        instrument_date = (st.date_input("Cheque / UTR date", key="pay_instr_date")
                           if has_instr_date else None)

    # What accounts says can still be paid on each bill right now.
    held_live = [b for i in picked["acc_bill_ref_id"]
                 if (b := live.get(int(i))) and b.get("on_hold")]
    if held_live:
        st.error(f"{first['Supplier']} is on hold for payment"
                 + (f" ({held_live[0].get('hold_note')})" if held_live[0].get("hold_note") else "")
                 + ". Release the hold in VoW (Accounting > Pay Bills) first.")
    grid = picked[["acc_bill_ref_id", "Bill", "MR No", "MR Date", "Bill Amount", "Pending"]].copy()
    grid["Can pay"] = [float((live.get(int(i)) or {}).get("allocatable_amount") or 0)
                       for i in grid["acc_bill_ref_id"]]
    grid["Pay now"] = grid["Can pay"]
    edited = st.data_editor(
        grid, hide_index=True, width="stretch", key="pay_grid",
        disabled=["acc_bill_ref_id", "Bill", "MR No", "MR Date", "Bill Amount", "Pending", "Can pay"],
        column_config={
            "acc_bill_ref_id": None,
            "MR Date": st.column_config.DateColumn(format="DD-MM-YYYY"),
            "Bill Amount": st.column_config.NumberColumn(format="%.2f"),
            "Pending": st.column_config.NumberColumn(format="%.2f"),
            "Can pay": st.column_config.NumberColumn(format="%.2f"),
            "Pay now": st.column_config.NumberColumn(format="%.2f", min_value=0.0),
        })
    over = edited[edited["Pay now"].fillna(0) > edited["Can pay"] + 0.005]
    total = float(edited["Pay now"].fillna(0).sum())
    st.markdown(f"**Payment amount: ₹{total:,.2f}**")
    if not over.empty:
        st.warning("Pay now is more than can be paid on: " + ", ".join(over["MR No"]))
    narration = st.text_area("Narration", key="pay_narration", max_chars=500,
                             placeholder=H.default_narration(first["Supplier"],
                                                             list(picked["MR No"])))
    st.caption("This creates a Draft payment voucher in VoW accounts. Pending reduces when "
               "accounts approves it there.")

    if st.button("Create payment voucher", type="primary", key="pay_save",
                 disabled=total <= 0 or not over.empty or bool(held_live)):
        form = dict(branch_id=branch_id, bank_ledger_id=bank_ledger_id, voucher_date=voucher_date,
                    pay_mode=pay_mode, instrument_no=instrument_no, instrument_date=instrument_date,
                    narration=narration or H.default_narration(first["Supplier"],
                                                               list(picked["MR No"])))
        pay_now = {int(r["acc_bill_ref_id"]): float(r["Pay now"] or 0)
                   for r in edited.to_dict("records")}
        try:
            payload = H.payment_payload(co_id, party_id, form, pay_now, user_id)
            result = vow_api.create_payment(payload)
        except (ValueError, vow_api.VowApiError) as e:
            st.error(f"The payment was not created: {e}")
            return
        st.session_state["pt_flash"] = (
            f"Payment voucher {result.get('voucher_no') or result.get('voucher_id')} created for "
            f"₹{total:,.2f} to {first['Supplier']} ({result.get('status_name') or 'Draft'}). "
            "Approve it in VoW accounts.")
        for key in [k for k in st.session_state if isinstance(k, str) and k.startswith("pay_")]:
            st.session_state.pop(key, None)
        st.session_state.pop("pt_grid", None)
        clear_cache()
        st.rerun()


def payment_tracker_page() -> None:
    st.title("Jute Payment Tracker")
    st.caption("One row per supplier bill: the mill where the gate entry was done, the company "
               "that bought the lorry (step 1), the bill and what is paid and pending.")
    flash = st.session_state.pop("pt_flash", None)
    if flash:
        st.success(flash)

    today = date.today()
    top = st.columns([1, 1, 1, 1, 1])
    with top[0]:
        fys = _fy_options(today)
        if st.session_state.get("pt_fy") not in fys:
            st.session_state["pt_fy"] = fys[0]
        fy = st.selectbox("Period", options=fys, key="pt_fy", format_func=_fy_text,
                          help="All years: every bill since accounts started, plus the "
                               "opening bills of earlier years. Pending bills show whatever "
                               "their year.")
    if fy == ALL_YEARS:
        date_from, date_to = ACCOUNTS_START, date(today.year + 1, 12, 31)
    else:
        date_from, date_to = H.fy_bounds(fy)
    try:
        data = load_bills(date_from, date_to)
    except Exception as e:
        st.error(f"Could not load the bills: {e}")
        return
    bills = H.pick_bills(data["mrs"], data["first_hop_of"])
    if not bills.empty:
        bills = bills.merge(data["acc"], on="jute_mr_id", how="left")
    holds = H.holds_map(data.get("holds"))
    rows = H.build_rows(bills, today, holds)
    opening = H.build_opening_rows(data.get("opening"), data.get("opening_mrs"), today, holds,
                                  accounts_start=ACCOUNTS_START)
    if not opening.empty and fy != ALL_YEARS:
        # a single year also shows what is still pending from the opening
        opening = opening[opening["Status"] != H.STATUS_PAID]
    # what was owed at the start of accounts comes first; an imported ref for an
    # MR accounts has not posted replaces that MR's 'Not in accounts' row
    rows = H.merge_rows(opening, rows)

    with top[1]:
        gate = _pick("Gate entry company", rows["Gate Entry Co"], "pt_gate")
    with top[2]:
        step1 = _pick("Step 1 company", rows["Step 1 Co"], "pt_step1")
    with top[3]:
        party = _pick("Party", rows["Party"], "pt_party")
    with top[4]:
        if st.session_state.get("pt_status") not in (ALL, "Unpaid") + H.STATUSES:
            st.session_state["pt_status"] = "Unpaid"
        status = st.selectbox("Status", options=[ALL, "Unpaid"] + list(H.STATUSES), key="pt_status")
    c1, c2, c2b, c2c, c3 = st.columns([3, 1, 1, 1, 1])
    with c1:
        search = st.text_input("Search supplier or MR no.", key="pt_search").strip().lower()
    with c2:
        only_overdue = st.checkbox("Overdue only", key="pt_overdue")
    with c2b:
        only_opening = st.checkbox("Opening / imported only", key="pt_opening")
    with c2c:
        hide_hold = st.checkbox("Hide on-hold", key="pt_hide_hold")
    with c3:
        if st.button("Refresh", key="pt_refresh"):
            clear_cache()
            st.rerun()

    shown = rows
    if gate != ALL:
        shown = shown[shown["Gate Entry Co"] == gate]
    if step1 != ALL:
        shown = shown[shown["Step 1 Co"] == step1]
    if party != ALL:
        shown = shown[shown["Party"] == party]
    if status == "Unpaid":
        shown = shown[shown["Status"] != H.STATUS_PAID]
    elif status != ALL:
        shown = shown[shown["Status"] == status]
    if only_overdue:
        shown = shown[shown["Overdue (days)"].notna()]
    if only_opening:
        shown = shown[shown["Bill"].isin(H.OPENING_KINDS)]
    if hide_hold:
        shown = shown[~shown["on_hold"]]
    if search:
        shown = shown[shown["Supplier"].str.lower().str.contains(search, regex=False)
                      | shown["MR No"].str.lower().str.contains(search, regex=False)]
    shown = shown.reset_index(drop=True)

    t = H.totals(shown)
    k = st.columns(5)
    k[0].metric("Bills", f"{t['bills']:,}")
    k[1].metric("Bill amount", _money(t["bill_amount"]))
    k[2].metric("Paid", _money(t["paid"]))
    k[3].metric("Pending", _money(t["pending"]))
    k[4].metric("Overdue", _money(t["overdue"]))

    held = shown[shown["on_hold"]]
    if not held.empty:
        st.caption(f"{len(held)} bills ({_money(float(held['Pending'].sum()))}) are on hold.")
    not_posted = int((shown["Status"] == H.STATUS_NOT_POSTED).sum())
    if not_posted:
        st.caption(f"{not_posted} of these bills are not in the accounts module yet, so they show "
                   "as fully pending. Payments appear here once the bill is posted in accounts.")
    if shown.empty:
        st.info("No bills for this filter.")
        return

    event = st.dataframe(
        shown[H.COLUMNS], hide_index=True, width="stretch",
        on_select="rerun", selection_mode="multi-row", key="pt_grid",
        column_config={
            "Bill Amount": st.column_config.NumberColumn(format="%.0f"),
            "Paid": st.column_config.NumberColumn(format="%.0f"),
            "Pending": st.column_config.NumberColumn(format="%.0f"),
            "Proposed": st.column_config.NumberColumn(
                format="%.0f", help="On payment vouchers waiting for approval in VoW"),
            "GE Date": st.column_config.DateColumn(format="DD-MM-YYYY"),
            "MR Date": st.column_config.DateColumn(format="DD-MM-YYYY"),
            "Due Date": st.column_config.DateColumn(format="DD-MM-YYYY"),
            "Last Payment": st.column_config.DateColumn(format="DD-MM-YYYY"),
        },
    )
    st.download_button("Download CSV", shown[H.COLUMNS].to_csv(index=False).encode("utf-8"),
                       file_name=f"jute_bills_{_fy_text(fy).replace(' ', '_').lower()}.csv",
                       mime="text/csv",
                       key="pt_csv")

    picked_idx = list(getattr(getattr(event, "selection", None), "rows", []) or [])
    if not picked_idx:
        st.caption("Tick bills of one company and one supplier to see what a payment would cover.")
        return
    picked = shown.iloc[picked_idx]
    problem = H.selection_problem(picked)
    st.subheader("Bills picked for payment")
    if problem:
        st.warning(problem)
        return
    first = picked.iloc[0]
    st.markdown(f"**{first['Step 1 Co']}** pays **{first['Supplier']}** "
                f"({len(picked)} bill(s)): **{_money(float(picked['Pending'].sum()))}** pending")
    st.dataframe(picked[["MR No", "MR Date", "Bill Amount", "Paid", "Pending", "Proposed",
                         "Due Date"]], hide_index=True, width="stretch")
    if not is_admin():
        st.caption("Only a transfer admin can create a payment.")
    elif not vow_api.configured():
        st.info("Payment entry is not switched on for this server yet (the link to VoW accounts "
                "is not set up).")
    elif st.button("Pay these bills", type="primary", key="pt_pay"):
        _pay_dialog(picked.reset_index(drop=True), int(st.session_state.get("user_id") or 0))
