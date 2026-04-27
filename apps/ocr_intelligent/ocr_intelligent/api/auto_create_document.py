# -*- coding: utf-8 -*-
import frappe
import os
import json
import time
import hashlib

EXTENSIONS_ACCEPTEES = frozenset([".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".bmp"])

def _is_pdf_textuel(path):
    try:
        import fitz
        doc = fitz.open(path)
        text = doc[0].get_text().strip()
        return len(text) > 50
    except:
        return False

def auto_create_ocr_document(doc, method):
    ext = os.path.splitext(doc.file_name or "")[1].lower()
    if ext not in EXTENSIONS_ACCEPTEES:
        return

    if frappe.db.exists("OCR Document", {"file_url": doc.file_url}):
        return

    chemin = _get_chemin_fichier(doc)
    if not chemin:
        return

    ocr_doc = frappe.get_doc({
        "doctype": "OCR Document",
        "document_name": doc.file_name,
        "file_url": doc.file_url,
        "uploaded_by": frappe.session.user,
        "status": "En attente",
    })
    ocr_doc.insert(ignore_permissions=True)

    frappe.enqueue(
        "ocr_intelligent.api.auto_create_document.traiter_ocr_en_arriere_plan",
        queue="short",
        timeout=120,
        ocr_doc_name=ocr_doc.name,
        chemin=chemin,
    )

def traiter_ocr_en_arriere_plan(ocr_doc_name, chemin):
    try:
        frappe.db.set_value("OCR Document", ocr_doc_name, "status", "En cours")

        # ⚡ FAST PATH PDF TEXTE
        if chemin.endswith(".pdf") and _is_pdf_textuel(chemin):
            import fitz
            doc = fitz.open(chemin)
            texte = "\n".join([p.get_text() for p in doc])

            res_ocr = {
                "texte": texte,
                "score_confiance": 95,
                "moteur": "pdf_text"
            }
        else:
            from ocr_intelligent.ocr.ocr_engine import get_engine
            engine = get_engine()
            res_ocr = engine.extraire_texte(chemin)

        frappe.db.set_value("OCR Document", ocr_doc_name, {
            "confidence_score": res_ocr["score_confiance"],
            "extracted_text": res_ocr["texte"],
            "status": "Validé",
        })

    except Exception:
        frappe.db.set_value("OCR Document", ocr_doc_name, "status", "Rejeté")