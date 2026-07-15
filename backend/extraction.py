"""
extraction.py — turn an uploaded source file into one or more RAW recipes.

Two backends behind one interface:
  • HeuristicExtractor — local, no API key. Finds the ingredient table wherever it
    is in the sheet/doc, maps columns by header name AND by cell values (units vs
    numbers vs cost columns), and pulls recipe name / category / process from the
    surrounding cells. Handles real hotel cost-sheet layouts.
  • AzureOpenAIExtractor — optional; handles very messy layouts, OCR text and
    free-text translation. Selected automatically when configured.

Output per recipe (RAW — before the deterministic normalization core runs):
  {"recipe_name","category_hint","process","ingredients":[{name,qty,unit}]}
"""
import re, json
from pathlib import Path
import settings

class NeedsOCR(Exception): ...
class ExtractionError(Exception): ...

# ---------------------------------------------------------------- readers ----
def read_docx(path):
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    from docx.oxml.ns import qn
    doc = Document(str(path))
    blocks = []
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            txt = Paragraph(child, doc).text.strip()
            if txt:
                blocks.append({"type": "p", "text": txt})
        elif child.tag == qn("w:tbl"):
            rows = [[c.text.strip() for c in row.cells] for row in Table(child, doc).rows]
            blocks.append({"type": "table", "rows": rows})
    return blocks

def read_xlsx(path):
    import openpyxl
    wb = openpyxl.load_workbook(str(path), data_only=True)
    blocks = []
    for ws in wb.worksheets:
        rows = []
        for row in ws.iter_rows(values_only=True):
            cells = [("" if v is None else str(v).strip()) for v in row]
            rows.append(cells)
        if any(any(c for c in r) for r in rows):
            blocks.append({"type": "table", "rows": rows})
    return blocks

def read_pdf_text(path):
    try:
        import pdfplumber
    except Exception:
        raise NeedsOCR("PDF text extraction library unavailable")
    blocks, text_total = [], 0
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            text_total += len(txt)
            for ln in txt.splitlines():
                if ln.strip():
                    blocks.append({"type": "p", "text": ln.strip()})
            for tb in (page.extract_tables() or []):
                blocks.append({"type": "table", "rows": [[(c or "").strip() for c in r] for r in tb]})
        if text_total < 20:
            raise NeedsOCR("PDF appears to be scanned (no selectable text)")
    return blocks

READERS = {".docx": read_docx, ".xlsx": read_xlsx, ".xls": read_xlsx, ".pdf": read_pdf_text}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic"}

def load_source(path: Path):
    ext = path.suffix.lower()
    if ext in IMAGE_EXT:
        raise NeedsOCR(f"{ext} image requires OCR")
    if ext not in READERS:
        raise ExtractionError(f"Unsupported file type: {ext}")
    return READERS[ext](path)

# ------------------------------------------------------- shared helpers ----
_NAME_RE = re.compile(r"(item|recipe|recette|produit|product|name|nom)\s*[:\-]\s*(.+)", re.I)
_CAT_RE = re.compile(r"(category|cat[ée]gorie)\s*[:\-]\s*(.+)", re.I)
_METHOD_HDR = re.compile(r"^(method|méthode|methode|process|procédé|procede|preparation|préparation|steps?)\b", re.I)
_METHOD_LABEL = re.compile(r"^(process|method|méthode|methode|procédé|procede|preparation|préparation)\b\s*[:\-]?\s*$", re.I)
_STOP = re.compile(r"^(comment|allerg|food allerg|picture|nutrition|yield|portion|vegetarian|dairy|nuts?|gluten|sesame|storage|shelf)", re.I)
_JUNK = re.compile(r"(cost center|cost:|code|rev\b|révision|revision|approv|printed|imprim|page\s*\d|"
                   r"signature|department|service|outlet|hôtel|hotel|storage|allerg|uncontrolled|property of|atlantis)", re.I)

_NAME_HDR = ("ingredient", "ingredients", "ingrédient", "ingredient", "component", "composant",
             "item", "items", "produit", "product", "formula", "formule", "recette")
_QTY_HDR = ("quantit", "qty", "weight", "poids", "amount", "grams", "gramme", "gr ")
_COST_HDR = ("cost", "price", "prix", "code", "extended", "total", "%")
_UNIT_TOKENS = {"gr", "g", "gm", "gram", "grams", "gramme", "grammes", "kg", "kilo", "pcs", "pc",
                "piece", "pieces", "pièce", "ml", "l", "cl", "dl", "pod", "pods", "gousse", "gousses",
                "unit", "units", "u", "ea", "tsp", "tbsp", "oz", "leaf", "leaves", "feuille", "feuilles",
                "sheet", "sheets"}

def _num(s):
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"-?\d+[.,]?\d*", str(s).replace(",", "."))
    return float(m.group(0)) if m else None

def _is_unit(v):
    return str(v).strip().lower() in _UNIT_TOKENS

def _clean(s):
    return re.sub(r"\s+", " ", str(s)).strip(" :-–").strip()

def _name_header_col(lc):
    """Index of a real ingredient-column HEADER cell (e.g. 'Ingredients'), not a
    recipe-name line like 'Item: 3 Chocolates Cookie'."""
    for j, c in enumerate(lc):
        if not c:
            continue
        base = c.rstrip(":").strip()
        if base in _NAME_HDR:
            return j
        # allow 'Ingredient Name' style: short, first word is a keyword, no digits, no mid-colon
        if len(c) <= 16 and not any(ch.isdigit() for ch in c) and ":" not in base:
            if c.split() and c.split()[0] in _NAME_HDR:
                return j
    return None

def _find_ingredient_header(rows):
    """Locate the header row anywhere in a grid and map name/qty/unit columns."""
    for i, row in enumerate(rows):
        lc = [str(c).strip().lower() for c in row]
        name_col = _name_header_col(lc)
        if name_col is None:
            continue
        data = rows[i + 1:i + 14]
        qty_col, unit_col = _pick_qty_unit(row, data, name_col)
        if qty_col is not None:
            return i, name_col, qty_col, unit_col
    return None

def _pick_qty_unit(header_row, data_rows, name_col):
    ncols = max([len(header_row)] + [len(r) for r in data_rows] + [0])
    unit_col, best_unit = None, 0.0
    numeric = []          # (col, header, has_nonzero)
    for j in range(ncols):
        if j == name_col:
            continue
        hdr = str(header_row[j]).strip().lower() if j < len(header_row) else ""
        if any(x in hdr for x in _COST_HDR):
            continue
        vals = [r[j] for r in data_rows if j < len(r) and str(r[j]).strip() != ""]
        if not vals:
            continue
        unit_ratio = sum(1 for v in vals if _is_unit(v)) / len(vals)
        num_ratio = sum(1 for v in vals if _num(v) is not None) / len(vals)
        if unit_ratio > best_unit and unit_ratio >= 0.5:
            best_unit, unit_col = unit_ratio, j
        if num_ratio >= 0.5:
            nums = [_num(v) for v in vals if _num(v) is not None]
            numeric.append((j, hdr, any(n not in (0, None) for n in nums)))
    qty_col = None
    for j, hdr, _ in numeric:                       # prefer a quantity-named column
        if j != unit_col and any(x in hdr for x in _QTY_HDR):
            qty_col = j; break
    if qty_col is None:                             # else first numeric, non-unit, non-zero column
        for j, hdr, nonzero in numeric:
            if j != unit_col and nonzero:
                qty_col = j; break
    return qty_col, unit_col

def _ingredients_from_table(rows):
    hdr = _find_ingredient_header(rows)
    if hdr is None:
        return []
    i, name_col, qty_col, unit_col = hdr
    out, blanks = [], 0
    for row in rows[i + 1:]:
        name = str(row[name_col]).strip() if name_col < len(row) and row[name_col] is not None else ""
        if not name or not re.search(r"[A-Za-zÀ-ÿ]", name):
            blanks += 1
            if blanks >= 3 and out:
                break
            continue
        if name.lower() in ("total", "totaux", "subtotal", "sub total", "grand total"):
            break
        blanks = 0
        qty = _num(row[qty_col]) if (qty_col is not None and qty_col < len(row)) else None
        if qty is None:
            continue
        unit = str(row[unit_col]).strip() if (unit_col is not None and unit_col < len(row) and row[unit_col] is not None) else ""
        out.append({"name": name, "qty": qty, "unit": unit})
    return out

def _looks_like_ingredient_table(rows):
    return _find_ingredient_header(rows) is not None

def _scan_grid_meta(rows):
    """Pull recipe name / category / process from scattered cells in a grid."""
    cells = [str(v).strip() for row in rows for v in row if str(v).strip()]
    name = None
    for t in cells:
        m = _NAME_RE.match(t)
        if m and not _JUNK.search(t):
            name = _clean(m.group(2)); break
    cat = ""
    for t in cells:
        m = _CAT_RE.match(t)
        if m:
            cat = _clean(m.group(2)); break
    proc, started = [], False
    for t in cells:
        if started:
            if _STOP.search(t):
                break
            if len(t) > 12 or proc:
                proc.append(t)
        elif _METHOD_LABEL.match(t):
            started = True
    return name, cat, " ".join(proc).strip()

# --------------------------------------------------- heuristic extractor -----
class HeuristicExtractor:
    name = "local-heuristic"

    def extract(self, path: Path, instructions: str = ""):   # instructions ignored (no AI)
        blocks = load_source(path)
        tables = [b["rows"] for b in blocks if b["type"] == "table"]
        # A spreadsheet is one big grid -> grid mode (find table + scan meta cells).
        big_grid = path.suffix.lower() in (".xlsx", ".xls")
        recipes = self._grid(tables, path) if big_grid else self._blocks(blocks, path)
        if not recipes:
            # fallback: try the other strategy before giving up
            recipes = self._blocks(blocks, path) if big_grid else self._grid(tables, path)
        if not recipes:
            raise ExtractionError("Could not locate an ingredient table. "
                                  "Configure the AI extractor for free-form recipes.")
        ambiguous = False
        return {"recipes": recipes, "detected": len(recipes),
                "ambiguous_multi": ambiguous, "engine": self.name, "ocr_uncertainty": 0}

    def _grid(self, tables, path):
        recipes = []
        for rows in tables:
            ings = _ingredients_from_table(rows)
            if not ings:
                continue
            name, cat, proc = _scan_grid_meta(rows)
            recipes.append({"recipe_name": name or path.stem, "category_hint": cat,
                            "process": proc, "ingredients": ings})
        return recipes

    def _blocks(self, blocks, path):
        recipes, pending_name, collecting = [], None, None
        for blk in blocks:
            if blk["type"] == "p":
                text = blk["text"]
                if _METHOD_HDR.match(text):
                    collecting = recipes[-1] if recipes else None
                    remainder = _clean(re.sub(_METHOD_HDR, "", text, count=1))
                    if collecting and remainder:
                        collecting["process"] = (collecting["process"] + " " + remainder).strip()
                    continue
                m = _NAME_RE.search(text)
                if m and not _JUNK.search(text):
                    pending_name = _clean(m.group(2)); collecting = None; continue
                if collecting is not None and not _JUNK.search(text) and not _STOP.search(text):
                    collecting["process"] = (collecting["process"] + " " + text).strip(); continue
                if pending_name is None and 3 <= len(text) <= 60 and ":" not in text and not _JUNK.search(text):
                    pending_name = _clean(text)
            elif blk["type"] == "table":
                ings = _ingredients_from_table(blk["rows"])
                if not ings:
                    continue
                recipes.append({"recipe_name": pending_name or path.stem,
                                "category_hint": "", "process": "", "ingredients": ings})
                pending_name, collecting = None, None
        return recipes

# --------------------------------------------------- Azure OpenAI extractor ---
_SYSTEM = """You extract pastry recipes from arbitrary documents into strict JSON.
Rules: extract only what is present, never invent. Translate everything to English
using professional pastry terminology. Ignore hotel/outlet/department/cost-center/
codes/revision/approvals/dates/page numbers/logos. Detect multiple independent
recipes and return each separately; if unsure whether it is one recipe or several,
set "ambiguous_multi": true. Do NOT merge duplicate ingredients — preserve order.
Return quantities and units exactly as written (the downstream engine converts
kg→g, eggs→g and Gelatine Mass; do not do those conversions yourself)."""
_SCHEMA_HINT = ('Return {"recipes":[{"recipe_name","category_hint","process",'
                '"ingredients":[{"name","qty","unit"}]}],"ambiguous_multi":bool}')

def _with_instructions(base, instructions):
    """Append the user's free-text instructions to the AI prompt (opt-in)."""
    if instructions and instructions.strip():
        return base + ("\n\nUSER INSTRUCTIONS — apply these to the recipe BEFORE returning the "
                       "JSON (e.g. substitute or rename ingredients, rescale all quantities to a "
                       "target total mass, adjust as asked). Apply them faithfully and keep the "
                       "same JSON shape:\n" + instructions.strip()[:2000])
    return base

class AzureOpenAIExtractor:
    name = "azure-openai"

    def extract(self, path: Path, instructions: str = ""):
        try:
            blocks = load_source(path)
        except NeedsOCR:
            blocks = self._ocr(path)
        doc_text = "\n".join(
            b["text"] if b["type"] == "p" else "\n".join(" | ".join(r) for r in b["rows"])
            for b in blocks)[:120_000]
        payload = self._call(doc_text, instructions)
        payload.setdefault("ocr_uncertainty", 0)
        payload["engine"] = self.name
        payload["detected"] = len(payload.get("recipes", []))
        payload.setdefault("ambiguous_multi", False)
        return payload

    def _ocr(self, path):
        raise NeedsOCR("OCR path requires AZURE_OCR_ENDPOINT/KEY (Document Intelligence)")

    def _call(self, doc_text, instructions=""):
        import requests
        url = (f"{settings.AZURE_OPENAI_ENDPOINT}/openai/deployments/"
               f"{settings.AZURE_OPENAI_DEPLOYMENT}/chat/completions?api-version=2024-06-01")
        body = {"messages": [{"role": "system", "content": _with_instructions(_SYSTEM + "\n" + _SCHEMA_HINT, instructions)},
                             {"role": "user", "content": doc_text}],
                "temperature": 0, "response_format": {"type": "json_object"}}
        r = requests.post(url, headers={"api-key": settings.AZURE_OPENAI_KEY}, json=body, timeout=90)
        r.raise_for_status()
        return json.loads(r.json()["choices"][0]["message"]["content"])

# --------------------------------------------------- Google Gemini extractor --
# FREE tier. Reads photos/scans/PDFs directly (vision) and any Excel/Word layout,
# and translates everything to English. Returns the SAME JSON shape as the others;
# the deterministic core still does kg→g / eggs / Gelatine Mass / template fill.
_IMG_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".heic": "image/heic"}
_RESOLVED_MODEL = [None]   # cache: the Gemini model that last worked for this account
_LISTED_MODELS = [None]    # cache: flash models this account can actually use

class GeminiExtractor:
    name = "gemini"

    def extract(self, path: Path, instructions: str = ""):
        prompt = _with_instructions(_SYSTEM + "\n" + _SCHEMA_HINT, instructions)
        ext = path.suffix.lower()
        if ext in IMAGE_EXT:
            return self._vision(path, _IMG_MIME.get(ext, "image/jpeg"), prompt)
        if ext == ".pdf":
            return self._vision(path, "application/pdf", prompt)   # Gemini reads PDFs (incl. scans)
        blocks = load_source(path)                                 # docx / xlsx -> text
        text = "\n".join(b["text"] if b["type"] == "p" else "\n".join(" | ".join(r) for r in b["rows"])
                         for b in blocks)[:120_000]
        return self._generate([{"text": prompt + "\n\nDOCUMENT:\n" + text}])

    def _vision(self, path: Path, mime: str, prompt: str):
        import base64
        data, mime = self._image_payload(path, mime)
        b64 = base64.b64encode(data).decode()
        return self._generate([{"text": prompt},
                               {"inline_data": {"mime_type": mime, "data": b64}}])

    def _image_payload(self, path: Path, mime: str):
        """Downscale big photos/screenshots before sending — faster & lighter."""
        raw = path.read_bytes()
        if mime.startswith("image/") and len(raw) > 1_500_000:
            try:
                import io
                from PIL import Image
                im = Image.open(io.BytesIO(raw)).convert("RGB")
                im.thumbnail((1600, 1600))
                buf = io.BytesIO(); im.save(buf, format="JPEG", quality=85)
                return buf.getvalue(), "image/jpeg"
            except Exception:
                pass
        return raw, mime

    def _candidates(self):
        """Models to try, best first: last-known-good, the env override, the models
        the account actually lists, then sensible fallbacks."""
        seen, out = set(), []
        def add(m):
            m = (m or "").strip()
            if m and m not in seen:
                seen.add(m); out.append(m)
        add(_RESOLVED_MODEL[0])
        add(settings.GEMINI_MODEL)
        for m in self._list_models():
            add(m)
        for m in ("gemini-flash-latest", "gemini-2.5-flash", "gemini-2.0-flash",
                  "gemini-2.0-flash-lite", "gemini-2.5-flash-lite"):
            add(m)
        return out

    def _list_models(self):
        """Ask Google which flash models THIS account may call (cached)."""
        if _LISTED_MODELS[0] is not None:
            return _LISTED_MODELS[0]
        import requests
        names = []
        try:
            r = requests.get("https://generativelanguage.googleapis.com/v1beta/models"
                             f"?key={settings.GEMINI_API_KEY}&pageSize=200", timeout=30)
            if r.status_code == 200:
                for m in r.json().get("models", []):
                    if "generateContent" in m.get("supportedGenerationMethods", []):
                        n = m.get("name", "").replace("models/", "")
                        if "flash" in n.lower() and "vision" not in n.lower():
                            names.append(n)
                names.sort(key=lambda n: ("lite" in n, "preview" in n or "exp" in n, n))
        except Exception:
            names = []
        _LISTED_MODELS[0] = names
        return names

    def _generate(self, parts):
        import requests, time
        body = {"contents": [{"parts": parts}],
                "generationConfig": {"response_mime_type": "application/json", "temperature": 0}}
        last = ""
        for model in self._candidates():
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{model}:generateContent?key={settings.GEMINI_API_KEY}")
            r = None
            for attempt in range(3):                       # auto-retry transient overloads
                try:
                    r = requests.post(url, json=body, timeout=75)
                except Exception as e:
                    raise ExtractionError(f"AI service is slow or unreachable right now: {e}")
                if r.status_code not in (500, 502, 503, 504):
                    break
                time.sleep(2 * (attempt + 1))
            if r.status_code == 200:
                _RESOLVED_MODEL[0] = model     # remember what worked
                try:
                    txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                    payload = json.loads(txt)
                except Exception as e:
                    raise ExtractionError(f"AI returned an unexpected response: {e}")
                payload.setdefault("ocr_uncertainty", 0)
                payload["engine"] = self.name
                payload["detected"] = len(payload.get("recipes", []))
                payload.setdefault("ambiguous_multi", False)
                return payload
            detail = ""
            try:
                detail = r.json().get("error", {}).get("message", "")
            except Exception:
                detail = (r.text or "")[:300]
            last = detail
            # model missing (404) or zero free quota -> try the next candidate
            if r.status_code == 404 or (r.status_code == 429 and "limit: 0" in detail):
                if _RESOLVED_MODEL[0] == model:
                    _RESOLVED_MODEL[0] = None
                continue
            if r.status_code in (500, 502, 503, 504):
                raise ExtractionError("The AI is temporarily busy (high demand). Please try again in a moment.")
            if r.status_code == 429:
                raise ExtractionError(f"Gemini rate limit reached — wait a minute. {detail}")
            if r.status_code in (400, 403):
                raise ExtractionError(f"Gemini key/permission problem: {detail}")
            raise ExtractionError(f"Gemini error {r.status_code}: {detail}")
        raise ExtractionError("No free Gemini model is available for this account/region "
                              f"(last: {last}). Enable pay-as-you-go billing in Google AI Studio, "
                              "or keep using the free local reader for Excel/Word/PDF.")

class HybridExtractor:
    """Best of both, and quota-friendly:
      • Excel / Word / text-PDF  -> free built-in reader (instant, no AI quota used)
      • Photos / scans           -> Gemini (only the AI can read images)
      • A document the reader can't parse -> falls back to Gemini
    This keeps the limited free Gemini quota for the files that truly need it."""
    name = "hybrid"

    def __init__(self):
        self.local = HeuristicExtractor()
        self.ai = GeminiExtractor()

    def extract(self, path: Path, instructions: str = ""):
        ext = path.suffix.lower()
        if ext in IMAGE_EXT:
            return self.ai.extract(path, instructions)          # images need vision AI
        if instructions and instructions.strip():
            return self.ai.extract(path, instructions)          # instructions -> AI applies them
        try:
            return self.local.extract(path)                     # documents: free + instant
        except (ExtractionError, NeedsOCR):
            return self.ai.extract(path, instructions)          # messy/scanned -> AI fallback

def get_extractor():
    if settings.gemini_enabled():
        return HybridExtractor()          # free local reader + Gemini for photos/messy files
    if settings.llm_enabled():
        return AzureOpenAIExtractor()
    return HeuristicExtractor()           # free, files-only, no free-text translation


# --------------------------------------------------- AI validation pass -------
_REVIEW_SYSTEM = """You review a standardized pastry recipe that was already extracted.
Return ONLY JSON:
{"category":"<exactly ONE value from the ALLOWED list>",
 "issues":["<short real problems: an error, an inconsistent ratio, a likely ACCIDENTAL
            duplicate ingredient, a suspicious quantity, a rule that seems broken>"],
 "recipe_name_en":"<the recipe name in English, Title Case>",
 "process_en":"<the method/process text in English>"}
Rules: do NOT change quantities or units. Do NOT merge duplicate ingredients (repeats
are often intentional stages) — you may FLAG a clearly accidental one in "issues".
List only REAL problems; use [] if none. Keep the name/process faithful, just in English."""

def review_recipe(recipe: dict, categories):
    """Ask Gemini to categorize, translate and flag issues on an already-extracted
    recipe. Reuses the model auto-detect + retry logic. Raises on failure so the
    caller can degrade gracefully (never blocks the conversion)."""
    payload = {
        "recipe_name": recipe.get("recipe_name", ""),
        "process": recipe.get("process", ""),
        "ingredients": [{"name": i["name"], "qty": i["qty"], "unit": i["unit"]}
                        for i in recipe.get("ingredients", [])],
    }
    text = (_REVIEW_SYSTEM + "\n\nALLOWED categories: " + " | ".join(categories)
            + "\n\nRECIPE:\n" + json.dumps(payload, ensure_ascii=False))
    return GeminiExtractor()._generate([{"text": text}])
