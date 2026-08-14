# -*- coding: utf-8 -*-
from odoo import http, fields
from odoo.http import request, Response
from odoo.exceptions import UserError
import json
import re
import base64
import os
import csv
import io
from datetime import datetime, timedelta, date
import logging

from ..models.mv_batch_shop_mapping_ext import CITY_PROXIMITY

_logger = logging.getLogger(__name__)

# Sociétés qui ne sont PAS des magasins de vente au détail (société grossiste
# d'import "MOD FOR LIFE" utilisée pour les ventes inter-sociétés, et "PAIE"
# = paie/RH). Leur stock ne doit jamais compter dans les KPIs "stock retail"
# (total, valorisation, stock dormant, alertes de rupture) sous peine de
# chiffres totalement faussés — ex: stock dormant à 267% causé par un produit
# dont l'essentiel du stock dormait dans l'entrepôt grossiste MOD FOR LIFE.
NON_RETAIL_COMPANIES = ['MOD FOR LIFE', 'PAIE']

_STATIC_JS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), 'static', 'src', 'js', 'dashboard.js'
)


def resolve_variant_color_size(product_variant):
    """Retourne (color_name, size_name) pour une variante de produit.

    Essaie d'abord une correspondance EXACTE sur le nom d'attribut
    ("COULEURS" / "POINTURES" / "TAILLES"), le nommage fiable déjà utilisé
    par mv_base_pivot pour créer les variantes (mv_article_base.py,
    _get_allowed_attribute). Si un attribut ne matche pas exactement (anciennes
    données / nommage différent), on retombe sur une correspondance par
    sous-chaîne comme avant.

    NB: cette fonction est intentionnellement dupliquée dans
    transfert_interne/models/transfert_interne.py (InterInternalTransferLine.
    _compute_variant_info) — transfert_interne ne dépend pas de mv_base_pivot
    et on ne veut pas ajouter cette dépendance de module pour ça (décision
    utilisateur). Toute correction ici doit être répercutée là-bas.
    """
    color_name, size_name = None, None
    for attr_val in product_variant.product_template_attribute_value_ids:
        attr_exact = (attr_val.attribute_id.name or '').strip().upper()
        if attr_exact == 'COULEURS':
            color_name = attr_val.name.strip()
        elif attr_exact in ('POINTURES', 'TAILLES'):
            size_name = attr_val.name.strip()
    if color_name is None or size_name is None:
        for attr_val in product_variant.product_template_attribute_value_ids:
            attr_upper = (attr_val.attribute_id.name or '').upper()
            if color_name is None and any(k in attr_upper for k in ('COULEUR', 'COLOR', 'COL')):
                color_name = attr_val.name.strip()
            elif size_name is None and any(k in attr_upper for k in ('TAILLE', 'POINTURE', 'SIZE')):
                size_name = attr_val.name.strip()
    return color_name, size_name


class MaVieDashboardController(http.Controller):
    """Dashboard analytique MaVie - données depuis tout le catalogue Odoo (product.template)"""

    @http.route('/mavie/dashboard', type='http', auth='user', methods=['GET'])
    def dashboard_page(self, **kwargs):
        try:
            # Cache-busting automatique basé sur la date de modification réelle
            # du fichier — évite qu'un navigateur continue de servir une
            # version JS en cache après une mise à jour du module (bug vécu :
            # un ancien "?v=" figé en dur faisait que les mises à jour du
            # dashboard n'apparaissaient jamais tant que le cache n'était pas
            # vidé manuellement).
            try:
                js_version = str(int(os.path.getmtime(_STATIC_JS_PATH)))
            except OSError:
                js_version = '0'
            return request.render('mavie_dashboard.dashboard_page', {'js_version': js_version})
        except Exception as e:
            _logger.error(f"Erreur dashboard_page: {str(e)}")
            return f"<h1>Erreur</h1><p>{str(e)}</p>"

    def _get_non_retail_company_ids(self):
        """
        Résout NON_RETAIL_COMPANIES en IDs une seule fois (recherche triviale,
        5 lignes dans res.company) pour pouvoir filtrer stock.quant par
        ('company_id', 'not in', [...ids]) — un simple NOT IN sur une colonne
        entière indexée — plutôt que ('company_id.name', 'not in', [...]),
        qui force Postgres à faire une sous-requête/jointure sur res_company
        à CHAQUE ligne de stock.quant (185 000+ lignes) et ralentissait
        nettement le chargement du dashboard.
        """
        companies = request.env['res.company'].sudo().search([('name', 'in', NON_RETAIL_COMPANIES)])
        return companies.ids

    def _get_mod_for_life_partner_id(self):
        """Résout le partner_id de la société MOD FOR LIFE — c'est le
        fournisseur sur les bons de commande des 3 sociétés magasins quand
        elles se réapprovisionnent auprès d'elle plutôt qu'un vrai
        fournisseur externe. Permet de distinguer "achat externe" d'"achat
        interne" sur CA Achat/Qté Achetée."""
        company = request.env['res.company'].sudo().search([('name', '=', 'MOD FOR LIFE')], limit=1)
        return company.partner_id.id if company and company.partner_id else None

    def _get_context_company_ids(self):
        """
        Sociétés actuellement cochées dans le sélecteur multi-société standard
        d'Odoo (menu en haut à droite : SALMEDO / BLACK AND GOLD / ...).
        Le dashboard tourne dans un iframe (voir dashboard_action.js), il n'a
        donc pas accès au contexte JS du client web (allowed_company_ids) —
        mais le client web pose ce choix dans un cookie "cids" (voir
        odoo/addons/web/static/src/webclient/company_service.js), partagé
        avec l'iframe car même origine. On le lit ici pour que le dashboard
        réagisse à ce sélecteur standard sans dupliquer un choix de société
        dans sa propre UI.
        """
        raw = request.httprequest.cookies.get('cids')
        if not raw:
            return []
        ids = []
        for part in raw.split(','):
            try:
                ids.append(int(part))
            except (ValueError, TypeError):
                continue
        return ids

    def _get_sachet_collection_ids(self):
        """IDs de la/des collection(s) "Sachet" à exclure de tous les KPIs.

        Résolution relationnelle (collection_id), pas un filtre texte sur le
        nom du produit : un filtre du type ('name', 'not ilike', '2026')
        excluait à tort tout produit dont la référence contient simplement
        les chiffres "2026" (ex: "JEANS2026"), sans rapport avec cette
        collection. Voir CORRECTION #2c plus bas, qui utilisait déjà ce
        pattern pour references_count — centralisé ici pour être appliqué
        partout où le même filtre texte fragile était dupliqué.
        """
        return request.env['product.collection'].sudo().search([
            '|', ('name', 'ilike', 'sachet'), ('name', 'ilike', 'sacher'),
        ]).ids

    def _sachet_exclude_domain(self, path='collection_id'):
        """Domaine d'exclusion de la collection Sachet, via un chemin relationnel
        (ex: 'collection_id' sur product.template, ou
        'product_id.product_tmpl_id.collection_id' sur pos.order.line /
        purchase.order.line / stock.quant)."""
        ids = self._get_sachet_collection_ids()
        return [(path, 'not in', ids)] if ids else []

    # ─────────────────────────────────────────────────────────────
    # DOMAINS
    # ─────────────────────────────────────────────────────────────

    def _build_product_domain(self, kw):
        domain = []

        if kw.get('collection_id') or kw.get('batch_id'):
            # Filtre directement sur product.template.collection_id/arrivage_id
            # (module natif product_collection_arrivage). Un détour par
            # mv.article.base (Base Pivot) existait ici mais n'apportait
            # jamais de référence supplémentaire — vérifié en base sur
            # toutes les collections/arrivages réels : Base Pivot est
            # systématiquement un sous-ensemble strict de ce que ces champs
            # natifs trouvent déjà (Base Pivot ne couvre qu'une poignée de
            # références sur ~5000).
            pt_domain = []
            if kw.get('collection_id'):
                try:
                    pt_domain.append(('collection_id', '=', int(kw['collection_id'])))
                except (ValueError, TypeError):
                    pass
            if kw.get('batch_id'):
                try:
                    pt_domain.append(('arrivage_id', '=', int(kw['batch_id'])))
                except (ValueError, TypeError):
                    pass

            direct_tmpl_ids = []
            if pt_domain:
                direct_tmpl = request.env['product.template'].sudo().search(pt_domain)
                direct_tmpl_ids = direct_tmpl.ids

            if direct_tmpl_ids:
                domain.append(('id', 'in', direct_tmpl_ids))
            else:
                domain.append(('id', '=', -1))

        if kw.get('categ_id'):
            try:
                categ_id = int(kw['categ_id'])
                # CORRECTION #3 : le filtre catégorie s'applique TOUJOURS,
                # même en combinaison avec collection/batch.
                # Si collection filtre déjà un domaine (id, 'in', [...]),
                # on filtre AUSSI par catégorie sur ce même sous-ensemble.
                # On n'utilise PAS child_of ici pour éviter les conflits de domaine
                # avec des catégories qui ne sont pas dans la collection.
                # On fait une intersection via search supplémentaire si besoin.
                if kw.get('collection_id') or kw.get('batch_id'):
                    # Le domaine a déjà un filtre ('id', 'in', all_tmpl_ids)
                    # On ajoute categ_id directement sur les product.template déjà filtrés
                    domain.append(('categ_id', 'child_of', categ_id))
                else:
                    domain.append(('categ_id', 'child_of', categ_id))
            except (ValueError, TypeError):
                pass

        domain += self._sachet_exclude_domain('collection_id')

        return domain

    def _build_pos_domain(self, kw, product_tmpl_ids):
        domain = [
            ('order_id.state', 'in', ['paid', 'done', 'invoiced']),
            ('is_reward_line', '=', False),
        ]
        domain += self._sachet_exclude_domain('product_id.product_tmpl_id.collection_id')
        # Cohérence avec _build_purchase_domain / _get_stock_quants : aucune
        # vente POS société non-retail vérifiée en base actuellement, mais
        # on exclut quand même par sécurité pour ne jamais reproduire
        # l'incohérence achats/stock corrigée ci-dessous.
        non_retail_company_ids = self._get_non_retail_company_ids()
        if non_retail_company_ids:
            domain.append(('order_id.company_id', 'not in', non_retail_company_ids))

        if product_tmpl_ids is not None:
            domain.append(('product_id.product_tmpl_id', 'in', product_tmpl_ids))

        if kw.get('date_start'):
            domain.append(('order_id.date_order', '>=', kw['date_start'] + ' 00:00:00'))
        if kw.get('date_end'):
            domain.append(('order_id.date_order', '<=', kw['date_end'] + ' 23:59:59'))

        if kw.get('shop_field'):
            mapping = request.env['mv.batch.shop.mapping'].sudo().search(
                [('shop_field', '=', kw['shop_field'])], limit=1
            )
            if mapping:
                if mapping.company_id:
                    domain.append(('order_id.company_id', '=', mapping.company_id.id))
                if mapping.warehouse_id:
                    configs = request.env['pos.config'].sudo().search([
                        ('picking_type_id.warehouse_id', '=', mapping.warehouse_id.id)
                    ])
                    if configs:
                        domain.append(('order_id.session_id.config_id', 'in', configs.ids))
        else:
            # Pas de magasin précis choisi dans le dashboard : on retombe sur
            # la/les société(s) cochée(s) dans le sélecteur standard Odoo.
            context_company_ids = self._get_context_company_ids()
            if context_company_ids:
                domain.append(('order_id.company_id', 'in', context_company_ids))

        return domain

    def _build_purchase_domain(self, kw, product_tmpl_ids):
        # CORRECTION : ('order_id.state', '!=', 'cancel') comptait aussi les
        # bons de commande brouillon/envoyés (non confirmés) comme "achetée",
        # gonflant Qté Achetée, le stock théorique et le dénominateur du
        # sell-through. On ne compte désormais que les commandes réellement
        # confirmées/validées.
        domain = [
            ('order_id.state', 'in', ['purchase', 'done']),
        ]
        domain += self._sachet_exclude_domain('product_id.product_tmpl_id.collection_id')
        # BUG CORRIGÉ (vérifié en base) : "Stock Réel Odoo" exclut déjà les
        # sociétés non-retail (MOD FOR LIFE, PAIE — voir _get_stock_quants),
        # mais ce domaine achats ne le faisait pas : les commandes reçues
        # par l'entrepôt MOD FOR LIFE (un point de réception fournisseur
        # séparé du réseau de magasins, PAS un magasin lui-même) étaient
        # comptées dans Qté Achetée alors que leur stock résiduel est
        # invisible dans Stock Réel. Sur un échantillon vérifié : 21 721
        # pièces / 331 lignes / 57 références concernées, ce qui gonflait
        # artificiellement l'écart "achats - vendu vs stock réel" affiché
        # avec ⚠️ (ex: +233 sur une référence, exactement le volume reçu
        # chez MOD FOR LIFE pour cette référence).
        non_retail_company_ids = self._get_non_retail_company_ids()
        if non_retail_company_ids:
            domain.append(('order_id.company_id', 'not in', non_retail_company_ids))
        if product_tmpl_ids is not None:
            domain.append(('product_id.product_tmpl_id', 'in', product_tmpl_ids))

        if kw.get('date_start'):
            domain.append(('order_id.date_order', '>=', kw['date_start'] + ' 00:00:00'))
        if kw.get('date_end'):
            domain.append(('order_id.date_order', '<=', kw['date_end'] + ' 23:59:59'))

        # CORRECTION : la Qté achetée ne bougeait jamais avec le filtre
        # société/magasin car ce domaine, contrairement à _build_pos_domain,
        # n'appliquait aucun filtre company/warehouse — les achats de TOUTES
        # les sociétés étaient donc toujours comptés, peu importe le magasin
        # sélectionné. On réplique ici le même filtre que pour les ventes
        # POS : société via order_id.company_id, magasin via le picking
        # (bon de réception) rattaché à l'entrepôt du magasin.
        if kw.get('shop_field'):
            mapping = request.env['mv.batch.shop.mapping'].sudo().search(
                [('shop_field', '=', kw['shop_field'])], limit=1
            )
            if mapping:
                if mapping.company_id:
                    domain.append(('order_id.company_id', '=', mapping.company_id.id))
                if mapping.warehouse_id:
                    domain.append(('order_id.picking_type_id.warehouse_id', '=', mapping.warehouse_id.id))
        else:
            # Pas de magasin précis choisi dans le dashboard : on retombe sur
            # la/les société(s) cochée(s) dans le sélecteur standard Odoo.
            context_company_ids = self._get_context_company_ids()
            if context_company_ids:
                domain.append(('order_id.company_id', 'in', context_company_ids))
        return domain

    def _get_stock_quants(self, product_tmpl_ids=None, shop_field=None):
        """Get stock.quant records for internal locations, optionally filtered by templates and shop."""
        quant_domain = [
            ('location_id.usage', '=', 'internal'),
            ('company_id', 'not in', self._get_non_retail_company_ids()),
        ]
        quant_domain += self._sachet_exclude_domain('product_id.product_tmpl_id.collection_id')
        if shop_field:
            mapping = request.env['mv.batch.shop.mapping'].sudo().search(
                [('shop_field', '=', shop_field)], limit=1
            )
            if mapping:
                if mapping.company_id:
                    quant_domain.append(('company_id', '=', mapping.company_id.id))
                if mapping.warehouse_id and mapping.warehouse_id.lot_stock_id:
                    quant_domain.append(('location_id', 'child_of', mapping.warehouse_id.lot_stock_id.id))
        if product_tmpl_ids is not None:
            variants = request.env['product.product'].sudo().search(
                [('product_tmpl_id', 'in', product_tmpl_ids)]
            )
            quant_domain.append(('product_id', 'in', variants.ids))
        return request.env['stock.quant'].sudo().search(quant_domain)

    # ─────────────────────────────────────────────────────────────
    # MAGASIN RESOLUTION (source unique de vérité : mv.batch.shop.mapping
    # + stock réel via stock.quant par entrepôt)
    # ─────────────────────────────────────────────────────────────

    def _get_active_shop_mappings(self):
        """Liste des magasins réellement configurés (hors ligne générique 'shop')."""
        return request.env['mv.batch.shop.mapping'].sudo().search([
            ('active', '=', True), ('shop_field', '!=', 'shop')
        ])

    def _clean_ref_for_lookup(self, text):
        if not text:
            return ""
        t = re.sub(r'\[.*?\]', '', text)
        t = re.sub(r'\(.*?\)', '', t)
        return t.strip()

    def _find_articles_for_template(self, product_tmpl_id):
        """
        Retrouve les mv.article.base liés à ce product.template.
        Beaucoup d'articles (notamment ceux en rupture, souvent anciens
        ou en attente de traitement réassort) n'ont pas product_tmpl_id
        renseigné : on retombe alors sur la référence / désignation,
        exactement comme le fait déjà api_product_detail.
        """
        ArticleBase = request.env['mv.article.base'].sudo()
        articles = ArticleBase.search([('product_tmpl_id', '=', product_tmpl_id)])
        if articles:
            return articles

        tmpl = request.env['product.template'].sudo().browse(product_tmpl_id)
        if not tmpl.exists():
            return articles

        refs_to_try = []
        base_ref = getattr(tmpl, 'base_pivot_reference', False)
        if base_ref:
            refs_to_try.append(base_ref.strip())
        if tmpl.default_code:
            refs_to_try.append(tmpl.default_code.strip())
            cleaned_code = self._clean_ref_for_lookup(tmpl.default_code)
            if cleaned_code:
                refs_to_try.append(cleaned_code)
        if tmpl.name:
            refs_to_try.append(tmpl.name.strip())
            cleaned_name = self._clean_ref_for_lookup(tmpl.name)
            if cleaned_name:
                refs_to_try.append(cleaned_name)

        refs_to_try = list(dict.fromkeys([r for r in refs_to_try if r]))

        for ref in refs_to_try:
            found = ArticleBase.search([('reference', '=ilike', ref)])
            if found:
                return found
            found = ArticleBase.search([('designation_odoo', '=ilike', ref)])
            if found:
                return found

        return articles  # vide

    def _resolve_exact_magasin(self, product_tmpl_id, shop_mappings=None, shop_field_filter=None):
        """
        Retourne le magasin réel où ce produit est en tension, basé sur le
        stock.quant réel par entrepôt (pas sur les colonnes pivot de dispatch
        qui reflètent seulement l'historique d'allocation).
        """
        if shop_mappings is None:
            shop_mappings = self._get_active_shop_mappings()

        if shop_field_filter:
            mapping = shop_mappings.filtered(lambda m: m.shop_field == shop_field_filter)[:1]
            if mapping:
                return mapping.warehouse_id.name if mapping.warehouse_id else (mapping.shop_label or shop_field_filter)
            return 'Réseau'

        variants = request.env['product.product'].sudo().search([
            ('product_tmpl_id', '=', product_tmpl_id)
        ])
        if not variants:
            return 'Réseau'

        stock_by_shop = {}
        for sm in shop_mappings:
            if not sm.warehouse_id or not sm.warehouse_id.lot_stock_id:
                continue
            quants = request.env['stock.quant'].sudo().search([
                ('product_id', 'in', variants.ids),
                ('location_id', 'child_of', sm.warehouse_id.lot_stock_id.id),
            ])
            qty = sum(quants.mapped('quantity')) if quants else 0.0
            label = sm.warehouse_id.name if sm.warehouse_id else (sm.shop_label or sm.shop_field)
            stock_by_shop[sm.shop_field] = (qty, label)

        if not stock_by_shop:
            return 'Réseau'

        field_min = min(stock_by_shop, key=lambda f: stock_by_shop[f][0])
        return stock_by_shop[field_min][1]

    def _resolve_magasin_batch(self, product_tmpl_ids, shop_mappings, shop_field_filter=None, mode='min'):
        """
        Version "en masse" de _resolve_exact_magasin : résout le magasin pour
        une LISTE de templates en (nombre de magasins) requêtes au lieu de
        (nombre de templates × nombre de magasins) — évite le N+1 catastrophique
        si on appelait _resolve_exact_magasin dans une boucle Python.

        mode='min' : magasin où le stock est le PLUS BAS (pour repérer où un
        produit est en tension — alertes de rupture).
        mode='max' : magasin où le stock est le PLUS ÉLEVÉ (pour repérer où un
        stock dormant/excédentaire est réellement immobilisé).
        """
        product_tmpl_ids = list(product_tmpl_ids)
        magasin_by_tid = {}
        if not product_tmpl_ids:
            return magasin_by_tid

        if shop_field_filter:
            filt_mapping = shop_mappings.filtered(lambda m: m.shop_field == shop_field_filter)[:1]
            default_label = (
                (filt_mapping.warehouse_id.name if filt_mapping.warehouse_id else (filt_mapping.shop_label or shop_field_filter))
                if filt_mapping else 'Réseau'
            )
            for tid in product_tmpl_ids:
                magasin_by_tid[tid] = default_label
            return magasin_by_tid

        variant_rows = request.env['product.product'].sudo().search_read(
            [('product_tmpl_id', 'in', product_tmpl_ids)],
            ['id', 'product_tmpl_id']
        )
        variant_ids_by_tid = {}
        for v in variant_rows:
            variant_ids_by_tid.setdefault(v['product_tmpl_id'][0], []).append(v['id'])
        all_variant_ids = [vid for vids in variant_ids_by_tid.values() for vid in vids]

        shop_qty_by_tid = {tid: {} for tid in product_tmpl_ids}
        if all_variant_ids:
            for sm in shop_mappings:
                if not sm.warehouse_id or not sm.warehouse_id.lot_stock_id:
                    continue
                q_grouped = request.env['stock.quant'].sudo().read_group(
                    [
                        ('product_id', 'in', all_variant_ids),
                        ('location_id', 'child_of', sm.warehouse_id.lot_stock_id.id),
                    ],
                    ['quantity:sum', 'product_id'], ['product_id'], lazy=False
                )
                qty_by_variant = {g['product_id'][0]: g.get('quantity') or 0.0 for g in q_grouped if g.get('product_id')}
                label = sm.warehouse_id.name if sm.warehouse_id else (sm.shop_label or sm.shop_field)
                for tid, vids in variant_ids_by_tid.items():
                    qty = sum(qty_by_variant.get(vid, 0.0) for vid in vids)
                    shop_qty_by_tid[tid][sm.shop_field] = (qty, label)

        picker = max if mode == 'max' else min
        for tid in product_tmpl_ids:
            shop_data = shop_qty_by_tid.get(tid) or {}
            if not shop_data:
                magasin_by_tid[tid] = 'Réseau'
            else:
                field_pick = picker(shop_data, key=lambda f: shop_data[f][0])
                magasin_by_tid[tid] = shop_data[field_pick][1]

        return magasin_by_tid

    # ─────────────────────────────────────────────────────────────
    # FILTERS
    # ─────────────────────────────────────────────────────────────

    @http.route('/mavie/api/filters', type='json', auth='user', methods=['POST'], csrf=False)
    def api_filters(self, **kw):
        try:
            filters = {
                'collections': [],
                'batches': [],
                'shops': [],
                'categories': [],
            }

            try:
                Collection = request.env['product.collection'].sudo()
                collections = Collection.search([])
                filters['collections'] = [
                    {'id': c.id, 'name': c.name} for c in collections
                    if c.name and 'sachet 2026' not in c.name.lower() and 'sacher 2026' not in c.name.lower()
                ]
            except Exception as e:
                _logger.warning(f"Erreur collections: {str(e)}")

            try:
                Arrivage = request.env['product.arrivage'].sudo()
                arrivages = Arrivage.search([])
                filters['batches'] = [
                    {'id': a.id, 'name': a.name, 'collection': a.collection_id.name if a.collection_id else '—'}
                    for a in arrivages
                    if a.name and 'sachet 2026' not in a.name.lower() and 'sacher 2026' not in a.name.lower()
                ]
            except Exception as e:
                _logger.warning(f"Erreur arrivages: {str(e)}")

            try:
                shops = self._get_active_shop_mappings()
                filters['shops'] = [
                    {'field': s.shop_field, 'name': s.warehouse_id.name if s.warehouse_id else (s.shop_label or s.shop_field)}
                    for s in shops
                ]
            except Exception as e:
                _logger.warning(f"Erreur shop mapping: {str(e)}")

            try:
                request.env.cr.execute("""
                    SELECT DISTINCT pc.id, pc.name
                    FROM product_template pt
                    JOIN product_category pc ON pc.id = pt.categ_id
                    WHERE pt.active = true
                    ORDER BY pc.name
                """)
                rows = request.env.cr.fetchall()
                excluded_names = {
                    'all', 'expenses', 'saleable', 'pos', 'bons & fidélité',
                    'demi0', 'solde test 2', 'étiquettes solde',
                }
                filters['categories'] = [
                    {'id': r[0], 'name': r[1]} for r in rows
                    if r[1] and r[1].lower().strip() not in excluded_names
                ]
            except Exception as e:
                _logger.warning(f"Erreur categories: {str(e)}")

            return filters

        except Exception as e:
            _logger.error(f"Erreur api_filters: {str(e)}")
            return {'error': str(e), 'collections': [], 'batches': [], 'shops': [], 'categories': []}

    # ─────────────────────────────────────────────────────────────
    # KPIs
    # ─────────────────────────────────────────────────────────────

    @http.route('/mavie/api/kpis', type='json', auth='user', methods=['POST'], csrf=False)
    def api_kpis(self, **kw):
        return self._compute_kpis(kw)

    def _compute_kpis(self, kw):
        try:
            # MOD FOR LIFE n'est pas un magasin retail (pas de vente en
            # caisse, pas d'alertes rupture au sens boutique) : jusqu'ici
            # NON_RETAIL_COMPANIES l'excluait de tout, ce qui rendait le
            # domaine contradictoire (company_id NOT IN [...] ET company_id
            # IN [MOD FOR LIFE] en même temps) si un utilisateur la
            # sélectionnait dans le sélecteur société standard -> 0 partout.
            # Si c'est la SEULE société cochée (et qu'aucun magasin précis
            # n'est choisi), on bascule vers un calcul dédié plutôt que de
            # forcer ce cas dans la logique retail.
            if not kw.get('shop_field'):
                mod_for_life = request.env['res.company'].sudo().search(
                    [('name', '=', 'MOD FOR LIFE')], limit=1
                )
                if mod_for_life and self._get_context_company_ids() == [mod_for_life.id]:
                    return self._compute_kpis_modforlife(kw, mod_for_life)

            is_filtered = bool(kw.get('collection_id') or kw.get('batch_id') or kw.get('categ_id'))

            product_tmpl_ids = None
            if is_filtered:
                domain = self._build_product_domain(kw)
                ProductTemplate = request.env['product.template'].sudo()
                products = ProductTemplate.search(domain)
                if not products:
                    return {
                        'ca_total': 0, 'ca_ht': 0, 'ca_achat': 0,
                        'ca_achat_externe': 0, 'ca_achat_interne': 0, 'ca_achat_by_company': [],
                        'qty_purchased_externe': 0, 'qty_purchased_interne': 0,
                        'vendu_avec_cout': 0, 'marge': 0,
                        'tickets': 0, 'panier_moyen': 0,
                        'qty_sold': 0, 'qty_sold_normal': 0, 'qty_sold_solde': 0,
                        'qty_purchased': 0, 'stock_total': 0,
                        'sell_through': 0, 'ruptures_count': 0, 'ruptures_list': [],
                        'top_products': [], 'flop_products': [],
                        'abc_analysis': {'A': [], 'B': [], 'C': []},
                        'references_count': 0, 'total_active_skus': 0,
                        'taux_rupture': 0, 'couverture_moy': 0,
                        'stock_dormant_pct': 0, 'dormant_count': 0, 'dormant_list': [], 'precision_inventaire': 99.5,
                        'alertes_stock': [], 'rotation_collection': [],
                        'gmroi_categorie': [], 'proches_rupture_30j': [],
                        'valeur_stock_ht': 0, 'valeur_stock_cost': 0, 'stock_val_by_store': [],
                    }
                product_tmpl_ids = products.ids
            else:
                ProductTemplate = request.env['product.template'].sudo()

            pos_domain = self._build_pos_domain(kw, product_tmpl_ids)

            # ✅ OPTIMISATION PERF : pos_agg (totaux) et pos_grouped (par
            # produit) scannaient chacun TOUTE pos_order_line (400k+ lignes,
            # jointure product_product/product_template) avec le même
            # domaine — un doublon pur qui coûtait ~0.5s par scan. La somme
            # des groupes par produit = la somme globale (SUM distributif),
            # donc ca_total/qty_sold_total se déduisent de pos_grouped sans
            # 2e scan. Seul le nombre de tickets (commandes DISTINCTES) ne
            # peut pas se déduire d'un regroupement par produit (une commande
            # multi-produits serait comptée plusieurs fois) : c'est la seule
            # requête encore séparée.
            pos_grouped = request.env['pos.order.line'].sudo().read_group(
                pos_domain,
                ['price_subtotal_incl:sum', 'price_subtotal:sum', 'qty:sum', 'product_id'],
                ['product_id'],
                lazy=False
            )
            ca_total = sum(g.get('price_subtotal_incl') or 0.0 for g in pos_grouped)
            # CA hors taxe — utilisé pour comparer à un coût (HT lui aussi),
            # afin que la marge ne soit pas gonflée artificiellement du
            # montant de la TVA (voir ca_achat_total / vendu_avec_cout_total
            # / marge_total plus bas).
            ca_ht_total = sum(g.get('price_subtotal') or 0.0 for g in pos_grouped)
            qty_sold_total = int(sum(g.get('qty') or 0 for g in pos_grouped))

            # Qté vendue "en solde" = lignes vendues à un prix effectif
            # (remise incluse) inférieur au prix catalogue (list_price) du
            # produit — détection par prix, pas par date. read_group ne peut
            # pas comparer deux champs entre eux (price_unit*(1-discount/100)
            # vs list_price d'un modèle lié), donc agrégation SQL directe,
            # restreinte aux IDs déjà filtrés par pos_domain (pas de 2e scan
            # complet de pos.order.line).
            qty_sold_solde_total = 0
            pos_line_ids_for_solde = request.env['pos.order.line'].sudo().search(pos_domain).ids
            if pos_line_ids_for_solde:
                request.env.cr.execute("""
                    SELECT COALESCE(SUM(
                        CASE WHEN pol.price_unit * (1 - COALESCE(pol.discount, 0) / 100.0) < pt.list_price * 0.999
                             THEN pol.qty ELSE 0 END
                    ), 0)
                    FROM pos_order_line pol
                    JOIN product_product pp ON pp.id = pol.product_id
                    JOIN product_template pt ON pt.id = pp.product_tmpl_id
                    WHERE pol.id IN %s AND pt.list_price > 0
                """, (tuple(pos_line_ids_for_solde),))
                qty_sold_solde_total = int(request.env.cr.fetchone()[0] or 0)
            qty_sold_normal_total = qty_sold_total - qty_sold_solde_total

            pos_tickets_agg = request.env['pos.order.line'].sudo().read_group(
                pos_domain, ['order_id:count_distinct'], []
            )
            tickets = (pos_tickets_agg[0].get('order_id') or 0) if pos_tickets_agg else 0

            panier_moyen = ca_total / tickets if tickets > 0 else 0.0

            pos_pids = [g['product_id'][0] for g in pos_grouped if g.get('product_id')]
            sales_by_tmpl = {}
            prod_to_tmpl = {}
            if pos_pids:
                prods = request.env['product.product'].sudo().search_read(
                    [('id', 'in', pos_pids)],
                    ['id', 'product_tmpl_id']
                )
                prod_to_tmpl = {p['id']: p['product_tmpl_id'][0] for p in prods if p.get('product_tmpl_id')}
                for g in pos_grouped:
                    pid = g['product_id'][0] if g.get('product_id') else None
                    tid = prod_to_tmpl.get(pid)
                    if not tid:
                        continue
                    if tid not in sales_by_tmpl:
                        sales_by_tmpl[tid] = {'qty': 0, 'ca': 0.0}
                    sales_by_tmpl[tid]['qty'] += g.get('qty') or 0
                    sales_by_tmpl[tid]['ca'] += g.get('price_subtotal_incl') or 0.0

            # ✅ Idem achats : un seul scan groupé par produit, le total
            # s'obtient en sommant les groupes au lieu d'un 2e scan complet
            # (po_agg supprimé).
            purchase_domain = self._build_purchase_domain(kw, product_tmpl_ids)
            po_grouped = request.env['purchase.order.line'].sudo().read_group(
                purchase_domain,
                ['product_qty:sum', 'price_subtotal:sum', 'product_id'],
                ['product_id'],
                lazy=False
            )
            qty_purchased_total = int(sum(g.get('product_qty') or 0 for g in po_grouped))
            # CA Achat = coût réel des marchandises achetées (HT, tel que
            # facturé sur les bons de commande), pas une reconstruction
            # qty*prix_standard.
            ca_achat_total = sum(g.get('price_subtotal') or 0.0 for g in po_grouped)

            # Achat externe (vrai fournisseur) vs achat interne (depuis
            # MOD FOR LIFE, au prix coûtant) — une requête supplémentaire sur
            # le même domaine + filtre fournisseur, pas un nouveau scan
            # complet (externe se déduit par soustraction du total déjà
            # calculé ci-dessus).
            mod_for_life_partner_id = self._get_mod_for_life_partner_id()
            qty_purchased_interne = 0
            ca_achat_interne = 0.0
            if mod_for_life_partner_id:
                po_grouped_interne = request.env['purchase.order.line'].sudo().read_group(
                    purchase_domain + [('partner_id', '=', mod_for_life_partner_id)],
                    ['product_qty:sum', 'price_subtotal:sum'], [], lazy=False
                )
                if po_grouped_interne:
                    qty_purchased_interne = int(po_grouped_interne[0].get('product_qty') or 0)
                    ca_achat_interne = po_grouped_interne[0].get('price_subtotal') or 0.0
            qty_purchased_externe = qty_purchased_total - qty_purchased_interne
            ca_achat_externe = ca_achat_total - ca_achat_interne

            # Répartition CA Achat par société magasin (SALMEDO / BLACK AND
            # GOLD / DELTA-GOLD) — le total ci-dessus combine déjà les 3
            # (vérifié en base : somme des 3 = total non filtré), mais on
            # veut aussi voir la contribution de chacune, pas seulement le
            # combiné, surtout quand aucun filtre société précis n'est actif.
            ca_achat_by_company = []
            po_grouped_by_company = request.env['purchase.order.line'].sudo().read_group(
                purchase_domain, ['price_subtotal:sum', 'company_id'], ['company_id'], lazy=False
            )
            for g in po_grouped_by_company:
                if g.get('company_id'):
                    ca_achat_by_company.append({
                        'societe': g['company_id'][1],
                        'ca_achat': round(g.get('price_subtotal') or 0.0, 2),
                    })
            ca_achat_by_company.sort(key=lambda x: -x['ca_achat'])

            # Sell-through = part du stock reçu qui a été vendue : vendu / acheté.
            # (PAS vendu / (vendu + acheté), qui donnait un chiffre bien trop bas.)
            sell_through = round((qty_sold_total / qty_purchased_total * 100), 1) if qty_purchased_total > 0 else 0.0

            po_pids = [g['product_id'][0] for g in po_grouped if g.get('product_id')]
            purchase_by_tmpl = {}
            if po_pids:
                po_prods = request.env['product.product'].sudo().search_read(
                    [('id', 'in', po_pids)],
                    ['id', 'product_tmpl_id']
                )
                po_prod_to_tmpl = {p['id']: p['product_tmpl_id'][0] for p in po_prods if p.get('product_tmpl_id')}
                for g in po_grouped:
                    pid = g['product_id'][0] if g.get('product_id') else None
                    tid = po_prod_to_tmpl.get(pid)
                    if not tid:
                        continue
                    purchase_by_tmpl[tid] = purchase_by_tmpl.get(tid, 0) + int(g.get('product_qty') or 0)

            # ✅ Groupement Stock Quant (SQL GROUP BY product_id) — même
            # principe : stock_total se déduit de quant_grouped (quant_agg
            # supprimé), ce qui évite un 3e scan complet en double.
            #
            # quant_domain_base = localisation + sachet + filtre société/
            # magasin choisi par l'utilisateur, SANS l'exclusion non-retail —
            # réutilisé tel quel plus bas pour la valorisation (qui doit
            # inclure TOUTES les sociétés, y compris MOD FOR LIFE).
            # quant_domain = version "retail" (stock_total, dormant,
            # ruptures...), avec l'exclusion en plus. BUG CORRIGÉ : la
            # valorisation tentait avant de retirer cette exclusion après
            # coup en filtrant sur la mauvaise clé de tuple
            # ('company_id.name' au lieu de 'company_id') — ça ne retirait
            # jamais rien, MOD FOR LIFE restait exclu malgré le commentaire
            # d'intention "garde TOUTES les sociétés".
            quant_domain_base = [
                ('location_id.usage', '=', 'internal'),
            ]
            quant_domain_base += self._sachet_exclude_domain('product_id.product_tmpl_id.collection_id')
            shop_field = kw.get('shop_field')
            if shop_field:
                mapping = request.env['mv.batch.shop.mapping'].sudo().search(
                    [('shop_field', '=', shop_field)], limit=1
                )
                if mapping:
                    if mapping.company_id:
                        quant_domain_base.append(('company_id', '=', mapping.company_id.id))
                    if mapping.warehouse_id and mapping.warehouse_id.lot_stock_id:
                        quant_domain_base.append(('location_id', 'child_of', mapping.warehouse_id.lot_stock_id.id))
            else:
                # Pas de magasin précis choisi : on retombe sur la/les
                # société(s) cochée(s) dans le sélecteur standard Odoo.
                context_company_ids = self._get_context_company_ids()
                if context_company_ids:
                    quant_domain_base.append(('company_id', 'in', context_company_ids))
            if product_tmpl_ids is not None:
                q_variants = request.env['product.product'].sudo().search_read(
                    [('product_tmpl_id', 'in', product_tmpl_ids)],
                    ['id']
                )
                quant_domain_base.append(('product_id', 'in', [v['id'] for v in q_variants]))

            quant_domain = quant_domain_base + [('company_id', 'not in', self._get_non_retail_company_ids())]

            quant_grouped = request.env['stock.quant'].sudo().read_group(
                quant_domain,
                ['quantity:sum', 'product_id'],
                ['product_id'],
                lazy=False
            )
            stock_total = int(sum(g.get('quantity') or 0 for g in quant_grouped))
            quant_pids = [g['product_id'][0] for g in quant_grouped if g.get('product_id')]
            stock_by_tmpl = {}
            if quant_pids:
                q_prods = request.env['product.product'].sudo().search_read(
                    [('id', 'in', quant_pids)],
                    ['id', 'product_tmpl_id']
                )
                q_prod_to_tmpl = {p['id']: p['product_tmpl_id'][0] for p in q_prods if p.get('product_tmpl_id')}
                for g in quant_grouped:
                    pid = g['product_id'][0] if g.get('product_id') else None
                    tid = q_prod_to_tmpl.get(pid)
                    if not tid:
                        continue
                    stock_by_tmpl[tid] = stock_by_tmpl.get(tid, 0) + int(g.get('quantity') or 0)

            page = kw.get('page', 'ventes')
            # ✅ On calcule et renvoie toujours jusqu'à 100 lignes (= le max du
            # sélecteur côté client), quelle que soit la valeur actuellement
            # affichée. Le nombre choisi par l'utilisateur (10, 20, 50...) ne
            # sert plus qu'à trancher côté client la liste déjà reçue : changer
            # cette valeur ne relance donc plus tout le calcul KPI (ABC,
            # ruptures, GMROI, stock dormant, valorisation...), qui scanne les
            # ventes/achats/stock et coûtait plusieurs centaines de ms à
            # chaque clic pour un simple changement d'affichage.
            top_limit = 100
            flop_limit = 100

            if is_filtered:
                filtered_set = set(product_tmpl_ids)
                filtered_sales = {k: v for k, v in sales_by_tmpl.items() if k in filtered_set}
                filtered_purchase = {k: v for k, v in purchase_by_tmpl.items() if k in filtered_set}
                filtered_stock = {k: v for k, v in stock_by_tmpl.items() if k in filtered_set}
            else:
                filtered_sales = sales_by_tmpl
                filtered_purchase = purchase_by_tmpl
                filtered_stock = stock_by_tmpl

            if page == 'stock':
                sorted_top = sorted(filtered_stock.keys(), key=lambda k: filtered_stock[k], reverse=True)
                sorted_flop = sorted(filtered_stock.keys(), key=lambda k: filtered_stock[k])
            elif page == 'commandes':
                sorted_top = sorted(filtered_purchase.keys(), key=lambda k: filtered_purchase[k], reverse=True)
                sorted_flop = sorted(filtered_purchase.keys(), key=lambda k: filtered_purchase[k])
            else:
                sorted_top = sorted(filtered_sales.keys(), key=lambda k: filtered_sales[k]['ca'], reverse=True)
                sorted_flop = sorted(filtered_sales.keys(), key=lambda k: filtered_sales[k]['ca'])

            candidate_ids = set(sorted_top[:top_limit] + sorted_flop[:flop_limit])

            if is_filtered and len(candidate_ids) < (top_limit + flop_limit):
                active_in_filter = set(filtered_sales.keys()) | set(filtered_purchase.keys()) | set(filtered_stock.keys())
                remaining = active_in_filter - candidate_ids
                if remaining:
                    candidate_ids.update(list(remaining)[:(top_limit + flop_limit) - len(candidate_ids)])
                if len(candidate_ids) < (top_limit + flop_limit):
                    rest = filtered_set - candidate_ids
                    candidate_ids.update(list(rest)[:(top_limit + flop_limit) - len(candidate_ids)])
            elif not is_filtered and len(candidate_ids) < (top_limit + flop_limit):
                active_all = set(filtered_sales.keys()) | set(filtered_purchase.keys()) | set(filtered_stock.keys())
                remaining = active_all - candidate_ids
                if remaining:
                    candidate_ids.update(list(remaining)[:(top_limit + flop_limit) - len(candidate_ids)])

            if not candidate_ids:
                if is_filtered and product_tmpl_ids:
                    candidate_ids = set(product_tmpl_ids[:top_limit + flop_limit])
                else:
                    candidate_ids = set(list(sales_by_tmpl.keys())[:top_limit] + list(sales_by_tmpl.keys())[-flop_limit:])

            active_products = ProductTemplate.browse(list(candidate_ids))

            product_stats = []
            for tmpl in active_products:
                stat = sales_by_tmpl.get(tmpl.id, {'qty': 0, 'ca': 0})
                ref = (
                    tmpl.base_pivot_reference
                    or tmpl.default_code
                    or tmpl.name
                    or '—'
                )
                product_stats.append({
                    'id': tmpl.id,
                    'name': tmpl.name or '—',
                    'ref': ref,
                    'ca': stat['ca'],
                    'qty': int(stat['qty']),
                    'qty_sold': int(stat['qty']),
                    'stock': int(stock_by_tmpl.get(tmpl.id, 0)),
                    'qty_purchased': int(purchase_by_tmpl.get(tmpl.id, 0)),
                })

            if is_filtered:
                relevant_tmpl_ids = set(product_tmpl_ids)
            else:
                # Vérifié en base : les 53 références suivies dans Base Pivot
                # ont TOUTES au moins une activité native (vente, achat ou
                # stock) — l'union avec mv.article.base ne changeait donc
                # jamais cet univers, elle est retirée sans impact.
                relevant_tmpl_ids = set(sales_by_tmpl.keys()) | set(purchase_by_tmpl.keys()) | set(stock_by_tmpl.keys())

            all_ruptures = []
            if relevant_tmpl_ids:
                tmpl_data = request.env['product.template'].sudo().search_read(
                    [('id', 'in', list(relevant_tmpl_ids))],
                    ['name', 'default_code', 'base_pivot_reference', 'standard_price', 'categ_id', 'collection_id']
                )
                tmpl_by_id = {t['id']: t for t in tmpl_data}

                for tid in relevant_tmpl_ids:
                    stock = int(stock_by_tmpl.get(tid, 0))
                    if stock <= 0:
                        t = tmpl_by_id.get(tid, {})
                        ref = (
                            t.get('base_pivot_reference')
                            or t.get('default_code')
                            or t.get('name')
                            or '—'
                        )
                        all_ruptures.append({
                            'id': tid,
                            'name': t.get('name') or '—',
                            'ref': ref,
                            'stock': stock,
                            'ca': sales_by_tmpl.get(tid, {}).get('ca', 0),
                            'qty_sold': int(sales_by_tmpl.get(tid, {}).get('qty', 0)),
                        })

            ruptures_count = len(all_ruptures)
            ruptures_list = sorted(all_ruptures, key=lambda a: a['qty_sold'], reverse=True)[:500]

            # CORRECTION #2c : references_count = nombre RÉEL de références Achats.
            # On exclut la collection "Sachet 2026" via son vrai lien relationnel
            # (collection_id), pas via un filtre texte sur le nom du produit
            # (voir _sachet_exclude_domain).
            # CORRECTION (vérifiée en base) : il manquait le filtre purchase_ok.
            # Le catalogue product.template contient de nombreuses fiches
            # incomplètes/non achetables (ex: 1020 produits actifs sans aucun
            # prix de vente) qui gonflaient le chiffre à 5995 au lieu des 4972
            # vraies références achetables ("Achats > Produits" dans Odoo
            # filtre lui aussi sur purchase_ok=True — le commentaire ci-dessus
            # visait déjà ce chiffre-là, le filtre manquait juste).
            references_exclude_domain = self._sachet_exclude_domain('collection_id') + [('purchase_ok', '=', True)]

            # Si filtré par collection/batch/catégorie : on compte les produits du filtre.
            # Si filtré par société/magasin (shop_field) : on compte les produits
            # réellement présents en stock dans CETTE société — la référence
            # "bouge" donc avec la société sélectionnée.
            # Sinon (vue globale) : on compte le vrai catalogue actif tel qu'affiché
            # dans Achats > Produits (product.template.search_count live, donc les
            # ajouts/suppressions de produits se répercutent automatiquement),
            # et non plus seulement les SKUs ayant déjà une vente/achat/stock —
            # c'est ce sous-ensemble qui faisait chuter le chiffre affiché très en
            # dessous du total réel du catalogue (ex: 3922 affiché pour 5996 produits).
            if is_filtered and product_tmpl_ids:
                # product_tmpl_ids vient déjà de _build_product_domain, qui
                # filtre sur les champs natifs collection_id/arrivage_id —
                # pas besoin d'un second comptage via mv.article.base.
                references_count = len(product_tmpl_ids)
            elif shop_field:
                quant_domain_refs = [('location_id.usage', '=', 'internal')]
                mapping_refs = request.env['mv.batch.shop.mapping'].sudo().search(
                    [('shop_field', '=', shop_field)], limit=1
                )
                if mapping_refs and mapping_refs.company_id:
                    quant_domain_refs.append(('company_id', '=', mapping_refs.company_id.id))
                else:
                    quant_domain_refs.append(('company_id', 'not in', self._get_non_retail_company_ids()))
                if mapping_refs and mapping_refs.warehouse_id and mapping_refs.warehouse_id.lot_stock_id:
                    quant_domain_refs.append(('location_id', 'child_of', mapping_refs.warehouse_id.lot_stock_id.id))
                tmpl_ids_here = request.env['stock.quant'].sudo().search(quant_domain_refs).mapped(
                    'product_id.product_tmpl_id'
                ).ids
                references_count = (
                    ProductTemplate.search_count(references_exclude_domain + [('id', 'in', tmpl_ids_here)])
                    if tmpl_ids_here else 0
                )
            else:
                context_company_ids = self._get_context_company_ids()
                if context_company_ids:
                    tmpl_ids_ctx = request.env['stock.quant'].sudo().search([
                        ('location_id.usage', '=', 'internal'),
                        ('company_id', 'in', context_company_ids),
                    ]).mapped('product_id.product_tmpl_id').ids
                    references_count = (
                        ProductTemplate.search_count(references_exclude_domain + [('id', 'in', tmpl_ids_ctx)])
                        if tmpl_ids_ctx else 0
                    )
                else:
                    references_count = ProductTemplate.search_count(references_exclude_domain)

            total_active_skus = len(relevant_tmpl_ids) or 1
            taux_rupture = round((ruptures_count / total_active_skus) * 100, 1)

            date_start = kw.get('date_start')
            date_end = kw.get('date_end')
            # BUG CORRIGÉ (vérifié en base) : quand aucune période n'est
            # choisie par l'utilisateur, qty_sold/sales_by_tmpl couvrent
            # TOUT l'historique des ventes POS (jusqu'à 2013 dans cette
            # base, ~4700 jours) — diviser ce total par un "days_in_period"
            # à 30 jours codé en dur gonflait la vélocité de vente d'un
            # facteur ~150x, d'où des "jours restants avant rupture" et des
            # "ventes/jour" totalement irréalistes (ex: 0.1j restant avec
            # 8-16 ventes/jour pour un article n'ayant que 1-2 pièces en
            # stock). On calcule maintenant un vrai volume de ventes sur une
            # fenêtre récente glissante de 90 jours (même principe que
            # pos_90d_domain plus bas pour le stock dormant) pour ces
            # calculs de vélocité, sauf si l'utilisateur a lui-même choisi
            # une période explicite (date_start ET date_end) — dans ce cas
            # sales_by_tmpl est déjà scopé à cette période, on la garde.
            if date_start and date_end:
                days_in_period = 30
                try:
                    d1 = datetime.strptime(date_start, '%Y-%m-%d')
                    d2 = datetime.strptime(date_end, '%Y-%m-%d')
                    days_in_period = max(1, (d2 - d1).days + 1)
                except ValueError:
                    pass
                sales_by_tmpl_velocity = {tid: s.get('qty', 0) for tid, s in sales_by_tmpl.items()}
                qty_sold_velocity_total = qty_sold_total
            else:
                days_in_period = 90
                kw_velocity = dict(kw)
                kw_velocity['date_start'] = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d')
                pos_domain_velocity = self._build_pos_domain(kw_velocity, product_tmpl_ids)
                pos_grouped_velocity = request.env['pos.order.line'].sudo().read_group(
                    pos_domain_velocity, ['qty:sum', 'product_id'], ['product_id'], lazy=False
                )
                sales_by_tmpl_velocity = {}
                for g in pos_grouped_velocity:
                    pid = g['product_id'][0] if g.get('product_id') else None
                    tid = prod_to_tmpl.get(pid)
                    if not tid:
                        continue
                    sales_by_tmpl_velocity[tid] = sales_by_tmpl_velocity.get(tid, 0) + int(g.get('qty') or 0)
                qty_sold_velocity_total = sum(sales_by_tmpl_velocity.values())

            product_coverages = {}
            for tid in relevant_tmpl_ids:
                stock = stock_by_tmpl.get(tid, 0)
                qty_sold = sales_by_tmpl_velocity.get(tid, 0)
                daily_rate = qty_sold / days_in_period if days_in_period > 0 else 0
                if daily_rate > 0:
                    cov = stock / daily_rate
                else:
                    cov = 999
                product_coverages[tid] = cov

            daily_sales_rate_total = qty_sold_velocity_total / days_in_period if days_in_period > 0 else 0
            couverture_moy = round(stock_total / daily_sales_rate_total) if daily_sales_rate_total > 0 else 0

            # ✅ OPTIMISATION SQL read_group pour les ventes à 90j (stock dormant)
            date_90d_ago = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d 00:00:00')
            pos_90d_domain = [
                ('order_id.date_order', '>=', date_90d_ago),
                ('order_id.state', 'in', ['paid', 'done', 'invoiced']),
                ('is_reward_line', '=', False),
            ] + self._sachet_exclude_domain('product_id.product_tmpl_id.collection_id')
            pos_90d_grouped = request.env['pos.order.line'].sudo().read_group(
                pos_90d_domain,
                ['product_id'],
                ['product_id'],
                lazy=False
            )
            pids_90d = [g['product_id'][0] for g in pos_90d_grouped if g.get('product_id')]
            sold_90d_tmpl_ids = set()
            if pids_90d:
                p90_prods = request.env['product.product'].sudo().search_read(
                    [('id', 'in', pids_90d)],
                    ['product_tmpl_id']
                )
                sold_90d_tmpl_ids = {p['product_tmpl_id'][0] for p in p90_prods if p.get('product_tmpl_id')}

            # ✅ CORRECTION : le pourcentage pouvait dépasser 100% quand des
            # stock.quant négatifs (écarts d'inventaire) faisaient chuter
            # stock_total en dessous de la somme des stocks POSITIFS dormants
            # (celle-ci ignorant volontairement les négatifs). On calcule donc
            # le dénominateur de la même façon que le numérateur : uniquement
            # sur les stocks positifs, pour que le ratio reste borné à 100%.
            dormant_stock_total = 0
            positive_stock_total = 0
            dormant_products = []
            for tid in relevant_tmpl_ids:
                stock = stock_by_tmpl.get(tid, 0)
                if stock > 0:
                    positive_stock_total += stock
                    if tid not in sold_90d_tmpl_ids:
                        dormant_stock_total += stock
                        t = tmpl_by_id.get(tid, {})
                        ref = (
                            t.get('base_pivot_reference')
                            or t.get('default_code')
                            or t.get('name')
                            or '—'
                        )
                        dormant_products.append({
                            'id': tid,
                            'name': t.get('name') or '—',
                            'ref': ref,
                            'stock': stock
                        })
            stock_dormant_pct = round((dormant_stock_total / positive_stock_total) * 100, 1) if positive_stock_total > 0 else 0.0

            inv_adjustments_count = request.env['stock.move'].sudo().search_count([
                ('date', '>=', (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d 00:00:00')),
                ('location_id.usage', '=', 'inventory'),
                ('state', '=', 'done')
            ])
            # Pas de plancher artificiel à 90% : une précision d'inventaire
            # réellement mauvaise (beaucoup d'ajustements) doit pouvoir
            # s'afficher comme telle plutôt que d'être masquée.
            precision_inventaire = min(100.0, round(100.0 - (inv_adjustments_count / (total_active_skus or 1)) * 100, 1)) if inv_adjustments_count > 0 else 99.5

            abc_class_map = {}
            ca_sorted = sorted(product_stats, key=lambda a: a['ca'], reverse=True)
            ca_cumul_total = sum(p['ca'] for p in product_stats)
            cumul = 0
            abc = {'A': [], 'B': [], 'C': []}

            for p in ca_sorted:
                cumul += p['ca']
                pct = (cumul / ca_cumul_total * 100) if ca_cumul_total > 0 else 0
                p_item = {'id': p['id'], 'name': p['name'], 'ref': p['ref'], 'ca': round(p['ca'], 2)}
                if pct <= 80:
                    abc['A'].append(p_item)
                    abc_class_map[p['id']] = 'A'
                elif pct <= 95:
                    abc['B'].append(p_item)
                    abc_class_map[p['id']] = 'B'
                else:
                    abc['C'].append(p_item)
                    abc_class_map[p['id']] = 'C'

            # ── Magasins configurés : source unique de vérité pour toutes les résolutions ──
            shop_mappings = self._get_active_shop_mappings()

            def _resolve_magasin_name(tid):
                return self._resolve_exact_magasin(
                    tid, shop_mappings=shop_mappings, shop_field_filter=kw.get('shop_field')
                )

            # Magasin où le stock dormant est réellement immobilisé (le plus
            # de stock, pas le moins — contraire de la résolution "rupture").
            magasin_by_tid_dormant = self._resolve_magasin_batch(
                [d['id'] for d in dormant_products], shop_mappings, kw.get('shop_field'), mode='max'
            )
            for d in dormant_products:
                d['magasin'] = magasin_by_tid_dormant.get(d['id'], 'Réseau')

            alertes_stock = []

            rupture_cands = [r for r in all_ruptures if r['qty_sold'] > 0]
            rupture_cands.sort(key=lambda x: x['qty_sold'], reverse=True)
            for r in rupture_cands[:3]:
                alertes_stock.append({
                    'type': 'danger',
                    'message': f"{r['name']} — RUPTURE ({r['qty_sold']} vendus)",
                    'magasin': _resolve_magasin_name(r['id']),
                    'id': r['id'],
                    'name': r['name'],
                })

            crit_cands = []
            for tid in relevant_tmpl_ids:
                stock = stock_by_tmpl.get(tid, 0)
                if 0 < stock <= 3:
                    t = tmpl_by_id.get(tid, {})
                    crit_cands.append({
                        'id': tid,
                        'name': t.get('name') or '—',
                        'stock': stock,
                        'qty_sold': sales_by_tmpl.get(tid, {}).get('qty', 0)
                    })
            crit_cands.sort(key=lambda x: x['qty_sold'], reverse=True)
            for c in crit_cands[:3]:
                alertes_stock.append({
                    'type': 'warning',
                    'message': f"{c['name']} — stock critique ({c['stock']} unités)",
                    'magasin': _resolve_magasin_name(c['id']),
                    'id': c['id'],
                    'name': c['name'],
                })

            dormant_products.sort(key=lambda x: x['stock'], reverse=True)
            for d in dormant_products[:2]:
                alertes_stock.append({
                    'type': 'info',
                    'message': f"{d['stock']} unités stock dormant — {d['name']}",
                    'magasin': 'Entrepôt'
                })

            alertes_stock = alertes_stock[:6]

            # ✅ Rotation par collection — 2 requêtes au total (au lieu d'1 recherche
            # product.template PAR collection en boucle, même anti-pattern N+1 que le
            # GMROI ci-dessus, corrigé pour la même raison de rapidité de chargement).
            rotation_collection = []
            try:
                collections = request.env['product.collection'].sudo().search([])
                col_names = {c.id: c.name for c in collections}

                col_tmpl_data = request.env['product.template'].sudo().search_read(
                    [('collection_id', 'in', list(col_names.keys()))],
                    ['collection_id']
                ) if col_names else []
                tmpl_ids_by_collection = {}
                for t in col_tmpl_data:
                    cid = t['collection_id'][0] if t.get('collection_id') else None
                    if cid:
                        tmpl_ids_by_collection.setdefault(cid, []).append(t['id'])

                for col_id, col_name in col_names.items():
                    col_tmpl_ids = tmpl_ids_by_collection.get(col_id, [])
                    col_stock = sum(stock_by_tmpl.get(tid, 0) for tid in col_tmpl_ids)
                    col_sales = sum(sales_by_tmpl_velocity.get(tid, 0) for tid in col_tmpl_ids)
                    col_sales_annualized = col_sales * (365 / days_in_period) if days_in_period > 0 else 0
                    col_turnover = round(col_sales_annualized / col_stock, 1) if col_stock > 0 else 0.0

                    if col_stock > 0 or col_sales > 0:
                        rotation_collection.append({
                            'name': col_name,
                            'turnover': col_turnover,
                            'pct': min(100, int((col_turnover / 6.0) * 100)) if col_turnover > 0 else 15,
                            'warning': f"{col_name} sous seuil critique" if col_turnover < 2.0 and col_turnover > 0 else None
                        })
            except Exception as e:
                _logger.warning(f"Error computing collection rotation: {str(e)}")

            rotation_collection.sort(key=lambda x: (x['turnover'], x['pct']), reverse=True)
            rotation_collection = rotation_collection[:5]

            # ✅ GMROI par catégorie — regroupement 100% en mémoire à partir de
            # tmpl_by_id/sales_by_tmpl/stock_by_tmpl déjà chargés : AUCUNE requête
            # SQL supplémentaire (l'ancienne version faisait 1 search(child_of) par
            # catégorie, jusqu'à 30 requêtes lentes en boucle — anti-pattern N+1).
            gmroi_categorie = []
            try:
                cat_groups = {}
                for tid in relevant_tmpl_ids:
                    categ = tmpl_by_id.get(tid, {}).get('categ_id')
                    if not categ:
                        continue
                    cat_id, cat_name = categ[0], categ[1]
                    cat_groups.setdefault(cat_id, {'name': cat_name, 'tmpl_ids': []})['tmpl_ids'].append(tid)

                for cat_id, info in cat_groups.items():
                    cat_tmpl_ids = info['tmpl_ids']
                    # Un produit sans coût réel (standard_price non renseigné)
                    # est exclu du calcul plutôt que de lui fabriquer un coût
                    # arbitraire (200 MAD) — ça faussait le GMROI de toute la
                    # catégorie pour n'importe quel produit avec un coût manquant.
                    cat_tmpl_ids_priced = [
                        tid for tid in cat_tmpl_ids
                        if (tmpl_by_id.get(tid, {}).get('standard_price') or 0.0) > 0
                    ]
                    cat_stock_cost = sum(
                        stock_by_tmpl.get(tid, 0) * tmpl_by_id.get(tid, {}).get('standard_price', 0.0)
                        for tid in cat_tmpl_ids_priced
                    )
                    cat_margin = 0.0
                    for tid in cat_tmpl_ids_priced:
                        sale = sales_by_tmpl.get(tid)
                        if not sale:
                            continue
                        cost = (tmpl_by_id.get(tid, {}).get('standard_price') or 0.0) * sale.get('qty', 0)
                        cat_margin += (sale.get('ca', 0.0) - cost)

                    cat_gmroi = round(cat_margin / cat_stock_cost, 1) if cat_stock_cost > 0 else 0.0
                    if cat_stock_cost > 0 or cat_margin > 0:
                        gmroi_categorie.append({
                            'name': info['name'],
                            'gmroi': cat_gmroi,
                            'pct': min(100, int((cat_gmroi / 4.0) * 100)) if cat_gmroi > 0 else 10
                        })
            except Exception as e:
                _logger.warning(f"Error computing GMROI: {str(e)}")

            gmroi_categorie.sort(key=lambda x: x['gmroi'], reverse=True)
            gmroi_categorie = gmroi_categorie[:5]

            # Vendu avec coût = coût (standard_price) des unités effectivement
            # vendues -> permet une vraie marge brute = CA vendu HT - coût des
            # ventes (comparaison HT contre HT, cf. ca_ht_total plus haut).
            vendu_avec_cout_total = 0.0
            for tid, sale in sales_by_tmpl.items():
                cost = (tmpl_by_id.get(tid, {}).get('standard_price') or 0.0) * sale.get('qty', 0)
                vendu_avec_cout_total += cost
            marge_total = ca_ht_total - vendu_avec_cout_total

            if page == 'stock':
                top_products = sorted(product_stats, key=lambda a: a['stock'], reverse=True)[:top_limit]
                flop_products = sorted(product_stats, key=lambda a: a['stock'])[:flop_limit]
            elif page == 'commandes':
                top_products = sorted(product_stats, key=lambda a: a['qty_purchased'], reverse=True)[:top_limit]
                flop_products = sorted(product_stats, key=lambda a: a['qty_purchased'])[:flop_limit]
            else:
                top_products = sorted(product_stats, key=lambda a: a['ca'], reverse=True)[:top_limit]
                flops_avec_stock = sorted([p for p in product_stats if p['stock'] > 0], key=lambda a: a['ca'])
                if flops_avec_stock:
                    flop_products = flops_avec_stock[:flop_limit]
                else:
                    flop_products = sorted(product_stats, key=lambda a: a['ca'])[:flop_limit]

            # ✅ VALORISATION DU STOCK (Société & Par Magasin)
            # NB : contrairement à stock_total/stock_by_tmpl (KPIs "retail" —
            # dormant, rupture, couverture — qui excluent volontairement les
            # sociétés non-magasin comme MOD FOR LIFE), la valorisation garde
            # TOUTES les sociétés : c'est un inventaire financier global, pas
            # une analyse de stock en boutique, donc le stock grossiste doit
            # y apparaître. quant_domain_base (défini plus haut) est déjà la
            # bonne base : mêmes filtres localisation/sachet/société-ou-
            # magasin choisi que quant_domain, mais SANS l'exclusion
            # non-retail — utilisé tel quel, sans filtrage a posteriori.
            val_ht_total = 0.0
            val_cost_total = 0.0
            stock_val_by_store = []

            quant_val_grouped = request.env['stock.quant'].sudo().read_group(
                quant_domain_base,
                ['quantity:sum', 'product_id', 'company_id'],
                ['product_id', 'company_id'],
                lazy=False
            )

            val_pids = [g['product_id'][0] for g in quant_val_grouped if g.get('product_id')]
            if val_pids:
                val_prods = request.env['product.product'].sudo().search_read(
                    [('id', 'in', val_pids)],
                    ['id', 'list_price', 'standard_price']
                )
                val_prod_map = {p['id']: p for p in val_prods}

                val_by_company = {}
                for g in quant_val_grouped:
                    pid = g['product_id'][0] if g.get('product_id') else None
                    cid = g['company_id'][1] if g.get('company_id') else 'Société'
                    qty = g.get('quantity') or 0.0
                    if qty <= 0 or not pid or pid not in val_prod_map:
                        continue
                    p_data = val_prod_map[pid]
                    price_ht = p_data.get('list_price') or 0.0
                    cost_price = p_data.get('standard_price') or 0.0

                    v_ht = qty * price_ht
                    v_cost = qty * cost_price

                    val_ht_total += v_ht
                    val_cost_total += v_cost

                    if cid not in val_by_company:
                        val_by_company[cid] = {'ht': 0.0, 'cost': 0.0, 'qty': 0}
                    val_by_company[cid]['ht'] += v_ht
                    val_by_company[cid]['cost'] += v_cost
                    val_by_company[cid]['qty'] += int(qty)

                for comp_name, vdata in val_by_company.items():
                    stock_val_by_store.append({
                        'store_name': comp_name,
                        'valeur_ht': round(vdata['ht'], 2),
                        'valeur_cost': round(vdata['cost'], 2),
                        'qty': vdata['qty']
                    })

            # ✅ ALERTE RUPTURE SOUS 30 JOURS (Prévision de rupture)
            proches_rupture_30j = []
            for tid in relevant_tmpl_ids:
                stk = stock_by_tmpl.get(tid, 0)
                qs = sales_by_tmpl_velocity.get(tid, 0)
                daily_rate = qs / days_in_period if days_in_period > 0 else 0.0
                if stk > 0 and daily_rate > 0:
                    days_left = round(stk / daily_rate, 1)
                    if days_left <= 30:
                        t = tmpl_by_id.get(tid, {})
                        ref = (
                            t.get('base_pivot_reference')
                            or t.get('default_code')
                            or t.get('name')
                            or '—'
                        )
                        proches_rupture_30j.append({
                            'id': tid,
                            'name': t.get('name') or '—',
                            'ref': ref,
                            'stock': int(stk),
                            'qty_sold': int(qs),
                            'daily_rate': round(daily_rate, 2),
                            'days_left': days_left
                        })

            proches_rupture_30j.sort(key=lambda x: x['days_left'])

            magasin_by_tid_30j = self._resolve_magasin_batch(
                [p['id'] for p in proches_rupture_30j], shop_mappings, kw.get('shop_field')
            )
            for p in proches_rupture_30j:
                p['magasin'] = magasin_by_tid_30j.get(p['id'], 'Réseau')

            return {
                'ca_total': round(ca_total, 2),
                'ca_ht': round(ca_ht_total, 2),
                'ca_achat': round(ca_achat_total, 2),
                'ca_achat_externe': round(ca_achat_externe, 2),
                'ca_achat_interne': round(ca_achat_interne, 2),
                'ca_achat_by_company': ca_achat_by_company,
                'qty_purchased_externe': qty_purchased_externe,
                'qty_purchased_interne': qty_purchased_interne,
                'vendu_avec_cout': round(vendu_avec_cout_total, 2),
                'marge': round(marge_total, 2),
                'tickets': tickets,
                'references_count': references_count,
                'panier_moyen': round(panier_moyen, 2),
                'qty_sold': qty_sold_total,
                'qty_sold_normal': qty_sold_normal_total,
                'qty_sold_solde': qty_sold_solde_total,
                'qty_purchased': qty_purchased_total,
                'stock_total': stock_total,
                'valeur_stock_ht': round(val_ht_total, 2),
                'valeur_stock_cost': round(val_cost_total, 2),
                'stock_val_by_store': stock_val_by_store,
                'sell_through': sell_through,
                'ruptures_count': ruptures_count,
                'ruptures_list': ruptures_list,
                'top_products': [dict(p, rank=idx + 1) for idx, p in enumerate(top_products)],
                'flop_products': [dict(p, rank=idx + 1) for idx, p in enumerate(flop_products)],
                'abc_analysis': {
                    'A': abc['A'][:10],
                    'B': abc['B'][:10],
                    'C': abc['C'][:10],
                },
                'taux_rupture': taux_rupture,
                'total_active_skus': total_active_skus,
                'couverture_moy': couverture_moy,
                'stock_dormant_pct': stock_dormant_pct,
                'dormant_count': len(dormant_products),
                'dormant_list': sorted(dormant_products, key=lambda x: x['stock'], reverse=True)[:500],
                'precision_inventaire': precision_inventaire,
                'alertes_stock': alertes_stock,
                'rotation_collection': rotation_collection,
                'gmroi_categorie': gmroi_categorie,
                'proches_rupture_30j': proches_rupture_30j[:100],
            }

        except Exception as e:
            _logger.error(f"Erreur api_kpis: {str(e)}", exc_info=True)
            return {'error': str(e)}

    def _compute_kpis_modforlife(self, kw, mod_for_life):
        """Calcul dédié pour MOD FOR LIFE : pas de vente en caisse ni
        d'alertes rupture retail (ce n'est pas un magasin), donc pas la même
        forme que _compute_kpis. Trois axes : ce qu'elle achète chez de vrais
        fournisseurs (purchase.order), ce qu'elle "vend" au prix coûtant à
        chacune des 3 sociétés magasins (sale.order — PAS le POS, confirmé
        dans mv_base_pivot/models/mv_article_batch.py:action_generate_sale_orders),
        et son propre stock entrepôt (stock.quant)."""
        try:
            retail_companies = request.env['res.company'].sudo().search([
                ('id', '!=', mod_for_life.id),
                ('name', 'not in', ['PAIE']),
            ])
            retail_partner_ids = retail_companies.mapped('partner_id').ids

            # ── Achats fournisseurs : bons de commande de MOD FOR LIFE dont
            # le fournisseur n'est PAS une des sociétés magasins (donc un
            # vrai fournisseur externe, pas un flux inter-société).
            po_domain = [
                ('order_id.state', 'in', ['purchase', 'done']),
                ('order_id.company_id', '=', mod_for_life.id),
                ('partner_id', 'not in', retail_partner_ids),
            ]
            po_domain += self._sachet_exclude_domain('product_id.product_tmpl_id.collection_id')
            if kw.get('date_start'):
                po_domain.append(('order_id.date_order', '>=', kw['date_start'] + ' 00:00:00'))
            if kw.get('date_end'):
                po_domain.append(('order_id.date_order', '<=', kw['date_end'] + ' 23:59:59'))

            po_grouped = request.env['purchase.order.line'].sudo().read_group(
                po_domain, ['product_qty:sum', 'price_subtotal:sum'], [], lazy=False
            )
            qty_achats_fournisseurs = int(po_grouped[0].get('product_qty') or 0) if po_grouped else 0
            ca_achats_fournisseurs = round(po_grouped[0].get('price_subtotal') or 0.0, 2) if po_grouped else 0.0

            po_tickets_agg = request.env['purchase.order.line'].sudo().read_group(
                po_domain, ['order_id:count_distinct'], []
            )
            nb_commandes_fournisseurs = (po_tickets_agg[0].get('order_id') or 0) if po_tickets_agg else 0

            # ── Ventes vers les sociétés magasins : sale.order.line, pas
            # pos.order.line — order_partner_id est le champ stocké (related
            # sur order_id.partner_id), pas "partner_id" (qui n'existe pas
            # sur sale.order.line).
            so_domain = [
                ('order_id.state', 'in', ['sale', 'done']),
                ('order_id.company_id', '=', mod_for_life.id),
                ('order_partner_id', 'in', retail_partner_ids),
            ]
            so_domain += self._sachet_exclude_domain('product_id.product_tmpl_id.collection_id')
            if kw.get('date_start'):
                so_domain.append(('order_id.date_order', '>=', kw['date_start'] + ' 00:00:00'))
            if kw.get('date_end'):
                so_domain.append(('order_id.date_order', '<=', kw['date_end'] + ' 23:59:59'))

            so_grouped = request.env['sale.order.line'].sudo().read_group(
                so_domain,
                ['product_uom_qty:sum', 'price_subtotal:sum', 'order_partner_id'],
                ['order_partner_id'],
                lazy=False
            )
            partner_name_by_id = {p.id: p.name for p in retail_companies.mapped('partner_id')}
            ventes_par_societe = []
            qty_ventes_total = 0.0
            ca_ventes_total = 0.0
            for g in so_grouped:
                pid = g['order_partner_id'][0] if g.get('order_partner_id') else None
                qty = g.get('product_uom_qty') or 0.0
                ca = g.get('price_subtotal') or 0.0
                qty_ventes_total += qty
                ca_ventes_total += ca
                ventes_par_societe.append({
                    'societe': partner_name_by_id.get(pid) or (g['order_partner_id'][1] if g.get('order_partner_id') else '—'),
                    'qty': int(qty),
                    'ca': round(ca, 2),
                })
            ventes_par_societe.sort(key=lambda x: -x['ca'])

            # ── Stock entrepôt : même principe que le stock retail
            # (stock.quant, emplacements internes) mais SANS l'exclusion
            # NON_RETAIL_COMPANIES — ici MOD FOR LIFE EST la société
            # regardée, son stock est justement ce qu'on veut voir.
            quant_domain = [
                ('location_id.usage', '=', 'internal'),
                ('company_id', '=', mod_for_life.id),
            ]
            quant_domain += self._sachet_exclude_domain('product_id.product_tmpl_id.collection_id')
            quant_grouped = request.env['stock.quant'].sudo().read_group(
                quant_domain, ['quantity:sum'], [], lazy=False
            )
            stock_entrepot = int(sum(g.get('quantity') or 0 for g in quant_grouped)) if quant_grouped else 0

            return {
                'is_modforlife': True,
                'company_name': mod_for_life.name,
                'ca_achats_fournisseurs': ca_achats_fournisseurs,
                'qty_achats_fournisseurs': qty_achats_fournisseurs,
                'nb_commandes_fournisseurs': nb_commandes_fournisseurs,
                'ca_ventes_societes': round(ca_ventes_total, 2),
                'qty_ventes_societes': int(qty_ventes_total),
                'ventes_par_societe': ventes_par_societe,
                'stock_entrepot': stock_entrepot,
            }
        except Exception as e:
            _logger.error(f"Erreur _compute_kpis_modforlife: {str(e)}", exc_info=True)
            return {'error': str(e)}

    @http.route('/mavie/api/top-flop/export', type='http', auth='user', methods=['GET'], csrf=False)
    def api_top_flop_export(self, **kw):
        """Export CSV du Top ou Flop Produits, avec les mêmes filtres et la
        même limite (top_limit / flop_limit) qu'affichés à l'écran."""
        kind = kw.get('kind') or 'top'
        data = self._compute_kpis(kw)
        if not data or data.get('error'):
            message = data.get('error') if data else 'Erreur inconnue'
            return request.make_response(
                'Erreur : ' + message,
                headers=[('Content-Type', 'text/plain; charset=utf-8')],
                status=404,
            )

        rows = data.get('flop_products') if kind == 'flop' else data.get('top_products')
        # _compute_kpis renvoie toujours jusqu'à 100 lignes (voir commentaire
        # sur top_limit/flop_limit) ; on tranche ici à la valeur réellement
        # demandée/affichée à l'écran au moment de l'export.
        try:
            requested_limit = max(1, int(kw.get('flop_limit' if kind == 'flop' else 'top_limit', 10)))
        except (ValueError, TypeError):
            requested_limit = 10
        rows = (rows or [])[:requested_limit]

        buffer = io.StringIO()
        buffer.write('﻿')  # BOM pour qu'Excel détecte l'UTF-8
        writer = csv.writer(buffer, delimiter=';')
        writer.writerow(['Top Produits' if kind != 'flop' else 'Flop Produits'])
        writer.writerow(['#', 'Produit', 'Réf', 'Qté vendue', 'CA Potentiel', 'Stock', 'Qté achetée'])
        for row in rows or []:
            writer.writerow([
                row.get('rank'), row.get('name'), row.get('ref'),
                row.get('qty_sold', row.get('qty', 0)), row.get('ca', 0),
                row.get('stock', 0), row.get('qty_purchased', 0),
            ])

        filename = 'mavie_export_{}.csv'.format('flop_produits' if kind == 'flop' else 'top_produits')
        return request.make_response(
            buffer.getvalue(),
            headers=[
                ('Content-Type', 'text/csv; charset=utf-8'),
                ('Content-Disposition', f'attachment; filename="{filename}"'),
            ],
        )

    # ─────────────────────────────────────────────────────────────
    # VENTES PAR ARRIVAGE
    # ─────────────────────────────────────────────────────────────

    @http.route('/mavie/api/sales-daily', type='json', auth='user', methods=['POST'], csrf=False)
    def api_sales_daily(self, **kw):
        try:
            is_filtered = bool(kw.get('collection_id') or kw.get('batch_id') or kw.get('categ_id'))

            product_tmpl_ids = None
            if is_filtered:
                domain = self._build_product_domain(kw)
                products = request.env['product.template'].sudo().search(domain)
                if not products:
                    return {'daily': []}
                product_tmpl_ids = products.ids

            pos_domain = self._build_pos_domain(kw, product_tmpl_ids)
            pos_grouped = request.env['pos.order.line'].sudo().read_group(
                pos_domain,
                ['price_subtotal_incl:sum', 'qty:sum', 'product_id'],
                ['product_id'],
                lazy=False
            )

            if not pos_grouped:
                return {'daily': []}

            pids = [g['product_id'][0] for g in pos_grouped if g.get('product_id')]
            sales_by_arrivage = {}
            prod_to_tmpl = {}
            tmpl_to_arrivage = {}
            if pids:
                prods = request.env['product.product'].sudo().search_read(
                    [('id', 'in', pids)],
                    ['id', 'product_tmpl_id']
                )
                prod_to_tmpl = {p['id']: p['product_tmpl_id'][0] for p in prods if p.get('product_tmpl_id')}
                tmpl_ids = list(set(prod_to_tmpl.values()))
                
                tmpls = request.env['product.template'].sudo().search_read(
                    [('id', 'in', tmpl_ids)],
                    ['id', 'arrivage_id']
                )
                tmpl_to_arrivage = {t['id']: (t['arrivage_id'][0], t['arrivage_id'][1]) for t in tmpls if t.get('arrivage_id')}

                for g in pos_grouped:
                    pid = g['product_id'][0] if g.get('product_id') else None
                    tid = prod_to_tmpl.get(pid)
                    if not tid:
                        continue
                    arr = tmpl_to_arrivage.get(tid)
                    if not arr:
                        continue
                    key = arr
                    if key not in sales_by_arrivage:
                        sales_by_arrivage[key] = {'ca': 0.0, 'qty': 0}
                    sales_by_arrivage[key]['ca'] += g.get('price_subtotal_incl') or 0.0
                    sales_by_arrivage[key]['qty'] += int(g.get('qty') or 0)

            daily_sales = []
            for (arrivage_id, arrivage_name), stats in sales_by_arrivage.items():
                daily_sales.append({
                    'date': arrivage_name,
                    'label': arrivage_name,
                    'ca': round(stats['ca'], 2),
                    'qty': int(stats['qty']),
                    'articles': int(stats['qty']),
                })

            daily_sales.sort(key=lambda x: x['ca'], reverse=True)

            # ── CA par Arrivage × Magasin ──
            # read_group ne peut pas grouper pos.order.line directement par
            # "magasin" (ce n'est pas un champ, c'est dérivé de
            # order_id.session_id.config_id / order_id.company_id) : on
            # regroupe donc par (product_id, order_id) — un 2e read_group,
            # toujours pas de scan ligne par ligne — puis on résout order_id
            # -> libellé magasin en 1-2 requêtes groupées (même règle que
            # sales_by_variant dans _compute_product_detail : préférer
            # session_id.config_id.name, repli sur company_id.name).
            by_shop_map = {}
            if pos_grouped:
                pos_grouped_by_order = request.env['pos.order.line'].sudo().read_group(
                    pos_domain,
                    ['price_subtotal_incl:sum', 'qty:sum', 'product_id', 'order_id'],
                    ['product_id', 'order_id'],
                    lazy=False
                )
                order_ids = list({g['order_id'][0] for g in pos_grouped_by_order if g.get('order_id')})
                orders = request.env['pos.order'].sudo().search_read(
                    [('id', 'in', order_ids)], ['id', 'company_id', 'session_id']
                )
                session_ids = list({o['session_id'][0] for o in orders if o.get('session_id')})
                sessions = request.env['pos.session'].sudo().search_read(
                    [('id', 'in', session_ids)], ['id', 'config_id']
                ) if session_ids else []
                session_to_config_name = {
                    s['id']: s['config_id'][1] for s in sessions if s.get('config_id')
                }
                order_to_shop = {}
                for o in orders:
                    shop_name = (o.get('company_id') and o['company_id'][1]) or 'Inconnu'
                    sid = o.get('session_id') and o['session_id'][0]
                    if sid and session_to_config_name.get(sid):
                        shop_name = session_to_config_name[sid]
                    order_to_shop[o['id']] = shop_name

                for g in pos_grouped_by_order:
                    pid = g['product_id'][0] if g.get('product_id') else None
                    oid = g['order_id'][0] if g.get('order_id') else None
                    tid = prod_to_tmpl.get(pid)
                    if not tid or not oid:
                        continue
                    arr = tmpl_to_arrivage.get(tid)
                    if not arr:
                        continue
                    arrivage_name = arr[1]
                    shop_name = order_to_shop.get(oid, 'Inconnu')
                    key = (arrivage_name, shop_name)
                    if key not in by_shop_map:
                        by_shop_map[key] = {'ca': 0.0, 'qty': 0}
                    by_shop_map[key]['ca'] += g.get('price_subtotal_incl') or 0.0
                    by_shop_map[key]['qty'] += int(g.get('qty') or 0)

            by_shop_grouped = {}
            for (arrivage_name, shop_name), stats in by_shop_map.items():
                by_shop_grouped.setdefault(arrivage_name, []).append({
                    'shop': shop_name,
                    'ca': round(stats['ca'], 2),
                    'qty': int(stats['qty']),
                })
            by_shop = [
                {'arrivage': arrivage_name, 'shops': sorted(shops, key=lambda s: s['ca'], reverse=True)}
                for arrivage_name, shops in by_shop_grouped.items()
            ]
            by_shop.sort(key=lambda a: sum(s['ca'] for s in a['shops']), reverse=True)

            return {'daily': daily_sales, 'by_shop': by_shop}
        except Exception as e:
            _logger.error(f"Erreur api_sales_daily: {str(e)}", exc_info=True)
            return {'error': str(e), 'daily': [], 'by_shop': []}

    # ─────────────────────────────────────────────────────────────
    # RECHERCHE
    # ─────────────────────────────────────────────────────────────

    @http.route('/mavie/api/search-products', type='json', auth='user', methods=['POST'], csrf=False)
    def api_search_products(self, **kw):
        try:
            query = (kw.get('query') or '').strip()
            if len(query) < 2:
                return {'results': []}

            ProductTemplate = request.env['product.template'].sudo()

            # Recherche native — base_pivot_reference est un champ stocké
            # simple sur product.template (une référence déjà écrite), donc
            # pas une requête live vers Base Pivot.
            products = ProductTemplate.search([
                '|', '|', '|',
                ('name', 'ilike', query),
                ('default_code', 'ilike', query),
                ('base_pivot_reference', 'ilike', query),
                ('categ_id.name', 'ilike', query),
            ], limit=30)

            results = [{
                'id': p.id,
                'name': p.name or '—',
                'ref': p.base_pivot_reference or p.default_code or '—',
            } for p in products[:20]]

            return {'results': results}
        except Exception as e:
            _logger.error(f"Erreur api_search_products: {str(e)}")
            return {'error': str(e), 'results': []}

    # ─────────────────────────────────────────────────────────────
    # DÉTAIL PRODUIT
    # ─────────────────────────────────────────────────────────────

    @http.route('/mavie/api/product-detail', type='json', auth='user', methods=['POST'], csrf=False)
    def api_product_detail(self, **kw):
        return self._compute_product_detail(kw)

    def _compute_product_detail(self, kw):
        try:
            article_id = kw.get('article_id')
            product_name = kw.get('product_name')

            ProductTemplate = request.env['product.template'].sudo()

            if article_id:
                product_tmpl = ProductTemplate.browse(int(article_id))
            elif product_name:
                product_tmpl = ProductTemplate.search([('name', 'ilike', product_name)], limit=1)
            else:
                return {'error': 'ID ou nom manquant'}

            if not product_tmpl or not product_tmpl.exists():
                return {'error': 'Produit non trouvé'}

            pos_domain = self._build_pos_domain(kw, [product_tmpl.id])
            pos_lines = request.env['pos.order.line'].sudo().search(pos_domain)

            qty_sold = int(sum(pos_lines.mapped('qty'))) if pos_lines else 0
            ca = sum(pos_lines.mapped('price_subtotal_incl')) if pos_lines else 0.0
            # CA HT — comparé au coût (lui aussi HT) pour calculer une marge
            # juste, sans le biais de la TVA incluse dans price_subtotal_incl.
            ca_ht = sum(pos_lines.mapped('price_subtotal')) if pos_lines else 0.0

            # Qté vendue "en solde" = lignes vendues à un prix effectif
            # (remise incluse) inférieur au prix catalogue (list_price) —
            # détection par prix, pas par date.
            list_price_ref = product_tmpl.list_price or 0.0
            qty_sold_solde = 0
            for line in pos_lines:
                effective_unit_price = line.price_unit * (1 - (line.discount or 0.0) / 100.0)
                if list_price_ref > 0 and effective_unit_price < list_price_ref * 0.999:
                    qty_sold_solde += line.qty
            qty_sold_solde = int(qty_sold_solde)
            qty_sold_normal = qty_sold - qty_sold_solde

            purchase_domain = self._build_purchase_domain(kw, [product_tmpl.id])
            po_lines = request.env['purchase.order.line'].sudo().search(purchase_domain)
            qty_purchased = int(sum(po_lines.mapped('product_qty'))) if po_lines else 0
            # CA Achat = coût réel des achats (HT, tel que facturé), pas une
            # reconstruction qty*prix_standard.
            ca_achat = sum(po_lines.mapped('price_subtotal')) if po_lines else 0.0

            # Achat externe (vrai fournisseur) vs achat interne (depuis
            # MOD FOR LIFE, au prix coûtant) — même logique que _compute_kpis.
            mod_for_life_partner_id = self._get_mod_for_life_partner_id()
            po_lines_interne = (
                po_lines.filtered(lambda l: l.partner_id.id == mod_for_life_partner_id)
                if mod_for_life_partner_id else po_lines.browse()
            )
            qty_purchased_interne = int(sum(po_lines_interne.mapped('product_qty'))) if po_lines_interne else 0
            ca_achat_interne = sum(po_lines_interne.mapped('price_subtotal')) if po_lines_interne else 0.0
            qty_purchased_externe = qty_purchased - qty_purchased_interne
            ca_achat_externe = ca_achat - ca_achat_interne

            # Marge (%) = (prix de vente - prix d'achat moyen réel) / prix de
            # vente. Le prix d'achat moyen vient des mêmes commandes
            # fournisseur que CA Achat/Qté Achetée ci-dessus (pas de nouvelle
            # source) — None (pas 0) quand la donnée n'existe pas, pour ne
            # pas laisser croire à une marge nulle.
            pv_ttc_ref = product_tmpl.list_price or 0.0
            if qty_purchased > 0 and ca_achat > 0 and pv_ttc_ref > 0:
                prix_achat_moyen = ca_achat / qty_purchased
                margin = round((pv_ttc_ref - prix_achat_moyen) / pv_ttc_ref * 100, 1)
            else:
                margin = None

            # Sell-through = part du stock reçu qui a été vendue : vendu / acheté.
            sell_through = round((qty_sold / qty_purchased * 100), 1) if qty_purchased > 0 else 0.0

            # Vendu avec coût = coût des unités vendues, pour calculer la
            # marge brute réelle (HT contre HT).
            vendu_avec_cout = qty_sold * (product_tmpl.standard_price or 0.0)
            marge = ca_ht - vendu_avec_cout

            sales_by_variant = {}
            for line in pos_lines:
                pid = line.product_id.id
                if pid not in sales_by_variant:
                    sales_by_variant[pid] = {'qty': 0, 'ca': 0, 'shops': {}}
                sales_by_variant[pid]['qty'] += line.qty
                sales_by_variant[pid]['ca'] += line.price_subtotal_incl

                shop_name = line.order_id.company_id.name or 'Inconnu'
                if line.order_id.session_id and line.order_id.session_id.config_id:
                    shop_name = line.order_id.session_id.config_id.name
                sales_by_variant[pid]['shops'][shop_name] = (
                    sales_by_variant[pid]['shops'].get(shop_name, 0) + line.qty
                )

            product_variants = request.env['product.product'].sudo().search(
                [('product_tmpl_id', '=', product_tmpl.id)]
            )

            # Périmètre "stock retail réel" — même logique que stock_by_store
            # plus bas (entrepôts avec un mapping magasin actif, hors
            # sociétés non-retail) : réutilisé pour que le stock réel par
            # couleur (best_variants) somme exactement au total affiché en
            # haut de fiche (stock_total), sans quoi les deux ne
            # correspondaient jamais (écart = stock dormant chez MOD FOR LIFE,
            # ou dans un entrepôt de la société retail non rattaché à un
            # magasin mappé).
            retail_lot_stock_ids = request.env['stock.warehouse'].sudo().search([
                ('company_id', 'not in', self._get_non_retail_company_ids()),
                ('id', 'in', self._get_active_shop_mappings().mapped('warehouse_id').ids),
            ]).mapped('lot_stock_id').ids

            # "Qté Magasin" doit être le STOCK ACTUEL de la variante dans le
            # magasin filtré — pas les ventes (voir sales_by_variant plus
            # haut, qui reste dédié à la répartition des ventes). Une seule
            # requête groupée, pas de N+1 par variante ; calculée uniquement
            # si un magasin est filtré (kw['shop_field']).
            stock_shop_by_variant = {}
            if kw.get('shop_field') and product_variants:
                shop_mapping_for_stock = request.env['mv.batch.shop.mapping'].sudo().search(
                    [('shop_field', '=', kw['shop_field'])], limit=1
                )
                if shop_mapping_for_stock and shop_mapping_for_stock.warehouse_id and shop_mapping_for_stock.warehouse_id.lot_stock_id:
                    shop_quants_grouped = request.env['stock.quant'].sudo().read_group(
                        [
                            ('product_id', 'in', product_variants.ids),
                            ('location_id', 'child_of', shop_mapping_for_stock.warehouse_id.lot_stock_id.id),
                        ],
                        ['quantity:sum'],
                        ['product_id'],
                        lazy=False,
                    )
                    for g in shop_quants_grouped:
                        pid = g.get('product_id') and g['product_id'][0]
                        if pid:
                            stock_shop_by_variant[pid] = g.get('quantity', 0.0) or 0.0

            shop_mappings = self._get_active_shop_mappings()
            shop_fields_cleaned = [
                (sm.shop_field, sm.warehouse_id.name if sm.warehouse_id else (sm.shop_label or sm.shop_field))
                for sm in shop_mappings if sm.shop_field
            ]

            # Total pièces reçues = commandes fournisseur confirmées, qui
            # existent pour la quasi-totalité des références (déjà utilisées
            # pour "Qté achetée") et ciblent la variante EXACTE
            # (product_id = couleur+taille précise) — vérifié en base :
            # reconciliation quasi parfaite par taille (41 variantes/42 avec
            # un "reste" positif sur un échantillon réel).
            purchased_by_variant = {}
            for pol in po_lines:
                purchased_by_variant[pol.product_id.id] = purchased_by_variant.get(pol.product_id.id, 0) + pol.product_qty

            best_variants = []
            for v in product_variants:
                stat = sales_by_variant.get(v.id, {'qty': 0, 'ca': 0, 'shops': {}})

                color_name, size_name = resolve_variant_color_size(v)
                color_name = (color_name or '—').upper().strip() if color_name else '—'

                dispatched_achats = purchased_by_variant.get(v.id, 0)
                if dispatched_achats > 0:
                    dispatched = int(round(dispatched_achats))
                    dispatch_source = 'achats'
                else:
                    dispatched = 0
                    dispatch_source = None

                # Même périmètre exact que stock_total du haut de fiche
                # (retail_lot_stock_ids, calculé plus haut) — sinon la somme
                # des stocks par couleur ne correspond jamais au total
                # affiché en haut.
                quants_v = request.env['stock.quant'].sudo().search([
                    ('product_id', '=', v.id),
                    ('location_id', 'child_of', retail_lot_stock_ids),
                ])
                var_stock = int(sum(quants_v.mapped('quantity'))) if quants_v else 0

                # Pas de fallback silencieux "dispatched = var_stock" : si
                # aucune commande fournisseur n'a de donnée pour cette
                # variante, on le signale explicitement (dispatch_missing)
                # plutôt que de fabriquer un "total pièces" à partir du stock
                # actuel, qui masquait le vrai problème de données.
                dispatch_missing = dispatched == 0 and (var_stock > 0 or stat['qty'] > 0)

                # Nom court : juste la couleur (+ taille si présente), sans le
                # préfixe "[réf] nom produit" de display_name — la référence
                # est déjà affichée en en-tête de la fiche produit.
                variant_label = color_name if color_name != '—' else 'Standard'
                if size_name:
                    variant_label += ', ' + size_name

                best_variants.append({
                    'name': variant_label,
                    'color': color_name,
                    'qty': int(stat['qty']),
                    'ca': round(stat['ca'], 2),
                    'dispatched': dispatched,
                    'dispatch_source': dispatch_source,
                    'dispatch_missing': dispatch_missing,
                    'stock': var_stock,
                    'stock_shop': int(stock_shop_by_variant.get(v.id, 0)) if kw.get('shop_field') else None,
                    'shops': stat['shops']
                })

            # "Total Pièces"/"Reste" ne sont indisponibles que si aucune
            # commande fournisseur confirmée n'a de donnée pour AUCUNE
            # variante de ce produit (cas rare — produit jamais réceptionné
            # via achat confirmé). Nom de clé conservé (has_base_pivot_data)
            # pour ne pas casser le front qui la lit déjà.
            has_base_pivot_data = bool(purchased_by_variant)

            # Panneau "Vérification des données" — permet à l'utilisateur de
            # comparer lui-même, pour n'importe quelle référence tapée dans
            # la barre de recherche, ce que le dashboard affiche avec la
            # source brute (Achats). Réutilise EXACTEMENT les mêmes dicts que
            # le calcul de best_variants ci-dessus — ne peut donc pas diverger
            # de ce qui est réellement affiché à l'écran.
            purchased_by_color_verif = {}
            for pol in po_lines:
                c_v, _s_v = resolve_variant_color_size(pol.product_id)
                c_v = (c_v or '—').upper().strip()
                purchased_by_color_verif[c_v] = purchased_by_color_verif.get(c_v, 0) + pol.product_qty

            verif_by_color = []
            for c_v in sorted(purchased_by_color_verif.keys()):
                ach_val = purchased_by_color_verif.get(c_v, 0)
                verif_by_color.append({
                    'color': c_v,
                    'achats': int(round(ach_val)),
                    'dashboard': int(round(ach_val)) if ach_val > 0 else None,
                    'source': 'achats' if ach_val > 0 else None,
                })

            # BUG CORRIGÉ (vérifié en base) : le [:10] coupait arbitrairement
            # la liste dès que plus de 10 variantes existaient (un article
            # chaussures peut avoir 12 à 60 variantes couleur×pointure) —
            # quand aucune n'a encore de vente (ca=0 partout), le tri par
            # "ca" ne les départage pas et l'ordre retenu pour les 10
            # premières est arbitraire : des variantes ayant pourtant un
            # vrai achat/dispatch enregistré pouvaient être coupées au
            # profit de variantes totalement vides. On trie maintenant par
            # CA, puis par pièces reçues (dispatché/acheté), puis par qté
            # vendue, et on n'affiche PLUS qu'un nombre limité que si le
            # reste n'a vraiment aucune donnée (pour ne pas noyer l'écran
            # de dizaines de lignes à 0 sur les très gros articles).
            best_variants = sorted(
                best_variants,
                key=lambda x: (x['ca'], x.get('dispatched', 0), x['qty']),
                reverse=True
            )
            has_signal = [v for v in best_variants if v['qty'] > 0 or v.get('dispatched', 0) > 0 or v['stock'] > 0]
            no_signal = [v for v in best_variants if v['qty'] == 0 and v.get('dispatched', 0) == 0 and v['stock'] == 0]
            best_variants = has_signal + no_signal[:max(0, 20 - len(has_signal))]
            for idx, v in enumerate(best_variants):
                v['rank'] = idx + 1
                qty_sold_v = v.get('qty', 0)
                stock_v = v.get('stock', 0)
                dispatched_v = v.get('dispatched', 0)

                # Ni Base Pivot ni les achats n'ont de donnée pour CETTE
                # couleur précise (dispatch_missing) : pas de "0 pièce"
                # trompeur, on affiche clairement l'absence de donnée.
                if v.get('dispatch_missing'):
                    v['total_pieces'] = None
                    v['reste'] = None
                    v['discordance'] = False
                    v['discordance_detail'] = None
                    continue

                # Total pièces = valeur dispatchée à l'arrivage (convertie en
                # pièces réelles), figée une bonne fois pour toutes — JAMAIS
                # recalculée à partir du stock actuel/des ventes. Un écart
                # entre "dispatché" et "stock + vendu" est un vrai problème de
                # données (mauvais dispatch, restock non tracé...) qui doit
                # être visible, pas masqué en gonflant artificiellement ce nombre.
                v['total_pieces'] = dispatched_v
                v['reste'] = v['total_pieces'] - qty_sold_v
                v['discordance'] = abs(dispatched_v - (stock_v + qty_sold_v)) > 0.01
                v['discordance_detail'] = (
                    f"Dispatché: {dispatched_v}, Stock+Vendu: {stock_v + qty_sold_v}"
                    if v['discordance'] else None
                )

            stock_by_store = []
            non_retail_company_ids = self._get_non_retail_company_ids()
            try:
                # Ne lister que les entrepôts qui correspondent à un magasin
                # réellement configuré/actif (mv.batch.shop.mapping) — sinon
                # cette liste, construite indépendamment sur stock.warehouse,
                # affichait aussi des entrepôts désactivés/non-magasins
                # (ex: "DIGITAL SHOP") même après avoir désactivé leur
                # mapping, puisqu'elle ne passait pas par lui.
                mapped_warehouse_ids = self._get_active_shop_mappings().mapped('warehouse_id').ids
                warehouses = request.env['stock.warehouse'].sudo().search([
                    ('company_id', 'not in', non_retail_company_ids),
                    ('id', 'in', mapped_warehouse_ids),
                ])
                for wh in warehouses:
                    if not wh.lot_stock_id:
                        continue
                    quants = request.env['stock.quant'].sudo().search([
                        ('product_id', 'in', product_variants.ids),
                        ('location_id', 'child_of', wh.lot_stock_id.id),
                    ])
                    stock_qty = sum(quants.mapped('quantity')) if quants else 0
                    reserved_qty = sum(quants.mapped('reserved_quantity')) if quants else 0

                    stock_by_store.append({
                        'store_name': wh.name,
                        'company_id': wh.company_id.id,
                        'stock': int(stock_qty),
                        'reserved': int(reserved_qty),
                        'available': int(stock_qty - reserved_qty),
                    })
            except Exception as e:
                _logger.warning(f"Erreur stock réel: {str(e)}")

            stock_total = 0
            if kw.get('shop_field'):
                mapping = request.env['mv.batch.shop.mapping'].sudo().search([
                    ('shop_field', '=', kw['shop_field'])
                ], limit=1)
                if mapping and mapping.warehouse_id:
                    target_wh = mapping.warehouse_id.name
                    stock_total = sum(s['stock'] for s in stock_by_store if s['store_name'] == target_wh)
                else:
                    stock_total = sum(s['stock'] for s in stock_by_store)
            else:
                # Pas de magasin précis choisi : on retombe sur la/les
                # société(s) cochée(s) dans le sélecteur standard Odoo.
                context_company_ids = self._get_context_company_ids()
                if context_company_ids:
                    stock_total = sum(
                        s['stock'] for s in stock_by_store if s['company_id'] in context_company_ids
                    )
                else:
                    stock_total = sum(s['stock'] for s in stock_by_store)

            # Qté Dispatché par magasin — deux sources, dans cet ordre de
            # priorité :
            #  1) Base Pivot (colonnes par magasin sur les lignes couleur) —
            #     ne couvre que 53 références sur ~4972.
            #  2) Repli : commandes fournisseur confirmées. Chaque commande
            #     est rattachée à un point de livraison
            #     (picking_type_id.warehouse_id) qui EST le magasin
            #     destinataire — vérifié en base sur des références réelles :
            #     le total par magasin correspond exactement à Qté Achetée,
            #     et cette donnée existe pour la quasi-totalité du
            #     catalogue (contrairement à Base Pivot). Recherche
            #     volontairement SANS restriction société/magasin/date (même
            #     principe que le dispatch Base Pivot : un fait historique
            #     figé, pas scopé au filtre actif) pour avoir la répartition
            #     complète sur tout le réseau.
            po_lines_all_shops = request.env['purchase.order.line'].sudo().search([
                ('order_id.state', 'in', ['purchase', 'done']),
                ('product_id.product_tmpl_id', '=', product_tmpl.id),
            ] + self._sachet_exclude_domain('product_id.product_tmpl_id.collection_id'))
            purchased_by_warehouse = {}
            for pol in po_lines_all_shops:
                wh = pol.order_id.picking_type_id.warehouse_id
                if not wh:
                    continue
                purchased_by_warehouse[wh.id] = purchased_by_warehouse.get(wh.id, 0) + pol.product_qty

            shop_mappings_by_field = {sm.shop_field: sm for sm in shop_mappings if sm.shop_field}

            stock_by_store_pivot = []
            for field, label in shop_fields_cleaned:
                mapping = shop_mappings_by_field.get(field)
                wh_id = mapping.warehouse_id.id if mapping and mapping.warehouse_id else None
                achats_qty = purchased_by_warehouse.get(wh_id, 0) if wh_id else 0
                if achats_qty > 0:
                    qty, dispatch_src = int(round(achats_qty)), 'achats'
                else:
                    qty, dispatch_src = None, None
                stock_by_store_pivot.append({
                    'field': field,
                    'name': label,
                    'qty': qty,
                    'dispatch_source': dispatch_src,
                })

            # Fusion avec le stock réel (stock.quant) par magasin — le label
            # du dispatch est déjà basé sur warehouse_id.name donc
            # correspond au store_name réel.
            stock_by_name = {s['store_name']: s['stock'] for s in stock_by_store}
            for row in stock_by_store_pivot:
                row['stock'] = stock_by_name.get(row['name'], 0)

            # Suite du panneau "Vérification des données" : le détail par
            # magasin, avec le même dict que stock_by_store_pivot ci-dessus
            # (purchased_by_warehouse).
            verif_by_magasin = []
            for field, label in shop_fields_cleaned:
                mapping = shop_mappings_by_field.get(field)
                wh_id = mapping.warehouse_id.id if mapping and mapping.warehouse_id else None
                ach_val = purchased_by_warehouse.get(wh_id, 0) if wh_id else 0
                verif_by_magasin.append({
                    'magasin': label,
                    'achats': int(round(ach_val)),
                    'dashboard': int(round(ach_val)) if ach_val > 0 else None,
                    'source': 'achats' if ach_val > 0 else None,
                })

            # ✅ IMAGE LAZY LOADING — chargée uniquement au clic sur le produit
            # (pas au chargement initial). Toujours l'image native du produit
            # (plus de repli mv.article.base).
            image_url = f'/web/image/product.template/{product_tmpl.id}/image_512'

            return {
                'id': product_tmpl.id,
                'name': product_tmpl.name,
                'ref': product_tmpl.base_pivot_reference or product_tmpl.default_code or '—',
                'family': product_tmpl.categ_id.name if product_tmpl.categ_id else '—',
                'qty_sold': qty_sold,
                'qty_purchased': qty_purchased,
                'stock_total': stock_total,
                # Stock théorique = ce qu'il devrait rester si tout mouvement
                # de stock était correctement tracé dans Odoo (achats - ventes).
                # L'écart avec stock_total pointe des pertes/sorties non
                # tracées (stock négatif ailleurs, ventes hors POS, casse...).
                'stock_theorique': qty_purchased - qty_sold,
                'stock_ecart': (qty_purchased - qty_sold) - stock_total,
                'ca': ca,
                'ca_ht': round(ca_ht, 2),
                'ca_achat': round(ca_achat, 2),
                'ca_achat_externe': round(ca_achat_externe, 2),
                'ca_achat_interne': round(ca_achat_interne, 2),
                'qty_purchased_externe': qty_purchased_externe,
                'qty_purchased_interne': qty_purchased_interne,
                'vendu_avec_cout': round(vendu_avec_cout, 2),
                'marge': round(marge, 2),
                'margin': margin,
                'sell_through': sell_through,
                'qty_sold_normal': qty_sold_normal,
                'qty_sold_solde': qty_sold_solde,
                'pv_ttc': product_tmpl.list_price or 0.0,
                'cost': product_tmpl.standard_price or 0.0,
                'collection_id': product_tmpl.collection_id.id if getattr(product_tmpl, 'collection_id', False) else None,
                'collection_name': product_tmpl.collection_id.name if getattr(product_tmpl, 'collection_id', False) else '—',
                'batch_id': product_tmpl.arrivage_id.id if getattr(product_tmpl, 'arrivage_id', False) else None,
                'batch_name': product_tmpl.arrivage_id.name if getattr(product_tmpl, 'arrivage_id', False) else '—',
                'best_variants': best_variants,
                'variants': best_variants,
                'has_base_pivot_data': has_base_pivot_data,
                'stock_by_store': stock_by_store_pivot,
                'real_stock_by_store': stock_by_store,
                'verification': {
                    'by_color': verif_by_color,
                    'by_magasin': verif_by_magasin,
                },
                'image_url': image_url,
            }
        except Exception as e:
            _logger.error(f"Erreur api_product_detail: {str(e)}", exc_info=True)
            return {'error': str(e)}

    @http.route('/mavie/api/product-detail/export', type='http', auth='user', methods=['GET'], csrf=False)
    def api_product_detail_export(self, **kw):
        """Export CSV de la fiche d'une seule référence : KPIs, variantes
        couleurs et stock par magasin, tels qu'affichés dans le panneau
        détail du dashboard."""
        data = self._compute_product_detail(kw)
        if not data or data.get('error'):
            message = data.get('error') if data else 'Produit introuvable'
            return request.make_response(
                'Erreur : ' + message,
                headers=[('Content-Type', 'text/plain; charset=utf-8')],
                status=404,
            )

        buffer = io.StringIO()
        buffer.write('﻿')  # BOM pour qu'Excel détecte l'UTF-8
        writer = csv.writer(buffer, delimiter=';')

        writer.writerow(['Fiche produit', data.get('name') or ''])
        writer.writerow(['Référence', data.get('ref') or ''])
        writer.writerow(['Famille', data.get('family') or ''])
        writer.writerow(['Collection', data.get('collection_name') or ''])
        writer.writerow([])

        writer.writerow(['KPI', 'Valeur'])
        writer.writerow(['Qté vendue', data.get('qty_sold', 0)])
        writer.writerow(['Qté achetée', data.get('qty_purchased', 0)])
        writer.writerow(['Stock réel Odoo', data.get('stock_total', 0)])
        writer.writerow(['Stock théorique (achetée - vendue)', data.get('stock_theorique', 0)])
        writer.writerow(['Écart stock (théorique - réel)', data.get('stock_ecart', 0)])
        writer.writerow(['CA Vendu (TTC)', data.get('ca', 0)])
        writer.writerow(['CA Achat', data.get('ca_achat', 0)])
        writer.writerow(['Sell-through (%)', data.get('sell_through', 0)])
        writer.writerow([])

        writer.writerow(['Variantes couleurs (meilleure vente en tête)'])
        writer.writerow(['#', 'Couleur', 'Total pièces', 'Qté vendue', 'Reste'])
        for idx, v in enumerate(data.get('variants') or [], start=1):
            writer.writerow([idx, v.get('name'), v.get('total_pieces', 0), v.get('qty', 0), v.get('reste', 0)])
        writer.writerow([])

        writer.writerow(['Stock par magasin'])
        writer.writerow(['Magasin', 'Qté dispatché', 'Stock'])
        for s in data.get('stock_by_store') or []:
            writer.writerow([s.get('name'), s.get('qty', 0), s.get('stock', 0)])

        ref_for_filename = re.sub(r'[^A-Za-z0-9_-]+', '_', data.get('ref') or str(data.get('id') or 'produit'))
        filename = f'mavie_export_{ref_for_filename}.csv'

        return request.make_response(
            buffer.getvalue(),
            headers=[
                ('Content-Type', 'text/csv; charset=utf-8'),
                ('Content-Disposition', f'attachment; filename="{filename}"'),
            ],
        )

    # ─────────────────────────────────────────────────────────────
    # EXTRACTION / TRANSFERT INTER-MAGASINS
    # ─────────────────────────────────────────────────────────────

    def _stock_by_mapping_for_template(self, product_tmpl_id, mappings, color=None):
        """Retourne {shop_field: qty disponible} pour ce template, par magasin
        (basé sur stock.quant réel dans mapping.warehouse_id.lot_stock_id).

        Si `color` est fourni, ne compte que les variantes de cette couleur —
        sans ce filtre, un magasin peut être suggéré comme source alors que
        tout son stock disponible est dans une AUTRE couleur que celle
        réellement recherchée pour la destination."""
        variants = request.env['product.product'].sudo().search(
            [('product_tmpl_id', '=', product_tmpl_id)]
        )
        if color:
            color = color.upper().strip()
            variants = variants.filtered(
                lambda v: (resolve_variant_color_size(v)[0] or '').upper().strip() == color
            )
        if not variants:
            return {}

        result = {}
        for m in mappings:
            if not m.warehouse_id or not m.warehouse_id.lot_stock_id:
                continue
            quants = request.env['stock.quant'].sudo().search([
                ('product_id', 'in', variants.ids),
                ('location_id', 'child_of', m.warehouse_id.lot_stock_id.id),
            ])
            result[m.shop_field] = sum(quants.mapped('quantity')) if quants else 0.0
        return result

    def _notify_transfer_responsible(self, transfer, source_mapping, dest_mapping):
        """Notifie (boîte de réception Odoo + email selon préférence utilisateur)
        le responsable du magasin source, avec référence/quantité/photo."""
        responsible = source_mapping.responsible_user_id
        if not responsible:
            label = source_mapping.shop_label or source_mapping.shop_field
            return f"Aucun responsable configuré pour le magasin {label} : notification non envoyée."

        lines_txt = []
        attachments = []
        for line in transfer.line_ids:
            lines_txt.append(
                f"<li>{line.product_id.display_name} — Réf : {line.reference or '—'} — Qté : {int(line.quantity)}</li>"
            )
            image = line.product_id.image_1920 or line.product_id.product_tmpl_id.image_1920
            if image and len(attachments) < 5:
                fname = f"{line.product_id.default_code or line.product_id.id}.png"
                try:
                    attachments.append((fname, base64.b64decode(image)))
                except Exception:
                    pass

        source_label = source_mapping.shop_label or source_mapping.shop_field
        dest_label = dest_mapping.shop_label or dest_mapping.shop_field
        body = (
            f"<p><strong>Transfert {transfer.name}</strong> à préparer : "
            f"<strong>{source_label}</strong> → <strong>{dest_label}</strong></p>"
            f"<ul>{''.join(lines_txt)}</ul>"
        )

        request.env['mail.thread'].sudo().message_notify(
            partner_ids=[responsible.partner_id.id],
            subject=f"Transfert {transfer.name} à préparer — {source_label}",
            body=body,
            model='inter.internal.transfer',
            res_id=transfer.id,
            attachments=attachments,
        )
        return None

    @http.route('/mavie/api/transfer-suggestions', type='json', auth='user', methods=['POST'], csrf=False)
    def api_transfer_suggestions(self, **kw):
        try:
            product_tmpl_id = kw.get('product_tmpl_id')
            dest_shop_field = kw.get('dest_shop_field')
            if not product_tmpl_id:
                return {'error': 'Produit manquant.', 'suggestions': []}

            product_tmpl_id = int(product_tmpl_id)
            color = (kw.get('color') or '').strip() or None
            mappings = self._get_active_shop_mappings()

            dest_mapping = mappings.filtered(lambda m: m.shop_field == dest_shop_field)[:1]
            dest_city = (dest_mapping.city or '').strip() if dest_mapping else ''
            nearby_cities = set(CITY_PROXIMITY.get(dest_city, [])) if dest_city else set()

            source_mappings = mappings.filtered(lambda m: m.shop_field != dest_shop_field)
            stock_by_field = self._stock_by_mapping_for_template(product_tmpl_id, source_mappings, color=color)

            suggestions = []
            for m in source_mappings:
                qty = stock_by_field.get(m.shop_field, 0.0)
                if qty <= 0:
                    continue
                city = (m.city or '').strip()
                if dest_city and city == dest_city:
                    tier = 'same_city'
                elif city and city in nearby_cities:
                    tier = 'nearby'
                else:
                    tier = 'other'
                suggestions.append({
                    'shop_field': m.shop_field,
                    'shop_label': m.warehouse_id.name if m.warehouse_id else (m.shop_label or m.shop_field),
                    'city': city or '—',
                    'available_qty': int(qty),
                    'tier': tier,
                    'has_responsible': bool(m.responsible_user_id),
                })

            tier_order = {'same_city': 0, 'nearby': 1, 'other': 2}
            suggestions.sort(key=lambda s: (tier_order.get(s['tier'], 3), -s['available_qty']))

            return {'suggestions': suggestions, 'dest_city': dest_city or None}
        except Exception as e:
            _logger.error(f"Erreur api_transfer_suggestions: {str(e)}", exc_info=True)
            return {'error': str(e), 'suggestions': []}

    @http.route('/mavie/api/transfer-variant-stock', type='json', auth='user', methods=['POST'], csrf=False)
    def api_transfer_variant_stock(self, **kw):
        """Détail par variante (couleur/taille) du stock disponible pour un
        produit dans UN magasin source précis — alimente la "matrice" affichée
        quand on clique sur un magasin suggéré, pour choisir les quantités
        ligne par ligne au lieu d'une quantité globale devinée automatiquement."""
        try:
            product_tmpl_id = kw.get('product_tmpl_id')
            source_shop_field = kw.get('source_shop_field')
            if not (product_tmpl_id and source_shop_field):
                return {'error': 'Produit ou magasin source manquant.', 'variants': []}

            product_tmpl_id = int(product_tmpl_id)
            color = (kw.get('color') or '').strip() or None
            mappings = self._get_active_shop_mappings()
            source_mapping = mappings.filtered(lambda m: m.shop_field == source_shop_field)[:1]
            if not source_mapping or not source_mapping.warehouse_id or not source_mapping.warehouse_id.lot_stock_id:
                return {'error': 'Magasin source non configuré (entrepôt manquant).', 'variants': []}

            source_location = source_mapping.warehouse_id.lot_stock_id
            variants = request.env['product.product'].sudo().search(
                [('product_tmpl_id', '=', product_tmpl_id)]
            )
            if color:
                color_upper = color.upper().strip()
                variants = variants.filtered(
                    lambda v: (resolve_variant_color_size(v)[0] or '').upper().strip() == color_upper
                )
            if not variants:
                return {'error': 'Aucune variante trouvée pour ce produit.', 'variants': []}

            Quant = request.env['stock.quant'].sudo().with_company(source_mapping.company_id)
            rows = []
            for v in variants:
                available = Quant._get_available_quantity(v, source_location)
                if available <= 0:
                    continue
                color_name, size_name = resolve_variant_color_size(v)
                rows.append({
                    'product_id': v.id,
                    'color': color_name or '—',
                    'size': size_name or '—',
                    'available_qty': int(available),
                })

            rows.sort(key=lambda r: (r['color'], r['size']))
            return {'variants': rows}
        except Exception as e:
            _logger.error(f"Erreur api_transfer_variant_stock: {str(e)}", exc_info=True)
            return {'error': str(e), 'variants': []}

    @http.route('/mavie/api/color-stock-by-store', type='json', auth='user', methods=['POST'], csrf=False)
    def api_color_stock_by_store(self, **kw):
        """Stock disponible d'UNE couleur d'un produit, détaillé par magasin
        (et par taille au sein de chaque magasin) — alimente le popup ouvert
        en cliquant une ligne du tableau "Variantes Couleurs"."""
        try:
            product_tmpl_id = kw.get('product_tmpl_id')
            color = (kw.get('color') or '').strip()
            if not (product_tmpl_id and color):
                return {'error': 'Produit ou couleur manquant.', 'stores': []}

            product_tmpl_id = int(product_tmpl_id)
            color_upper = color.upper().strip()

            variants = request.env['product.product'].sudo().search(
                [('product_tmpl_id', '=', product_tmpl_id)]
            )
            color_variants = variants.filtered(
                lambda v: (resolve_variant_color_size(v)[0] or '').upper().strip() == color_upper
            )
            if not color_variants:
                return {'error': 'Aucune variante trouvée pour cette couleur.', 'stores': []}

            size_by_variant = {}
            for v in color_variants:
                _c, size_name = resolve_variant_color_size(v)
                size_by_variant[v.id] = size_name or '—'

            mappings = self._get_active_shop_mappings()
            stores = []
            for m in mappings:
                if not m.warehouse_id or not m.warehouse_id.lot_stock_id:
                    continue
                quants = request.env['stock.quant'].sudo().search([
                    ('product_id', 'in', color_variants.ids),
                    ('location_id', 'child_of', m.warehouse_id.lot_stock_id.id),
                ])
                by_size = {}
                total = 0.0
                for q in quants:
                    total += q.quantity
                    size_name = size_by_variant.get(q.product_id.id, '—')
                    by_size[size_name] = by_size.get(size_name, 0.0) + q.quantity
                stores.append({
                    'shop_field': m.shop_field,
                    'shop_label': m.warehouse_id.name or m.shop_label or m.shop_field,
                    'city': m.city or '—',
                    'stock_total': int(round(total)),
                    'by_size': {k: int(round(v)) for k, v in by_size.items()},
                })

            # Plus gros stock en premier — facilite le choix d'un magasin
            # source pour un futur transfert de cette couleur.
            stores.sort(key=lambda s: -s['stock_total'])

            return {'color': color, 'stores': stores}
        except Exception as e:
            _logger.error(f"Erreur api_color_stock_by_store: {str(e)}", exc_info=True)
            return {'error': str(e), 'stores': []}

    @http.route('/mavie/api/transfer-create', type='json', auth='user', methods=['POST'], csrf=False)
    def api_transfer_create(self, **kw):
        try:
            product_tmpl_id = kw.get('product_tmpl_id')
            source_shop_field = kw.get('source_shop_field')
            dest_shop_field = kw.get('dest_shop_field')
            lines = kw.get('lines') or []

            if not (product_tmpl_id and source_shop_field and dest_shop_field and lines):
                return {'error': 'Paramètres manquants (produit, magasin source, magasin cible, lignes).'}

            product_tmpl_id = int(product_tmpl_id)

            mappings = self._get_active_shop_mappings()
            source_mapping = mappings.filtered(lambda m: m.shop_field == source_shop_field)[:1]
            dest_mapping = mappings.filtered(lambda m: m.shop_field == dest_shop_field)[:1]

            if not source_mapping or not source_mapping.warehouse_id or not source_mapping.warehouse_id.lot_stock_id:
                return {'error': 'Magasin source non configuré (entrepôt manquant).'}
            if not dest_mapping or not dest_mapping.warehouse_id or not dest_mapping.warehouse_id.lot_stock_id:
                return {'error': 'Magasin cible non configuré (entrepôt manquant).'}
            if not source_mapping.company_id or not dest_mapping.company_id:
                return {'error': 'Société non configurée pour un des deux magasins.'}
            if source_mapping.company_id.id == dest_mapping.company_id.id:
                return {'error': 'Le magasin source et le magasin cible appartiennent à la même société.'}

            source_location = source_mapping.warehouse_id.lot_stock_id
            Quant = request.env['stock.quant'].sudo().with_company(source_mapping.company_id)

            line_vals = []
            warning_parts = []
            for line in lines:
                try:
                    variant_id = int(line.get('product_id'))
                    requested = float(line.get('qty') or 0)
                except (TypeError, ValueError):
                    continue
                if requested <= 0:
                    continue
                variant = request.env['product.product'].sudo().browse(variant_id)
                if not variant.exists() or variant.product_tmpl_id.id != product_tmpl_id:
                    continue
                available = Quant._get_available_quantity(variant, source_location)
                take = min(requested, available)
                if take <= 0:
                    continue
                if take < requested:
                    warning_parts.append(f"{variant.display_name} : {take:.0f}/{requested:.0f}")
                line_vals.append((0, 0, {'product_id': variant_id, 'quantity': take}))

            if not line_vals:
                return {'error': 'Aucune quantité valide à transférer (stock insuffisant ou lignes vides).'}

            warning = ("Quantités réduites (stock insuffisant) : " + ", ".join(warning_parts)) if warning_parts else None

            # group_ref (optionnel) : posé côté client sur tous les bons créés
            # dans la même session de transfert pour la même référence +
            # destination (plusieurs magasins source nécessaires) — permet de
            # les regrouper à l'affichage (liste + PDF) sans changer le modèle
            # de données (toujours un enregistrement par paire source/cible).
            group_ref = (kw.get('group_ref') or '').strip() or None

            transfer = request.env['inter.internal.transfer'].sudo().create({
                'company_source_id': source_mapping.company_id.id,
                'location_source_id': source_location.id,
                'company_target_id': dest_mapping.company_id.id,
                'location_target_id': dest_mapping.warehouse_id.lot_stock_id.id,
                'line_ids': line_vals,
                'group_ref': group_ref,
            })
            transfer.sudo().action_submit()

            notif_warning = self._notify_transfer_responsible(transfer, source_mapping, dest_mapping)

            return {
                'transfer_id': transfer.id,
                'transfer_name': transfer.name,
                'group_ref': transfer.group_ref,
                'warning': warning,
                'notif_warning': notif_warning,
            }
        except UserError as e:
            return {'error': str(e)}
        except Exception as e:
            _logger.error(f"Erreur api_transfer_create: {str(e)}", exc_info=True)
            return {'error': str(e)}