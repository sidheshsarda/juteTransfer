"""Company-wise Sales / Purchase / Net P&L dashboard (raw jute only).

For a selected financial year (April-March), shows per-company monthly totals
for sales, purchases, and net P&L. Purchases come from `jute_mr.net_total`;
sales come from `sales_invoice` where `invoice_type = 5`, net of claim amounts,
attributed to the seller (branch_id -> co_id).
"""

from datetime import date, datetime
from typing import List, Tuple

import pandas as pd
import streamlit as st

from ..queries import (
    get_company_wise_purchases_by_month,
    get_company_wise_sales_by_month,
)


MONTH_ORDER = [4, 5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3]
MONTH_LABELS = ["Apr", "May", "Jun", "Jul", "Aug", "Sep",
                "Oct", "Nov", "Dec", "Jan", "Feb", "Mar"]


# ---------------------------------------------------------------------------
# Financial year helpers (pure logic — no DB access)
# ---------------------------------------------------------------------------

def _date_to_fy_label(d: date) -> str:
    """Return the FY label (e.g. '25-26') that contains the given date."""
    if d.month >= 4:
        start_year = d.year
    else:
        start_year = d.year - 1
    return f"{start_year % 100:02d}-{(start_year + 1) % 100:02d}"


def _fy_label_to_bounds(label: str) -> Tuple[date, date]:
    """Convert '25-26' -> (2025-04-01, 2026-03-31)."""
    start_yy, _end_yy = label.split("-")
    start_year = 2000 + int(start_yy)
    return date(start_year, 4, 1), date(start_year + 1, 3, 31)


def _current_and_previous_fy_labels() -> List[str]:
    """Return [current_fy, previous_fy] as labels like '25-26'."""
    today = datetime.now().date()
    current = _date_to_fy_label(today)
    start_year = 2000 + int(current.split("-")[0])
    prev_start = start_year - 1
    previous = f"{prev_start % 100:02d}-{(prev_start + 1) % 100:02d}"
    return [current, previous]


# ---------------------------------------------------------------------------
# Data fetch (cached) + pivot
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def _load_fy_data(fy_label: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch sales and purchase long-format dataframes for a FY label."""
    fy_start, fy_end = _fy_label_to_bounds(fy_label)
    purchases = get_company_wise_purchases_by_month(fy_start, fy_end)
    sales = get_company_wise_sales_by_month(fy_start, fy_end)
    return sales, purchases


def _pivot_to_fy_months(df: pd.DataFrame, value_col: str) -> pd.DataFrame:
    """Pivot long-form (co_id, co_name, month, value) into a wide FY grid.

    Rows = companies, columns = Apr..Mar + 'FY Total', plus a 'Total' row at
    the bottom. Missing cells become 0.0.
    """
    if df is None or df.empty:
        wide = pd.DataFrame(columns=["Company"] + MONTH_LABELS + ["FY Total"])
        wide.loc[0] = ["Total"] + [0.0] * (len(MONTH_LABELS) + 1)
        return wide

    pivot = df.pivot_table(
        index=["co_id", "co_name"],
        columns="month",
        values=value_col,
        aggfunc="sum",
        fill_value=0.0,
    )
    # Ensure all 12 months are present, ordered Apr..Mar
    pivot = pivot.reindex(columns=MONTH_ORDER, fill_value=0.0)
    pivot.columns = MONTH_LABELS
    pivot["FY Total"] = pivot.sum(axis=1)

    # Flatten index and rename to Company
    pivot = pivot.reset_index()
    pivot = pivot.drop(columns=["co_id"])
    pivot = pivot.rename(columns={"co_name": "Company"})
    pivot = pivot.sort_values("Company").reset_index(drop=True)

    # Append a Total row
    total_row = {"Company": "Total"}
    for col in MONTH_LABELS + ["FY Total"]:
        total_row[col] = float(pivot[col].sum())
    pivot = pd.concat([pivot, pd.DataFrame([total_row])], ignore_index=True)

    return pivot


def _align_for_pnl(sales_wide: pd.DataFrame, purchases_wide: pd.DataFrame) -> pd.DataFrame:
    """Compute Net P&L = Sales - Purchases across the union of companies."""
    num_cols = MONTH_LABELS + ["FY Total"]

    # Drop the Total row from each; we'll recompute it after the subtraction.
    s = sales_wide[sales_wide["Company"] != "Total"].set_index("Company")[num_cols]
    p = purchases_wide[purchases_wide["Company"] != "Total"].set_index("Company")[num_cols]

    companies = sorted(set(s.index).union(set(p.index)))
    s = s.reindex(companies, fill_value=0.0)
    p = p.reindex(companies, fill_value=0.0)

    pnl = s.sub(p, fill_value=0.0)
    pnl = pnl.reset_index()

    total_row = {"Company": "Total"}
    for col in num_cols:
        total_row[col] = float(pnl[col].sum())
    pnl = pd.concat([pnl, pd.DataFrame([total_row])], ignore_index=True)

    return pnl


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _number_column_config() -> dict:
    return {
        col: st.column_config.NumberColumn(format="%.2f")
        for col in MONTH_LABELS + ["FY Total"]
    }


def _render_grid(title: str, df: pd.DataFrame) -> None:
    st.subheader(title)
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config=_number_column_config(),
        height=min(600, 80 + len(df) * 35),
    )


def company_pl_dashboard_page() -> None:
    """Render the Company-wise Sales / Purchase / P&L dashboard."""
    st.title("🏢 Company P&L (Raw Jute)")
    st.caption(
        "Monthly sales, purchases, and net P&L by company for a financial year. "
        "Purchases are from gate entries (jute_mr). Sales are raw-jute invoices "
        "(invoice_type = 5) net of claims, attributed to the selling company."
    )

    fy_options = _current_and_previous_fy_labels()
    fy_label = st.selectbox("Financial Year", options=fy_options, index=0)

    sales_df, purchases_df = _load_fy_data(fy_label)

    sales_wide = _pivot_to_fy_months(sales_df, "net_sales")
    purchases_wide = _pivot_to_fy_months(purchases_df, "net_purchases")
    pnl_wide = _align_for_pnl(sales_wide, purchases_wide)

    # Extract totals for KPI cards
    def _total_for(df: pd.DataFrame) -> float:
        total_rows = df[df["Company"] == "Total"]
        if total_rows.empty:
            return 0.0
        return float(total_rows.iloc[0]["FY Total"])

    total_sales = _total_for(sales_wide)
    total_purchases = _total_for(purchases_wide)
    total_pnl = total_sales - total_purchases

    if (sales_df is None or sales_df.empty) and (purchases_df is None or purchases_df.empty):
        st.info(f"No data for FY {fy_label}.")

    col1, col2, col3 = st.columns(3)
    col1.metric("Total Sales", f"{total_sales:,.2f}")
    col2.metric("Total Purchases", f"{total_purchases:,.2f}")
    col3.metric("Net P&L", f"{total_pnl:,.2f}")

    st.markdown("---")

    _render_grid(f"Sales — FY {fy_label}", sales_wide)
    _render_grid(f"Purchases — FY {fy_label}", purchases_wide)
    _render_grid(f"Net P&L — FY {fy_label}", pnl_wide)
