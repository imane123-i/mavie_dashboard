# -*- coding: utf-8 -*-
from odoo import fields, models


class StockPickingTransferExt(models.Model):
    """Lien retour entre l'opération d'inventaire et le bon de transfert.

    Un transfert entre deux magasins d'une MÊME société est transmis à
    l'Inventaire dès la saisie dans le dashboard (voir
    InterInternalTransferExt.action_submit) : l'opération interne y est
    créée, réservée, et c'est le responsable du magasin source qui la
    collecte puis la valide. Le bon de transfert doit suivre cet état sans
    que personne ait à le rouvrir — d'où la répercussion ci-dessous, qui est
    aussi ce qui fait apparaître « Fait » dans l'historique du dashboard.
    """
    _inherit = 'stock.picking'

    inter_transfer_id = fields.Many2one(
        'inter.internal.transfer',
        string='Bon de transfert',
        readonly=True,
        index=True,
        copy=False,
        help="Bon de transfert interne à l'origine de cette opération.",
    )

    def _action_done(self):
        res = super()._action_done()
        # sudo() : le responsable de magasin valide le picking dans sa
        # société ; il n'a pas forcément le droit d'écrire sur le bon de
        # transfert, qui reste piloté par le module Transferts.
        transfers = self.mapped('inter_transfer_id').sudo().filtered(
            lambda t: t.state == 'transmitted'
        )
        if transfers:
            transfers.write({'state': 'done'})
        return res

    def action_cancel(self):
        res = super().action_cancel()
        # Opération annulée dans l'Inventaire : le bon redevient un
        # brouillon re-soumissible (une nouvelle opération sera créée).
        # Le laisser « transmis » le figerait sur un picking annulé, sans
        # aucun moyen de le relancer.
        transfers = self.mapped('inter_transfer_id').sudo().filtered(
            lambda t: t.state == 'transmitted'
        )
        if transfers:
            transfers.write({'state': 'draft', 'picking_id': False})
        return res
