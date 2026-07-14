"""
extraction.py — turn an uploaded source file into one or more RAW recipes.

Two backends behind one interface:
  • HeuristicExtractor — local, no API key; good for clean table-based docx/xlsx.
  • AzureOpenAIExtractor — production; handles messy layouts, OCR text and novel
    translation. Selected automatically when Azure OpenAI is configured.

Readers return an ORDERED list of blocks so each recipe keeps its own name and
method:  [{"type":"p","text":...} | {"type":"table","rows":[[...]]}]

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
            if any(cells):
                rows.append(cells)
        if rows:
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

# ------------------------------------------------------- heuristic helpers ----
_NAME_RE = re.compile(r"(recipe|recette|produit|product|name|nom)\s*[:\-]\s*(.+)", re.I)
_METHOD_HDR = re.compile(r"^(method|méthode|methode|process|procédé|procede|preparation|préparation|steps?)\b", re.I)
_JUNK = re.compile(r"(cost center|code|rev\b|révision|revision|approv|printed|imprim|page\s*\d|"
                   r"signature|department|service|outlet|hôtel|hotel|storage|allerg|uncontrolled|property of)", re.I)
_ING_HDR = ("ingredient", "ingrédient", "component", "composant", "formula", "formule", "item")
_QTY_HDR = ("qty", "quantity", "quantité", "quantite", "amount", "poids", "weight")
_UNIT_HDR = ("unit", "unité", "unite", "uom")          # NB: no bare "u" (matched "quantité")

def _num(s):
    m = re.search(r"-?\d+[.,]?\d*", str(s).replace(",", "."))
    return float(m.group(0)) if m else None

def _split_qty_unit(qty_cell, unit_cell):
    unit = (unit_cell or "").strip()
    n = _num(qty_cell)
    if not unit:                                   # unit glued to the qty ("250 g")
        m = re.search(r"[a-zA-Zéè%]+", str(qty_cell))
        unit = m.group(0) if m else ""
    return n, unit

def _pick_columns(header):
    hl = [h.lower() for h in header]
    def find(cands):
        for i, h in enumerate(hl):
            if any(c in h for c in cands):
                return i
        return None
    return find(_ING_HDR), find(_QTY_HDR), find(_UNIT_HDR)

def _looks_like_ingredient_table(rows):
    if not rows or len(rows) < 2:
        return False
    ci, cq, _ = _pick_columns(rows[0])
    if ci is not None and cq is not None:
        return True
    numeric = sum(1 for r in rows if len(r) > 1 and _num(r[1]) is not None)
    return numeric >= max(2, len(rows) // 2)

def _table_to_ingredients(rows):
    if not rows or len(rows) < 2:
        return []
    ci, cq, cu = _pick_columns(rows[0])
    body = rows[1:]
    if ci is None:
        ci, cq, cu = 0, 1, (2 if len(rows[0]) > 2 else None)
        body = rows
    out = []
    for row in body:
        if ci >= len(row):
            continue
        name = row[ci].strip()
        # skip blanks / pure-number rows / totals — but KEEP names that merely
        # contain a number, e.g. "Dark Chocolate 70%".
        if not name or not re.search(r"[A-Za-zÀ-ÿ]", name) or \
           name.lower() in ("total", "totaux", "subtotal", "sub total"):
            continue
        qty_cell = row[cq] if (cq is not None and cq < len(row)) else ""
        unit_cell = row[cu] if (cu is not None and cu < len(row)) else ""
        qty, unit = _split_qty_unit(qty_cell, unit_cell)
        if qty is None:
            continue
        out.append({"name": name, "qty": qty, "unit": unit})
    return out

def _clean_name(s):
    return re.sub(r"\s+", " ", s).strip(" :-–").strip()

# --------------------------------------------------- heuristic extractor -----
class HeuristicExtractor:
    name = "local-heuristic"

    def extract(self, path: Path):
        blocks = load_source(path)
        recipes, pending_name, collecting = [], None, None
        name_lines = 0
        for blk in blocks:
            if blk["type"] == "p":
                text = blk["text"]
                if _METHOD_HDR.match(text):                       # method heading
                    collecting = recipes[-1] if recipes else None
                    remainder = _clean_name(re.sub(_METHOD_HDR, "", text, count=1))
                    if collecting and remainder:
                        collecting["process"] = (collecting["process"] + " " + remainder).strip()
                    continue
                m = _NAME_RE.search(text)
                if m and not _JUNK.search(text):                  # explicit "Product:" line
                    pending_name = _clean_name(m.group(2)); collecting = None; name_lines += 1; continue
                if collecting is not None and not _JUNK.search(text):
                    collecting["process"] = (collecting["process"] + " " + text).strip(); continue
                if pending_name is None and 3 <= len(text) <= 60 and ":" not in text and not _JUNK.search(text):
                    pending_name = _clean_name(text)              # weak fallback title
            elif blk["type"] == "table" and _looks_like_ingredient_table(blk["rows"]):
                ings = _table_to_ingredients(blk["rows"])
                if not ings:
                    continue
                recipes.append({"recipe_name": pending_name or path.stem,
                                "category_hint": "", "process": "", "ingredients": ings})
                pending_name, collecting = None, None

        if not recipes:
            raise ExtractionError("Could not locate an ingredient table. "
                                  "Configure the AI extractor for free-form recipes.")
        # ambiguous: several product headings but only one table -> ask the user
        ambiguous = len(recipes) == 1 and name_lines > 1
        return {"recipes": recipes, "detected": len(recipes),
                "ambiguous_multi": ambiguous, "engine": self.name, "ocr_uncertainty": 0}

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

class AzureOpenAIExtractor:
    name = "azure-openai"

    def extract(self, path: Path):
        try:
            blocks = load_source(path)
        except NeedsOCR:
            blocks = self._ocr(path)
        doc_text = "\n".join(
            b["text"] if b["type"] == "p" else "\n".join(" | ".join(r) for r in b["rows"])
            for b in blocks)[:120_000]
        payload = self._call(doc_text)
        payload.setdefault("ocr_uncertainty", 0)
        payload["engine"] = self.name
        payload["detected"] = len(payload.get("recipes", []))
        payload.setdefault("ambiguous_multi", False)
        return payload

    def _ocr(self, path):
        raise NeedsOCR("OCR path requires AZURE_OCR_ENDPOINT/KEY (Document Intelligence)")

    def _call(self, doc_text):
        import requests
        url = (f"{settings.AZURE_OPENAI_ENDPOINT}/openai/deployments/"
               f"{settings.AZURE_OPENAI_DEPLOYMENT}/chat/completions?api-version=2024-06-01")
        body = {"messages": [{"role": "system", "content": _SYSTEM + "\n" + _SCHEMA_HINT},
                             {"role": "user", "content": doc_text}],
                "temperature": 0, "response_format": {"type": "json_object"}}
        r = requests.post(url, headers={"api-key": settings.AZURE_OPENAI_KEY}, json=body, timeout=90)
        r.raise_for_status()
        return json.loads(r.json()["choices"][0]["message"]["content"])

def get_extractor():
    return AzureOpenAIExtractor() if settings.llm_enabled() else HeuristicExtractor()
