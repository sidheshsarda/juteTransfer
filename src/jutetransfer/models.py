"""SQLAlchemy ORM models for JuteTransfer database tables.

This module defines database models to help with writing queries and understanding
the database schema. These models map to the existing database tables.
"""

from sqlalchemy import (
    Column, Integer, BigInteger, String, Float, Date, DateTime, Time,
    Boolean, Numeric, Text, ForeignKey
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime

Base = declarative_base()


class Company(Base):
    """Company master table (co_mst)."""
    __tablename__ = 'co_mst'
    
    co_id = Column(Integer, primary_key=True, autoincrement=True)
    co_name = Column(String(255), nullable=False, unique=True)
    co_prefix = Column(String(25), nullable=False, unique=True)
    co_address1 = Column(String(255), nullable=False)
    co_address2 = Column(String(255), nullable=False)
    co_zipcode = Column(Integer, nullable=False)
    country_id = Column(Integer, nullable=False)
    state_id = Column(Integer, nullable=False)
    city_id = Column(Integer)
    co_logo = Column(String(255))
    auto_datetime_insert = Column(DateTime, default=datetime.now)
    created_by_con_user = Column(Integer)
    co_cin_no = Column(String(25))
    co_email_id = Column(String(255))
    co_pan_no = Column(String(25))
    s3bucket_name = Column(String(255))
    s3folder_name = Column(String(255))
    tally_sync = Column(String(255))
    alert_email_id = Column(String(255))
    updated_by = Column(Integer)
    updated_date_time = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class Branch(Base):
    """Branch master table (branch_mst)."""
    __tablename__ = 'branch_mst'
    
    branch_id = Column(Integer, primary_key=True, autoincrement=True)
    branch_name = Column(String(255), nullable=False)
    co_id = Column(Integer, ForeignKey('co_mst.co_id'), nullable=False)
    branch_address1 = Column(String(255))
    branch_address2 = Column(String(255))
    branch_zipcode = Column(Integer)
    country_id = Column(Integer)
    city_id = Column(Integer)
    state_id = Column(Integer)
    gst_no = Column(String(25))
    contact_no = Column(Integer)
    contact_person = Column(String(255))
    branch_email = Column(String(255))
    active = Column(Boolean)
    gst_verified = Column(Boolean)
    updated_date_time = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)
    updated_by = Column(Integer, nullable=False)
    branch_prefix = Column(String(100))


class JuteMR(Base):
    """Jute Material Receipt table (jute_mr)."""
    __tablename__ = 'jute_mr'
    
    jute_mr_id = Column(BigInteger, primary_key=True, autoincrement=True)
    jute_gate_entry_no = Column(Integer)
    branch_mr_no = Column(Integer)
    jute_gate_entry_date = Column(Date)
    jute_mr_date = Column(Date)
    challan_date = Column(Date)
    challan_no = Column(String(50))
    challan_weight = Column(Float)
    gross_weight = Column(Float)
    tare_weight = Column(Float)
    net_weight = Column(Float)
    variable_shortage = Column(Float)
    actual_weight = Column(Float)
    in_time = Column(Time)
    out_date = Column(Date)
    out_time = Column(Time)
    qc_check = Column(Boolean)
    mukam_id = Column(Integer)
    unit_conversion = Column(String(255))
    mr_weight = Column(Float)
    remarks = Column(String(500))
    status_id = Column(Integer)
    vehicle_no = Column(String(255))
    marketing_slip = Column(Boolean)
    transporter = Column(String(255))
    driver_name = Column(String(255))
    frieght_paid = Column(Float)
    updated_by = Column(Integer)
    updated_date_time = Column(DateTime)
    po_id = Column(Integer)
    branch_id = Column(Integer)
    party_id = Column(Integer)
    party_branch_id = Column(Integer)
    jute_supplier_id = Column(Integer)
    src_com_id = Column(Integer)
    bill_pass_no = Column(BigInteger)
    bill_pass_date = Column(Date)
    total_amount = Column(Float)
    claim_amount = Column(Float)
    roundoff = Column(Float)
    net_total = Column(Float)
    tds_amount = Column(Float)
    invoice_no = Column(String(255))
    invoice_date = Column(Date)
    invoice_amount = Column(Float)
    payment_due_date = Column(Date)
    invoice_received_date = Column(Date)
    invoice_upload = Column(String(255))
    challan_upload = Column(String(255))
    bill_pass_complete = Column(Integer)
    approval_level = Column(Integer)
    src_jute_mr_id = Column(Integer)
    transfer_mode = Column(Integer, nullable=False, default=0)


class JuteMRLineItem(Base):
    """Jute Material Receipt Line Items table (jute_mr_li)."""
    __tablename__ = 'jute_mr_li'
    
    jute_mr_li_id = Column(BigInteger, primary_key=True, autoincrement=True)
    jute_po_li_id = Column(Integer)
    jute_mr_id = Column(BigInteger, ForeignKey('jute_mr.jute_mr_id'))
    actual_item_id = Column(Integer)
    actual_quality = Column(BigInteger)
    actual_qty = Column(Float, default=0)
    actual_weight = Column(Float, default=0)
    challan_item_id = Column(Integer)
    challan_quality_id = Column(BigInteger)
    challan_quantity = Column(Float)
    challan_weight = Column(Float, default=0)
    allowable_moisture = Column(Float)
    actual_moisture = Column(String(255))
    claim_dust = Column(Float)
    shortage_kgs = Column(Integer, default=0)
    accepted_weight = Column(Float)
    rate = Column(Float, default=0)
    claim_rate = Column(Float, nullable=False, default=0)
    water_damage_amount = Column(Numeric(10, 2), default=0.00)
    premium_amount = Column(Numeric(10, 2), default=0.00)
    total_price = Column(Float)
    claim_quality = Column(String(255))
    warehouse_id = Column(BigInteger)
    remarks = Column(String(255))
    status = Column(String(255))
    marka = Column(String(255))
    crop_year = Column(Integer)
    active = Column(Integer, default=1)
    updated_date_time = Column(DateTime)
    unit_conversion = Column(String(255))
    actual_rate = Column(Float)


class JuteMukam(Base):
    """Jute Mukam master table (jute_mukam_mst)."""
    __tablename__ = 'jute_mukam_mst'
    
    mukam_id = Column(BigInteger, primary_key=True, autoincrement=True)
    mukam_name = Column(String(255))
    updated_by = Column(Integer)
    updated_date_time = Column(DateTime, default=datetime.now)


class JuteSupplier(Base):
    """Jute Supplier master table (jute_supplier_mst)."""
    __tablename__ = 'jute_supplier_mst'
    
    supplier_id = Column(Integer, primary_key=True, autoincrement=True)
    supplier_name = Column(String(255))
    email = Column(String(255))
    contact_no = Column(String(255))
    updated_by = Column(Integer)
    updated_date_time = Column(DateTime, default=datetime.now)


class JuteSupplierPartyMap(Base):
    """Jute Supplier to Party mapping table (jute_supp_party_map)."""
    __tablename__ = 'jute_supp_party_map'
    
    map_id = Column(BigInteger, primary_key=True, autoincrement=True)
    co_id = Column(Integer)
    jute_supplier_id = Column(Integer, ForeignKey('jute_supplier_mst.supplier_id'))
    party_id = Column(Integer)
    updated_by = Column(Integer)
    updated_date_time = Column(DateTime, nullable=False, default=datetime.now)


class Item(Base):
    """Item master table (item_mst)."""
    __tablename__ = 'item_mst'
    
    item_id = Column(Integer, primary_key=True, autoincrement=True)
    item_name = Column(String(255))
    item_code = Column(String(255))
    legacy_item_code = Column(String(255))
    hsn_code = Column(String(255))
    item_grp_id = Column(BigInteger)
    uom_id = Column(Integer)
    tangible = Column(Boolean)
    saleable = Column(Boolean)
    consumable = Column(Boolean)
    purchaseable = Column(Boolean)
    manufacturable = Column(Boolean)
    assembly = Column(Boolean)
    tax_percentage = Column(Float)
    uom_rounding = Column(Integer)
    rate_rounding = Column(Integer)
    item_photo = Column(Text)
    active = Column(Integer, nullable=False, default=1)
    updated_by = Column(Integer, nullable=False)
    updated_date_time = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class ItemGroup(Base):
    """Item Group master table (item_grp_mst)."""
    __tablename__ = 'item_grp_mst'
    
    item_grp_id = Column(BigInteger, primary_key=True, autoincrement=True)
    item_grp_name = Column(String(255))
    item_grp_code = Column(String(255))
    parent_grp_id = Column(BigInteger)
    co_id = Column(Integer, ForeignKey('co_mst.co_id'))
    item_type_id = Column(Integer)
    purchase_code = Column(BigInteger)
    active = Column(String(255))
    updated_by = Column(Integer, nullable=False)
    updated_date_time = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class Party(Base):
    """Party master table (party_mst)."""
    __tablename__ = 'party_mst'
    
    party_id = Column(Integer, primary_key=True, autoincrement=True)
    supp_name = Column(String(255))
    prefix = Column(String(25))
    active = Column(Integer, nullable=False, default=1)
    co_id = Column(Integer, ForeignKey('co_mst.co_id'))
    supp_code = Column(String(25))
    supp_contact_person = Column(String(255))
    supp_contact_designation = Column(String(255))
    supp_email_id = Column(String(255))
    phone_no = Column(String(25))
    party_pan_no = Column(String(255))
    cin = Column(String(25))
    entity_type_id = Column(Integer)
    country_id = Column(Integer)
    party_type_id = Column(String(255))
    msme_certified = Column(String(30))
    updated_by = Column(Integer, nullable=False)
    updated_date_time = Column(DateTime, nullable=False, default=datetime.now)


class PartyBranch(Base):
    """Party Branch master table (party_branch_mst)."""
    __tablename__ = 'party_branch_mst'
    
    party_mst_branch_id = Column(Integer, primary_key=True, autoincrement=True)
    party_id = Column(Integer, ForeignKey('party_mst.party_id'))
    active = Column(Integer)
    gst_no = Column(String(255))
    address = Column(String(255))
    address_additional = Column(String(255))
    zip_code = Column(Integer)
    city_id = Column(Integer)
    state_id = Column(Integer)
    contact_no = Column(String(25))
    contact_person = Column(String(255))
    created_date = Column(DateTime)
    created_by = Column(Integer)
    updated_by = Column(Integer, nullable=False)
    updated_date_time = Column(DateTime, nullable=False, default=datetime.now)


class SalesInvoice(Base):
    """Sales Invoice header table (sales_invoice)."""
    __tablename__ = 'sales_invoice'
    
    invoice_id = Column(BigInteger, primary_key=True, autoincrement=True)
    invoice_no = Column(BigInteger)
    invoice_date = Column(Date)
    invoice_type = Column(Integer)
    invoice_amount = Column(Float)
    challan_date = Column(Date)
    challan_no = Column(String(255))
    party_id = Column(BigInteger)
    sales_delivery_order_id = Column(Integer)
    broker_id = Column(Integer)
    billing_to_id = Column(BigInteger)
    shipping_to_id = Column(BigInteger)
    branch_id = Column(BigInteger, ForeignKey('branch_mst.branch_id'))
    due_date = Column(Date)
    footer_notes = Column(String(255))
    internal_note = Column(String(500))
    freight_charges = Column(String(255))
    intra_inter_state = Column(Integer)
    active = Column(Integer)
    status_id = Column(Integer)
    shipping_state_code = Column(Integer)
    approval_level = Column(Integer, default=0)
    tax_amount = Column(Float)
    tax_id = Column(BigInteger)
    tax_payable = Column(Float)
    terms = Column(String(255))
    terms_conditions = Column(String(255))
    type_of_sale = Column(String(255))
    vehicle_no = Column(String(255))
    transporter_id = Column(BigInteger)
    transporter_name = Column(String(100))
    transporter_address = Column(String(500))
    transporter_state_code = Column(String(100))
    transporter_state_name = Column(String(100))
    container_no = Column(String(100))
    contract_no = Column(BigInteger)
    contract_date = Column(Date)
    consignment_no = Column(String(255))
    consignment_date = Column(Date)
    eway_bill_no = Column(String(100))
    eway_bill_date = Column(Date)
    round_off = Column(Numeric(10, 2), nullable=False)
    updated_by = Column(Integer)
    updated_date_time = Column(DateTime, default=datetime.now)


class SalesInvoiceDetail(Base):
    """Sales Invoice detail/line items table (sales_invoice_dtl)."""
    __tablename__ = 'sales_invoice_dtl'

    invoice_line_item_id = Column(BigInteger, primary_key=True, autoincrement=True)
    invoice_id = Column(BigInteger, ForeignKey('sales_invoice.invoice_id'))
    item_id = Column(Integer)
    item_make_id = Column(Integer)
    hsn_code = Column(String(255))
    quantity = Column(Float)
    sales_weight = Column(Float)
    uom_id = Column(Integer)
    rate = Column(Float)
    amount_without_tax = Column(Float)
    total_amount = Column(Float)
    remarks = Column(String(255))


class SalesInvoiceJute(Base):
    """Sales Invoice Jute-specific table (sales_invoice_jute)."""
    __tablename__ = 'sales_invoice_jute'
    
    sales_invoice_jute_id = Column(Integer, primary_key=True, autoincrement=True)
    invoice_id = Column(BigInteger, ForeignKey('sales_invoice.invoice_id'))
    mr_no = Column(String(50))
    mr_id = Column(BigInteger)
    mukam_id = Column(Integer)
    claim_amount = Column(BigInteger)
    claim_description = Column(String(255))
    other_reference = Column(String(100))
    unit_conversion = Column(String(50))


class SalesInvoiceJuteDetail(Base):
    """Sales Invoice Jute detail table (sales_invoice_jute_dtl)."""
    __tablename__ = 'sales_invoice_jute_dtl'
    
    sales_invoice_jute_dtl_id = Column(Integer, primary_key=True, autoincrement=True)
    invoice_line_item_id = Column(Integer)
    claim_desc = Column(String(255))
    claim_rate = Column(Float)
    claim_amount_dtl = Column(Float)
    unit_conversion = Column(String(255))
    qty_untit_conversion = Column(Integer)


class Warehouse(Base):
    """Warehouse master table (warehouse_mst)."""
    __tablename__ = 'warehouse_mst'
    
    warehouse_id = Column(Integer, primary_key=True, autoincrement=True)
    warehouse_name = Column(String(30))
    warehouse_type = Column(String(20))
    branch_id = Column(Integer, ForeignKey('branch_mst.branch_id'))
    parent_warehouse_id = Column(Integer)
    updated_by = Column(Integer)
    updated_date_time = Column(DateTime)


class Status(Base):
    """Status master table (status_mst)."""
    __tablename__ = 'status_mst'
    
    status_id = Column(Integer, primary_key=True, autoincrement=True)
    status_name = Column(String(255))
    status_grp = Column(String(255))
    created_date = Column(DateTime)
    created_by = Column(Integer)


# Convenience function to get table information
def get_model_by_tablename(tablename: str):
    """Get model class by table name.
    
    Args:
        tablename: Name of the database table
        
    Returns:
        Model class or None if not found
    """
    model_map = {
        'co_mst': Company,
        'branch_mst': Branch,
        'jute_mr': JuteMR,
        'jute_mr_li': JuteMRLineItem,
        'jute_mukam_mst': JuteMukam,
        'jute_supplier_mst': JuteSupplier,
        'jute_supp_party_map': JuteSupplierPartyMap,
        'item_mst': Item,
        'item_grp_mst': ItemGroup,
        'party_mst': Party,
        'party_branch_mst': PartyBranch,
        'sales_invoice': SalesInvoice,
        'sales_invoice_dtl': SalesInvoiceDetail,
        'sales_invoice_jute': SalesInvoiceJute,
        'sales_invoice_jute_dtl': SalesInvoiceJuteDetail,
        'warehouse_mst': Warehouse,
        'status_mst': Status,
    }
    return model_map.get(tablename)


def get_all_models():
    """Get list of all model classes.
    
    Returns:
        list: List of all model classes
    """
    return [
        Company, Branch, JuteMR, JuteMRLineItem, JuteMukam,
        JuteSupplier, JuteSupplierPartyMap, Item, ItemGroup, Party, PartyBranch,
        SalesInvoice, SalesInvoiceDetail, SalesInvoiceJute,
        SalesInvoiceJuteDetail, Warehouse, Status
    ]
