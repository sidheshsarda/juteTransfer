# MySQL Database Setup for JuteTransfer

## Overview
The JuteTransfer application now includes MySQL database integration for persistent data storage. This document provides a quick start guide.

## Prerequisites
- MySQL 8.0+ installed and running
- Database credentials with CREATE, INSERT, SELECT permissions

## Quick Start

### 1. Configure Database Connection

Copy the example environment file and configure your database credentials:

```bash
cp .env.example .env
```

Edit `.env` with your MySQL details:
```env
DB_HOST=localhost
DB_PORT=3306
DB_USER=your_mysql_username
DB_PASSWORD=your_mysql_password
DB_NAME=jutetransfer
```

### 2. Create the Database

Connect to MySQL and create the database:

```sql
CREATE DATABASE jutetransfer CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

Or via command line:
```bash
mysql -u root -p -e "CREATE DATABASE jutetransfer CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
```

### 3. Initialize Database Tables

Run the initialization script to create tables and insert sample data:

```bash
python init_database.py
```

This script will:
- Create all necessary tables (users, warehouses, factories, transfers, transfer_logs)
- Insert sample warehouse and factory data
- Set up foreign key relationships

### 4. Verify Installation

Test the database connection and explore the data:

```bash
python example_database_usage.py
```

This will show:
- Connection status
- List of created tables
- Table schemas
- Sample data from each table

## Database Schema

### Tables Created

1. **users** - User authentication and profiles
   - id, username, password_hash, full_name, email, role, etc.

2. **warehouses** - Warehouse information
   - id, name, location, capacity_tons, manager_name, contact_number

3. **factories** - Factory details
   - id, name, location, production_capacity, manager_name, contact_number

4. **transfers** - Jute transfer records
   - id, transfer_id, transfer_date, from_warehouse_id, to_factory_id
   - jute_type, quality_grade, quantity_tons, rate_per_ton, total_cost
   - transport_mode, vehicle_number, driver_name, status, notes

5. **transfer_logs** - Audit trail for status changes
   - id, transfer_id, action, old_status, new_status, changed_by, notes

## Using the Database in Your Code

### Import Database Utilities

```python
from src.jutetransfer.database import (
    DatabaseConnection,
    fetch_table_data,
    get_all_tables,
    get_table_schema
)
from src.jutetransfer.config import DatabaseConfig
```

### Test Connection

```python
success, message = DatabaseConnection.test_connection()
print(message)
```

### Fetch Data

```python
# Fetch all warehouses
warehouses_df = fetch_table_data('warehouses')

# Fetch with filters
recent_transfers = fetch_table_data(
    'transfers',
    where_clause="status = 'pending'",
    order_by="transfer_date DESC",
    limit=10
)
```

### Execute Custom Queries

```python
query = """
SELECT w.name, COUNT(t.id) as transfer_count
FROM warehouses w
LEFT JOIN transfers t ON w.id = t.from_warehouse_id
GROUP BY w.id, w.name
"""
result_df = DatabaseConnection.execute_query(query)
```

### Insert Data

```python
# From DataFrame
import pandas as pd

new_warehouse = pd.DataFrame({
    'name': ['New Warehouse'],
    'location': ['Mumbai, Maharashtra'],
    'capacity_tons': [3500.00],
    'manager_name': ['Manager Name'],
    'contact_number': ['+91-9876543216']
})

DatabaseConnection.insert_dataframe(new_warehouse, 'warehouses')
```

### Execute Non-Query Operations

```python
# Update status
update_query = """
UPDATE transfers 
SET status = :new_status 
WHERE transfer_id = :transfer_id
"""
rows_affected = DatabaseConnection.execute_non_query(
    update_query,
    params={'new_status': 'completed', 'transfer_id': 'TRF-001'}
)
```

## Configuration Options

### Connection Pooling

The database connection uses SQLAlchemy connection pooling with these settings (configurable in `.env`):

```env
DB_POOL_SIZE=5          # Number of connections to maintain
DB_MAX_OVERFLOW=10      # Additional connections allowed during high load
```

### Environment Variables

All database configuration is managed through environment variables:

- `DB_HOST` - Database host (default: localhost)
- `DB_PORT` - Database port (default: 3306)
- `DB_USER` - Database username
- `DB_PASSWORD` - Database password
- `DB_NAME` - Database name
- `DB_POOL_SIZE` - Connection pool size (default: 5)
- `DB_MAX_OVERFLOW` - Max overflow connections (default: 10)

## Troubleshooting

### Connection Failed

If you see "Database connection failed":
1. Verify MySQL is running: `sudo systemctl status mysql`
2. Check credentials in `.env` file
3. Ensure database exists: `SHOW DATABASES;`
4. Verify user permissions: `SHOW GRANTS FOR 'username'@'localhost';`

### Table Creation Issues

If tables aren't created:
1. Check user has CREATE permission
2. Verify database name is correct
3. Check init_database.py output for specific errors

### Import Errors

If you get import errors:
1. Ensure packages are installed: `pip list | grep -E "(mysql|sqlalchemy|dotenv)"`
2. Reinstall if needed: `pip install mysql-connector-python sqlalchemy python-dotenv`

## Next Steps

1. **Integrate with Streamlit App**: Update `app.py` to fetch data from database instead of generating sample data
2. **Add User Management**: Implement user registration and authentication against the users table
3. **Create Data Entry Forms**: Add Streamlit forms for creating new transfers, warehouses, etc.
4. **Add Data Validation**: Implement validation rules before inserting data
5. **Create Reports**: Build dashboard queries and analytics from real data

## Security Notes

⚠️ **Important Security Practices:**

- Never commit `.env` file to version control (already in `.gitignore`)
- Use strong database passwords
- Limit database user permissions to only what's needed
- Use environment-specific credentials (dev, staging, production)
- Consider using secrets management for production deployments
- Enable SSL/TLS for database connections in production

## Additional Resources

- [SQLAlchemy Documentation](https://docs.sqlalchemy.org/)
- [MySQL Connector Python](https://dev.mysql.com/doc/connector-python/en/)
- [Streamlit Database Connections](https://docs.streamlit.io/develop/tutorials/databases)
