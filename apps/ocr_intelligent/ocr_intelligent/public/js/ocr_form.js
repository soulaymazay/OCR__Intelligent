// ocr_form.js
// v5 — Recalcul HT/TVA côté client (filet de sécurité TVA multi-taux)

// ── Mapping OCR → Frappe (aligné avec field_matcher.py) ────────
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
        date_cheque    : "reference_date",
        banque         : "bank",
        beneficiaire   : "party",
    },
    traite: {
        numero_traite  : "reference_no",
        draft_number   : "reference_no",
        date_echeance  : "reference_date",
        montant        : "paid_amount",
        tireur         : "party",
        drawer         : "party",
        tire           : "bank",
        drawee         : "bank",
        date_emission  : "custom_issue_date",
        issue_date     : "custom_issue_date",
        beneficiaire   : "custom_beneficiary",
        domiciliation  : "custom_domiciliation",
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

// ── Champs facture ──────────────────────────────────────────────
const OCR_DIALOG_FIELDS = [
    { ocr: "numero_facture",  frappe: "bill_no",                    label: "N° Facture Fournisseur",            type: "Data"     },
    { ocr: "fournisseur",     frappe: "supplier",                   label: "Fournisseur",                       type: "Link", options: "Supplier" },
    { ocr: "date",            frappe: "bill_date",                  label: "Date de la Facture du Fournisseur", type: "Date"     },
    { ocr: "date",            frappe: "posting_date",               label: "Date",                              type: "Date"     },
    { ocr: "date_echeance",   frappe: "due_date",                   label: "Date d'Échéance",                   type: "Date"     },
    { ocr: "montant_ht",      frappe: "net_total",                  label: "Total net (TND)",                   type: "Currency" },
    { ocr: "montant_tva",     frappe: "total_taxes_and_charges",    label: "Total des Taxes et Frais (TND)",    type: "Currency" },
    { ocr: "montant_ttc",     frappe: "grand_total",                label: "Total TTC (TND)",                   type: "Currency" },
];

// ── Champs chèque ───────────────────────────────────────────────
const OCR_DIALOG_FIELDS_CHEQUE = [
    { ocr: "numero_cheque",    frappe: "reference_no",          label: "N° Chèque / Référence",  type: "Data"     },
    { ocr: "date_cheque",      frappe: "reference_date",        label: "Date du Chèque",          type: "Date"     },
    { ocr: "amount",           frappe: "paid_amount",           label: "Montant (TND)",           type: "Currency" },
    { ocr: "beneficiaire",     frappe: "party",                 label: "Fournisseur / Tiers",     type: "Data"     },
    { ocr: "banque",           frappe: "bank",                  label: "Banque",                  type: "Data"     },
    { ocr: "titulaire_compte", frappe: "account_holder_name",   label: "Titulaire du Compte",     type: "Data"     },
    { ocr: "rib",              frappe: "reference_no",          label: "RIB",                     type: "Data"     },
];

// ── Champs traite ───────────────────────────────────────────────
const OCR_DIALOG_FIELDS_TRAITE = [
    {
        // N° traite : reference_no | numero_traite | draft_number
        ocr: "numero_traite",
        frappe: "reference_no",
        label: "N° Traite / Référence",
        type: "Data",
        aliases: ["reference_no", "numero_traite", "draft_number"],
    },
    {
        // Date échéance : reference_date | date_echeance | due_date
        ocr: "date_echeance",
        frappe: "reference_date",
        label: "Date d'Échéance",
        type: "Date",
        aliases: ["reference_date", "date_echeance", "due_date"],
    },
    {
        // Date émission : custom_issue_date | date_emission | issue_date
        ocr: "date_emission",
        frappe: "custom_issue_date",
        label: "Date d'Émission",
        type: "Date",
        aliases: ["custom_issue_date", "date_emission", "issue_date"],
    },
    {
        // Montant : paid_amount | amount | montant
        ocr: "montant",
        frappe: "paid_amount",
        label: "Montant (TND)",
        type: "Currency",
        aliases: ["paid_amount", "amount", "montant"],
    },
    {
        // Tireur : party | tireur | drawer
        ocr: "tireur",
        frappe: "party",
        label: "Tireur (Émetteur)",
        type: "Data",
        aliases: ["party", "tireur", "drawer"],
    },
    {
        // Tiré (banque) : bank | tire | drawee
        ocr: "tire",
        frappe: "bank",
        label: "Tiré (Banque/Personne)",
        type: "Data",
        aliases: ["bank", "tire", "drawee"],
    },
    {
        ocr: "beneficiaire",
        frappe: "custom_beneficiary",
        label: "Bénéficiaire",
        type: "Data",
        aliases: ["custom_beneficiary", "beneficiaire"],
    },
    {
        ocr: "domiciliation",
        frappe: "custom_domiciliation",
        label: "Domiciliation",
        type: "Data",
        aliases: ["custom_domiciliation", "domiciliation"],
    },
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

    // ── ÉTAPE 1 : Dialog d'upload ──────────────────────────────
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

    // ── ÉTAPE 2 : Envoi pipeline OCR ──────────────────────────
    _lancer(frm, file_url) {
        if (!frm || !file_url) {
            frappe.msgprint({ title: __("Erreur"), message: __("Paramètres OCR invalides."), indicator: "red" });
            return;
        }
        frappe.show_progress(__("OCR en cours..."), 20, 100, __("Envoi du fichier..."));

        frappe.call({
            method: "ocr_intelligent.api.ocr_pipeline.pipeline_complet",
            args: {
                file_url       : file_url,
                source_doctype : frm.doctype,
                payment_method : frm.doc.mode_of_payment || "",
            },
            freeze: false,
            callback(r) {
                const result = r && r.message;
                if (!result || !result.success) {
                    frappe.hide_progress();
                    frappe.msgprint({
                        title    : __("Erreur"),
                        message  : (result && result.erreur) || __("Réponse inattendue du serveur."),
                        indicator: "red"
                    });
                    return;
                }
                if (result.async && result.job_id) {
                    frappe.show_progress(__("OCR en cours..."), 40, 100, __("Analyse du document..."));
                    OCRForm._poll_ocr(frm, result.job_id, Date.now(), 0);
                } else {
                    frappe.hide_progress();
                    OCRForm._traiter(frm, result);
                }
            },
            error() {
                frappe.hide_progress();
                frappe.msgprint({ title: __("Erreur"), message: __("Impossible de contacter le serveur OCR."), indicator: "red" });
            }
        });
    },

    // ── ÉTAPE 2b : Polling ─────────────────────────────────────
    _poll_ocr(frm, job_id, startTime, retryCount = 0) {
        const TIMEOUT_MS  = 480000;
        const INTERVAL_MS = 2500;
        const MAX_RETRIES = 8;

        if (Date.now() - startTime > TIMEOUT_MS) {
            frappe.hide_progress();
            frappe.msgprint({
                title    : __("Délai dépassé"),
                message  : __("L'analyse OCR prend trop longtemps. Veuillez réessayer."),
                indicator: "orange"
            });
            return;
        }

        const pct = Math.min(40 + Math.floor(((Date.now() - startTime) / TIMEOUT_MS) * 50), 90);
        frappe.show_progress(__("OCR en cours..."), pct, 100, __("Analyse du document..."));

        frappe.call({
            method  : "ocr_intelligent.api.ocr_pipeline.get_ocr_statut",
            args    : { job_id },
            freeze  : false,
            callback(r) {
                const s = (r && r.message) || {};
                if (s.status === "termine") {
                    frappe.hide_progress();
                    OCRForm._traiter(frm, s.result || {});
                } else if (s.status === "erreur") {
                    frappe.hide_progress();
                    frappe.msgprint({ title: __("Erreur OCR"), message: s.erreur || __("Erreur inconnue."), indicator: "red" });
                } else {
                    setTimeout(() => OCRForm._poll_ocr(frm, job_id, startTime, 0), INTERVAL_MS);
                }
            },
            error() {
                if (retryCount + 1 > MAX_RETRIES) {
                    frappe.hide_progress();
                    frappe.msgprint({ title: __("OCR indisponible"), message: __("Service OCR ne répond pas."), indicator: "red" });
                    return;
                }
                const backoff = INTERVAL_MS + Math.min((retryCount + 1) * 600, 4000);
                setTimeout(() => OCRForm._poll_ocr(frm, job_id, startTime, retryCount + 1), backoff);
            }
        });
    },

    // ── ÉTAPE 3 : Traitement résultat ─────────────────────────
    _traiter(frm, result) {
        if (!result) {
            frappe.msgprint({ title: __("Erreur"), message: __("Réponse vide."), indicator: "red" });
            return;
        }
        if (!result.success) {
            frappe.msgprint({
                title    : result.titre ? __(result.titre) : __("Document non traitable"),
                message  : (result.erreur || __("Erreur inconnue")).replace(/\n/g, "<br>"),
                indicator: "red"
            });
            return;
        }
        if (result.doublon) {
            OCRForm._dialog_doublon(frm, result);
            return;
        }

        const champs = (frm.doctype === "Payment Entry")
            ? (result.form_fields || result.champs_remplis || {})
            : (result.champs_remplis || result.form_fields || {});

        if (!Object.keys(champs).length) {
            frappe.msgprint({ title: __("Aucun champ extrait"), message: __("Aucun champ extrait."), indicator: "orange" });
            return;
        }

        const uncertain = result.uncertain_fields || [];
        if (uncertain.length) {
            frappe.show_alert({ message: __("Champs incertains : {0}", [uncertain.join(", ")]), indicator: "orange" }, 7);
        }

        OCRForm._dialog_validation(frm, champs, result);
    },

    // ── Dialog doublon ─────────────────────────────────────────
    _dialog_doublon(frm, result) {
        const num  = result.numero_facture || "—";
        const date = result.doublon_date   || "—";
        const name = result.doublon_name   || "";
        const d    = new frappe.ui.Dialog({
            title: __("⚠️ Doublon détecté"),
            fields: [{
                fieldtype: "HTML",
                options: `<div style="background:#fff8e1;border-left:4px solid #f9a825;padding:16px 18px;border-radius:4px;font-size:14px;">
                    <b>⚠️ ${__("Doublon détecté")}</b><br><br>
                    ${__("La facture N°")} <b>${frappe.utils.escape_html(num)}</b>
                    ${__("a déjà été importée le")} <b>${frappe.utils.escape_html(date)}</b>.<br><br>
                    <b>${__("Consulter l'existant ou forcer l'import ?")}</b>
                </div>`
            }],
            primary_action_label   : __("Consulter l'existant"),
            secondary_action_label : __("Forcer l'import"),
            secondary_action() { d.hide(); OCRForm._dialog_validation(frm, result.champs_remplis || {}, result); },
            primary_action()   { d.hide(); frappe.set_route("Form", "Purchase Invoice", name); }
        });
        d.show();
    },

    // ── ÉTAPE 4 : Formulaire de validation ────────────────────
    _dialog_validation(frm, champs, result) {
        champs   = (champs && typeof champs === "object") ? champs : {};
        const type_doc = (result && result.type_document) || "";

        // ── Recalcul HT/TVA côté client (filet de sécurité TVA multi-taux) ──
        // Garantit que HT et TVA sont corrects même si le serveur n'a pas pu
        // les calculer (ex: facture avec TVA multi-taux 5.5% + 20%).
        const _toNum = (k) => {
            const v = champs[k];
            if (v === null || v === undefined || v === "") return 0;
            const n = OCRForm._convertir_montant(v);
            return isFinite(n) ? n : 0;
        };

        // Lire TTC, TVA et HT depuis toutes les clés possibles
        const ttc_client = _toNum("montant_ttc") || _toNum("grand_total");
        const tva_client = _toNum("montant_tva") || _toNum("total_taxes_and_charges");
        const ht_client  = _toNum("montant_ht")  || _toNum("net_total");

        // Si TTC et TVA sont connus mais HT = 0 → recalculer HT
        if (ttc_client > 0 && tva_client > 0 && ht_client <= 0) {
            const ht_calc = Math.round((ttc_client - tva_client) * 1000) / 1000;
            if (ht_calc > 0) {
                champs["montant_ht"] = ht_calc;
                champs["net_total"]  = ht_calc;
            }
        }

        // Si TTC et HT sont connus mais TVA = 0 → recalculer TVA
        if (ttc_client > 0 && ht_client > 0 && tva_client <= 0) {
            const tva_calc = Math.round((ttc_client - ht_client) * 1000) / 1000;
            if (tva_calc > 0) {
                champs["montant_tva"]             = tva_calc;
                champs["total_taxes_and_charges"] = tva_calc;
            }
        }
        // ─────────────────────────────────────────────────────────────────

        let defs;
        if (frm.doctype === "Payment Entry") {
            defs = type_doc === "traite" ? OCR_DIALOG_FIELDS_TRAITE : OCR_DIALOG_FIELDS_CHEQUE;
        } else {
            defs = OCR_DIALOG_FIELDS;
        }

        const fields = [];

        for (const def of defs) {
            let val = "";

            // Résolution avec aliases (liste ordonnée de clés à essayer)
            const keys_to_try = [
                ...(def.aliases || []),
                def.frappe,
                def.ocr,
            ].filter(Boolean);

            for (const key of keys_to_try) {
                const candidate = champs[key];
                if (candidate !== undefined && candidate !== null && candidate !== "" && candidate !== 0) {
                    val = candidate;
                    break;
                }
            }

            // Cas spécial : date de référence chèque
            if (!val && type_doc === "cheque" && def.frappe === "reference_date") {
                val = (result && result.date_cheque_retenue) || "";
            }

            // Conversion de type
            if (def.type === "Date" && val) {
                val = OCRForm._convertir_date(String(val));
            }

            if (def.type === "Currency" && val !== "" && val !== null && val !== undefined) {
                const converted = OCRForm._convertir_montant(val);
                val = (converted && converted > 0) ? converted : "";
            }

            const field = {
                fieldtype : def.type,
                fieldname : def.frappe,
                label     : def.label,
                default   : (val !== "" && val !== null && val !== undefined) ? val : undefined,
            };
            if (def.options) field.options = def.options;
            fields.push(field);
        }

        const d = new frappe.ui.Dialog({
            title               : __("Formulaire OCR à valider"),
            fields              : fields,
            size                : "large",
            primary_action_label: __("Enregistrer"),
            secondary_action_label: __("Appliquer sans enregistrer"),
            secondary_action() {
                const vals = d.get_values();
                if (!vals) return;
                d.hide();
                OCRForm._appliquer_au_formulaire(frm, vals, false, result);
            },
            primary_action(vals) {
                if (!vals) return;
                d.hide();
                OCRForm._appliquer_au_formulaire(frm, vals, true, result);
            }
        });
        d.show();
    },

    // ── Application au formulaire ──────────────────────────────
    _appliquer_au_formulaire(frm, vals, enregistrer, result) {
        vals = (vals && typeof vals === "object") ? vals : {};
        if (frm.doctype === "Payment Entry") {
            const type_doc = (result && result.type_document) || "";
            return type_doc === "traite"
                ? OCRForm._appliquer_traite(frm, vals, enregistrer)
                : OCRForm._appliquer_cheque(frm, vals, enregistrer);
        }

        const CHAMPS_MONTANTS = ["net_total", "total_taxes_and_charges", "grand_total"];
        const header_batch    = {};
        const montants        = {};

        for (const [fn, val] of Object.entries(vals)) {
            if (val === null || val === undefined || val === "") continue;
            if (CHAMPS_MONTANTS.includes(fn)) montants[fn] = val;
            else header_batch[fn] = val;
        }

        OCRForm._normaliser_liens(frm, header_batch)
            .then(({ batch_valide, liens_manquants }) => {
                if (liens_manquants.length) {
                    frappe.msgprint({
                        title    : __("Enregistrement introuvable"),
                        indicator: "orange",
                        message  : __("<b>{0}</b> n'existe pas dans <b>{1}</b>. Les autres champs ont été appliqués.",
                            [liens_manquants[0].valeur, liens_manquants[0].doctype]),
                    });
                    enregistrer = false;
                }
                return frm.set_value(batch_valide);
            })
            .then(() => new Promise(r => frappe.after_ajax(r)))
            .then(() => OCRForm._creer_ligne_article(frm, montants, vals))
            .then(() => OCRForm._appliquer_modele_taxes(frm))
            .then(() => new Promise(r => frappe.after_ajax(r)))
            .then(() => OCRForm._forcer_expense_account(frm))
            .then(() => {
                frm.refresh_fields();
                if (enregistrer) {
                    return frm.save().then(() => {
                        frappe.show_alert({ message: __("Facture d'Achat sauvegardée"), indicator: "green" }, 4);
                    });
                }
                frappe.show_alert({ message: __("Champs appliqués"), indicator: "blue" }, 3);
            })
            .catch(err => {
                console.error("OCR apply error:", err);
                frappe.show_alert({ message: __("Erreur lors de l'application."), indicator: "red" }, 5);
            });
    },

    // ── Application chèque ─────────────────────────────────────
    _appliquer_cheque(frm, vals, enregistrer) {
        vals = (vals && typeof vals === "object") ? vals : {};
        return frm.set_value("payment_type", "Pay")
            .then(() => new Promise(r => frappe.after_ajax(r)))
            .then(() => frm.set_value("party_type", "Supplier"))
            .then(() => new Promise(r => frappe.after_ajax(r)))
            .then(() => {
                const nom = vals.party || "";
                if (!nom) return;
                return OCRForm._resoudre_valeur_link("Supplier", String(nom)).then(resolved => {
                    if (resolved) return frm.set_value("party", resolved);
                    frappe.show_alert({ message: __("Fournisseur '{0}' introuvable.", [nom]), indicator: "orange" }, 6);
                });
            })
            .then(() => new Promise(r => frappe.after_ajax(r)))
            .then(() => {
                const simple = {};
                if (vals.reference_no)   simple.reference_no   = vals.reference_no;
                if (vals.reference_date) simple.reference_date = OCRForm._convertir_date(vals.reference_date);
                if (vals.paid_amount)    simple.paid_amount     = OCRForm._convertir_montant(vals.paid_amount);
                if (vals.bank)           simple.bank            = vals.bank;
                if (!Object.keys(simple).length) return;
                return frm.set_value(simple);
            })
            .then(() => {
                frm.refresh_fields();
                if (enregistrer) return frm.save().then(() =>
                    frappe.show_alert({ message: __("Écriture de Paiement (Chèque) sauvegardée"), indicator: "green" }, 4));
                frappe.show_alert({ message: __("Champs chèque appliqués"), indicator: "blue" }, 3);
            })
            .catch(err => {
                console.error("OCR cheque apply error:", err);
                frappe.show_alert({ message: __("Erreur lors de l'application chèque."), indicator: "red" }, 5);
            });
    },

    // ── Application traite ─────────────────────────────────────
    _appliquer_traite(frm, vals, enregistrer) {
        vals = (vals && typeof vals === "object") ? vals : {};
        return frm.set_value("payment_type", "Pay")
            .then(() => new Promise(r => frappe.after_ajax(r)))
            .then(() => frm.set_value("party_type", "Supplier"))
            .then(() => new Promise(r => frappe.after_ajax(r)))
            .then(() => {
                const nom = vals.party || "";
                if (!nom) return;
                return OCRForm._resoudre_valeur_link("Supplier", String(nom)).then(resolved => {
                    if (resolved) return frm.set_value("party", resolved);
                    frappe.show_alert({ message: __("Tireur '{0}' introuvable.", [nom]), indicator: "orange" }, 6);
                });
            })
            .then(() => new Promise(r => frappe.after_ajax(r)))
            .then(() => {
                const simple = {};
                if (vals.reference_no)          simple.reference_no          = vals.reference_no;
                if (vals.reference_date)         simple.reference_date        = OCRForm._convertir_date(vals.reference_date);
                if (vals.paid_amount)            simple.paid_amount           = OCRForm._convertir_montant(vals.paid_amount);
                if (vals.bank)                   simple.bank                  = vals.bank;
                if (vals.custom_issue_date)      simple.custom_issue_date     = OCRForm._convertir_date(vals.custom_issue_date);
                if (vals.custom_beneficiary)     simple.custom_beneficiary    = vals.custom_beneficiary;
                if (vals.custom_domiciliation)   simple.custom_domiciliation  = vals.custom_domiciliation;
                if (!Object.keys(simple).length) return;
                return frm.set_value(simple);
            })
            .then(() => {
                frm.refresh_fields();
                if (enregistrer) return frm.save().then(() =>
                    frappe.show_alert({ message: __("Écriture de Paiement (Traite) sauvegardée"), indicator: "green" }, 4));
                frappe.show_alert({ message: __("Champs traite appliqués"), indicator: "blue" }, 3);
            })
            .catch(err => {
                console.error("OCR traite apply error:", err);
                frappe.show_alert({ message: __("Erreur lors de l'application traite."), indicator: "red" }, 5);
            });
    },

    // ── Convertir date → YYYY-MM-DD ─────────────────────────
    _convertir_date(val) {
        if (!val) return "";
        const s = String(val).trim();
        if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;

        const m = s.match(/^(\d{1,2})[\/\-\.](\d{1,2})[\/\-\.](\d{4})$/);
        if (m) return `${m[3]}-${m[2].padStart(2,"0")}-${m[1].padStart(2,"0")}`;

        const m2 = s.match(/^(\d{1,2})[\/\-\.](\d{1,2})[\/\-\.](\d{2})$/);
        if (m2) {
            const yyyy = parseInt(m2[3]) > 50 ? "19" + m2[3] : "20" + m2[3];
            return `${yyyy}-${m2[2].padStart(2,"0")}-${m2[1].padStart(2,"0")}`;
        }
        return s;
    },

    // ── Convertir montant → nombre ──────────────────────────
    // Gestion des formats de montants avec separateurs mixtes.
    _convertir_montant(val) {
        if (!val && val !== 0) return 0;
        if (typeof val === "number") return isFinite(val) ? val : 0;

        let s = String(val).trim();

        // Parenthèses = négatif
        let isNeg = false;
        if (/^\(.*\)$/.test(s)) { isNeg = true; s = s.slice(1, -1).trim(); }

        // Nettoyer symboles et espaces insécables
        s = s.replace(/[\u00A0\u202F]/g, " ").trim();
        s = s.replace(/^\s*(?:TND|DT|EUR|€|\$)\s*/i, "").replace(/\s*(?:TND|DT|EUR|€|\$)\s*$/i, "").trim();
        s = s.replace(/['']/g, "").replace(/[^\d,\.\-\s]/g, "").trim();
        if (!s) return 0;

        // Cas 1 : espaces présents → séparateurs de milliers
        if (/\s/.test(s)) {
            const sansEsp = s.replace(/\s+/g, "");
            const norm    = sansEsp.replace(",", ".");
            const v       = parseFloat(norm);
            return isNaN(v) ? 0 : (isNeg ? -Math.abs(v) : v);
        }

        // Cas 2 : virgule ET point
        if (s.includes(",") && s.includes(".")) {
            const lastComma = s.lastIndexOf(",");
            const lastDot   = s.lastIndexOf(".");
            if (lastComma > lastDot) {
                // "2.520,000" → point=milliers, virgule=décimal
                s = s.replace(/\./g, "").replace(",", ".");
            } else {
                // "2,520.000" → virgule=milliers
                s = s.replace(/,/g, "");
            }
        }
        // Cas 3 : virgule seule
        else if (s.includes(",")) {
            const parts = s.split(",");
            if (parts.length === 2) {
                const after = parts[1];
                if (after.length === 3 && /^\d{3}$/.test(after)) {
                    // Ambigu : si partie entière ≥ 4 chiffres → milliers
                    if (parts[0].length >= 4) s = parts[0] + after;
                    else                      s = parts[0] + "." + after;
                } else {
                    s = s.replace(",", ".");
                }
            }
        }
        // Cas 4 : point seul avec exactement 3 décimales → milliers tunisien
        else if (s.includes(".")) {
            const parts = s.split(".");
            if (parts.length === 2 && /^\d{3}$/.test(parts[1])) {
                const entierVal = parseInt(parts[0], 10);
                // "520.000" → 520.000 (décimal), "2520.000" → 2520.000 (décimal)
                // "2.520"   → 2520 (milliers) si entier < 10
                if (entierVal < 10) s = parts[0] + parts[1];
                // sinon laisser tel quel (520.000 TND = 520 dinars)
            }
        }

        const v = parseFloat(s);
        if (isNaN(v)) return 0;
        return isNeg ? -Math.abs(v) : v;
    },

    // ── Vérifier champs Link ───────────────────────────────
    _normaliser_liens(frm, batch) {
        const out = { ...batch };
        const warnings = [], liens_manquants = [], checks = [];

        if (!frm || !batch) return Promise.resolve({ batch_valide: out, avertissements: warnings, liens_manquants });

        for (const [fn, val] of Object.entries(batch)) {
            const df = frappe.meta.get_docfield(frm.doctype, fn);
            if (!df || df.fieldtype !== "Link" || !df.options || !val) continue;
            checks.push(
                OCRForm._resoudre_valeur_link(df.options, String(val)).then(resolved => {
                    if (!resolved) {
                        delete out[fn];
                        liens_manquants.push({ fieldname: fn, doctype: df.options, valeur: val });
                    } else if (resolved !== val) {
                        out[fn] = resolved;
                    }
                })
            );
        }
        return Promise.all(checks).then(() => ({ batch_valide: out, avertissements: warnings, liens_manquants }));
    },

    // ── Résoudre valeur Link ───────────────────────────────
    _resoudre_valeur_link(doctype, valeur) {
        return frappe.db.exists(doctype, valeur)
            .then(exists => {
                if (exists) return valeur;
                return frappe.call({
                    method: "frappe.desk.search.search_link",
                    args  : { doctype, txt: valeur, page_length: 5 },
                }).then(r => {
                    const rows = (r.message || []).map(row =>
                        Array.isArray(row) ? { value: row[0] } : { value: row.value || row.name || "" }
                    ).filter(x => x.value);
                    if (!rows.length) return null;
                    const lower = valeur.toLowerCase();
                    const exact = rows.find(x => x.value.toLowerCase() === lower);
                    return (exact || rows[0]).value;
                });
            })
            .catch(() => null);
    },

    // ── Créer ligne article ────────────────────────────────
    _creer_ligne_article(frm, montants, vals) {
        montants = montants || {};
        vals     = vals     || {};
        const raw  = montants.net_total || montants.grand_total || 0;
        const rate = typeof raw === "number" ? raw : OCRForm._convertir_montant(raw);
        const desc = ("Facture OCR " + (vals.bill_no || frm.doc.bill_no || "") + " " + (vals.supplier || frm.doc.supplier || "")).trim() || "Article OCR";

        return frappe.db.exists("Item", "Article OCR").then(exists => {
            if (!exists) {
                frm.doc.items = [];
                frm.refresh_field("items");
                frappe.show_alert({ message: __("Article 'Article OCR' introuvable. Ajoutez manuellement."), indicator: "orange" }, 6);
                return;
            }
            return frappe.db.exists("UOM", "Unité").then(uom_exists => {
                const uom = uom_exists ? "Unité" : "Nos";
                frm.doc.items = [];
                frm.add_child("items", {
                    item_code: "Article OCR", item_name: desc, description: desc,
                    qty: 1, rate, amount: rate, uom, stock_uom: uom,
                    conversion_factor: 1, stock_qty: 1,
                });
                frm.refresh_field("items");
            });
        });
    },

    // ── Forcer expense_account ─────────────────────────────
    _forcer_expense_account(frm) {
        if (!frm.doc.company || !frm.doc.items?.length) return Promise.resolve();
        return frappe.db.get_value("Company", frm.doc.company, "default_expense_account")
            .then(r => {
                const account = (r.message || {}).default_expense_account || "";
                if (!account) return;
                const updates = frm.doc.items
                    .filter(item => !item.expense_account)
                    .map(item => frappe.model.set_value(item.doctype, item.name, "expense_account", account));
                if (updates.length) return Promise.all(updates).then(() => new Promise(r => frappe.after_ajax(r)));
            }).catch(() => {});
    },

    // ── Appliquer modèle de taxes ──────────────────────────
    _appliquer_modele_taxes(frm) {
        if (frm.doc.taxes_and_charges) return Promise.resolve();
        return frappe.call({
            method: "frappe.client.get_list",
            args  : {
                doctype: "Purchase Taxes and Charges Template",
                filters: { company: frm.doc.company },
                fields : ["name", "is_default"],
                limit_page_length: 5, order_by: "is_default desc, name asc",
            }
        }).then(r => {
            const templates = (r.message || r) || [];
            if (!templates.length) return;
            const tpl = templates[0].name;
            return frm.set_value("taxes_and_charges", tpl)
                .then(() => new Promise(r => frappe.after_ajax(r)))
                .then(() => frappe.call({
                    method: "erpnext.controllers.accounts_controller.get_taxes_and_charges",
                    args  : { master_doctype: "Purchase Taxes and Charges Template", master_name: tpl }
                })).then(tax_r => {
                    frm.doc.taxes = [];
                    for (const row of (tax_r.message || [])) frm.add_child("taxes", row);
                    frm.refresh_field("taxes");
                });
        }).catch(err => console.warn("OCR: modèle taxes non chargé", err));
    },
};

// ── Intégration formulaires Frappe ─────────────────────────────
frappe.ui.form.on("Purchase Invoice", { refresh(frm) { OCRForm.init(frm); } });
frappe.ui.form.on("Payment Entry",    { refresh(frm) { OCRForm.init(frm); } });
frappe.ui.form.on("OCR Document",     { refresh(frm) { OCRForm.init(frm); } });