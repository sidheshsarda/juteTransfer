# Setting Up Environment Variables in GitHub Codespaces

## 🎯 Quick Answer

When working in GitHub Codespaces or browser-based workspaces, store your `.env` variables in one of these locations:

1. **GitHub Codespaces Secrets** (Recommended) - Encrypted and secure
2. **Manually create `.env` file** in the Codespace (already in .gitignore)
3. **Use our setup script** - `./setup_codespace_env.sh`

## 📋 Step-by-Step Guide

### Method 1: GitHub Codespaces Secrets (Recommended)

#### For This Repository Only (Repository Secrets)

1. **Go to your repository on GitHub**
   - Navigate to: `https://github.com/sidheshsarda/juteTransfer`

2. **Click on "Settings"** (top menu)

3. **Navigate to Secrets**
   - In the left sidebar, click **"Secrets and variables"**
   - Then click **"Codespaces"**

4. **Add Repository Secret**
   - Click the green **"New repository secret"** button
   
5. **Add each secret one by one:**

   | Name | Value (Example) |
   |------|-----------------|
   | `DB_HOST` | `your-database-host.com` |
   | `DB_PORT` | `3306` |
   | `DB_USER` | `your_username` |
   | `DB_PASSWORD` | `your_secure_password` |
   | `DB_NAME` | `jutetransfer` |
   | `DB_POOL_SIZE` | `5` |
   | `DB_MAX_OVERFLOW` | `10` |

6. **That's it!** 🎉
   - These will automatically be available as environment variables in your Codespace
   - No `.env` file needed
   - Never committed to your repository

#### For All Your Codespaces (User Secrets)

If you want these secrets available across multiple repositories:

1. **Go to your GitHub profile settings**
   - Click your profile picture → **Settings**

2. **Navigate to Codespaces**
   - In the left sidebar, scroll down to **"Codespaces"**

3. **Click "New secret"**

4. **Add secrets** (same as above)

5. **Select repository access**
   - Choose which repositories can use these secrets
   - Or select "All repositories"

### Method 2: Use Our Automated Setup Script

The easiest way! Just run this in your Codespace terminal:

```bash
./setup_codespace_env.sh
```

This interactive script will:
- ✅ Check for existing environment variables
- ✅ Offer to set up Docker MySQL container
- ✅ Guide you through manual configuration
- ✅ Test the database connection
- ✅ Optionally initialize the database

**Example Session:**
```
==================================
JuteTransfer Environment Setup
==================================

No environment variables found. Let's create a .env file.

Choose your setup option:
1) Cloud database (PlanetScale, AWS RDS, etc.)
2) Docker container in Codespace (will start MySQL)
3) Manual entry (for other setups)

Enter your choice (1-3): 2

Setting up Docker MySQL container...
Waiting for MySQL to start (30 seconds)...
✓ MySQL container started
✓ .env file created successfully

Testing database connection...
Database connection successful

Do you want to initialize the database now? (Y/n): y
...
```

### Method 3: Manual .env File Creation

#### Using Terminal (Fastest)

```bash
cd /workspaces/juteTransfer

cat > .env << 'EOF'
DB_HOST=your-host
DB_PORT=3306
DB_USER=your-username
DB_PASSWORD=your-password
DB_NAME=jutetransfer
DB_POOL_SIZE=5
DB_MAX_OVERFLOW=10
EOF
```

#### Using VS Code UI in Browser

1. Open VS Code in your Codespace
2. In the **Explorer** panel (left sidebar), right-click on the project root folder
3. Select **"New File"**
4. Name it `.env`
5. Copy the content from [.env.example](.env.example) and update with your values
6. Press `Ctrl+S` (or `Cmd+S` on Mac) to save

**Important:** The `.env` file is already in `.gitignore`, so it won't be committed!

## 🗄️ Database Options for Codespaces

### Option A: Cloud Database (Recommended)

Use a managed database service:

#### PlanetScale (Free Tier Available)
- Website: https://planetscale.com
- Free tier: 5GB storage, 1 billion row reads/month
- No credit card required for free tier

```env
DB_HOST=aws.connect.psdb.cloud
DB_PORT=3306
DB_USER=your-username
DB_PASSWORD=pscale_pw_xxxxxxxxxxxx
DB_NAME=jutetransfer
```

#### Clever Cloud (Free Tier Available)
- Website: https://www.clever-cloud.com
- Free MySQL addon available

#### Railway (Free Trial Available)
- Website: https://railway.app
- Easy MySQL deployment

### Option B: Docker Container in Codespace

Run MySQL directly in your Codespace:

```bash
# Start MySQL container
docker run --name jutetransfer-mysql \
  -e MYSQL_ROOT_PASSWORD=rootpassword \
  -e MYSQL_DATABASE=jutetransfer \
  -e MYSQL_USER=juteuser \
  -e MYSQL_PASSWORD=jutepass \
  -p 3306:3306 \
  -d mysql:8.0

# Wait 30 seconds for startup
sleep 30

# Use these credentials in your .env
DB_HOST=localhost
DB_PORT=3306
DB_USER=juteuser
DB_PASSWORD=jutepass
DB_NAME=jutetransfer
```

**Pros:**
- ✅ No external service needed
- ✅ Free
- ✅ Fast setup

**Cons:**
- ❌ Data is lost when Codespace stops
- ❌ Uses Codespace resources

### Option C: Port Forward to Local Database

If you have MySQL on your local machine:

**On your local machine:**
```bash
# Install GitHub CLI if needed
# Then forward port
gh codespace ports forward 3306:3306 -c <codespace-name>
```

**In your Codespace .env:**
```env
DB_HOST=localhost
DB_PORT=3306
DB_USER=root
DB_PASSWORD=your-local-password
DB_NAME=jutetransfer
```

## 🔒 Security Best Practices

### ✅ DO:
- Use GitHub Codespaces Secrets for sensitive data
- Create `.env` files manually in Codespace (already in `.gitignore`)
- Use strong, unique passwords
- Use cloud databases with SSL/TLS
- Rotate passwords regularly

### ❌ DON'T:
- Never commit `.env` files to git
- Never share your `.env` file in chat, email, or screenshots
- Don't use production database credentials in development
- Don't store passwords in code or commit messages

## 🧪 Testing Your Setup

After setting up your environment, test the connection:

```bash
# Test database connection
python -c "
from src.jutetransfer.database import DatabaseConnection
success, message = DatabaseConnection.test_connection()
print(message)
"

# Initialize database (if not done yet)
python init_database.py

# Run the example script
python example_database_usage.py
```

## 🐛 Troubleshooting

### "Database connection failed"

**Check 1: Are environment variables set?**
```bash
echo $DB_HOST
echo $DB_USER
echo $DB_NAME
```

**Check 2: Is .env file present?**
```bash
cat .env
```

**Check 3: Can you reach the database?**
```bash
# For cloud databases
ping $DB_HOST

# For Docker container
docker ps | grep mysql
docker logs jutetransfer-mysql
```

### "Module not found" errors

```bash
# Reinstall dependencies
pip install -e .
```

### Docker container won't start in Codespace

```bash
# Check if Docker is running
docker ps

# Check Docker logs
docker logs jutetransfer-mysql

# Remove and recreate
docker rm -f jutetransfer-mysql
./setup_codespace_env.sh
```

## 📚 Additional Resources

- [GitHub Codespaces Documentation](https://docs.github.com/en/codespaces)
- [Managing Secrets for Codespaces](https://docs.github.com/en/codespaces/managing-your-codespaces/managing-encrypted-secrets-for-your-codespaces)
- [DATABASE_SETUP.md](DATABASE_SETUP.md) - Complete database setup guide
- [PlanetScale Documentation](https://docs.planetscale.com/)

## 🆘 Need Help?

If you're still stuck:
1. Check the [DATABASE_SETUP.md](DATABASE_SETUP.md) guide
2. Run `./setup_codespace_env.sh` for interactive setup
3. Check the example: `python example_database_usage.py`
4. Open an issue on GitHub
