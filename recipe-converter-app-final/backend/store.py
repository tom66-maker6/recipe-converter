"""
store.py — shared ingredient database governance + audit trail.

• Shared DB is one file used by all authorized users.
• Normal users PROPOSE corrections; only an admin/reviewer APPROVES a global
  mapping. Approved mappings are reversible and every change is kept in history.
• Audit records who generated which recipe and when — never the source document.
"""
import json, sys, datetime, threading
import settings

sys.path.insert(0, str(settings.CORE_DIR))
from ingredient_db import IngredientDB, _key      # noqa: E402

_LOCK = threading.Lock()

def _now():
    return datetime.datetime.now().replace(microsecond=0).isoformat()

def _append(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

# ---- shared ingredient DB (seeded once from the tested core seed) ----
def _ensure_shared_db():
    if not settings.SHARED_DB.exists():
        seed = json.load(open(settings.CORE_DIR / "ingredient_db.json", encoding="utf-8"))
        json.dump(seed, open(settings.SHARED_DB, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

def load_db() -> IngredientDB:
    _ensure_shared_db()
    return IngredientDB(str(settings.SHARED_DB))

# ---- correction governance ----
_pending = {}     # id -> {raw_name, canonical, proposed_by, at}
_pid = [0]

def propose_correction(user, raw_name, canonical):
    with _LOCK:
        _pid[0] += 1
        pid = str(_pid[0])
        _pending[pid] = {"id": pid, "raw_name": raw_name, "canonical": canonical,
                         "proposed_by": user.email, "at": _now(), "status": "pending"}
        _append(settings.DB_HISTORY, {"action": "propose", "by": user.email, "at": _now(),
                                      "raw_name": raw_name, "canonical": canonical})
        return _pending[pid]

def list_pending():
    return [p for p in _pending.values() if p["status"] == "pending"]

def approve_correction(admin, pid):
    with _LOCK:
        p = _pending.get(pid)
        if not p or p["status"] != "pending":
            raise KeyError("proposal not found")
        db = load_db()
        before = db.aliases.get(_key(p["raw_name"]))
        db.add_correction(p["raw_name"], p["canonical"])     # persists to shared DB
        p["status"] = "approved"; p["approved_by"] = admin.email; p["approved_at"] = _now()
        _append(settings.DB_HISTORY, {"action": "approve", "by": admin.email, "at": _now(),
                                      "raw_name": p["raw_name"], "canonical": p["canonical"],
                                      "previous": before})
        return p

def reverse_mapping(admin, raw_name):
    """Remove/undo a global mapping (reversible per spec)."""
    with _LOCK:
        db = load_db()
        k = _key(raw_name)
        removed = db.aliases.pop(k, None)
        if removed is not None:
            db._save()
        _append(settings.DB_HISTORY, {"action": "reverse", "by": admin.email, "at": _now(),
                                      "raw_name": raw_name, "removed": removed})
        return {"raw_name": raw_name, "removed": removed}

def list_mappings():
    db = load_db()
    return dict(sorted(db.aliases.items()))

def history(limit=200):
    if not settings.DB_HISTORY.exists():
        return []
    lines = settings.DB_HISTORY.read_text(encoding="utf-8").splitlines()[-limit:]
    return [json.loads(l) for l in lines]

# ---- audit (who generated what, when — NOT the source content) ----
def record_generation(user, recipe_name, confidence, source_name):
    _append(settings.AUDIT_LOG, {
        "at": _now(), "user": user.email, "action": "generate",
        "recipe": recipe_name, "confidence": confidence,
        "source_name": source_name,           # filename only, source bytes not retained
    })

def audit(limit=500):
    if not settings.AUDIT_LOG.exists():
        return []
    lines = settings.AUDIT_LOG.read_text(encoding="utf-8").splitlines()[-limit:]
    return [json.loads(l) for l in lines]
