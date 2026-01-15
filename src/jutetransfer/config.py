"""Configuration management for JuteTransfer application."""

import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class DatabaseConfig:
    """Database configuration settings."""
    
    HOST = os.getenv("DB_HOST", "localhost")
    PORT = int(os.getenv("DB_PORT", "3306"))
    USER = os.getenv("DB_USER", "root")
    PASSWORD = os.getenv("DB_PASSWORD", "")
    DATABASE = os.getenv("DB_NAME", "jutetransfer")
    
    # Connection pool settings
    POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "5"))
    MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "10"))
    
    @classmethod
    def get_connection_string(cls) -> str:
        """Generate SQLAlchemy connection string."""
        return f"mysql+mysqlconnector://{cls.USER}:{cls.PASSWORD}@{cls.HOST}:{cls.PORT}/{cls.DATABASE}"
    
    @classmethod
    def get_mysql_config(cls) -> dict:
        """Get MySQL connector configuration dictionary."""
        return {
            "host": cls.HOST,
            "port": cls.PORT,
            "user": cls.USER,
            "password": cls.PASSWORD,
            "database": cls.DATABASE,
        }


class AppConfig:
    """Application configuration settings."""
    
    # App settings
    APP_NAME = "JuteTransfer"
    APP_ICON = "🌾"
    
    # Session settings
    SESSION_TIMEOUT = int(os.getenv("SESSION_TIMEOUT", "3600"))  # 1 hour
