"""
auto_create_document.py - Groupe Bayoudh Metal

CORRECTION : valeurs de statut alignées sur le doctype OCR Document
  Options valides : "En attente" | "En cours" | "Validation requise" | "Validé" | "Rejeté"
  ERREUR corrigée : "Validé automatiquement" → "Validé"
                    "Avertissement"          → "Validation requise"
"""

import frappe
import os
import json


def auto_create_ocr_document(doc, method):
    """
    Hook after_insert sur File.
    Crée automatiquement un OCR Document après chaque upload de fichier.
    """

    extensions_acceptees = [".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".bmp"]
    ext = os.path.splitext(doc.file_name or "")[1].lower()
    if ext not in extensions_acceptees:
        return

    try:
        chemin = _get_chemin_fichier(doc)
        if not chemin or not os.path.exists(chemin):
            frappe.log_error(f"Fichier introuvable : {chemin}", "OCR Auto Create")
            return

        from ocr_intelligent.ocr.ocr_engine import OCREngine
        from ocr_intelligent.ocr.extractor import ExtracteurIntelligent
        from ocr_intelligent.ocr.validator import Validateur

        engine     = OCREngine()
        res_ocr    = engine.extraire_texte(chemin)
        texte_brut = res_ocr["texte"]
        score      = res_ocr["score_confiance"]

        extracteur = ExtracteurIntelligent(texte_brut)
        donnees    = extracteur.extraire_tout()
        type_doc   = donnees["type_document"]
        champs     = donnees["champs"]

        validateur = Validateur(type_doc, champs)
        rapport    = validateur.valider()

        # ── Valeurs alignées sur les options du Select dans le doctype ──
        statut_map = {
            "valide":             "Validé",             # était "Validé automatiquement" → ERREUR
            "validation_requise": "Validation requise",
            "avertissement":      "Validation requise", # était "Avertissement" → ERREUR
        }
        statut = statut_map.get(rapport["statut"], "En attente")

        ocr_doc = frappe.get_doc({
            "doctype":          "OCR Document",
            "document_name":    doc.file_name,
            "file_url":         doc.file_url,
            "uploaded_by":      frappe.session.user,
            "confidence_score": score,
            "extracted_text":   texte_brut,
            "extracted_fields": json.dumps(rapport["champs_valides"], ensure_ascii=False, indent=2),
            "status":           statut,
        })
        ocr_doc.insert(ignore_permissions=True)
        frappe.db.commit()

        frappe.msgprint(
            f"Document '{doc.file_name}' traité.<br>"
            f"Type : <b>{type_doc}</b> | Score : <b>{score}%</b> | Statut : <b>{statut}</b>",
            title="OCR Intelligent",
            indicator="green" if statut == "Validé" else "orange"
        )

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "OCR Auto Create")


def _get_chemin_fichier(doc):
    site_path = frappe.get_site_path()
    if doc.file_url:
        nom = doc.file_url.replace("/private/files/", "").replace("/files/", "")
        for c in [
            os.path.join(site_path, "private", "files", nom),
            os.path.join(site_path, "public",  "files", nom),
        ]:
            if os.path.exists(c):
                return c
    return None