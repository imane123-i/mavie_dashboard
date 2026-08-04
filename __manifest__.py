{
    'name': 'MaVie Dashboard — Centre d\'Analyse',
    'version': '17.0.1.0.7',
    'category': 'Sales',
    'summary': 'Dashboard analytique connecté aux ventes POS, stock et fournisseurs',
    'description': """
        Dashboard de gestion pour MaVie que j'adore.
        - Tableau de bord ventes (CA, tickets, top/flop produits, ABC)
        - Suivi stock & ruptures
        - Commandes fournisseurs
        - Estimation et risques
    """,
    'author': 'MaVie',
    'depends': [
        'web',
        'point_of_sale',
        'stock',
        'purchase',
        'product_collection_arrivage',
        'spreadsheet_dashboard',
        'mv_base_pivot',      # mv.article.base, mv.article.batch, mv.batch.shop.mapping
        'transfert_interne',  # inter.internal.transfer (extraction / transfert magasin à magasin)
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/dashboard_menu.xml',
        'views/dashboard_templates.xml',
        'views/mv_batch_shop_mapping_views_ext.xml',
        'report/mavie_transfer_report.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'mavie_dashboard/static/src/js/dashboard_action.js',
            'mavie_dashboard/static/src/xml/dashboard_action.xml',
        ],
    },
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
    'icon': '/mavie_dashboard/static/description/icon.png',
    'post_init_hook': 'post_init_hook',
}