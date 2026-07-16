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
                result = _process_one(extractor, db, Path(f["path"]), instr, f["name"])
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

def _name_from_filename(fname):
    import re
    stem = Path(fname).stem
    stem = re.sub(r"\s*\(\d+\)\s*$", "", stem)          # drop a trailing " (2)"
    return stem.replace("_", " ").replace("-", " ").strip()

def _process_one(extractor, db, path: Path, instructions="", original_name=""):
    payload = extractor.extract(path, instructions)
    temp_stem = path.stem
    recipes = []
    for raw in payload.get("recipes", []):
        # if the recipe name wasn't found in the sheet, fall back to the ORIGINAL
        # uploaded filename (not the temporary storage name).
        if original_name and raw.get("recipe_name") in (temp_stem, "", None):
            raw["recipe_name"] = _name_from_filename(original_name)
        prev = build_preview(raw, db, payload.get("ocr_uncertainty", 0))
        prev["recipe_id"] = uuid.uuid4().hex[:8]
        prev["source_name"] = path.name
        prev["generated"] = None
        recipes.append(prev)
    # AI validation pass on EVERY recipe (categorize, translate, flag errors/
    # duplicates). Resilient: if the AI is busy, the code result still stands.
    if settings.gemini_enabled() and settings.AI_REVIEW_ENABLED:
        from extraction import review_recipe
        for prev in recipes:
            try:
                _apply_review(prev, review_recipe(prev, prev.get("categories", [])))
            except Exception:
                prev.setdefault("warnings", []).append(
                    "AI validation could not run right now (service busy) — please review manually.")
                prev["needs_review"] = True
    return {"recipes": recipes, "detected": payload.get("detected", len(recipes)),
            "ambiguous_multi": payload.get("ambiguous_multi", False),
            "engine": payload.get("engine", "unknown")}

def _apply_review(prev, rev):
    """Merge the AI review into a recipe preview (code owns quantities/units)."""
    if not isinstance(rev, dict):
        return
    cat = rev.get("category")
    if cat and cat in prev.get("categories", []):
        prev["category"] = cat
    for issue in (rev.get("issues") or []):
        if str(issue).strip():
            prev.setdefault("warnings", []).append("AI: " + str(issue).strip())
    name = (rev.get("recipe_name_en") or "").strip()
    if name:
        prev["recipe_name"] = name
    proc = (rev.get("process_en") or "").strip()
    if proc:
        prev["process"] = proc
    prev["ai_reviewed"] = True
    prev["needs_review"] = prev.get("confidence", 100) < 100 or bool(prev.get("warnings"))
