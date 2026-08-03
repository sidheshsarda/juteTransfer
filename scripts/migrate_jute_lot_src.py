"""Create the jute_lot_src provenance table (sls only). Idempotent.

Run: python -m scripts.migrate_jute_lot_src
"""
from src.jutetransfer.database import DatabaseConnection, get_table_schema

DDL = """
CREATE TABLE IF NOT EXISTS jute_lot_src (
    lot_src_id         BIGINT PRIMARY KEY AUTO_INCREMENT,
    new_jute_mr_li_id  BIGINT NOT NULL,
    src_jute_mr_li_id  BIGINT NOT NULL,
    qty_kg             DECIMAL(12,3) NOT NULL,
    actual_qty_delta   DECIMAL(12,3) NULL,
    actual_weight_delta DECIMAL(12,3) NULL,
    created_by         INT NULL,
    created_date_time  DATETIME NULL,
    KEY idx_lot_src_new (new_jute_mr_li_id),
    KEY idx_lot_src_src (src_jute_mr_li_id)
)
"""

if __name__ == "__main__":
    DatabaseConnection.execute_non_query(DDL)
    print(get_table_schema("jute_lot_src").to_string(index=False))
