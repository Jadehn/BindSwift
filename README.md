# Bindswift

Fast General Liability quotes and instant certificates of insurance for NYC small and mid-size businesses.
Built by Howard Analytics (NY P&C License #1931665).

- `index.html`: the customer site (home, instant-quote check, appetite page, quote form, login, dashboard).
  Opened without the backend it runs as a demo with sample data.
- `backend/`: FastAPI + PostgreSQL. Admin panel, ACORD 125/126 filling, quotes, binding, the $50 fee,
  certificates, contractor accounts. Full setup, deployment and security notes: [`backend/README.md`](backend/README.md).
- `api/index.py` + `vercel.json`: Vercel entry point.

## Quick start

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
createdb bindswift && psql bindswift -f schema.sql
cp .env.example .env            # fill in; ENV=development for local
python -m scripts.create_admin you@howardanalytics.net
uvicorn app.main:app --reload   # site: http://localhost:8000  admin: http://localhost:8000/admin
```

Never commit `.env`. Secrets go in your host's environment variables.
