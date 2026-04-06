app_name = "ocr_intelligent"
app_title = "OCR Intelligent"
app_publisher = "OCR automatique pour les documents financiers"
app_description = "Bayoudh Metal"
app_email = "soulaymazay@gmail.com"
app_license = "MIT"

# ─────────────────────────────────────────────────────────────────────
# JS chargé dans toutes les pages du bureau Frappe
# ─────────────────────────────────────────────────────────────────────

app_include_js = [
    "/assets/ocr_intelligent/js/ocr_form.js"
]

# ─────────────────────────────────────────────────────────────────────
# Hook sur File : déclenche l'OCR automatique après chaque upload
# ─────────────────────────────────────────────────────────────────────

doc_events = {
    "File": {
        "after_insert": "ocr_intelligent.api.auto_create_document.auto_create_ocr_document"
    }
}