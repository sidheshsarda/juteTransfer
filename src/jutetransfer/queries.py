"""Database queries for JuteTransfer application."""

import pandas as pd
from typing import Optional, Tuple
from datetime import datetime
from .database import DatabaseConnection


# ---------------------------------------------------------------------------
# Cached master data loaders — hit DB once, reuse in-memory
# ---------------------------------------------------------------------------

def load_companies() -> pd.DataFrame:
    """Load all companies (no cache - always fresh from database)."""
    return DatabaseConnection.execute_query(
        "SELECT co_id, co_name, co_prefix FROM co_mst ORDER BY co_name"
    )


def load_branches() -> pd.DataFrame:
    """Load all branches (no cache - always fresh from database)."""
    return DatabaseConnection.execute_query(
        "SELECT branch_id, branch_name, co_id FROM branch_mst ORDER BY branch_name"
    )


def load_warehouses() -> pd.DataFrame:
    """Load all warehouses (no cache - always fresh from database)."""
    return DatabaseConnection.execute_query(
        "SELECT warehouse_id, warehouse_name, branch_id FROM warehouse_mst ORDER BY warehouse_name"
    )


# ---------------------------------------------------------------------------
# Derived lookup functions — filter cached DataFrames, no DB calls
# ---------------------------------------------------------------------------

def get_companies() -> dict:
    """Return {company_name: company_id} from cached data."""
    df = load_companies()
    if df is not None and not df.empty:
        return {name: int(cid) for name, cid in zip(df['co_name'], df['co_id'])}
    return {}


def get_branches_by_company(company_id: int) -> dict:
    """Return {branch_name: branch_id} for a company from cached data."""
    df = load_branches()
    if df is None or df.empty:
        return {}
    filtered = df[df['co_id'] == int(company_id)]
    return {name: int(bid) for name, bid in zip(filtered['branch_name'], filtered['branch_id'])}


def get_company_branch_options() -> Tuple[list, dict]:
    """Return company-branch dropdown options from cached data."""
    cos = load_companies()
    brs = load_branches()
    if cos is None or brs is None or cos.empty or brs.empty:
        return [""], {}
    merged = brs.merge(cos, on='co_id')
    merged['co_branch_label'] = merged['co_prefix'] + '-' + merged['branch_name']
    merged = merged.sort_values(['co_prefix', 'branch_name'])
    options = [""] + merged['co_branch_label'].tolist()
    mapping = {row['co_branch_label']: (int(row['co_id']), int(row['branch_id']))
               for _, row in merged.iterrows()}
    return options, mapping


def _get_financial_year_bounds() -> Tuple[datetime, datetime]:
    """Get the current financial year start and end dates (April to March)."""
    now = datetime.now()
    if now.month >= 4:
        return datetime(now.year, 4, 1), datetime(now.year + 1, 3, 31)
    return datetime(now.year - 1, 4, 1), datetime(now.year, 3, 31)


def get_next_mr_number(co_id: int, branch_id: int) -> int:
    """Get the next MR number for a company/branch in the current financial year.

    Financial year is April to March.

    Args:
        co_id: Company ID
        branch_id: Branch ID

    Returns:
        int: Next MR number (max + 1)
    """
    fy_start, fy_end = _get_financial_year_bounds()

    df = DatabaseConnection.execute_query(
        """
        SELECT COALESCE(MAX(branch_mr_no), 0) AS max_mr_no
        FROM jute_mr
        WHERE branch_id = :branch_id
        AND jute_mr_date BETWEEN :fy_start AND :fy_end
        """,
        {
            "branch_id": branch_id,
            "fy_start": fy_start.strftime('%Y-%m-%d'),
            "fy_end": fy_end.strftime('%Y-%m-%d')
        }
    )

    if df is not None and not df.empty:
        return int(df['max_mr_no'].iloc[0]) + 1
    return 1


def get_next_mr_numbers_batch(co_branch_counts: dict) -> dict:
    """Get next MR numbers for multiple company/branch pairs in one batch.

    Instead of one DB call per row, makes one call per unique (co_id, branch_id) pair.

    Args:
        co_branch_counts: Dict mapping (co_id, branch_id) to count of numbers needed

    Returns:
        Dict mapping (co_id, branch_id) to list of sequential MR numbers
    """
    fy_start, fy_end = _get_financial_year_bounds()
    result = {}

    for (co_id, branch_id), count in co_branch_counts.items():
        df = DatabaseConnection.execute_query(
            """
            SELECT COALESCE(MAX(branch_mr_no), 0) AS max_mr_no
            FROM jute_mr
            WHERE branch_id = :branch_id
            AND jute_mr_date BETWEEN :fy_start AND :fy_end
            """,
            {
                "branch_id": branch_id,
                "fy_start": fy_start.strftime('%Y-%m-%d'),
                "fy_end": fy_end.strftime('%Y-%m-%d')
            }
        )

        base = int(df['max_mr_no'].iloc[0]) + 1 if df is not None and not df.empty else 1
        result[(co_id, branch_id)] = list(range(base, base + count))

    return result


def get_jute_mr_with_line_items(
    year: int,
    month: int,
    company_id: Optional[int] = None,
    branch_id: Optional[int] = None
) -> pd.DataFrame:
    """Fetch Jute MR records joined with line items.
    
    Args:
        year: Year to filter by
        month: Month to filter by
        company_id: Optional company ID to filter by
        branch_id: Optional branch ID to filter by
        
    Returns:
        pd.DataFrame: DataFrame with MR and line item data
    """
    query = """
        SELECT
            mr.jute_mr_id AS `jute_mr_id`,
            mr.jute_gate_entry_no AS `Jute Gate Entry No`,
            mr.jute_gate_entry_date AS `Jute Gate Entry Date`,
            p.po_no AS `PO.No.`,
            p.po_date AS `PO DATE`,
            mr.branch_mr_no AS `EJM MR No.`,
            mr.jute_gate_entry_no AS `CO_MR_No`,
            mr.jute_mr_date AS `MR DATE`,
            s.supplier_name AS `Jute Supplier`,
            pm.supp_name AS `Party Name`,
            pb.address AS `Party Branch Address`,
            im.item_name AS `Item Quality`,
            li.accepted_weight AS `Weight (KG)`,
            mr.invoice_no AS `Invoice No`,
            DATE(mr.invoice_date) AS `Invoice Date`,
            CASE mr.status_id
                WHEN 0 THEN 'Pending'
                WHEN 1 THEN 'Approved'
                WHEN 2 THEN 'Completed'
                ELSE CONCAT('Status-', mr.status_id)
            END AS `Status`,
            li.rate AS `MR Rate`,
            (COALESCE(li.accepted_weight, 0) * COALESCE(li.rate, 0) / 100) AS `Total Amount`,
            li.claim_rate AS `Claim Rate`,
            (COALESCE(li.accepted_weight, 0) * COALESCE(li.claim_rate, 0) / 100 + COALESCE(li.water_damage_amount, 0) - COALESCE(li.premium_amount, 0)) AS `Claim Amount`,
            ((COALESCE(li.accepted_weight, 0) * COALESCE(li.rate, 0) / 100) - (COALESCE(li.accepted_weight, 0) * COALESCE(li.claim_rate, 0) / 100 + COALESCE(li.water_damage_amount, 0) - COALESCE(li.premium_amount, 0))) AS `Net Total`,
            mr.challan_date AS `Challan Date`,
            wh.warehouse_name AS `Warehouse`
        FROM jute_mr mr
        INNER JOIN branch_mst bm ON mr.branch_id = bm.branch_id
        INNER JOIN jute_mr_li li ON mr.jute_mr_id = li.jute_mr_id
        LEFT JOIN jute_po p ON mr.po_id = p.jute_po_id
        LEFT JOIN jute_supplier_mst s ON mr.jute_supplier_id = s.supplier_id
        LEFT JOIN party_mst pm ON pm.party_id = mr.party_id AND pm.co_id = bm.co_id
        LEFT JOIN party_branch_mst pb ON pb.party_id = pm.party_id AND pb.party_mst_branch_id = mr.party_branch_id
        LEFT JOIN item_mst im ON li.actual_item_id = im.item_id
        LEFT JOIN warehouse_mst wh ON li.warehouse_id = wh.warehouse_id
        WHERE YEAR(mr.jute_gate_entry_date) = :year
        AND MONTH(mr.jute_gate_entry_date) = :month
    """
    params = {"year": year, "month": month}

    if company_id:
        query += " AND bm.co_id = :co_id"
        params["co_id"] = company_id

    if branch_id:
        query += " AND mr.branch_id = :branch_id"
        params["branch_id"] = branch_id
    
    query += " ORDER BY mr.jute_gate_entry_date DESC, mr.jute_gate_entry_no"

    return DatabaseConnection.execute_query(query, params)


def get_source_mr_full(jute_mr_id: int, conn=None) -> Optional[dict]:
    """Fetch complete jute_mr record + line items for copying during transfer.

    Args:
        jute_mr_id: Primary key of the source MR
        conn: Optional existing DB connection (for use inside a transaction)

    Returns:
        dict with all jute_mr columns and a 'line_items' list of dicts,
        or None if not found.
    """
    from sqlalchemy import text as sa_text

    if conn is not None:
        mr_row = conn.execute(
            sa_text("SELECT * FROM jute_mr WHERE jute_mr_id = :id"),
            {"id": jute_mr_id},
        ).fetchone()
        if not mr_row:
            return None
        mr_dict = dict(mr_row._mapping)

        li_rows = conn.execute(
            sa_text("SELECT * FROM jute_mr_li WHERE jute_mr_id = :id"),
            {"id": jute_mr_id},
        ).fetchall()
        mr_dict["line_items"] = [dict(r._mapping) for r in li_rows]
        return mr_dict

    mr_df = DatabaseConnection.execute_query(
        "SELECT * FROM jute_mr WHERE jute_mr_id = :id",
        {"id": jute_mr_id},
    )
    if mr_df is None or mr_df.empty:
        return None

    mr_dict = mr_df.iloc[0].to_dict()

    li_df = DatabaseConnection.execute_query(
        "SELECT * FROM jute_mr_li WHERE jute_mr_id = :id",
        {"id": jute_mr_id},
    )
    mr_dict["line_items"] = (
        li_df.to_dict("records") if li_df is not None and not li_df.empty else []
    )
    return mr_dict


def get_transfer_chain(root_mr_id: int) -> pd.DataFrame:
    """Fetch all transferred MRs for a given root MR, with company info.

    Returns DataFrame with columns: jute_mr_id, src_com_id, branch_id,
    jute_mr_date, challan_date, branch_mr_no, total_amount, claim_amount, net_total,
    owner_co_id, branch_name, co_name, co_prefix.
    Ordered by jute_mr_id ASC for chain reconstruction.
    """
    return DatabaseConnection.execute_query(
        """
        SELECT mr.jute_mr_id, mr.src_com_id, mr.branch_id, mr.jute_mr_date,
               mr.challan_date, mr.branch_mr_no, mr.total_amount, mr.claim_amount, mr.net_total,
               bm.co_id AS owner_co_id, bm.branch_name,
               cm.co_name, cm.co_prefix
        FROM jute_mr mr
        JOIN branch_mst bm ON mr.branch_id = bm.branch_id
        JOIN co_mst cm ON bm.co_id = cm.co_id
        WHERE mr.src_jute_mr_id = :root_id
        ORDER BY mr.jute_mr_id ASC
        """,
        {"root_id": root_mr_id},
    )


def get_transfer_chains_batch(mr_ids: list) -> dict:
    """Fetch transfer chains for multiple root MRs in one query.

    Returns:
        dict mapping src_jute_mr_id -> DataFrame of chain records.
        MR IDs with no chain are absent from the dict.
    """
    if not mr_ids:
        return {}
    placeholders = ",".join(str(int(mid)) for mid in mr_ids)
    df = DatabaseConnection.execute_query(f"""
        SELECT mr.jute_mr_id, mr.src_jute_mr_id, mr.src_com_id, mr.branch_id,
               mr.jute_mr_date, mr.challan_date, mr.branch_mr_no, mr.total_amount, mr.claim_amount, mr.net_total,
               bm.co_id AS owner_co_id, bm.branch_name,
               cm.co_name, cm.co_prefix
        FROM jute_mr mr
        JOIN branch_mst bm ON mr.branch_id = bm.branch_id
        JOIN co_mst cm ON bm.co_id = cm.co_id
        WHERE mr.src_jute_mr_id IN ({placeholders})
        ORDER BY mr.jute_mr_id ASC
    """)
    if df is None or df.empty:
        return {}
    return {int(mid): group for mid, group in df.groupby("src_jute_mr_id")}


def get_warehouses_by_branch(branch_id: int) -> dict:
    """Return {warehouse_name: warehouse_id} for a branch from cached data."""
    df = load_warehouses()
    if df is None or df.empty:
        return {}
    # Explicit int cast to avoid numpy/pandas dtype mismatch from iterrows()
    filtered = df[df['branch_id'] == int(branch_id)]
    return dict(zip(filtered['warehouse_name'], filtered['warehouse_id']))


def get_invoice_details_by_mr_id(mr_id: int) -> Optional[dict]:
    """Fetch LC/contract fields from sales_invoice linked to an MR via sales_invoice_jute.

    Returns dict with consignment_no, consignment_date, contract_no, contract_date
    or None if no invoice found.
    """
    df = DatabaseConnection.execute_query(
        """SELECT si.consignment_no, si.consignment_date,
                  si.contract_no, si.contract_date
           FROM sales_invoice si
           JOIN sales_invoice_jute sij ON si.invoice_id = sij.invoice_id
           WHERE sij.mr_id = :mr_id
           LIMIT 1""",
        {"mr_id": mr_id},
    )
    if df is not None and not df.empty:
        return df.iloc[0].to_dict()
    return None


# ---------------------------------------------------------------------------
# Company P&L dashboard aggregations
# ---------------------------------------------------------------------------

def get_company_wise_purchases_by_month(fy_start, fy_end) -> pd.DataFrame:
    """Return per-(company, month) net purchase totals from jute_mr for a FY.

    Columns: co_id, co_name, month (1-12), net_purchases (float).
    """
    return DatabaseConnection.execute_query(
        """
        SELECT
            cm.co_id           AS co_id,
            cm.co_name         AS co_name,
            MONTH(mr.jute_mr_date) AS month,
            SUM(COALESCE(mr.net_total, 0)) AS net_purchases
        FROM jute_mr mr
        JOIN branch_mst bm ON mr.branch_id = bm.branch_id
        JOIN co_mst cm ON bm.co_id = cm.co_id
        WHERE mr.jute_mr_date BETWEEN :fy_start AND :fy_end
        GROUP BY cm.co_id, cm.co_name, MONTH(mr.jute_mr_date)
        """,
        {
            "fy_start": fy_start.strftime("%Y-%m-%d"),
            "fy_end": fy_end.strftime("%Y-%m-%d"),
        },
    )


def get_company_wise_sales_by_month(fy_start, fy_end) -> pd.DataFrame:
    """Return per-(company, month) net sales totals for raw-jute invoices in a FY.

    Sales are attributed to the SELLER (sales_invoice.branch_id -> co_id).
    Net sales = invoice_amount - COALESCE(sales_invoice_jute.claim_amount, 0).
    Filters: invoice_type = 5 (raw jute), active = 1.
    Columns: co_id, co_name, month (1-12), net_sales (float).
    """
    return DatabaseConnection.execute_query(
        """
        SELECT
            cm.co_id           AS co_id,
            cm.co_name         AS co_name,
            MONTH(si.invoice_date) AS month,
            SUM(COALESCE(si.invoice_amount, 0) - COALESCE(sij.claim_amount, 0)) AS net_sales
        FROM sales_invoice si
        JOIN branch_mst bm ON si.branch_id = bm.branch_id
        JOIN co_mst cm ON bm.co_id = cm.co_id
        LEFT JOIN sales_invoice_jute sij ON sij.invoice_id = si.invoice_id
        WHERE si.invoice_type = 5
          AND si.active = 1
          AND si.invoice_date BETWEEN :fy_start AND :fy_end
        GROUP BY cm.co_id, cm.co_name, MONTH(si.invoice_date)
        """,
        {
            "fy_start": fy_start.strftime("%Y-%m-%d"),
            "fy_end": fy_end.strftime("%Y-%m-%d"),
        },
    )


def get_company_wise_unsold_stock(fy_start, fy_end) -> pd.DataFrame:
    """Return per-company unsold-MR stock value (valued at purchase net_total).

    An MR is considered 'stock' iff ALL of:
      - Dated within the FY (jute_mr_date in [fy_start, fy_end])
      - status_id = 3 (active intermediate transfer MR; excludes pending
        gate entries at status 0 and finalized roots at status 1)
      - Its chain root (src_jute_mr_id -> jute_mr) is NOT in a closed
        state (root.status_id NOT IN (1, 13)). Status 13 is set by the
        upstream ERP on roots whose source company is consuming the
        material rather than reselling it; status 1 is set by this app
        on finalization. Both indicate the chain is closed and any
        return MR sitting at the source company should not be counted.
      - Not referenced by an active raw-jute sales_invoice_jute row
        (i.e., the seller has not yet invoiced this MR forward).

    Columns: co_id, co_name, stock_value (float).
    """
    return DatabaseConnection.execute_query(
        """
        SELECT
            cm.co_id   AS co_id,
            cm.co_name AS co_name,
            COALESCE(SUM(mr.net_total), 0) AS stock_value
        FROM jute_mr mr
        JOIN branch_mst bm ON mr.branch_id = bm.branch_id
        JOIN co_mst    cm ON bm.co_id = cm.co_id
        WHERE mr.jute_mr_date BETWEEN :fy_start AND :fy_end
          AND mr.status_id = 3
          AND NOT EXISTS (
              SELECT 1
              FROM jute_mr root
              WHERE root.jute_mr_id = mr.src_jute_mr_id
                AND root.status_id IN (1, 13)
          )
          AND NOT EXISTS (
              SELECT 1
              FROM sales_invoice_jute sij
              JOIN sales_invoice si ON si.invoice_id = sij.invoice_id
              WHERE sij.mr_id = mr.jute_mr_id
                AND si.active = 1
                AND si.invoice_type = 5
          )
        GROUP BY cm.co_id, cm.co_name
        """,
        {
            "fy_start": fy_start.strftime("%Y-%m-%d"),
            "fy_end": fy_end.strftime("%Y-%m-%d"),
        },
    )
