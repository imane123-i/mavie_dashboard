# -*- coding: utf-8 -*-
from odoo import http, fields
from odoo.http import request, Response
import json
import re
from datetime import datetime, timedelta, date
import logging

_logger = logging.getLogger(__name__)


class MaVieDashboardController(http.Controller):
    """Dashboard analytique MaVie - données depuis tout le catalogue Odoo (product.template)"""

    @http.route('/mavie/dashboard', type='http', auth='user', methods=['GET'])
    def dashboard_page(self, **kwargs):
        try:
            return request.render('mavie_dashboard.dashboard_page')
        except Exception as e:
            _logger.error(f"Erreur dashboard_page: {str(e)}")
            return f"<h1>Erreur</h1><p>{str(e)}</p>"

    # ─────────────────────────────────────────────────────────────
    # DOMAINS
    # ─────────────────────────────────────────────────────────────

    def _build_product_domain(self, kw):
        domain = []

        if kw.get('collection_id') or kw.get('batch_id'):
            art_domain = []
            if kw.get('collection_id'):
                try:
                    art_domain.append(('collection_id', '=', int(kw['collection_id'])))
                except (ValueError, TypeError):
                    pass
            if kw.get('batch_id'):
                try:
                    art_domain.append(('arrivage_id', '=', int(kw['batch_id'])))
                except (ValueError, TypeError):
                    pass

            articles = request.env['mv.article.base'].sudo().search(art_domain)

            linked_tmpl_ids = []
            for a in articles:
                if a.product_tmpl_id and a.product_tmpl_id.id:
                    linked_tmpl_ids.append(a.product_tmpl_id.id)

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

            all_tmpl_ids = list(set(linked_tmpl_ids + direct_tmpl_ids))

            if all_tmpl_ids:
                domain.append(('id', 'in', all_tmpl_ids))
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

        domain.append(('name', 'not ilike', 'sachet'))
        domain.append(('name', 'not ilike', '2026'))

        return domain

    def _build_pos_domain(self, kw, product_tmpl_ids):
        domain = [
            ('order_id.state', 'in', ['paid', 'done', 'invoiced']),
            ('is_reward_line', '=', False),
            ('product_id.product_tmpl_id.name', 'not ilike', 'sachet'),
            ('product_id.product_tmpl_id.name', 'not ilike', '2026'),
        ]

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

        return domain

    def _build_purchase_domain(self, kw, product_tmpl_ids):
        domain = [
            ('order_id.state', '!=', 'cancel'),
            ('product_id.product_tmpl_id.name', 'not ilike', 'sachet'),
            ('product_id.product_tmpl_id.name', 'not ilike', '2026'),
        ]
        if product_tmpl_ids is not None:
            domain.append(('product_id.product_tmpl_id', 'in', product_tmpl_ids))

        if kw.get('date_start'):
            domain.append(('order_id.date_order', '>=', kw['date_start'] + ' 00:00:00'))
        if kw.get('date_end'):
            domain.append(('order_id.date_order', '<=', kw['date_end'] + ' 23:59:59'))
        return domain

    def _get_stock_quants(self, product_tmpl_ids=None, shop_field=None):
        """Get stock.quant records for internal locations, optionally filtered by templates and shop."""
        quant_domain = [
            ('location_id.usage', '=', 'internal'),
            ('product_id.product_tmpl_id.name', 'not ilike', 'sachet'),
            ('product_id.product_tmpl_id.name', 'not ilike', '2026'),
        ]
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
                excluded_names = {'all', 'expenses', 'saleable', 'pos', 'bons & fidélité'}
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
        try:
            is_filtered = bool(kw.get('collection_id') or kw.get('batch_id') or kw.get('categ_id'))

            product_tmpl_ids = None
            if is_filtered:
                domain = self._build_product_domain(kw)
                ProductTemplate = request.env['product.template'].sudo()
                products = ProductTemplate.search(domain)
                if not products:
                    return {
                        'ca_total': 0, 'tickets': 0, 'panier_moyen': 0,
                        'qty_sold': 0, 'qty_purchased': 0, 'stock_total': 0,
                        'sell_through': 0, 'ruptures_count': 0, 'ruptures_list': [],
                        'top_products': [], 'flop_products': [],
                        'abc_analysis': {'A': [], 'B': [], 'C': []},
                        'references_count': 0, 'total_active_skus': 0,
                        'taux_rupture': 0, 'couverture_moy': 0,
                        'stock_dormant_pct': 0, 'precision_inventaire': 99.5,
                        'alertes_stock': [], 'rotation_collection': [],
                        'gmroi_categorie': [], 'proches_rupture': [],
                    }
                product_tmpl_ids = products.ids
            else:
                ProductTemplate = request.env['product.template'].sudo()

            pos_domain = self._build_pos_domain(kw, product_tmpl_ids)

            # ✅ OPTIMISATION SQL read_group ultra-rapide (0.01s au lieu de 20s)
            pos_agg = request.env['pos.order.line'].sudo().read_group(
                pos_domain,
                ['price_subtotal_incl:sum', 'qty:sum', 'order_id:count_distinct'],
                []
            )
            if pos_agg and pos_agg[0]:
                ca_total = pos_agg[0].get('price_subtotal_incl') or 0.0
                tickets = pos_agg[0].get('order_id') or 0
                qty_sold_total = int(pos_agg[0].get('qty') or 0)
            else:
                ca_total = 0.0
                tickets = 0
                qty_sold_total = 0

            panier_moyen = ca_total / tickets if tickets > 0 else 0.0

            purchase_domain = self._build_purchase_domain(kw, product_tmpl_ids)
            po_agg = request.env['purchase.order.line'].sudo().read_group(
                purchase_domain,
                ['product_qty:sum'],
                []
            )
            qty_purchased_total = int(po_agg[0].get('product_qty') or 0) if (po_agg and po_agg[0]) else 0

            total_qty = qty_sold_total + qty_purchased_total
            sell_through = round((qty_sold_total / total_qty * 100), 1) if total_qty > 0 else 0.0

            # ✅ Groupement par produit POS (SQL GROUP BY product_id)
            pos_grouped = request.env['pos.order.line'].sudo().read_group(
                pos_domain,
                ['price_subtotal_incl:sum', 'qty:sum', 'product_id'],
                ['product_id'],
                lazy=False
            )
            pos_pids = [g['product_id'][0] for g in pos_grouped if g.get('product_id')]
            sales_by_tmpl = {}
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

            # ✅ Groupement par produit Achats (SQL GROUP BY product_id)
            po_grouped = request.env['purchase.order.line'].sudo().read_group(
                purchase_domain,
                ['product_qty:sum', 'product_id'],
                ['product_id'],
                lazy=False
            )
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

            # ✅ Groupement Stock Quant (SQL GROUP BY product_id)
            quant_domain = [
                ('location_id.usage', '=', 'internal'),
                ('product_id.product_tmpl_id.name', 'not ilike', 'sachet'),
                ('product_id.product_tmpl_id.name', 'not ilike', '2026'),
            ]
            shop_field = kw.get('shop_field')
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
                q_variants = request.env['product.product'].sudo().search_read(
                    [('product_tmpl_id', 'in', product_tmpl_ids)],
                    ['id']
                )
                quant_domain.append(('product_id', 'in', [v['id'] for v in q_variants]))

            quant_agg = request.env['stock.quant'].sudo().read_group(quant_domain, ['quantity:sum'], [])
            stock_total = int(quant_agg[0].get('quantity') or 0) if (quant_agg and quant_agg[0]) else 0

            quant_grouped = request.env['stock.quant'].sudo().read_group(
                quant_domain,
                ['quantity:sum', 'product_id'],
                ['product_id'],
                lazy=False
            )
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
            top_limit = max(1, int(kw.get('top_limit', 10)))
            flop_limit = max(1, int(kw.get('flop_limit', 10)))

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

            articles_for_cands = request.env['mv.article.base'].sudo().search([
                ('product_tmpl_id', 'in', list(candidate_ids))
            ])
            ref_by_tmpl = {a.product_tmpl_id.id: a.reference for a in articles_for_cands if a.product_tmpl_id}

            product_stats = []
            for tmpl in active_products:
                stat = sales_by_tmpl.get(tmpl.id, {'qty': 0, 'ca': 0})
                ref = (
                    tmpl.base_pivot_reference
                    or ref_by_tmpl.get(tmpl.id)
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
                mv_tmpl_ids = request.env['mv.article.base'].sudo().search([]).mapped('product_tmpl_id').ids
                relevant_tmpl_ids = set(sales_by_tmpl.keys()) | set(purchase_by_tmpl.keys()) | set(stock_by_tmpl.keys()) | set(mv_tmpl_ids)

            all_ruptures = []
            if relevant_tmpl_ids:
                articles_for_ruptures = request.env['mv.article.base'].sudo().search([
                    ('product_tmpl_id', 'in', list(relevant_tmpl_ids))
                ])
                ref_by_tmpl_all = {a.product_tmpl_id.id: a.reference for a in articles_for_ruptures if a.product_tmpl_id}

                tmpl_data = request.env['product.template'].sudo().search_read(
                    [('id', 'in', list(relevant_tmpl_ids))],
                    ['name', 'default_code', 'base_pivot_reference']
                )
                tmpl_by_id = {t['id']: t for t in tmpl_data}

                for tid in relevant_tmpl_ids:
                    stock = int(stock_by_tmpl.get(tid, 0))
                    if stock <= 0:
                        t = tmpl_by_id.get(tid, {})
                        ref = (
                            t.get('base_pivot_reference')
                            or ref_by_tmpl_all.get(tid)
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

            # CORRECTION #2c : references_count = nombre de références dans la sélection
            # Si filtré par collection/batch : on compte les produits du filtre
            # Sinon : on compte tous les SKUs actifs (ceux avec des données)
            if is_filtered and product_tmpl_ids:
                if kw.get('collection_id') or kw.get('batch_id'):
                    pivot_domain = []
                    if kw.get('collection_id'):
                        try:
                            pivot_domain.append(('collection_id', '=', int(kw['collection_id'])))
                        except (ValueError, TypeError):
                            pass
                    if kw.get('batch_id'):
                        try:
                            pivot_domain.append(('arrivage_id', '=', int(kw['batch_id'])))
                        except (ValueError, TypeError):
                            pass
                    pivot_count = request.env['mv.article.base'].sudo().search_count(pivot_domain) if pivot_domain else 0
                    references_count = max(len(product_tmpl_ids), pivot_count)
                else:
                    references_count = len(product_tmpl_ids)
            else:
                references_count = len(relevant_tmpl_ids) if relevant_tmpl_ids else len(sales_by_tmpl)

            total_active_skus = len(relevant_tmpl_ids) or 1
            taux_rupture = round((ruptures_count / total_active_skus) * 100, 1)

            date_start = kw.get('date_start')
            date_end = kw.get('date_end')
            days_in_period = 30
            if date_start and date_end:
                try:
                    d1 = datetime.strptime(date_start, '%Y-%m-%d')
                    d2 = datetime.strptime(date_end, '%Y-%m-%d')
                    days_in_period = max(1, (d2 - d1).days + 1)
                except ValueError:
                    pass

            product_coverages = {}
            for tid in relevant_tmpl_ids:
                stock = stock_by_tmpl.get(tid, 0)
                qty_sold = sales_by_tmpl.get(tid, {}).get('qty', 0)
                daily_rate = qty_sold / days_in_period if days_in_period > 0 else 0
                if daily_rate > 0:
                    cov = stock / daily_rate
                else:
                    cov = 999
                product_coverages[tid] = cov

            daily_sales_rate_total = qty_sold_total / days_in_period if days_in_period > 0 else 0
            couverture_moy = round(stock_total / daily_sales_rate_total) if daily_sales_rate_total > 0 else 0

            # ✅ OPTIMISATION SQL read_group pour les ventes à 90j (stock dormant)
            date_90d_ago = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d 00:00:00')
            pos_90d_domain = [
                ('order_id.date_order', '>=', date_90d_ago),
                ('order_id.state', 'in', ['paid', 'done', 'invoiced']),
                ('is_reward_line', '=', False),
                ('product_id.product_tmpl_id.name', 'not ilike', 'sachet'),
                ('product_id.product_tmpl_id.name', 'not ilike', '2026')
            ]
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

            dormant_stock_total = 0
            dormant_products = []
            for tid in relevant_tmpl_ids:
                stock = stock_by_tmpl.get(tid, 0)
                if stock > 0 and tid not in sold_90d_tmpl_ids:
                    dormant_stock_total += stock
                    t = tmpl_by_id.get(tid, {})
                    dormant_products.append({
                        'id': tid,
                        'name': t.get('name') or '—',
                        'stock': stock
                    })
            stock_dormant_pct = round((dormant_stock_total / stock_total) * 100, 1) if stock_total > 0 else 0.0

            inv_adjustments_count = request.env['stock.move'].sudo().search_count([
                ('date', '>=', (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d 00:00:00')),
                ('location_id.usage', '=', 'inventory'),
                ('state', '=', 'done')
            ])
            precision_inventaire = max(90.0, min(100.0, round(100.0 - (inv_adjustments_count / (total_active_skus or 1)) * 100, 1))) if inv_adjustments_count > 0 else 99.5

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

            alertes_stock = []

            rupture_cands = [r for r in all_ruptures if r['qty_sold'] > 0]
            rupture_cands.sort(key=lambda x: x['qty_sold'], reverse=True)
            for r in rupture_cands[:3]:
                alertes_stock.append({
                    'type': 'danger',
                    'message': f"{r['name']} — RUPTURE ({r['qty_sold']} vendus)",
                    'magasin': _resolve_magasin_name(r['id'])
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
                    'magasin': _resolve_magasin_name(c['id'])
                })

            dormant_products.sort(key=lambda x: x['stock'], reverse=True)
            for d in dormant_products[:2]:
                alertes_stock.append({
                    'type': 'info',
                    'message': f"{d['stock']} unités stock dormant — {d['name']}",
                    'magasin': 'Entrepôt'
                })

            alertes_stock = alertes_stock[:6]

            rotation_collection = []
            try:
                collections = request.env['product.collection'].sudo().search([])
                for col in collections:
                    col_tmpl_ids = request.env['product.template'].sudo().search([
                        ('collection_id', '=', col.id)
                    ]).ids
                    col_stock = sum(stock_by_tmpl.get(tid, 0) for tid in col_tmpl_ids)
                    col_sales = sum(sales_by_tmpl.get(tid, {}).get('qty', 0) for tid in col_tmpl_ids)
                    col_sales_annualized = col_sales * (365 / days_in_period) if days_in_period > 0 else 0
                    col_turnover = round(col_sales_annualized / col_stock, 1) if col_stock > 0 else 0.0

                    if col_stock > 0 or col_sales > 0:
                        rotation_collection.append({
                            'name': col.name,
                            'turnover': col_turnover,
                            'pct': min(100, int((col_turnover / 6.0) * 100)) if col_turnover > 0 else 15,
                            'warning': f"{col.name} sous seuil critique" if col_turnover < 2.0 and col_turnover > 0 else None
                        })
            except Exception as e:
                _logger.warning(f"Error computing collection rotation: {str(e)}")

            rotation_collection.sort(key=lambda x: (x['turnover'], x['pct']), reverse=True)
            rotation_collection = rotation_collection[:5]

            gmroi_categorie = []
            try:
                categories = request.env['product.category'].sudo().search([('parent_id', '!=', False)], limit=30)
                for cat in categories:
                    cat_tmpl_ids = request.env['product.template'].sudo().search([
                        ('categ_id', 'child_of', cat.id)
                    ]).ids
                    if not cat_tmpl_ids:
                        continue
                    cat_stock_cost = sum(stock_by_tmpl.get(tid, 0) * (tmpl_by_id.get(tid, {}).get('standard_price') or 200.0) for tid in cat_tmpl_ids)
                    cat_margin = 0.0
                    for line in pos_lines:
                        if line.product_id.product_tmpl_id.id in cat_tmpl_ids:
                            cost = (line.product_id.standard_price or 0.0) * line.qty
                            cat_margin += (line.price_subtotal_incl - cost)

                    cat_gmroi = round(cat_margin / cat_stock_cost, 1) if cat_stock_cost > 0 else 0.0
                    if cat_stock_cost > 0 or cat_margin > 0:
                        gmroi_categorie.append({
                            'name': cat.name,
                            'gmroi': cat_gmroi,
                            'pct': min(100, int((cat_gmroi / 4.0) * 100)) if cat_gmroi > 0 else 10
                        })
            except Exception as e:
                _logger.warning(f"Error computing GMROI: {str(e)}")

            gmroi_categorie.sort(key=lambda x: x['gmroi'], reverse=True)
            gmroi_categorie = gmroi_categorie[:5]

            candidate_proches_tids = [tid for tid in relevant_tmpl_ids if 0 < stock_by_tmpl.get(tid, 0) <= 10 or product_coverages.get(tid, 999) < 30]

            proches_rupture = []
            for tid in candidate_proches_tids:
                stock = stock_by_tmpl.get(tid, 0)
                cov = product_coverages.get(tid, 999)
                t = tmpl_by_id.get(tid, {})
                tmpl_obj = request.env['product.template'].sudo().browse(tid)
                col_name = tmpl_obj.collection_id.name if tmpl_obj.collection_id else '—'
                if col_name == '—':
                    ref_base = request.env['mv.article.base'].sudo().search([('product_tmpl_id', '=', tid)], limit=1)
                    if ref_base and ref_base.collection_id:
                        col_name = ref_base.collection_id.name

                store_loc = _resolve_magasin_name(tid)

                proches_rupture.append({
                    'id': tid,
                    'name': tmpl_obj.display_name or t.get('name') or '—',
                    'ref': t.get('base_pivot_reference') or t.get('default_code') or '—',
                    'collection': col_name,
                    'magasin': store_loc,
                    'stock': int(stock),
                    'couverture': round(cov) if cov < 999 else min(90, int(stock * 7)),
                    'classe': abc_class_map.get(tid, 'C')
                })

            proches_rupture.sort(key=lambda x: (x['stock'], x['couverture']))
            proches_rupture = proches_rupture[:100]

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

            return {
                'ca_total': round(ca_total, 2),
                'tickets': tickets,
                'references_count': references_count,
                'panier_moyen': round(panier_moyen, 2),
                'qty_sold': qty_sold_total,
                'qty_purchased': qty_purchased_total,
                'stock_total': stock_total,
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
                'precision_inventaire': precision_inventaire,
                'alertes_stock': alertes_stock,
                'rotation_collection': rotation_collection,
                'gmroi_categorie': gmroi_categorie,
                'proches_rupture': proches_rupture,
            }

        except Exception as e:
            _logger.error(f"Erreur api_kpis: {str(e)}", exc_info=True)
            return {'error': str(e)}

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
            return {'daily': daily_sales}
        except Exception as e:
            _logger.error(f"Erreur api_sales_daily: {str(e)}", exc_info=True)
            return {'error': str(e), 'daily': []}

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

            # CORRECTION #2 : Recherche élargie dans product.template
            products = ProductTemplate.search([
                '|', '|', '|',
                ('name', 'ilike', query),
                ('default_code', 'ilike', query),
                ('base_pivot_reference', 'ilike', query),
                ('categ_id.name', 'ilike', query),
            ], limit=30)

            # Chercher aussi dans mv.article.base (références de la Base Pivot)
            # Même si product_tmpl_id n'est pas renseigné, on peut retrouver le produit
            # par désignation ou référence
            articles = request.env['mv.article.base'].sudo().search([
                '|',
                ('reference', 'ilike', query),
                ('designation_odoo', 'ilike', query),
            ], limit=30)

            extra_tmpl_ids = []
            for a in articles:
                if a.product_tmpl_id and a.product_tmpl_id.id:
                    extra_tmpl_ids.append(a.product_tmpl_id.id)
                else:
                    # Article sans product_tmpl_id : on cherche par nom/référence
                    ref = (a.reference or '').strip()
                    desig = (a.designation_odoo or '').strip()
                    for search_val in [ref, desig]:
                        if search_val:
                            found = ProductTemplate.search([
                                '|', '|',
                                ('name', '=ilike', search_val),
                                ('default_code', '=ilike', search_val),
                                ('base_pivot_reference', '=ilike', search_val),
                            ], limit=1)
                            if found:
                                extra_tmpl_ids.append(found.id)
                                break

            extra_ids = set(extra_tmpl_ids) - set(products.ids)
            if extra_ids:
                extra_tmpls = ProductTemplate.browse(list(extra_ids))
                products = products | extra_tmpls

            # Si toujours pas de résultat dans product.template, créer une entrée
            # directement depuis la Base Pivot (sans product.template associé)
            pivot_only = []
            if not products:
                for a in articles:
                    if not a.product_tmpl_id:
                        pivot_only.append({
                            'id': None,
                            'article_id': a.id,
                            'name': a.designation_odoo or a.reference or '—',
                            'ref': a.reference or '—',
                            'source': 'pivot_only'
                        })

            results = [{
                'id': p.id,
                'name': p.name or '—',
                'ref': p.base_pivot_reference or p.default_code or '—',
            } for p in products[:20]]

            # Ajouter les résultats pivot uniquement si pas de résultat product.template
            if not results and pivot_only:
                results = pivot_only[:20]

            return {'results': results}
        except Exception as e:
            _logger.error(f"Erreur api_search_products: {str(e)}")
            return {'error': str(e), 'results': []}

    # ─────────────────────────────────────────────────────────────
    # DÉTAIL PRODUIT
    # ─────────────────────────────────────────────────────────────

    @http.route('/mavie/api/product-detail', type='json', auth='user', methods=['POST'], csrf=False)
    def api_product_detail(self, **kw):
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

            def _clean_ref(text):
                if not text:
                    return ""
                t = re.sub(r'\[.*?\]', '', text)
                t = re.sub(r'\(.*?\)', '', t)
                return t.strip()

            art_domain = [('product_tmpl_id', '=', product_tmpl.id)]
            if kw.get('batch_id'):
                try:
                    art_domain.append(('arrivage_id', '=', int(kw['batch_id'])))
                except (ValueError, TypeError):
                    pass
            if kw.get('collection_id'):
                try:
                    art_domain.append(('collection_id', '=', int(kw['collection_id'])))
                except (ValueError, TypeError):
                    pass

            articles = request.env['mv.article.base'].sudo().search(art_domain)

            if not articles and (kw.get('batch_id') or kw.get('collection_id')):
                articles = request.env['mv.article.base'].sudo().search(
                    [('product_tmpl_id', '=', product_tmpl.id)]
                )
            if not articles:
                refs_to_try = []
                if product_tmpl.base_pivot_reference:
                    refs_to_try.append(product_tmpl.base_pivot_reference.strip())
                if product_tmpl.default_code:
                    refs_to_try.append(product_tmpl.default_code.strip())
                    cleaned_code = _clean_ref(product_tmpl.default_code)
                    if cleaned_code:
                        refs_to_try.append(cleaned_code)
                if product_tmpl.name:
                    refs_to_try.append(product_tmpl.name.strip())
                    cleaned_name = _clean_ref(product_tmpl.name)
                    if cleaned_name:
                        refs_to_try.append(cleaned_name)

                refs_to_try = list(dict.fromkeys([r for r in refs_to_try if r]))

                for ref in refs_to_try:
                    articles = request.env['mv.article.base'].sudo().search([('reference', '=ilike', ref)])
                    if articles:
                        break
                    articles = request.env['mv.article.base'].sudo().search([('designation_odoo', '=ilike', ref)])
                    if articles:
                        break

            if articles and not articles[0].product_tmpl_id:
                try:
                    articles[0].sudo().write({'product_tmpl_id': product_tmpl.id})
                    if not product_tmpl.base_pivot_reference and articles[0].reference:
                        product_tmpl.sudo().write({'base_pivot_reference': articles[0].reference})
                except Exception as e:
                    _logger.warning(f"Auto-link failed for template {product_tmpl.id}: {e}")

            pos_domain = self._build_pos_domain(kw, [product_tmpl.id])
            pos_lines = request.env['pos.order.line'].sudo().search(pos_domain)

            qty_sold = int(sum(pos_lines.mapped('qty'))) if pos_lines else 0
            ca = sum(pos_lines.mapped('price_subtotal_incl')) if pos_lines else 0.0

            purchase_domain = self._build_purchase_domain(kw, [product_tmpl.id])
            po_lines = request.env['purchase.order.line'].sudo().search(purchase_domain)
            qty_purchased = int(sum(po_lines.mapped('product_qty'))) if po_lines else 0

            margin = articles[0].margin_percent if articles and hasattr(articles[0], 'margin_percent') else 0.0

            total_qty = qty_sold + qty_purchased
            sell_through = round((qty_sold / total_qty * 100), 1) if total_qty > 0 else 0.0

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

            shop_mappings = self._get_active_shop_mappings()
            shop_fields_cleaned = [
                (sm.shop_field, sm.warehouse_id.name if sm.warehouse_id else (sm.shop_label or sm.shop_field))
                for sm in shop_mappings if sm.shop_field
            ]

            dispatch_by_color = {}
            if articles and shop_fields_cleaned:
                for art in articles:
                    for cl in art.color_line_ids:
                        color_name = (cl.color or '').upper().strip()
                        if color_name:
                            shop_sum = sum(float(getattr(cl, f, 0.0) or 0.0) for f, _ in shop_fields_cleaned)
                            disp = (
                                float(getattr(cl, 'dispatched_total', 0) or 0)
                                or shop_sum
                                or float(getattr(cl, 'line_total_pieces', 0) or 0)
                                or 0
                            )
                            dispatch_by_color[color_name] = dispatch_by_color.get(color_name, 0) + int(disp)

            best_variants = []
            for v in product_variants:
                stat = sales_by_variant.get(v.id, {'qty': 0, 'ca': 0, 'shops': {}})

                color_name = "—"
                for attr_val in v.product_template_attribute_value_ids:
                    attr_name = (attr_val.attribute_id.name or '').upper()
                    if any(k in attr_name for k in ['COULEUR', 'COLOR', 'COL']):
                        color_name = attr_val.name.upper().strip()
                        break

                dispatched = int(dispatch_by_color.get(color_name, 0))

                quants_v = request.env['stock.quant'].sudo().search([
                    ('product_id', '=', v.id),
                    ('location_id.usage', '=', 'internal')
                ])
                var_stock = int(sum(quants_v.mapped('quantity'))) if quants_v else 0

                if dispatched == 0 and var_stock > 0:
                    dispatched = var_stock

                best_variants.append({
                    'name': v.display_name,
                    'qty': int(stat['qty']),
                    'ca': round(stat['ca'], 2),
                    'dispatched': dispatched,
                    'stock': var_stock,
                    'shops': stat['shops']
                })

            best_variants = sorted(best_variants, key=lambda x: x['ca'], reverse=True)[:10]
            for idx, v in enumerate(best_variants):
                v['rank'] = idx + 1
                v['total_pieces'] = max(0, v.get('dispatched', 0) if v.get('dispatched', 0) > 0 else v.get('stock', 0))
                v['reste'] = v.get('total_pieces', 0) - v.get('qty', 0)

            stock_by_store = []
            try:
                warehouses = request.env['stock.warehouse'].sudo().search([])
                for wh in warehouses:
                    if not wh.lot_stock_id:
                        continue
                    wh_name = (wh.name or '').upper()
                    if any(x in wh_name for x in ['MOD FOR LIFE', 'DIGITAL SHOP', 'PAIE', 'ORANGER']):
                        continue
                    quants = request.env['stock.quant'].sudo().search([
                        ('product_id', 'in', product_variants.ids),
                        ('location_id', 'child_of', wh.lot_stock_id.id),
                    ])
                    stock_qty = sum(quants.mapped('quantity')) if quants else 0
                    reserved_qty = sum(quants.mapped('reserved_quantity')) if quants else 0

                    stock_by_store.append({
                        'store_name': wh.name,
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
                stock_total = sum(s['stock'] for s in stock_by_store)

            stock_by_store_pivot = []
            if articles and shop_fields_cleaned:
                try:
                    qty_by_field = {field: 0 for field, _ in shop_fields_cleaned}
                    label_by_field = {field: label for field, label in shop_fields_cleaned}

                    for art in articles:
                        for line in art.color_line_ids:
                            for field in qty_by_field.keys():
                                qty_by_field[field] += int(getattr(line, field, 0) or 0)

                    for field, label in shop_fields_cleaned:
                        stock_by_store_pivot.append({
                            'field': field,
                            'name': label,
                            'qty': qty_by_field[field]
                        })
                except Exception as e:
                    _logger.warning(f"Erreur dispatch pivot: {str(e)}")

            if not stock_by_store_pivot and stock_by_store:
                for s in stock_by_store:
                    stock_by_store_pivot.append({
                        'field': s['store_name'],
                        'name': s['store_name'],
                        'qty': s['stock']
                    })

            batch_data = None
            if articles and articles[0].batch_id:
                batch_data = {
                    'name': articles[0].batch_id.name,
                    'date': articles[0].batch_id.create_date.strftime('%Y-%m-%d') if articles[0].batch_id.create_date else '',
                    'collection': articles[0].collection_id.name if articles[0].collection_id else '—'
                }

            image_url = (
                f'/web/image/mv.article.base/{articles[0].id}/image_128'
                if articles
                else f'/web/image/product.template/{product_tmpl.id}/image_128'
            )

            return {
                'id': product_tmpl.id,
                'name': product_tmpl.name,
                'ref': product_tmpl.base_pivot_reference or product_tmpl.default_code or '—',
                'family': product_tmpl.categ_id.name if product_tmpl.categ_id else '—',
                'qty_sold': qty_sold,
                'qty_purchased': qty_purchased,
                'stock_total': stock_total,
                'ca': ca,
                'margin': margin,
                'sell_through': sell_through,
                'pv_ttc': product_tmpl.list_price or 0.0,
                'cost': product_tmpl.standard_price or 0.0,
                'collection_id': product_tmpl.collection_id.id if getattr(product_tmpl, 'collection_id', False) else None,
                'collection_name': product_tmpl.collection_id.name if getattr(product_tmpl, 'collection_id', False) else '—',
                'batch_id': product_tmpl.arrivage_id.id if getattr(product_tmpl, 'arrivage_id', False) else None,
                'batch_name': product_tmpl.arrivage_id.name if getattr(product_tmpl, 'arrivage_id', False) else '—',
                'best_variants': best_variants,
                'variants': best_variants,
                'stock_by_store': stock_by_store_pivot,
                'real_stock_by_store': stock_by_store,
                'batch': batch_data,
                'image_url': image_url,
            }
        except Exception as e:
            _logger.error(f"Erreur api_product_detail: {str(e)}", exc_info=True)
            return {'error': str(e)}