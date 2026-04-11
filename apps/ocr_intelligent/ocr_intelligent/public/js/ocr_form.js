// ═══════════════════════════════════════════════════════════════
// ocr_form.js — Groupe Bayoudh Metal
// Formulaire OCR à valider : dialog éditable avec tous les champs
// ═══════════════════════════════════════════════════════════════

const OCR_MAPPING = {
    facture: {
        numero_facture : "bill_no",
        date           : "bill_date",
        fournisseur    : "supplier",
        montant_ht     : "net_total",
        montant_tva    : "total_taxes_and_charges",
        montant_ttc    : "grand_total",
        date_echeance  : "due_date",
    },
    bon_livraison: {
        numero_bl      : "lr_no",
        date_livraison : "lr_date",
        fournisseur    : "supplier",
    },
    cheque: {
        numero_cheque  : "reference_no",
        montant        : "paid_amount",
        date           : "reference_date",
        banque         : "bank",
    },
    bon_commande: {
        numero_commande: "po_no",
        date_commande  : "transaction_date",
        fournisseur    : "supplier",
    },
    inconnu: {
        date           : "posting_date",
        montant_ttc    : "grand_total",
        fournisseur    : "supplier",
        reference      : "reference_no",
    }
};

// ── Champs à afficher dans le dialog de validation ─────────────
// Ordre d'affichage + labels + fieldtype pour le dialog
const OCR_DIALOG_FIELDS = [
    { ocr: "numero_facture",  frappe: "bill_no",                   label: "N° Facture Fournisseur",        type: "Data"     },
    { ocr: "fournisseur",     frappe: "supplier",                  label: "Fournisseur",                   type: "Link", options: "Supplier" },
    { ocr: "date",            frappe: "bill_date",                 label: "Date de la Facture du Fournisseur", type: "Date" },
    { ocr: "date_posting",    frappe: "posting_date",              label: "Date",                          type: "Date"     },
    { ocr: "date_echeance",   frappe: "due_date",                  label: "Date d'Échéance",               type: "Date"     },
    { ocr: "montant_ht",      frappe: "net_total",                 label: "Total net (TND)",               type: "Currency" },
    { ocr: "montant_tva",     frappe: "total_taxes_and_charges",   label: "Total des Taxes et Frais (TND)",type: "Currency" },
    { ocr: "montant_ttc",     frappe: "grand_total",               label: "Total TTC (TND)",               type: "Currency" },
];


// ── Module principal ────────────────────────────────────────────
const OCRForm = {

    init(frm) {
        if (frm._ocr_init) return;
        frm._ocr_init = true;
        frm.add_custom_button(__("Téléverser document"), () => {
            OCRForm._dialog_upload(frm);
        }, __("OCR"));
    },

    // ────────────────────────────────────────────────────────────
    // ÉTAPE 1 : Dialog d'upload (Attach)
    // ────────────────────────────────────────────────────────────
    _dialog_upload(frm) {
        const d = new frappe.ui.Dialog({
            title: __("Analyser un document (OCR)"),
            fields: [
                {
                    fieldtype: "HTML",
                    options: `<div style="background:var(--bg-light-blue);
                                border-left:3px solid var(--blue-500);
                                padding:10px 14px;border-radius:4px;
                                margin-bottom:12px;font-size:13px;">
                        <b>Formats acceptés :</b> PDF · PNG · JPG · JPEG · TIFF · BMP
                    </div>`
                },
                {
                    fieldtype : "Attach",
                    fieldname : "fichier",
                    label     : __("Sélectionner le fichier"),
                    reqd      : 1,
                }
            ],
            primary_action_label: __("Analyser"),
            primary_action(vals) {
                if (!vals.fichier) {
                    frappe.show_alert({ message: __("Sélectionnez un fichier."), indicator: "red" }, 3);
                    return;
                }
                d.hide();
                OCRForm._lancer(frm, vals.fichier);
            }
        });
        d.show();
    },

    // ────────────────────────────────────────────────────────────
    // ÉTAPE 2 : Envoi au pipeline OCR + NLP
    // ────────────────────────────────────────────────────────────
    _lancer(frm, file_url) {
        frappe.show_progress(__("OCR en cours..."), 30, 100, __("Analyse du document..."));

        frappe.call({
            method: "ocr_intelligent.api.ocr_pipeline.pipeline_complet",
            args: { file_url: file_url },
            freeze: false,
            callback(r) {
                frappe.hide_progress();
                const result = r.message || r;
                OCRForm._traiter(frm, result);
            },
            error() {
                frappe.hide_progress();
                frappe.msgprint({
                    title: __("Erreur"),
                    message: __("Impossible de contacter le serveur OCR."),
                    indicator: "red"
                });
            }
        });
    },

    // ────────────────────────────────────────────────────────────
    // ÉTAPE 3 : Traitement → ouvrir le formulaire de validation
    // ────────────────────────────────────────────────────────────
    _traiter(frm, result) {
        if (!result) {
            frappe.msgprint({ title: __("Erreur"), message: __("Réponse vide du serveur."), indicator: "red" });
            return;
        }

        if (!result.success) {
            frappe.msgprint({
                title: __("Document non traitable"),
                message: result.erreur || __("Erreur inconnue"),
                indicator: "red"
            });
            return;
        }

        const champs = result.champs_remplis || {};
        if (!Object.keys(champs).length) {
            frappe.msgprint({
                title: __("Aucun champ extrait"),
                message: __("Le document a été analysé mais aucun champ n'a pu être extrait."),
                indicator: "orange"
            });
            return;
        }

        // Ouvrir le formulaire de validation éditable
        OCRForm._dialog_validation(frm, champs, result);
    },

    // ────────────────────────────────────────────────────────────
    // ÉTAPE 4 : Formulaire OCR à valider (dialog éditable)
    //   L'utilisateur peut modifier chaque champ avant d'appliquer
    // ────────────────────────────────────────────────────────────
    _dialog_validation(frm, champs, result) {

        // Construire la liste de champs Frappe pour le dialog
        const fields = [];

        for (const def of OCR_DIALOG_FIELDS) {
            // Chercher la valeur : d'abord dans champs (par fieldname Frappe)
            let val = champs[def.frappe] || "";

            // Si pas trouvé par fieldname, chercher par nom OCR
            if (!val && def.ocr) {
                val = champs[def.ocr] || "";
            }

            // Convertir les dates DD/MM/YYYY ou DD-MM-YYYY → YYYY-MM-DD
            if (def.type === "Date" && val) {
                val = OCRForm._convertir_date(val);
            }

            // Convertir les montants texte → nombre
            if (def.type === "Currency" && val) {
                val = OCRForm._convertir_montant(val);
            }

            const field = {
                fieldtype : def.type,
                fieldname : def.frappe,
                label     : def.label,
                default   : val || undefined,
            };

            if (def.options) {
                field.options = def.options;
            }

            fields.push(field);
        }

        const d = new frappe.ui.Dialog({
            title: __("Formulaire OCR à valider"),
            fields: fields,
            size: "large",
            primary_action_label: __("Enregistrer"),
            secondary_action_label: __("Appliquer sans enregistrer"),
            secondary_action() {
                const vals = d.get_values();
                if (!vals) return;
                d.hide();
                OCRForm._appliquer_au_formulaire(frm, vals, false);
            },
            primary_action(vals) {
                if (!vals) return;
                d.hide();
                OCRForm._appliquer_au_formulaire(frm, vals, true);
            }
        });

        d.show();
    },

    // ────────────────────────────────────────────────────────────
    // Appliquer les valeurs du dialog au formulaire Purchase Invoice
    // ────────────────────────────────────────────────────────────
    _appliquer_au_formulaire(frm, vals, enregistrer) {

        // 1. Séparer les champs d'en-tête des montants
        const CHAMPS_MONTANTS = ["net_total", "total_taxes_and_charges", "grand_total"];
        const header_batch = {};
        const montants = {};

        for (const [fn, val] of Object.entries(vals)) {
            if (val === null || val === undefined || val === "") continue;
            if (CHAMPS_MONTANTS.includes(fn)) {
                montants[fn] = val;
            } else {
                header_batch[fn] = val;
            }
        }

        // 2. Résoudre les champs Link (Fournisseur, etc.)
        OCRForm._normaliser_liens(frm, header_batch).then(({ batch_valide, avertissements }) => {

            if (avertissements.length) {
                frappe.show_alert({
                    message: avertissements.join("<br>"),
                    indicator: "orange"
                }, 5);
            }

            // 3. Injecter les champs d'en-tête
            return frm.set_value(batch_valide);

        }).then(() => {
            // 4. Attendre que TOUS les hooks ERPNext terminent (supplier fetch, etc.)
            return new Promise(resolve => frappe.after_ajax(resolve));

        }).then(() => {
            // 5. Créer la ligne article (après que les hooks aient fini)
            return OCRForm._creer_ligne_article(frm, montants, vals);

        }).then(() => {
            // 6. Appliquer le modèle de taxes
            return OCRForm._appliquer_modele_taxes(frm);

        }).then(() => {
            // 7. Attendre encore (les taxes peuvent déclencher des recalculs)
            return new Promise(resolve => frappe.after_ajax(resolve));

        }).then(() => {
            // 8. Vérifier et forcer le expense_account sur chaque ligne article
            return OCRForm._forcer_expense_account(frm);

        }).then(() => {
            frm.refresh_fields();
            if (enregistrer) {
                return frm.save().then(() => {
                    frappe.show_alert({
                        message: __("Facture d'Achat sauvegardée avec succès"),
                        indicator: "green"
                    }, 4);
                });
            } else {
                frappe.show_alert({
                    message: __("Champs appliqués au formulaire"),
                    indicator: "blue"
                }, 3);
            }

        }).catch((err) => {
            console.error("OCR apply error:", err);
            frappe.show_alert({
                message: __("Erreur lors de l'application. Vérifiez les champs obligatoires."),
                indicator: "red"
            }, 5);
        });
    },

    // ── Convertir date DD/MM/YYYY ou DD-MM-YYYY → YYYY-MM-DD ───────
    _convertir_date(val) {
        if (!val) return "";
        const s = String(val).trim();

        // Déjà au format YYYY-MM-DD ?
        if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;

        // DD/MM/YYYY ou DD-MM-YYYY ou DD.MM.YYYY
        const m = s.match(/^(\d{1,2})[\/\-\.](\d{1,2})[\/\-\.](\d{4})$/);
        if (m) {
            const dd = m[1].padStart(2, "0");
            const mm = m[2].padStart(2, "0");
            return `${m[3]}-${mm}-${dd}`;
        }

        // DD/MM/YY
        const m2 = s.match(/^(\d{1,2})[\/\-\.](\d{1,2})[\/\-\.](\d{2})$/);
        if (m2) {
            const dd = m2[1].padStart(2, "0");
            const mm = m2[2].padStart(2, "0");
            const yyyy = parseInt(m2[3]) > 50 ? "19" + m2[3] : "20" + m2[3];
            return `${yyyy}-${mm}-${dd}`;
        }

        return s;
    },

    // ── Convertir montant texte → nombre ────────────────────────────
    _convertir_montant(val) {
        if (!val) return 0;
        let s = String(val).replace(/[^\d.,\s-]/g, "").trim();

        // Format tunisien : "1 438,128" ou "1.438,128"
        // Format anglais  : "1,438.128" ou "1438.128"
        if (s.includes(",") && s.includes(".")) {
            // Déterminer lequel est le séparateur décimal (le dernier)
            const lastComma = s.lastIndexOf(",");
            const lastDot   = s.lastIndexOf(".");
            if (lastComma > lastDot) {
                // 1.438,128 → virgule = décimal
                s = s.replace(/\./g, "").replace(",", ".");
            } else {
                // 1,438.128 → point = décimal
                s = s.replace(/,/g, "");
            }
        } else if (s.includes(",")) {
            // Virgule seule → peut être décimal (438,128) ou milliers (1,000)
            const parts = s.split(",");
            if (parts.length === 2 && parts[1].length === 3 && !parts[1].includes(" ")) {
                // Ambigu : considérer comme décimal (tunisien)
                s = s.replace(",", ".");
            } else {
                s = s.replace(",", ".");
            }
        }

        s = s.replace(/\s/g, "");
        return parseFloat(s) || 0;
    },

    // ── Vérifie les champs Link (ex: Supplier) ─────────────────────
    _normaliser_liens(frm, batch) {
        const out = { ...batch };
        const warnings = [];
        const checks = [];

        for (const [fn, val] of Object.entries(batch)) {
            const df = frappe.meta.get_docfield(frm.doctype, fn);
            if (!df || df.fieldtype !== "Link" || !df.options || !val) continue;

            checks.push(
                OCRForm._resoudre_valeur_link(df.options, String(val)).then((resolved) => {
                    if (!resolved) {
                        delete out[fn];
                        warnings.push(__("Fournisseur: {0} n'existe pas", [val]));
                    } else if (resolved !== val) {
                        out[fn] = resolved;
                    }
                })
            );
        }

        return Promise.all(checks).then(() => ({
            batch_valide: out,
            avertissements: warnings,
        }));
    },

    // ── Résoudre une valeur Link vers un enregistrement existant ────
    _resoudre_valeur_link(doctype, valeur) {
        return frappe.db.exists(doctype, valeur)
            .then((exists) => {
                if (exists) return valeur;

                return frappe.call({
                    method: "frappe.desk.search.search_link",
                    args: { doctype, txt: valeur, page_length: 5 },
                }).then((r) => {
                    const rows = r.message || [];
                    if (!rows.length) return null;

                    const normalized = rows.map((row) => {
                        if (Array.isArray(row)) return { value: row[0], description: row[1] || "" };
                        return { value: row.value || row.name || "", description: row.description || "" };
                    }).filter(x => x.value);

                    if (!normalized.length) return null;

                    const lower = valeur.toLowerCase();
                    const exact = normalized.find(x =>
                        x.value.toLowerCase() === lower ||
                        (x.description && x.description.toLowerCase().includes(lower))
                    );
                    return (exact || normalized[0]).value;
                });
            })
            .catch(() => null);
    },

    // ── Créer la ligne article automatiquement ──────────────────────
    _creer_ligne_article(frm, montants, vals) {
        const raw = montants.net_total || montants.grand_total || 0;
        const rate = typeof raw === "number" ? raw : (parseFloat(String(raw).replace(/\s/g, "").replace(",", ".")) || 0);

        const bill_no  = vals.bill_no || frm.doc.bill_no || "";
        const supplier = vals.supplier || frm.doc.supplier || "";
        const item_name = ("Facture OCR " + bill_no + " " + supplier).trim() || "Article OCR";

        // Vider les articles existants
        frm.doc.items = [];

        frm.add_child("items", {
            item_name        : item_name,
            description      : item_name,
            qty              : 1,
            rate             : rate,
            amount           : rate,
            uom              : "Unité",
            conversion_factor: 1,
            stock_qty        : 1,
        });

        frm.refresh_field("items");
        return Promise.resolve();
    },

    // ── Forcer expense_account sur toutes les lignes articles ───────
    _forcer_expense_account(frm) {
        const company = frm.doc.company;
        if (!company || !frm.doc.items || !frm.doc.items.length) return Promise.resolve();

        return frappe.db.get_value("Company", company, "default_expense_account")
            .then((r) => {
                const account = (r.message || {}).default_expense_account || "";
                if (!account) return;

                for (const item of frm.doc.items) {
                    if (!item.expense_account) {
                        frappe.model.set_value(item.doctype, item.name, "expense_account", account);
                    }
                }
            })
            .catch(() => {});
    },

    // ── Appliquer le modèle de taxes d'achat ────────────────────────
    _appliquer_modele_taxes(frm) {
        if (frm.doc.taxes_and_charges) return Promise.resolve();

        return frappe.call({
            method: "frappe.client.get_list",
            args: {
                doctype: "Purchase Taxes and Charges Template",
                filters: { company: frm.doc.company },
                fields: ["name", "is_default"],
                limit_page_length: 5,
                order_by: "is_default desc, name asc"
            }
        }).then((r) => {
            const templates = (r.message || r) || [];
            if (!templates.length) return;

            const tpl = templates[0].name;
            frm.set_value("taxes_and_charges", tpl);

            return frappe.call({
                method: "erpnext.controllers.accounts_controller.get_taxes_and_charges",
                args: {
                    master_doctype: "Purchase Taxes and Charges Template",
                    master_name: tpl
                }
            }).then((tax_r) => {
                const rows = tax_r.message || [];
                frm.doc.taxes = [];
                for (const row of rows) {
                    frm.add_child("taxes", row);
                }
                frm.refresh_field("taxes");
            });
        }).catch((err) => {
            console.warn("OCR: impossible de charger le modèle de taxes", err);
        });
    },
};


// ── Intégration dans les formulaires Frappe ─────────────────────
frappe.ui.form.on("Purchase Invoice", { refresh(frm) { OCRForm.init(frm); } });
frappe.ui.form.on("OCR Document",     { refresh(frm) { OCRForm.init(frm); } });
