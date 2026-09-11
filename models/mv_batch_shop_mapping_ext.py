# -*- coding: utf-8 -*-
import re

from odoo import api, fields, models

# Déduction automatique de la ville à partir du champ magasin (base pivot).
# CORRIGÉ le 2026-08-03 : la première version se basait sur le code interne
# (ex: "marina_1") en supposant que "Marina"/"Salam" = Casablanca, ce qui
# était faux — les entrepôts réels "MAGASIN MARINA AGADIR" / "MAGASIN SALAM1
# AGADIR" / "MAGASIN CARREFOUR AGADIR" / "MAGASIN CARREFOUR TEMARA" /
# "MAGASIN CITY MALL TANGER" / "MAGASIN IBN BATOUTA TANGER" sont dans
# d'autres villes que ce que le code suggérait. Reste éditable par
# l'utilisateur dans Base Pivot → Configuration → Mapping Magasins.
SHOP_CITY_GUESS = {
    'marina_1': 'Agadir',
    'salam_1': 'Agadir',
    'salam_2': 'Agadir',
    'selapark_a': 'Agadir',
    'marina_vetements_agadir': 'Agadir',
    'morocco_mall': 'Casablanca',
    'californie': 'Casablanca',
    'marina_s': 'Casablanca',
    'tachefine': 'Casablanca',
    'ain_sebaa': 'Casablanca',
    'twin_c': 'Casablanca',
    'citymall': 'Tanger',
    'ibn_batouta': 'Tanger',
    'agdal': 'Rabat',
    'r_center': 'Rabat',
    'selapark_t': 'Témara',
    'mohammadia': 'Mohammadia',
    'shop': '',
    # 'mohamed_v' et 'oranger' : ville non confirmée, laissés vides
    # volontairement plutôt que de re-deviner à l'aveugle.
}

# Mots-clés de ville reconnaissables dans le nom réel de l'entrepôt Odoo —
# utilisé en priorité sur SHOP_CITY_GUESS quand un warehouse_id est fourni,
# car le nom de l'entrepôt s'est avéré plus fiable que le code magasin.
CITY_KEYWORDS = [
    'Agadir', 'Casablanca', 'Rabat', 'Salé', 'Sale', 'Témara', 'Temara',
    'Mohammadia', 'Tanger', 'Marrakech', 'Fès', 'Fes',
]


def _guess_city_from_warehouse_name(name):
    if not name:
        return None
    for kw in CITY_KEYWORDS:
        if re.search(r'\b' + re.escape(kw) + r'\b', name, re.IGNORECASE):
            return 'Salé' if kw.lower() == 'sale' else ('Témara' if kw.lower() == 'temara' else kw)
    return None


# Villes "voisines" pour le tri de proximité lors des suggestions de
# transfert : même ville d'abord, puis ces villes voisines, puis le reste.
# Rabat / Salé / Témara forment une même agglomération contiguë.
CITY_PROXIMITY = {
    'Casablanca': ['Mohammadia'],
    'Mohammadia': ['Casablanca'],
    'Rabat': ['Salé', 'Témara'],
    'Salé': ['Rabat', 'Témara'],
    'Témara': ['Rabat', 'Salé'],
}


class MvBatchShopMappingExt(models.Model):
    _inherit = 'mv.batch.shop.mapping'

    shop_field = fields.Selection(
        selection_add=[
            ('marina_vetements_agadir', 'Marina Vêtements Agadir'),
            ('oranger', 'Oranger'),
        ],
        ondelete={'marina_vetements_agadir': 'cascade', 'oranger': 'cascade'},
    )

    city = fields.Char(
        string="Ville",
        default=lambda self: '',
        help="Ville du magasin. Déduite automatiquement du nom de l'entrepôt "
             "(ou à défaut du code magasin) à la création — modifiable si la "
             "déduction est incorrecte. Utilisée pour prioriser les "
             "suggestions de transfert inter-magasins (même ville d'abord, "
             "puis les environs).",
    )

    responsible_user_id = fields.Many2one(
        'res.users',
        string="Responsable magasin",
        help="Utilisateur notifié EN PLUS des responsables trouvés "
             "automatiquement (poste « Manager » + ce magasin dans ses POS "
             "autorisés) lors d'un transfert de stock impliquant ce magasin. "
             "Utile pour un magasin qui n'a aucun Manager dans Paramètres → "
             "Utilisateurs.",
    )

    def _get_store_managers(self):
        """Responsables à notifier pour ce magasin.

        DEMANDE UTILISATEUR : le responsable d'un magasin se lit dans
        Paramètres → Utilisateurs & Sociétés → Utilisateurs — c'est
        l'utilisateur dont l'employé a le poste « Manager » et dont les
        « POS autorisé(s) » (champ allowed_pos, module pos_restrict)
        contiennent un point de vente de ce magasin.

        Le magasin est reconnu par l'entrepôt du POS (picking_type_id.
        warehouse_id), pas par son nom : « MAGASIN AIN SEBAA » et
        « Online - AIN SEBAA » pointent le même entrepôt, c'est celui du
        mapping.

        Le poste est comparé exactement, espaces et casse ignorés : il est
        saisi « Manager » ou « Manager␣» selon les sociétés, et un
        « Assistant manager » ne doit pas recevoir la notification.

        responsible_user_id, s'il est renseigné, est ajouté au résultat.
        """
        self.ensure_one()
        users = self.responsible_user_id.sudo().filtered('active')
        Users = self.env['res.users'].sudo()
        # pos_restrict et hr ne sont pas des dépendances déclarées du module :
        # sans eux, on se contente du responsable saisi sur le mapping.
        if (not self.warehouse_id or 'allowed_pos' not in Users._fields
                or 'hr.employee' not in self.env):
            return users
        configs = self.env['pos.config'].sudo().search([
            ('picking_type_id.warehouse_id', '=', self.warehouse_id.id),
        ])
        if not configs:
            return users
        candidates = Users.search([
            ('allowed_pos', 'in', configs.ids),
            ('share', '=', False),
        ])
        if not candidates:
            return users
        employees = self.env['hr.employee'].sudo().search([
            ('user_id', 'in', candidates.ids),
        ])
        managers = employees.filtered(
            lambda e: (e.job_id.name or '').strip().casefold() == 'manager'
        ).mapped('user_id')
        return users | managers

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('city'):
                city = None
                if vals.get('warehouse_id'):
                    wh = self.env['stock.warehouse'].sudo().browse(vals['warehouse_id'])
                    city = _guess_city_from_warehouse_name(wh.name)
                if not city and vals.get('shop_field'):
                    city = SHOP_CITY_GUESS.get(vals['shop_field'])
                vals['city'] = city or ''
        return super().create(vals_list)
