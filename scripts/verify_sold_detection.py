"""Read-only verification: can sold marked stock be auto-detected? (spec section 7)

Run: python -m scripts.verify_sold_detection
"""
from src.jutetransfer.database import DatabaseConnection

CHECKS = {
    "mode1_total": (
        "count of marked (transfer_mode=1) MRs",
        "SELECT COUNT(*) AS c FROM jute_mr WHERE transfer_mode = 1",
    ),
    "mode1_sold_linked": (
        "marked MRs referenced by an active raw-jute invoice via sales_invoice_jute.mr_id",
        """
        SELECT COUNT(DISTINCT mr.jute_mr_id) AS c
        FROM jute_mr mr
        JOIN sales_invoice_jute sij ON sij.mr_id = mr.jute_mr_id
        JOIN sales_invoice si ON si.invoice_id = sij.invoice_id
        WHERE mr.transfer_mode = 1 AND si.active = 1 AND si.invoice_type = 5
        """,
    ),
    "type5_mr_id_population": (
        "active raw-jute invoice rows: how many carry a non-null mr_id",
        """
        SELECT SUM(sij.mr_id IS NOT NULL) AS linked, COUNT(*) AS total
        FROM sales_invoice_jute sij
        JOIN sales_invoice si ON si.invoice_id = sij.invoice_id
        WHERE si.active = 1 AND si.invoice_type = 5
        """,
    ),
    "type5_mr_id_valid": (
        "non-null mr_id values that resolve to a real jute_mr row",
        """
        SELECT COUNT(*) AS c
        FROM sales_invoice_jute sij
        JOIN sales_invoice si ON si.invoice_id = sij.invoice_id
        JOIN jute_mr mr ON mr.jute_mr_id = sij.mr_id
        WHERE si.active = 1 AND si.invoice_type = 5
        """,
    ),
    "jute_issue_mr_no_link": (
        "informational: jute_issue rows (status_id <> 4) with a populated jute_mr_li_id, "
        "and how many resolve to a real jute_mr_li row",
        """
        SELECT
            COUNT(*) AS total_issue_rows,
            SUM(ji.jute_mr_li_id IS NOT NULL) AS with_mr_li_id,
            SUM(li.jute_mr_li_id IS NOT NULL) AS joinable_to_jute_mr_li
        FROM jute_issue ji
        LEFT JOIN jute_mr_li li ON li.jute_mr_li_id = ji.jute_mr_li_id
        WHERE ji.status_id <> 4
        """,
    ),
    "vw_jute_stock_outstanding_exists": (
        "owner-ruled consumption-detection source: does the view exist and return balances "
        "(balance = actual_weight - SUM(jute_issue.weight WHERE status_id <> 4) per jute_mr_li_id)",
        """
        SELECT COUNT(*) AS total_rows, SUM(bal_weight > 0) AS positive_bal
        FROM vw_jute_stock_outstanding
        """,
    ),
}


def main() -> None:
    for key, (label, sql) in CHECKS.items():
        df = DatabaseConnection.execute_query(sql)
        print(f"{key}: {label}")
        print(df.to_string(index=False))
        print("-" * 60)
    print(
        "RESOLVED (owner ruling 2026-08-03): sales_invoice_jute.mr_id-based sold-detection "
        "was REJECTED — 0/2980 active type-5 invoice rows in FY2025-26 carried a usable link. "
        "Consumption detection now uses vw_jute_stock_outstanding "
        "(balance = actual_weight - SUM(jute_issue.weight WHERE status_id <> 4) per "
        "jute_mr_li_id) per the owner's ruling. The counts above are kept as the historical "
        "record of why mr_id-based detection was rejected; they no longer gate the plan."
    )


if __name__ == "__main__":
    main()
