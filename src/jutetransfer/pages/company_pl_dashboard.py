"""Company-wise Sales / Purchase / Net P&L dashboard (raw jute only).

For a selected financial year (April-March), shows per-company monthly totals
for sales and purchases, the opening and closing stock, and the adjusted P&L:

    Adjusted P&L = Sales - Purchases - Opening stock + Closing stock

Purchases come from `jute_mr.net_total`; sales come from `sales_invoice` where
`invoice_type = 5`, net of claim amounts, attributed to the seller (branch_id
-> co_id). Stock is the ERP's per-line balance evaluated on a date
(queries.get_company_wise_stock_as_of): opening = the day before the FY
starts, closing = the FY's last day, or today inside the running FY.
"""

from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple

import pandas as pd
import streamlit as st

from ..queries import (
    get_company_wise_purchases_by_month,
    get_company_wise_sales_by_month,
    get_company_wise_stock_as_of,
)


MONTH_ORDER = [4, 5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3]
MONTH_LABELS = ["Apr", "May", "Jun", "Jul", "Aug", "Sep",
                "Oct", "Nov", "Dec", "Jan", "Feb", "Mar"]
STOCK_COLS = ["Opening", "Closing"]


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


def _stock_as_of_dates(fy_label: str, today: Optional[date] = None) -> Tuple[date, date]:
    """(opening as-of, closing as-of) for a FY: the day before it starts, and
    its last day or today, whichever comes first."""
    fy_start, fy_end = _fy_label_to_bounds(fy_label)
    today = today or datetime.now().date()
    return fy_start - timedelta(days=1), min(fy_end, today)


# ---------------------------------------------------------------------------
# Data fetch (cached) + pivot
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def _load_fy_data(fy_label: str) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame,
                                          pd.DataFrame, pd.DataFrame]:
    """Fetch sales, purchases, opening stock, closing stock and the marked
    share of the closing stock for a FY label."""
    fy_start, fy_end = _fy_label_to_bounds(fy_label)
    opening_on, closing_on = _stock_as_of_dates(fy_label)
    purchases = get_company_wise_purchases_by_month(fy_start, fy_end)
    sales = get_company_wise_sales_by_month(fy_start, fy_end)
    opening = get_company_wise_stock_as_of(opening_on)
    closing = get_company_wise_stock_as_of(closing_on)
    marked = get_company_wise_stock_as_of(closing_on, transfer_mode=1)
    return sales, purchases, opening, closing, marked


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


def _stock_series(df: pd.DataFrame) -> pd.Series:
    """Long-form (co_id, co_name, stock_value) -> Series indexed by company name."""
    if df is None or df.empty:
        return pd.Series(dtype=float, name="stock_value")
    return df.groupby("co_name")["stock_value"].sum().astype(float)


def _stock_wide(**columns: pd.Series) -> pd.DataFrame:
    """Company | <one column per keyword> over the union of companies, with a
    Total row at the bottom. Missing cells become 0.0."""
    companies = sorted(set().union(*(set(s.index) for s in columns.values())))
    wide = pd.DataFrame({"Company": companies})
    for name, series in columns.items():
        wide[name] = series.reindex(companies, fill_value=0.0).to_numpy(dtype=float)
    total_row = {"Company": "Total", **{name: float(wide[name].sum()) for name in columns}}
    return pd.concat([wide, pd.DataFrame([total_row])], ignore_index=True)


def _align_for_pnl(
    sales_wide: pd.DataFrame,
    purchases_wide: pd.DataFrame,
    opening: pd.Series,
    closing: pd.Series,
) -> pd.DataFrame:
    """Adjusted P&L = (Sales - Purchases) - Opening + Closing across the union
    of companies.

    Monthly columns reflect (sales - purchases) per month and 'FY Total' their
    sum. Three columns follow on the right: 'Opening' and 'Closing' (one
    FY-scoped stock value each per company) and 'Adjusted P&L'
    (= FY Total - Opening + Closing).
    """
    num_cols = MONTH_LABELS + ["FY Total"]

    # Drop the Total row from each; we'll recompute it after the subtraction.
    s = sales_wide[sales_wide["Company"] != "Total"].set_index("Company")[num_cols]
    p = purchases_wide[purchases_wide["Company"] != "Total"].set_index("Company")[num_cols]

    companies = sorted(set(s.index) | set(p.index) | set(opening.index) | set(closing.index))
    s = s.reindex(companies, fill_value=0.0)
    p = p.reindex(companies, fill_value=0.0)

    pnl = s.sub(p, fill_value=0.0)
    pnl["Opening"] = opening.reindex(companies, fill_value=0.0).to_numpy(dtype=float)
    pnl["Closing"] = closing.reindex(companies, fill_value=0.0).to_numpy(dtype=float)
    pnl["Adjusted P&L"] = pnl["FY Total"] - pnl["Opening"] + pnl["Closing"]
    pnl.index.name = "Company"
    pnl = pnl.reset_index()

    total_row = {"Company": "Total"}
    for col in num_cols + STOCK_COLS + ["Adjusted P&L"]:
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
        "(invoice_type = 5) net of claims, attributed to the selling company. "
        "Stock is the ERP line balance (receipts less issues and sales) on a date, "
        "valued at the post-claim rate; Adjusted P&L = Sales - Purchases - Opening + Closing."
    )

    fy_options = _current_and_previous_fy_labels()
    fy_label = st.selectbox("Financial Year", options=fy_options, index=0)
    opening_on, closing_on = _stock_as_of_dates(fy_label)

    sales_df, purchases_df, opening_df, closing_df, marked_df = _load_fy_data(fy_label)

    sales_wide = _pivot_to_fy_months(sales_df, "net_sales")
    purchases_wide = _pivot_to_fy_months(purchases_df, "net_purchases")
    opening, closing, marked = (_stock_series(d) for d in (opening_df, closing_df, marked_df))
    stock_wide = _stock_wide(Opening=opening, Closing=closing)
    marked_wide = _stock_wide(Stock=marked)
    pnl_wide = _align_for_pnl(sales_wide, purchases_wide, opening, closing)

    # Extract totals for KPI cards
    def _total_for(df: pd.DataFrame, col: str = "FY Total") -> float:
        total_rows = df[df["Company"] == "Total"]
        if total_rows.empty:
            return 0.0
        return float(total_rows.iloc[0][col])

    total_sales = _total_for(sales_wide)
    total_purchases = _total_for(purchases_wide)
    total_opening = _total_for(stock_wide, "Opening")
    total_closing = _total_for(stock_wide, "Closing")
    total_adj_pnl = total_sales - total_purchases - total_opening + total_closing

    if all(d is None or d.empty
           for d in (sales_df, purchases_df, opening_df, closing_df, marked_df)):
        st.info(f"No data for FY {fy_label}.")

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Total Sales", f"{total_sales:,.2f}")
    col2.metric("Total Purchases", f"{total_purchases:,.2f}")
    col3.metric(f"Opening Stock ({opening_on:%d-%b-%Y})", f"{total_opening:,.2f}")
    col4.metric(f"Closing Stock ({closing_on:%d-%b-%Y})", f"{total_closing:,.2f}")
    col5.metric("Adjusted P&L", f"{total_adj_pnl:,.2f}")

    st.markdown("---")

    month_cols = MONTH_LABELS + ["FY Total"]
    pnl_cols = month_cols + STOCK_COLS + ["Adjusted P&L"]

    _render_grid(f"Sales — FY {fy_label}", sales_wide, month_cols)
    _render_grid(f"Purchases — FY {fy_label}", purchases_wide, month_cols)
    _render_grid(f"Net P&L — FY {fy_label}", pnl_wide, pnl_cols)
    _render_grid(f"Stock — opening {opening_on:%d-%b-%Y}, closing {closing_on:%d-%b-%Y}",
                 stock_wide, STOCK_COLS)
    _render_grid(f"Marked Godown Stock — as of {closing_on:%d-%b-%Y}", marked_wide, ["Stock"])
