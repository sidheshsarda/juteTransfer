# juteTransfer

A Streamlit web application for managing jute transfers between warehouses and factories with login authentication, interactive data grids, and analytics.

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
- **numpy**: Numerical computing

## Installation

### Prerequisites

- Python 3.12 or higher
- uv (Python package manager)

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

The application will open in your default web browser at `http://localhost:8502`

### Demo Credentials

Use these credentials to log in:
- **Username**: `admin` | **Password**: `admin123`
- **Username**: `user` | **Password**: `user123`

## Project Structure

```
juteTransfer/
├── app.py                          # Main Streamlit application
├── src/
│   └── jutetransfer/
│       ├── __init__.py            # Package initialization
│       ├── auth.py                # Authentication module
│       └── data.py                # Data generation and utilities
├── pyproject.toml                 # Project dependencies
├── .python-version                # Python version specification
├── .gitignore                     # Git ignore rules
└── README.md                      # This file
```

## Features Overview

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

## License

MIT License - feel free to use this project for your needs.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
