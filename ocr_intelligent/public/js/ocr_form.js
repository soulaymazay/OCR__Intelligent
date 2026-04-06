// ═══════════════════════════════════════════════════════════════
// ocr_form.js — Groupe Bayoudh Metal
// Emplacement : ocr_intelligent/public/js/ocr_form.js
// hooks.py   : app_include_js = ["/assets/ocr_intelligent/js/ocr_form.js"]
//
// Scénario complet :
//  1. Bouton "Téléverser document" ouvre un dialog
//  2. Upload → OCR → extraction → sauvegarde OCR Document
//  3. Boucle d'itération sur les candidats (côté serveur)
//  4. Si compatible → remplit les champs du formulaire
//  5. Si incompatible → affiche l'erreur avec le champ bloquant
// ═══════════════════════════════════════════════════════════════


// ── Mapping type_document → fieldnames Frappe ──────────────────
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


// ── Module principal ────────────────────────────────────────────
const OCRForm = {

    init(frm) {
        if (frm._ocr_init) return;
        frm._ocr_init = true;

        frm.add_custom_button(__("Téléverser document"), () => {
            OCRForm._dialog(frm);
        }, __("OCR"));
    },

    // ── Dialog d'upload ─────────────────────────────────────────
    _dialog(frm) {
        const d = new frappe.ui.Dialog({
            title: __("Analyser un document"),
            fields: [
                {
                    fieldtype: "HTML",
                    options: `
                    <div style="background:var(--color-background-info);
                                border-left:3px solid var(--color-text-info);
                                padding:10px 14px;border-radius:4px;
                                margin-bottom:12px;font-size:13px;
                                color:var(--color-text-primary);">
                        <b>Formats acceptés :</b> PDF, PNG, JPG, JPEG, TIFF, BMP
                    </div>`
                },
                {
                    fieldtype   : "Attach",
                    fieldname   : "fichier",
                    label       : __("Sélectionner le fichier"),
                    reqd        : 1,
                }
            ],
            primary_action_label: __("Analyser et remplir"),
            primary_action(vals) {
                if (!vals.fichier) {
                    frappe.show_alert({ message: __("Sélectionnez un fichier"), indicator: "red" }, 3);
                    return;
                }
                d.hide();
                OCRForm._lancer(frm, vals.fichier);
            }
        });
        d.show();
    },

    // ── Appel pipeline ──────────────────────────────────────────
    _lancer(frm, file_url) {
    frappe.show_progress(__("OCR en cours..."), 10, 100, __("Chargement..."));

    // Récupère le fichier via frappe.call
    frappe.call({
        method: "frappe.client.get_value",
        args: {
            doctype: "File",
            filters: { file_url: file_url },
            fieldname: ["name", "file_name"]
        },
        callback: function(r) {
            if (r.message && r.message.name) {
                const file_doc = r.message;
                
                // Télécharge le fichier via l'API
                fetch(file_url)
                    .then(res => {
                        if (!res.ok) throw new Error(`HTTP ${res.status}`);
                        return res.blob();
                    })
                    .then(blob => {
                        frappe.show_progress(__("OCR en cours..."), 40, 100, __("Extraction du texte..."));
                        const fd = new FormData();
                        fd.append("file", blob, file_doc.file_name || "document.pdf");
                        
                        return fetch("/api/method/ocr_intelligent.api.ocr_pipeline.pipeline_complet", {
                            method: "POST",
                            headers: {
                                "X-Frappe-CSRF-Token": frappe.csrf_token,
                                "Accept": "application/json"
                            },
                            body: fd
                        });
                    })
                    .then(response => response.json())
                    .then(data => {
                        frappe.hide_progress();
                        OCRForm._traiter(frm, data.message || data);
                    })
                    .catch(err => {
                        frappe.hide_progress();
                        console.error("OCR error:", err);
                        OCRForm._afficher_erreur(
                            __("Erreur réseau"),
                            __("Impossible de contacter le serveur OCR."),
                            null, null
                        );
                    });
            } else {
                frappe.hide_progress();
                OCRForm._afficher_erreur(
                    __("Fichier introuvable"),
                    __("Impossible de récupérer le fichier sélectionné."),
                    null, null
                );
            }
        },
        error: function(err) {
            frappe.hide_progress();
            console.error(err);
            OCRForm._afficher_erreur(
                __("Erreur"),
                __("Erreur lors de la récupération du fichier."),
                null, null
            );
        }
    });
},

    // ── Traitement du résultat ──────────────────────────────────
    _traiter(frm, result) {

        // ── CAS 1 : Erreur technique (OCR échoué, format, etc.) ──
        if (!result || !result.success) {
            OCRForm._afficher_erreur(
                __("Document non traitable"),
                result?.erreur || __("Erreur inconnue"),
                result?.champ_bloquant || null,
                result?.iterations || null,
                result?.conseil || null,
                result?.texte_extrait || null
            );
            return;
        }

        // ── CAS 2 : Succès → remplir le formulaire ───────────────
        const mapping        = OCR_MAPPING[result.type_document] || OCR_MAPPING["inconnu"];
        const champs         = result.champs_remplis || {};
        const lignes_remplis = [];
        let   nb_remplis     = 0;

        for (const [fieldname, valeur] of Object.entries(champs)) {
            if (frm.fields_dict[fieldname] !== undefined) {
                frm.set_value(fieldname, valeur);
                lignes_remplis.push(
                    `<li><b>${fieldname}</b> <span style="color:var(--color-text-secondary)">←</span> ${valeur}</li>`
                );
                nb_remplis++;
            }
        }

        if (nb_remplis > 0) frm.dirty();

        // ── Message de succès ─────────────────────────────────────
        const erreurs   = result.erreurs_validation || [];
        const pct       = Math.round((result.score_final || 0) * 100);
        const candidat  = result.candidat_choisi || "—";
        const n_cands   = result.nb_candidats || 1;

        let html = `
        <div style="font-size:13px;">
          <div style="margin-bottom:10px;padding:8px 12px;
                      background:var(--color-background-secondary);
                      border-radius:var(--border-radius-md);">
            <b>Type détecté :</b> ${result.type_document || "—"} &nbsp;|&nbsp;
            <b>Score OCR :</b> ${result.score_confiance}% &nbsp;|&nbsp;
            <b>Compatibilité :</b> ${pct}% &nbsp;|&nbsp;
            <b>Candidat :</b> ${candidat} (${n_cands} testé(s))
          </div>`;

        if (lignes_remplis.length > 0) {
            html += `
          <div style="background:var(--color-background-success);
                      padding:8px 12px;border-radius:var(--border-radius-md);
                      margin-bottom:8px;">
            <b style="color:var(--color-text-success);">
              Champs remplis automatiquement (${nb_remplis})
            </b>
            <ul style="margin:6px 0 0;padding-left:18px;">
              ${lignes_remplis.join("")}
            </ul>
          </div>`;
        }

        if (erreurs.length > 0) {
            const err_html = erreurs.map(e =>
                `<li><b>${e.champ}</b> — ${e.message}
                 <br><small style="color:var(--color-text-secondary)">→ ${e.action}</small></li>`
            ).join("");
            html += `
          <div style="background:var(--color-background-warning);
                      padding:8px 12px;border-radius:var(--border-radius-md);">
            <b style="color:var(--color-text-warning);">
              À saisir manuellement (${erreurs.length})
            </b>
            <ul style="margin:6px 0 0;padding-left:18px;">${err_html}</ul>
          </div>`;
        }

        // Journal d'itérations (collapsible)
        if (result.iterations && result.iterations.length > 0) {
            html += OCRForm._html_iterations(result.iterations);
        }

        html += "</div>";

        frappe.msgprint({
            title    : __("Document identifié"),
            message  : html,
            indicator: erreurs.length === 0 ? "green" : "orange"
        });
    },

    // ── Affichage erreur avec détails ──────────────────────────
    _afficher_erreur(titre, message, champ_bloquant, iterations, conseil, texte_debug) {

        let html = `
        <div style="font-size:13px;">
          <div style="background:var(--color-background-danger);
                      padding:10px 14px;border-radius:var(--border-radius-md);
                      margin-bottom:10px;color:var(--color-text-danger);">
            ${(message || "").replace(/\n/g, "<br>")}
          </div>`;

        if (champ_bloquant) {
            html += `
          <div style="padding:8px 12px;border-left:3px solid var(--color-text-danger);
                      margin-bottom:8px;">
            <b>Champ incompatible :</b>
            <code style="background:var(--color-background-secondary);
                         padding:2px 6px;border-radius:4px;">${champ_bloquant}</code>
            <br>La valeur extraite du document ne correspond pas au champ attendu.
          </div>`;
        }

        if (conseil) {
            html += `
          <div style="padding:8px 12px;background:var(--color-background-info);
                      border-radius:var(--border-radius-md);margin-bottom:8px;
                      color:var(--color-text-info);">
            <b>Conseil :</b> ${conseil}
          </div>`;
        }

        if (iterations && iterations.length > 0) {
            html += OCRForm._html_iterations(iterations);
        }

        if (texte_debug) {
            html += `
          <details style="margin-top:8px;">
            <summary style="cursor:pointer;font-size:12px;
                            color:var(--color-text-secondary);">
              Texte extrait (debug)
            </summary>
            <pre style="background:var(--color-background-secondary);
                        padding:8px;margin-top:4px;
                        white-space:pre-wrap;font-size:11px;
                        border-radius:var(--border-radius-md);">${texte_debug}</pre>
          </details>`;
        }

        html += "</div>";

        frappe.msgprint({ title: titre, message: html, indicator: "red" });
    },

    // ── HTML du journal d'itérations ────────────────────────────
    _html_iterations(iterations) {
        const lignes = iterations.map((it, i) => {
            const icone   = it.resultat === "validé"  ? "✓" :
                            it.resultat === "rejeté"  ? "✗" : "~";
            const couleur = it.resultat === "validé"  ? "var(--color-text-success)" :
                            it.resultat === "rejeté"  ? "var(--color-text-danger)"  :
                            "var(--color-text-warning)";
            const bloq    = it.champ_bloquant
                ? ` — bloqué sur <code style="font-size:11px;">${it.champ_bloquant}</code>`
                : "";
            const nb_ok   = (it.champs_testes || []).filter(c => c.compatible === true).length;
            const nb_tot  = (it.champs_testes || []).filter(c => c.compatible !== null).length;

            return `
            <tr>
              <td style="padding:4px 8px;color:${couleur};font-weight:500;">${icone}</td>
              <td style="padding:4px 8px;font-size:12px;">${it.ocr_doc_id}</td>
              <td style="padding:4px 8px;font-size:12px;">score OCR: ${it.score_ocr}%</td>
              <td style="padding:4px 8px;font-size:12px;">${nb_ok}/${nb_tot} champs${bloq}</td>
            </tr>`;
        }).join("");

        return `
        <details style="margin-top:10px;">
          <summary style="cursor:pointer;font-size:12px;
                          color:var(--color-text-secondary);">
            Journal de la boucle d'itération (${iterations.length} candidat(s) testés)
          </summary>
          <table style="width:100%;margin-top:6px;border-collapse:collapse;font-size:12px;">
            <thead>
              <tr style="border-bottom:1px solid var(--color-border-secondary);">
                <th style="padding:4px 8px;text-align:left;"></th>
                <th style="padding:4px 8px;text-align:left;">OCR Document</th>
                <th style="padding:4px 8px;text-align:left;">Score OCR</th>
                <th style="padding:4px 8px;text-align:left;">Résultat</th>
              </tr>
            </thead>
            <tbody>${lignes}</tbody>
          </table>
        </details>`;
    }
};


// ── Intégration dans les formulaires ────────────────────────────
frappe.ui.form.on("OCR Document", {
    refresh(frm) { OCRForm.init(frm); }
});

// Décommentez selon les modules installés dans votre instance :
// frappe.ui.form.on("Purchase Invoice",  { refresh(frm) { OCRForm.init(frm); } });
// frappe.ui.form.on("Sales Invoice",     { refresh(frm) { OCRForm.init(frm); } });
// frappe.ui.form.on("Purchase Order",    { refresh(frm) { OCRForm.init(frm); } });
// frappe.ui.form.on("Payment Entry",     { refresh(frm) { OCRForm.init(frm); } });
// frappe.ui.form.on("Delivery Note",     { refresh(frm) { OCRForm.init(frm); } });