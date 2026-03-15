"""Database queries for JuteTransfer application."""

import pandas as pd
from typing import Optional, Tuple
from .database import DatabaseConnection


def get_companies() -> dict:
    """Fetch all companies for dropdown.
    
    Returns:
        dict: Dictionary mapping company name to company id
    """
    df = DatabaseConnection.execute_query(
        "SELECT co_id, co_name FROM co_mst ORDER BY co_name"
    )
    if df is not None and not df.empty:
        return {row['co_name']: row['co_id'] for _, row in df.iterrows()}
    return {}


def get_branches_by_company(company_id: int) -> dict:
    """Fetch branches for a specific company.
    
    Args:
        company_id: The company ID to filter branches by
        
    Returns:
        dict: Dictionary mapping branch name to branch id
    """
    df = DatabaseConnection.execute_query(
        "SELECT branch_id, branch_name FROM branch_mst WHERE co_id = :co_id ORDER BY branch_name",
        {"co_id": company_id}
    )
    if df is not None and not df.empty:
        return {row['branch_name']: row['branch_id'] for _, row in df.iterrows()}
    return {}


def get_company_branch_options() -> Tuple[list, dict]:
    """Fetch concatenated company-branch options for dropdown.
    
    Only includes companies that have at least one branch.
    
    Returns:
        Tuple[list, dict]: List of company-branch labels and dict mapping label to (co_id, branch_id)
    """
    df = DatabaseConnection.execute_query(
        """
        SELECT CONCAT(c.co_prefix, '-', b.branch_name) AS co_branch_label,
               b.branch_id, c.co_id
        FROM co_mst c
        INNER JOIN branch_mst b ON c.co_id = b.co_id
        ORDER BY c.co_prefix, b.branch_name
        """
    )
    if df is not None and not df.empty:
        options = [""] + df['co_branch_label'].tolist()
        mapping = {row['co_branch_label']: (row['co_id'], row['branch_id']) 
                   for _, row in df.iterrows()}
        return options, mapping
    return [""], {}


def get_next_mr_number(co_id: int, branch_id: int) -> int:
    """Get the next MR number for a company/branch in the current financial year.
    
    Financial year is April to March.
    
    Args:
        co_id: Company ID
        branch_id: Branch ID
        
    Returns:
        int: Next MR number (max + 1)
    """
    from datetime import datetime
    
    now = datetime.now()
    # Financial year: April to March
    if now.month >= 4:
        fy_start = datetime(now.year, 4, 1)
        fy_end = datetime(now.year + 1, 3, 31)
    else:
        fy_start = datetime(now.year - 1, 4, 1)
        fy_end = datetime(now.year, 3, 31)
    
    df = DatabaseConnection.execute_query(
        """
        SELECT COALESCE(MAX(branch_mr_no), 0) AS max_mr_no
        FROM jute_mr
        WHERE src_com_id = :co_id 
        AND branch_id = :branch_id
        AND jute_mr_date BETWEEN :fy_start AND :fy_end
        """,
        {
            "co_id": co_id,
            "branch_id": branch_id,
            "fy_start": fy_start.strftime('%Y-%m-%d'),
            "fy_end": fy_end.strftime('%Y-%m-%d')
        }
    )
    
    if df is not None and not df.empty:
        return int(df['max_mr_no'].iloc[0]) + 1
    return 1


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
            mr.jute_gate_entry_no AS `Jute Gate Entry No`,
            mr.jute_gate_entry_date AS `Jute Gate Entry Date`,
            p.po_no AS `PO.No.`,
            p.po_date AS `PO DATE`,
            mr.branch_mr_no AS `EJM MR No.`,
            mr.jute_gate_entry_no AS `CO_MR_No`,
            mr.jute_mr_date AS `MR DATE`,
            s.supplier_name AS `Party Name`,
            q.jute_quality AS `Item Quality`,
            li.accepted_weight AS `Weight (KG)`,
            mr.invoice_no AS `Invoice No`,
            DATE(mr.invoice_date) AS `Invoice Date`,
            mr.status_id AS `Status`,
            li.rate AS `MR Rate`,
            (COALESCE(li.accepted_weight, 0) * COALESCE(li.rate, 0) / 100) AS `Total Amount`,
            li.claim_rate AS `Claim Rate`,
            (COALESCE(li.accepted_weight, 0) * COALESCE(li.claim_rate, 0) + COALESCE(li.water_damage_amount, 0) - COALESCE(li.premium_amount, 0)) AS `Claim Amount`,
            ((COALESCE(li.accepted_weight, 0) * COALESCE(li.rate, 0) / 100) - (COALESCE(li.accepted_weight, 0) * COALESCE(li.claim_rate, 0) + COALESCE(li.water_damage_amount, 0) - COALESCE(li.premium_amount, 0))) AS `Net Total`
        FROM jute_mr mr
        INNER JOIN jute_mr_li li ON mr.jute_mr_id = li.jute_mr_id
        LEFT JOIN jute_po p ON mr.po_id = p.jute_po_id
        LEFT JOIN jute_supplier_mst s ON mr.jute_supplier_id = s.supplier_id
        LEFT JOIN jute_quality_mst q ON li.actual_quality = q.jute_qlty_id
        WHERE YEAR(mr.jute_gate_entry_date) = :year 
        AND MONTH(mr.jute_gate_entry_date) = :month
    """
    params = {"year": year, "month": month}

    if company_id:
        query += " AND mr.src_com_id = :co_id"
        params["co_id"] = company_id

    if branch_id:
        query += " AND mr.branch_id = :branch_id"
        params["branch_id"] = branch_id
    
    query += " ORDER BY mr.jute_gate_entry_date DESC, mr.jute_gate_entry_no"

    return DatabaseConnection.execute_query(query, params)
