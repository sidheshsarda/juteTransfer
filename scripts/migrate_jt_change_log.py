"""Create the jt_change_log table (sls only): the record of every exception
a transfer admin makes (date-rule overrides, in-place edits). Idempotent;
adds one empty table, touches nothing else.

Dry run (default): python -m scripts.migrate_jt_change_log
Apply:             python -m scripts.migrate_jt_change_log --apply
"""
import sys

from src.jutetransfer import audit
from src.jutetransfer.database import DatabaseConnection, get_table_schema

if __name__ == "__main__":
    if "--apply" not in sys.argv:
        print("DRY RUN. Would run:\n" + audit.DDL)
        sys.exit(0)
    DatabaseConnection.execute_non_query(audit.DDL)
    print(get_table_schema("jt_change_log").to_string(index=False))
