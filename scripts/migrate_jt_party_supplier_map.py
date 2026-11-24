"""Create the jt_party_supplier_map table (sls only): the owner's confirmed
party -> jute supplier list used by the Payment Summary page
(src/jutetransfer/supplier_map.py). Idempotent; adds one empty table,
touches nothing else.

Dry run (default): python -m scripts.migrate_jt_party_supplier_map
Apply:             python -m scripts.migrate_jt_party_supplier_map --apply
"""
import sys

from src.jutetransfer import supplier_map
from src.jutetransfer.database import DatabaseConnection, get_table_schema

if __name__ == "__main__":
    if "--apply" not in sys.argv:
        print("DRY RUN. Would run:\n" + supplier_map.DDL)
        sys.exit(0)
    DatabaseConnection.execute_non_query(supplier_map.DDL)
    print(get_table_schema("jt_party_supplier_map").to_string(index=False))
