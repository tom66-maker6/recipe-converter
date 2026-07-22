"""
normalize.py — deterministic quantity/unit/name normalization.

Every rule here is fixed code driven by config.json — never a model guess.
Order of a single ingredient: gelatine special-case -> name standardization ->
egg count->grams -> unit normalization (kg->g, pieces). Duplicates are NEVER
merged: each source line becomes exactly one output line, in original order.
"""
import re
from ingredient_db import _key

_KG   = ("kg", "kilo", "kilogram", "kilogramme", "kilogrammes")
_GRAM = ("g", "gr", "gram", "grams", "gramme", "grammes", "gm", "gms", "grm", "grms")
_PCS  = ("pc", "pcs", "pce", "pces", "piece", "pieces", "pièce", "pièces", "unit", "units",
         "u", "x", "pod", "pods", "gousse", "gousses", "ea", "each", "no", "no.", "no's",
         "nos", "nos.", "number", "numbers", "count", "ct")
_SHEET = ("sheet", "sheets", "feuille", "feuilles", "leaf", "leaves")
# volume → grams, assuming density ~1 (fine for pastry liquids)
_ML = {"ml": 1, "milliliter": 1, "millilitre": 1, "cc": 1, "cl": 10, "dl": 100,
       "l": 1000, "lt": 1000, "liter": 1000, "litre": 1000}
# too generic to standardize — the chef must state which one (brand/type)
_GENERIC = ("pectin", "candied fruit", "candied fruits", "stabilizer", "stabiliser",
            "stabilizers", "stabilisers")

def _is_gelatine(name: str) -> bool:
    return "gelatin" in _key(name)   # matches gelatin / gelatine / gélatine

def _egg_kind(name: str):
    k = _key(name)
    if not re.search(r"\begg\b|\boeuf|jaune|yolk|\bblanc\b|egg white", k):
        return None
    if "yolk" in k or "jaune" in k: return "yolk"
    if "white" in k or "blanc" in k: return "white"
    return "whole"

def normalize_ingredient(raw, db, cfg):
    """raw = {'name', 'qty', 'unit'} from extraction -> normalized dict + notes."""
    name_in = db.pre_correct(raw["name"])   # fix typos first (Getaine→Gelatine, Millk→Milk)
    qty = float(raw["qty"])
    unit_k = _key(str(raw.get("unit", "")))
    notes = []

    # ---------- 1. GELATINE -> always Gelatine Mass (unless already mass) ----------
    if _is_gelatine(name_in):
        k = _key(name_in)
        sheet_g = cfg["gelatin_sheet_grams"]; ratio = cfg["gelatine_mass_ratio"]
        if "mass" in k or "masse" in k:
            grams = qty * 1000 if unit_k in _KG else qty
            return _row("Gelatine Mass", round(grams), "Gr", "known", notes)
        if unit_k in _SHEET or any(s in k for s in _SHEET) or unit_k in _PCS:
            pure = qty * sheet_g
            mass = pure * ratio
            notes.append(f"Gelatine: {int(qty)} sheet(s) × {sheet_g} g × {ratio} → {round(mass)} g Gelatine Mass")
            return _row("Gelatine Mass", round(mass), "Gr", "known", notes)
        # grams (or powder) of pure gelatin
        pure = qty * 1000 if unit_k in _KG else qty
        mass = pure * ratio
        notes.append(f"Gelatine: {round(pure)} g pure gelatin × {ratio} → {round(mass)} g Gelatine Mass")
        return _row("Gelatine Mass", round(mass), "Gr", "known", notes)

    # ---------- 2. NAME standardization ----------
    name, status = db.standardize(name_in)
    if status == "unknown":
        if _key(name) in _GENERIC:
            notes.append(f"Please specify which '{name}' (type/brand) — too generic to standardize")
        else:
            notes.append(f"Unknown ingredient: '{name}' (not yet in ingredient database)")

    # ---------- 3. EGGS ----------
    kind = _egg_kind(name)
    if kind in ("white", "yolk"):
        # egg WHITES and YOLKS are ALWAYS weighed in grams (never pieces)
        grams = round(qty * 1000) if unit_k in _KG else round(qty)
        return _row(name, grams, "Gr", status, notes)
    if kind == "whole" and unit_k in _PCS:
        # only WHOLE eggs can be counted in pieces -> grams
        grams = qty * cfg["egg_gram_weight"]
        notes.append(f"Eggs: {int(qty)} × {cfg['egg_gram_weight']} g → {round(grams)} g")
        return _row(name, round(grams), "Gr", status, notes)
    # whole eggs given in grams (or no unit) fall through to normal unit handling below

    # ---------- 4. UNIT normalization ----------
    if unit_k in _KG:
        return _row(name, round(qty * 1000), "Gr", status, notes)
    if unit_k in _GRAM:
        return _row(name, round(qty), "Gr", status, notes)
    if unit_k in _ML:
        return _row(name, round(qty * _ML[unit_k]), "Gr", status, notes)   # volume ≈ mass
    if unit_k in _PCS:
        return _row(name, round(qty), "Pcs", status, notes)
    if unit_k == "":
        return _row(name, round(qty), "Gr", status, notes)   # default to grams
    notes.append(f"Unknown unit '{raw.get('unit')}' on '{name}' — defaulted to grams, please verify")
    return _row(name, round(qty), "Gr", status, notes)

def _row(name, qty, unit, status, notes):
    return {"name": name, "qty": qty, "unit": unit, "status": status, "notes": notes}

def normalize_recipe(ingredients, db, cfg):
    """Normalize a list; preserve order and duplicates exactly (never merge)."""
    norm = [normalize_ingredient(r, db, cfg) for r in ingredients]
    _fix_mislabelled_eggs(norm, ingredients, cfg)
    return norm

def _fix_mislabelled_eggs(norm, raw_list, cfg):
    """SECONDARY safety net — the source UNIT is the primary truth. Whole eggs written
    in pieces are already converted above; eggs written in grams are normally KEPT as
    grams. We only reinterpret a gram value when it is clearly INCOHERENT, judged by the
    egg's share of the TOTAL recipe mass — never an absolute threshold:
        • 40 g of egg in a 200 g recipe  → 20 %  → real grams, keep.
        • '40' in a 6 kg recipe          → 0.7 % → far too little egg → really 40 pieces.
    So: reinterpret only if the value-as-grams is an implausibly tiny fraction of the
    recipe AND reading it as a piece count (× egg weight) gives a sensible proportion."""
    egg_w = cfg["egg_gram_weight"]
    total = sum(n["qty"] for n in norm if n["unit"] == "Gr" and n["qty"] > 0)
    if total <= 0:
        return
    for i, n in enumerate(norm):
        if _egg_kind(n["name"]) != "whole" or n["unit"] != "Gr":
            continue
        raw = raw_list[i] if i < len(raw_list) else {}
        if _key(str(raw.get("unit", ""))) in _PCS:      # source explicitly said pieces → trust it
            continue
        q = n["qty"]
        if q != round(q) or q < 1:                      # a piece count is a whole number
            continue
        rest = total - q
        share_g  = q / total                             # egg's share if the number is grams
        share_pc = (q * egg_w) / (rest + q * egg_w)      # …if the number is really a count
        if share_g < 0.04 and 0.03 <= share_pc <= 0.75:  # incoherent as grams, sensible as a count
            g = round(q * egg_w)
            n["qty"] = g
            n["notes"].append(
                f"Eggs: '{int(q)}' was written in grams but that is only {share_g*100:.1f}% of the "
                f"recipe — implausibly low for whole eggs. Read as {int(q)} pieces → {int(q)} × {egg_w} g "
                f"= {g} g ({share_pc*100:.0f}% of the recipe).")
