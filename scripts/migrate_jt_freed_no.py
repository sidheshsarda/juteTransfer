"""Create the jt_freed_no table (sls only): numbers freed by a delete, so
the next save whose date fits takes them back (src/jutetransfer/freed.py).
Idempotent; adds one empty table, touches nothing else.

Dry run (default): python -m scripts.migrate_jt_freed_no
Apply:             python -m scripts.migrate_jt_freed_no --apply
"""
import sys

from src.jutetransfer import freed
from src.jutetransfer.database import DatabaseConnection, get_table_schema

if __name__ == "__main__":
    if "--apply" not in sys.argv:
        print("DRY RUN. Would run:\n" + freed.DDL)
        sys.exit(0)
    DatabaseConnection.execute_non_query(freed.DDL)
    print(get_table_schema("jt_freed_no").to_string(index=False))
