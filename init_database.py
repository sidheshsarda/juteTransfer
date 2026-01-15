"""Database initialization script for JuteTransfer."""

from src.jutetransfer.database import DatabaseConnection
from src.jutetransfer.config import DatabaseConfig
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_tables():
    """Create initial database tables."""
    
    # Create users table
    create_users_table = """
    CREATE TABLE IF NOT EXISTS users (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(50) UNIQUE NOT NULL,
        password_hash VARCHAR(255) NOT NULL,
        full_name VARCHAR(100),
        email VARCHAR(100),
        role VARCHAR(20) DEFAULT 'user',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        is_active BOOLEAN DEFAULT TRUE
    )
    """
    
    # Create warehouses table
    create_warehouses_table = """
    CREATE TABLE IF NOT EXISTS warehouses (
        id INT AUTO_INCREMENT PRIMARY KEY,
        name VARCHAR(100) NOT NULL,
        location VARCHAR(255),
        capacity_tons DECIMAL(10, 2),
        manager_name VARCHAR(100),
        contact_number VARCHAR(20),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    )
    """
    
    # Create factories table
    create_factories_table = """
    CREATE TABLE IF NOT EXISTS factories (
        id INT AUTO_INCREMENT PRIMARY KEY,
        name VARCHAR(100) NOT NULL,
        location VARCHAR(255),
        production_capacity DECIMAL(10, 2),
        manager_name VARCHAR(100),
        contact_number VARCHAR(20),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    )
    """
    
    # Create transfers table
    create_transfers_table = """
    CREATE TABLE IF NOT EXISTS transfers (
        id INT AUTO_INCREMENT PRIMARY KEY,
        transfer_id VARCHAR(50) UNIQUE NOT NULL,
        transfer_date DATE NOT NULL,
        from_warehouse_id INT,
        to_factory_id INT,
        jute_type VARCHAR(50),
        quality_grade VARCHAR(20),
        quantity_tons DECIMAL(10, 2) NOT NULL,
        rate_per_ton DECIMAL(10, 2),
        total_cost DECIMAL(15, 2),
        transport_mode VARCHAR(50),
        vehicle_number VARCHAR(50),
        driver_name VARCHAR(100),
        status VARCHAR(20) DEFAULT 'pending',
        notes TEXT,
        created_by INT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        FOREIGN KEY (from_warehouse_id) REFERENCES warehouses(id),
        FOREIGN KEY (to_factory_id) REFERENCES factories(id),
        FOREIGN KEY (created_by) REFERENCES users(id)
    )
    """
    
    # Create transfer_logs table for audit trail
    create_logs_table = """
    CREATE TABLE IF NOT EXISTS transfer_logs (
        id INT AUTO_INCREMENT PRIMARY KEY,
        transfer_id INT,
        action VARCHAR(50),
        old_status VARCHAR(20),
        new_status VARCHAR(20),
        changed_by INT,
        changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        notes TEXT,
        FOREIGN KEY (transfer_id) REFERENCES transfers(id),
        FOREIGN KEY (changed_by) REFERENCES users(id)
    )
    """
    
    tables = {
        "users": create_users_table,
        "warehouses": create_warehouses_table,
        "factories": create_factories_table,
        "transfers": create_transfers_table,
        "transfer_logs": create_logs_table,
    }
    
    try:
        for table_name, create_query in tables.items():
            logger.info(f"Creating table: {table_name}")
            DatabaseConnection.execute_non_query(create_query)
            logger.info(f"Table {table_name} created successfully")
        
        logger.info("All tables created successfully!")
        return True
    except Exception as e:
        logger.error(f"Error creating tables: {e}")
        return False


def insert_sample_data():
    """Insert sample data for testing."""
    
    # Sample warehouses
    warehouses_data = """
    INSERT INTO warehouses (name, location, capacity_tons, manager_name, contact_number)
    VALUES 
        ('Central Warehouse', 'Kolkata, West Bengal', 5000.00, 'Rajesh Kumar', '+91-9876543210'),
        ('North Zone Storage', 'Malda, West Bengal', 3000.00, 'Amit Singh', '+91-9876543211'),
        ('East Regional Hub', 'Burdwan, West Bengal', 4000.00, 'Suresh Gupta', '+91-9876543212')
    ON DUPLICATE KEY UPDATE name=name
    """
    
    # Sample factories
    factories_data = """
    INSERT INTO factories (name, location, production_capacity, manager_name, contact_number)
    VALUES 
        ('Bengal Jute Mill', 'Howrah, West Bengal', 2000.00, 'Priya Sharma', '+91-9876543213'),
        ('Eastern Textile Factory', 'Hooghly, West Bengal', 1500.00, 'Vikram Mehta', '+91-9876543214'),
        ('Premium Jute Industries', 'Kolkata, West Bengal', 2500.00, 'Anita Das', '+91-9876543215')
    ON DUPLICATE KEY UPDATE name=name
    """
    
    try:
        logger.info("Inserting sample warehouse data...")
        DatabaseConnection.execute_non_query(warehouses_data)
        
        logger.info("Inserting sample factory data...")
        DatabaseConnection.execute_non_query(factories_data)
        
        logger.info("Sample data inserted successfully!")
        return True
    except Exception as e:
        logger.error(f"Error inserting sample data: {e}")
        return False


def main():
    """Main initialization function."""
    logger.info("=" * 50)
    logger.info("JuteTransfer Database Initialization")
    logger.info("=" * 50)
    
    # Test connection
    success, message = DatabaseConnection.test_connection()
    logger.info(message)
    
    if not success:
        logger.error("Cannot proceed without database connection")
        return
    
    # Create tables
    logger.info("\n" + "=" * 50)
    logger.info("Creating database tables...")
    logger.info("=" * 50)
    if create_tables():
        logger.info("✓ Tables created successfully")
    else:
        logger.error("✗ Failed to create tables")
        return
    
    # Insert sample data
    logger.info("\n" + "=" * 50)
    logger.info("Inserting sample data...")
    logger.info("=" * 50)
    if insert_sample_data():
        logger.info("✓ Sample data inserted successfully")
    else:
        logger.warning("⚠ Failed to insert sample data (may already exist)")
    
    logger.info("\n" + "=" * 50)
    logger.info("Database initialization complete!")
    logger.info("=" * 50)


if __name__ == "__main__":
    main()
