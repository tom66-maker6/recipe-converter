"""
auth.py — identity resolution (framework-free).

The signed-in user comes from a signed session cookie (see security.py). No
corporate directory. Used by the HTTP server to gate requests.
"""
import settings
from security import read_session, User

COOKIE = "rc_session"

def resolve_user(cookies: dict):
    """Return a User or None from the request cookies."""
    if settings.AUTH_MODE == "dev":                       # local testing only
        email = settings.DEV_USER
        return User(email=email, name=email.split("@")[0], is_admin=email.lower() in settings.ADMIN_USERS)
    token = cookies.get(COOKIE)
    return read_session(token) if token else None
