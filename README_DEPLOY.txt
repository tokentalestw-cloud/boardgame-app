
Boardgame App - Free Persistent Deployment (Render + Supabase)

Files:
- app.py : deploy entrypoint
- requirements.txt : Python dependencies

Recommended free setup:
1. Create a free Supabase project.
2. In Supabase Dashboard, open Connect and copy the Postgres connection string.
3. In Render, create a new Web Service from this repo.
4. Set Build Command to:
   pip install -r requirements.txt
5. Set Start Command to:
   uvicorn app:app --host 0.0.0.0 --port $PORT
6. In Render Environment Variables, add:
   DATABASE_URL = <your Supabase Postgres connection string>
   DATA_DIR = /tmp/boardgame-data

Notes:
- With DATABASE_URL set to a postgres://... URL, the app uses PostgreSQL.
- Uploaded images are stored on local disk. On Render free web services, local disk is ephemeral.
  If you need images to persist too, move image storage to Supabase Storage or Cloudinary later.
- Database records themselves persist in Supabase.
