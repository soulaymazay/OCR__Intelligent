import frappe
import re
import json
from ocr_intelligent.ocr.extractor import extract_field_candidates


@frappe.whitelist()
def match_and_fill(ocr_document_name: str, target_doctype: str, target_docname: str = None):
    """
    Endpoint: /api/method/ocr_intelligent.api.field_extractor.match_and_fill

    Compare le texte extrait d'un OCR Document avec les champs
    du doctype cible et retourne les valeurs correspondantes.
    """
    # ── 1. Récupérer le texte OCR ────────────────────────────────────────────
    ocr_doc = frappe.get_doc("OCR Document", ocr_document_name)

    # ✅ CORRECTION : utiliser le bon nom de champ du DocType
    structured_text = (
        getattr(ocr_doc, "texte_extrait", None)      # champ FR
        or getattr(ocr_doc, "extracted_text", None)   # champ EN
        or getattr(ocr_doc, "structured_text", None)  # ancien nom
        or getattr(ocr_doc, "raw_text", None)          # fallback
    )

    if not structured_text:
        frappe.throw("Le document OCR ne contient pas de texte extrait.")

    # ── 2. Extraire les candidats depuis le texte ────────────────────────────
    text_candidates = extract_field_candidates(structured_text)

    # Enrichir avec les patterns spécifiques tunisiens / factures
    text_candidates.update(_extraire_patterns_avances(structured_text))

    # ── 3. Récupérer les champs du doctype cible ─────────────────────────────
    meta = frappe.get_meta(target_doctype)
    target_fields = {
        df.fieldname: {
            "label":     df.label or df.fieldname,
            "fieldtype": df.fieldtype,
        }
        for df in meta.fields
        if df.fieldtype not in [
            "Section Break", "Column Break", "Tab Break",
            "HTML", "Button", "Heading"
        ]
    }

    # ── 4. Matching : comparer clés OCR ↔ champs formulaire ─────────────────
    matched_fields   = {}
    unmatched_fields = []
    warnings         = []

    for fieldname, field_info in target_fields.items():
        label_norm     = _normalize(field_info["label"])
        fieldname_norm = _normalize(fieldname)
        best_match     = None

        for ocr_key, ocr_value in text_candidates.items():
            ocr_key_norm = _normalize(ocr_key)

            # Correspondance exacte
            if ocr_key_norm in (label_norm, fieldname_norm):
                best_match = ocr_value
                break
            # Correspondance partielle
            elif ocr_key_norm in label_norm or label_norm in ocr_key_norm:
                best_match = ocr_value

        if best_match:
            validated, warning = _validate_field_value(
                best_match, field_info["fieldtype"], fieldname
            )
            if validated is not None:
                matched_fields[fieldname] = validated
            if warning:
                warnings.append(warning)
        else:
            unmatched_fields.append(fieldname)

    # ── 5. Calcul confiance ──────────────────────────────────────────────────
    total_fields  = len(target_fields)
    matched_count = len(matched_fields)
    confidence    = int((matched_count / total_fields) * 100) if total_fields > 0 else 0

    if confidence < 30:
        warnings.append(
            f"Faible correspondance ({confidence}%). "
            "Vérifiez que le bon fichier est sélectionné."
        )

    # ── 6. Mettre à jour le document cible si demandé ───────────────────────
    if target_docname and matched_fields and confidence >= 30:
        _apply_to_document(target_doctype, target_docname, matched_fields)

    # ── 7. ✅ CORRECTION : sauvegarder les champs extraits dans OCR Document ─
    _sauvegarder_champs_extraits(ocr_doc, matched_fields, text_candidates, confidence)

    return {
        "success":         True,
        "matched_fields":  matched_fields,
        "unmatched_fields": unmatched_fields,
        "confidence":      confidence,
        "warnings":        warnings,
        "ocr_document":    ocr_document_name,
        "candidats_detectes": text_candidates,
    }


# ════════════════════════════════════════════════════════════════════════════
#  EXTRACTION AVANCÉE — patterns spécifiques (factures TN, entreprises)
# ════════════════════════════════════════════════════════════════════════════

def _extraire_patterns_avances(texte: str) -> dict:
    """
    Extrait automatiquement les champs courants d'une facture tunisienne
    même sans labels explicites dans le texte.
    """
    candidats = {}

    patterns = {
        # Matricule Fiscal tunisien : ex. MF: 1234567/A/B/C/000
        "matricule_fiscal": [
            r"MF\s*[:\-]?\s*([A-Z0-9]{7,20})",
            r"Matricule\s*Fiscal\s*[:\-]?\s*([A-Z0-9\/]{7,20})",
        ],
        # Téléphone tunisien
        "telephone": [
            r"(?:Tél|Tel|TEL|Wel|GSM)\s*[:\-]?\s*((?:\+216\s?)?[2-9]\d{7})",
            r"\b((?:\+216[\s\-]?)?[2-9]\d[\s\-]?\d{3}[\s\-]?\d{3})\b",
        ],
        # Email
        "email": [
            r"([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})",
        ],
        # Numéro de facture
        "numero_facture": [
            r"(?:Facture|Fact|FAC|Invoice)\s*N[°o]?\s*[:\-]?\s*([A-Z0-9\/\-]{3,20})",
        ],
        # Date
        "date": [
            r"(?:Date|DATE)\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
            r"\b(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{4})\b",
        ],
        # Montant total
        "montant_total": [
            r"(?:Total|TOTAL|Montant\s*Total)\s*[:\-]?\s*([\d\s,\.]+)\s*(?:DT|TND|د\.ت)?",
        ],
        # TVA
        "tva": [
            r"(?:TVA|T\.V\.A)\s*[:\-]?\s*([\d,\.]+)\s*%?",
        ],
        # Nom société (ligne en majuscules au début)
        "nom_societe": [
            r"^([A-Z][A-Z\s&\.]{5,50})$",
        ],
        # Adresse
        "adresse": [
            r"(?:Zone|Rue|Avenue|Route|Cité|BP)\s+[A-Za-zÀ-ÿ\s\-\d,]+",
        ],
        # Code postal tunisien
        "code_postal": [
            r"\b(\d{4})\s+(?:Tunis|Sfax|Sousse|Monastir|Nabeul|Bizerte|Kairouan)",
        ],
    }

    for champ, liste_patterns in patterns.items():
        for pattern in liste_patterns:
            match = re.search(pattern, texte, re.MULTILINE | re.IGNORECASE)
            if match:
                valeur = match.group(1).strip()
                if valeur:
                    candidats[champ] = valeur
                    break  # Premier match suffit pour ce champ

    return candidats


# ════════════════════════════════════════════════════════════════════════════
#  SAUVEGARDE DANS OCR DOCUMENT
# ════════════════════════════════════════════════════════════════════════════

def _sauvegarder_champs_extraits(ocr_doc, matched_fields: dict,
                                  candidats: dict, confidence: int):
    """
    Sauvegarde les résultats dans le OCR Document.
    Utilise db_set() pour éviter les déclenchements de hooks.
    """
    try:
        # Fusion matched + candidats bruts
        tous_champs = {**candidats, **matched_fields}
        champs_json = json.dumps(tous_champs, ensure_ascii=False, indent=2)

        updates = {}

        # ✅ Tenter les différents noms de champs possibles
        champs_possibles_extraits = [
            "extracted_fields", "champs_extraits",
            "champs_extraits_json", "fields_json"
        ]
        champs_possibles_statut = [
            "status", "statut"
        ]

        meta_fields = [df.fieldname for df in frappe.get_meta("OCR Document").fields]

        for nom in champs_possibles_extraits:
            if nom in meta_fields:
                updates[nom] = champs_json
                break

        for nom in champs_possibles_statut:
            if nom in meta_fields:
                updates[nom] = "Validé" if confidence >= 30 else "Non correspondant"
                break

        for fieldname, value in updates.items():
            ocr_doc.db_set(fieldname, value)

        frappe.db.commit()

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "OCR Save Extracted Fields Error")
        frappe.logger().warning(f"[OCR] Impossible de sauvegarder champs extraits : {e}")


# ════════════════════════════════════════════════════════════════════════════
#  UTILITAIRES
# ════════════════════════════════════════════════════════════════════════════

def _normalize(text: str) -> str:
    """Normalise un texte pour comparaison (minuscules, sans accents)."""
    if not text:
        return ""
    text = text.lower().strip()
    remplacements = {
        'é': 'e', 'è': 'e', 'ê': 'e', 'ë': 'e',
        'à': 'a', 'â': 'a', 'ä': 'a',
        'ù': 'u', 'û': 'u', 'ü': 'u',
        'î': 'i', 'ï': 'i',
        'ô': 'o', 'ö': 'o',
        'ç': 'c', ' ': '_'
    }
    for char, remplacement in remplacements.items():
        text = text.replace(char, remplacement)
    return re.sub(r'[^a-z0-9_]', '', text)


def _validate_field_value(value: str, fieldtype: str, fieldname: str):
    """Valide et convertit une valeur selon le type de champ Frappe."""
    value = str(value).strip()

    if fieldtype in ["Data", "Small Text", "Text", "Long Text", "Code"]:
        return value, None

    elif fieldtype == "Int":
        digits = re.sub(r'[^\d]', '', value)
        if digits:
            return int(digits), None
        return None, f"Champ '{fieldname}' : '{value}' non numérique ignoré."

    elif fieldtype in ["Float", "Currency"]:
        numeric = re.sub(r'[^\d,\.]', '', value).replace(',', '.')
        try:
            return float(numeric), None
        except ValueError:
            return None, f"Champ '{fieldname}' : '{value}' non convertible en nombre."

    elif fieldtype == "Date":
        date_match = re.search(
            r'(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{2,4})', value
        )
        if date_match:
            day, month, year = date_match.groups()
            if len(year) == 2:
                year = "20" + year
            return f"{year}-{month.zfill(2)}-{day.zfill(2)}", None
        return None, f"Champ '{fieldname}' : format date '{value}' non reconnu."

    elif fieldtype == "Check":
        affirmative = ['oui', 'yes', '1', 'true', 'vrai']
        return 1 if value.lower() in affirmative else 0, None

    return value, None


def _apply_to_document(doctype: str, docname: str, fields: dict):
    """Met à jour un document Frappe existant avec les champs matchés."""
    try:
        doc = frappe.get_doc(doctype, docname)
        for fieldname, value in fields.items():
            if hasattr(doc, fieldname):
                setattr(doc, fieldname, value)
        doc.save(ignore_permissions=True)
        frappe.db.commit()
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "OCR Field Apply Error")
        frappe.throw(f"Erreur lors de la mise à jour du document : {str(e)}")


# ════════════════════════════════════════════════════════════════════════════
#  API UTILITAIRE — diagnostic des champs disponibles
# ════════════════════════════════════════════════════════════════════════════

@frappe.whitelist()
def diagnostiquer_ocr_document():
    """
    Retourne la liste des champs disponibles dans OCR Document.
    Utile pour débogage.

    Appel :
        /api/method/ocr_intelligent.api.field_extractor.diagnostiquer_ocr_document
    """
    meta = frappe.get_meta("OCR Document")
    return {
        "champs": [
            {
                "fieldname": df.fieldname,
                "label":     df.label,
                "fieldtype": df.fieldtype,
            }
            for df in meta.fields
        ]
    }