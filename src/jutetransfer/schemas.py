"""Schema utilities for database tables."""

import json
import os
from pathlib import Path

# Path to the schemas file
SCHEMAS_FILE = Path(__file__).parent / "schemas.json"


def load_schemas() -> dict:
    """Load schemas from JSON file.

    Returns:
        dict: Dictionary of table schemas
    """
    if SCHEMAS_FILE.exists():
        with open(SCHEMAS_FILE, 'r') as f:
            return json.load(f)
    return {}


def get_table_schema(table_name: str) -> list | None:
    """Get schema for a specific table.

    Args:
        table_name: Name of the table

    Returns:
        list: List of column definitions or None if not found
    """
    schemas = load_schemas()
    return schemas.get(table_name)


def get_all_table_names() -> list:
    """Get list of all table names with saved schemas.

    Returns:
        list: List of table names
    """
    schemas = load_schemas()
    return [name for name, schema in schemas.items() if schema is not None]


def refresh_schemas_from_db():
    """Refresh schemas from database and save to file."""
    from .database import DatabaseConnection

    tables = [
        'jute_mr',
        'jute_mr_li',
        'co_mst',
        'branch_mst',
        'jute_mukam_mst',
        'jute_po',
        'jute_supp_party_map',
        'jute_supplier_mst',
        'party_mst',
        'jute_quality_mst'
    ]

    schemas = {}
    for table in tables:
        try:
            query = f'DESCRIBE {table}'
            df = DatabaseConnection.execute_query(query)
            schemas[table] = df.to_dict(orient='records')
        except Exception:
            schemas[table] = None

    with open(SCHEMAS_FILE, 'w') as f:
        json.dump(schemas, f, indent=2)

    return schemas
