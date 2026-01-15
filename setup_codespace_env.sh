# Environment Variables in Codespaces & Online Workspaces

## Overview
When working in GitHub Codespaces or browser-based development environments, you need to securely manage environment variables without committing sensitive data to your repository.

## Recommended Approach: GitHub Codespaces Secrets

### Option 1: Repository Secrets (Recommended for Team Projects)

1. **Navigate to your repository on GitHub**
2. Go to **Settings** → **Secrets and variables** → **Codespaces**
3. Click **New repository secret**
4. Add each environment variable:

   ```
   Name: DB_HOST
   Value: your-database-host.com
   
   Name: DB_PORT
   Value: 3306
   
   Name: DB_USER
   Value: your_username
   
   Name: DB_PASSWORD
   Value: your_secure_password
   
   Name: DB_NAME
   Value: jutetransfer
   ```

5. These secrets will be automatically available as environment variables in all Codespaces for this repository

### Option 2: User Secrets (For Personal Projects Across Multiple Repos)

1. Go to your GitHub profile → **Settings**
2. Navigate to **Codespaces** → **Secrets**
3. Click **New secret**
4. Add the same environment variables as above
5. Select which repositories can access these secrets

### Accessing Secrets in Your Application

The Python `dotenv` library will automatically read from environment variables if the `.env` file doesn't exist. Our current setup in `config.py` already handles this:

```python
import os
from dotenv import load_dotenv

load_dotenv()  # Tries to load .env, falls back to environment variables

class DatabaseConfig:
    HOST = os.getenv("DB_HOST", "localhost")  # Reads from env vars
    PORT = int(os.getenv("DB_PORT", "3306"))
    USER = os.getenv("DB_USER", "root")
    PASSWORD = os.getenv("DB_PASSWORD", "")
    DATABASE = os.getenv("DB_NAME", "jutetransfer")
```

## Alternative Approach: Manual .env File Creation

If you prefer to create the `.env` file manually in your Codespace:

### Method 1: Using the Terminal

```bash
# Navigate to project root
cd /workspaces/juteTransfer

# Create .env file
cat > .env << 'EOF'
DB_HOST=your-host
DB_PORT=3306
DB_USER=your-username
DB_PASSWORD=your-password
DB_NAME=jutetransfer
DB_POOL_SIZE=5
DB_MAX_OVERFLOW=10
EOF

# Verify it's created
cat .env
```

### Method 2: Using VS Code in Browser

1. In the Explorer panel, right-click on the project root
2. Select **New File**
3. Name it `.env`
4. Copy contents from `.env.example` and update with your credentials
5. Save the file

**Note:** The `.env` file is already in `.gitignore`, so it won't be committed to your repository.

## Setting Up Database Access in Codespaces

### Option A: Use a Cloud Database

For Codespaces, it's recommended to use a cloud-hosted MySQL database:

#### 1. **PlanetScale** (Free tier available)
```env
DB_HOST=aws.connect.psdb.cloud
DB_PORT=3306
DB_USER=your-username
DB_PASSWORD=pscale_pw_xxxxxxxxxxxx
DB_NAME=jutetransfer
```

#### 2. **AWS RDS MySQL** (Free tier for 12 months)
```env
DB_HOST=your-db-instance.region.rds.amazonaws.com
DB_PORT=3306
DB_USER=admin
DB_PASSWORD=your-password
DB_NAME=jutetransfer
```

#### 3. **Azure Database for MySQL**
```env
DB_HOST=your-server.mysql.database.azure.com
DB_PORT=3306
DB_USER=your-username@your-server
DB_PASSWORD=your-password
DB_NAME=jutetransfer
```

#### 4. **Google Cloud SQL**
```env
DB_HOST=your-instance-ip
DB_PORT=3306
DB_USER=root
DB_PASSWORD=your-password
DB_NAME=jutetransfer
```

### Option B: Port Forward to Local Database

If you have MySQL running on your local machine:

1. **In Codespace terminal:**
   ```bash
   # Forward local port to Codespace
   # This requires GitHub CLI on your local machine
   ```

2. **On your local machine:**
   ```bash
   gh codespace ports forward 3306:3306
   ```

3. **Use localhost in your .env:**
   ```env
   DB_HOST=localhost
   DB_PORT=3306
   DB_USER=root
   DB_PASSWORD=your-local-password
   DB_NAME=jutetransfer
   ```

### Option C: Docker Container in Codespace

Run MySQL directly in your Codespace using Docker:

```bash
# Start MySQL container
docker run --name jutetransfer-mysql \
  -e MYSQL_ROOT_PASSWORD=rootpassword \
  -e MYSQL_DATABASE=jutetransfer \
  -e MYSQL_USER=juteuser \
  -e MYSQL_PASSWORD=jutepass \
  -p 3306:3306 \
  -d mysql:8.0

# Wait for MySQL to start (about 30 seconds)
sleep 30

# Test connection
docker exec -it jutetransfer-mysql mysql -u juteuser -pjutepass jutetransfer
```

Then use these credentials in your `.env`:
```env
DB_HOST=localhost
DB_PORT=3306
DB_USER=juteuser
DB_PASSWORD=jutepass
DB_NAME=jutetransfer
```

## Quick Setup Script for Codespaces

Create this file to automate environment setup:

<function_calls>
<invoke name="create_file">
<parameter name="content">#!/bin/bash
# setup_codespace_env.sh - Quick environment setup for Codespaces

echo "=================================="
echo "JuteTransfer Environment Setup"
echo "=================================="
echo ""

# Check if .env already exists
if [ -f .env ]; then
    echo "✓ .env file already exists"
    echo ""
    read -p "Do you want to recreate it? (y/N): " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Keeping existing .env file"
        exit 0
    fi
fi

# Check for GitHub Codespaces secrets first
if [ -n "$DB_HOST" ] && [ -n "$DB_PASSWORD" ]; then
    echo "✓ Found environment variables (Codespaces secrets)"
    echo "  No .env file needed - using environment variables"
    echo ""
    echo "Current configuration:"
    echo "  DB_HOST: $DB_HOST"
    echo "  DB_PORT: ${DB_PORT:-3306}"
    echo "  DB_USER: ${DB_USER:-root}"
    echo "  DB_NAME: ${DB_NAME:-jutetransfer}"
    exit 0
fi

# Prompt for database credentials
echo "No environment variables found. Let's create a .env file."
echo ""
echo "Choose your setup option:"
echo "1) Cloud database (PlanetScale, AWS RDS, etc.)"
echo "2) Docker container in Codespace (will start MySQL)"
echo "3) Manual entry (for other setups)"
echo ""
read -p "Enter your choice (1-3): " choice

case $choice in
    1)
        echo ""
        echo "Cloud Database Setup"
        echo "-------------------"
        read -p "Database Host: " db_host
        read -p "Database Port [3306]: " db_port
        db_port=${db_port:-3306}
        read -p "Database User: " db_user
        read -sp "Database Password: " db_password
        echo ""
        read -p "Database Name [jutetransfer]: " db_name
        db_name=${db_name:-jutetransfer}
        ;;
    2)
        echo ""
        echo "Setting up Docker MySQL container..."
        docker run --name jutetransfer-mysql \
          -e MYSQL_ROOT_PASSWORD=rootpassword \
          -e MYSQL_DATABASE=jutetransfer \
          -e MYSQL_USER=juteuser \
          -e MYSQL_PASSWORD=jutepass \
          -p 3306:3306 \
          -d mysql:8.0
        
        echo "Waiting for MySQL to start (30 seconds)..."
        sleep 30
        
        db_host="localhost"
        db_port="3306"
        db_user="juteuser"
        db_password="jutepass"
        db_name="jutetransfer"
        
        echo "✓ MySQL container started"
        ;;
    3)
        echo ""
        echo "Manual Database Setup"
        echo "--------------------"
        read -p "Database Host: " db_host
        read -p "Database Port [3306]: " db_port
        db_port=${db_port:-3306}
        read -p "Database User: " db_user
        read -sp "Database Password: " db_password
        echo ""
        read -p "Database Name [jutetransfer]: " db_name
        db_name=${db_name:-jutetransfer}
        ;;
    *)
        echo "Invalid choice. Exiting."
        exit 1
        ;;
esac

# Create .env file
cat > .env << EOF
# MySQL Database Configuration
DB_HOST=$db_host
DB_PORT=$db_port
DB_USER=$db_user
DB_PASSWORD=$db_password
DB_NAME=$db_name

# Connection Pool Settings
DB_POOL_SIZE=5
DB_MAX_OVERFLOW=10
EOF

echo ""
echo "✓ .env file created successfully"
echo ""

# Test database connection
echo "Testing database connection..."
python3 -c "
from src.jutetransfer.database import DatabaseConnection
success, message = DatabaseConnection.test_connection()
print(message)
exit(0 if success else 1)
" 2>/dev/null

if [ $? -eq 0 ]; then
    echo ""
    echo "✓ Database connection successful!"
    echo ""
    read -p "Do you want to initialize the database now? (Y/n): " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Nn]$ ]]; then
        python init_database.py
    fi
else
    echo ""
    echo "⚠ Could not connect to database. Please check your credentials."
    echo "  You can manually edit the .env file or run this script again."
fi

echo ""
echo "=================================="
echo "Setup complete!"
echo "=================================="
