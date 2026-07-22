"""
server.py — Recipe Converter web server (Python standard library only).

No web framework to install. Serves the frontend, handles self-managed login,
runs uploads through the single sequential worker, and streams generated Excel
files. Business logic (extraction, normalize, generate, governance, security) is
unchanged.

Run:  python server.py     (honours $PORT, default 8000)
"""
import io, json, os, uuid, zipfile, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from pathlib import Path

import settings, store, jobs
from pipeline import generate_xlsx, generate_combined_xlsx
from security import authenticate, make_session, rate_ok, record_fail, reset_fail
from auth import resolve_user, COOKIE

ALLOWED_EXT = {".xlsx", ".xls", ".pdf", ".docx", ".jpg", ".jpeg", ".png", ".heic"}
STATIC = {"/styles.css": "text/css", "/app.js": "application/javascript",
          "/login.html": "text/html; charset=utf-8", "/logo.png": "image/png"}
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

def _parse_target_grams(raw):
    """Interpret the dedicated weight field. Empty/absent -> None (use the app
    default of 1000 g). 0 -> keep the original quantities (no rescale). Any other
    number is clamped to a sane 1 g – 1000 kg range."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        g = int(round(float(str(raw).replace(",", ".").strip())))
    except (TypeError, ValueError):
        return None
    if g <= 0:
        return 0
    return max(1, min(g, 1_000_000))

def _cleanup_generated():
    cutoff = datetime.datetime.now().timestamp() - settings.GENERATED_RETENTION_MINUTES * 60
    for p in settings.OUTPUT_DIR.glob("*.xlsx"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except Exception:
            pass


class Handler(BaseHTTPRequestHandler):
    server_version = "RecipeConverter/1.0"

    # ---------- low-level helpers ----------
    def _send(self, status, content_type, body: bytes, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status=200, extra=None):
        self._send(status, "application/json", json.dumps(obj).encode("utf-8"), extra)

    def _err(self, status, detail):
        self._json({"detail": detail}, status)

    def _static(self, name, content_type):
        path = settings.FRONTEND_DIR / name.lstrip("/")
        if not path.exists():
            return self._err(404, "Not found")
        self._send(200, content_type, path.read_bytes())

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _cookies(self):
        raw = self.headers.get("Cookie", "")
        out = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(n) if n else b""

    def _json_body(self):
        try:
            return json.loads(self._body() or b"{}")
        except Exception:
            return {}

    def _user(self):
        return resolve_user(self._cookies())

    def _multipart(self):
        """Return (files, fields) from a multipart/form-data body."""
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype or "boundary=" not in ctype:
            return [], {}
        boundary = ctype.split("boundary=", 1)[1].strip().strip('"').encode()
        body = self._body()
        files, fields = [], {}
        for part in body.split(b"--" + boundary):
            if b"Content-Disposition" not in part:
                continue
            head, _, content = part.partition(b"\r\n\r\n")
            content = content.rstrip(b"\r\n")
            if b'filename="' in head:
                fname = head.split(b'filename="', 1)[1].split(b'"', 1)[0].decode("utf-8", "ignore")
                if fname:
                    files.append((fname, content))
            elif b'name="' in head:
                fname = head.split(b'name="', 1)[1].split(b'"', 1)[0].decode("utf-8", "ignore")
                fields[fname] = content.decode("utf-8", "ignore")
        return files, fields

    # ---------- routing ----------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            # Don't even show the app UI without the access code.
            if settings.AUTH_MODE != "dev" and not self._user():
                return self._redirect("/login")
            return self._static("index.html", "text/html; charset=utf-8")
        if path == "/login":
            return self._static("login.html", "text/html; charset=utf-8")
        if path in STATIC:
            return self._static(path, STATIC[path])
        if path == "/api/version":
            # PUBLIC on purpose: lets you confirm which build is live after a deploy.
            return self._json({"version": settings.VERSION, "note": settings.VERSION_NOTE})
        if path == "/api/config":
            return self._json({"auth_mode": settings.AUTH_MODE, "version": settings.VERSION})
        if path == "/api/me":
            u = self._user()
            return self._json({"email": u.email, "name": u.name, "is_admin": u.is_admin}) if u \
                else self._err(401, "Not signed in.")

        if not self._user():
            return self._err(401, "Not signed in.")
        if path.startswith("/api/batch/") and path.endswith("/download-all"):
            return self._download_all(path.split("/")[3])
        if path.startswith("/api/batch/"):
            return self._batch(path.split("/")[3])
        if path.startswith("/api/download/"):
            return self._download(path.split("/")[3])
        if path == "/api/ingredients/mappings":
            return self._json(store.list_mappings())
        if path == "/api/ingredients/history":
            return self._json(store.history())
        if path == "/api/ingredients/pending":
            return self._admin_json(lambda a: store.list_pending())
        if path == "/api/audit":
            return self._admin_json(lambda a: store.audit())
        return self._err(404, "Not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/login":
            return self._login()
        if path == "/api/logout":
            return self._json({"ok": True}, extra=[("Set-Cookie", f"{COOKIE}=; Path=/; Max-Age=0")])
        if not self._user():
            return self._err(401, "Not signed in.")
        if path == "/api/upload":
            return self._upload()
        if path == "/api/generate":
            return self._generate()
        if path == "/api/generate-combined":
            return self._generate_combined()
        if path == "/api/ingredients/propose":
            b = self._json_body()
            if not b.get("raw_name") or not b.get("canonical"):
                return self._err(400, "raw_name and canonical required.")
            return self._json(store.propose_correction(self._user(), b["raw_name"].strip(), b["canonical"].strip()))
        if path == "/api/ingredients/approve":
            return self._admin_json(lambda a: store.approve_correction(a, self._json_body().get("id")))
        if path == "/api/ingredients/reverse":
            return self._admin_json(lambda a: store.reverse_mapping(a, self._json_body().get("raw_name", "")))
        return self._err(404, "Not found")

    # ---------- endpoint impls ----------
    def _login(self):
        b = self._json_body()
        username = (b.get("username") or "").strip()
        key = self.client_address[0] + "|" + username.lower()
        if not rate_ok(key):
            return self._err(429, "Too many attempts. Please wait a few minutes.")
        user = authenticate(username, b.get("password") or "")
        if not user:
            record_fail(key)
            return self._err(401, "Incorrect credentials.")
        reset_fail(key)
        # Session cookie (no Max-Age) -> cleared when the browser is closed.
        flags = f"{COOKIE}={make_session(user)}; HttpOnly; Path=/; SameSite=Lax"
        if settings.COOKIE_SECURE:
            flags += "; Secure"
        self._json({"ok": True, "name": user.name, "is_admin": user.is_admin}, extra=[("Set-Cookie", flags)])

    def _upload(self):
        files, fields = self._multipart()
        instructions = (fields.get("instructions") or "").strip()[:2000]
        # dedicated weight field — deterministic rescale, never touches the AI path.
        target_grams = _parse_target_grams(fields.get("target_grams"))
        if len(files) > settings.MAX_BATCH_FILES:
            return self._err(400, f"Too many files (max {settings.MAX_BATCH_FILES}).")
        u = self._user()
        bid = jobs.JOBS.new_batch(u, instructions, target_grams)
        accepted = []
        for fname, data in files:
            if Path(fname).suffix.lower() not in ALLOWED_EXT:
                continue
            if len(data) > settings.MAX_FILE_MB * 1024 * 1024:
                return self._err(400, f"{fname} exceeds {settings.MAX_FILE_MB} MB.")
            dest = settings.UPLOAD_DIR / (uuid.uuid4().hex[:12] + Path(fname).suffix.lower())
            dest.write_bytes(data)
            fid = jobs.JOBS.add_file(bid, fname, dest)
            jobs.enqueue(bid, fid)
            accepted.append(fname)
        if not accepted:
            return self._err(400, "No supported files. Allowed: xlsx, xls, pdf, docx, jpg, png, heic.")
        self._json({"batch_id": bid, "accepted": accepted})

    def _batch(self, bid):
        if bid not in jobs.JOBS.batches:
            return self._err(404, "Batch not found.")
        self._json(jobs.JOBS.public(bid))

    def _generate(self):
        b = self._json_body()
        recipe = b.get("recipe")
        if not recipe or not recipe.get("ingredients"):
            return self._err(400, "Recipe with ingredients required.")
        path = generate_xlsx(recipe)
        token = uuid.uuid4().hex
        jobs.JOBS.generated[token] = {"path": path, "recipe_name": recipe["recipe_name"], "batch_id": b.get("batch_id")}
        store.record_generation(self._user(), recipe["recipe_name"], recipe.get("confidence"), recipe.get("source_name", ""))
        self._json({"token": token, "filename": Path(path).name})

    def _generate_combined(self):
        b = self._json_body()
        recipes = [r for r in (b.get("recipes") or []) if r.get("ingredients")]
        if len(recipes) < 2:
            return self._err(400, "A combined file needs at least two recipes.")
        path = generate_combined_xlsx(recipes, b.get("file_name", ""))
        token = uuid.uuid4().hex
        jobs.JOBS.generated[token] = {"path": path, "recipe_name": Path(path).stem, "batch_id": b.get("batch_id")}
        for r in recipes:
            store.record_generation(self._user(), r.get("recipe_name", ""), r.get("confidence"), b.get("file_name", ""))
        self._json({"token": token, "filename": Path(path).name})

    def _download(self, token):
        g = jobs.JOBS.generated.get(token)
        if not g or not Path(g["path"]).exists():
            return self._err(404, "File not available (it may have expired).")
        name = Path(g["path"]).name
        self._send(200, XLSX_MIME, Path(g["path"]).read_bytes(),
                   extra=[("Content-Disposition", f'attachment; filename="{name}"')])

    def _download_all(self, bid):
        items = [g for g in jobs.JOBS.generated.values() if g.get("batch_id") == bid and Path(g["path"]).exists()]
        if not items:
            return self._err(404, "Nothing generated yet for this batch.")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for g in items:
                z.write(g["path"], arcname=Path(g["path"]).name)
        self._send(200, "application/zip", buf.getvalue(),
                   extra=[("Content-Disposition", 'attachment; filename="recipes.zip"')])

    def _admin_json(self, fn):
        u = self._user()
        if not u or not u.is_admin:
            return self._err(403, "Administrator approval required for this action.")
        try:
            self._json(fn(u))
        except KeyError:
            self._err(404, "Not found.")

    def log_message(self, *_):        # keep the console quiet
        pass


def main():
    port = int(os.getenv("PORT", "8000"))
    _cleanup_generated()
    jobs.start_worker()
    print(f"Recipe Converter running on http://0.0.0.0:{port}  (auth mode: {settings.AUTH_MODE})")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
