# 🚀 Quick Start: Environment Setup in Codespaces

## TL;DR - Three Ways to Set Environment Variables

### 1️⃣ Automated Script (Easiest)
```bash
./setup_codespace_env.sh
```
Choose option 2 for instant Docker MySQL setup!

### 2️⃣ GitHub Secrets (Most Secure)
1. Go to: **Repository → Settings → Secrets and variables → Codespaces**
2. Add secrets: `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`
3. Done! No .env file needed

### 3️⃣ Manual .env File
```bash
cat > .env << 'EOF'
DB_HOST=localhost
DB_PORT=3306
DB_USER=root
DB_PASSWORD=your-password
DB_NAME=jutetransfer
EOF
```

---

## 📦 Complete Setup in 3 Commands

```bash
# 1. Run setup script
./setup_codespace_env.sh

# 2. Initialize database
python init_database.py

# 3. Test it
python example_database_usage.py
```

---

## 🐳 Quick Docker MySQL Setup

```bash
docker run --name jutetransfer-mysql \
  -e MYSQL_ROOT_PASSWORD=rootpassword \
  -e MYSQL_DATABASE=jutetransfer \
  -e MYSQL_USER=juteuser \
  -e MYSQL_PASSWORD=jutepass \
  -p 3306:3306 -d mysql:8.0 && sleep 30
```

Then create `.env`:
```bash
cat > .env << 'EOF'
DB_HOST=localhost
DB_PORT=3306
DB_USER=juteuser
DB_PASSWORD=jutepass
DB_NAME=jutetransfer
EOF
```

---

## ✅ Verify Everything Works

```bash
python -c "from src.jutetransfer.database import DatabaseConnection; print(DatabaseConnection.test_connection()[1])"
```

Should output: `Database connection successful`

---

## 📖 Full Documentation

- [CODESPACES_ENV_SETUP.md](CODESPACES_ENV_SETUP.md) - Complete guide
- [DATABASE_SETUP.md](DATABASE_SETUP.md) - Database details
- [README.md](README.md) - Project overview
