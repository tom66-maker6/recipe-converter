"""
security.py — self-managed authentication (no Microsoft Entra / corporate directory).

You define the users yourself via the APP_USERS environment variable; passwords are
stored only as PBKDF2 hashes (generate them with make_user.py). Sessions are signed,
time-limited tokens kept in an httpOnly + Secure cookie. Uses only the standard
library — nothing to install, nothing for IT to configure.
"""
import base64, hashlib, hmac, json, os, time
from dataclasses import dataclass
import settings

_ITER = 200_000
# Stable secret for signing sessions. Set SESSION_SECRET in the host so sessions
# survive restarts; otherwise a random per-process secret is used (still safe,
# just logs everyone out on restart).
_EPHEMERAL = base64.b64encode(os.urandom(32)).decode()

@dataclass
class User:
    email: str
    name: str
    is_admin: bool

# ---- password hashing ----
def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, _ITER)
    return f"pbkdf2${_ITER}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"

def verify_password(pw: str, stored: str) -> bool:
    try:
        _algo, it, salt_b, hash_b = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(salt_b), int(it))
        return hmac.compare_digest(dk, base64.b64decode(hash_b))
    except Exception:
        return False

# ---- user store (parsed from APP_USERS) ----
def load_users():
    users = {}
    for entry in settings.APP_USERS.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = [p.strip() for p in entry.split("|")]
        email = parts[0].lower()
        users[email] = {"hash": parts[1] if len(parts) > 1 else "",
                        "is_admin": len(parts) > 2 and parts[2].lower() == "admin"}
    return users

# ---- simple in-memory login rate limiting ----
_fails = {}   # key -> [count, first_ts]
_MAX, _WINDOW = 8, 300

def rate_ok(key: str) -> bool:
    c = _fails.get(key)
    if not c:
        return True
    if time.time() - c[1] > _WINDOW:
        _fails.pop(key, None)
        return True
    return c[0] < _MAX

def record_fail(key: str):
    c = _fails.get(key)
    if not c or time.time() - c[1] > _WINDOW:
        _fails[key] = [1, time.time()]
    else:
        c[0] += 1

def reset_fail(key: str):
    _fails.pop(key, None)

# ---- authentication ----
def authenticate(username: str, password: str):
    username = (username or "").strip()
    if settings.AUTH_MODE == "code":                    # one shared team code
        if settings.ACCESS_CODE and hmac.compare_digest(password or "", settings.ACCESS_CODE):
            name = username or "Team member"
            email = username.lower() if "@" in username else (username.lower() or "guest") + "@team"
            return User(email=email, name=name, is_admin=email in settings.ADMIN_USERS)
        return None
    u = load_users().get(username.lower())              # per-user accounts (recommended)
    if u and u["hash"] and verify_password(password or "", u["hash"]):
        return User(email=username.lower(), name=username.split("@")[0], is_admin=u["is_admin"])
    return None

# ---- signed session cookie ----
def _secret() -> bytes:
    return (settings.SESSION_SECRET or _EPHEMERAL).encode()

def make_session(user: User) -> str:
    payload = {"email": user.email, "name": user.name, "admin": user.is_admin,
               "exp": int(time.time()) + settings.SESSION_HOURS * 3600}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    sig = hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"

def read_session(token: str):
    try:
        raw, sig = token.split(".")
        if not hmac.compare_digest(sig, hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()):
            return None
        p = json.loads(base64.urlsafe_b64decode(raw))
        if p["exp"] < time.time():
            return None
        return User(email=p["email"], name=p["name"], is_admin=p["admin"])
    except Exception:
        return None
