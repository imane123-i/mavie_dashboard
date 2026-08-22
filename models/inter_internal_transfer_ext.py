# -*- coding: utf-8 -*-
from odoo import api, fields, models


class InterInternalTransferExt(models.Model):
    """Extension de inter.internal.transfer (module transfert_interne) côté
    mavie_dashboard : transfert_interne ne dépend pas de mv_base_pivot (décision
    utilisateur — pas de changement de dépendance de module juste pour ça), donc
    la résolution du magasin réel via mv.batch.shop.mapping ne peut pas se faire
    directement dans transfert_interne. mavie_dashboard dépend déjà des deux
    modules, donc c'est le bon endroit pour cette extension.
    """
    _inherit = 'inter.internal.transfer'

    emetteur_display = fields.Char(
        string='Émetteur',
        compute='_compute_emetteur_recepteur_display',
        store=True,
        help="Magasin réel d'où part le transfert, résolu via "
             "mv.batch.shop.mapping (une société peut posséder plusieurs "
             "magasins — ex: SALMEDO en a 7 — donc la société seule ne "
             "suffit pas à identifier le bon magasin).",
    )
    recepteur_display = fields.Char(
        string='Récepteur',
        compute='_compute_emetteur_recepteur_display',
        store=True,
        help="Magasin réel qui reçoit le transfert, résolu via "
             "mv.batch.shop.mapping.",
    )

    # ── Regroupement à l'affichage "un seul bon par référence" ──
    # Pas de changement du modèle de données (pas de lignes source multiples
    # sur un même enregistrement) : group_ref est juste un repère commun posé
    # côté client (dashboard.js) sur tous les inter.internal.transfer créés
    # dans la même session de transfert pour la même référence+destination,
    # utilisé ensuite pour les regrouper à l'affichage (liste + PDF).
    group_ref = fields.Char(string='Groupe de transfert', index=True, copy=False)

    # DÉCISION UTILISATEUR : l'historique du dashboard doit repartir de zéro
    # et ne lister QUE les transferts lancés depuis le dashboard. Les 590
    # bons déjà en base ont été créés autrement (formulaire Odoo, reprise de
    # données) et ne doivent pas y apparaître.
    #
    # Un marqueur explicite plutôt qu'un filtre sur la date de création :
    # une date de bascule masquerait les anciens bons, mais ferait aussi
    # remonter dans l'historique tout bon créé plus tard depuis le
    # formulaire Odoo — ce qui n'est pas « un transfert fait depuis le
    # dashboard ». Le champ vaut False sur tout l'existant, l'historique
    # démarre donc vide.
    created_from_dashboard = fields.Boolean(
        string='Créé depuis le dashboard',
        default=False,
        index=True,
        copy=False,
        help="Coché automatiquement quand le bon est créé par le bouton "
             "« Proposer un transfert » du dashboard MaVie. Seuls ces bons "
             "apparaissent dans la section Historique du dashboard.",
    )

    group_size = fields.Integer(
        string='Nb bons du groupe',
        compute='_compute_group_info',
        help="Nombre de bons de transfert partageant le même groupe "
             "(même référence + même destination, créés dans la même "
             "session) — > 1 quand plusieurs magasins source ont été "
             "nécessaires pour couvrir le besoin.",
    )
    group_member_names = fields.Char(
        string='Autres bons du groupe',
        compute='_compute_group_info',
    )

    @api.depends(
        'location_source_id', 'company_source_id',
        'location_target_id', 'company_target_id',
    )
    def _compute_emetteur_recepteur_display(self):
        Mapping = self.env['mv.batch.shop.mapping'].sudo()
        for rec in self:
            src_mapping = (
                Mapping.search([('warehouse_id.lot_stock_id', '=', rec.location_source_id.id)], limit=1)
                or Mapping.search([('company_id', '=', rec.company_source_id.id)], limit=1)
            )
            dst_mapping = (
                Mapping.search([('warehouse_id.lot_stock_id', '=', rec.location_target_id.id)], limit=1)
                or Mapping.search([('company_id', '=', rec.company_target_id.id)], limit=1)
            )
            rec.emetteur_display = (
                (src_mapping.warehouse_id.name or src_mapping.shop_label or rec.company_source_id.name)
                if src_mapping else (rec.company_source_id.name or '—')
            )
            rec.recepteur_display = (
                (dst_mapping.warehouse_id.name or dst_mapping.shop_label or rec.company_target_id.name)
                if dst_mapping else (rec.company_target_id.name or '—')
            )

    @api.depends('group_ref')
    def _compute_group_info(self):
        # NB: @api.depends('group_ref') ne déclenche que sur le group_ref de
        # CET enregistrement — Odoo ne peut pas nativement observer "d'autres
        # enregistrements qui viennent de prendre la même valeur". Le search()
        # ci-dessous est donc toujours ré-évalué à la lecture si non stocké ;
        # champ non stocké intentionnellement pour rester à jour (voir store
        # non précisé = False par défaut).
        for rec in self:
            if not rec.group_ref:
                rec.group_size = 1
                rec.group_member_names = ''
                continue
            siblings = self.search([('group_ref', '=', rec.group_ref)])
            rec.group_size = len(siblings)
            others = siblings - rec
            rec.group_member_names = ', '.join(others.mapped('name')) if others else ''
