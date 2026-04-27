"""
Validateur - Groupe Bayoudh Metal
Valide les champs extraits et génère des messages d'erreur précis
"""

import re
from datetime import datetime


class Validateur:

    CHAMPS_OBLIGATOIRES = {
        "facture":       ["numero_facture", "date", "fournisseur", "montant_ttc"],
        "bon_livraison": ["numero_bl", "date_livraison", "fournisseur"],
        "cheque":        ["numero_cheque", "amount", "date_cheque"],
        "traite":        ["amount", "date_echeance"],
        "bon_commande":  ["numero_commande", "date_commande", "fournisseur"],
        "inconnu":       [],
    }

    def __init__(self, type_document, champs_extraits):
        self.type_doc = type_document
        self.champs = champs_extraits
        self.erreurs = []
        self.avertissements = []

    def valider(self):
        self.erreurs = []
        self.avertissements = []

        self._verifier_champs_obligatoires()
        self._valider_formats()
        self._verifier_coherence()

        if self.erreurs:
            statut = "validation_requise"
        elif self.avertissements:
            statut = "avertissement"
        else:
            statut = "valide"

        return {
            "statut": statut,
            "erreurs": self.erreurs,
            "avertissements": self.avertissements,
            "champs_valides": self.champs,
            "nombre_erreurs": len(self.erreurs),
        }

    def _verifier_champs_obligatoires(self):
        requis = self.CHAMPS_OBLIGATOIRES.get(self.type_doc, [])
        for champ in requis:
            if champ not in self.champs or not self.champs[champ]:
                self.erreurs.append({
                    "code": "CHAMP_MANQUANT",
                    "champ": champ,
                    "message": f"❌ Champ obligatoire manquant : '{champ}' non détecté",
                    "action": f"Saisir manuellement '{champ}'"
                })

    def _valider_formats(self):
        # Valider dates
        for champ in ["date", "date_livraison", "date_commande", "date_echeance"]:
            if champ in self.champs:
                val = str(self.champs[champ])
                if not re.match(r'\d{1,4}[/-]\d{1,2}[/-]\d{2,4}', val):
                    self.erreurs.append({
                        "code": "FORMAT_DATE_INVALIDE",
                        "champ": champ,
                        "message": f"❌ Format de date invalide : '{val}'",
                        "action": "Format attendu : JJ/MM/AAAA"
                    })
                else:
                    self.champs[champ] = val.replace('-', '/')

        # Valider montants
        for champ in ["montant_ttc", "montant_ht", "montant_tva", "montant"]:
            if champ in self.champs:
                try:
                    val = float(str(self.champs[champ]).replace(',', '.'))
                    if val < 0:
                        raise ValueError
                    self.champs[champ] = val
                except:
                    self.erreurs.append({
                        "code": "MONTANT_INVALIDE",
                        "champ": champ,
                        "message": f"❌ Montant invalide : '{self.champs[champ]}'",
                        "action": "Le montant doit être un nombre positif"
                    })

    def _verifier_coherence(self):
        # HT + TVA doit = TTC
        if all(c in self.champs for c in ["montant_ht", "montant_tva", "montant_ttc"]):
            try:
                ht = float(self.champs["montant_ht"])
                tva = float(self.champs["montant_tva"])
                ttc = float(self.champs["montant_ttc"])
                if abs((ht + tva) - ttc) > 0.5:
                    self.erreurs.append({
                        "code": "INCOHERENCE_MONTANTS",
                        "champ": "montant_ttc",
                        "message": f"❌ Incohérence : HT({ht}) + TVA({tva}) = {ht+tva:.2f} ≠ TTC({ttc})",
                        "action": "Vérifiez les montants HT, TVA et TTC"
                    })
            except:
                pass
            
