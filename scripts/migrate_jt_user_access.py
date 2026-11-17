"""Create the jt_user_access table (sls only): who may use the transfer app
and who is a transfer admin. Idempotent; adds one empty table, touches
nothing else.

Dry run (default): python -m scripts.migrate_jt_user_access
Apply:             python -m scripts.migrate_jt_user_access --apply
"""
import sys

from src.jutetransfer import access
from src.jutetransfer.database import DatabaseConnection, get_table_schema

if __name__ == "__main__":
    if "--apply" not in sys.argv:
        print("DRY RUN. Would run:\n" + access.DDL)
        print("Table exists already:", access.table_exists())
        sys.exit(0)
    DatabaseConnection.execute_non_query(access.DDL)
    print(get_table_schema("jt_user_access").to_string(index=False))
