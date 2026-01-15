# juteTransfer

A Streamlit web application for managing jute transfers between warehouses and factories with login authentication, interactive data grids, and analytics.

> 🚀 **Working in Codespaces?** See [QUICKSTART_ENV.md](QUICKSTART_ENV.md) for quick environment setup!
> 
> 📚 **New to the project?** Check [DOCUMENTATION_INDEX.md](DOCUMENTATION_INDEX.md) to find the right guide for you!

## Features

- 🔐 **User Authentication**: Secure login system with session management
- 📊 **Interactive Data Grid**: AgGrid integration for advanced data viewing and filtering
- 📈 **Analytics Dashboard**: Visual analytics with charts and statistics
- 🌾 **Jute Transfer Management**: Track transfers, quantities, costs, and status
- 🎨 **Modern UI**: Clean and responsive Streamlit interface

## Tech Stack

- **Python 3.12+**
- **uv**: Fast Python package manager
- **Streamlit**: Web application framework
- **pandas**: Data manipulation and analysis
- **streamlit-aggrid**: Advanced data grid component
- **MySQL**: Database for data persistence
- **SQLAlchemy**: SQL toolkit and ORM
- **mysql-connector-python**: MySQL database driver

## Installation

### Prerequisites

- Python 3.12 or higher
- uv (Python package manager)
- MySQL 8.0 or higher

### Setup

1. Clone the repository:
```bash
git clone https://github.com/sidheshsarda/juteTransfer.git
cd juteTransfer
```

2. Install uv (if not already installed):
```bash
pip install uv
```

3. Create virtual environment and install dependencies:
```bash
uv sync
```

4. Set up the database configuration:

**Option A: For GitHub Codespaces (Recommended)**
```bash
# Use the automated setup script
./setup_codespace_env.sh
```
This script will guide you through setting up your database connection (cloud DB or Docker container).

**Option B: Manual Setup**
```bash
# Copy the example environment file
cp .env.example .env

# Edit .env with your MySQL credentials
# Update DB_HOST, DB_USER, DB_PASSWORD, DB_NAME
```

For detailed Codespaces setup instructions, see [CODESPACES_ENV_SETUP.md](CODESPACES_ENV_SETUP.md)

5. Initialize the database:
```bash
# Make sure MySQL is running and credentials in .env are correct
python init_database.py
```

This will create the necessary tables and insert sample data.

## Usage

### Running the Application

Activate the virtual environment and run the Streamlit app:

```bash
# Activate virtual environment
source .venv/bin/activate  # On Unix/macOS
# or
.venv\Scripts\activate  # On Windows

# Run the application
streamlit run app.py
```

Or use uv to run directly:

```bash
uv run streamlit run app.py
```

The application will open in your default web browser at `http://localhost:8501`

### Demo Credentials

Use these credentials to log in:
- **Username**: `admin` | **Password**: `admin123`
- **Username**: `user` | **Password**: `user123`

## Project Structure

```
juteTransfer/
├── app.py                          # Main Streamlit application
├── init_database.py                # Database initialization script
├── .env.example                    # Environment variables template
├── src/
│   └── jutetransfer/
│       ├── __init__.py            # Package initialization
│       ├── auth.py                # Authentication module
│       ├── config.py              # Configuration management
│       ├── database.py            # Database connection utilities
│       └── data.py                # Data generation and utilities
├── pyproject.toml                 # Project dependencies
├── .python-version                # Python version specification
├── .gitignore                     # Git ignore rules
└── README.md                      # This file
```

## Features Overview

### Database
- MySQL integration with SQLAlchemy ORM
- Connection pooling for efficient resource usage
- Automated table creation and initialization
- Support for warehouses, factories, transfers, and audit logs

### Authentication
- Secure login page with form-based authentication
- Session state management
- User-specific greetings and navigation

### Dashboard
- Summary metrics (total transfers, quantities, costs)
- Status distribution charts
- Source location analytics

### Data View
- Interactive AgGrid with pagination
- Multi-select row selection
- Filtering by status and location
- Color-coded status indicators
- Sortable and searchable columns

### Analytics
- Quality grade analysis
- Cost analysis by destination
- Quantity distribution charts
- Tabbed interface for different views

## Development

### Adding Dependencies

Use uv to add new packages:
```bash
uv add package-name
```

### Project Configuration

The project uses `pyproject.toml` for dependency management and configuration. Key dependencies:
- `streamlit>=1.53.0`
- `streamlit-aggrid>=1.2.1.post2`
- `pandas>=2.3.3`
- `mysql-connector-python>=9.0.0`
- `sqlalchemy>=2.0.0`
- `python-dotenv>=1.0.0`

### Environment Variables

Create a `.env` file in the project root with your database credentials:
```env
DB_HOST=localhost
DB_PORT=3306
DB_USER=your_username
DB_PASSWORD=your_password
DB_NAME=jutetransfer
```

### Database Schema

The application uses the following tables:
- **users**: User authentication and profiles
- **warehouses**: Warehouse information and locations
- **factories**: Factory details and production capacity
- **transfers**: Jute transfer records between warehouses and factories
- **transfer_logs**: Audit trail for transfer status changes

## License

MIT License - feel free to use this project for your needs.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
