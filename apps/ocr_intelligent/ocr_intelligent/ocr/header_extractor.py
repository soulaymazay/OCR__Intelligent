"""
header_extractor.py — Groupe Bayoudh Metal
Lecture des en-têtes du document OCR.

Retourne :
{
    "type_document"   : "facture" | "bon_commande" | "devis" | "inconnu",
    "champs"          : { champ: valeur | None },   ← tous les champs
    "champs_trouves"  : ["numero_facture", ...],    ← champs avec valeur
    "champs_manquants": ["montant_ht", ...]         ← champs vides (None)
}
"""

import re


# ─────────────────────────────────────────────────────────────────────
# DÉTECTION DU TYPE DE DOCUMENT
# ─────────────────────────────────────────────────────────────────────

TYPE_PATTERNS = {
    "facture": [
        r"\bfacture\b", r"\binvoice\b", r"\bfact\s*n[°o]", r"\bfac[-\s]?\d+\b"
    ],
    "bon_commande": [
        r"\bbon\s+de\s+commande\b", r"\bpurchase\s+order\b", r"\bbc[-\s]?\d+\b", r"\bpo[-\s]?\d+\b"
    ],
    "devis": [
        r"\bdevis\b", r"\bquotation\b", r"\bproforma\b", r"\boffre\s+de\s+prix\b"
    ],
    "bon_livraison": [
        r"\bbon\s+de\s+livraison\b", r"\bdelivery\s+note\b", r"\bbl[-\s]?\d+\b"
    ],
}

# ─────────────────────────────────────────────────────────────────────
# CHAMPS PAR TYPE DE DOCUMENT
# ─────────────────────────────────────────────────────────────────────

CHAMPS_PAR_TYPE = {
    "facture":      ["numero_facture", "date", "echeance", "fournisseur", "client",
                     "reference_commande", "montant_ht", "tva", "montant_ttc",
                     "mode_paiement", "rib_iban"],
    "bon_commande": ["numero_commande", "date", "fournisseur", "client",
                     "reference_article", "quantite", "prix_unitaire",
                     "montant_ht", "tva", "montant_ttc", "delai_livraison"],
    "devis":        ["numero_devis", "date", "validite", "fournisseur", "client",
                     "reference_article", "quantite", "prix_unitaire",
                     "montant_ht", "tva", "montant_ttc"],
    "bon_livraison":["numero_bl", "date", "fournisseur", "client",
                     "reference_commande", "reference_article", "quantite"],
    "inconnu":      ["date", "fournisseur", "client", "montant_ht", "tva", "montant_ttc"],
}

# ─────────────────────────────────────────────────────────────────────
# PATTERNS D'EXTRACTION PAR CHAMP
# ─────────────────────────────────────────────────────────────────────

CHAMP_PATTERNS = {

    # ── Numéros de documents ──────────────────────────────────────────
    "numero_facture": [
        r"(?:facture|invoice)\s*n[°o]?\.?\s*[:\-]?\s*([A-Z0-9][\w\-/]{1,20})",
        r"n[°o]?\s*facture\s*[:\-]?\s*([A-Z0-9][\w\-/]{1,20})",
        r"\bfac[-\s]?(\d{3,})\b",
    ],
    "numero_commande": [
        r"(?:bon\s*de\s*commande|purchase\s*order|commande)\s*n[°o]?\.?\s*[:\-]?\s*([A-Z0-9][\w\-/]{1,20})",
        r"\b(?:bc|po)[-\s]?(\d{3,})\b",
    ],
    "numero_devis": [
        r"(?:devis|quotation|offre)\s*n[°o]?\.?\s*[:\-]?\s*([A-Z0-9][\w\-/]{1,20})",
        r"\bdev[-\s]?(\d{3,})\b",
    ],
    "numero_bl": [
        r"(?:bon\s*de\s*livraison|delivery\s*note)\s*n[°o]?\.?\s*[:\-]?\s*([A-Z0-9][\w\-/]{1,20})",
        r"\bbl[-\s]?(\d{3,})\b",
    ],

    # ── Dates ─────────────────────────────────────────────────────────
    "date": [
        r"(?:date\s*(?:de\s*)?(?:facture|émission|document)?)\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
        r"(?:^|\s)(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{4})(?:\s|$)",
        r"(\d{1,2}\s+(?:janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre)\s+\d{4})",
    ],
    "echeance": [
        r"(?:échéance|echeance|date\s*limite|due\s*date|payer\s*avant)\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
    ],
    "validite": [
        r"(?:valide?\s*(?:jusqu[''au]*)?|validity|expir[ae])\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
    ],
    "delai_livraison": [
        r"(?:délai|delai|livraison\s*(?:prévue)?|delivery\s*date)\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d+\s*(?:jours?|semaines?|days?))",
    ],

    # ── Parties ───────────────────────────────────────────────────────
    "fournisseur": [
        r"(?:fournisseur|vendor|supplier|émis\s*par|issued\s*by|de\s*la\s*part\s*de)\s*[:\-]?\s*([A-ZÀ-Ü][^\n]{3,60})",
        r"(?:raison\s*sociale|company\s*name)\s*[:\-]?\s*([A-ZÀ-Ü][^\n]{3,60})",
    ],
    "client": [
        r"(?:client|customer|bill\s*to|facturer\s*à|destinataire|adresser\s*à)\s*[:\-]?\s*([A-ZÀ-Ü][^\n]{3,60})",
        r"(?:vendu\s*à|sold\s*to)\s*[:\-]?\s*([A-ZÀ-Ü][^\n]{3,60})",
    ],

    # ── Références ────────────────────────────────────────────────────
    "reference_commande": [
        r"(?:référence\s*commande|ref\.?\s*commande|order\s*ref\.?|bc\s*n[°o]?)\s*[:\-]?\s*([A-Z0-9][\w\-/]{1,20})",
        r"(?:votre\s*(?:référence|commande))\s*[:\-]?\s*([A-Z0-9][\w\-/]{1,20})",
    ],
    "reference_article": [
        r"(?:référence\s*article|ref\.?\s*article|part\s*n[°o]?|article\s*n[°o]?)\s*[:\-]?\s*([A-Z0-9][\w\-/]{1,20})",
    ],

    # ── Quantités et prix ─────────────────────────────────────────────
    "quantite": [
        r"(?:quantité|qté|qty|quantity)\s*[:\-]?\s*(\d+(?:[.,]\d+)?(?:\s*(?:pcs?|unités?|kg|m|l))?)",
    ],
    "prix_unitaire": [
        r"(?:prix\s*unitaire|p\.?\s*u\.?|unit\s*price)\s*[:\-]?\s*([\d\s.,]+\s*(?:€|\$|TND|DT)?)",
    ],

    # ── Montants ──────────────────────────────────────────────────────
    "montant_ht": [
        r"(?:total\s*h\.?t\.?|montant\s*h\.?t\.?|sous[-\s]?total|hors\s*taxe|subtotal|net\s*amount)\s*[:\-]?\s*([\d\s.,]+\s*(?:€|\$|TND|DT)?)",
    ],
    "tva": [
        r"(?:tva|t\.v\.a\.?|vat|taxe)\s*(?:\(?\d+\s*%\)?)?\s*[:\-]?\s*([\d\s.,]+\s*(?:€|\$|TND|DT)?)",
    ],
    "montant_ttc": [
        r"(?:total\s*t\.?t\.?c\.?|montant\s*t\.?t\.?c\.?|total\s*(?:à\s*payer|due|general)|net\s*à\s*payer|amount\s*due)\s*[:\-]?\s*([\d\s.,]+\s*(?:€|\$|TND|DT)?)",
        r"(?:total)\s*[:\-]?\s*([\d\s.,]+\s*(?:€|\$|TND|DT))",
    ],

    # ── Paiement ──────────────────────────────────────────────────────
    "mode_paiement": [
        r"(?:mode\s*(?:de\s*)?paiement|payment\s*method|règlement|payment\s*terms)\s*[:\-]?\s*([^\n]{3,40})",
    ],
    "rib_iban": [
        r"(?:rib|iban|compte\s*bancaire|bank\s*account)\s*[:\-]?\s*([A-Z]{2}\d{2}[\w\s]{10,30}|\d[\d\s]{15,30})",
    ],
}


# ─────────────────────────────────────────────────────────────────────
# FONCTIONS PRINCIPALES
# ─────────────────────────────────────────────────────────────────────

def detecter_type_document(texte: str) -> str:
    """Détecte le type du document à partir du texte OCR."""
    texte_lower = texte.lower()
    scores = {t: 0 for t in TYPE_PATTERNS}

    for type_doc, patterns in TYPE_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, texte_lower, re.IGNORECASE):
                scores[type_doc] += 1

    meilleur = max(scores, key=scores.get)
    return meilleur if scores[meilleur] > 0 else "inconnu"


def _est_faux_positif_partie(valeur: str) -> bool:
    """
    Filtre les faux positifs fréquents dans les tableaux de taxes.
    Ex: FODEC, Base TVA, Timbre fiscal, Total taxes...
    """
    v = (valeur or "").strip().lower()
    if not v:
        return True

    mots_interdits = [
        "fodec", "base tva", "tva", "timbre", "taxe", "taxes",
        "total ht", "total ttc", "total taxe", "montant", "remise",
        "article", "quantite", "designation"
    ]
    if any(m in v for m in mots_interdits):
        return True

    # Chaîne surtout numérique/punctuation → pas une partie prenante
    if re.fullmatch(r"[\d\s\W_]+", v):
        return True

    return False


def extraire_champ(texte: str, champ: str) -> str | None:
    """
    Extrait un champ précis du texte.
    Retourne la valeur nettoyée, ou None si non trouvé.
    """
    patterns = CHAMP_PATTERNS.get(champ, [])

    for pattern in patterns:
        match = re.search(pattern, texte, re.IGNORECASE | re.MULTILINE)
        if match:
            valeur = match.group(1).strip()
            # Nettoyage basique
            valeur = re.sub(r'\s+', ' ', valeur)
            valeur = valeur.strip(" .,;:")

            # Anti faux positifs sur fournisseur/client
            if champ in {"fournisseur", "client"} and _est_faux_positif_partie(valeur):
                continue

            if valeur:
                return valeur

    return None


def extraire_champs_entete(texte: str) -> dict:
    """
    Fonction principale — appelée par ocr_pipeline.py.

    Analyse le texte OCR, détecte le type de document,
    puis tente d'extraire chaque champ pertinent.
    Les champs non trouvés ont la valeur None (formulaire laissé vide).

    Args:
        texte: texte brut extrait par OCR

    Returns:
        {
            "type_document"   : str,
            "champs"          : { champ: valeur | None },
            "champs_trouves"  : [str, ...],
            "champs_manquants": [str, ...]
        }
    """
    type_doc = detecter_type_document(texte)
    champs_a_extraire = CHAMPS_PAR_TYPE.get(type_doc, CHAMPS_PAR_TYPE["inconnu"])

    tous_champs    = {}
    champs_trouves = []
    champs_manquants = []

    for champ in champs_a_extraire:
        valeur = extraire_champ(texte, champ)
        tous_champs[champ] = valeur
        if valeur is not None:
            champs_trouves.append(champ)
        else:
            champs_manquants.append(champ)

    return {
        "type_document":    type_doc,
        "champs":           tous_champs,
        "champs_trouves":   champs_trouves,
        "champs_manquants": champs_manquants,
    }