import logging
from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

SHOP_CITY_GUESS = {
    'marina_1': 'Casablanca',
    'salam_1': 'Casablanca',
    'salam_2': 'Casablanca',
    'morocco_mall': 'Casablanca',
    'californie': 'Casablanca',
    'marina_s': 'Casablanca',
    'tachefine': 'Casablanca',
    'ain_sebaa': 'Casablanca',
    'citymall': 'Casablanca',
    'ibn_batouta': 'Casablanca',
    'twin_c': 'Casablanca',
    'agdal': 'Rabat',
    'r_center': 'Rabat',
    'mohamed_v': 'Rabat',
    'selapark_a': 'Salé',
    'selapark_t': 'Salé',
    'mohammadia': 'Mohammadia',
    'shop': '',
}


def migrate(cr, version):
    """Backfill the new 'city' field on existing mv.batch.shop.mapping rows
    (deduced from shop_field) — the create() override only handles new rows."""
    _logger.info("mavie_dashboard: backfilling city on mv.batch.shop.mapping")
    env = api.Environment(cr, SUPERUSER_ID, {})
    mappings = env['mv.batch.shop.mapping'].search([('city', 'in', [False, ''])])
    for mapping in mappings:
        city = SHOP_CITY_GUESS.get(mapping.shop_field)
        if city:
            mapping.city = city
