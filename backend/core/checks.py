"""
checks.py — confidence score + consistency detection.

Never silently guesses. Every deduction has an explicit human-readable reason,
so the user knows exactly why a recipe is below 100% before export.
"""
import re
from ingredient_db import _key

# ingredients commonly named in a method that must also appear in the list
_PROCESS_SIGNALS = {
    "vanilla": ("vanilla", "vanille"), "salt": ("salt", "sel"),
    "gelatine": ("gelatin", "gelatine", "gelatine mass"), "lemon": ("lemon", "citron"),
    "orange": ("orange",), "butter": ("butter", "beurre"), "sugar": ("sugar", "sucre"),
    "cream": ("cream", "creme"), "milk": ("milk", "lait"), "chocolate": ("chocolate", "chocolat"),
    "coffee": ("coffee", "cafe"), "honey": ("honey", "miel"),
}

def score_recipe(normalized, process_text, cfg, ocr_uncertainty=0):
    reasons, score = [], 100

    # --- ingredient-level flags coming from normalization ---
    for ing in normalized:
        for n in ing["notes"]:
            if n.startswith("Unknown ingredient"):
                score -= 8;  reasons.append(n)
            elif n.startswith("Unknown unit") or n.startswith("No gram factor"):
                score -= 6;  reasons.append(n)

    # --- suspicious quantities (e.g. "Eggs = 500 kg") ---
    for ing in normalized:
        if ing["unit"] == "Gr" and ing["qty"] > cfg["suspicious_qty_grams"]:
            score -= 15
            reasons.append(f"Possible unit error: {ing['name']} = {ing['qty']} g looks too large — please verify")

    # --- consistency: ingredient named in the method but absent from the list ---
    have = " | ".join(_key(i["name"]) for i in normalized)
    ptext = _key(process_text or "")
    for label, terms in _PROCESS_SIGNALS.items():
        if any(re.search(rf"\b{re.escape(t)}", ptext) for t in terms) and \
           not any(t in have for t in terms):
            score -= 10
            reasons.append(f"Possible missing ingredient: {label.capitalize()} is mentioned in the process "
                           f"but not in the ingredient list")

    # --- OCR uncertainty passed in from the OCR layer (0 for digital files) ---
    if ocr_uncertainty:
        pts = round(ocr_uncertainty)
        score -= pts
        reasons.append(f"OCR uncertainty on scanned/photographed input (−{pts}%)")

    return max(0, min(100, score)), reasons
