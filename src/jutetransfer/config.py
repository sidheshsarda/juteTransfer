"""Database configuration utilities."""

import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class DatabaseConfig:
    """Database configuration from environment variables."""

    # Database connection settings
    HOST = os.getenv("DB_HOST", "localhost")
    PORT = int(os.getenv("DB_PORT", "3306"))
    DATABASE = os.getenv("DB_NAME", "jutetransfer")
    USER = os.getenv("DB_USER", "root")
    PASSWORD = os.getenv("DB_PASSWORD", "")

    # Connection pool settings
    POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "5"))
    MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "10"))

    @classmethod
    def get_connection_string(cls) -> str:
        """Get SQLAlchemy connection string for MySQL.

        Returns:
            str: MySQL connection string
        """
        return (
            f"mysql+mysqlconnector://{cls.USER}:{cls.PASSWORD}"
            f"@{cls.HOST}:{cls.PORT}/{cls.DATABASE}"
        )

    @classmethod
    def get_mysql_config(cls) -> dict:
        """Get MySQL connector configuration dictionary.

        Returns:
            dict: MySQL connection configuration
        """
        return {
            "host": cls.HOST,
            "port": cls.PORT,
            "database": cls.DATABASE,
            "user": cls.USER,
            "password": cls.PASSWORD,
        }
