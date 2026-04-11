// Copyright (c) 2026, OCR automatique pour les documents financiers and contributors
// For license information, please see license.txt

frappe.ui.form.on('OCR Document', {

    refresh: function (frm) {
        // Bouton principal : lancer l'analyse OCR
        frm.add_custom_button(__('📄 Analyser le document'), function () {
            lancer_ocr(frm);
        }, __('OCR'));

        // Si des champs extraits existent déjà, afficher le bouton de saisie manuelle
        if (frm.doc.extracted_field) {
            frm.add_custom_button(__('✏️ Compléter les champs'), function () {
                afficher_formulaire_saisie(frm);
            }, __('OCR'));
        }
    }
});


// ─────────────────────────────────────────────────────────────────────
// 1. LANCER L'OCR
// ─────────────────────────────────────────────────────────────────────

function lancer_ocr(frm) {
    if (!frm.doc.file_url) {
        frappe.msgprint({
            title: __('Fichier manquant'),
            message: __('Veuillez d\'abord attacher un fichier via le champ "URL fichier".'),
            indicator: 'orange'
        });
        return;
    }

    frappe.show_progress(__('Analyse OCR en cours...'), 30, 100);

    // Récupérer le fichier depuis l'URL attachée
    fetch(frm.doc.file_url)
        .then(r => r.blob())
        .then(blob => {
            const ext        = frm.doc.file_url.split('.').pop().toLowerCase();
            const nom        = frm.doc.document_name || ('document.' + ext);
            const formData   = new FormData();
            formData.append('file', blob, nom);

            frappe.show_progress(__('Analyse OCR en cours...'), 60, 100);

            return fetch('/api/method/ocr_intelligent.api.ocr_pipeline.pipeline_complet', {
                method:  'POST',
                headers: { 'X-Frappe-CSRF-Token': frappe.csrf_token },
                body:    formData
            });
        })
        .then(r => r.json())
        .then(data => {
            frappe.hide_progress();
            const res = data.message || data;

            if (!res.success) {
                frappe.msgprint({
                    title:     __('Document non traitable'),
                    message:   res.erreur || __('Erreur inconnue'),
                    indicator: 'red'
                });
                return;
            }

            // Sauvegarder les résultats dans le doc
            frm.set_value('confidence_score', res.score_confiance || 0);
            frm.set_value('status', res.champs_vides && res.champs_vides.length > 0
                ? 'Validation requise' : 'Validé');

            // Stocker les champs extraits en JSON
            frm.set_value('extracted_field',
                JSON.stringify(res.champs_remplis || {}, null, 2));

            frm.save().then(() => {
                afficher_popup_resultat(frm, res);
            });
        })
        .catch(err => {
            frappe.hide_progress();
            frappe.msgprint({
                title:     __('Erreur'),
                message:   err.message || String(err),
                indicator: 'red'
            });
        });
}


// ─────────────────────────────────────────────────────────────────────
// 2. POPUP RÉSULTAT OCR
// ─────────────────────────────────────────────────────────────────────

function afficher_popup_resultat(frm, res) {
    const champs_remplis  = res.champs_remplis  || {};
    const champs_vides    = res.champs_vides    || [];
    const nb_remplis      = Object.keys(champs_remplis).length;
    const nb_vides        = champs_vides.length;

    // ── Lignes champs remplis ──
    let html_remplis = '';
    if (nb_remplis > 0) {
        html_remplis = `
        <div class="ocr-section-title" style="color:#2e7d32;font-weight:600;margin:16px 0 8px;">
            ✅ Champs remplis automatiquement
        </div>
        <table class="ocr-table" style="width:100%;border-collapse:collapse;">`;
        for (const [champ, valeur] of Object.entries(champs_remplis)) {
            html_remplis += `
            <tr style="border-bottom:1px solid #e8f5e9;">
                <td style="padding:7px 10px;color:#1b5e20;font-weight:500;width:45%;">
                    ${__(champ.replace(/_/g, ' '))}
                </td>
                <td style="padding:7px 10px;color:#333;">${valeur}</td>
            </tr>`;
        }
        html_remplis += '</table>';
    }

    // ── Lignes champs vides ──
    let html_vides = '';
    if (nb_vides > 0) {
        html_vides = `
        <div class="ocr-section-title" style="color:#e65100;font-weight:600;margin:16px 0 8px;">
            ✏️ Champs à compléter manuellement
        </div>
        <table class="ocr-table" style="width:100%;border-collapse:collapse;">`;
        for (const champ of champs_vides) {
            html_vides += `
            <tr style="border-bottom:1px solid #fff3e0;">
                <td style="padding:7px 10px;color:#bf360c;font-weight:500;width:45%;">
                    ${__(champ.replace(/_/g, ' '))}
                </td>
                <td style="padding:7px 10px;">
                    <input
                        type="text"
                        class="ocr-input-manuel"
                        data-champ="${champ}"
                        placeholder="Saisir manuellement..."
                        style="width:100%;padding:5px 8px;border:1px solid #ffccbc;
                               border-radius:4px;font-size:13px;outline:none;"
                    />
                </td>
            </tr>`;
        }
        html_vides += '</table>';
    }

    const d = new frappe.ui.Dialog({
        title: __('Résultat de l\'analyse OCR'),
        size:  'large',
        fields: [{
            fieldtype: 'HTML',
            fieldname: 'contenu_ocr',
            options: `
            <div style="padding:0 4px;">

                <!-- Statistiques -->
                <div style="display:flex;gap:12px;margin-bottom:16px;flex-wrap:wrap;">
                    <div style="flex:1;min-width:110px;background:#e3f2fd;border-radius:8px;
                                padding:12px;text-align:center;">
                        <div style="font-size:11px;color:#666;">Type détecté</div>
                        <div style="font-weight:700;color:#1565c0;font-size:15px;">
                            ${res.type_document || '—'}
                        </div>
                    </div>
                    <div style="flex:1;min-width:110px;background:#f3e5f5;border-radius:8px;
                                padding:12px;text-align:center;">
                        <div style="font-size:11px;color:#666;">Score OCR</div>
                        <div style="font-weight:700;color:#6a1b9a;font-size:15px;">
                            ${res.score_confiance || 0}%
                        </div>
                    </div>
                    <div style="flex:1;min-width:110px;background:#e8f5e9;border-radius:8px;
                                padding:12px;text-align:center;">
                        <div style="font-size:11px;color:#666;">Champs remplis</div>
                        <div style="font-weight:700;color:#2e7d32;font-size:15px;">${nb_remplis}</div>
                    </div>
                    <div style="flex:1;min-width:110px;background:#fff3e0;border-radius:8px;
                                padding:12px;text-align:center;">
                        <div style="font-size:11px;color:#666;">À compléter</div>
                        <div style="font-weight:700;color:#e65100;font-size:15px;">${nb_vides}</div>
                    </div>
                </div>

                ${html_remplis}
                ${html_vides}

                <div style="margin-top:16px;padding:10px;background:#f5f5f5;
                            border-radius:6px;font-size:12px;color:#666;text-align:center;">
                    ${res.message || ''}
                </div>
            </div>`
        }],
        primary_action_label: __('💾 Enregistrer le document'),
        primary_action: function () {
            enregistrer_saisie_manuelle(frm, d, champs_remplis);
        },
        secondary_action_label: __('Continuer la saisie'),
        secondary_action: function () {
            d.hide();
        }
    });

    d.show();
}


// ─────────────────────────────────────────────────────────────────────
// 3. ENREGISTRER LA SAISIE MANUELLE
// ─────────────────────────────────────────────────────────────────────

function enregistrer_saisie_manuelle(frm, dialog, champs_remplis) {
    // Récupérer les valeurs saisies manuellement
    const inputs = dialog.$wrapper.find('.ocr-input-manuel');
    const champs_manuels = {};

    inputs.each(function () {
        const champ  = $(this).data('champ');
        const valeur = $(this).val().trim();
        if (valeur) {
            champs_manuels[champ] = valeur;
        }
    });

    // Fusionner champs OCR + champs manuels
    const tous_champs = Object.assign({}, champs_remplis, champs_manuels);
    const nb_manuels  = Object.keys(champs_manuels).length;

    // Mettre à jour extracted_field avec la fusion complète
    frm.set_value('extracted_field', JSON.stringify(tous_champs, null, 2));

    // Mettre à jour le statut
    const champs_encore_vides = dialog.$wrapper
        .find('.ocr-input-manuel')
        .toArray()
        .filter(el => !$(el).val().trim());

    frm.set_value('status', champs_encore_vides.length === 0 ? 'Validé' : 'Validation requise');

    frm.save().then(() => {
        dialog.hide();
        frappe.show_alert({
            message: nb_manuels > 0
                ? __(`Document enregistré — ${nb_manuels} champ(s) ajouté(s) manuellement`)
                : __('Document enregistré'),
            indicator: 'green'
        });
    });
}


// ─────────────────────────────────────────────────────────────────────
// 4. BOUTON "COMPLÉTER LES CHAMPS" — depuis un doc existant
// ─────────────────────────────────────────────────────────────────────

function afficher_formulaire_saisie(frm) {
    let champs_existants = {};
    try {
        champs_existants = JSON.parse(frm.doc.extracted_field || '{}');
    } catch (e) {
        champs_existants = {};
    }

    // Tous les champs connus selon le type de document
    const tous_champs_possibles = [
        'numero_facture', 'date', 'echeance', 'fournisseur', 'client',
        'reference_commande', 'montant_ht', 'tva', 'montant_ttc',
        'mode_paiement', 'rib_iban', 'numero_commande', 'numero_devis',
        'numero_bl', 'validite', 'delai_livraison', 'quantite', 'prix_unitaire'
    ];

    const champs_vides = tous_champs_possibles.filter(
        c => !champs_existants[c]
    );

    // Construire le faux résultat pour réutiliser afficher_popup_resultat
    const res_fake = {
        type_document:   '—',
        score_confiance: frm.doc.confidence_score || 0,
        champs_remplis:  champs_existants,
        champs_vides:    champs_vides,
        message:         `${Object.keys(champs_existants).length} champ(s) déjà rempli(s), ` +
                         `${champs_vides.length} champ(s) à compléter`
    };

    afficher_popup_resultat(frm, res_fake);
}