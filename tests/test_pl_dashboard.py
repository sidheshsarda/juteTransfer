"""Pure helpers of pages/company_pl_dashboard.py (no database): the stock
as-of dates of a FY, the stock grid, and the adjusted P&L
(= FY Total - Opening + Closing)."""
from datetime import date

import pandas as pd

from src.jutetransfer.pages.company_pl_dashboard import (
    MONTH_LABELS,
    _align_for_pnl,
    _stock_as_of_dates,
    _stock_series,
    _stock_wide,
)


def _wide(data: dict) -> pd.DataFrame:
    """{company: {month label: amount}} -> Company | Apr..Mar | FY Total, plus a Total row."""
    df = pd.DataFrame.from_dict(data, orient="index").reindex(columns=MONTH_LABELS).fillna(0.0)
    df["FY Total"] = df.sum(axis=1)
    df.loc["Total"] = df.sum()
    return df.rename_axis("Company").reset_index()


def test_stock_dates_of_the_running_fy_close_today():
    """Running FY: opening = 31-Mar before it, closing = today."""
    assert _stock_as_of_dates("26-27", today=date(2026, 10, 23)) == (
        date(2026, 3, 31), date(2026, 10, 23))


def test_stock_dates_of_a_past_fy_close_on_31_march():
    """Past FY: opening = 31-Mar of the prior year, closing = its own 31-Mar."""
    assert _stock_as_of_dates("25-26", today=date(2026, 10, 23)) == (
        date(2025, 3, 31), date(2026, 3, 31))
    assert _stock_as_of_dates("25-26", today=date(2026, 3, 31)) == (
        date(2025, 3, 31), date(2026, 3, 31))


def test_stock_wide_covers_every_company_with_zeros_and_a_total_row():
    """Union of companies, missing cells 0, Total row at the bottom."""
    opening = _stock_series(pd.DataFrame(
        {"co_id": [2], "co_name": ["A"], "stock_value": [10.0]}))
    closing = _stock_series(pd.DataFrame(
        {"co_id": [74, 2], "co_name": ["B", "A"], "stock_value": [5.0, 2.0]}))
    wide = _stock_wide(Opening=opening, Closing=closing)
    assert wide.to_dict("records") == [
        {"Company": "A", "Opening": 10.0, "Closing": 2.0},
        {"Company": "B", "Opening": 0.0, "Closing": 5.0},
        {"Company": "Total", "Opening": 10.0, "Closing": 7.0},
    ]
    # no stock at all: only the Total row, at 0
    assert _stock_wide(Stock=_stock_series(pd.DataFrame())).to_dict("records") == [
        {"Company": "Total", "Stock": 0.0}]


def test_adjusted_pnl_is_fy_total_less_opening_plus_closing():
    """Monthly sales - purchases, Opening / Closing, Adjusted P&L and a Total row
    over the union of companies (D has opening stock only)."""
    sales = _wide({"A": {"Apr": 100.0, "May": 50.0}, "B": {"Jun": 30.0}})
    purchases = _wide({"A": {"Apr": 80.0}, "C": {"Jul": 20.0}})
    opening = pd.Series({"A": 10.0, "D": 7.0})
    closing = pd.Series({"A": 25.0, "B": 5.0})

    pnl = _align_for_pnl(sales, purchases, opening, closing).set_index("Company")

    assert list(pnl.index) == ["A", "B", "C", "D", "Total"]
    cols = ["Apr", "May", "Jun", "Jul", "FY Total", "Opening", "Closing", "Adjusted P&L"]
    assert pnl.loc["A", cols].tolist() == [20.0, 50.0, 0.0, 0.0, 70.0, 10.0, 25.0, 85.0]
    assert pnl.loc["B", cols].tolist() == [0.0, 0.0, 30.0, 0.0, 30.0, 0.0, 5.0, 35.0]
    assert pnl.loc["C", cols].tolist() == [0.0, 0.0, 0.0, -20.0, -20.0, 0.0, 0.0, -20.0]
    assert pnl.loc["D", cols].tolist() == [0.0, 0.0, 0.0, 0.0, 0.0, 7.0, 0.0, -7.0]
    assert pnl.loc["Total", cols].tolist() == [20.0, 50.0, 30.0, -20.0, 80.0, 17.0, 30.0, 93.0]
    # the Total row is the column sums of the company rows, every column
    pd.testing.assert_series_equal(pnl.drop("Total").sum(), pnl.loc["Total"].astype(float),
                                   check_names=False)
