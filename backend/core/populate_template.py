"""
populate_template.py  —  Recipe Converter, template-population engine (Phase 0)

Writes extracted recipe data into the master Excel template WITHOUT altering any
formatting. It does NOT use a spreadsheet library to re-save the workbook (that
would drop the header logo, printer settings and calc chain). Instead it treats
the .xlsx as a ZIP of XML parts and surgically edits ONLY the target cells,
leaving styles.xml, theme, drawings, media (logo), formulas and protection
byte-for-byte identical.

Only 8 targets are ever written:
  A7  Recipe Name (label + value share the cell)
  C10 Category            A10 Date Created        A11 Last modified
  B14:B33 Ingredient names    D14:D33 Units        E14:E33 Recipe 1 quantities
  A38 Process
Columns C and F:I are formulas and are never touched; they recalculate on open.
"""
import re, shutil, zipfile, unicodedata
from xml.sax.saxutils import escape

def safe_filename(recipe_name: str) -> str:
    """Filesystem-safe .xlsx name. The recipe name keeps its accents INSIDE the
    workbook (correct French, e.g. 'Crémeux'); the FILENAME is transliterated to
    ASCII to avoid Unicode-normalization breakage across Windows/macOS/OneDrive."""
    ascii_name = unicodedata.normalize("NFKD", recipe_name).encode("ascii", "ignore").decode()
    ascii_name = re.sub(r'[\\/:*?"<>|]', "", ascii_name).strip() or "Recipe"
    return f"{ascii_name}.xlsx"

# Fixed layout of the master template (rows 14..33 = 20 ingredient slots)
FIRST_ROW, LAST_ROW = 14, 33
LABEL = {
    "name":          "Recipe Name : ",
    "date_created":  "Date Created:    ",
    "last_modified": "Last modified :    ",
    "category":      "Category : ",
}

def _find_style(attrs: str) -> str:
    m = re.search(r's="(\d+)"', attrs)
    return m.group(1) if m else "0"

def _replace_cell(sheet: str, coord: str, new_cell_builder) -> str:
    """Replace the <c r="coord"> element, preserving its style index."""
    # empty self-closing form first, then full form
    for pat in (rf'<c r="{coord}"([^>]*?)/>', rf'<c r="{coord}"([^>]*?)>.*?</c>'):
        m = re.search(pat, sheet, re.S)
        if m:
            style = _find_style(m.group(1))
            return sheet[:m.start()] + new_cell_builder(style) + sheet[m.end():]
    raise KeyError(f"cell {coord} not found in sheet XML")

def _set_text(sheet: str, coord: str, text: str) -> str:
    esc = escape(text)
    build = lambda s: f'<c r="{coord}" s="{s}" t="inlineStr"><is><t xml:space="preserve">{esc}</t></is></c>'
    return _replace_cell(sheet, coord, build)

def _num(value) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))

def _set_number(sheet: str, coord: str, value) -> str:
    build = lambda s: f'<c r="{coord}" s="{s}"><v>{_num(value)}</v></c>'
    return _replace_cell(sheet, coord, build)

def _fit_row_height(sheet: str, rownum: int, text: str, width_chars: int = 95,
                    base: float = 205.5, per_line: float = 15.5, cap: float = 409.0) -> str:
    """Raise a row's height so a long wrapped process stays fully visible. Never
    shrinks below the template's own height — only grows for oversized content."""
    lines = 0
    for para in str(text).split("\n"):
        lines += max(1, -(-len(para) // width_chars))     # ceil: visual wrapped lines
    ht = min(cap, max(base, lines * per_line))
    if ht <= base:
        return sheet
    m = re.search(rf'<row r="{rownum}"([^>]*?)>', sheet)
    if not m:
        return sheet
    attrs = re.sub(r'\s+ht="[^"]*"', '', m.group(1))
    attrs = re.sub(r'\s+customHeight="[^"]*"', '', attrs)
    return sheet[:m.start()] + f'<row r="{rownum}"{attrs} ht="{ht:g}" customHeight="1">' + sheet[m.end():]

def _set_formula_cache(sheet: str, coord: str, value) -> str:
    """Refresh a formula cell's cached RESULT without altering its <f> formula.
    Keeps the workbook internally consistent so any viewer shows the right numbers
    immediately, exactly like an Excel-saved file — no recalc dependency."""
    m = re.search(rf'<c r="{coord}"([^>]*?)>(.*?)</c>', sheet, re.S)
    if not m:
        return sheet
    style = _find_style(m.group(1))
    fm = re.search(r'<f\b.*?(?:</f>|/>)', m.group(2), re.S)   # capture formula verbatim
    if not fm:
        return sheet
    new = f'<c r="{coord}" s="{style}">{fm.group(0)}<v>{_num(value)}</v></c>'
    return sheet[:m.start()] + new + sheet[m.end():]

def populate(template_path: str, data: dict, output_path: str) -> None:
    z = zipfile.ZipFile(template_path)
    sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
    workbook = z.read("xl/workbook.xml").decode("utf-8")

    # --- header fields (label prefix preserved, value appended) ---
    sheet = _set_text(sheet, "A7",  LABEL["name"]          + data["recipe_name"])
    sheet = _set_text(sheet, "C10", LABEL["category"]      + data["category"])
    sheet = _set_text(sheet, "A10", LABEL["date_created"]  + data["date_created"])
    sheet = _set_text(sheet, "A11", LABEL["last_modified"] + data["last_modified"])

    # --- ingredient rows ---
    ings = data["ingredients"]
    if len(ings) > (LAST_ROW - FIRST_ROW + 1):
        raise ValueError(f"template holds max {LAST_ROW-FIRST_ROW+1} ingredients, got {len(ings)}")
    for i, ing in enumerate(ings):
        row = FIRST_ROW + i
        sheet = _set_text(sheet,   f"B{row}", ing["name"])
        sheet = _set_text(sheet,   f"D{row}", ing["unit"])          # Gr / Pcs
        sheet = _set_number(sheet, f"E{row}", ing["qty"])           # Recipe 1 only

    # --- process (full method, may span many lines) ---
    if data.get("process"):
        sheet = _set_text(sheet, "A38", data["process"])
        sheet = _fit_row_height(sheet, 38, data["process"])

    # --- refresh cached results of the auto-calc columns (%, Recipe 2-5, totals)
    #     so the file displays correctly in every viewer, not just on Excel recalc.
    #     Formula MULTIPLIERS mirror the template: F=x2, G=x3, H=x3.5, I=x5.
    qtys = [ing["qty"] for ing in ings]
    e_total = sum(qtys)
    col_mult = {"F": 2, "G": 3, "H": 3.5, "I": 5}
    for i, e in enumerate(qtys):
        row = FIRST_ROW + i
        sheet = _set_formula_cache(sheet, f"C{row}", (e / e_total * 100) if e_total else 0)
        for col, mult in col_mult.items():
            sheet = _set_formula_cache(sheet, f"{col}{row}", e * mult)
    # totals row (35)
    sheet = _set_formula_cache(sheet, "C35", 100 if e_total else 0)
    sheet = _set_formula_cache(sheet, "E35", e_total)
    for col, mult in col_mult.items():
        sheet = _set_formula_cache(sheet, f"{col}35", e_total * mult)

    # --- also flag a full recalc on open, as a belt-and-braces safeguard ---
    workbook = workbook.replace('<calcPr calcId="191028"/>',
                                '<calcPr calcId="191028" fullCalcOnLoad="1"/>')

    # --- rewrite the zip: every other part is copied byte-for-byte ---
    shutil.copyfile(template_path, output_path)  # not used for content, ensures path exists
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as out:
        for item in z.infolist():
            if item.filename == "xl/worksheets/sheet1.xml":
                out.writestr(item, sheet.encode("utf-8"))
            elif item.filename == "xl/workbook.xml":
                out.writestr(item, workbook.encode("utf-8"))
            else:
                out.writestr(item, z.read(item.filename))   # identical bytes
    z.close()
