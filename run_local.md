# Run locally (optional test before deploying)
    cd recipe-converter-app
    pip install -r backend/requirements.txt
    AUTH_MODE=code SHARED_ACCESS_CODE=wynn2027 COOKIE_SECURE=false python backend/server.py
Open http://localhost:8000  and enter the access code.

# No-login quick look
    AUTH_MODE=dev COOKIE_SECURE=false python backend/server.py
