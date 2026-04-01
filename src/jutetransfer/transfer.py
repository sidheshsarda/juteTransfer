"""Transfer chain finalization logic for JuteTransfer.

Handles the complete finalization flow when a transfer chain returns to its
source company: masters checks, MR creation for intermediate companies,
sales invoice generation, and updating the original MR.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional
import logging

from sqlalchemy import text

from .database import DatabaseConnection
from .queries import get_source_mr_full, _get_financial_year_bounds

logger = logging.getLogger(__name__)

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
    warehouse_id: Optional[int] = None
    gate_entry_no: Optional[int] = None


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
        "party_type_id": party_dict.get("party_type_id"),
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
        branch_id = _get_party_branch_id(conn, existing)
        return existing, branch_id

    # Create party from company master
    supp_code = _generate_supp_code(conn, in_co_id)
    new_party_id = DatabaseConnection.execute_insert_returning_id(conn, """
        INSERT INTO party_mst (supp_name, prefix, active, co_id,
            supp_email_id, party_pan_no, cin, country_id,
            supp_code, updated_by, updated_date_time)
        VALUES (:name, :prefix, 1, :co_id,
            :email, :pan, :cin, :country_id,
            :supp_code, :updated_by, NOW())
    """, {
        "name": co["co_name"],
        "prefix": co.get("co_prefix"),
        "co_id": in_co_id,
        "email": co.get("co_email_id"),
        "pan": co.get("co_pan_no"),
        "cin": co.get("co_cin_no"),
        "country_id": co.get("country_id"),
        "supp_code": supp_code,
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


# ---------------------------------------------------------------------------
# MR creation
# ---------------------------------------------------------------------------

def _create_mr(conn, source_mr: dict, step: TransferStep,
               party_id: int, party_branch_id: Optional[int],
               updated_by: int, rate_multiplier: float,
               prev_co_id: int, root_mr_id: int) -> int:
    """Create a new jute_mr + jute_mr_li records for a transfer step.

    Copies most fields from the source MR, overriding company/branch/party/rate.

    Returns the new jute_mr_id.
    """
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
            src_jute_mr_id
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
            :src_jute_mr_id
        )
    """, {
        "gate_entry_no": _get_next_gate_entry_no(conn, step.branch_id),
        "branch_mr_no": step.mr_no,
        "gate_entry_date": step.mr_date,
        "mr_date": step.mr_date,
        "challan_date": source_mr.get("challan_date"),
        "challan_no": source_mr.get("challan_no"),
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
        "vehicle_no": source_mr.get("vehicle_no"),
        "marketing_slip": source_mr.get("marketing_slip"),
        "transporter": source_mr.get("transporter"),
        "driver_name": source_mr.get("driver_name"),
        "frieght_paid": source_mr.get("frieght_paid"),
        "updated_by": updated_by,
        "po_id": None,  # PO is company-specific
        "branch_id": step.branch_id,
        "party_id": str(party_id),
        "party_branch_id": party_branch_id,
        "jute_supplier_id": source_mr.get("jute_supplier_id"),
        "src_com_id": prev_co_id,  # received-from company
        "total_amount": step.total_amount,
        "claim_amount": step.claim_amount,
        "roundoff": step.roundoff,
        "net_total": step.net_amount,
        "tds_amount": source_mr.get("tds_amount"),
        "src_jute_mr_id": root_mr_id,  # always root
    })

    # Copy line items with per-item rate via rate_multiplier
    for li in source_mr.get("line_items", []):
        accepted_weight = round(float(li.get("accepted_weight") or 0), 0)
        original_rate = float(li.get("rate") or 0)
        new_rate = original_rate * rate_multiplier
        total_price = round(accepted_weight * new_rate / 100, 2)

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
            "actual_item_id": target_actual_item_id,
            "actual_quality": li.get("actual_quality"),
            "actual_qty": li.get("actual_qty"),
            "actual_weight": li.get("actual_weight"),
            "challan_item_id": target_challan_item_id,
            "challan_quality_id": li.get("challan_quality_id"),
            "challan_quantity": li.get("challan_quantity"),
            "challan_weight": li.get("challan_weight"),
            "allowable_moisture": li.get("allowable_moisture"),
            "actual_moisture": li.get("actual_moisture"),
            "claim_dust": li.get("claim_dust"),
            "shortage_kgs": li.get("shortage_kgs"),
            "accepted_weight": accepted_weight,
            "rate": new_rate,
            "claim_rate": li.get("claim_rate", 0),
            "water_damage_amount": li.get("water_damage_amount", 0),
            "premium_amount": li.get("premium_amount", 0),
            "total_price": total_price,
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


def _create_sales_invoice(conn, seller_step: TransferStep,
                           buyer_party_id: int, buyer_party_branch_id: Optional[int],
                           mr_id: int, source_mr: dict,
                           updated_by: int, rate_multiplier: float) -> int:
    """Create a sales invoice from the seller to the buyer.

    Inserts into sales_invoice, sales_invoice_dtl, and sales_invoice_jute.

    Returns the new invoice_id.
    """
    invoice_no = _get_next_invoice_no(conn, seller_step.branch_id)

    invoice_id = DatabaseConnection.execute_insert_returning_id(conn, """
        INSERT INTO sales_invoice (
            invoice_no, invoice_date, invoice_type, invoice_amount,
            party_id, billing_to_id, shipping_to_id, branch_id,
            active, status_id, round_off, updated_by, updated_date_time
        ) VALUES (
            :invoice_no, :invoice_date, :invoice_type, :invoice_amount,
            :party_id, :billing_to_id, :shipping_to_id, :branch_id,
            1, 3, 0, :updated_by, NOW()
        )
    """, {
        "invoice_no": invoice_no,
        "invoice_date": seller_step.mr_date,
        "invoice_type": RAW_JUTE_INVOICE_TYPE,
        "invoice_amount": seller_step.total_amount,
        "party_id": buyer_party_id,
        "billing_to_id": buyer_party_branch_id,
        "shipping_to_id": buyer_party_branch_id,
        "branch_id": seller_step.branch_id,
        "updated_by": updated_by,
    })

    # Line items from source MR with per-item rates
    for li in source_mr.get("line_items", []):
        accepted_weight = round(float(li.get("accepted_weight") or 0), 0)
        original_rate = float(li.get("rate") or 0)
        new_rate = original_rate * rate_multiplier
        amount = round(accepted_weight * new_rate / 100, 2)

        # Map item to seller's company
        target_item_id = li.get("actual_item_id")
        if target_item_id:
            target_item_id = _ensure_item(
                conn, int(target_item_id), seller_step.co_id, updated_by
            )

        conn.execute(text("""
            INSERT INTO sales_invoice_dtl (
                invoice_id, item_id, hsn_code, quantity, sales_weight,
                uom_id, rate, amount_without_tax, total_amount
            ) VALUES (
                :invoice_id, :item_id, :hsn_code, :quantity, :weight,
                :uom_id, :rate, :amount, :amount
            )
        """), {
            "invoice_id": invoice_id,
            "item_id": target_item_id,
            "hsn_code": None,
            "quantity": li.get("actual_qty"),
            "weight": accepted_weight,
            "uom_id": li.get("uom_id"),
            "rate": new_rate,
            "amount": amount,
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

    return invoice_id


# ---------------------------------------------------------------------------
# Update original MR
# ---------------------------------------------------------------------------

def _update_original_mr(conn, jute_mr_id: int, rate_multiplier: float,
                         final_party_id: int, final_party_branch_id: Optional[int],
                         source_mr: dict, branch_id: int,
                         mr_date: date, updated_by: int) -> None:
    """Update the original MR with final rate/party and assign branch_mr_no."""
    # Assign branch_mr_no
    new_mr_no = _get_next_mr_number_in_txn(conn, branch_id)

    # Update each line item with its computed absolute rate
    for li in source_mr.get("line_items", []):
        li_id = li["jute_mr_li_id"]
        original_rate = float(li.get("rate") or 0)
        new_rate = original_rate * rate_multiplier
        accepted_weight = round(float(li.get("accepted_weight") or 0), 0)
        new_total_price = round(accepted_weight * new_rate / 100, 2)

        conn.execute(text("""
            UPDATE jute_mr_li SET
                rate = :rate,
                total_price = :total_price,
                updated_date_time = NOW()
            WHERE jute_mr_li_id = :li_id
        """), {"rate": new_rate, "total_price": new_total_price, "li_id": li_id})

    # Recompute header totals from line items
    conn.execute(text("""
        UPDATE jute_mr SET
            party_id = :party_id,
            party_branch_id = :party_branch_id,
            branch_mr_no = :mr_no,
            jute_mr_date = :mr_date,
            total_amount = (SELECT ROUND(COALESCE(SUM(total_price), 0), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id),
            roundoff = (SELECT COALESCE(SUM(total_price), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id) -
                       (SELECT ROUND(COALESCE(SUM(total_price), 0), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id),
            net_total = (SELECT ROUND(COALESCE(SUM(total_price), 0), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id) - claim_amount,
            updated_by = :updated_by,
            updated_date_time = NOW()
        WHERE jute_mr_id = :mr_id
    """), {
        "party_id": str(final_party_id),
        "party_branch_id": final_party_branch_id,
        "mr_no": new_mr_no,
        "mr_date": mr_date,
        "updated_by": updated_by,
        "mr_id": jute_mr_id,
    })


def revert_original_mr(conn, jute_mr_id: int, source_mr: dict, updated_by: int) -> None:
    """Revert the original MR to its pre-finalization state.

    Restores original party, rates, branch_mr_no=NULL, and line item rates.
    Called when deleting a completed chain's final step.
    """
    # Restore each line item to original rate
    for li in source_mr.get("line_items", []):
        li_id = li["jute_mr_li_id"]
        original_rate = float(li.get("rate") or 0)
        accepted_weight = round(float(li.get("accepted_weight") or 0), 0)
        original_total = round(accepted_weight * original_rate / 100, 2)

        conn.execute(text("""
            UPDATE jute_mr_li SET
                rate = :rate, total_price = :total_price, updated_date_time = NOW()
            WHERE jute_mr_li_id = :li_id
        """), {"rate": original_rate, "total_price": original_total, "li_id": li_id})

    # Restore header: original party, NULL branch_mr_no, recompute totals
    conn.execute(text("""
        UPDATE jute_mr SET
            party_id = :party_id,
            party_branch_id = :party_branch_id,
            branch_mr_no = NULL,
            total_amount = (SELECT ROUND(COALESCE(SUM(total_price), 0), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id),
            roundoff = (SELECT COALESCE(SUM(total_price), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id) -
                       (SELECT ROUND(COALESCE(SUM(total_price), 0), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id),
            net_total = (SELECT ROUND(COALESCE(SUM(total_price), 0), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id) - claim_amount,
            updated_by = :updated_by,
            updated_date_time = NOW()
        WHERE jute_mr_id = :mr_id
    """), {
        "party_id": str(source_mr.get("party_id", "")),
        "party_branch_id": source_mr.get("party_branch_id"),
        "updated_by": updated_by,
        "mr_id": jute_mr_id,
    })


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
) -> dict:
    """Save a single transfer step: create MR + invoice.

    Args:
        source_mr_id: Root MR ID (for fetching source data)
        step: The transfer step being saved
        prev_co_id: Company from which this step receives
        prev_branch_id: Branch of the previous step (for invoice)
        source_co_id: Original source company co_id
        source_branch_id: Original source branch_id
        root_mr_id: Root MR ID (for src_jute_mr_id)
        updated_by: User ID
        rate_multiplier: Cumulative rate multiplier for this step
        is_first_step: True if this is step[0] (supplier party, no invoice from prev)
        is_final: True if chain returns to source

    Returns:
        dict with keys: mr_id (int or None), invoice_id (int or None)
    """
    mr_id = None
    invoice_id = None

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
            mr_id = _create_mr(
                conn, source_mr, step, party_id, party_branch_id,
                updated_by, rate_multiplier, prev_co_id, root_mr_id
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

            invoice_id = _create_sales_invoice(
                conn, prev_step_for_invoice, buyer_party_id,
                buyer_party_branch_id, prev_mr_id, source_mr,
                updated_by, rate_multiplier
            )

            if is_final:
                # Final step: update original MR, don't create new MR
                last_seller_party_id, last_seller_party_branch_id = _ensure_company_as_party(
                    conn, prev_co_id, prev_branch_id, source_co_id, updated_by
                )
                _update_original_mr(
                    conn, source_mr_id, rate_multiplier,
                    last_seller_party_id, last_seller_party_branch_id,
                    source_mr, source_branch_id, step.mr_date, updated_by
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
                    updated_by, rate_multiplier, prev_co_id, root_mr_id
                )

    logger.info(f"Transfer step saved for MR {source_mr_id}: mr_id={mr_id}, invoice_id={invoice_id}")
    return {"mr_id": mr_id, "invoice_id": invoice_id}


def delete_transfer_step(jute_mr_id: int, updated_by: int) -> None:
    """Delete a transfer MR and its associated invoice.

    Finds the invoice via sales_invoice_jute.mr_id linkage,
    then deletes invoice records and the MR + line items.
    """
    with DatabaseConnection.get_transaction() as conn:
        # Find linked invoice(s) via sales_invoice_jute
        inv_rows = conn.execute(
            text("SELECT invoice_id FROM sales_invoice_jute WHERE mr_id = :mr_id"),
            {"mr_id": jute_mr_id},
        ).fetchall()

        for inv_row in inv_rows:
            inv_id = inv_row[0]
            conn.execute(text("DELETE FROM sales_invoice_jute WHERE invoice_id = :id"), {"id": inv_id})
            conn.execute(text("DELETE FROM sales_invoice_dtl WHERE invoice_id = :id"), {"id": inv_id})
            conn.execute(text("DELETE FROM sales_invoice WHERE invoice_id = :id"), {"id": inv_id})

        # Delete MR line items and MR
        conn.execute(text("DELETE FROM jute_mr_li WHERE jute_mr_id = :id"), {"id": jute_mr_id})
        conn.execute(text("DELETE FROM jute_mr WHERE jute_mr_id = :id"), {"id": jute_mr_id})

    logger.info(f"Deleted transfer MR {jute_mr_id} and linked invoices")


def delete_chain_from_step(root_mr_id: int, from_mr_id: int,
                            source_mr: dict, updated_by: int) -> None:
    """Delete all chain steps from a given MR onward (cascade).

    Uses chain reconstruction to find the order, then deletes in reverse.
    If the chain was complete (original MR finalized), reverts the original.
    """
    from .queries import get_transfer_chain
    chain_df = get_transfer_chain(root_mr_id)
    if chain_df is None or chain_df.empty:
        return

    # Reconstruct ordered chain
    from .pages.jute_mr import _reconstruct_chain
    chain_mrs = chain_df.to_dict("records")
    # Derive root co_id
    root_mr = get_source_mr_full(root_mr_id)
    if not root_mr:
        return

    with DatabaseConnection.get_transaction() as conn:
        root_branch = int(root_mr.get("branch_id") or 0)
        root_co_row = conn.execute(
            text("SELECT co_id FROM branch_mst WHERE branch_id = :bid"),
            {"bid": root_branch},
        ).fetchone()
        root_co_id = root_co_row[0] if root_co_row else 0

    ordered = _reconstruct_chain(chain_mrs, root_co_id)

    # Find index of from_mr_id
    from_idx = next((i for i, m in enumerate(ordered) if m["jute_mr_id"] == from_mr_id), None)
    if from_idx is None:
        return

    # Check if chain was complete (original MR has branch_mr_no)
    was_complete = root_mr.get("branch_mr_no") is not None

    # Delete in reverse order from the end back to from_idx
    to_delete = ordered[from_idx:]
    for mr in reversed(to_delete):
        delete_transfer_step(mr["jute_mr_id"], updated_by)

    # Revert original MR if chain was complete
    if was_complete:
        with DatabaseConnection.get_transaction() as conn:
            source_mr_full = get_source_mr_full(root_mr_id, conn=conn)
            revert_original_mr(conn, root_mr_id, source_mr, updated_by)


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
    cumulative_multiplier = 1.0

    for i, step in enumerate(steps):
        if i > 0:
            cumulative_multiplier *= (1 + step.pct_rate_increase / 100)

        prev_co_id = source_co_id if i == 0 else steps[i - 1].co_id
        prev_branch_id = source_branch_id if i == 0 else steps[i - 1].branch_id
        is_final = (i == len(steps) - 1)

        result = save_transfer_step(
            source_mr_id=source_mr_id,
            step=step,
            prev_co_id=prev_co_id,
            prev_branch_id=prev_branch_id,
            source_co_id=source_co_id,
            source_branch_id=source_branch_id,
            root_mr_id=source_mr_id,
            updated_by=updated_by,
            rate_multiplier=cumulative_multiplier,
            is_first_step=(i == 0),
            is_final=is_final,
        )

        if result.get("mr_id"):
            mr_ids.append(result["mr_id"])
        if result.get("invoice_id"):
            invoice_ids.append(result["invoice_id"])

    logger.info(
        f"Transfer chain finalized for MR {source_mr_id}: "
        f"created {len(mr_ids)} MRs, {len(invoice_ids)} invoices"
    )
    return {"mr_ids": mr_ids, "invoice_ids": invoice_ids}
