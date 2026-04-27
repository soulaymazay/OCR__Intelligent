"""
Validators OCR — hooks de validation Frappe.
Appliqués sur les DocTypes via doc_events dans hooks.py.
"""
import frappe
from datetime import datetime, timedelta, date as date_type


def validate_cheque_date(doc, method=None):
    """
    Bloque l'enregistrement d'un Payment Entry dont la date de chèque
    dépasse 6 mois (183 jours).
    Déclenché par : doc_events > Payment Entry > validate
    """
    # Seulement si un numéro de référence (chèque) est saisi
    if not doc.get("reference_no"):
        return

    ref_date = doc.get("reference_date")
    if not ref_date:
        # Date absente → bloquer pour forcer la saisie
        frappe.throw(
            "La date du chèque est obligatoire.<br>"
            "Veuillez saisir la date avant d'enregistrer.",
            title="Date du chèque manquante"
        )
        return

    # Normaliser en objet date Python
    if isinstance(ref_date, str):
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                ref_date = datetime.strptime(ref_date, fmt).date()
                break
            except ValueError:
                continue
        else:
            return  # format non reconnu — ne pas bloquer

    if isinstance(ref_date, datetime):
        ref_date = ref_date.date()

    limite = (datetime.now() - timedelta(days=183)).date()

    if ref_date < limite:
        frappe.throw(
            f"Ce chèque est périmé : date d'émission <b>{ref_date.strftime('%d/%m/%Y')}</b>.<br>"
            "Un chèque est valable <b>6 mois</b> à partir de sa date d'émission.<br>"
            "Remplacez ce chèque ou contactez l'émetteur.",
            title="Chèque périmé — Enregistrement refusé"
        )
