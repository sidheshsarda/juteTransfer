"""Transfer chain finalization logic for JuteTransfer.

Handles the complete finalization flow when a transfer chain returns to its
source company: masters checks, MR creation for intermediate companies,
sales invoice generation, and updating the original MR.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
import logging

from sqlalchemy import text

from .database import DatabaseConnection
from .jute_mr_chain_helpers import _calculate_line_item_amount
from .queries import get_source_mr_full, _get_financial_year_bounds

logger = logging.getLogger(__name__)


def _debug_log(msg: str) -> None:
    """Append debug message to a log file (Streamlit swallows stdout)."""
    import os
    log_path = os.path.join(os.path.dirname(__file__), "..", "..", "debug_transfer.log")
    with open(log_path, "a") as f:
        f.write(f"[{datetime.now().isoformat()}] {msg}\n")

# Fixed invoice type for raw jute transfers
RAW_JUTE_INVOICE_TYPE = 5


@dataclass
class TransferStep:
    """A single step in the transfer chain."""
    co_id: int
    branch_id: int
    mr_date: date
    mr_rate: float              # weighted avg rate (display/header)
    total_amount: float         # aggregate, rounded to 0
    claim_amount: float         # aggregate, rounded to 0 (unaffected by %)
    net_amount: float           # total_amount - claim_amount
    mr_no: int
    pct_rate_increase: float = 0.0
    roundoff: float = 0.0
    challan_date: Optional[date] = None
    warehouse_id: Optional[int] = None
    gate_entry_no: Optional[int] = None
    lc_reference_no: str = ""
    lc_date: Optional[date] = None
    po_no_for_lc: str = ""
    order_date_for_lc: Optional[date] = None
    transfer_transport: bool = True


# ---------------------------------------------------------------------------
# Party lookup / creation helpers
# ---------------------------------------------------------------------------

def _generate_supp_code(conn, co_id: int, prefix: str = "J") -> str:
    """Generate the next sequential supp_code for a company.

    Finds the highest existing code matching ``<prefix><digits>`` in the
    company and returns the next one, e.g. J001 → J002.  Falls back to
    <prefix>001 if none exist yet.
    """
    result = conn.execute(
        text("""
            SELECT supp_code FROM party_mst
            WHERE co_id = :co_id
              AND supp_code REGEXP :pattern
            ORDER BY CAST(SUBSTRING(supp_code, :offset) AS UNSIGNED) DESC
            LIMIT 1
        """),
        {"co_id": co_id, "pattern": f"^{prefix}[0-9]+$", "offset": len(prefix) + 1},
    ).fetchone()

    if result and result[0]:
        last_num = int(result[0][len(prefix):])
        return f"{prefix}{last_num + 1:03d}"
    return f"{prefix}001"


def _find_party_by_supp_name(conn, supp_name: str, co_id: int) -> Optional[int]:
    """Find an existing party by supplier name in a given company.

    Returns party_id or None.
    """
    result = conn.execute(
        text("SELECT party_id FROM party_mst WHERE LOWER(TRIM(supp_name)) = LOWER(TRIM(:name)) AND co_id = :co_id LIMIT 1"),
        {"name": supp_name, "co_id": co_id},
    )
    row = result.fetchone()
    return row[0] if row else None


def _ensure_party_types(conn, party_id: int, updated_by: int) -> None:
    """Ensure a party includes party_type_ids '2,3' (customer and jute supplier).

    Additively adds 2 and 3 to existing party types without removing others.
    """
    required_ids = {2, 3}

    result = conn.execute(
        text("SELECT party_type_id FROM party_mst WHERE party_id = :pid"),
        {"pid": party_id},
    )
    row = result.fetchone()
    existing_types_str = row[0] if row and row[0] else ""

    # Parse existing IDs
    existing_ids = set()
    if existing_types_str:
        try:
            existing_ids = set(int(x.strip()) for x in existing_types_str.split(",") if x.strip())
        except (ValueError, AttributeError):
            existing_ids = set()

    # Combine existing and required IDs
    combined_ids = existing_ids | required_ids
    combined_str = ",".join(str(x) for x in sorted(combined_ids))

    # Only update if changed
    if combined_str != existing_types_str:
        conn.execute(
            text("UPDATE party_mst SET party_type_id = :types, updated_by = :updated_by, updated_date_time = NOW() WHERE party_id = :pid"),
            {"pid": party_id, "types": combined_str, "updated_by": updated_by},
        )


def _get_party_branch_id(conn, party_id: int) -> Optional[int]:
    """Get the first party_branch for a given party_id."""
    result = conn.execute(
        text("SELECT party_mst_branch_id FROM party_branch_mst WHERE party_id = :pid LIMIT 1"),
        {"pid": party_id},
    )
    row = result.fetchone()
    return row[0] if row else None


def _create_party_from_source(conn, source_party_id: int, source_co_id: int,
                               target_co_id: int, updated_by: int) -> tuple[int, int]:
    """Copy a party + its first branch from one company to another.

    Looks up the source party, creates a new party_mst entry for target_co_id,
    and copies the first party_branch_mst entry.

    Returns (new_party_id, new_party_branch_id).
    """
    # Fetch source party
    party_row = conn.execute(
        text("SELECT * FROM party_mst WHERE party_id = :pid"),
        {"pid": source_party_id},
    ).fetchone()
    if not party_row:
        raise ValueError(f"Source party_id {source_party_id} not found")

    party_dict = party_row._mapping

    # Insert new party for target company
    # Hard code party_type_id to "2,3" (customer and jute supplier)
    new_party_id = DatabaseConnection.execute_insert_returning_id(conn, """
        INSERT INTO party_mst (supp_name, prefix, active, co_id, supp_code,
            supp_contact_person, supp_contact_designation, supp_email_id,
            phone_no, party_pan_no, cin, entity_type_id, country_id,
            party_type_id, msme_certified, updated_by, updated_date_time)
        VALUES (:supp_name, :prefix, 1, :co_id, :supp_code,
            :contact_person, :contact_designation, :email,
            :phone, :pan, :cin, :entity_type_id, :country_id,
            :party_type_id, :msme, :updated_by, NOW())
    """, {
        "supp_name": party_dict["supp_name"],
        "prefix": party_dict.get("prefix"),
        "co_id": target_co_id,
        "supp_code": party_dict.get("supp_code"),
        "contact_person": party_dict.get("supp_contact_person"),
        "contact_designation": party_dict.get("supp_contact_designation"),
        "email": party_dict.get("supp_email_id"),
        "phone": party_dict.get("phone_no"),
        "pan": party_dict.get("party_pan_no"),
        "cin": party_dict.get("cin"),
        "entity_type_id": party_dict.get("entity_type_id"),
        "country_id": party_dict.get("country_id"),
        "party_type_id": "2,3",  # Customer (2) and Jute Supplier (3)
        "msme": party_dict.get("msme_certified"),
        "updated_by": updated_by,
    })

    # Copy first party branch
    branch_row = conn.execute(
        text("SELECT * FROM party_branch_mst WHERE party_id = :pid LIMIT 1"),
        {"pid": source_party_id},
    ).fetchone()

    new_branch_id = None
    if branch_row:
        bd = branch_row._mapping
        new_branch_id = DatabaseConnection.execute_insert_returning_id(conn, """
            INSERT INTO party_branch_mst (party_id, active, gst_no, address,
                address_additional, zip_code, city_id, state_id, contact_no,
                contact_person, created_date, created_by, updated_by, updated_date_time)
            VALUES (:party_id, 1, :gst_no, :address, :address_additional,
                :zip_code, :city_id, :state_id, :contact_no, :contact_person,
                NOW(), :created_by, :updated_by, NOW())
        """, {
            "party_id": new_party_id,
            "gst_no": bd.get("gst_no"),
            "address": bd.get("address"),
            "address_additional": bd.get("address_additional"),
            "zip_code": bd.get("zip_code"),
            "city_id": bd.get("city_id"),
            "state_id": bd.get("state_id"),
            "contact_no": bd.get("contact_no"),
            "contact_person": bd.get("contact_person"),
            "created_by": updated_by,
            "updated_by": updated_by,
        })

    return new_party_id, new_branch_id


def _ensure_company_as_party(conn, company_co_id: int, company_branch_id: int,
                              in_co_id: int, updated_by: int) -> tuple[int, int]:
    """Ensure a company exists as a party in another company's party_mst.

    Creates the party from co_mst/branch_mst data if it doesn't exist.

    Returns (party_id, party_branch_id) in ``in_co_id``'s context.
    """
    # Get company name
    co_row = conn.execute(
        text("SELECT * FROM co_mst WHERE co_id = :cid"),
        {"cid": company_co_id},
    ).fetchone()
    if not co_row:
        raise ValueError(f"Company co_id {company_co_id} not found")
    co = co_row._mapping

    # Check if already exists as party
    existing = _find_party_by_supp_name(conn, co["co_name"], in_co_id)
    if existing:
        # Ensure existing party has correct party types
        _ensure_party_types(conn, existing, updated_by)
        branch_id = _get_party_branch_id(conn, existing)
        return existing, branch_id

    # Create party from company master
    supp_code = _generate_supp_code(conn, in_co_id)
    new_party_id = DatabaseConnection.execute_insert_returning_id(conn, """
        INSERT INTO party_mst (supp_name, prefix, active, co_id,
            supp_email_id, party_pan_no, cin, country_id,
            supp_code, party_type_id, updated_by, updated_date_time)
        VALUES (:name, :prefix, 1, :co_id,
            :email, :pan, :cin, :country_id,
            :supp_code, :party_type_id, :updated_by, NOW())
    """, {
        "name": co["co_name"],
        "prefix": co.get("co_prefix"),
        "co_id": in_co_id,
        "email": co.get("co_email_id"),
        "pan": co.get("co_pan_no"),
        "cin": co.get("co_cin_no"),
        "country_id": co.get("country_id"),
        "supp_code": supp_code,
        "party_type_id": "2,3",  # Customer (2) and Jute Supplier (3)
        "updated_by": updated_by,
    })

    # Create party branch from branch_mst
    br_row = conn.execute(
        text("SELECT * FROM branch_mst WHERE branch_id = :bid"),
        {"bid": company_branch_id},
    ).fetchone()

    new_branch_id = None
    if br_row:
        br = br_row._mapping
        new_branch_id = DatabaseConnection.execute_insert_returning_id(conn, """
            INSERT INTO party_branch_mst (party_id, active, gst_no, address,
                address_additional, zip_code, city_id, state_id, contact_no,
                contact_person, created_date, created_by, updated_by, updated_date_time)
            VALUES (:party_id, 1, :gst_no, :address, :address2,
                :zip_code, :city_id, :state_id, :contact_no, :contact_person,
                NOW(), :created_by, :updated_by, NOW())
        """, {
            "party_id": new_party_id,
            "gst_no": br.get("gst_no"),
            "address": br.get("branch_address1"),
            "address2": br.get("branch_address2"),
            "zip_code": br.get("branch_zipcode"),
            "city_id": br.get("city_id"),
            "state_id": br.get("state_id"),
            "contact_no": str(br.get("contact_no") or ""),
            "contact_person": br.get("contact_person"),
            "created_by": updated_by,
            "updated_by": updated_by,
        })

    return new_party_id, new_branch_id


def _ensure_supplier_party(conn, source_mr: dict, target_co_id: int,
                            updated_by: int) -> tuple[int, int]:
    """Ensure the original supplier's party exists in the target company.

    Looks up the source party's supp_name and checks if it exists in
    target_co_id. If not, copies it.

    Also ensures the jute_supp_party_map entry exists.

    Returns (party_id, party_branch_id) in target company.
    """
    source_party_id = int(source_mr.get("party_id") or 0)
    jute_supplier_id = int(source_mr.get("jute_supplier_id") or 0)

    # Derive owner co_id from branch_id
    source_branch_id = int(source_mr.get("branch_id") or 0)
    result = conn.execute(
        text("SELECT co_id FROM branch_mst WHERE branch_id = :bid"),
        {"bid": source_branch_id},
    )
    row = result.fetchone()
    source_co_id = row[0] if row else 0

    # Get the supplier name from source company's party record
    if source_party_id:
        party_row = conn.execute(
            text("SELECT supp_name FROM party_mst WHERE party_id = :pid"),
            {"pid": source_party_id},
        ).fetchone()
        supp_name = party_row[0] if party_row else None
    else:
        supp_name = None

    if not supp_name:
        raise ValueError(
            f"Cannot resolve supplier name for party_id={source_party_id} "
            f"in source MR {source_mr.get('jute_mr_id')}"
        )

    # Check if party already exists in target company
    existing = _find_party_by_supp_name(conn, supp_name, target_co_id)
    if existing:
        # Ensure existing party has correct party types
        _ensure_party_types(conn, existing, updated_by)
        branch_id = _get_party_branch_id(conn, existing)
        _ensure_supplier_party_map(conn, jute_supplier_id, target_co_id, existing, updated_by)
        return existing, branch_id

    # Copy party from source
    new_party_id, new_branch_id = _create_party_from_source(
        conn, source_party_id, source_co_id, target_co_id, updated_by
    )
    _ensure_supplier_party_map(conn, jute_supplier_id, target_co_id, new_party_id, updated_by)
    return new_party_id, new_branch_id


def _ensure_supplier_party_map(conn, jute_supplier_id: int, co_id: int,
                                party_id: int, updated_by: int) -> None:
    """Ensure a jute_supp_party_map row exists for (co_id, jute_supplier_id)."""
    if not jute_supplier_id:
        return

    existing = conn.execute(
        text("""SELECT map_id FROM jute_supp_party_map
                WHERE co_id = :co_id AND jute_supplier_id = :sid LIMIT 1"""),
        {"co_id": co_id, "sid": jute_supplier_id},
    ).fetchone()

    if not existing:
        conn.execute(
            text("""INSERT INTO jute_supp_party_map
                    (co_id, jute_supplier_id, party_id, updated_by, updated_date_time)
                    VALUES (:co_id, :sid, :pid, :updated_by, NOW())"""),
            {"co_id": co_id, "sid": jute_supplier_id, "pid": party_id,
             "updated_by": updated_by},
        )


# ---------------------------------------------------------------------------
# Item / Item Group lookup / creation helpers
# ---------------------------------------------------------------------------

def _ensure_item_group(conn, source_item_grp_id: int, target_co_id: int,
                       updated_by: int) -> int:
    """Ensure an item group exists in the target company.

    Looks up the source item group, checks if one with the same name
    exists in target_co_id. If not, copies it.

    Returns the target company's item_grp_id.
    """
    # Fetch source group
    grp_row = conn.execute(
        text("SELECT * FROM item_grp_mst WHERE item_grp_id = :id"),
        {"id": source_item_grp_id},
    ).fetchone()
    if not grp_row:
        raise ValueError(f"Source item_grp_id {source_item_grp_id} not found")
    grp = grp_row._mapping

    # Already in target company?
    existing = conn.execute(
        text("""SELECT item_grp_id FROM item_grp_mst
                WHERE LOWER(TRIM(item_grp_name)) = LOWER(TRIM(:name))
                AND co_id = :co_id LIMIT 1"""),
        {"name": grp["item_grp_name"], "co_id": target_co_id},
    ).fetchone()
    if existing:
        return existing[0]

    # Create copy for target company
    new_grp_id = DatabaseConnection.execute_insert_returning_id(conn, """
        INSERT INTO item_grp_mst (item_grp_name, item_grp_code, parent_grp_id,
            co_id, item_type_id, purchase_code, active,
            updated_by, updated_date_time)
        VALUES (:name, :code, NULL, :co_id, :item_type_id, :purchase_code,
            :active, :updated_by, NOW())
    """, {
        "name": grp["item_grp_name"],
        "code": grp.get("item_grp_code"),
        "co_id": target_co_id,
        "item_type_id": grp.get("item_type_id"),
        "purchase_code": grp.get("purchase_code"),
        "active": grp.get("active", "1"),
        "updated_by": updated_by,
    })
    return new_grp_id


def _ensure_item(conn, source_item_id: int, target_co_id: int,
                 updated_by: int) -> int:
    """Ensure an item exists under the correct group in the target company.

    Looks up the source item, ensures its item group exists in the target
    company, then checks if an item with the same name exists under any
    group in the target company. If not, copies it.

    Returns the target company's item_id.
    """
    # Fetch source item
    item_row = conn.execute(
        text("SELECT * FROM item_mst WHERE item_id = :id"),
        {"id": source_item_id},
    ).fetchone()
    if not item_row:
        raise ValueError(f"Source item_id {source_item_id} not found")
    item = item_row._mapping

    # Ensure item group exists in target company
    source_grp_id = item.get("item_grp_id")
    target_grp_id = _ensure_item_group(conn, source_grp_id, target_co_id, updated_by) if source_grp_id else None

    # Check if item already exists in target company (by name + company via group join)
    existing = conn.execute(
        text("""SELECT i.item_id FROM item_mst i
                INNER JOIN item_grp_mst g ON i.item_grp_id = g.item_grp_id
                WHERE LOWER(TRIM(i.item_name)) = LOWER(TRIM(:name))
                AND g.co_id = :co_id LIMIT 1"""),
        {"name": item["item_name"], "co_id": target_co_id},
    ).fetchone()
    if existing:
        return existing[0]

    # Create copy for target company
    new_item_id = DatabaseConnection.execute_insert_returning_id(conn, """
        INSERT INTO item_mst (item_name, item_code, legacy_item_code, hsn_code,
            item_grp_id, uom_id, tangible, saleable, consumable, purchaseable,
            manufacturable, assembly, tax_percentage, uom_rounding, rate_rounding,
            active, updated_by, updated_date_time)
        VALUES (:item_name, :item_code, :legacy_item_code, :hsn_code,
            :item_grp_id, :uom_id, :tangible, :saleable, :consumable, :purchaseable,
            :manufacturable, :assembly, :tax_percentage, :uom_rounding, :rate_rounding,
            :active, :updated_by, NOW())
    """, {
        "item_name": item["item_name"],
        "item_code": item.get("item_code"),
        "legacy_item_code": item.get("legacy_item_code"),
        "hsn_code": item.get("hsn_code"),
        "item_grp_id": target_grp_id,
        "uom_id": item.get("uom_id"),
        "tangible": item.get("tangible"),
        "saleable": item.get("saleable"),
        "consumable": item.get("consumable"),
        "purchaseable": item.get("purchaseable"),
        "manufacturable": item.get("manufacturable"),
        "assembly": item.get("assembly"),
        "tax_percentage": item.get("tax_percentage"),
        "uom_rounding": item.get("uom_rounding"),
        "rate_rounding": item.get("rate_rounding"),
        "active": item.get("active", 1),
        "updated_by": updated_by,
    })
    return new_item_id


# ---------------------------------------------------------------------------
# Gate entry / MR number helpers
# ---------------------------------------------------------------------------

def _get_next_gate_entry_no(conn, branch_id: int) -> int:
    """Get the next gate entry number for a branch in the current FY."""
    fy_start, fy_end = _get_financial_year_bounds()
    result = conn.execute(
        text("""SELECT COALESCE(MAX(jute_gate_entry_no), 0) AS max_no
                FROM jute_mr
                WHERE branch_id = :bid
                AND jute_gate_entry_date BETWEEN :fy_start AND :fy_end"""),
        {"bid": branch_id, "fy_start": fy_start.strftime("%Y-%m-%d"),
         "fy_end": fy_end.strftime("%Y-%m-%d")},
    )
    return int(result.scalar() or 0) + 1


def _get_next_mr_number_in_txn(conn, branch_id: int) -> int:
    """Get next branch_mr_no inside an existing transaction."""
    fy_start, fy_end = _get_financial_year_bounds()
    result = conn.execute(
        text("""SELECT COALESCE(MAX(branch_mr_no), 0) AS max_no
                FROM jute_mr
                WHERE branch_id = :bid
                AND jute_mr_date BETWEEN :fy_start AND :fy_end"""),
        {"bid": branch_id, "fy_start": fy_start.strftime("%Y-%m-%d"),
         "fy_end": fy_end.strftime("%Y-%m-%d")},
    )
    return int(result.scalar() or 0) + 1


def _get_next_bill_pass_no_in_txn(conn, branch_id: int) -> int:
    """Get next bill_pass_no inside an existing transaction.

    Bill pass numbers are sequential per branch within a financial year
    (April 1 to March 31).
    """
    fy_start, fy_end = _get_financial_year_bounds()
    result = conn.execute(
        text("""SELECT COALESCE(MAX(bill_pass_no), 0) AS max_no
                FROM jute_mr
                WHERE branch_id = :bid
                AND bill_pass_date BETWEEN :fy_start AND :fy_end"""),
        {"bid": branch_id, "fy_start": fy_start.strftime("%Y-%m-%d"),
         "fy_end": fy_end.strftime("%Y-%m-%d")},
    )
    return int(result.scalar() or 0) + 1


# ---------------------------------------------------------------------------
# MR creation
# ---------------------------------------------------------------------------

def _create_mr(conn, source_mr: dict, step: TransferStep,
               party_id: int, party_branch_id: Optional[int],
               updated_by: int, rate_multiplier: float,
               prev_co_id: int, root_mr_id: int,
               challan_date: Optional[date] = None,
               challan_no: Optional[str] = None,
               seller_invoice: Optional[dict] = None,
               use_new_rounding: bool = False) -> int:
    """Create a new jute_mr + jute_mr_li records for a transfer step.

    Copies most fields from the source MR, overriding company/branch/party/rate.
    If challan_date/challan_no are provided, uses those instead of source_mr values.

    When use_new_rounding=True, rounds rates at kg level (2 decimals) then *100,
    and rounds line item amounts to 2 decimals (no largest-item adjustment).

    If seller_invoice is provided (intermediate buyer step), writes
    invoice_no/invoice_date/invoice_amount onto the new MR row from the
    just-created sales_invoice. When None (first-step / supplier delivery),
    those columns are written as NULL.

    Returns the new jute_mr_id.
    """
    # Ensure the party has correct party types before creating MR
    _ensure_party_types(conn, party_id, updated_by)

    # Generate bill_pass_no for this branch/financial year
    new_bill_pass_no = _get_next_bill_pass_no_in_txn(conn, step.branch_id)
    _debug_log(f"_create_mr: branch_id={step.branch_id}, mr_date={step.mr_date}, "
               f"bill_pass_no={new_bill_pass_no}")

    new_mr_id = DatabaseConnection.execute_insert_returning_id(conn, """
        INSERT INTO jute_mr (
            jute_gate_entry_no, branch_mr_no, jute_gate_entry_date,
            jute_mr_date, challan_date, challan_no, challan_weight,
            gross_weight, tare_weight, net_weight, variable_shortage,
            actual_weight, in_time, out_date, out_time, qc_check,
            mukam_id, unit_conversion, mr_weight, remarks, status_id,
            vehicle_no, marketing_slip, transporter, driver_name, frieght_paid,
            updated_by, updated_date_time, po_id, branch_id, party_id,
            party_branch_id, jute_supplier_id, src_com_id,
            total_amount, claim_amount, roundoff, net_total, tds_amount,
            src_jute_mr_id, bill_pass_no, bill_pass_date,
            invoice_no, invoice_date, invoice_amount
        ) VALUES (
            :gate_entry_no, :branch_mr_no, :gate_entry_date,
            :mr_date, :challan_date, :challan_no, :challan_weight,
            :gross_weight, :tare_weight, :net_weight, :variable_shortage,
            :actual_weight, :in_time, :out_date, :out_time, :qc_check,
            :mukam_id, :unit_conversion, :mr_weight, :remarks, :status_id,
            :vehicle_no, :marketing_slip, :transporter, :driver_name, :frieght_paid,
            :updated_by, NOW(), :po_id, :branch_id, :party_id,
            :party_branch_id, :jute_supplier_id, :src_com_id,
            :total_amount, :claim_amount, :roundoff, :net_total, :tds_amount,
            :src_jute_mr_id, :bill_pass_no, :bill_pass_date,
            :invoice_no, :invoice_date, :invoice_amount
        )
    """, {
        "gate_entry_no": _get_next_gate_entry_no(conn, step.branch_id),
        "branch_mr_no": step.mr_no,
        "gate_entry_date": step.mr_date,
        "mr_date": step.mr_date,
        "challan_date": challan_date or source_mr.get("challan_date"),
        "challan_no": challan_no or source_mr.get("challan_no"),
        "challan_weight": source_mr.get("challan_weight"),
        "gross_weight": source_mr.get("gross_weight"),
        "tare_weight": source_mr.get("tare_weight"),
        "net_weight": source_mr.get("net_weight"),
        "variable_shortage": source_mr.get("variable_shortage"),
        "actual_weight": source_mr.get("actual_weight"),
        "in_time": source_mr.get("in_time"),
        "out_date": source_mr.get("out_date"),
        "out_time": source_mr.get("out_time"),
        "qc_check": source_mr.get("qc_check"),
        "mukam_id": source_mr.get("mukam_id"),
        "unit_conversion": source_mr.get("unit_conversion"),
        "mr_weight": source_mr.get("mr_weight"),
        "remarks": source_mr.get("remarks"),
        "status_id": 3,  # Approved
        "vehicle_no": source_mr.get("vehicle_no") if step.transfer_transport else None,
        "marketing_slip": source_mr.get("marketing_slip"),
        "transporter": source_mr.get("transporter") if step.transfer_transport else None,
        "driver_name": source_mr.get("driver_name") if step.transfer_transport else None,
        "frieght_paid": source_mr.get("frieght_paid") if step.transfer_transport else None,
        "updated_by": updated_by,
        "po_id": None,  # PO is company-specific
        "branch_id": step.branch_id,
        "party_id": party_id,
        "party_branch_id": party_branch_id,
        "jute_supplier_id": source_mr.get("jute_supplier_id"),
        "src_com_id": prev_co_id,  # received-from company
        "total_amount": step.total_amount,
        "claim_amount": step.claim_amount,
        "roundoff": step.roundoff,
        "net_total": step.net_amount,
        "tds_amount": source_mr.get("tds_amount"),
        "src_jute_mr_id": root_mr_id,  # always root
        "bill_pass_no": new_bill_pass_no,
        "bill_pass_date": step.mr_date,
        "invoice_no": str(seller_invoice["invoice_no"]) if seller_invoice else None,
        "invoice_date": seller_invoice["invoice_date"] if seller_invoice else None,
        "invoice_amount": seller_invoice["invoice_amount"] if seller_invoice else None,
    })

    # Copy line items with per-item rate via rate_multiplier.
    li_data = []
    for li in source_mr.get("line_items", []):
        accepted_weight = round(float(li.get("accepted_weight") or 0), 0)
        original_rate = float(li.get("rate") or 0)

        if use_new_rounding:
            # Round at kg level (2 decimals), then convert back to quintal
            raw_quintal = original_rate * rate_multiplier
            rate_kg = float(Decimal(str(raw_quintal / 100.0))
                            .quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
            new_rate = rate_kg * 100
            # Use shared function for amount (rounded to 2 decimals)
            total_price = _calculate_line_item_amount(accepted_weight, new_rate)
        else:
            new_rate = float(Decimal(str(original_rate * rate_multiplier)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
            total_price = float(
                (Decimal(str(accepted_weight)) * Decimal(str(new_rate)) / Decimal('100'))
                .quantize(Decimal('1'), rounding=ROUND_HALF_UP)
            )

        # Map items to target company
        target_actual_item_id = li.get("actual_item_id")
        if target_actual_item_id:
            target_actual_item_id = _ensure_item(
                conn, int(target_actual_item_id), step.co_id, updated_by
            )

        target_challan_item_id = li.get("challan_item_id")
        if target_challan_item_id:
            target_challan_item_id = _ensure_item(
                conn, int(target_challan_item_id), step.co_id, updated_by
            )

        li_data.append({
            "li": li,
            "accepted_weight": accepted_weight,
            "new_rate": new_rate,
            "total_price": total_price,
            "target_actual_item_id": target_actual_item_id,
            "target_challan_item_id": target_challan_item_id,
        })

    if not use_new_rounding:
        # Old behavior: adjust largest line item to match header total exactly
        target_total = float(step.total_amount)
        li_sum = sum(d["total_price"] for d in li_data)
        diff = round(target_total - li_sum, 0)
        if diff != 0 and li_data:
            largest_idx = max(range(len(li_data)), key=lambda k: li_data[k]["total_price"])
            li_data[largest_idx]["total_price"] += diff

    for d in li_data:
        li = d["li"]
        conn.execute(text("""
            INSERT INTO jute_mr_li (
                jute_mr_id, jute_po_li_id, actual_item_id, actual_quality,
                actual_qty, actual_weight, challan_item_id, challan_quality_id,
                challan_quantity, challan_weight, allowable_moisture, actual_moisture,
                claim_dust, shortage_kgs, accepted_weight, rate, claim_rate,
                water_damage_amount, premium_amount, total_price, claim_quality,
                warehouse_id, remarks, status, marka, crop_year, active,
                updated_date_time, unit_conversion, actual_rate
            ) VALUES (
                :jute_mr_id, :jute_po_li_id, :actual_item_id, :actual_quality,
                :actual_qty, :actual_weight, :challan_item_id, :challan_quality_id,
                :challan_quantity, :challan_weight, :allowable_moisture, :actual_moisture,
                :claim_dust, :shortage_kgs, :accepted_weight, :rate, :claim_rate,
                :water_damage_amount, :premium_amount, :total_price, :claim_quality,
                :warehouse_id, :remarks, :status, :marka, :crop_year, 1,
                NOW(), :unit_conversion, :actual_rate
            )
        """), {
            "jute_mr_id": new_mr_id,
            "jute_po_li_id": None,  # PO is company-specific
            "actual_item_id": d["target_actual_item_id"],
            "actual_quality": li.get("actual_quality"),
            "actual_qty": li.get("actual_qty"),
            "actual_weight": li.get("actual_weight"),
            "challan_item_id": d["target_challan_item_id"],
            "challan_quality_id": li.get("challan_quality_id"),
            "challan_quantity": li.get("challan_quantity"),
            "challan_weight": li.get("challan_weight"),
            "allowable_moisture": li.get("allowable_moisture"),
            "actual_moisture": li.get("actual_moisture"),
            "claim_dust": li.get("claim_dust"),
            "shortage_kgs": li.get("shortage_kgs"),
            "accepted_weight": d["accepted_weight"],
            "rate": d["new_rate"],
            "claim_rate": li.get("claim_rate", 0),
            "water_damage_amount": li.get("water_damage_amount", 0),
            "premium_amount": li.get("premium_amount", 0),
            "total_price": d["total_price"],
            "claim_quality": li.get("claim_quality"),
            "warehouse_id": step.warehouse_id,
            "remarks": li.get("remarks"),
            "status": li.get("status"),
            "marka": li.get("marka"),
            "crop_year": li.get("crop_year"),
            "unit_conversion": li.get("unit_conversion"),
            "actual_rate": None,
        })

    return new_mr_id


# ---------------------------------------------------------------------------
# Sales invoice creation
# ---------------------------------------------------------------------------

def _get_next_invoice_no(conn, branch_id: int) -> int:
    """Get the next sequential invoice number for a branch in the current FY."""
    fy_start, fy_end = _get_financial_year_bounds()
    result = conn.execute(
        text("""SELECT COALESCE(MAX(invoice_no), 0) AS max_no
                FROM sales_invoice
                WHERE branch_id = :bid
                AND invoice_date BETWEEN :fy_start AND :fy_end"""),
        {
            "bid": branch_id,
            "fy_start": fy_start.strftime("%Y-%m-%d"),
            "fy_end": fy_end.strftime("%Y-%m-%d"),
        },
    )
    row = result.fetchone()
    return int(row[0] or 0) + 1


def _get_next_challan_no(conn, branch_id: int, challan_date: date) -> str:
    """Get the next sequential challan number for a branch in the format month/number.

    Format: MonthName/NNNN (e.g., May/0001)
    """
    # Get month name (abbreviated, uppercase)
    month_name = challan_date.strftime("%b").upper()  # e.g., "MAY"
    month_start = challan_date.replace(day=1)
    month_end = (challan_date.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)

    # Find max challan number for this month
    result = conn.execute(
        text("""SELECT COALESCE(MAX(CAST(SUBSTRING(challan_no, POSITION('/' IN challan_no) + 1) AS UNSIGNED)), 0) AS max_no
                FROM sales_invoice
                WHERE branch_id = :bid
                AND challan_date BETWEEN :month_start AND :month_end
                AND challan_no IS NOT NULL"""),
        {
            "bid": branch_id,
            "month_start": month_start.strftime("%Y-%m-%d"),
            "month_end": month_end.strftime("%Y-%m-%d"),
        },
    )
    row = result.fetchone()
    next_num = int(row[0] or 0) + 1

    return f"{month_name}/{next_num:04d}"


def _create_sales_invoice(conn, seller_step: TransferStep,
                           buyer_party_id: int, buyer_party_branch_id: Optional[int],
                           mr_id: int, source_mr: dict,
                           updated_by: int, rate_multiplier: float,
                           use_new_rounding: bool = False) -> dict:
    """Create a sales invoice from the seller to the buyer.

    Inserts into sales_invoice, sales_invoice_dtl, and sales_invoice_jute.

    When use_new_rounding=True, rounds rates at kg level (2 decimals),
    line item amounts to 2 decimals, and uses roundoff instead of largest-item adjustment.

    Returns dict with keys:
        invoice_id (int): Newly created sales_invoice.invoice_id
        invoice_no (int): Sequential invoice number for the seller's branch in the FY
        invoice_date (date): Invoice date (= seller_step.mr_date)
        invoice_amount (float): Header invoice amount
        challan_date (date): Challan date (= seller_step.mr_date)
        challan_no (str): Generated challan number
    """
    invoice_no = _get_next_invoice_no(conn, seller_step.branch_id)
    challan_no = _get_next_challan_no(conn, seller_step.branch_id, seller_step.mr_date)

    # Calculate invoice amounts with precise decimal arithmetic
    line_amounts = []
    line_rates_in_kg = []  # Store rates in kg for invoice storage
    line_weights = []  # Store weights for invoice storage
    unrounded_sum = Decimal('0')  # Track unrounded total for accurate round_off calculation

    for li in source_mr.get("line_items", []):
        accepted_weight = round(float(li.get("accepted_weight") or 0), 0)
        line_weights.append(accepted_weight)

        original_rate = float(li.get("rate") or 0)

        if use_new_rounding:
            # Round at kg level (2 decimals), derive quintal rate from that
            raw_quintal = original_rate * rate_multiplier
            rate_in_kg = float(Decimal(str(raw_quintal / 100.0))
                               .quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
            new_rate = rate_in_kg * 100
            # Use shared function for amount (rounded to 2 decimals)
            amount = _calculate_line_item_amount(accepted_weight, new_rate)
            unrounded_sum += Decimal(str(amount))
        else:
            new_rate = float(Decimal(str(original_rate * rate_multiplier)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
            rate_in_kg = float(
                (Decimal(str(new_rate)) / Decimal('100'))
                .quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            )
            unrounded_amount = Decimal(str(accepted_weight)) * Decimal(str(rate_in_kg))
            unrounded_sum += unrounded_amount
            amount = float(
                unrounded_amount.quantize(Decimal('1'), rounding=ROUND_HALF_UP)
            )

        line_rates_in_kg.append(rate_in_kg)
        line_amounts.append(amount)

    invoice_amount = float(seller_step.total_amount)
    if use_new_rounding:
        # round_off = rounded-to-0 header total - sum of 2-decimal line items
        round_off = round(invoice_amount - float(unrounded_sum), 2)
    else:
        # Old behavior: round_off absorbs difference between display total and per-item sum
        per_item_sum = float(unrounded_sum.quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        round_off = round(invoice_amount - per_item_sum, 2)

    invoice_id = DatabaseConnection.execute_insert_returning_id(conn, """
        INSERT INTO sales_invoice (
            invoice_no, invoice_date, invoice_type, invoice_amount,
            party_id, billing_to_id, shipping_to_id, branch_id,
            challan_date, challan_no,
            consignment_no, consignment_date, contract_no, contract_date,
            vehicle_no, transporter_name,
            active, status_id, round_off, updated_by, updated_date_time
        ) VALUES (
            :invoice_no, :invoice_date, :invoice_type, :invoice_amount,
            :party_id, :billing_to_id, :shipping_to_id, :branch_id,
            :challan_date, :challan_no,
            :consignment_no, :consignment_date, :contract_no, :contract_date,
            :vehicle_no, :transporter_name,
            1, 3, :round_off, :updated_by, NOW()
        )
    """, {
        "invoice_no": invoice_no,
        "invoice_date": seller_step.mr_date,
        "invoice_type": RAW_JUTE_INVOICE_TYPE,
        "invoice_amount": invoice_amount,
        "party_id": buyer_party_id,
        "billing_to_id": buyer_party_branch_id,
        "shipping_to_id": buyer_party_branch_id,
        "branch_id": seller_step.branch_id,
        "challan_date": seller_step.mr_date,
        "challan_no": challan_no,
        "consignment_no": seller_step.lc_reference_no or None,
        "consignment_date": seller_step.lc_date,
        "contract_no": seller_step.po_no_for_lc or None,
        "contract_date": seller_step.order_date_for_lc,
        "vehicle_no": source_mr.get("vehicle_no") if seller_step.transfer_transport else None,
        "transporter_name": source_mr.get("transporter") if seller_step.transfer_transport else None,
        "round_off": round_off,
        "updated_by": updated_by,
    })

    if not use_new_rounding:
        # Old behavior: adjust largest line item to match invoice_amount exactly
        li_sum = sum(line_amounts)
        diff = round(invoice_amount - li_sum, 0)
        if diff != 0 and line_amounts:
            largest_idx = max(range(len(line_amounts)), key=lambda k: line_amounts[k])
            line_amounts[largest_idx] += diff

    # Line items from source MR with per-item rates
    for idx, li in enumerate(source_mr.get("line_items", [])):
        amount = line_amounts[idx]
        rate_in_kg = line_rates_in_kg[idx]
        accepted_weight = line_weights[idx]

        # Map item to seller's company
        target_item_id = li.get("actual_item_id")
        if target_item_id:
            target_item_id = _ensure_item(
                conn, int(target_item_id), seller_step.co_id, updated_by
            )

        # Construct line item remarks: "Raw Jute - {qty} {unit_conversion}"
        actual_qty = li.get("actual_qty") or ""
        unit_conv = li.get("unit_conversion") or ""
        remarks = f"Raw Jute - {actual_qty} {unit_conv}".strip()

        invoice_dtl_id = DatabaseConnection.execute_insert_returning_id(conn, """
            INSERT INTO sales_invoice_dtl (
                invoice_id, item_id, hsn_code, quantity, sales_weight,
                uom_id, rate, amount_without_tax, total_amount, remarks
            ) VALUES (
                :invoice_id, :item_id, :hsn_code, :quantity, :weight,
                :uom_id, :rate, :amount, :amount, :remarks
            )
        """, {
            "invoice_id": invoice_id,
            "item_id": target_item_id,
            "hsn_code": None,
            "quantity": accepted_weight,
            "weight": accepted_weight,
            "uom_id": 163,
            "rate": rate_in_kg,
            "amount": amount,
            "remarks": remarks,
        })

        # Persist per-line claim breakdown into sales_invoice_jute_dtl.
        # Formula matches get_jute_mr_with_line_items SQL (queries.py:189) so the
        # editor display and the saved invoice rows always agree.
        # Claim does NOT scale by rate_multiplier — it copies forward unchanged.
        li_claim_rate = float(li.get("claim_rate") or 0)
        li_water_damage = float(li.get("water_damage_amount") or 0)
        li_premium = float(li.get("premium_amount") or 0)
        claim_amount_dtl = round(
            (accepted_weight * li_claim_rate / 100.0) + li_water_damage - li_premium,
            2,
        )
        try:
            qty_unit_conv = int(float(li.get("actual_qty") or 0))
        except (TypeError, ValueError):
            qty_unit_conv = 0

        conn.execute(text("""
            INSERT INTO sales_invoice_jute_dtl (
                invoice_line_item_id, claim_desc, claim_rate,
                claim_amount_dtl, unit_conversion, qty_untit_conversion
            ) VALUES (
                :invoice_line_item_id, :claim_desc, :claim_rate,
                :claim_amount_dtl, :unit_conversion, :qty_untit_conversion
            )
        """), {
            "invoice_line_item_id": invoice_dtl_id,
            "claim_desc": li.get("claim_quality"),
            "claim_rate": li_claim_rate,
            "claim_amount_dtl": claim_amount_dtl,
            "unit_conversion": li.get("unit_conversion"),
            "qty_untit_conversion": qty_unit_conv,
        })

    # Jute-specific invoice data
    conn.execute(text("""
        INSERT INTO sales_invoice_jute (
            invoice_id, mr_no, mr_id, mukam_id, claim_amount,
            unit_conversion
        ) VALUES (
            :invoice_id, :mr_no, :mr_id, :mukam_id, :claim_amount,
            :unit_conversion
        )
    """), {
        "invoice_id": invoice_id,
        "mr_no": str(seller_step.mr_no),
        "mr_id": mr_id,
        "mukam_id": source_mr.get("mukam_id"),
        "claim_amount": int(seller_step.claim_amount or 0),
        "unit_conversion": source_mr.get("unit_conversion"),
    })

    return {
        "invoice_id": invoice_id,
        "invoice_no": invoice_no,            # int (BigInteger from sales_invoice)
        "invoice_date": seller_step.mr_date, # date
        "invoice_amount": invoice_amount,    # float
        "challan_date": seller_step.mr_date,
        "challan_no": challan_no,
    }


# ---------------------------------------------------------------------------
# Update original MR
# ---------------------------------------------------------------------------

def _update_original_mr(conn, jute_mr_id: int, rate_multiplier: float,
                         final_party_id: int, final_party_branch_id: Optional[int],
                         source_mr: dict, branch_id: int,
                         mr_date: date, updated_by: int,
                         target_total: Optional[float] = None,
                         rate_source_line_items: Optional[list] = None,
                         use_new_rounding: bool = False,
                         challan_no: Optional[str] = None,
                         challan_date: Optional[date] = None) -> None:
    """Update the original MR with final rate/party and assign branch_mr_no.

    Args:
        rate_source_line_items: When provided, use these line items' rates as the
            base for applying rate_multiplier (previous step's DB rates), while
            using source_mr's line items for jute_mr_li_id (row to UPDATE).
            Position-based mapping — line items are always in the same order.
        use_new_rounding: When True, round rates at kg level (2 decimals),
            amounts to 2 decimals, no largest-item adjustment.
        challan_no: If provided, set on the source MR (receiving challan from
            the previous hop's sales invoice). If None, column is left alone.
        challan_date: If provided, set on the source MR. If None, column is
            left alone. mukam_id is never touched.
    """
    # Ensure the final party has correct party types before updating MR
    _ensure_party_types(conn, final_party_id, updated_by)

    # Assign branch_mr_no and bill_pass_no
    new_mr_no = _get_next_mr_number_in_txn(conn, branch_id)
    new_bill_pass_no = _get_next_bill_pass_no_in_txn(conn, branch_id)
    _debug_log(f"_update_original_mr: jute_mr_id={jute_mr_id}, branch_id={branch_id}, "
               f"mr_date={mr_date}, new_mr_no={new_mr_no}, new_bill_pass_no={new_bill_pass_no}")

    # Update each line item with its computed absolute rate.
    # source_mr provides the jute_mr_li_id (which DB row to update).
    # rate_source_line_items (if given) provides the base rates from the
    # previous step's saved MR, so we apply only a single-step multiplier.
    root_line_items = source_mr.get("line_items", [])
    rate_items = rate_source_line_items or root_line_items

    li_updates = []
    for idx, li in enumerate(root_line_items):
        li_id = li["jute_mr_li_id"]
        base_rate = float(rate_items[idx].get("rate") or 0) if idx < len(rate_items) else float(li.get("rate") or 0)
        accepted_weight = round(float(li.get("accepted_weight") or 0), 0)

        if use_new_rounding:
            # Round at kg level (2 decimals), then convert back to quintal
            raw_quintal = base_rate * rate_multiplier
            rate_kg = float(Decimal(str(raw_quintal / 100.0))
                            .quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
            new_rate = rate_kg * 100
            new_total_price = _calculate_line_item_amount(accepted_weight, new_rate)
        else:
            new_rate = float(Decimal(str(base_rate * rate_multiplier)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
            new_total_price = float(
                (Decimal(str(accepted_weight)) * Decimal(str(new_rate)) / Decimal('100'))
                .quantize(Decimal('1'), rounding=ROUND_HALF_UP)
            )

        li_updates.append({"li_id": li_id, "new_rate": new_rate, "total_price": new_total_price})

    if not use_new_rounding:
        # Old behavior: adjust largest line item to match header total
        if target_total is not None and li_updates:
            li_sum = sum(d["total_price"] for d in li_updates)
            diff = round(target_total - li_sum, 0)
            if diff != 0:
                largest_idx = max(range(len(li_updates)), key=lambda k: li_updates[k]["total_price"])
                li_updates[largest_idx]["total_price"] += diff

    for d in li_updates:
        conn.execute(text("""
            UPDATE jute_mr_li SET
                rate = :rate,
                total_price = :total_price,
                updated_date_time = NOW()
            WHERE jute_mr_li_id = :li_id
        """), {"rate": d["new_rate"], "total_price": d["total_price"], "li_id": d["li_id"]})

    # Recompute header totals from line items; assign bill_pass_no and bill_pass_date.
    # challan_no / challan_date are only written when the caller passes them —
    # this keeps the "mukam_id untouched" and "leave columns alone when None"
    # invariants explicit. mukam_id is never in this UPDATE.
    optional_assignments = []
    optional_params: dict = {}
    if challan_no is not None:
        optional_assignments.append("challan_no = :challan_no")
        optional_params["challan_no"] = challan_no
    if challan_date is not None:
        optional_assignments.append("challan_date = :challan_date")
        optional_params["challan_date"] = challan_date

    optional_sql = (", " + ", ".join(optional_assignments)) if optional_assignments else ""

    conn.execute(text(f"""
        UPDATE jute_mr SET
            party_id = :party_id,
            party_branch_id = :party_branch_id,
            branch_mr_no = :mr_no,
            jute_mr_date = :mr_date,
            bill_pass_no = :bill_pass_no,
            bill_pass_date = :mr_date,
            status_id = :status_id,
            total_amount = (SELECT ROUND(COALESCE(SUM(total_price), 0), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id),
            roundoff = (SELECT ROUND(COALESCE(SUM(total_price), 0), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id) -
                       (SELECT COALESCE(SUM(total_price), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id),
            net_total = (SELECT ROUND(COALESCE(SUM(total_price), 0), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id) - claim_amount,
            updated_by = :updated_by,
            updated_date_time = NOW()
            {optional_sql}
        WHERE jute_mr_id = :mr_id
    """), {
        **optional_params,
        "party_id": str(final_party_id),
        "party_branch_id": final_party_branch_id,
        "mr_no": new_mr_no,
        "mr_date": mr_date,
        "bill_pass_no": new_bill_pass_no,
        "status_id": 1,
        "updated_by": updated_by,
        "mr_id": jute_mr_id,
    })
    _debug_log(f"UPDATE executed — bill_pass_no={new_bill_pass_no}, "
               f"bill_pass_date={mr_date} for jute_mr_id={jute_mr_id}")

    # Verify the update actually persisted
    verify = conn.execute(text(
        "SELECT bill_pass_no, bill_pass_date FROM jute_mr WHERE jute_mr_id = :mr_id"
    ), {"mr_id": jute_mr_id}).fetchone()
    _debug_log(f"VERIFY after update: bill_pass_no={verify[0]}, bill_pass_date={verify[1]}")


def revert_original_mr(conn, jute_mr_id: int, step1_source_mr: dict, updated_by: int) -> None:
    """Revert the original MR to its pre-finalization state.

    Restores line item rates from Step 1 (the first transferred MR's snapshot,
    which is unaffected by finalization), clears branch_mr_no/bill_pass_*,
    sets status_id back to Pending. Does NOT touch party_id/party_branch_id —
    the original supplier party is not reliably recoverable, and leaving the
    current party in place is safe (a re-finalize will overwrite it correctly).

    Args:
        conn: Active SQLAlchemy connection inside a transaction.
        jute_mr_id: The root MR id (the in-place finalized MR being reverted).
        step1_source_mr: Full dict of Step 1's MR (the first transferred MR
            in the chain), including a "line_items" list. Used as the source
            of pre-finalization rates. Line items are matched positionally to
            the root MR's line items (1:1 — both were cloned from the same
            source).
        updated_by: User id for audit columns.
    """
    # Load root MR's line items in stable order for positional matching
    root_lis = conn.execute(
        text("SELECT jute_mr_li_id, accepted_weight FROM jute_mr_li "
             "WHERE jute_mr_id = :id ORDER BY jute_mr_li_id"),
        {"id": jute_mr_id},
    ).fetchall()

    step1_lis = step1_source_mr.get("line_items", [])
    pair_count = min(len(step1_lis), len(root_lis))

    for idx in range(pair_count):
        root_li = root_lis[idx]
        step1_li = step1_lis[idx]
        li_id = root_li[0]
        accepted_weight = round(float(root_li[1] or 0), 0)
        rate = float(step1_li.get("rate") or 0)
        new_total = float(
            (Decimal(str(accepted_weight)) * Decimal(str(rate)) / Decimal('100'))
            .quantize(Decimal('1'), rounding=ROUND_HALF_UP)
        )

        conn.execute(text("""
            UPDATE jute_mr_li SET
                rate = :rate, total_price = :total_price, updated_date_time = NOW()
            WHERE jute_mr_li_id = :li_id
        """), {"rate": rate, "total_price": new_total, "li_id": li_id})

    # Restore header: clear branch_mr_no/bill_pass_*, status back to Pending,
    # recompute totals. Do NOT touch party_id/party_branch_id or mukam_id.
    # Also restore challan_no / challan_date from Step 1's MR: Step 1 preserved
    # the original gate-entry challan because _create_mr falls back to
    # source_mr's values when no override is supplied. Finalization overwrote
    # them with the last hop's invoice challan, so we need to put the original
    # back here.
    step1_challan_no = step1_source_mr.get("challan_no")
    step1_challan_date = step1_source_mr.get("challan_date")

    revert_assignments = []
    revert_params: dict = {"updated_by": updated_by, "mr_id": jute_mr_id}
    if step1_challan_no is not None:
        revert_assignments.append("challan_no = :challan_no")
        revert_params["challan_no"] = step1_challan_no
    if step1_challan_date is not None:
        revert_assignments.append("challan_date = :challan_date")
        revert_params["challan_date"] = step1_challan_date

    revert_sql = (", " + ", ".join(revert_assignments)) if revert_assignments else ""

    conn.execute(text(f"""
        UPDATE jute_mr SET
            branch_mr_no = NULL,
            bill_pass_no = NULL,
            bill_pass_date = NULL,
            status_id = 0,
            total_amount = (SELECT ROUND(COALESCE(SUM(total_price), 0), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id),
            roundoff = (SELECT ROUND(COALESCE(SUM(total_price), 0), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id) -
                       (SELECT COALESCE(SUM(total_price), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id),
            net_total = (SELECT ROUND(COALESCE(SUM(total_price), 0), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id) - claim_amount,
            updated_by = :updated_by,
            updated_date_time = NOW()
            {revert_sql}
        WHERE jute_mr_id = :mr_id
    """), revert_params)


# ---------------------------------------------------------------------------
# Per-step save / delete
# ---------------------------------------------------------------------------

def save_transfer_step(
    source_mr_id: int,
    step: TransferStep,
    prev_co_id: int,
    prev_branch_id: int,
    source_co_id: int,
    source_branch_id: int,
    root_mr_id: int,
    updated_by: int,
    rate_multiplier: float,
    is_first_step: bool = False,
    is_final: bool = False,
    original_source_mr_id: Optional[int] = None,
    use_new_rounding: bool = False,
) -> dict:
    """Save a single transfer step: create MR + invoice.

    Args:
        source_mr_id: MR ID for fetching rate base (prev step's MR, or root for step 0)
        step: The transfer step being saved
        prev_co_id: Company from which this step receives
        prev_branch_id: Branch of the previous step (for invoice)
        source_co_id: Original source company co_id
        source_branch_id: Original source branch_id
        root_mr_id: Root MR ID (for src_jute_mr_id)
        updated_by: User ID
        rate_multiplier: Single-step rate multiplier (1 + pct/100) for this step
        is_first_step: True if this is step[0] (supplier party, no invoice from prev)
        is_final: True if chain returns to source
        original_source_mr_id: Root MR ID when source_mr_id is a prev step's MR.
            Used by _update_original_mr to find the correct jute_mr_li_id rows.

    Returns:
        dict with keys: mr_id (int or None), invoice_id (int or None)
    """
    mr_id = None
    invoice_id = None

    _debug_log(f"save_transfer_step: is_first_step={is_first_step}, is_final={is_final}, "
               f"source_mr_id={source_mr_id}, step.co_id={step.co_id}, step.branch_id={step.branch_id}, "
               f"source_co_id={source_co_id}, source_branch_id={source_branch_id}")

    with DatabaseConnection.get_transaction() as conn:
        source_mr = get_source_mr_full(source_mr_id, conn=conn)
        if not source_mr:
            raise ValueError(f"Source MR {source_mr_id} not found")

        # Assign MR number inside transaction
        step.mr_no = _get_next_mr_number_in_txn(conn, step.branch_id)
        step.gate_entry_no = _get_next_gate_entry_no(conn, step.branch_id)

        if is_first_step:
            # Step[0]: first receiver gets MR from original supplier
            party_id, party_branch_id = _ensure_supplier_party(
                conn, source_mr, step.co_id, updated_by
            )
            # No seller_invoice: first-step is a supplier delivery, not a
            # company-to-company sale — no inbound sales_invoice exists yet,
            # so invoice_no/invoice_date/invoice_amount remain NULL on this MR.
            mr_id = _create_mr(
                conn, source_mr, step, party_id, party_branch_id,
                updated_by, rate_multiplier, prev_co_id, root_mr_id,
                challan_date=step.challan_date,
                use_new_rounding=use_new_rounding,
            )
        else:
            # Intermediate or final: create invoice from seller, then MR for buyer
            # 1. Ensure buyer exists as party in seller's company
            buyer_party_id, buyer_party_branch_id = _ensure_company_as_party(
                conn, step.co_id, step.branch_id, prev_co_id, updated_by
            )
            # 2. Create sales invoice from seller
            prev_step_for_invoice = TransferStep(
                co_id=prev_co_id, branch_id=prev_branch_id,
                mr_date=step.mr_date, mr_rate=0, total_amount=step.total_amount,
                claim_amount=step.claim_amount, net_amount=step.net_amount,
                mr_no=0, roundoff=step.roundoff,
                lc_reference_no=step.lc_reference_no,
                lc_date=step.lc_date,
                po_no_for_lc=step.po_no_for_lc,
                order_date_for_lc=step.order_date_for_lc,
            )
            # Find the previous MR ID for invoice linkage
            prev_mr_result = conn.execute(
                text("""SELECT jute_mr_id FROM jute_mr
                        WHERE src_jute_mr_id = :root AND branch_id = :bid
                        ORDER BY jute_mr_id DESC LIMIT 1"""),
                {"root": root_mr_id, "bid": prev_branch_id},
            )
            prev_mr_row = prev_mr_result.fetchone()
            prev_mr_id = prev_mr_row[0] if prev_mr_row else source_mr_id

            seller_invoice = _create_sales_invoice(
                conn, prev_step_for_invoice, buyer_party_id,
                buyer_party_branch_id, prev_mr_id, source_mr,
                updated_by, rate_multiplier,
                use_new_rounding=use_new_rounding,
            )
            invoice_id = seller_invoice["invoice_id"]
            inv_challan_date = seller_invoice["challan_date"]
            inv_challan_no = seller_invoice["challan_no"]

            if is_final:
                # Final step: update original MR, don't create new MR.
                # source_mr may be the previous step's MR (for rates).
                # We need the root MR for jute_mr_li_id (which rows to UPDATE).
                root_mr_id_for_update = original_source_mr_id or root_mr_id
                if root_mr_id_for_update != source_mr_id:
                    root_mr = get_source_mr_full(root_mr_id_for_update, conn=conn)
                    if not root_mr:
                        raise ValueError(f"Root MR {root_mr_id_for_update} not found")
                    rate_source_lis = source_mr.get("line_items", [])
                else:
                    root_mr = source_mr
                    rate_source_lis = None  # same MR, no separate rate source needed

                _debug_log(f"FINAL STEP — calling _update_original_mr "
                           f"for root_mr_id={root_mr_id_for_update}, source_branch_id={source_branch_id}, mr_date={step.mr_date}")
                last_seller_party_id, last_seller_party_branch_id = _ensure_company_as_party(
                    conn, prev_co_id, prev_branch_id, source_co_id, updated_by
                )
                # Inherit challan_no/challan_date from the previous hop's invoice,
                # mirroring the non-final branch below. mukam_id is preserved
                # (not in the UPDATE inside _update_original_mr).
                _update_original_mr(
                    conn, root_mr_id_for_update, rate_multiplier,
                    last_seller_party_id, last_seller_party_branch_id,
                    root_mr, source_branch_id, step.mr_date, updated_by,
                    target_total=float(step.total_amount),
                    rate_source_line_items=rate_source_lis,
                    use_new_rounding=use_new_rounding,
                    challan_no=inv_challan_no,
                    challan_date=step.challan_date or inv_challan_date,
                )
            else:
                # Create MR for buyer
                seller_party_id, seller_party_branch_id = _ensure_company_as_party(
                    conn, prev_co_id, prev_branch_id, step.co_id, updated_by
                )
                jute_supplier_id = int(source_mr.get("jute_supplier_id") or 0)
                _ensure_supplier_party_map(
                    conn, jute_supplier_id, step.co_id, seller_party_id, updated_by
                )
                mr_id = _create_mr(
                    conn, source_mr, step, seller_party_id, seller_party_branch_id,
                    updated_by, rate_multiplier, prev_co_id, root_mr_id,
                    challan_date=step.challan_date or inv_challan_date,
                    challan_no=inv_challan_no,
                    seller_invoice=seller_invoice,
                    use_new_rounding=use_new_rounding,
                )

    logger.info(f"Transfer step saved for MR {source_mr_id}: mr_id={mr_id}, invoice_id={invoice_id}")
    return {"mr_id": mr_id, "invoice_id": invoice_id}


def delete_transfer_step(jute_mr_id: int, updated_by: int) -> None:
    """Delete a transfer MR and its associated invoices.

    Invoices created for this step are linked to the PREVIOUS step's MR,
    so this deletes:
    1. Invoices directly linked to this MR
    2. Invoices linked to the previous MR (created as input to this step)
    """
    with DatabaseConnection.get_transaction() as conn:
        # Get info about the MR being deleted
        mr_info = conn.execute(
            text("SELECT src_jute_mr_id, branch_id FROM jute_mr WHERE jute_mr_id = :id"),
            {"id": jute_mr_id},
        ).fetchone()

        if not mr_info:
            return

        root_mr_id, current_branch_id = mr_info

        # Find the previous MR in the chain (same root, different branch, most recent)
        prev_mr_result = conn.execute(
            text("""SELECT jute_mr_id FROM jute_mr
                    WHERE src_jute_mr_id = :root AND branch_id != :bid
                    ORDER BY jute_mr_id DESC LIMIT 1"""),
            {"root": root_mr_id, "bid": current_branch_id},
        )
        prev_mr_row = prev_mr_result.fetchone()
        prev_mr_id = prev_mr_row[0] if prev_mr_row else None

        # Collect all invoices to delete
        invoice_ids_to_delete = set()

        # Find invoices linked to the current MR
        inv_rows = conn.execute(
            text("SELECT invoice_id FROM sales_invoice_jute WHERE mr_id = :mr_id"),
            {"mr_id": jute_mr_id},
        ).fetchall()
        for inv_row in inv_rows:
            invoice_ids_to_delete.add(inv_row[0])

        # Find invoices linked to the previous MR (sale invoices created as input to this step)
        if prev_mr_id:
            prev_inv_rows = conn.execute(
                text("SELECT invoice_id FROM sales_invoice_jute WHERE mr_id = :mr_id"),
                {"mr_id": prev_mr_id},
            ).fetchall()
            for inv_row in prev_inv_rows:
                invoice_ids_to_delete.add(inv_row[0])

        # Delete all invoices
        for inv_id in invoice_ids_to_delete:
            conn.execute(text("DELETE FROM sales_invoice_jute WHERE invoice_id = :id"), {"id": inv_id})
            conn.execute(text("DELETE FROM sales_invoice_dtl WHERE invoice_id = :id"), {"id": inv_id})
            conn.execute(text("DELETE FROM sales_invoice WHERE invoice_id = :id"), {"id": inv_id})

        # Delete MR line items and MR
        conn.execute(text("DELETE FROM jute_mr_li WHERE jute_mr_id = :id"), {"id": jute_mr_id})
        conn.execute(text("DELETE FROM jute_mr WHERE jute_mr_id = :id"), {"id": jute_mr_id})

    logger.info(f"Deleted transfer MR {jute_mr_id} and linked invoices")


def delete_chain_from_step(root_mr_id: int, from_mr_id: int, updated_by: int) -> None:
    """Delete chain steps from a given point onward, OR revert finalization.

    Special case: if from_mr_id == root_mr_id, the caller is "unfinalizing"
    the in-place return-to-origin step. We do not delete any chain rows;
    we only call revert_original_mr to restore the root MR to its
    pre-finalization state, using Step 1's line items as the rate snapshot.

    Otherwise: cascade-delete transfer rows from from_mr_id onward (in
    reverse chain order), and if the chain was finalized, additionally
    revert the root MR.
    """
    from .queries import get_transfer_chain
    from .jute_mr_chain_helpers import _reconstruct_chain

    root_mr = get_source_mr_full(root_mr_id)
    if not root_mr:
        return

    chain_df = get_transfer_chain(root_mr_id)
    if chain_df is None or chain_df.empty:
        return

    chain_mrs = chain_df.to_dict("records")

    # Derive root co_id
    with DatabaseConnection.get_transaction() as conn:
        root_branch = int(root_mr.get("branch_id") or 0)
        root_co_row = conn.execute(
            text("SELECT co_id FROM branch_mst WHERE branch_id = :bid"),
            {"bid": root_branch},
        ).fetchone()
        root_co_id = root_co_row[0] if root_co_row else 0

    ordered = _reconstruct_chain(chain_mrs, root_co_id)

    was_complete = root_mr.get("branch_mr_no") is not None

    # Snapshot Step 1 BEFORE any deletes (Step 1 = first transferred MR).
    step1_source_mr = None
    if ordered:
        step1_id = ordered[0]["jute_mr_id"]
        step1_source_mr = get_source_mr_full(step1_id)

    # Branch A: root-return revert only (unfinalize the synthetic final step)
    if from_mr_id == root_mr_id:
        if not was_complete:
            return
        if step1_source_mr is None:
            logger.warning(
                f"delete_chain_from_step: cannot revert root MR {root_mr_id} — "
                f"Step 1 source MR not found."
            )
            return
        with DatabaseConnection.get_transaction() as conn:
            revert_original_mr(conn, root_mr_id, step1_source_mr, updated_by)
        return

    # Branch B: middle-step cascade delete
    from_idx = next((i for i, m in enumerate(ordered) if m["jute_mr_id"] == from_mr_id), None)
    if from_idx is None:
        return

    to_delete = ordered[from_idx:]
    for mr in reversed(to_delete):
        delete_transfer_step(mr["jute_mr_id"], updated_by)

    if was_complete:
        if step1_source_mr is None:
            logger.warning(
                f"delete_chain_from_step: cannot revert root MR {root_mr_id} — "
                f"Step 1 source MR not found; root may remain finalized."
            )
        elif from_idx == 0:
            logger.warning(
                f"delete_chain_from_step: Step 1 itself was deleted for root MR "
                f"{root_mr_id}; cannot revert finalization using Step 1's data. "
                f"Root MR may be left in a finalized state with no clean revert path."
            )
        else:
            with DatabaseConnection.get_transaction() as conn:
                revert_original_mr(conn, root_mr_id, step1_source_mr, updated_by)


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def finalize_transfer_chain(
    source_mr_id: int,
    steps: list[TransferStep],
    source_co_id: int,
    source_branch_id: int,
    updated_by: int,
) -> dict:
    """Execute full transfer chain in sequence using save_transfer_step.

    Convenience wrapper for when the full chain is known upfront.
    """
    if len(steps) < 2:
        raise ValueError("Transfer chain must have at least 2 steps")

    mr_ids = []
    invoice_ids = []
    # Track the previous step's MR ID so each step uses it as rate base
    prev_step_mr_id = source_mr_id  # start with root

    for i, step in enumerate(steps):
        # Single-step multiplier: only this step's pct increase
        single_step_multiplier = 1.0 + step.pct_rate_increase / 100.0 if i > 0 else 1.0

        prev_co_id = source_co_id if i == 0 else steps[i - 1].co_id
        prev_branch_id = source_branch_id if i == 0 else steps[i - 1].branch_id
        is_final = (i == len(steps) - 1)

        result = save_transfer_step(
            source_mr_id=prev_step_mr_id,
            step=step,
            prev_co_id=prev_co_id,
            prev_branch_id=prev_branch_id,
            source_co_id=source_co_id,
            source_branch_id=source_branch_id,
            root_mr_id=source_mr_id,
            updated_by=updated_by,
            rate_multiplier=single_step_multiplier,
            is_first_step=(i == 0),
            is_final=is_final,
            original_source_mr_id=source_mr_id,
        )

        if result.get("mr_id"):
            mr_ids.append(result["mr_id"])
            prev_step_mr_id = result["mr_id"]  # next step uses this MR as base
        if result.get("invoice_id"):
            invoice_ids.append(result["invoice_id"])

    logger.info(
        f"Transfer chain finalized for MR {source_mr_id}: "
        f"created {len(mr_ids)} MRs, {len(invoice_ids)} invoices"
    )
    return {"mr_ids": mr_ids, "invoice_ids": invoice_ids}
