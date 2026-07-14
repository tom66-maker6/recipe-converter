"""
pipeline.py — orchestration around the PROVEN core (unchanged).

raw recipe --> normalize (core) --> consistency/confidence (core) --> review
preview  --> [user edits] --> populate master template (core) --> .xlsx
"""
import sys, datetime
import settings

# Keep the tested core exactly as-is: add its folder to the path and import by name.
sys.path.insert(0, str(settings.CORE_DIR))
import json as _json
from ingredient_db import IngredientDB          # noqa: E402
from normalize import normalize_recipe          # noqa: E402
from checks import score_recipe                 # noqa: E402
from populate_template import populate, safe_filename  # noqa: E402

CFG = _json.load(open(settings.CORE_DIR / "config.json", encoding="utf-8"))

# The 20 allowed categories (source of truth). Never emit anything else.
CATEGORIES = ["Pudding", "Bakery Dough", "Dough", "Biscuit and Sponge", "Chantilly",
    "Cheesecake Cream", "Confiserie and Meringue", "Cremeux and Cream",
    "Ganache and Namelaka", "Confiserie and Marmalade", "Crunchy", "Gel", "Mousse",
    "Glaze and Spray", "Chocolate Decor", "Candied and Caramelized", "Other",
    "Syrup and Sauce", "Sorbet and Ice Cream", "Savory"]

_CAT_KEYWORDS = [
    (("cremeux", "crémeux", "cremaux", "cremeux"), "Cremeux and Cream"),
    (("mousse",), "Mousse"), (("ganache", "namelaka"), "Ganache and Namelaka"),
    (("chantilly",), "Chantilly"), (("cheesecake",), "Cheesecake Cream"),
    (("sponge", "biscuit", "genoise", "génoise", "joconde", "dacquoise"), "Biscuit and Sponge"),
    (("sorbet", "ice cream", "glace", "gelato"), "Sorbet and Ice Cream"),
    (("glaze", "glazing", "glaçage", "glacage", "spray", "miroir"), "Glaze and Spray"),
    (("syrup", "sirop", "sauce", "coulis"), "Syrup and Sauce"),
    (("meringue",), "Confiserie and Meringue"),
    (("marmalade", "confit", "jam", "compote", "confiture"), "Confiserie and Marmalade"),
    (("crunchy", "croustillant", "streusel", "praline feuillet", "feuilletine"), "Crunchy"),
    (("brioche", "viennoiserie", "croissant", "laminated", "puff", "feuilletée"), "Bakery Dough"),
    (("dough", "pâte", "pate", "pastry dough", "sablé", "sable", "sucrée", "brisée"), "Dough"),
    (("pudding", "flan", "custard set"), "Pudding"), (("gel", "gelée", "gelee"), "Gel"),
    (("chocolate decor", "décor", "decor", "tempered chocolate"), "Chocolate Decor"),
    (("caramel", "candied", "candy", "nougat", "confiserie"), "Candied and Caramelized"),
    (("savory", "savoury", "salé", "sale"), "Savory"),
]

def _strip(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c)).lower()

def classify_category(name, ingredients, hint=""):
    # Match on the NAME/hint (not ingredient names) so 'gelatine' can't imply 'Gel'.
    text = " " + _strip(" ".join([name or "", hint or ""])) + " "
    import re as _re
    for keys, cat in _CAT_KEYWORDS:
        if any(_re.search(r"\b" + _re.escape(_strip(k)) + r"\b", text) for k in keys):
            return cat, "matched"
    return "Other", "low-confidence"     # per spec: unsure -> Other

def _titlecase_name(name: str) -> str:
    # Keep tokens that contain a digit (e.g. "70%"); Title-Case everything else.
    return " ".join(w if any(c.isdigit() for c in w) else w.capitalize()
                    for w in (name or "").split()) or "Untitled Recipe"

def build_preview(raw, db: IngredientDB, ocr_uncertainty=0):
    norm = normalize_recipe(raw.get("ingredients", []), db, CFG)
    process = raw.get("process", "") or ""
    score, reasons = score_recipe(norm, process, CFG, ocr_uncertainty)

    conversions, warnings = [], list(reasons)
    for n in norm:
        for note in n["notes"]:
            (conversions if ("→" in note or "Gelatine:" in note or "Eggs:" in note) else warnings).append(note)

    cat, cat_status = classify_category(raw.get("recipe_name", ""), norm, raw.get("category_hint", ""))
    if cat_status == "low-confidence":
        warnings.append("Category could not be determined confidently — defaulted to 'Other'.")

    def _dedupe(seq):
        seen, out = set(), []
        for x in seq:
            if x not in seen:
                seen.add(x); out.append(x)
        return out
    conversions, warnings = _dedupe(conversions), _dedupe(warnings)

    today = datetime.date.today().strftime("%d/%m/%Y")
    ingredients = [{
        "no": i + 1, "name": n["name"], "unit": n["unit"], "qty": n["qty"],
        "status": n["status"], "notes": n["notes"],
        "raw": raw["ingredients"][i] if i < len(raw.get("ingredients", [])) else {},
    } for i, n in enumerate(norm)]

    return {
        "recipe_name": _titlecase_name(raw.get("recipe_name", "")),
        "category": cat, "categories": CATEGORIES,
        "date_created": today, "last_modified": today,
        "ingredients": ingredients, "process": process,
        "confidence": score, "warnings": warnings, "conversions": conversions,
        "needs_review": score < 100 or bool(warnings),
    }

def generate_xlsx(recipe: dict) -> str:
    """recipe = final (user-approved) preview dict -> path to generated .xlsx."""
    data = {
        "recipe_name": recipe["recipe_name"],
        "category": recipe["category"] if recipe["category"] in CATEGORIES else "Other",
        "date_created": recipe.get("date_created") or datetime.date.today().strftime("%d/%m/%Y"),
        "last_modified": datetime.date.today().strftime("%d/%m/%Y"),
        "ingredients": [{"name": i["name"], "unit": i["unit"], "qty": i["qty"]}
                        for i in recipe["ingredients"] if str(i.get("name", "")).strip()],
        "process": recipe.get("process", ""),
    }
    out = settings.OUTPUT_DIR / safe_filename(data["recipe_name"])
    populate(str(settings.MASTER_TEMPLATE), data, str(out))
    return str(out)
