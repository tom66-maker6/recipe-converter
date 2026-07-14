"""
jobs.py — batch/job state + a SINGLE-worker sequential processing queue.

Files can be uploaded together, but they are processed ONE AT A TIME (a FIFO
queue on a single worker thread) so extraction stays reliable regardless of batch
size. Each file is independent and carries its own recipes, status and warnings.
"""
import queue as _queue, threading, uuid
from pathlib import Path
import settings, store
from extraction import get_extractor, NeedsOCR, ExtractionError
from pipeline import build_preview

class JobStore:
    def __init__(self):
        self.batches = {}          # batch_id -> {user_email, files:{file_id:{...}}}
        self.generated = {}        # token -> {path, recipe_name, batch_id}

    def new_batch(self, user, instructions=""):
        bid = uuid.uuid4().hex[:12]
        self.batches[bid] = {"user_email": user.email, "files": {}, "instructions": instructions}
        return bid

    def add_file(self, bid, filename, path):
        fid = uuid.uuid4().hex[:12]
        self.batches[bid]["files"][fid] = {
            "file_id": fid, "name": filename, "path": str(path),
            "status": "queued", "detected": None, "ambiguous_multi": False,
            "engine": None, "recipes": [], "error": None}
        return fid

    def file(self, bid, fid):
        return self.batches[bid]["files"][fid]

    def public(self, bid):
        b = self.batches[bid]
        files = []
        for f in b["files"].values():
            files.append({k: f[k] for k in
                          ("file_id", "name", "status", "detected", "ambiguous_multi", "engine", "error")}
                         | {"recipes": f["recipes"]})
        return {"batch_id": bid, "files": files}

JOBS = JobStore()
_Q = _queue.Queue()

def enqueue(bid, fid):
    _Q.put((bid, fid))

def _worker():
    extractor = get_extractor()
    while True:
        bid, fid = _Q.get()
        try:
            f = JOBS.file(bid, fid)
            f["status"] = "processing"
            try:
                db = store.load_db()          # pick up newly approved global mappings
                instr = JOBS.batches[bid].get("instructions", "")
                result = _process_one(extractor, db, Path(f["path"]), instr)
                f.update(engine=result["engine"], detected=result["detected"],
                         ambiguous_multi=result["ambiguous_multi"], recipes=result["recipes"])
                if result["ambiguous_multi"]:
                    f["status"] = "needs_review"
                elif not result["recipes"]:
                    f["status"] = "error"; f["error"] = "No recipe could be extracted."
                else:
                    f["status"] = "needs_review" if any(r["needs_review"] for r in result["recipes"]) else "ready"
            except NeedsOCR as e:
                f["status"] = "error"; f["error"] = f"Needs OCR (configure Document Intelligence): {e}"
            except ExtractionError as e:
                f["status"] = "error"; f["error"] = str(e)
            except Exception as e:
                f["status"] = "error"; f["error"] = f"Unexpected error: {e}"
            finally:
                if settings.DELETE_SOURCE_AFTER_PROCESSING:
                    try: Path(f["path"]).unlink(missing_ok=True)
                    except Exception: pass
        finally:
            _Q.task_done()

def start_worker():
    threading.Thread(target=_worker, daemon=True).start()

def _process_one(extractor, db, path: Path, instructions=""):
    payload = extractor.extract(path, instructions)
    recipes = []
    for raw in payload.get("recipes", []):
        prev = build_preview(raw, db, payload.get("ocr_uncertainty", 0))
        prev["recipe_id"] = uuid.uuid4().hex[:8]
        prev["source_name"] = path.name
        prev["generated"] = None
        recipes.append(prev)
    return {"recipes": recipes, "detected": payload.get("detected", len(recipes)),
            "ambiguous_multi": payload.get("ambiguous_multi", False),
            "engine": payload.get("engine", "unknown")}
