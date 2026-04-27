import frappe
import re
import json
import os
from ocr_intelligent.ocr.extractor import extract_field_candidates

# ── Import du module de paiement spécialisé ──────────────────────────────────
try:
    from ocr_intelligent.ocr.payment_doc_extractor import (
        analyser_document_paiement,
        _normaliser_payment_method,
    )
    _PAYMENT_MODULE_AVAILABLE = True
except ImportError:
    _PAYMENT_MODULE_AVAILABLE = False
    frappe.logger().warning(
        "[OCR] payment_doc_extractor non disponible — "
        "les documents de paiement seront traités par le pipeline générique."
    )

# ── Types de paiement reconnus comme documents spécialisés ───────────────────
_PAYMENT_METHODS = {"chèque", "cheque", "check", "traite", "lettre de change", "draft"}


# ════════════════════════════════════════════════════════════════════════════
#  POINT D'ENTRÉE PRINCIPAL
# ════════════════════════════════════════════════════════════════════════════

@frappe.whitelist()
def match_and_fill(ocr_document_name: str, target_doctype: str, target_docname: str = None):
    """
    Endpoint: /api/method/ocr_intelligent.api.field_extractor.match_and_fill

    Pipeline :
      1. Récupère le texte OCR et les métadonnées du document
      2. Si payment_method détecté → pipeline spécialisé (chèque / traite)
         Sinon → pipeline générique (factures, etc.)
      3. Matche les candidats extraits avec les champs du doctype cible
      4. Met à jour le document cible si demandé
      5. Sauvegarde les résultats dans OCR Document
    """

    # ── 1. Récupérer le document OCR ─────────────────────────────────────────
    ocr_doc = frappe.get_doc("OCR Document", ocr_document_name)

    structured_text = (
        getattr(ocr_doc, "texte_extrait", None)
        or getattr(ocr_doc, "extracted_text", None)
        or getattr(ocr_doc, "structured_text", None)
        or getattr(ocr_doc, "raw_text", None)
    )

    if not structured_text:
        frappe.throw("Le document OCR ne contient pas de texte extrait.")

    # ── 2. Déterminer le mode de traitement ──────────────────────────────────
    payment_method = (
        getattr(ocr_doc, "payment_method", None)
        or getattr(ocr_doc, "mode_paiement", None)
        or ""
    )
    is_payment_doc = (
        _PAYMENT_MODULE_AVAILABLE
        and _normaliser_payment_method(payment_method) is not None
    )

    # ── 3A. Pipeline PAIEMENT (chèque / traite) ──────────────────────────────
    if is_payment_doc:
        return _pipeline_paiement(
            ocr_doc         = ocr_doc,
            structured_text = structured_text,
            payment_method  = payment_method,
            target_doctype  = target_doctype,
            target_docname  = target_docname,
        )

    # ── 3B. Pipeline GÉNÉRIQUE (factures, etc.) ──────────────────────────────
    return _pipeline_generique(
        ocr_doc         = ocr_doc,
        structured_text = structured_text,
        target_doctype  = target_doctype,
        target_docname  = target_docname,
    )


# ════════════════════════════════════════════════════════════════════════════
#  PIPELINE PAIEMENT — chèque / traite
# ════════════════════════════════════════════════════════════════════════════

def _pipeline_paiement(
    ocr_doc, structured_text: str,
    payment_method: str, target_doctype: str, target_docname: str
) -> dict:
    """
    Analyse un document de paiement (chèque ou traite) via payment_doc_extractor,
    puis matche les champs extraits avec le doctype cible.
    """
    warnings  = []
    chemin_img = _resoudre_chemin_fichier(ocr_doc)
    score_ocr  = int(getattr(ocr_doc, "ocr_score", 0) or 0)

    # ── Appel du moteur spécialisé ────────────────────────────────────────────
    resultat = analyser_document_paiement(
        chemin_img     = chemin_img,
        texte_ocr      = structured_text,
        payment_method = payment_method,
        score_ocr      = score_ocr,
    )

    # ── Document invalide → retour d'erreur immédiat ──────────────────────────
    if not resultat["valid"]:
        erreur_msg = " | ".join(resultat.get("errors", ["Document non valide."]))
        _marquer_document_invalide(ocr_doc, erreur_msg)
        frappe.throw(erreur_msg)

    # ── Champs extraits par le moteur ─────────────────────────────────────────
    champs_remplis  = resultat.get("champs_remplis") or {}
    form_fields     = resultat.get("form_fields") or {}
    champs_incertains = resultat.get("uncertain_fields") or []
    confiance_ocr   = resultat.get("confiance_globale", 0.0)

    if resultat.get("image_enhanced"):
        warnings.append("Image améliorée automatiquement avant extraction.")

    for champ in champs_incertains:
        warnings.append(f"Champ '{champ}' extrait avec faible confiance — à vérifier.")

    # ── Validation des valeurs selon les types Frappe ─────────────────────────
    meta           = frappe.get_meta(target_doctype)
    target_fields  = _get_target_fields(meta)
    # Construire form_fields_frappe : clés OCR converties en clés Frappe pour le fallback
    _MAPPING_COMBINE = {
        "numero_cheque":    "reference_no",
        "date_cheque":      "reference_date",
        "cheque_date":      "reference_date",
        "amount":           "paid_amount",
        "banque":           "bank",
        "beneficiaire":     "party",
        "titulaire_compte": "account_holder_name",
        "rib":              "bank_account",
        "numero_traite":    "reference_no",
        "date_echeance":    "reference_date",
        "tire":             "bank",
        "tireur":           "party",
        "domiciliation":    "custom_domiciliation",
        "date_emission":    "custom_issue_date",
        "due_date":         "reference_date",
        "issue_date":       "custom_issue_date",
        "drawer":           "party",
        "drawee":           "bank",
        "draft_number":     "reference_no",
    }
    form_fields_frappe = {}
    for _ocr_key, _frappe_key in _MAPPING_COMBINE.items():
        _val = form_fields.get(_ocr_key)
        if _val not in (None, "", 0, 0.0) and _frappe_key not in form_fields_frappe:
            form_fields_frappe[_frappe_key] = _val

    matched_fields = {}
    unmatched      = []

    for fieldname, field_info in target_fields.items():
        valeur = champs_remplis.get(fieldname)

        # Fallback : chercher dans form_fields converti en clés Frappe
        if valeur is None:
            valeur = form_fields_frappe.get(fieldname)

        if valeur is not None and valeur != "" and valeur != 0.0:
            validated, warning = _validate_field_value(
                str(valeur), field_info["fieldtype"], fieldname
            )
            if validated is not None:
                matched_fields[fieldname] = validated
            if warning:
                warnings.append(warning)
        else:
            unmatched.append(fieldname)

    # ── Calcul confiance finale ───────────────────────────────────────────────
    total      = len(target_fields)
    n_matched  = len(matched_fields)
    confidence = int((n_matched / total) * 100) if total > 0 else int(confiance_ocr * 100)

    if confidence < 30:
        warnings.append(
            f"Faible correspondance ({confidence}%). "
            "Vérifiez que le bon fichier est sélectionné."
        )

    # ── Appliquer au document cible ───────────────────────────────────────────
    if target_docname and matched_fields and confidence >= 30:
        _apply_to_document(target_doctype, target_docname, matched_fields)

    # ── Sauvegarder dans OCR Document ────────────────────────────────────────
    tous_candidats = {**form_fields, **champs_remplis}
    _sauvegarder_champs_extraits(ocr_doc, matched_fields, tous_candidats, confidence)

    return {
        "success":               True,
        "pipeline":              "paiement",
        "document_type_detected": resultat.get("document_type_detected"),
        "matched_fields":        matched_fields,
        "unmatched_fields":      unmatched,
        "confidence":            confidence,
        "confiance_ocr":         round(confiance_ocr * 100, 1),
        "warnings":              warnings,
        "ocr_document":          ocr_doc.name,
        "date_cheque_retenue":   resultat.get("date_cheque_retenue"),
        "candidats_detectes":    tous_candidats,
    }


# ════════════════════════════════════════════════════════════════════════════
#  PIPELINE GÉNÉRIQUE — factures, documents divers
# ════════════════════════════════════════════════════════════════════════════

def _pipeline_generique(
    ocr_doc, structured_text: str,
    target_doctype: str, target_docname: str
) -> dict:
    """
    Pipeline générique : extraction par patterns + matching label/fieldname.
    """
    warnings = []

    # ── Extraction des candidats ──────────────────────────────────────────────
    text_candidates = extract_field_candidates(structured_text)
    text_candidates.update(_extraire_patterns_avances(structured_text))

    # ── Matching ─────────────────────────────────────────────────────────────
    meta           = frappe.get_meta(target_doctype)
    target_fields  = _get_target_fields(meta)
    matched_fields = {}
    unmatched      = []

    for fieldname, field_info in target_fields.items():
        label_norm     = _normalize(field_info["label"])
        fieldname_norm = _normalize(fieldname)
        best_match     = None

        for ocr_key, ocr_value in text_candidates.items():
            ocr_key_norm = _normalize(ocr_key)
            if ocr_key_norm in (label_norm, fieldname_norm):
                best_match = ocr_value
                break
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
            unmatched.append(fieldname)

    # ── Calcul confiance ──────────────────────────────────────────────────────
    total      = len(target_fields)
    n_matched  = len(matched_fields)
    confidence = int((n_matched / total) * 100) if total > 0 else 0

    if confidence < 30:
        warnings.append(
            f"Faible correspondance ({confidence}%). "
            "Vérifiez que le bon fichier est sélectionné."
        )

    # ── Appliquer + sauvegarder ───────────────────────────────────────────────
    if target_docname and matched_fields and confidence >= 30:
        _apply_to_document(target_doctype, target_docname, matched_fields)

    _sauvegarder_champs_extraits(ocr_doc, matched_fields, text_candidates, confidence)

    return {
        "success":            True,
        "pipeline":           "generique",
        "matched_fields":     matched_fields,
        "unmatched_fields":   unmatched,
        "confidence":         confidence,
        "warnings":           warnings,
        "ocr_document":       ocr_doc.name,
        "candidats_detectes": text_candidates,
    }


# ════════════════════════════════════════════════════════════════════════════
#  EXTRACTION AVANCÉE — patterns factures TN
# ════════════════════════════════════════════════════════════════════════════

def _extraire_patterns_avances(texte: str) -> dict:
    """Extrait les champs courants d'une facture tunisienne."""
    candidats = {}

    patterns = {
        "matricule_fiscal": [
            r"MF\s*[:\-]?\s*([A-Z0-9]{7,20})",
            r"Matricule\s*Fiscal\s*[:\-]?\s*([A-Z0-9\/]{7,20})",
        ],
        "telephone": [
            r"(?:Tél|Tel|TEL|GSM)\s*[:\-]?\s*((?:\+216\s?)?[2-9]\d{7})",
            r"\b((?:\+216[\s\-]?)?[2-9]\d[\s\-]?\d{3}[\s\-]?\d{3})\b",
        ],
        "email": [
            r"([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})",
        ],
        "numero_facture": [
            r"(?:Facture|Fact|FAC|Invoice)\s*N[°o]?\s*[:\-]?\s*([A-Z0-9\/\-]{3,20})",
        ],
        "date": [
            r"(?:Date|DATE)\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
            r"\b(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{4})\b",
        ],
        "montant_total": [
            r"(?:Total|TOTAL|Montant\s*Total)\s*[:\-]?\s*([\d\s,\.]+)\s*(?:DT|TND|د\.ت)?",
        ],
        "tva": [
            r"(?:TVA|T\.V\.A)\s*[:\-]?\s*([\d,\.]+)\s*%?",
        ],
        "nom_societe": [
            r"^([A-Z][A-Z\s&\.]{5,50})$",
        ],
        "adresse": [
            r"(?:Zone|Rue|Avenue|Route|Cité|BP)\s+[A-Za-zÀ-ÿ\s\-\d,]+",
        ],
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
                    break

    return candidats


# ════════════════════════════════════════════════════════════════════════════
#  SAUVEGARDE DANS OCR DOCUMENT
# ════════════════════════════════════════════════════════════════════════════

def _sauvegarder_champs_extraits(
    ocr_doc, matched_fields: dict, candidats: dict, confidence: int
):
    """Sauvegarde les résultats dans le OCR Document via db_set()."""
    try:
        tous_champs  = {**candidats, **matched_fields}
        champs_json  = json.dumps(tous_champs, ensure_ascii=False, indent=2)
        meta_fields  = [df.fieldname for df in frappe.get_meta("OCR Document").fields]
        updates      = {}

        # Ordre de priorité : nom exact du champ doctype en premier
        for nom in ["extracted_field", "extracted_fields", "champs_extraits", "champs_extraits_json", "fields_json"]:
            if nom in meta_fields:
                updates[nom] = champs_json
                break

        for nom in ["status", "statut"]:
            if nom in meta_fields:
                updates[nom] = "Validé" if confidence >= 30 else "Non correspondant"
                break

        for fieldname, value in updates.items():
            ocr_doc.db_set(fieldname, value)

        frappe.db.commit()

    except Exception:
        frappe.log_error(frappe.get_traceback(), "OCR Save Extracted Fields Error")
        frappe.logger().warning("[OCR] Impossible de sauvegarder champs extraits.")


# ════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════════════════════════════════

def _get_target_fields(meta) -> dict:
    """Retourne les champs utiles du doctype cible (sans les breaks/buttons)."""
    return {
        df.fieldname: {
            "label":     df.label or df.fieldname,
            "fieldtype": df.fieldtype,
        }
        for df in meta.fields
        if df.fieldtype not in [
            "Section Break", "Column Break", "Tab Break",
            "HTML", "Button", "Heading",
        ]
    }


def _resoudre_chemin_fichier(ocr_doc) -> str:
    """
    Résout le chemin absolu du fichier attaché au OCR Document.
    Cherche dans plusieurs attributs possibles.
    """
    file_url = (
        getattr(ocr_doc, "file_url", None)
        or getattr(ocr_doc, "fichier", None)
        or getattr(ocr_doc, "attached_file", None)
        or getattr(ocr_doc, "image", None)
        or ""
    )

    if not file_url:
        return ""

    # Chemin déjà absolu
    if os.path.isabs(file_url) and os.path.exists(file_url):
        return file_url

    # URL Frappe → chemin absolu sur le serveur
    # Ex: /files/cheque.jpg → /home/frappe/frappe-bench/sites/site1.local/public/files/cheque.jpg
    if file_url.startswith("/files/") or file_url.startswith("/private/files/"):
        site_path = frappe.get_site_path()
        chemin = os.path.join(site_path, "public" + file_url)
        if os.path.exists(chemin):
            return chemin
        # Essai sans "public"
        chemin2 = os.path.join(site_path, file_url.lstrip("/"))
        if os.path.exists(chemin2):
            return chemin2

    return file_url  # retourner tel quel en dernier recours


def _marquer_document_invalide(ocr_doc, message: str):
    """Met à jour le statut du OCR Document en cas d'échec."""
    try:
        meta_fields = [df.fieldname for df in frappe.get_meta("OCR Document").fields]
        for nom in ["status", "statut"]:
            if nom in meta_fields:
                ocr_doc.db_set(nom, "Non valide")
                break
        for nom in ["error_message", "message_erreur", "remarks"]:
            if nom in meta_fields:
                ocr_doc.db_set(nom, message[:500])
                break
        frappe.db.commit()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "OCR Mark Invalid Error")


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
        'ç': 'c', ' ': '_',
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
        # Gestion format tunisien : virgule = décimal si 2-3 chiffres après
        s = re.sub(r'[^\d,\.]', '', value)
        has_comma = ',' in s
        has_dot   = '.' in s
        if has_comma and has_dot:
            # ex: 2.520,000 → virgule = décimal
            if s.rfind(',') > s.rfind('.'):
                s = s.replace('.', '').replace(',', '.')
            else:
                s = s.replace(',', '')
        elif has_comma:
            parts = s.split(',')
            if len(parts) == 2 and len(parts[1]) in (2, 3):
                s = s.replace(',', '.')
            else:
                s = s.replace(',', '')
        try:
            return float(s), None
        except ValueError:
            return None, f"Champ '{fieldname}' : '{value}' non convertible en nombre."

    elif fieldtype == "Date":
        date_match = re.search(r'(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{2,4})', value)
        if date_match:
            day, month, year = date_match.groups()
            if len(year) == 2:
                year = "20" + year
            return f"{year}-{month.zfill(2)}-{day.zfill(2)}", None
        return None, f"Champ '{fieldname}' : format date '{value}' non reconnu."

    elif fieldtype == "Check":
        return 1 if value.lower() in ['oui', 'yes', '1', 'true', 'vrai'] else 0, None

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
    except Exception:
        frappe.log_error(frappe.get_traceback(), "OCR Field Apply Error")
        frappe.throw(f"Erreur lors de la mise à jour du document {doctype}/{docname}.")


# ════════════════════════════════════════════════════════════════════════════
#  API UTILITAIRE — diagnostic
# ════════════════════════════════════════════════════════════════════════════

@frappe.whitelist()
def diagnostiquer_ocr_document():
    """
    Retourne la liste des champs disponibles dans OCR Document.
    GET /api/method/ocr_intelligent.api.field_extractor.diagnostiquer_ocr_document
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
        ],
        "payment_module_available": _PAYMENT_MODULE_AVAILABLE,
    }


@frappe.whitelist()
def tester_pipeline_paiement(ocr_document_name: str):
    """
    Teste uniquement le moteur payment_doc_extractor sur un OCR Document,
    sans toucher au document cible.
    GET /api/method/ocr_intelligent.api.field_extractor.tester_pipeline_paiement
    """
    if not _PAYMENT_MODULE_AVAILABLE:
        frappe.throw("Module payment_doc_extractor non disponible.")

    ocr_doc = frappe.get_doc("OCR Document", ocr_document_name)
    structured_text = (
        getattr(ocr_doc, "texte_extrait", None)
        or getattr(ocr_doc, "extracted_text", None)
        or getattr(ocr_doc, "structured_text", None)
        or getattr(ocr_doc, "raw_text", None)
        or ""
    )
    payment_method = (
        getattr(ocr_doc, "payment_method", None)
        or getattr(ocr_doc, "mode_paiement", None)
        or ""
    )
    chemin_img = _resoudre_chemin_fichier(ocr_doc)
    score_ocr  = int(getattr(ocr_doc, "ocr_score", 0) or 0)

    resultat = analyser_document_paiement(
        chemin_img     = chemin_img,
        texte_ocr      = structured_text,
        payment_method = payment_method,
        score_ocr      = score_ocr,
    )

    return {
        "ocr_document":  ocr_document_name,
        "chemin_img":    chemin_img,
        "payment_method_detecte": resultat.get("document_type_detected"),
        "resultat":      resultat,
    }