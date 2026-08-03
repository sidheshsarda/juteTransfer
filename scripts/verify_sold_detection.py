"""Read-only verification: can sold marked stock be auto-detected? (spec section 7)

Run: python scripts/verify_sold_detection.py
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
        "informational: jute_issue rows joinable to jute_mr on (mr_no, branch)",
        """
        SELECT COUNT(*) AS c
        FROM jute_issue ji
        JOIN jute_mr mr ON mr.branch_mr_no = ji.mr_no AND mr.branch_id = ji.branch_id
        WHERE COALESCE(ji.is_active, 1) = 1
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
        "DECISION RULE: proceed iff type5_mr_id_population shows mr_id populated "
        "on (nearly) all rows AND type5_mr_id_valid matches the linked count. "
        "mode1_sold_linked may legitimately be 0 if no marked lot was sold yet. "
        "If mr_id is broadly NULL: STOP and report to owner (spec section 7)."
    )


if __name__ == "__main__":
    main()
