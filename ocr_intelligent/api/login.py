import frappe
from frappe import _
import secrets

@frappe.whitelist(allow_guest=True)
def login(email=None, password=None):

    if not email or not password:
        frappe.throw(_("Email et mot de passe requis."))

    try:
        login_manager = frappe.auth.LoginManager()
        login_manager.authenticate(user=email, pwd=password)
        login_manager.post_login()

        user_roles = frappe.get_roles(email)

        ocr_role = None
        if "OCR Admin" in user_roles:
            ocr_role = "OCR Admin"
        elif "OCR Validator" in user_roles:
            ocr_role = "OCR Validator"
        elif "OCR Operator" in user_roles:
            ocr_role = "OCR Operator"
        else:
            frappe.local.login_manager.logout()
            frappe.throw(_("Accès refusé : aucun rôle OCR assigné."))

        # Générer API KEY et SECRET
        user = frappe.get_doc("User", email)

        if not user.api_key:
            user.api_key = frappe.generate_hash(length=15)

        api_secret = secrets.token_hex(16)
        user.api_secret = api_secret
        user.save(ignore_permissions=True)

        token = f"{user.api_key}:{api_secret}"

        return {
            "status": "success",
            "message": "Connecté avec succès",
            "user": email,
            "role": ocr_role,
            "full_name": frappe.get_value("User", email, "full_name"),
            "token": token
        }

    except frappe.AuthenticationError:
        frappe.clear_messages()
        frappe.throw(_("Email ou mot de passe incorrect."), frappe.AuthenticationError)