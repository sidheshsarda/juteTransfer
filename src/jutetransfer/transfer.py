"""Transfer chain finalization logic for JuteTransfer.

Handles the complete finalization flow when a transfer chain returns to its
source company: masters checks, MR creation for intermediate companies,
sales invoice generation, and updating the original MR.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from sqlalchemy import text

from . import po_ops
from .database import DatabaseConnection, delete_by_ids, select_ids
from .jute_mr_chain_helpers import (
    _calculate_line_item_amount, _reconstruct_chain, hop_rate,
    is_root_eligible_for_new_chain, step_multiplier,
)
from .lot_helpers import production_rate
from .queries import get_source_mr_full, _get_financial_year_bounds

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
        if branch_id is None:
            raise ValueError(
                f"Party {existing} (from company {company_co_id}) has no branch in "
                f"party_branch_mst — decision D4 requires a party branch. Add one in Party Master."
            )
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

    if new_branch_id is None:
        raise ValueError(
            f"branch_mst {company_branch_id} (company {company_co_id}) has no data to create "
            f"a party branch from — decision D4 requires a party branch. Add one in Party Master."
        )
    return new_party_id, new_branch_id


def _ensure_party_branch_from_source_branch(
    conn, party_id: int, source_branch_id: int, updated_by: int
) -> Optional[int]:
    """Ensure a party_branch_mst row exists for party_id matching branch_mst[source_branch_id].

    Match by gst_no (case-insensitive, trimmed). If source branch_mst has no
    gst_no, inserts a new row unconditionally. Returns the party_mst_branch_id
    (existing or newly inserted), or None if source branch_mst is not found.
    """
    br_row = conn.execute(
        text("SELECT * FROM branch_mst WHERE branch_id = :bid"),
        {"bid": source_branch_id},
    ).fetchone()
    if not br_row:
        return None
    br = br_row._mapping

    src_gst = (br.get("gst_no") or "").strip()
    if src_gst:
        existing = conn.execute(
            text("""SELECT party_mst_branch_id FROM party_branch_mst
                    WHERE party_id = :pid
                      AND LOWER(TRIM(gst_no)) = LOWER(TRIM(:gst))
                    LIMIT 1"""),
            {"pid": party_id, "gst": src_gst},
        ).fetchone()
        if existing:
            return existing[0]

    return DatabaseConnection.execute_insert_returning_id(conn, """
        INSERT INTO party_branch_mst (party_id, active, gst_no, address,
            address_additional, zip_code, city_id, state_id, contact_no,
            contact_person, created_date, created_by, updated_by, updated_date_time)
        VALUES (:party_id, 1, :gst_no, :address, :address2,
            :zip_code, :city_id, :state_id, :contact_no, :contact_person,
            NOW(), :created_by, :updated_by, NOW())
    """, {
        "party_id": party_id,
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
        if branch_id is None:
            raise ValueError(
                f"Supplier party {existing} in company {target_co_id} has no branch in "
                f"party_branch_mst — decision D4 requires a party branch. Add one in Party Master."
            )
        _ensure_supplier_party_map(conn, jute_supplier_id, target_co_id, existing, updated_by)
        return existing, branch_id

    # Copy party from source
    new_party_id, new_branch_id = _create_party_from_source(
        conn, source_party_id, source_co_id, target_co_id, updated_by
    )
    if new_branch_id is None:
        raise ValueError(
            f"Supplier party (copied from party {source_party_id}) has no branch to copy into "
            f"company {target_co_id} — decision D4 requires a party branch. Add one in Party Master."
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

def _get_next_gate_entry_no(conn, branch_id: int, mr_date: date = None) -> int:
    """Get the next gate entry number for a branch within mr_date's FY.

    FY window is derived from mr_date (the date being stamped on the row),
    so back-dated or forward-dated entries are numbered within their own FY.
    mr_date=None keeps today's behavior (matches _get_financial_year_bounds).
    """
    fy_start, fy_end = _get_financial_year_bounds(mr_date)
    result = conn.execute(
        text("""SELECT COALESCE(MAX(jute_gate_entry_no), 0) AS max_no
                FROM jute_mr
                WHERE branch_id = :bid
                AND jute_gate_entry_date BETWEEN :fy_start AND :fy_end"""),
        {"bid": branch_id, "fy_start": fy_start.strftime("%Y-%m-%d"),
         "fy_end": fy_end.strftime("%Y-%m-%d")},
    )
    return int(result.scalar() or 0) + 1


def _get_next_mr_number_in_txn(conn, branch_id: int, mr_date: date) -> int:
    """Get next branch_mr_no inside an existing transaction.

    FY window is derived from mr_date (the date of the MR being inserted), so
    back-dated or forward-dated entries are numbered within their own FY.
    """
    fy_start, fy_end = _get_financial_year_bounds(mr_date)
    result = conn.execute(
        text("""SELECT COALESCE(MAX(branch_mr_no), 0) AS max_no
                FROM jute_mr
                WHERE branch_id = :bid
                AND jute_mr_date BETWEEN :fy_start AND :fy_end"""),
        {"bid": branch_id, "fy_start": fy_start.strftime("%Y-%m-%d"),
         "fy_end": fy_end.strftime("%Y-%m-%d")},
    )
    return int(result.scalar() or 0) + 1


def _get_next_bill_pass_no_in_txn(conn, branch_id: int, mr_date: date = None) -> int:
    """Get next bill_pass_no inside an existing transaction.

    Bill pass numbers are sequential per branch within a financial year
    (April 1 to March 31). FY window is derived from mr_date (the date being
    stamped on the row); mr_date=None keeps today's behavior.
    """
    fy_start, fy_end = _get_financial_year_bounds(mr_date)
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
    new_bill_pass_no = _get_next_bill_pass_no_in_txn(conn, step.branch_id, step.mr_date)

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
        "gate_entry_no": _get_next_gate_entry_no(conn, step.branch_id, step.mr_date),
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
        "invoice_no": seller_invoice["invoice_no_formatted"] if seller_invoice else None,
        "invoice_date": seller_invoice["invoice_date"] if seller_invoice else None,
        "invoice_amount": seller_invoice["invoice_amount"] if seller_invoice else None,
    })

    # Copy line items with per-item rate via rate_multiplier.
    li_data = []
    for li in source_mr.get("line_items", []):
        accepted_weight = round(float(li.get("accepted_weight") or 0), 0)
        original_rate = float(li.get("rate") or 0)

        if use_new_rounding:
            # Round at kg level (2 decimals), then convert back to quintal --
            # in Decimal, exactly as the screen does (hop_rate).
            new_rate, _ = hop_rate(original_rate, rate_multiplier)
            # Use shared function for amount (rounded to 2 decimals)
            total_price = _calculate_line_item_amount(accepted_weight, new_rate)
        else:
            new_rate = float((Decimal(str(original_rate)) * Decimal(str(rate_multiplier))).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
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
            # production rate rides along unchanged; the hop mark-up is `rate` only
            "actual_rate": production_rate(li),
        })

    return new_mr_id


# ---------------------------------------------------------------------------
# Sales invoice creation
# ---------------------------------------------------------------------------

def _get_seller_prefixes(conn, branch_id: int) -> tuple[Optional[str], Optional[str]]:
    """Fetch (co_prefix, branch_prefix) for a branch in-transaction."""
    row = conn.execute(
        text("""SELECT cm.co_prefix, bm.branch_prefix
                FROM branch_mst bm
                JOIN co_mst cm ON cm.co_id = bm.co_id
                WHERE bm.branch_id = :bid"""),
        {"bid": branch_id},
    ).fetchone()
    if not row:
        return None, None
    return row[0], row[1]


def _format_financial_year(ref_date: date) -> str:
    """FY label like '25-26' for an Indian fiscal year (Apr–Mar) containing ref_date."""
    start_year = ref_date.year if ref_date.month >= 4 else ref_date.year - 1
    return f"{start_year % 100:02d}-{(start_year + 1) % 100:02d}"


def _format_document_no(
    doc_no: Optional[int],
    co_prefix: Optional[str],
    branch_prefix: Optional[str],
    ref_date: date,
    document_type: str = "SI",
) -> str:
    """Format '<co>/<branch>/<type>/<FY>/<n>', dropping empty prefix parts.

    Returns "" when doc_no is None or 0.
    """
    if not doc_no:
        return ""
    parts: list[str] = []
    if co_prefix:
        parts.append(co_prefix)
    if branch_prefix:
        parts.append(branch_prefix)
    parts.extend([document_type, _format_financial_year(ref_date), str(doc_no)])
    return "/".join(parts)


def _get_next_invoice_no(conn, branch_id: int, invoice_date: date) -> int:
    """Next sequential invoice_no for a branch within the FY of invoice_date.

    FY window is derived from invoice_date so back-dated / forward-dated
    invoices are numbered within their own FY (matches _get_next_mr_number_in_txn).
    """
    fy_start, fy_end = _get_financial_year_bounds(invoice_date)
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
        invoice_no_formatted (str): '<co>/<branch>/SI/<FY>/<n>' for jute_mr.invoice_no
        invoice_date (date): Invoice date (= seller_step.mr_date)
        invoice_amount (float): Header invoice amount
        challan_date (date): Challan date (= seller_step.mr_date)
        challan_no (str): Generated challan number
    """
    invoice_no = _get_next_invoice_no(conn, seller_step.branch_id, seller_step.mr_date)
    challan_no = _get_next_challan_no(conn, seller_step.branch_id, seller_step.mr_date)

    co_prefix, branch_prefix = _get_seller_prefixes(conn, seller_step.branch_id)
    invoice_no_formatted = _format_document_no(
        invoice_no, co_prefix, branch_prefix, seller_step.mr_date, document_type="SI",
    )

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
            # Round at kg level (2 decimals), derive quintal rate from that --
            # in Decimal, exactly as the screen does (hop_rate).
            new_rate, rate_in_kg = hop_rate(original_rate, rate_multiplier)
            # Use shared function for amount (rounded to 2 decimals)
            amount = _calculate_line_item_amount(accepted_weight, new_rate)
            unrounded_sum += Decimal(str(amount))
        else:
            new_rate = float((Decimal(str(original_rate)) * Decimal(str(rate_multiplier))).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
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
        "invoice_no_formatted": invoice_no_formatted,  # str for jute_mr.invoice_no
        "invoice_date": seller_step.mr_date, # date
        "invoice_amount": invoice_amount,    # float
        "challan_date": seller_step.mr_date,
        "challan_no": challan_no,
    }


# ---------------------------------------------------------------------------
# Update original MR
# ---------------------------------------------------------------------------

# The ERP's money rules for an MR header, mirrored: vowerp3be
# src/juteProcurement/mr.py (recompute_mr_money, calculate_mr_amounts,
# calculate_tds_amount, get_cumulative_mr_value_for_party_in_fy) and totals.py
# (compute_jute_totals). A header the app writes is then exactly the header the
# ERP itself would write, so its bill pass list, view and save agree. Keep in
# step with those functions.
ERP_TDS_THRESHOLD = 5000000.0   # 194Q: purchases from one party in a FY
ERP_TDS_RATE = 0.001


def _erp_tds_amount(cumulative_previous: float, current_total: float) -> float:
    """calculate_tds_amount: 0.1 % of what crosses Rs 50 lakh of the party's
    approved MR value in the financial year."""
    cumulative_after = cumulative_previous + current_total
    if cumulative_after <= ERP_TDS_THRESHOLD:
        return 0.0
    if cumulative_previous >= ERP_TDS_THRESHOLD:
        applicable = current_total
    else:
        applicable = cumulative_after - ERP_TDS_THRESHOLD
    return round(applicable * ERP_TDS_RATE, 2)


def _erp_jute_totals(total_amount, claim_amount, tds_amount) -> tuple:
    """compute_jute_totals: (roundoff, net_total); the net is a whole rupee
    (Python round, as in the ERP)."""
    net_pre = round(
        float(total_amount or 0) - float(claim_amount or 0) - float(tds_amount or 0), 2
    )
    roundoff = round(round(net_pre) - net_pre, 2)
    return roundoff, round(net_pre + roundoff, 2)


def _erp_money(conn, mr_id: int, tds_amount: Optional[float] = None,
               cumulative_previous: Optional[float] = None) -> dict:
    """The header money the ERP would compute for an MR from its active lines
    (calculate_mr_amounts + calculate_tds_amount + compute_jute_totals) --
    READ ONLY. tds_amount=None derives 194Q TDS as the ERP's approve and bill
    pass do, from the party's other approved MRs in the FY of jute_mr_date --
    or from cumulative_previous, when the caller knows what had been approved
    before this MR (a repair of rows approved long ago: today every later MR
    is approved too, and would wrongly count)."""
    sums = conn.execute(text("""
        SELECT COALESCE(SUM((COALESCE(accepted_weight, actual_weight, 0) / 100) * COALESCE(rate, 0)), 0),
               COALESCE(SUM((COALESCE(accepted_weight, actual_weight, 0) / 100) * COALESCE(claim_rate, 0)), 0)
        FROM jute_mr_li WHERE jute_mr_id = :id AND (active = 1 OR active IS NULL)
    """), {"id": mr_id}).fetchone()
    raw_total, raw_claim = float(sums[0] or 0), float(sums[1] or 0)
    total_amount, claim_amount = round(raw_total, 2), round(raw_claim, 2)
    if tds_amount is None and cumulative_previous is not None:
        tds_amount = _erp_tds_amount(float(cumulative_previous), raw_total)
    if tds_amount is None:
        hdr = conn.execute(text(
            "SELECT party_id, jute_mr_date FROM jute_mr WHERE jute_mr_id = :id"
        ), {"id": mr_id}).fetchone()
        cumulative = 0.0
        if hdr and hdr[0] and hdr[1]:
            mr_date = hdr[1].date() if isinstance(hdr[1], datetime) else hdr[1]
            fy_start, fy_end = _get_financial_year_bounds(mr_date)
            cumulative = float(conn.execute(text("""
                SELECT COALESCE(SUM(COALESCE(total_amount, 0)), 0)
                FROM jute_mr
                WHERE party_id = :party AND status_id = 3
                  AND jute_mr_date IS NOT NULL
                  AND jute_mr_date BETWEEN :fy_start AND :fy_end
                  AND jute_mr_id <> :id
            """), {"party": str(hdr[0]), "fy_start": fy_start.strftime("%Y-%m-%d"),
                   "fy_end": fy_end.strftime("%Y-%m-%d"), "id": mr_id}).scalar() or 0)
        tds_amount = _erp_tds_amount(cumulative, raw_total)
    tds_amount = round(float(tds_amount), 2)
    roundoff, net_total = _erp_jute_totals(total_amount, claim_amount, tds_amount)
    return {"total_amount": total_amount, "claim_amount": claim_amount,
            "tds_amount": tds_amount, "roundoff": roundoff, "net_total": net_total}


def _erp_recompute_money(conn, mr_id: int, tds_amount: Optional[float] = None) -> dict:
    """recompute_mr_money on the caller's connection: active lines' total_price
    = ROUND(accepted / 100 x rate, 2), then the header from _erp_money. Call
    it after party / date / status are written (TDS depends on them). A
    Pending hand-off carries tds_amount=0."""
    line_ids = select_ids(conn, """
        SELECT jute_mr_li_id FROM jute_mr_li
        WHERE jute_mr_id = :id AND (active = 1 OR active IS NULL)
    """, {"id": mr_id})
    if line_ids:
        conn.execute(text(f"""
            UPDATE jute_mr_li
            SET total_price = ROUND((COALESCE(accepted_weight, actual_weight, 0) / 100)
                                    * COALESCE(rate, 0), 2)
            WHERE jute_mr_li_id IN ({','.join(str(i) for i in line_ids)})
        """))
    money = _erp_money(conn, mr_id, tds_amount)
    conn.execute(text("""
        UPDATE jute_mr
        SET total_amount = :total_amount, claim_amount = :claim_amount,
            tds_amount = :tds_amount, roundoff = :roundoff, net_total = :net_total
        WHERE jute_mr_id = :id
    """), {**money, "id": mr_id})
    return money


def _update_original_mr(conn, jute_mr_id: int, rate_multiplier: float,
                         final_party_id: int, final_party_branch_id: Optional[int],
                         source_mr: dict, branch_id: int,
                         mr_date: date, updated_by: int,
                         target_total: Optional[float] = None,
                         rate_source_line_items: Optional[list] = None,
                         use_new_rounding: bool = False,
                         challan_no: Optional[str] = None,
                         challan_date: Optional[date] = None,
                         seller_invoice: Optional[dict] = None) -> None:
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
        seller_invoice: Dict from _create_sales_invoice (the last-hop invoice
            for the final step). When provided, invoice_no/invoice_date/
            invoice_amount on the root MR are set from these values
            (via the same conditional SQL machinery as challan_no/challan_date).
            When None, those columns are left untouched.
    """
    # Ensure the final party has correct party types before updating MR
    _ensure_party_types(conn, final_party_id, updated_by)

    # Assign branch_mr_no and bill_pass_no
    new_mr_no = _get_next_mr_number_in_txn(conn, branch_id, mr_date)
    new_bill_pass_no = _get_next_bill_pass_no_in_txn(conn, branch_id, mr_date)

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
            # Round at kg level (2 decimals), then convert back to quintal --
            # in Decimal, exactly as the screen does (hop_rate).
            new_rate, _ = hop_rate(base_rate, rate_multiplier)
            new_total_price = _calculate_line_item_amount(accepted_weight, new_rate)
        else:
            new_rate = float((Decimal(str(base_rate)) * Decimal(str(rate_multiplier))).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
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
    # challan_no / challan_date / invoice_no / invoice_date / invoice_amount are
    # only written when the caller passes them — this keeps the "mukam_id
    # untouched" and "leave columns alone when None" invariants explicit.
    # mukam_id is never in this UPDATE.
    optional_assignments = []
    optional_params: dict = {}
    if challan_no is not None:
        optional_assignments.append("challan_no = :challan_no")
        optional_params["challan_no"] = challan_no
    if challan_date is not None:
        optional_assignments.append("challan_date = :challan_date")
        optional_params["challan_date"] = challan_date
    if seller_invoice is not None:
        optional_assignments.append("invoice_no = :invoice_no")
        optional_params["invoice_no"] = seller_invoice["invoice_no_formatted"]
        optional_assignments.append("invoice_date = :invoice_date")
        optional_params["invoice_date"] = seller_invoice["invoice_date"]
        optional_assignments.append("invoice_amount = :invoice_amount")
        optional_params["invoice_amount"] = seller_invoice["invoice_amount"]

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
        "status_id": 3,
        "updated_by": updated_by,
        "mr_id": jute_mr_id,
    })
    # Money exactly as the ERP's approve would write it (line totals, claim
    # from the lines, 194Q TDS for the new party, roundoff, net). It used to
    # leave claim and net NULL, so the P&L read the purchase as 0.
    _erp_recompute_money(conn, jute_mr_id)


def _derive_original_party(conn, root_mr_id: int, step1_source_mr: dict):
    """(party_id, party_branch_id) the root most likely had before finalize,
    for a chain finalized without a remembering Final PO; None if unknown.

    Step 1 copied the root's party into the first forwarding company BY NAME
    (_ensure_supplier_party), so the same name in the root's own company
    leads back to it. Same-name duplicates: the root's PO party, else the
    one mapped to the root's jute supplier, else the lowest id."""
    hop_party = str(step1_source_mr.get("party_id") or "").strip()
    if not hop_party.isdigit():
        return None
    name_row = conn.execute(text(
        "SELECT supp_name FROM party_mst WHERE party_id = :pid"
    ), {"pid": int(hop_party)}).fetchone()
    if not name_row or not name_row[0]:
        return None
    root = conn.execute(text("""
        SELECT r.party_id, r.party_branch_id, r.po_id, r.jute_supplier_id, bm.co_id
        FROM jute_mr r JOIN branch_mst bm ON bm.branch_id = r.branch_id
        WHERE r.jute_mr_id = :id
    """), {"id": root_mr_id}).fetchone()
    if not root:
        return None
    root = root._mapping
    candidates = [int(r[0]) for r in conn.execute(text("""
        SELECT party_id FROM party_mst
        WHERE co_id = :co AND LOWER(TRIM(supp_name)) = LOWER(TRIM(:name))
        ORDER BY party_id
    """), {"co": root["co_id"], "name": name_row[0]}).fetchall()]
    if not candidates:
        return None
    chosen = candidates[0]
    if len(candidates) > 1:
        po_party = conn.execute(text(
            "SELECT party_id FROM jute_po WHERE jute_po_id = :id"
        ), {"id": root["po_id"] or 0}).scalar()
        mapped = [int(r[0]) for r in conn.execute(text("""
            SELECT party_id FROM jute_supp_party_map
            WHERE co_id = :co AND jute_supplier_id = :sid ORDER BY map_id
        """), {"co": root["co_id"], "sid": root["jute_supplier_id"] or 0}).fetchall()
            if r[0] is not None]
        if po_party in candidates:
            chosen = int(po_party)
        else:
            chosen = next((m for m in mapped if m in candidates), candidates[0])
    if str(chosen) == str(root["party_id"]).strip():
        return str(chosen), root["party_branch_id"]
    branch = conn.execute(text("""
        SELECT party_mst_branch_id FROM party_branch_mst
        WHERE party_id = :pid ORDER BY party_mst_branch_id LIMIT 1
    """), {"pid": chosen}).scalar()
    return str(chosen), branch


def _original_header(conn, root_mr_id: int, step1_source_mr: dict) -> dict:
    """Header values to put back on un-finalize, by column (a column left out
    is left as it is).

    Exact when the chain's Final PO remembered them at finalize
    (po_ops.final_po_original). Otherwise (finalized before transfer POs, or
    with them switched off): the party derived from step 1, and jute_mr_date
    back to NULL -- the state of a Pending (13) hand-off MR in the ERP."""
    remembered = po_ops.final_po_original(conn, root_mr_id)
    if remembered is not None:
        return dict(remembered)
    out = {"jute_mr_date": None}
    derived = _derive_original_party(conn, root_mr_id, step1_source_mr or {})
    if derived:
        out["party_id"], out["party_branch_id"] = derived
    return out


def revert_original_mr(conn, jute_mr_id: int, step1_source_mr: dict, updated_by: int) -> list:
    """Revert the original MR to its pre-finalization state.

    Restores line item rates from Step 1 (the first transferred MR's snapshot,
    which is unaffected by finalization), clears branch_mr_no/bill_pass_*,
    clears invoice_no/invoice_date/invoice_amount (which finalization wrote
    from the last seller's sales_invoice), sets status_id back to Pending (13,
    decision D3 2026-09-03 — never 0, never 1: 13 is the ERP hand-off state).
    Also puts back the header fields finalize overwrote -- party_id,
    party_branch_id, jute_mr_date -- see _original_header. (They used to stay
    on the last seller, and a chain restarted afterwards copied the sister
    company as the jute supplier.)

    Args:
        conn: Active SQLAlchemy connection inside a transaction.
        jute_mr_id: The root MR id (the in-place finalized MR being reverted).
        step1_source_mr: Full dict of Step 1's MR (the first transferred MR
            in the chain), including a "line_items" list. Used as the source
            of pre-finalization rates. Line items are matched positionally to
            the root MR's line items (1:1 — both were cloned from the same
            source).
        updated_by: User id for audit columns.

    Returns the Final PO(s) removed with the revert (po_ops.delete_final_po).
    """
    # What finalize overwrote on the header, read before its Final PO goes.
    original = _original_header(conn, jute_mr_id, step1_source_mr)

    # The Final PO written at finalize goes first: if it cannot be removed
    # (an ERP user booked another MR against it) nothing else is touched.
    deleted_final_pos = po_ops.delete_final_po(conn, jute_mr_id)

    # Load root MR's line items in stable order for positional matching
    root_lis = conn.execute(
        text("SELECT jute_mr_li_id, accepted_weight FROM jute_mr_li "
             "WHERE jute_mr_id = :id AND (active = 1 OR active IS NULL) ORDER BY jute_mr_li_id"),
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
        # In paise, like finalize and the ERP's own MR save
        # (ROUND(accepted/100 * rate, 2), vowerp3be mr.py) -- whole rupees
        # here used to change the restored line by up to 50 paise.
        new_total = _calculate_line_item_amount(accepted_weight, rate)

        conn.execute(text("""
            UPDATE jute_mr_li SET
                rate = :rate, total_price = :total_price, updated_date_time = NOW()
            WHERE jute_mr_li_id = :li_id
        """), {"rate": rate, "total_price": new_total, "li_id": li_id})

    # Restore header: clear branch_mr_no/bill_pass_*, clear invoice_no/date/amount
    # (which finalization wrote from the last hop's sales_invoice), status back
    # to Pending, recompute totals, put back party / party branch / MR date.
    # mukam_id is never touched. Also restore challan_no / challan_date from Step 1's MR: Step 1 preserved
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
    for col in ("party_id", "party_branch_id", "jute_mr_date"):
        if col in original:
            revert_assignments.append(f"{col} = :orig_{col}")
            revert_params[f"orig_{col}"] = original[col]

    revert_sql = (", " + ", ".join(revert_assignments)) if revert_assignments else ""

    conn.execute(text(f"""
        UPDATE jute_mr SET
            branch_mr_no = NULL,
            bill_pass_no = NULL,
            bill_pass_date = NULL,
            invoice_no = NULL,
            invoice_date = NULL,
            invoice_amount = NULL,
            status_id = 13,
            updated_by = :updated_by,
            updated_date_time = NOW()
            {revert_sql}
        WHERE jute_mr_id = :mr_id
    """), revert_params)
    # Money as the ERP's own Pending hand-off writes it: recomputed from the
    # restored lines, no TDS before approval.
    _erp_recompute_money(conn, jute_mr_id, tds_amount=0.0)
    return deleted_final_pos


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
        dict with keys: mr_id (int or None), invoice_id (int or None),
        po (the transfer PO written with this step — see po_ops — or None)
    """
    mr_id = None
    invoice_id = None
    po = None
    # Exact Decimal multiplier (1 + pct/100) when the step carries the % the
    # user typed; the float rate_multiplier otherwise. Posting then uses the
    # same arithmetic as the screen (see jute_mr_chain_helpers.hop_rate).
    rate_multiplier = step_multiplier(step.pct_rate_increase, rate_multiplier)

    with DatabaseConnection.get_transaction() as conn:
        # Lock the root first: serialises every save / delete on this chain,
        # so a stale second tab cannot fork it or finalize it twice.
        root_row = conn.execute(
            text("SELECT status_id, branch_mr_no FROM jute_mr WHERE jute_mr_id = :id FOR UPDATE"),
            {"id": root_mr_id},
        ).fetchone()
        if not root_row:
            raise ValueError(f"Root MR {root_mr_id} not found")
        root_status, root_branch_mr_no = root_row[0], root_row[1]

        source_mr = get_source_mr_full(source_mr_id, conn=conn)
        if not source_mr:
            raise ValueError(f"Source MR {source_mr_id} not found")

        # Decision D1 (2026-09-03): a NEW chain may only start from an ERP
        # root at status 13 (Pending). Backend enforcement behind the UI
        # guard in pages/new_transfer_chain.py — the UI can be bypassed on
        # a rerun, this cannot.
        if is_first_step:
            if not is_root_eligible_for_new_chain(root_status):
                raise ValueError(
                    f"Root MR {root_mr_id} is not Pending (13) in the ERP; "
                    f"cannot start a new transfer chain (status={root_status})"
                )
            if conn.execute(
                text("SELECT 1 FROM jute_mr WHERE src_jute_mr_id = :root "
                     "AND transfer_mode = 0 LIMIT 1"),
                {"root": root_mr_id},
            ).fetchone():
                raise ValueError(
                    f"Root MR {root_mr_id} already has a transfer chain "
                    "(saved from another session?); refresh the page"
                )
        else:
            # A later step must continue the chain as it stands NOW: a stale
            # tab or a double save would otherwise add a hop after the
            # finalize, or a second hop to the same seller.
            if root_branch_mr_no is not None:
                raise ValueError(
                    f"Root MR {root_mr_id} is already finalized; refresh the page"
                )
            newest = conn.execute(
                text("SELECT MAX(jute_mr_id) FROM jute_mr WHERE src_jute_mr_id = :root "
                     "AND transfer_mode = 0"),
                {"root": root_mr_id},
            ).scalar()
            if newest is None or int(newest) != int(source_mr_id):
                raise ValueError(
                    f"The transfer chain of MR {root_mr_id} has changed since this page "
                    "was loaded (saved from another session?); refresh the page"
                )

        # Assign MR number inside transaction
        step.mr_no = _get_next_mr_number_in_txn(conn, step.branch_id, step.mr_date)
        step.gate_entry_no = _get_next_gate_entry_no(conn, step.branch_id, step.mr_date)

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
            # The forwarding company's PO on the original supplier.
            po = po_ops.create_forward_po(conn, mr_id, updated_by)
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
            # mode-1 marked children share src_jute_mr_id; never chain rows
            prev_mr_result = conn.execute(
                text("""SELECT jute_mr_id FROM jute_mr
                        WHERE src_jute_mr_id = :root AND branch_id = :bid
                        AND transfer_mode = 0
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

                last_seller_party_id, last_seller_party_branch_id = _ensure_company_as_party(
                    conn, prev_co_id, prev_branch_id, source_co_id, updated_by
                )
                ensured_branch_id = _ensure_party_branch_from_source_branch(
                    conn, last_seller_party_id, prev_branch_id, updated_by
                )
                if ensured_branch_id is not None:
                    last_seller_party_branch_id = ensured_branch_id
                # What finalize is about to overwrite on the root's header; the
                # Final PO remembers it so un-finalize can put it back.
                original_header = {
                    "party_id": root_mr.get("party_id"),
                    "party_branch_id": root_mr.get("party_branch_id"),
                    "jute_mr_date": root_mr.get("jute_mr_date"),
                }
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
                    seller_invoice=seller_invoice,
                )
                # The origin's final PO on the last forwarding company, from
                # the root's lines as just re-priced. The root MR itself keeps
                # its original ERP PO (owner decision 2026-10-01).
                po = po_ops.create_final_po(conn, root_mr_id_for_update, updated_by,
                                            original=original_header)
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
                # The buyer's PO on the selling company.
                po = po_ops.create_forward_po(conn, mr_id, updated_by)

    return {"mr_id": mr_id, "invoice_id": invoice_id, "po": po}


def _invoice_ids_for_mr(conn, mr_id: int) -> list:
    """Raw-Jute invoices hanging off an MR through sales_invoice_jute.mr_id."""
    rows = conn.execute(
        text("SELECT invoice_id FROM sales_invoice_jute WHERE mr_id = :mr_id"),
        {"mr_id": mr_id},
    ).fetchall()
    return [r[0] for r in rows]


def _delete_invoice(conn, invoice_id: int) -> None:
    """Hard-delete one chain invoice from all four of its tables (the claim
    breakdown in sales_invoice_jute_dtl has no FK and used to be left behind),
    each by primary key -- see database.delete_by_ids."""
    dtl_ids = select_ids(conn, """
        SELECT invoice_line_item_id FROM sales_invoice_dtl WHERE invoice_id = :id
    """, {"id": invoice_id})
    if dtl_ids:
        delete_by_ids(conn, "sales_invoice_jute_dtl", "sales_invoice_jute_dtl_id", select_ids(
            conn,
            "SELECT sales_invoice_jute_dtl_id FROM sales_invoice_jute_dtl "
            f"WHERE invoice_line_item_id IN ({','.join(str(i) for i in dtl_ids)})",
        ))
    delete_by_ids(conn, "sales_invoice_jute", "sales_invoice_jute_id", select_ids(
        conn, "SELECT sales_invoice_jute_id FROM sales_invoice_jute WHERE invoice_id = :id",
        {"id": invoice_id}))
    delete_by_ids(conn, "sales_invoice_dtl", "invoice_line_item_id", dtl_ids)
    conn.execute(text("DELETE FROM sales_invoice WHERE invoice_id = :id"), {"id": invoice_id})


def _assert_step_deletable(conn, jute_mr_id: int) -> None:
    """Refuse when this step already has ERP issue entries drawn against it
    (consumption started) -- deleting it would orphan the issues."""
    issued = conn.execute(text(
        "SELECT 1 FROM jute_issue ji JOIN jute_mr_li li ON li.jute_mr_li_id = ji.jute_mr_li_id "
        "WHERE li.jute_mr_id = :id AND COALESCE(ji.status_id, 0) <> 4 LIMIT 1"), {"id": jute_mr_id}).fetchone()
    if issued:
        raise ValueError("This transfer step has ERP issue entries (consumption started); cannot delete")


def _delete_transfer_step_in_txn(conn, jute_mr_id: int, updated_by: int) -> Optional[dict]:
    """Delete one chain hop on the caller's connection: the invoices booked
    for it, its transfer PO, its lines and its header.

    Invoices created for a step are linked to the PREVIOUS step's MR (the
    seller), so this deletes:
    1. Invoices directly linked to this MR (the sale onward / the finalize
       invoice when this is the last seller)
    2. Invoices linked to the previous MR (the sale that created this step)

    Returns the Forwarding PO removed with the step ({'po_id',
    'po_no_formatted'}) or None.
    """
    mr_info = conn.execute(
        text("SELECT src_jute_mr_id, transfer_mode FROM jute_mr WHERE jute_mr_id = :id"),
        {"id": jute_mr_id},
    ).fetchone()

    if not mr_info:
        return None

    root_mr_id, transfer_mode = mr_info
    if root_mr_id is None or int(transfer_mode or 0) != 0:
        # Never hard-delete a gate-entry (ERP) MR or a marked child from here.
        raise ValueError(f"MR {jute_mr_id} is not a vertical-chain step; cannot delete")

    _assert_step_deletable(conn, jute_mr_id)

    # Previous MR in the chain. Hops are inserted in chain order, so it is the
    # highest id below this one (a branch comparison picks the wrong MR when
    # the same branch appears twice in a chain).
    # mode-1 marked children share src_jute_mr_id; never chain rows
    prev_mr_row = conn.execute(
        text("""SELECT jute_mr_id FROM jute_mr
                WHERE src_jute_mr_id = :root AND jute_mr_id < :id
                AND transfer_mode = 0
                ORDER BY jute_mr_id DESC LIMIT 1"""),
        {"root": root_mr_id, "id": jute_mr_id},
    ).fetchone()
    prev_mr_id = prev_mr_row[0] if prev_mr_row else None

    invoice_ids_to_delete = set(_invoice_ids_for_mr(conn, jute_mr_id))
    if prev_mr_id:
        invoice_ids_to_delete.update(_invoice_ids_for_mr(conn, prev_mr_id))
    for inv_id in sorted(invoice_ids_to_delete):
        _delete_invoice(conn, inv_id)

    # The step's Forwarding PO goes with it (raises if an ERP user has since
    # booked another MR against that PO).
    deleted_po = po_ops.delete_forward_po(conn, jute_mr_id)

    # Delete MR line items and MR (lines by primary key: no gap locks on the
    # jute_mr_id index, where ERP gate entries insert new lines)
    delete_by_ids(conn, "jute_mr_li", "jute_mr_li_id", select_ids(
        conn, "SELECT jute_mr_li_id FROM jute_mr_li WHERE jute_mr_id = :id", {"id": jute_mr_id}))
    conn.execute(text("DELETE FROM jute_mr WHERE jute_mr_id = :id"), {"id": jute_mr_id})
    return deleted_po


def delete_transfer_step(jute_mr_id: int, updated_by: int) -> Optional[dict]:
    """Delete a transfer MR with its invoices and transfer PO, in one
    transaction with its root locked first (like every other chain write).
    See _delete_transfer_step_in_txn."""
    with DatabaseConnection.get_transaction() as conn:
        root = conn.execute(
            text("SELECT src_jute_mr_id FROM jute_mr WHERE jute_mr_id = :id"),
            {"id": jute_mr_id},
        ).scalar()
        if root is not None:
            conn.execute(
                text("SELECT jute_mr_id FROM jute_mr WHERE jute_mr_id = :id FOR UPDATE"),
                {"id": int(root)},
            )
        return _delete_transfer_step_in_txn(conn, jute_mr_id, updated_by)


def _chain_rows(conn, root_mr_id: int) -> list:
    """Chain hop MRs of a root, on the caller's connection (the columns
    _reconstruct_chain needs; same filter as queries.get_transfer_chain)."""
    rows = conn.execute(text("""
        SELECT mr.jute_mr_id, mr.src_com_id, mr.branch_id, bm.co_id AS owner_co_id
        FROM jute_mr mr
        JOIN branch_mst bm ON mr.branch_id = bm.branch_id
        WHERE mr.src_jute_mr_id = :root_id
        AND mr.transfer_mode = 0
        ORDER BY mr.jute_mr_id ASC
    """), {"root_id": root_mr_id}).fetchall()
    return [dict(r._mapping) for r in rows]


def delete_chain_from_step(root_mr_id: int, from_mr_id: int, updated_by: int) -> dict:
    """Delete chain steps from a given point onward, OR revert finalization.

    Special case: if from_mr_id == root_mr_id, the caller is "unfinalizing"
    the in-place return-to-origin step. We do not delete any chain rows;
    we only call revert_original_mr to restore the root MR to its
    pre-finalization state, using Step 1's line items as the rate snapshot.

    Otherwise: cascade-delete transfer rows from from_mr_id onward (in
    reverse chain order), and if the chain was finalized, additionally
    revert the root MR.

    Everything -- reads, guards, deletes, the root update -- runs in ONE
    transaction with the root row locked, so a failure on any step leaves
    the chain exactly as it was.

    Returns what was removed: {'deleted_mr_ids': [...], 'deleted_pos':
    [printed PO numbers], 'reverted': bool} (all empty/False on a no-op).
    """
    summary = {"deleted_mr_ids": [], "deleted_pos": [], "reverted": False}
    with DatabaseConnection.get_transaction() as conn:
        # Lock the root first: serialises this against any concurrent save
        # or delete on the same chain.
        locked = conn.execute(
            text("SELECT jute_mr_id FROM jute_mr WHERE jute_mr_id = :id FOR UPDATE"),
            {"id": root_mr_id},
        ).fetchone()
        if not locked:
            return summary

        root_mr = get_source_mr_full(root_mr_id, conn=conn)
        if not root_mr:
            return summary

        chain_mrs = _chain_rows(conn, root_mr_id)
        if not chain_mrs:
            return summary

        # Derive root co_id
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
            step1_source_mr = get_source_mr_full(step1_id, conn=conn)

        # Branch A: root-return revert only (unfinalize the synthetic final step)
        if from_mr_id == root_mr_id:
            if not was_complete:
                return summary
            if step1_source_mr is None:
                return summary
            # The finalization invoice was created with sales_invoice_jute.mr_id =
            # the last seller's MR (last entry of the reconstructed chain).
            last_seller_mr_id = ordered[-1]["jute_mr_id"]
            for inv_id in _invoice_ids_for_mr(conn, last_seller_mr_id):
                _delete_invoice(conn, inv_id)
            finals = revert_original_mr(conn, root_mr_id, step1_source_mr, updated_by)
            summary["deleted_pos"] = [p["po_no_formatted"] for p in finals]
            summary["reverted"] = True
            return summary

        # Branch B: middle-step cascade delete
        from_idx = next((i for i, m in enumerate(ordered) if m["jute_mr_id"] == from_mr_id), None)
        if from_idx is None:
            return summary

        to_delete = ordered[from_idx:]
        # Every guard for every step before the first DELETE.
        for mr in to_delete:
            _assert_step_deletable(conn, mr["jute_mr_id"])
        for mr in reversed(to_delete):
            deleted_po = _delete_transfer_step_in_txn(conn, mr["jute_mr_id"], updated_by)
            summary["deleted_mr_ids"].append(mr["jute_mr_id"])
            if deleted_po:
                summary["deleted_pos"].append(deleted_po["po_no_formatted"])

        if was_complete and step1_source_mr is not None:
            # The chain was finalized: whichever step the delete started from,
            # the root must leave its finalized state (rates, MR / bill-pass
            # numbers, invoice fields, final PO) and return to Pending (13).
            finals = revert_original_mr(conn, root_mr_id, step1_source_mr, updated_by)
            summary["deleted_pos"].extend(p["po_no_formatted"] for p in finals)
            summary["reverted"] = True
        elif from_idx == 0:
            # Whole chain rolled back from Step 1: root returns to Pending (13) —
            # the ERP hand-off state, never Open (decision D3 2026-09-03).
            conn.execute(
                text("UPDATE jute_mr SET status_id = 13, updated_by = :uid, "
                     "updated_date_time = NOW() WHERE jute_mr_id = :id"),
                {"uid": updated_by, "id": root_mr_id},
            )
    return summary


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

    return {"mr_ids": mr_ids, "invoice_ids": invoice_ids}
