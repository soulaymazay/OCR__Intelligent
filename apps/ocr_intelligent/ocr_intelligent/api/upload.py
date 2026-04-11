"""
upload.py - Groupe Bayoudh Metal
Gère l'upload des fichiers et déclenche le traitement OCR
"""

import frappe
from frappe import _
import os


@frappe.whitelist()
def upload_document():
    """
    Reçoit un fichier uploadé et le traite avec l'OCR
    
    Utilisation :
    - Via formulaire Frappe (formulaire de upload)
    - Via API multipart/form-data
    
    Retourne :
    {
        "success": True/False,
        "message": "...",
        "document_id": "...",
        "file_name": "..."
    }
    """
    
    try:
        # Récupérer le fichier depuis la requête
        files = frappe.request.files
        
        if not files or 'file' not in files:
            return {
                "success": False,
                "erreur": "❌ Aucun fichier reçu. Veuillez sélectionner un fichier."
            }
        
        file = files['file']
        
        if not file or not file.filename:
            return {
                "success": False,
                "erreur": "❌ Le fichier est vide ou invalide."
            }
        
        # Vérifier l'extension
        extensions_acceptees = ['.pdf', '.png', '.jpg', '.jpeg', '.tiff', '.bmp']
        ext = os.path.splitext(file.filename)[1].lower()
        
        if ext not in extensions_acceptees:
            return {
                "success": False,
                "erreur": f"❌ Format de fichier non supporté. Acceptés: {', '.join(extensions_acceptees)}"
            }
        
        # Créer un document File dans Frappe
        file_doc = frappe.get_doc({
            "doctype": "File",
            "file_name": file.filename,
            "file_content": file.read(),
            "is_private": 1  # Stocker en privé
        })
        
        file_doc.insert(ignore_permissions=True)
        frappe.db.commit()
        
        return {
            "success": True,
            "message": f"✅ Fichier '{file.filename}' uploadé avec succès",
            "file_id": file_doc.name,
            "file_name": file.filename,
            "file_url": file_doc.file_url
        }
        
    except Exception as e:
        frappe.log_error(f"Erreur upload: {str(e)}", "OCR Upload")
        return {
            "success": False,
            "erreur": f"❌ Erreur lors de l'upload: {str(e)}"
        }


@frappe.whitelist(allow_guest=True)
def upload_document_direct(nom_fichier=None):
    """
    Version simplifiée pour uploader directement un fichier depuis une URL ou chemin local
    """
    try:
        if not nom_fichier:
            return {
                "success": False,
                "erreur": "❌ Nom de fichier requis"
            }
        
        # Vérifier si le fichier existe localement
        site_path = frappe.get_site_path()
        chemin_possible = os.path.join(site_path, "private", "files", nom_fichier)
        
        if not os.path.exists(chemin_possible):
            return {
                "success": False,
                "erreur": f"❌ Fichier '{nom_fichier}' introuvable"
            }
        
        # Créer un document File
        with open(chemin_possible, 'rb') as f:
            file_content = f.read()
        
        file_doc = frappe.get_doc({
            "doctype": "File",
            "file_name": nom_fichier,
            "file_content": file_content,
            "is_private": 1
        })
        
        file_doc.insert(ignore_permissions=True)
        frappe.db.commit()
        
        return {
            "success": True,
            "message": f"✅ Fichier '{nom_fichier}' traité",
            "file_id": file_doc.name,
            "file_url": file_doc.file_url
        }
        
    except Exception as e:
        frappe.log_error(f"Erreur upload direct: {str(e)}", "OCR Upload")
        return {
            "success": False,
            "erreur": f"❌ Erreur: {str(e)}"
        }