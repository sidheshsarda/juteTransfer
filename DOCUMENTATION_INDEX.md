# 📚 Documentation Index

Welcome to JuteTransfer! This guide will help you find the right documentation for your needs.

## 🚀 Quick Links by Scenario

### "I just opened this in Codespaces, what do I do?"
→ **Start here:** [QUICKSTART_ENV.md](QUICKSTART_ENV.md) (2 min read)

### "Where do I put my database credentials in Codespaces?"
→ **Read this:** [ENV_STORAGE_FAQ.md](ENV_STORAGE_FAQ.md) (comprehensive answer)

### "Show me exactly where with pictures/diagrams"
→ **Visual guide:** [ENV_VISUAL_GUIDE.md](ENV_VISUAL_GUIDE.md) (diagrams included)

### "I need complete step-by-step instructions for Codespaces"
→ **Full guide:** [CODESPACES_ENV_SETUP.md](CODESPACES_ENV_SETUP.md) (detailed)

### "Tell me about the database setup and schema"
→ **Database docs:** [DATABASE_SETUP.md](DATABASE_SETUP.md) (technical details)

### "Just give me the basic project info"
→ **Main readme:** [README.md](README.md) (project overview)

---

## 📖 Documentation Files

| File | What It Covers | When to Use | Time |
|------|----------------|-------------|------|
| [QUICKSTART_ENV.md](QUICKSTART_ENV.md) | 3 ways to set up env vars | First time setup | 2 min |
| [ENV_STORAGE_FAQ.md](ENV_STORAGE_FAQ.md) | Complete FAQ about .env storage | Comprehensive answer | 5 min |
| [ENV_VISUAL_GUIDE.md](ENV_VISUAL_GUIDE.md) | Diagrams and visual explanations | Visual learner | 5 min |
| [CODESPACES_ENV_SETUP.md](CODESPACES_ENV_SETUP.md) | Full Codespaces guide | Detailed setup | 10 min |
| [DATABASE_SETUP.md](DATABASE_SETUP.md) | Database configuration & schema | Understanding DB | 10 min |
| [README.md](README.md) | Project overview & features | General info | 5 min |

---

## 🎯 By Skill Level

### Beginner (Never used Codespaces before)
1. [QUICKSTART_ENV.md](QUICKSTART_ENV.md) - Quick start
2. [ENV_VISUAL_GUIDE.md](ENV_VISUAL_GUIDE.md) - See where everything goes
3. Run: `./setup_codespace_env.sh` - Let script do the work

### Intermediate (Know Codespaces, need specifics)
1. [CODESPACES_ENV_SETUP.md](CODESPACES_ENV_SETUP.md) - Detailed options
2. [DATABASE_SETUP.md](DATABASE_SETUP.md) - Database details
3. Choose your preferred method and implement

### Advanced (Want to customize everything)
1. [DATABASE_SETUP.md](DATABASE_SETUP.md) - Full schema & config
2. `src/jutetransfer/config.py` - Configuration code
3. `src/jutetransfer/database.py` - Database utilities

---

## 🔧 By Task

### Task: "Set up my environment in Codespaces"
```bash
# Option 1: Automated (recommended)
./setup_codespace_env.sh

# Option 2: Read docs first
cat QUICKSTART_ENV.md
```

**Docs:** [QUICKSTART_ENV.md](QUICKSTART_ENV.md), [ENV_VISUAL_GUIDE.md](ENV_VISUAL_GUIDE.md)

---

### Task: "Use GitHub Secrets for my team"
**Steps:**
1. Read: [CODESPACES_ENV_SETUP.md](CODESPACES_ENV_SETUP.md) → Method 1
2. Go to: GitHub → Settings → Codespaces → Add secrets
3. Add: `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`

---

### Task: "Connect to an external database"
**Options:**
- PlanetScale, AWS RDS, Azure MySQL, etc.
- Read: [DATABASE_SETUP.md](DATABASE_SETUP.md) → "Option A: Cloud Database"

**Quick setup:**
```bash
cat > .env << 'EOF'
DB_HOST=your-cloud-db-host.com
DB_PORT=3306
DB_USER=your-username
DB_PASSWORD=your-password
DB_NAME=jutetransfer
EOF
```

---

### Task: "Run database locally in Codespace"
**Solution:** Docker MySQL container

```bash
./setup_codespace_env.sh
# Choose option 2
```

**Docs:** [CODESPACES_ENV_SETUP.md](CODESPACES_ENV_SETUP.md) → "Option B: Docker Container"

---

### Task: "Initialize the database tables"
```bash
# After environment is configured:
python init_database.py
```

**Docs:** [DATABASE_SETUP.md](DATABASE_SETUP.md) → "Database Schema"

---

### Task: "Run the application"
```bash
streamlit run app.py
```

**Docs:** [README.md](README.md) → "Usage"

---

## 🆘 Troubleshooting

### "Can't connect to database"
1. Check environment variables: `cat .env` or `echo $DB_HOST`
2. Test connection: `python example_database_usage.py`
3. Read: [ENV_STORAGE_FAQ.md](ENV_STORAGE_FAQ.md) → "Troubleshooting"

### "Don't see .env file"
- It's hidden! Try: `ls -la .env`
- Read: [ENV_VISUAL_GUIDE.md](ENV_VISUAL_GUIDE.md) → "Common Locations"

### "GitHub Secrets not working"
- Restart Codespace after adding secrets
- Check names match exactly (case-sensitive)
- Read: [ENV_STORAGE_FAQ.md](ENV_STORAGE_FAQ.md) → "GitHub Secrets not working"

---

## 🎓 Learning Path

### Day 1: Get it running
1. Open in Codespaces
2. Run `./setup_codespace_env.sh`
3. Choose option 2 (Docker MySQL)
4. Run `python init_database.py`
5. Run `streamlit run app.py`

**Time:** 5 minutes  
**Docs:** [QUICKSTART_ENV.md](QUICKSTART_ENV.md)

---

### Day 2: Understand the setup
1. Read about environment variables
2. Explore database schema
3. Try different connection methods

**Time:** 30 minutes  
**Docs:** [ENV_STORAGE_FAQ.md](ENV_STORAGE_FAQ.md), [DATABASE_SETUP.md](DATABASE_SETUP.md)

---

### Day 3: Set up for team
1. Configure GitHub Secrets
2. Set up cloud database
3. Share with team

**Time:** 1 hour  
**Docs:** [CODESPACES_ENV_SETUP.md](CODESPACES_ENV_SETUP.md)

---

## 📝 Quick Command Reference

```bash
# Setup
./setup_codespace_env.sh              # Interactive setup
python init_database.py               # Create tables
python example_database_usage.py      # Test database

# Run
streamlit run app.py                  # Start the app

# Test
python -c "from src.jutetransfer.database import DatabaseConnection; print(DatabaseConnection.test_connection()[1])"

# Check environment
cat .env                              # View .env file
echo $DB_HOST                         # View env variable
ls -la .env                           # Check if .env exists
```

---

## 🌟 Most Important Files

### For Getting Started:
1. **[QUICKSTART_ENV.md](QUICKSTART_ENV.md)** ⭐⭐⭐⭐⭐
2. **`./setup_codespace_env.sh`** ⭐⭐⭐⭐⭐

### For Understanding:
3. **[ENV_STORAGE_FAQ.md](ENV_STORAGE_FAQ.md)** ⭐⭐⭐⭐
4. **[DATABASE_SETUP.md](DATABASE_SETUP.md)** ⭐⭐⭐⭐

### For Reference:
5. **[CODESPACES_ENV_SETUP.md](CODESPACES_ENV_SETUP.md)** ⭐⭐⭐
6. **[README.md](README.md)** ⭐⭐⭐

---

## 💡 Pro Tips

1. **Start with the automated script** - saves time!
   ```bash
   ./setup_codespace_env.sh
   ```

2. **Use GitHub Secrets for team projects** - one setup, everyone benefits

3. **Use Docker MySQL for development** - no external dependencies

4. **Read QUICKSTART_ENV.md first** - it's only 2 minutes

5. **Bookmark ENV_STORAGE_FAQ.md** - answers all environment questions

---

## 🔗 External Resources

- [GitHub Codespaces Docs](https://docs.github.com/en/codespaces)
- [Streamlit Documentation](https://docs.streamlit.io/)
- [SQLAlchemy Docs](https://docs.sqlalchemy.org/)
- [MySQL Documentation](https://dev.mysql.com/doc/)

---

## ✅ Success Checklist

After setup, you should be able to:

- [ ] See database connection success message
- [ ] Run `python init_database.py` without errors
- [ ] Run `streamlit run app.py` and see the app
- [ ] Log in with demo credentials
- [ ] See data in the AgGrid table

If all checked: **You're ready to develop! 🎉**

---

**Still stuck?** Open an issue on GitHub with:
- Which documentation you read
- What command you ran
- What error you got
- Your environment (Codespaces/local/other)
