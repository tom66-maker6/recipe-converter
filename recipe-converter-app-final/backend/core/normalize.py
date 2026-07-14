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
_GRAM = ("g", "gr", "gram", "grams", "gramme", "grammes", "gm")
_PCS  = ("pc", "pcs", "piece", "pieces", "piÃ¨ce", "piece", "unit", "units", "u",
         "x", "pod", "pods", "gousse", "gousses", "ea")
_SHEET = ("sheet", "sheets", "feuille", "feuilles", "leaf", "leaves")

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
    name_in = raw["name"]
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
        notes.append(f"Unknown ingredient: '{name}' (not yet in ingredient database)")

    # ---------- 3. EGGS: whole eggs counted in pieces -> grams ----------
    kind = _egg_kind(name)
    if kind and (unit_k in _PCS or unit_k in ("", "oeuf", "egg", "oeufs", "eggs")):
        if kind == "whole":
            grams = qty * cfg["egg_gram_weight"]
            notes.append(f"Eggs: {int(qty)} × {cfg['egg_gram_weight']} g → {round(grams)} g")
            return _row(name, round(grams), "Gr", status, notes)
        # yolks/whites by piece have no configured gram factor -> keep pieces + flag
        notes.append(f"No gram factor for {kind} eggs by piece — left as pieces, please verify")
        return _row(name, round(qty), "Pcs", status, notes)

    # ---------- 4. UNIT normalization ----------
    if unit_k in _KG:
        return _row(name, round(qty * 1000), "Gr", status, notes)
    if unit_k in _GRAM:
        return _row(name, round(qty), "Gr", status, notes)
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
    return [normalize_ingredient(r, db, cfg) for r in ingredients]
