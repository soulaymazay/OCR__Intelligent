"""
API principale - Groupe Bayoudh Metal
Remplit automatiquement un formulaire depuis le OCR Document
Compare les champs du formulaire avec les données extraites
"""

import frappe
import json
import os


@frappe.whitelist()
def remplir_formulaire(nom_fichier):
    """
    API appelée quand l'utilisateur clique "Ajouter un document"
    dans n'importe quel formulaire.

    1. Cherche le OCR Document correspondant au fichier
    2. Extrait les données OCR du fichier si nécessaire
    3. Retourne les données structurées pour remplir le formulaire
    4. Retourne les erreurs si des champs manquent

    Utilisation depuis le formulaire Frappe (JS) :
    frappe.call({
        method: 'ocr_intelligent.api.ocr_api.remplir_formulaire',
        args: { nom_fichier: 'Facture_001.pdf' },
        callback: function(r) { ... }
    })
    """

    if not nom_fichier:
        return {"success": False, "erreur": "Nom du fichier obligatoire"}

    try:
        # Chercher le OCR Document correspondant
        ocr_docs = frappe.get_list(
            "OCR Document",
            filters={"document_name": nom_fichier},
            fields=["name", "document_name", "file_url", "extracted_fields",
                    "extracted_text", "confidence_score", "status"],
            order_by="creation desc",
            limit=1
        )
    except Exception as e:
        frappe.log_error(f"Erreur recherche document: {str(e)}", "OCR API")
        return {
            "success": False,
            "erreur": f"Erreur base de données: {str(e)}"
        }

    if not ocr_docs:
        return {
            "success": False,
            "erreur": f"❌ Aucun document traité trouvé pour '{nom_fichier}'.<br>"
                      f"Veuillez d'abord uploader le fichier pour le traiter."
        }

    ocr_doc = ocr_docs[0]

    # ══════════════════════════════════════════════════════════════
    # ÉTAPE 1 : Récupérer les champs extraits
    # ══════════════════════════════════════════════════════════════
    
    champs_extraits = {}
    type_document = "inconnu"
    
    try:
        if ocr_doc.get("extracted_fields"):
            champs_json = json.loads(ocr_doc.extracted_fields or "{}")
            if champs_json:
                champs_extraits = champs_json
    except Exception as e:
        frappe.log_error(f"Erreur JSON parsing: {str(e)}", "OCR API")
        champs_extraits = {}

    # ══════════════════════════════════════════════════════════════
    # ÉTAPE 2 : Si les champs sont vides, réextraire du texte
    # ══════════════════════════════════════════════════════════════
    
    if not champs_extraits or len(champs_extraits) == 0:
        try:
            # Récupérer le chemin du fichier
            chemin_fichier = _get_chemin_fichier_from_url(ocr_doc.file_url)
            
            if chemin_fichier and os.path.exists(chemin_fichier):
                # Réextraire les données
                from ocr_intelligent.ocr.ocr_engine import OCREngine
                from ocr_intelligent.ocr.extractor import ExtracteurIntelligent
                from ocr_intelligent.ocr.validator import Validateur

                # 1. Extraction OCR
                engine = OCREngine()
                resultat_ocr = engine.extraire_texte(chemin_fichier)
                texte_brut = resultat_ocr["texte"]
                score = resultat_ocr["score_confiance"]

                # 2. Extraction intelligente des champs
                extracteur = ExtracteurIntelligent(texte_brut)
                donnees = extracteur.extraire_tout()
                type_document = donnees["type_document"]
                champs_extraits = donnees["champs"]

                # 3. Validation
                validateur = Validateur(type_document, champs_extraits)
                rapport = validateur.valider()
                champs_extraits = rapport["champs_valides"]

                # Mettre à jour le OCR Document
                frappe.db.set_value(
                    "OCR Document",
                    ocr_doc.name,
                    {
                        "extracted_fields": json.dumps(champs_extraits, ensure_ascii=False, indent=2),
                        "extracted_text": texte_brut,
                        "confidence_score": score
                    }
                )
        except Exception as e:
            frappe.log_error(f"Erreur lors de la réextraction : {str(e)}", "OCR API")

    # ══════════════════════════════════════════════════════════════
    # ÉTAPE 3 : Vérifier si des champs ont été trouvés
    # ══════════════════════════════════════════════════════════════
    
    if not champs_extraits:
        return {
            "success": False,
            "erreur": f"❌ Aucun champ détecté dans le document '{nom_fichier}'.<br>"
                      f"Score OCR : {ocr_doc.confidence_score}% — Document peut-être illisible."
        }

    # ══════════════════════════════════════════════════════════════
    # ÉTAPE 4 : Structurer et valider les champs
    # ══════════════════════════════════════════════════════════════
    
    # Déterminer le type de document s'il n'a pas été trouvé
    if type_document == "inconnu":
        type_document = _detecter_type_document(champs_extraits)

    # Nettoyer et structurer les champs
    champs_remplis = _structurer_champs(champs_extraits, type_document)

    # ══════════════════════════════════════════════════════════════
    # ÉTAPE 5 : Générer le rapport avec les erreurs
    # ══════════════════════════════════════════════════════════════
    
    erreurs = _verifier_champs_obligatoires(type_document, champs_remplis)

    # ══════════════════════════════════════════════════════════════
    # ÉTAPE 6 : Construire la réponse finale
    # ══════════════════════════════════════════════════════════════
    
    return {
        "success": True,
        "document_ocr_id": ocr_doc.name,
        "nom_fichier": nom_fichier,
        "score_confiance": ocr_doc.confidence_score,
        "statut_ocr": ocr_doc.status,
        "type_document": type_document,
        "champs_extraits": champs_remplis,
        "champs_remplis": champs_remplis,
        "erreurs": erreurs,
        "nombre_champs_remplis": len(champs_remplis),
        "nombre_erreurs": len(erreurs),
        "message": _generer_message(len(champs_remplis), len(erreurs))
    }


@frappe.whitelist()
def get_liste_documents_traites():
    """
    Retourne la liste de tous les documents OCR traités
    Pour afficher dans le sélecteur du formulaire
    """
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
    """Statistiques pour le tableau de bord"""
    try:
        return {
            "succes": True,
            "stats": {
                "total": frappe.db.count("OCR Document"),
                "valides": frappe.db.count("OCR Document", {"status": "Validé"}),
                "validation_requise": frappe.db.count("OCR Document", {"status": "Validation requise"}),
                "en_attente": frappe.db.count("OCR Document", {"status": "En attente"}),
                "rejetes": frappe.db.count("OCR Document", {"status": "Rejeté"}),
            }
        }
    except Exception as e:
        frappe.log_error(f"Erreur statistiques: {str(e)}", "OCR API")
        return {
            "succes": False,
            "stats": {}
        }


# ══════════════════════════════════════════════════════════════════════════════
# FONCTIONS UTILITAIRES
# ══════════════════════════════════════════════════════════════════════════════


def _get_chemin_fichier_from_url(file_url):
    """Retourne le chemin physique complet du fichier uploadé"""
    if not file_url:
        return None
        
    site_path = frappe.get_site_path()

    # Enlever le /files/ du début
    nom = file_url.replace("/private/files/", "").replace("/files/", "")

    chemins = [
        os.path.join(site_path, "private", "files", nom),
        os.path.join(site_path, "public", "files", nom),
    ]
    
    for c in chemins:
        try:
            if os.path.exists(c):
                return c
        except:
            pass

    return None


def _detecter_type_document(champs):
    """Détecte le type de document basé sur les champs présents"""
    champs_lower = {k.lower(): v for k, v in champs.items()}
    
    # Vérifier les patterns
    if any(k in champs_lower for k in ["numero_facture", "montant_ttc", "montant_tva"]):
        return "facture"
    elif any(k in champs_lower for k in ["numero_bl", "date_livraison"]):
        return "bon_livraison"
    elif any(k in champs_lower for k in ["numero_cheque", "banque"]):
        return "cheque"
    elif any(k in champs_lower for k in ["numero_commande", "numero_bc"]):
        return "bon_commande"
    
    return "inconnu"


def _structurer_champs(champs, type_document):
    """
    Structure et nettoie les champs extraits
    Retourne un dictionnaire avec les clés standardisées
    """
    champs_structures = {}

    # Mapping des champs possibles
    mapping_standard = {
        "date": ["date", "date_facture", "date_emission"],
        "date_echeance": ["date_echeance", "date_paiement", "date_due"],
        "date_livraison": ["date_livraison", "date_bl"],
        "date_commande": ["date_commande", "date_bc"],
        "numero_facture": ["numero_facture", "num_facture", "facture_no", "invoice_no"],
        "numero_bl": ["numero_bl", "num_bl", "bl_no", "lr_no"],
        "numero_commande": ["numero_commande", "num_commande", "commande_no", "po_no"],
        "numero_cheque": ["numero_cheque", "num_cheque", "cheque_no"],
        "montant_ht": ["montant_ht", "montant_hors_taxe", "net_total"],
        "montant_tva": ["montant_tva", "montant_taxe", "tva", "total_taxes"],
        "montant_ttc": ["montant_ttc", "montant_total", "total", "grand_total"],
        "montant": ["montant", "amount", "paid_amount"],
        "fournisseur": ["fournisseur", "supplier", "vendeur", "societe"],
        "client": ["client", "customer", "destinataire", "acheteur"],
        "banque": ["banque", "bank", "nom_banque"],
    }

    # Inverser le mapping pour la recherche
    champs_lower = {k.lower(): (k, v) for k, v in champs.items()}

    for champ_standard, aliases in mapping_standard.items():
        for alias in aliases:
            if alias.lower() in champs_lower:
                _, valeur = champs_lower[alias.lower()]
                if valeur:  # Ne pas ajouter les valeurs vides
                    champs_structures[champ_standard] = valeur
                break

    return champs_structures


def _verifier_champs_obligatoires(type_document, champs):
    """
    Vérifie les champs obligatoires selon le type de document
    Retourne une liste d'erreurs
    """
    
    CHAMPS_OBLIGATOIRES = {
        "facture": [
            "numero_facture",
            "date",
            "fournisseur",
            "montant_ttc"
        ],
        "bon_livraison": [
            "numero_bl",
            "date_livraison",
            "fournisseur"
        ],
        "cheque": [
            "numero_cheque",
            "montant",
            "date"
        ],
        "bon_commande": [
            "numero_commande",
            "date_commande",
            "fournisseur"
        ],
        "inconnu": [],
    }

    erreurs = []
    requis = CHAMPS_OBLIGATOIRES.get(type_document, [])

    messages_erreur = {
        "numero_facture": "Numéro de facture non détecté",
        "date": "Date non détectée",
        "fournisseur": "Fournisseur non détecté",
        "montant_ttc": "Montant TTC non détecté",
        "montant_ht": "Montant HT non détecté",
        "montant_tva": "TVA non détectée",
        "numero_bl": "Numéro BL non détecté",
        "date_livraison": "Date de livraison non détectée",
        "numero_cheque": "Numéro de chèque non détecté",
        "montant": "Montant non détecté",
        "numero_commande": "Numéro de commande non détecté",
        "client": "Client non détecté",
    }

    for champ in requis:
        if champ not in champs or not champs[champ]:
            msg = messages_erreur.get(champ, f"Champ '{champ}' non détecté")
            erreurs.append({
                "champ": champ,
                "message": f"❌ {msg}",
                "action": f"Saisir manuellement '{champ}'"
            })

    return erreurs


def _generer_message(nb_remplis, nb_erreurs):
    """Génère un message de résumé"""
    if nb_erreurs == 0:
        return f"✅ {nb_remplis} champs remplis automatiquement avec succès"
    elif nb_remplis == 0:
        return f"❌ Aucun champ détecté — saisie manuelle requise"
    else:
        return f"⚠️ {nb_remplis} champs remplis, {nb_erreurs} champ(s) à saisir manuellement"