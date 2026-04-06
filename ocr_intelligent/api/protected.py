import frappe
from frappe import _


def get_ocr_role(user):
    user_roles = frappe.get_roles(user)
    if "OCR Admin" in user_roles:
        return "OCR Admin"
    elif "OCR Validator" in user_roles:
        return "OCR Validator"
    elif "OCR Operator" in user_roles:
        return "OCR Operator"
    return None


@frappe.whitelist(allow_guest=True)
def get_current_user():
    if frappe.session.user == "Guest":
        frappe.throw(
            _("Non authentifié. Token invalide ou manquant."),
            frappe.AuthenticationError
        )

    role = get_ocr_role(frappe.session.user)

    if not role:
        frappe.throw(
            _("Aucun rôle OCR assigné."),
            frappe.PermissionError
        )

    return {
        "status": "success",
        "user": frappe.session.user,
        "full_name": frappe.get_value("User", frappe.session.user, "full_name"),
        "role": role,
    }


@frappe.whitelist(allow_guest=True)
def logout():
    frappe.local.login_manager.logout()
    return {
        "status": "success",
        "message": "Déconnecté avec succès"
    }