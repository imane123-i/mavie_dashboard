import logging
from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

# Correction du 2026-08-03 : la ville déduite en 17.0.1.0.6 se basait sur le
# code magasin interne ("marina_1" -> supposé Casablanca) et était fausse
# pour plusieurs entrepôts réels (Marina/Salam/Carrefour Agadir, Carrefour
# Temara, City Mall/Ibn Batouta Tanger). On corrige avec la ville réelle,
# et on ajoute les 2 magasins qui n'avaient encore aucune ligne de mapping.
CITY_FIX_BY_SHOP_FIELD = {
    'marina_1': 'Agadir',
    'salam_1': 'Agadir',
    'salam_2': 'Agadir',
    'selapark_a': 'Agadir',
    'citymall': 'Tanger',
    'ibn_batouta': 'Tanger',
    'selapark_t': 'Témara',
    # 'mohamed_v' : ville non fiable (devinée à tort), on vide plutôt que de
    # remplacer une erreur par une autre — à confirmer manuellement.
    'mohamed_v': None,
}

# (shop_field, nom exact de l'entrepôt Odoo, ville)
MISSING_MAPPINGS = [
    ('twin_c', 'MAGASIN TWIN CENTER', 'Casablanca'),
    ('marina_vetements_agadir', 'MAGASIN MARINA VETEMENTS AGADIR', 'Agadir'),
    ('oranger', 'MAGASIN ORANGER', None),  # ville non confirmée
]


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Mapping = env['mv.batch.shop.mapping']

    _logger.info("mavie_dashboard: correction des villes mv.batch.shop.mapping")
    for shop_field, city in CITY_FIX_BY_SHOP_FIELD.items():
        mapping = Mapping.search([('shop_field', '=', shop_field)], limit=1)
        if mapping:
            mapping.city = city or ''

    _logger.info("mavie_dashboard: création des mappings magasin manquants")
    Warehouse = env['stock.warehouse']
    for shop_field, wh_name, city in MISSING_MAPPINGS:
        if Mapping.search([('shop_field', '=', shop_field)], limit=1):
            continue
        warehouse = Warehouse.search([('name', '=', wh_name)], limit=1)
        if not warehouse:
            _logger.warning(
                "mavie_dashboard: entrepôt '%s' introuvable, mapping '%s' non créé",
                wh_name, shop_field
            )
            continue
        Mapping.create({
            'shop_field': shop_field,
            'company_id': warehouse.company_id.id,
            'warehouse_id': warehouse.id,
            'city': city or '',
            'active': True,
        })
