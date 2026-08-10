import logging

_logger = logging.getLogger(__name__)

# pos_order_line n'avait aucun index sur product_id : chaque requête du
# dashboard (KPIs, fiche produit, top/flop) qui filtre ou regroupe par
# produit forçait un scan complet de la table (400k+ lignes) avec jointure
# product_product/product_template. Le cas le plus pénalisé est la fiche
# produit (un seul product_tmpl_id, donc requête très sélective) qui aurait
# dû être quasi instantanée mais scannait toute la table faute d'index.
def migrate(cr, version):
    _logger.info("mavie_dashboard: création de l'index pos_order_line(product_id)")
    cr.execute(
        "CREATE INDEX IF NOT EXISTS pos_order_line__product_id_index "
        "ON pos_order_line (product_id)"
    )
