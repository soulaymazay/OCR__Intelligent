"""
API principale - Groupe Bayoudh Metal
Remplit automatiquement un formulaire depuis le OCR Document
Compare les champs du formulaire avec les données extraites

CORRECTIONS MAPPING v2 :
  ─ create_purchase_invoice_from_log :
      • Lecture depuis extracted_field (clé singulier, alignée sur field_matcher.py)
        avec fallback sur les clés OCR brutes via _OCR_TO_FRAPPE
      • rate de l'item = net_total (montant_ht OCR) et NON grand_total - tax_amount
        → évite la double déduction de TVA par ERPNext
      • posting_date obligatoire alimenté depuis bill_date
      • Compte de charge : cascade expense_account → stock_received_but_not_billed
        → premier compte Expense actif
      • Taxe charge_type="Actual" : ERPNext utilise tax_amount directement
        sans recalculer sur la base HT

  ─ create_payment_entry_from_invoice :
      • paid_to root_type="Liability" (comptes fournisseurs = dettes)
        CORRECTION : l'ancien code cherchait root_type="Asset" → aucun résultat
      • outstanding_amount lu depuis inv.outstanding_amount (montant réel dû)
      • Devises lues depuis account_currency de chaque compte GL
      • posting_date obligatoire
      • Si log_name fourni : reference_no + reference_date lus depuis extracted_field
        (champs mappés par payment_doc_extractor → reference_no, reference_date)
"""

import frappe
import json
import os
import time



# ══════════════════════════════════════════════════════════════════════════════
# MAPPING OCR → FRAPPE  (aligné sur field_matcher.py MAPPING_CHAMPS["facture"])
# ══════════════════════════════════════════════════════════════════════════════
#
# field_matcher.py MAPPING_CHAMPS["facture"] :
#   "numero_facture"  → "bill_no"
#   "date"            → "bill_date"
#   "fournisseur"     → "supplier"
#   "montant_ht"      → "net_total"
#   "montant_tva"     → "total_taxes_and_charges"
#   "montant_ttc"     → "grand_total"
#   "date_echeance"   → "due_date"
#   "mode_paiement"   → "payment_terms_template"
#   "numero_commande" → "po_no"
#
# ocr_pipeline.py (correction v4) copie aussi les clés OCR brutes dans
# champs_remplis : montant_ht, montant_tva, montant_ttc
# → on cherche d'abord les clés Frappe, puis les clés OCR brutes en fallback.

_OCR_TO_FRAPPE = {
    "numero_facture":  "bill_no",
    "date":            "bill_date",
    "fournisseur":     "supplier",
    "montant_ht":      "net_total",
    "montant_tva":     "total_taxes_and_charges",
    "montant_ttc":     "grand_total",
    "date_echeance":   "due_date",
    "mode_paiement":   "payment_terms_template",
    "numero_commande": "po_no",       # manquait : aligné sur MAPPING_CHAMPS["facture"]
}

# Clés Frappe de paiement (mappées par payment_doc_extractor)
_OCR_TO_FRAPPE_PAYMENT = {
    "numero_cheque":  "reference_no",
    "numero_traite":  "reference_no",
    "draft_number":   "reference_no",
    "date_cheque":    "reference_date",
    "cheque_date":    "reference_date",
    "date_echeance":  "reference_date",
    "due_date":       "reference_date",
    "date_emission":  "reference_date",
    "issue_date":     "reference_date",
    "montant":        "paid_amount",
    "amount":         "paid_amount",
}


def _lire_extracted_field(ocr_doc):
    """
    Lit le JSON de extracted_field (clé singulier, alignée sur field_matcher.py
    et ocr_pipeline.py) depuis un OCR Document.

    Retourne un dict avec les clés OCR brutes
    (ex: "montant_ht", "montant_ttc", "numero_facture", …).
    """
    # Priorite: extracted_field (singulier), puis ancien nom extracted_fields.
    raw = (
        getattr(ocr_doc, "extracted_field",  None)
        or getattr(ocr_doc, "extracted_fields", None)   # ancien nom éventuel
        or "{}"
    )
    try:
        return json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (ValueError, TypeError):
        return {}


def _champs_frappe_depuis_ocr(ocr_doc):
    """
    Construit un dict avec les clés Frappe à partir d'un OCR Document.

    Ordre de priorité pour chaque champ :
      1. Attribut direct Frappe sur le doc (ex: ocr_doc.bill_no)
      2. extracted_field JSON → clé Frappe (ex: "bill_no")
      3. extracted_field JSON → clé OCR brute via _OCR_TO_FRAPPE (ex: "numero_facture")
    """
    ocr_data = _lire_extracted_field(ocr_doc)
    result = {}

    for ocr_key, frappe_key in _OCR_TO_FRAPPE.items():
        # 1. Attribut direct Frappe
        val = getattr(ocr_doc, frappe_key, None)
        if val not in (None, "", 0, 0.0):
            result[frappe_key] = val
            continue

        # 2. Clé Frappe dans le JSON extrait
        if frappe_key in ocr_data and ocr_data[frappe_key] not in (None, "", "0"):
            result[frappe_key] = ocr_data[frappe_key]
            continue

        # 3. Clé OCR brute dans le JSON extrait
        if ocr_key in ocr_data and ocr_data[ocr_key] not in (None, "", "0"):
            result[frappe_key] = ocr_data[ocr_key]

    return result


def _champs_paiement_depuis_ocr(ocr_doc):
    """
    Lit les champs de paiement (chèque / traite) depuis extracted_field.
    Retourne un dict avec les clés Frappe Payment Entry.
    """
    ocr_data = _lire_extracted_field(ocr_doc)
    result = {}

    # Clés Frappe directes déjà mappées par payment_doc_extractor
    for frappe_key in ("reference_no", "reference_date", "paid_amount", "bank",
                        "mode_of_payment", "payment_method"):
        val = getattr(ocr_doc, frappe_key, None) or ocr_data.get(frappe_key)
        if val not in (None, "", 0, 0.0):
            result[frappe_key] = val

    # Fallback : clés OCR brutes
    for ocr_key, frappe_key in _OCR_TO_FRAPPE_PAYMENT.items():
        if frappe_key not in result and ocr_key in ocr_data:
            val = ocr_data[ocr_key]
            if val not in (None, "", "0"):
                result[frappe_key] = val

    return result


def _to_float(val, default=0.0):
    """Convertit une valeur OCR (str ou float) en float."""
    if val is None:
        return default
    try:
        return float(
            str(val)
            .replace(",", ".")
            .replace(" ", "")
            .replace("\u00a0", "")
        )
    except (ValueError, TypeError):
        return default


def _get_company():
    return (
        frappe.defaults.get_user_default("Company")
        or frappe.defaults.get_global_default("company")
        or frappe.db.get_single_value("Global Defaults", "default_company")
    )


def _get_expense_account(company):
    """Compte de charge — cascade de fallback pour éviter les erreurs ERPNext."""
    acc = frappe.db.get_value("Company", company, "default_expense_account")
    if acc:
        return acc
    acc = frappe.db.get_value("Company", company, "stock_received_but_not_billed")
    if acc:
        return acc
    acc = frappe.db.get_value(
        "Account",
        {"account_type": "Expense Account", "company": company, "disabled": 0, "is_group": 0},
        "name"
    )
    if acc:
        return acc
    return frappe.db.get_value(
        "Account",
        {"root_type": "Expense", "company": company, "disabled": 0, "is_group": 0},
        "name"
    )


def _get_tax_account(company):
    """Compte TVA achat (Liability / Tax)."""
    acc = frappe.db.get_value(
        "Account",
        {"account_type": "Tax", "root_type": "Liability",
         "company": company, "disabled": 0, "is_group": 0},
        "name"
    )
    if acc:
        return acc
    return frappe.db.get_value(
        "Account",
        {"account_name": ["like", "%TVA%"], "company": company, "disabled": 0, "is_group": 0},
        "name"
    )


def _get_bank_account(company):
    """Compte bancaire source pour Payment Entry de type Pay."""
    acc = frappe.db.get_value("Company", company, "default_bank_account")
    if acc:
        return acc
    acc = frappe.db.get_value(
        "Account",
        {"account_type": "Bank", "company": company, "disabled": 0, "is_group": 0},
        "name"
    )
    if acc:
        return acc
    return frappe.db.get_value(
        "Account",
        {"account_type": "Cash", "company": company, "disabled": 0, "is_group": 0},
        "name"
    )


def _get_payable_account(company):
    """
    Compte fournisseur (Payable).

    CORRECTION : root_type="Liability" (comptes fournisseurs = dettes)
    L'ancien code cherchait root_type="Asset" → aucun résultat trouvé.
    """
    acc = frappe.db.get_value("Company", company, "default_payable_account")
    if acc:
        return acc
    return frappe.db.get_value(
        "Account",
        {
            "account_type": "Payable",
            "root_type":    "Liability",   # ← CORRECTION
            "company":      company,
            "disabled":     0,
            "is_group":     0,
        },
        "name"
    )


# ══════════════════════════════════════════════════════════════════════════════
# CRÉATION PURCHASE INVOICE DEPUIS OCR LOG
# ══════════════════════════════════════════════════════════════════════════════

@frappe.whitelist()
def create_purchase_invoice_from_log(log_name: str) -> str:
    """
    Crée une Purchase Invoice ERPNext depuis un OCR Log.

    Mapping OCR → Frappe (aligné sur field_matcher.py MAPPING_CHAMPS["facture"]) :
      OCR "numero_facture" → bill_no
      OCR "date"           → bill_date  +  posting_date
      OCR "fournisseur"    → supplier
      OCR "montant_ht"     → net_total  → items[0].rate   ← CORRECTION PRINCIPALE
      OCR "montant_tva"    → total_taxes_and_charges → taxes[0].tax_amount
      OCR "montant_ttc"    → grand_total (recalculé automatiquement par ERPNext)

    CORRECTION rate :
      Ancien code : rate = grand_total - tax_amount
        → ERPNext recalcule la TVA sur ce rate = double TVA dans le grand_total final.
      Correction   : rate = net_total (montant_ht OCR, HT pur)
        Si net_total absent : net_total = grand_total - total_taxes_and_charges.
    """
    log     = frappe.get_doc("OCR Log", log_name)
    company = _get_company()

    # ── Lecture des champs mappés ──────────────────────────────────────
    champs = _champs_frappe_depuis_ocr(log)

    bill_no     = champs.get("bill_no")   or ""
    bill_date   = champs.get("bill_date") or frappe.utils.today()
    supplier    = champs.get("supplier")  or ""
    due_date    = champs.get("due_date")  or ""

    grand_total = _to_float(champs.get("grand_total"))
    tax_amount  = _to_float(champs.get("total_taxes_and_charges"))

    # ── Calcul net_total (HT) ──────────────────────────────────────────
    # CORRECTION : net_total = montant_ht OCR, pas grand_total - tax_amount
    net_total_raw = champs.get("net_total")
    if net_total_raw:
        net_total = _to_float(net_total_raw)
    elif grand_total > 0 and tax_amount > 0:
        net_total = round(grand_total - tax_amount, 3)
    elif grand_total > 0:
        net_total = grand_total
    else:
        net_total = 0.0

    # ── Validations ────────────────────────────────────────────────────
    if not supplier:
        frappe.throw(
            f"Fournisseur non détecté dans l'OCR Log « {log_name} ».<br>"
            "Vérifiez que le document a été correctement traité par l'OCR.",
            title="Fournisseur manquant"
        )

    if net_total <= 0 and grand_total <= 0:
        frappe.throw(
            f"Aucun montant détecté dans l'OCR Log « {log_name} ».",
            title="Montant manquant"
        )

    # ── Comptes GL ────────────────────────────────────────────────────
    expense_account = _get_expense_account(company)
    if not expense_account:
        frappe.throw(
            "Aucun compte de charge configuré.<br>"
            "Configurez « Default Expense Account » dans les paramètres de la société.",
            title="Compte de charge manquant"
        )

    # ── Création de la facture ────────────────────────────────────────
    inv = frappe.new_doc("Purchase Invoice")
    inv.supplier         = supplier
    inv.bill_no          = bill_no
    inv.bill_date        = bill_date
    inv.posting_date     = bill_date     # CORRECTION : obligatoire ERPNext
    inv.set_posting_time = 1
    inv.company          = company

    if due_date:
        inv.due_date = due_date

    # ── Ligne article ──────────────────────────────────────────────────
    # CORRECTION : rate = net_total (HT pur) et non grand_total - tax_amount
    inv.append("items", {
        "item_name":       f"Facture {bill_no}" if bill_no else "Facture fournisseur",
        "qty":             1,
        "rate":            net_total,           # ← CORRECTION PRINCIPALE
        "uom":             "Nos",               # unité obligatoire ERPNext
        "expense_account": expense_account,     # compte de charge obligatoire
        "description":     f"Import OCR — {log_name}",
    })

    # ── Taxe TVA ──────────────────────────────────────────────────────
    # charge_type="Actual" → ERPNext utilise tax_amount directement
    # sans recalculer sur la base HT → évite la double TVA
    if tax_amount > 0:
        tax_account = _get_tax_account(company)
        if tax_account:
            inv.append("taxes", {
                "charge_type":  "Actual",
                "account_head": tax_account,
                "tax_amount":   tax_amount,
                "description":  "TVA (import OCR)",
            })
        else:
            frappe.msgprint(
                f"Compte TVA non trouvé pour « {company} ».<br>"
                "La TVA n'a pas été ajoutée automatiquement.",
                indicator="orange",
                title="Compte TVA absent"
            )

    inv.insert(ignore_permissions=True)
    inv.submit()

    # ── Mise à jour de l'OCR Log ──────────────────────────────────────
    try:
        frappe.db.set_value("OCR Log", log_name, "purchase_invoice", inv.name)
        frappe.db.commit()
    except Exception:
        pass  # champ optionnel

    return inv.name


# ══════════════════════════════════════════════════════════════════════════════
# CRÉATION PAYMENT ENTRY DEPUIS PURCHASE INVOICE
# ══════════════════════════════════════════════════════════════════════════════

@frappe.whitelist()
def create_payment_entry_from_invoice(invoice_name: str, log_name: str = None) -> str:
    """
    Crée un Payment Entry depuis une Purchase Invoice.

    Mapping Payment Entry ERPNext :
      payment_type  = "Pay"
      party_type    = "Supplier"
      paid_from     = compte Bank/Cash  (source des fonds)
      paid_to       = compte Payable    (dette fournisseur)

    CORRECTIONS :
      ─ paid_to root_type="Liability" (et non "Asset")
        Les comptes fournisseurs sont des dettes, pas des actifs.
      ─ outstanding_amount lu depuis inv.outstanding_amount (montant réel dû)
        et non une valeur fixe égale à grand_total.
      ─ posting_date obligatoire.
      ─ Devises lues depuis account_currency de chaque compte GL.
      ─ Si log_name fourni : reference_no + reference_date lus depuis
        extracted_field du OCR Log (champs mappés par payment_doc_extractor :
        reference_no ← numero_cheque/numero_traite,
        reference_date ← date_cheque/date_echeance).
    """
    inv     = frappe.get_doc("Purchase Invoice", invoice_name)
    company = inv.company

    # ── Comptes GL ────────────────────────────────────────────────────
    paid_from = _get_bank_account(company)
    if not paid_from:
        frappe.throw(
            "Aucun compte bancaire configuré pour la société.<br>"
            "Configurez « Default Bank Account » dans les paramètres de la société.",
            title="Compte bancaire manquant"
        )

    # CORRECTION : root_type="Liability" pour les comptes Payable (fournisseurs)
    paid_to = _get_payable_account(company)
    if not paid_to:
        frappe.throw(
            "Aucun compte fournisseur (Payable) configuré pour la société.",
            title="Compte fournisseur manquant"
        )

    # ── Devises ────────────────────────────────────────────────────────
    currency_from = frappe.db.get_value("Account", paid_from, "account_currency") or "TND"
    currency_to   = frappe.db.get_value("Account", paid_to,   "account_currency") or "TND"

    # ── Montant du paiement ────────────────────────────────────────────
    # CORRECTION : utilise outstanding_amount (montant réellement dû)
    # et non grand_total qui peut déjà inclure des acomptes.
    outstanding = _to_float(inv.outstanding_amount)
    paid_amount = outstanding if outstanding > 0 else _to_float(inv.grand_total)

    # ── Infos chèque / traite depuis OCR Log ──────────────────────────
    reference_no   = ""
    reference_date = frappe.utils.today()
    mode_paiement  = ""

    if log_name:
        try:
            log = frappe.get_doc("OCR Log", log_name)
            paiement = _champs_paiement_depuis_ocr(log)

            reference_no   = paiement.get("reference_no",   "")
            reference_date = paiement.get("reference_date", frappe.utils.today())
            mode_paiement  = paiement.get("mode_of_payment", "") or paiement.get("payment_method", "")

            # Si le montant OCR du paiement est différent, le prioriser
            montant_log = _to_float(paiement.get("paid_amount"))
            if montant_log > 0:
                paid_amount = montant_log

        except frappe.DoesNotExistError:
            frappe.msgprint(
                f"OCR Log « {log_name} » introuvable. "
                "Le paiement sera créé sans référence chèque/traite.",
                indicator="orange"
            )

    # ── Création du Payment Entry ─────────────────────────────────────
    pe = frappe.new_doc("Payment Entry")
    pe.payment_type   = "Pay"
    pe.party_type     = "Supplier"
    pe.party          = inv.supplier
    pe.company        = company
    pe.posting_date   = frappe.utils.today()   # CORRECTION : obligatoire

    # Comptes GL
    pe.paid_from = paid_from
    pe.paid_to   = paid_to                     # CORRECTION : compte Payable (Liability)

    # Devises et montants
    pe.paid_from_account_currency = currency_from
    pe.paid_to_account_currency   = currency_to
    pe.paid_amount                = paid_amount
    pe.received_amount            = paid_amount
    pe.source_exchange_rate       = 1
    pe.target_exchange_rate       = 1

    # Référence chèque / traite
    if reference_no:
        pe.reference_no   = reference_no
        pe.reference_date = reference_date

    if mode_paiement:
        pe.mode_of_payment = mode_paiement

    # ── Réconciliation avec la facture ────────────────────────────────
    pe.append("references", {
        "reference_doctype":  "Purchase Invoice",
        "reference_name":     invoice_name,
        "total_amount":       _to_float(inv.grand_total),
        # CORRECTION : outstanding_amount réel et non grand_total fixe
        "outstanding_amount": _to_float(inv.outstanding_amount),
        "allocated_amount":   paid_amount,
    })

    pe.insert(ignore_permissions=True)
    pe.submit()

    # ── Mise à jour de l'OCR Log ──────────────────────────────────────
    if log_name:
        try:
            frappe.db.set_value("OCR Log", log_name, "payment_entry", pe.name)
            frappe.db.commit()
        except Exception:
            pass  # champ optionnel

    frappe.db.commit()
    return pe.name


# ══════════════════════════════════════════════════════════════════════════════
# FONCTIONS EXISTANTES — inchangées
# ══════════════════════════════════════════════════════════════════════════════

@frappe.whitelist()
def remplir_formulaire(nom_fichier):
    """
    API appelée quand l'utilisateur clique "Ajouter un document"
    dans n'importe quel formulaire.
    (Code original conservé intégralement)
    """
    if not nom_fichier:
        return {"success": False, "erreur": "Nom du fichier obligatoire"}

    try:
        ocr_docs = frappe.get_list(
            "OCR Document",
            filters={"document_name": nom_fichier},
            fields=["name", "extracted_field", "extracted_text", "confidence_score", "status"],
            order_by="creation desc",
            limit=1
        )
    except Exception as e:
        frappe.log_error(f"Erreur recherche document: {str(e)}", "OCR API")
        return {"success": False, "erreur": f"Erreur base de données: {str(e)}"}

    if not ocr_docs:
        return {
            "success": False,
            "erreur": f"❌ Aucun document traité trouvé pour '{nom_fichier}'.<br>"
                      f"Veuillez d'abord uploader le fichier pour le traiter."
        }

    ocr_doc = ocr_docs[0]

    champs_extraits = {}
    type_document   = "inconnu"

    try:
        if ocr_doc.get("extracted_field"):
            champs_json = json.loads(ocr_doc.get("extracted_field") or "{}")
            if champs_json:
                champs_extraits = champs_json
    except Exception as e:
        frappe.log_error(f"Erreur JSON parsing: {str(e)}", "OCR API")
        champs_extraits = {}

    if not champs_extraits or len(champs_extraits) == 0:
        try:
            chemin_fichier = _get_chemin_fichier_from_url(
                getattr(ocr_doc, "file_url", None) or ""
            )
            if chemin_fichier and os.path.exists(chemin_fichier):
                from ocr_intelligent.ocr.ocr_engine import get_engine
                from ocr_intelligent.ocr.extractor import ExtracteurIntelligent
                from ocr_intelligent.ocr.validator import Validateur

                engine        = get_engine()
                resultat_ocr  = engine.extraire_texte(chemin_fichier)
                texte_brut    = resultat_ocr["texte"]
                score         = resultat_ocr["score_confiance"]

                extracteur    = ExtracteurIntelligent(texte_brut)
                donnees       = extracteur.extraire_tout()
                type_document = donnees["type_document"]
                champs_extraits = donnees["champs"]

                validateur    = Validateur(type_document, champs_extraits)
                rapport       = validateur.valider()
                champs_extraits = rapport["champs_valides"]

                frappe.db.set_value("OCR Document", ocr_doc.name, {
                    "extracted_field": json.dumps(champs_extraits, ensure_ascii=False, indent=2),
                    "extracted_text":  texte_brut,
                    "confidence_score": score
                })
        except Exception as e:
            frappe.log_error(f"Erreur lors de la réextraction : {str(e)}", "OCR API")

    if not champs_extraits:
        return {
            "success": False,
            "erreur": f"❌ Aucun champ détecté dans le document '{nom_fichier}'.<br>"
                      f"Score OCR : {ocr_doc.get('confidence_score')}% — Document peut-être illisible."
        }

    if type_document == "inconnu":
        type_document = _detecter_type_document(champs_extraits)

    champs_remplis = _structurer_champs(champs_extraits, type_document)
    erreurs        = _verifier_champs_obligatoires(type_document, champs_remplis)

    return {
        "success":               True,
        "document_ocr_id":       ocr_doc.name,
        "nom_fichier":           nom_fichier,
        "score_confiance":       ocr_doc.get("confidence_score"),
        "statut_ocr":            ocr_doc.get("status"),
        "type_document":         type_document,
        "champs_extraits":       champs_remplis,
        "champs_remplis":        champs_remplis,
        "erreurs":               erreurs,
        "nombre_champs_remplis": len(champs_remplis),
        "nombre_erreurs":        len(erreurs),
        "message":               _generer_message(len(champs_remplis), len(erreurs))
    }


@frappe.whitelist()
def get_liste_documents_traites():
    try:
        docs = frappe.get_list(
            "OCR Document",
            fields=["name", "document_name", "file_url",
                    "status", "confidence_score", "creation"],
            order_by="creation desc",
            limit=100
        )
        return {"succes": True, "documents": docs}
    except Exception as e:
        frappe.log_error(f"Erreur liste documents: {str(e)}", "OCR API")
        return {"succes": False, "documents": []}


@frappe.whitelist()
def get_statistiques():
    try:
        return {
            "succes": True,
            "stats": {
                "total":              frappe.db.count("OCR Document"),
                "valides":            frappe.db.count("OCR Document", {"status": "Validé"}),
                "validation_requise": frappe.db.count("OCR Document", {"status": "Validation requise"}),
                "en_attente":         frappe.db.count("OCR Document", {"status": "En attente"}),
                "rejetes":            frappe.db.count("OCR Document", {"status": "Rejeté"}),
            }
        }
    except Exception as e:
        frappe.log_error(f"Erreur statistiques: {str(e)}", "OCR API")
        return {"succes": False, "stats": {}}


# ══════════════════════════════════════════════════════════════════════════════
# FONCTIONS UTILITAIRES (inchangées)
# ══════════════════════════════════════════════════════════════════════════════

def _get_chemin_fichier_from_url(file_url):
    if not file_url:
        return None
    site_path = frappe.get_site_path()
    nom = file_url.replace("/private/files/", "").replace("/files/", "")
    for c in [
        os.path.join(site_path, "private", "files", nom),
        os.path.join(site_path, "public",  "files", nom),
    ]:
        try:
            if os.path.exists(c):
                return c
        except Exception:
            pass
    return None


def _detecter_type_document(champs):
    champs_lower = {k.lower(): v for k, v in champs.items()}
    if any(k in champs_lower for k in ["numero_facture", "montant_ttc", "montant_tva", "bill_no", "grand_total"]):
        return "facture"
    elif any(k in champs_lower for k in ["numero_bl", "date_livraison"]):
        return "bon_livraison"
    elif any(k in champs_lower for k in ["numero_cheque", "banque", "reference_no"]):
        return "cheque"
    elif any(k in champs_lower for k in ["numero_commande", "numero_bc"]):
        return "bon_commande"
    return "inconnu"


def _structurer_champs(champs, type_document):
    champs_structures = {}
    mapping_standard = {
        "date":           ["date", "date_facture", "bill_date"],
        "date_echeance":  ["date_echeance", "date_paiement", "due_date"],
        "date_livraison": ["date_livraison", "date_bl"],
        "date_commande":  ["date_commande", "date_bc"],
        "numero_facture": ["numero_facture", "bill_no", "facture_no"],
        "numero_bl":      ["numero_bl", "num_bl", "lr_no"],
        "numero_commande":["numero_commande", "po_no"],
        "numero_cheque":  ["numero_cheque", "reference_no"],
        "montant_ht":     ["montant_ht", "net_total"],
        "montant_tva":    ["montant_tva", "total_taxes_and_charges"],
        "montant_ttc":    ["montant_ttc", "grand_total"],
        "montant":        ["montant", "paid_amount"],
        "fournisseur":    ["fournisseur", "supplier"],
        "client":         ["client", "customer"],
        "banque":         ["banque", "bank"],
    }
    champs_lower = {k.lower(): (k, v) for k, v in champs.items()}
    for champ_standard, aliases in mapping_standard.items():
        for alias in aliases:
            if alias.lower() in champs_lower:
                _, valeur = champs_lower[alias.lower()]
                if valeur:
                    champs_structures[champ_standard] = valeur
                break
    return champs_structures


def _verifier_champs_obligatoires(type_document, champs):
    CHAMPS_OBLIGATOIRES = {
        "facture":       ["numero_facture", "date", "fournisseur", "montant_ttc"],
        "bon_livraison": ["numero_bl",      "date_livraison",      "fournisseur"],
        "cheque":        ["numero_cheque",  "montant",             "date"],
        "bon_commande":  ["numero_commande","date_commande",       "fournisseur"],
        "inconnu":       [],
    }
    messages_erreur = {
        "numero_facture": "Numéro de facture non détecté",
        "date":           "Date non détectée",
        "fournisseur":    "Fournisseur non détecté",
        "montant_ttc":    "Montant TTC non détecté",
        "montant_ht":     "Montant HT non détecté",
        "montant_tva":    "TVA non détectée",
        "numero_bl":      "Numéro BL non détecté",
        "date_livraison": "Date de livraison non détectée",
        "numero_cheque":  "Numéro de chèque non détecté",
        "montant":        "Montant non détecté",
        "numero_commande":"Numéro de commande non détecté",
        "client":         "Client non détecté",
    }
    erreurs = []
    for champ in CHAMPS_OBLIGATOIRES.get(type_document, []):
        if champ not in champs or not champs[champ]:
            msg = messages_erreur.get(champ, f"Champ '{champ}' non détecté")
            erreurs.append({
                "champ":   champ,
                "message": f"❌ {msg}",
                "action":  f"Saisir manuellement '{champ}'"
            })
    return erreurs


def _generer_message(nb_remplis, nb_erreurs):
    if nb_erreurs == 0:
        return f"✅ {nb_remplis} champs remplis automatiquement avec succès"
    elif nb_remplis == 0:
        return "❌ Aucun champ détecté — saisie manuelle requise"
    else:
        return f"⚠️ {nb_remplis} champs remplis, {nb_erreurs} champ(s) à saisir manuellement"


# ══════════════════════════════════════════════════════════════════════════════
# TRAITEMENT ASYNCHRONE (inchangé)
# ══════════════════════════════════════════════════════════════════════════════

@frappe.whitelist()
def lancer_traitement_async(ocr_document_name):
    if not ocr_document_name:
        return {"succes": False, "erreur": "Nom du document OCR obligatoire"}
    if not frappe.db.exists("OCR Document", ocr_document_name):
        return {"succes": False, "erreur": f"Document '{ocr_document_name}' introuvable"}

    frappe.db.set_value("OCR Document", ocr_document_name, {
        "status": "En cours",
        "performance_log": json.dumps({
            "debut": time.time(),
            "etapes": [],
            "statut_courant": "Mise en file d'attente…"
        })
    })
    frappe.db.commit()

    job = frappe.enqueue(
        method="ocr_intelligent.api.ocr_api._worker_ocr_background",
        queue="long",
        timeout=600,
        is_async=True,
        job_name=f"ocr_{ocr_document_name}",
        ocr_document_name=ocr_document_name,
    )

    return {
        "succes":              True,
        "message":             "Traitement OCR lancé en arrière-plan",
        "ocr_document_name":   ocr_document_name,
        "job_id":              getattr(job, "id", f"ocr_{ocr_document_name}"),
    }


def _worker_ocr_background(ocr_document_name):
    def _chrono(etapes, nom_etape, debut_etape):
        duree = round(time.time() - debut_etape, 3)
        etapes.append({"etape": nom_etape, "duree_s": duree})
        return duree

    def _sauvegarder_progression(ocr_document_name, etapes, statut_courant, perf_debut):
        frappe.db.set_value("OCR Document", ocr_document_name, "performance_log", json.dumps({
            "debut": perf_debut,
            "elapsed_s": round(time.time() - perf_debut, 2),
            "etapes": etapes,
            "statut_courant": statut_courant,
        }))
        frappe.db.commit()

    perf_debut = time.time()
    etapes     = []

    try:
        t = time.time()
        ocr_doc = frappe.get_doc("OCR Document", ocr_document_name)
        _chrono(etapes, "Chargement document", t)
        _sauvegarder_progression(ocr_document_name, etapes, "Lecture du fichier…", perf_debut)

        t = time.time()
        chemin = _get_chemin_fichier_from_url(getattr(ocr_doc, "file_url", "") or "")
        if not chemin or not os.path.exists(chemin):
            raise FileNotFoundError(f"Fichier physique introuvable : {getattr(ocr_doc, 'file_url', '')}")
        _chrono(etapes, "Résolution chemin fichier", t)
        _sauvegarder_progression(ocr_document_name, etapes, "Extraction OCR en cours…", perf_debut)

        t = time.time()
        from ocr_intelligent.ocr.ocr_engine import get_engine
        engine     = get_engine()
        res_ocr    = engine.extraire_texte(chemin)
        texte_brut = res_ocr["texte"]
        score      = res_ocr["score_confiance"]
        _chrono(etapes, "Extraction OCR", t)
        _sauvegarder_progression(ocr_document_name, etapes, "Analyse des champs…", perf_debut)

        t = time.time()
        from ocr_intelligent.ocr.extractor import ExtracteurIntelligent
        extracteur = ExtracteurIntelligent(texte_brut)
        donnees    = extracteur.extraire_tout()
        type_doc   = donnees["type_document"]
        champs     = donnees["champs"]
        _chrono(etapes, "Extraction champs intelligente", t)
        _sauvegarder_progression(ocr_document_name, etapes, "Validation des données…", perf_debut)

        t = time.time()
        from ocr_intelligent.ocr.validator import Validateur
        validateur     = Validateur(type_doc, champs)
        rapport        = validateur.valider()
        champs_valides = rapport["champs_valides"]
        _chrono(etapes, "Validation", t)

        statut_map = {
            "valide":             "Validé",
            "validation_requise": "Validation requise",
            "avertissement":      "Validation requise",
        }
        statut       = statut_map.get(rapport["statut"], "En attente")
        duree_totale = round(time.time() - perf_debut, 2)
        log_final    = json.dumps({
            "debut": perf_debut, "elapsed_s": duree_totale,
            "etapes": etapes, "statut_courant": "Terminé",
        })

        frappe.db.set_value("OCR Document", ocr_document_name, {
            "status":           statut,
            "confidence_score": score,
            "extracted_text":   texte_brut,
            "extracted_field":  json.dumps(champs_valides, ensure_ascii=False, indent=2),
            "performance_log":  log_final,
        })
        frappe.db.commit()

        frappe.publish_realtime(
            event="ocr_traitement_termine",
            message={
                "ocr_document_name": ocr_document_name,
                "statut":            statut,
                "type_document":     type_doc,
                "score":             score,
                "duree_totale_s":    duree_totale,
                "nb_champs":         len(champs_valides),
            },
            user=frappe.db.get_value("OCR Document", ocr_document_name, "uploaded_by"),
        )

    except Exception as e:
        duree_totale = round(time.time() - perf_debut, 2)
        frappe.log_error(frappe.get_traceback(), "OCR Background Worker")
        frappe.db.set_value("OCR Document", ocr_document_name, {
            "status": "Rejeté",
            "performance_log": json.dumps({
                "debut": perf_debut, "elapsed_s": duree_totale,
                "etapes": etapes, "statut_courant": f"Erreur : {str(e)}",
            }),
        })
        frappe.db.commit()


@frappe.whitelist()
def get_statut_traitement(ocr_document_name):
    if not frappe.db.exists("OCR Document", ocr_document_name):
        return {"succes": False, "erreur": "Document introuvable"}

    doc = frappe.db.get_value(
        "OCR Document", ocr_document_name,
        ["status", "confidence_score", "performance_log"],
        as_dict=True,
    )
    STATUTS_TERMINAUX = {"Validé", "Validation requise", "Rejeté", "En attente"}
    termine = doc.status in STATUTS_TERMINAUX and doc.status != "En cours"

    perf = {}
    try:
        if doc.performance_log:
            perf = json.loads(doc.performance_log)
    except Exception:
        pass

    return {
        "succes":             True,
        "ocr_document_name":  ocr_document_name,
        "statut":             doc.status,
        "score":              doc.confidence_score,
        "termine":            termine,
        "elapsed_s":          perf.get("elapsed_s", 0),
        "statut_courant":     perf.get("statut_courant", ""),
        "etapes":             perf.get("etapes", []),
    }