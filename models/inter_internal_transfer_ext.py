# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class InterInternalTransferExt(models.Model):
    """Extension de inter.internal.transfer (module transfert_interne) côté
    mavie_dashboard : transfert_interne ne dépend pas de mv_base_pivot (décision
    utilisateur — pas de changement de dépendance de module juste pour ça), donc
    la résolution du magasin réel via mv.batch.shop.mapping ne peut pas se faire
    directement dans transfert_interne. mavie_dashboard dépend déjà des deux
    modules, donc c'est le bon endroit pour cette extension.
    """
    _inherit = 'inter.internal.transfer'

    # ── Relais vers l'Inventaire pour les transferts intra-société ──
    #
    # DEMANDE UTILISATEUR : le transfert se SAISIT dans le dashboard, puis
    # « il continue à une autre place » selon les sociétés :
    #
    #   • même société  → dès que les références sont saisies, notification
    #     au responsable + l'opération existe déjà dans Inventaire →
    #     Transferts → Interne, réservée. Le responsable n'a plus qu'à
    #     collecter la marchandise et valider là-bas. Aucune validation
    #     intermédiaire dans le module Transferts.
    #   • sociétés ≠    → exactement la même chose, mais le bon reste dans
    #     le module Transferts : c'est sa validation qui déclenche le circuit
    #     inter-sociétés (avoir MOD FOR LIFE + livraison + commande d'achat),
    #     qu'un simple picking d'inventaire ne sait pas faire.
    #
    # Tout est fait ici et non dans transfert_interne : seul mavie_dashboard
    # est modifiable (décision utilisateur), et il dépend déjà de
    # transfert_interne et de stock.
    state = fields.Selection(
        # ('done',) sans libellé = ancre de position : Odoo fusionne les deux
        # listes en respectant l'ordre de chacune, donc écrire « transmitted
        # puis done » place le nouvel état AVANT « Fait » dans la barre
        # d'état, comme dans le parcours réel. Sans cette ancre, il serait
        # simplement ajouté à la fin, après « Fait ».
        selection_add=[('transmitted', "Transmis à l'inventaire"), ('done',)],
        ondelete={'transmitted': 'set default'},
    )
    picking_id = fields.Many2one(
        'stock.picking',
        string="Opération d'inventaire",
        readonly=True,
        copy=False,
        help="Transfert interne créé dans l'Inventaire (Transferts → Interne) "
             "pour un transfert entre deux magasins d'une même société. C'est "
             "là que le responsable collecte la marchandise et valide.",
    )

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

    def action_submit(self):
        """Aiguille le bon vers l'endroit où il sera réellement collecté."""
        self.ensure_one()
        if self.company_source_id and self.company_source_id == self.company_target_id:
            if self.state != 'draft':
                raise UserError(_("Seul un bon en brouillon peut être soumis pour validation."))
            if not self.line_ids:
                raise UserError(_(
                    "Veuillez ajouter au moins une ligne de produit avant de soumettre."
                ))
            # Mêmes garde-fous que la validation appro : on va créer de vrais
            # mouvements de stock, autant échouer avant de les créer.
            if self.location_source_id.company_id != self.company_source_id:
                raise UserError(_("L'emplacement source n'appartient pas à la société source."))
            if self.location_target_id.company_id != self.company_target_id:
                raise UserError(_("L'emplacement cible n'appartient pas à la société cible."))
            self.picking_id = self._create_intra_company_picking()
            self.state = 'transmitted'
            return True
        return super().action_submit()

    def action_validate(self):
        """Un bon transmis à l'Inventaire ne se valide plus ici."""
        self.ensure_one()
        if self.state == 'transmitted':
            raise UserError(_(
                "Ce transfert a été transmis à l'Inventaire : il se collecte et se "
                "valide dans Inventaire → Transferts → Interne, sur l'opération %s."
            ) % (self.picking_id.name or '—'))
        return super().action_validate()

    def _create_intra_company_picking(self):
        """Crée l'opération d'inventaire interne (même société, deux
        emplacements) : confirmée et réservée, mais PAS validée.

        C'est le bon que le responsable retrouve dans Inventaire →
        Transferts → Interne. Sa validation repasse ce transfert à « Fait »
        (voir StockPickingTransferExt._action_done).

        NB : la création du picking reprend celle de
        transfert_interne._create_intra_company_transfer, dont le rôle est
        différent (elle crée ET valide en une fois, pour les bons soumis
        avant cette évolution). Elle est réécrite ici plutôt que factorisée
        là-bas parce que transfert_interne n'est pas modifiable.
        """
        self.ensure_one()
        # Type d'opération interne de l'entrepôt SOURCE de préférence :
        # _get_picking_type prend le premier type interne de la société, ce
        # qui rattacherait le bon à un autre entrepôt de la même société.
        warehouse = self.env['stock.warehouse'].sudo().search([
            ('company_id', '=', self.company_source_id.id),
            ('view_location_id', 'parent_of', self.location_source_id.id),
        ], limit=1)
        picking_type = self.env['stock.picking.type'].sudo().search([
            ('code', '=', 'internal'),
            ('warehouse_id', '=', warehouse.id),
        ], limit=1) if warehouse else False
        if not picking_type:
            picking_type = self._get_picking_type(self.company_source_id, 'internal')

        picking = self.env['stock.picking'].sudo().with_company(self.company_source_id).create({
            'picking_type_id': picking_type.id,
            'location_id': self.location_source_id.id,
            'location_dest_id': self.location_target_id.id,
            'company_id': self.company_source_id.id,
            # Le responsable doit reconnaître le bon dans la liste des
            # transferts internes : on y met la référence du transfert telle
            # qu'elle apparaît sur le dashboard et sur le PDF imprimé.
            'origin': self.name,
            'scheduled_date': self.scheduled_date or fields.Datetime.now(),
            # Lien retour, exploité à la validation du picking pour clore le
            # bon de transfert (voir stock_picking_ext.py).
            'inter_transfer_id': self.id,
        })

        for line in self.line_ids:
            self.env['stock.move'].sudo().with_company(self.company_source_id).create({
                'name': line.product_id.display_name,
                'product_id': line.product_id.id,
                'product_uom_qty': line.quantity,
                'product_uom': line.product_id.uom_id.id,
                'location_id': self.location_source_id.id,
                'location_dest_id': self.location_target_id.id,
                'picking_id': picking.id,
                'company_id': self.company_source_id.id,
            })

        picking.action_confirm()
        # Réserve la marchandise : elle n'est plus proposée comme disponible
        # pour un autre transfert tant que celui-ci n'est pas collecté.
        picking.action_assign()
        return picking

    def open_picking(self):
        """Ouvre l'opération d'inventaire à collecter puis valider."""
        self.ensure_one()
        if self.picking_id:
            return {
                'type': 'ir.actions.act_window',
                'name': "Opération d'inventaire",
                'res_model': 'stock.picking',
                'res_id': self.picking_id.id,
                'view_mode': 'form',
                'target': 'current',
            }

