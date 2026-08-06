# Alembic Database Migrations

এই ডিরেক্টরিটি database schema পরিবর্তন track করে।

## First-time setup (existing database)

আপনার database-এ ইতিমধ্যে সব table আছে (v2 + v3 migration scripts চালানোর পর)।
Alembic-কে বলুন সেটা current state হিসেবে ধরতে:

```bash
cd telegram-bot
pip install alembic
alembic stamp head
```

## নতুন schema change যোগ করার সময়

1. `database/models.py`-এ নতুন column/table যোগ করুন
2. Auto-generate migration:
   ```bash
   alembic revision --autogenerate -m "add xyz column"
   ```
3. `alembic/versions/`-এ নতুন ফাইল check করুন — ঠিক আছে কিনা
4. Apply:
   ```bash
   alembic upgrade head
   ```

## Rollback

```bash
alembic downgrade -1        # এক ধাপ পেছনে
alembic downgrade base      # সম্পূর্ণ ফাঁকা
```

## History দেখা

```bash
alembic current
alembic history
```
