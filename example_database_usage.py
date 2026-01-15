"""Example script demonstrating database usage in JuteTransfer."""

from src.jutetransfer.database import (
    DatabaseConnection,
    fetch_table_data,
    get_all_tables,
    get_table_schema
)
from src.jutetransfer.config import DatabaseConfig
import pandas as pd


def example_test_connection():
    """Test database connection."""
    print("Testing database connection...")
    success, message = DatabaseConnection.test_connection()
    print(f"Status: {message}\n")
    return success


def example_list_tables():
    """List all tables in the database."""
    print("Listing all tables...")
    try:
        tables = get_all_tables()
        print(f"Found {len(tables)} tables:")
        for table in tables:
            print(f"  - {table}")
        print()
        return tables
    except Exception as e:
        print(f"Error: {e}\n")
        return []


def example_table_schema(table_name: str):
    """Show table schema."""
    print(f"Schema for table '{table_name}':")
    try:
        schema = get_table_schema(table_name)
        print(schema.to_string())
        print()
    except Exception as e:
        print(f"Error: {e}\n")


def example_fetch_data(table_name: str, limit: int = 5):
    """Fetch and display data from a table."""
    print(f"Fetching data from '{table_name}' (limit {limit}):")
    try:
        df = fetch_table_data(table_name, limit=limit)
        if not df.empty:
            print(df.to_string())
            print(f"\nTotal rows: {len(df)}")
        else:
            print("No data found")
        print()
        return df
    except Exception as e:
        print(f"Error: {e}\n")
        return pd.DataFrame()


def example_custom_query():
    """Execute a custom SQL query."""
    print("Executing custom query...")
    query = """
    SELECT 
        t.transfer_id,
        t.transfer_date,
        w.name as warehouse,
        f.name as factory,
        t.quantity_tons,
        t.status
    FROM transfers t
    LEFT JOIN warehouses w ON t.from_warehouse_id = w.id
    LEFT JOIN factories f ON t.to_factory_id = f.id
    LIMIT 5
    """
    
    try:
        df = DatabaseConnection.execute_query(query)
        if not df.empty:
            print(df.to_string())
            print(f"\nTotal rows: {len(df)}")
        else:
            print("No data found")
        print()
    except Exception as e:
        print(f"Error: {e}\n")


def example_insert_data():
    """Insert sample data using DataFrame."""
    print("Inserting sample data...")
    
    # Create sample transfer data
    sample_data = pd.DataFrame({
        'transfer_id': ['TRF-DEMO-001'],
        'transfer_date': ['2026-01-15'],
        'from_warehouse_id': [1],
        'to_factory_id': [1],
        'jute_type': ['Raw Jute'],
        'quality_grade': ['A'],
        'quantity_tons': [50.5],
        'rate_per_ton': [45000.00],
        'total_cost': [2272500.00],
        'transport_mode': ['Truck'],
        'vehicle_number': ['WB-01-AB-1234'],
        'driver_name': ['Demo Driver'],
        'status': ['completed'],
        'notes': ['Demo transfer created by example script']
    })
    
    try:
        # Insert data (use if_exists='append' to add to existing data)
        DatabaseConnection.insert_dataframe(sample_data, 'transfers', if_exists='append')
        print("Sample data inserted successfully!\n")
    except Exception as e:
        print(f"Error: {e}\n")


def main():
    """Main function to run all examples."""
    print("=" * 60)
    print("JuteTransfer Database Usage Examples")
    print("=" * 60)
    print(f"Database: {DatabaseConfig.DATABASE}")
    print(f"Host: {DatabaseConfig.HOST}:{DatabaseConfig.PORT}")
    print("=" * 60)
    print()
    
    # Test connection
    if not example_test_connection():
        print("Cannot proceed without database connection.")
        print("Please check your .env file and MySQL server.")
        return
    
    # List tables
    tables = example_list_tables()
    
    # Show schemas and data for main tables
    main_tables = ['warehouses', 'factories', 'transfers']
    for table in main_tables:
        if table in tables:
            example_table_schema(table)
            example_fetch_data(table, limit=3)
    
    # Execute custom query
    if 'transfers' in tables:
        example_custom_query()
    
    # Optional: Uncomment to insert demo data
    # example_insert_data()
    
    print("=" * 60)
    print("Examples completed!")
    print("=" * 60)


if __name__ == "__main__":
    main()
