"""Tests for MR grouping logic."""
import pandas as pd
from src.jutetransfer.pages.jute_mr import _group_by_mr


def test_single_mr_single_li():
    """One MR with one line item — passthrough."""
    df = pd.DataFrame([{
        "jute_mr_id": 111,
        "Jute Gate Entry No": 1,
        "MR DATE": "2026-03-01",
        "Party Name": "Supplier A",
        "Item Quality": "TD5",
        "Weight (KG)": 1000,
        "MR Rate": 2500,
        "Total Amount": 25000.0,
        "Claim Amount": 50.0,
        "Net Total": 24950.0,
    }])
    grouped, line_items_map = _group_by_mr(df)
    assert len(grouped) == 1
    assert grouped.iloc[0]["jute_mr_id"] == 111
    assert grouped.iloc[0]["Weight (KG)"] == 1000
    assert len(line_items_map[111]) == 1


def test_single_mr_multiple_li():
    """One MR with two line items — aggregated."""
    df = pd.DataFrame([
        {
            "jute_mr_id": 111, "Jute Gate Entry No": 1, "MR DATE": "2026-03-01",
            "Party Name": "Supplier A", "Item Quality": "TD5",
            "Weight (KG)": 500, "MR Rate": 2450,
            "Total Amount": 12250.0, "Claim Amount": 30.0, "Net Total": 12220.0,
        },
        {
            "jute_mr_id": 111, "Jute Gate Entry No": 1, "MR DATE": "2026-03-01",
            "Party Name": "Supplier A", "Item Quality": "TD4",
            "Weight (KG)": 750, "MR Rate": 2800,
            "Total Amount": 21000.0, "Claim Amount": 45.0, "Net Total": 20955.0,
        },
    ])
    grouped, line_items_map = _group_by_mr(df)
    assert len(grouped) == 1
    row = grouped.iloc[0]
    assert row["Weight (KG)"] == 1250       # 500 + 750
    assert row["Total Amount"] == 33250.0   # 12250 + 21000
    assert row["Claim Amount"] == 75.0      # 30 + 45
    assert "TD5 / TD4" in row["Item Quality"] or "TD4 / TD5" in row["Item Quality"]
    assert len(line_items_map[111]) == 2


def test_multiple_mrs():
    """Two different MRs — two grouped rows."""
    df = pd.DataFrame([
        {"jute_mr_id": 111, "Jute Gate Entry No": 1, "MR DATE": "2026-03-01",
         "Party Name": "A", "Item Quality": "TD5",
         "Weight (KG)": 1000, "MR Rate": 2500,
         "Total Amount": 25000.0, "Claim Amount": 50.0, "Net Total": 24950.0},
        {"jute_mr_id": 222, "Jute Gate Entry No": 2, "MR DATE": "2026-03-02",
         "Party Name": "B", "Item Quality": "TD4",
         "Weight (KG)": 800, "MR Rate": 2600,
         "Total Amount": 20800.0, "Claim Amount": 40.0, "Net Total": 20760.0},
    ])
    grouped, line_items_map = _group_by_mr(df)
    assert len(grouped) == 2
    assert set(grouped["jute_mr_id"]) == {111, 222}


def test_weighted_avg_rate():
    """Weighted average rate = Total Amount / Weight * 100."""
    df = pd.DataFrame([
        {"jute_mr_id": 111, "Jute Gate Entry No": 1, "MR DATE": "2026-03-01",
         "Party Name": "A", "Item Quality": "TD5",
         "Weight (KG)": 500, "MR Rate": 2000,
         "Total Amount": 10000.0, "Claim Amount": 0.0, "Net Total": 10000.0},
        {"jute_mr_id": 111, "Jute Gate Entry No": 1, "MR DATE": "2026-03-01",
         "Party Name": "A", "Item Quality": "TD4",
         "Weight (KG)": 500, "MR Rate": 3000,
         "Total Amount": 15000.0, "Claim Amount": 0.0, "Net Total": 15000.0},
    ])
    grouped, _ = _group_by_mr(df)
    row = grouped.iloc[0]
    # Total = 25000, Weight = 1000 → avg rate = 25000/1000*100 = 2500
    assert row["MR Rate"] == 2500.0
