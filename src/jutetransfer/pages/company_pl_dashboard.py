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
    get_company_wise_marked_stock,
    get_company_wise_purchases_by_month,
    get_company_wise_sales_by_month,
    get_company_wise_unsold_stock,
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
def _load_fy_data(fy_label: str) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Fetch sales, purchase, and unsold-stock dataframes for a FY label."""
    fy_start, fy_end = _fy_label_to_bounds(fy_label)
    purchases = get_company_wise_purchases_by_month(fy_start, fy_end)
    sales = get_company_wise_sales_by_month(fy_start, fy_end)
    stock = get_company_wise_unsold_stock(fy_start, fy_end)
    marked = get_company_wise_marked_stock(fy_start, fy_end)
    return sales, purchases, stock, marked


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


def _stock_to_wide(df: pd.DataFrame) -> pd.DataFrame:
    """Return stock wide-form: Company | Stock, with a Total row at bottom."""
    if df is None or df.empty:
        wide = pd.DataFrame(columns=["Company", "Stock"])
        wide.loc[0] = ["Total", 0.0]
        return wide

    wide = (
        df.rename(columns={"co_name": "Company", "stock_value": "Stock"})
          [["Company", "Stock"]]
          .sort_values("Company")
          .reset_index(drop=True)
    )
    wide["Stock"] = wide["Stock"].astype(float)
    total = pd.DataFrame([{"Company": "Total", "Stock": float(wide["Stock"].sum())}])
    return pd.concat([wide, total], ignore_index=True)


def _combine_stock(chain_df: pd.DataFrame, marked_df: pd.DataFrame) -> pd.DataFrame:
    """Sum chain-stock and marked-stock per company into one long-form df
    (co_id, co_name, stock_value)."""
    frames = [d for d in (chain_df, marked_df)
              if d is not None and not d.empty]
    if not frames:
        return chain_df if chain_df is not None else marked_df
    combined = pd.concat(frames, ignore_index=True)[["co_id", "co_name", "stock_value"]]
    return combined.groupby(["co_id", "co_name"], as_index=False)["stock_value"].sum()


def _align_for_pnl(
    sales_wide: pd.DataFrame,
    purchases_wide: pd.DataFrame,
    stock_wide: pd.DataFrame,
) -> pd.DataFrame:
    """Compute Adjusted P&L = (Sales - Purchases) + Stock across the union of companies.

    Monthly columns reflect (sales - purchases) per month. Two extra columns
    are appended on the right: 'Stock' (a single FY-scoped value per company)
    and 'Adjusted P&L' (= FY Total + Stock).
    """
    num_cols = MONTH_LABELS + ["FY Total"]

    # Drop the Total row from each; we'll recompute it after the subtraction.
    s = sales_wide[sales_wide["Company"] != "Total"].set_index("Company")[num_cols]
    p = purchases_wide[purchases_wide["Company"] != "Total"].set_index("Company")[num_cols]
    stk = (
        stock_wide[stock_wide["Company"] != "Total"]
        .set_index("Company")["Stock"]
    )

    companies = sorted(set(s.index).union(set(p.index)).union(set(stk.index)))
    s = s.reindex(companies, fill_value=0.0)
    p = p.reindex(companies, fill_value=0.0)
    stk = stk.reindex(companies, fill_value=0.0)

    pnl = s.sub(p, fill_value=0.0)
    pnl["Stock"] = stk
    pnl["Adjusted P&L"] = pnl["FY Total"] + pnl["Stock"]
    pnl = pnl.reset_index()

    total_row = {"Company": "Total"}
    for col in num_cols + ["Stock", "Adjusted P&L"]:
        total_row[col] = float(pnl[col].sum())
    pnl = pd.concat([pnl, pd.DataFrame([total_row])], ignore_index=True)

    return pnl


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _number_column_config(cols) -> dict:
    return {col: st.column_config.NumberColumn(format="%.2f") for col in cols}


def _render_grid(title: str, df: pd.DataFrame, numeric_cols) -> None:
    st.subheader(title)
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config=_number_column_config(numeric_cols),
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

    sales_df, purchases_df, stock_df, marked_df = _load_fy_data(fy_label)

    sales_wide = _pivot_to_fy_months(sales_df, "net_sales")
    purchases_wide = _pivot_to_fy_months(purchases_df, "net_purchases")
    stock_wide = _stock_to_wide(_combine_stock(stock_df, marked_df))
    marked_wide = _stock_to_wide(marked_df)
    pnl_wide = _align_for_pnl(sales_wide, purchases_wide, stock_wide)

    # Extract totals for KPI cards
    def _total_for(df: pd.DataFrame, col: str = "FY Total") -> float:
        total_rows = df[df["Company"] == "Total"]
        if total_rows.empty:
            return 0.0
        return float(total_rows.iloc[0][col])

    total_sales = _total_for(sales_wide)
    total_purchases = _total_for(purchases_wide)
    total_stock = _total_for(stock_wide, "Stock")
    total_adj_pnl = total_sales - total_purchases + total_stock

    if (
        (sales_df is None or sales_df.empty)
        and (purchases_df is None or purchases_df.empty)
        and (stock_df is None or stock_df.empty)
        and (marked_df is None or marked_df.empty)
    ):
        st.info(f"No data for FY {fy_label}.")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Sales", f"{total_sales:,.2f}")
    col2.metric("Total Purchases", f"{total_purchases:,.2f}")
    col3.metric("Current Stock", f"{total_stock:,.2f}")
    col4.metric("Adjusted P&L", f"{total_adj_pnl:,.2f}")

    st.markdown("---")

    month_cols = MONTH_LABELS + ["FY Total"]
    pnl_cols = month_cols + ["Stock", "Adjusted P&L"]

    _render_grid(f"Sales — FY {fy_label}", sales_wide, month_cols)
    _render_grid(f"Purchases — FY {fy_label}", purchases_wide, month_cols)
    _render_grid(f"Net P&L — FY {fy_label}", pnl_wide, pnl_cols)
    _render_grid(f"Current Stock — FY {fy_label}", stock_wide, ["Stock"])
    _render_grid(f"Marked Godown Stock — FY {fy_label}", marked_wide, ["Stock"])
