# -*- coding: utf-8 -*-
from odoo import http, fields
from odoo.http import request, Response
import json
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
        """
        Build a flat Odoo domain for product.template based on the given filters.
        When collection_id or batch_id is provided, we search mv.article.base first
        (which is the pivot table with the real data), then translate to product.template IDs.
        """
        domain = []

        if kw.get('collection_id') or kw.get('batch_id'):
            # Build article domain on mv.article.base
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

            # Collect product_tmpl_ids from articles that are already linked
            linked_tmpl_ids = []
            for a in articles:
                if a.product_tmpl_id and a.product_tmpl_id.id:
                    linked_tmpl_ids.append(a.product_tmpl_id.id)

            # Also search product.template directly using the native fields
            # (collection_id / arrivage_id are set on product.template by the module)
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

            # Merge both sets
            all_tmpl_ids = list(set(linked_tmpl_ids + direct_tmpl_ids))

            if all_tmpl_ids:
                domain.append(('id', 'in', all_tmpl_ids))
            else:
                # No products found for this filter — return impossible domain
                domain.append(('id', '=', -1))

        if kw.get('categ_id'):
            try:
                domain.append(('categ_id', 'child_of', int(kw['categ_id'])))
            except (ValueError, TypeError):
                pass

        return domain

    def _build_pos_domain(self, kw, product_tmpl_ids):
        domain = [
            ('order_id.state', 'in', ['paid', 'done', 'invoiced']),
            ('is_reward_line', '=', False),
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
        quant_domain = [('location_id.usage', '=', 'internal')]
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
                ShopMapping = request.env['mv.batch.shop.mapping'].sudo()
                shops = ShopMapping.search([('active', '=', True), ('shop_field', '!=', 'shop')])
                filters['shops'] = [
                    {'field': s.shop_field, 'name': s.shop_label or s.shop_field}
                    for s in shops
                ]
            except Exception as e:
                _logger.warning(f"Erreur shop mapping: {str(e)}")

            try:
                # Get distinct categories from product.template
                request.env.cr.execute("""
                    SELECT DISTINCT pc.id, pc.name
                    FROM product_template pt
                    JOIN product_category pc ON pc.id = pt.categ_id
                    ORDER BY pc.name
                """)
                rows = request.env.cr.fetchall()
                excluded_names = {'all', 'expenses', 'saleable', 'pos', 'bons & fidélité', 'demi0', 'solde test 2', 'étiquettes solde'}
                filters['categories'] = [
                    {'id': r[0], 'name': r[1]} for r in rows
                    if r[1] and r[1].lower().strip() not in excluded_names
                    and 'test' not in r[1].lower()
                    and 'solde' not in r[1].lower()
                    and '()' not in r[1]
                    and not r[1].strip().endswith('0')
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
                    }
                product_tmpl_ids = products.ids
            else:
                ProductTemplate = request.env['product.template'].sudo()

            # POS lines
            pos_domain = self._build_pos_domain(kw, product_tmpl_ids)
            pos_lines = request.env['pos.order.line'].sudo().search(pos_domain)

            ca_total = sum(pos_lines.mapped('price_subtotal_incl')) if pos_lines else 0
            unique_orders = pos_lines.mapped('order_id')
            tickets = len(unique_orders)
            qty_sold_total = int(sum(pos_lines.mapped('qty'))) if pos_lines else 0
            panier_moyen = ca_total / tickets if tickets > 0 else 0

            # Purchase lines
            purchase_domain = self._build_purchase_domain(kw, product_tmpl_ids)
            po_lines = request.env['purchase.order.line'].sudo().search(purchase_domain)
            qty_purchased_total = int(sum(po_lines.mapped('product_qty'))) if po_lines else 0

            total_qty = qty_sold_total + qty_purchased_total
            sell_through = round((qty_sold_total / total_qty * 100), 1) if total_qty > 0 else 0

            # Stock
            quants = self._get_stock_quants(product_tmpl_ids, shop_field=kw.get('shop_field'))
            stock_total = int(sum(quants.mapped('quantity'))) if quants else 0

            # Aggregate by template
            sales_by_tmpl = {}
            for line in pos_lines:
                tid = line.product_id.product_tmpl_id.id
                if not tid:
                    continue
                if tid not in sales_by_tmpl:
                    sales_by_tmpl[tid] = {'qty': 0, 'ca': 0}
                sales_by_tmpl[tid]['qty'] += line.qty
                sales_by_tmpl[tid]['ca'] += line.price_subtotal_incl

            purchase_by_tmpl = {}
            for line in po_lines:
                tid = line.product_id.product_tmpl_id.id
                if not tid:
                    continue
                purchase_by_tmpl[tid] = purchase_by_tmpl.get(tid, 0) + line.product_qty

            stock_by_tmpl = {}
            for q in quants:
                tid = q.product_id.product_tmpl_id.id
                if not tid:
                    continue
                stock_by_tmpl[tid] = stock_by_tmpl.get(tid, 0) + q.quantity

            page = kw.get('page', 'ventes')
            top_limit = max(1, int(kw.get('top_limit', 10)))
            flop_limit = max(1, int(kw.get('flop_limit', 10)))

            # If filtered, we only care about templates in product_tmpl_ids
            if is_filtered:
                filtered_set = set(product_tmpl_ids)
                filtered_sales = {k: v for k, v in sales_by_tmpl.items() if k in filtered_set}
                filtered_purchase = {k: v for k, v in purchase_by_tmpl.items() if k in filtered_set}
                filtered_stock = {k: v for k, v in stock_by_tmpl.items() if k in filtered_set}
            else:
                filtered_sales = sales_by_tmpl
                filtered_purchase = purchase_by_tmpl
                filtered_stock = stock_by_tmpl

            # Determine candidate product IDs for top/flop tables
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

            # Pad candidates if not enough
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
                # Fallback: just load whatever products we know
                if is_filtered and product_tmpl_ids:
                    candidate_ids = set(product_tmpl_ids[:top_limit + flop_limit])
                else:
                    candidate_ids = set(list(sales_by_tmpl.keys())[:top_limit] + list(sales_by_tmpl.keys())[-flop_limit:])

            active_products = ProductTemplate.browse(list(candidate_ids))

            # Resolve references from mv.article.base
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

            # Compute ruptures count and list across all relevant templates
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
            ruptures_list = sorted(all_ruptures, key=lambda a: a['qty_sold'], reverse=True)[:100]

            if page == 'stock':
                top_products = sorted(product_stats, key=lambda a: a['stock'], reverse=True)[:top_limit]
                flop_products = sorted(product_stats, key=lambda a: a['stock'])[:flop_limit]
            elif page == 'commandes':
                top_products = sorted(product_stats, key=lambda a: a['qty_purchased'], reverse=True)[:top_limit]
                flop_products = sorted(product_stats, key=lambda a: a['qty_purchased'])[:flop_limit]
            else:
                top_products = sorted(product_stats, key=lambda a: a['ca'], reverse=True)[:top_limit]
                # Les flops doivent idéalement avoir du stock pour être considérés comme des flops
                flops_avec_stock = sorted([p for p in product_stats if p['stock'] > 0], key=lambda a: a['ca'])
                if flops_avec_stock:
                    flop_products = flops_avec_stock[:flop_limit]
                else:
                    flop_products = sorted(product_stats, key=lambda a: a['ca'])[:flop_limit]

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
                elif pct <= 95:
                    abc['B'].append(p_item)
                else:
                    abc['C'].append(p_item)

            return {
                'ca_total': round(ca_total, 2),
                'tickets': tickets,
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
            pos_lines = request.env['pos.order.line'].sudo().search(pos_domain)

            if not pos_lines:
                return {'daily': []}

            # Pre-fetch template data
            pos_lines.mapped('product_id.product_tmpl_id.arrivage_id')

            sales_by_arrivage = {}
            for line in pos_lines:
                arrivage = line.product_id.product_tmpl_id.arrivage_id
                key = (arrivage.id, arrivage.name) if arrivage and arrivage.id else (0, 'Sans Arrivage')

                if key not in sales_by_arrivage:
                    sales_by_arrivage[key] = {'ca': 0.0, 'qty': 0}
                sales_by_arrivage[key]['ca'] += line.price_subtotal_incl
                sales_by_arrivage[key]['qty'] += line.qty

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
            products = ProductTemplate.search([
                '|', '|',
                ('name', 'ilike', query),
                ('default_code', 'ilike', query),
                ('base_pivot_reference', 'ilike', query),
            ], limit=20)

            # Also search mv.article.base by reference
            articles = request.env['mv.article.base'].sudo().search([
                ('reference', 'ilike', query)
            ], limit=20)
            extra_tmpl_ids = [a.product_tmpl_id.id for a in articles if a.product_tmpl_id]
            extra_ids = set(extra_tmpl_ids) - set(products.ids)
            if extra_ids:
                extra_tmpls = ProductTemplate.browse(list(extra_ids))
                products = products | extra_tmpls

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

            # Find pivot article — try multiple strategies filtering by active filters first
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

            article = request.env['mv.article.base'].sudo().search(art_domain, limit=1)

            # Fallback 1: search without active filter
            if not article and (kw.get('batch_id') or kw.get('collection_id')):
                article = request.env['mv.article.base'].sudo().search(
                    [('product_tmpl_id', '=', product_tmpl.id)], limit=1
                )
            # Fallback 2: search by base_pivot_reference
            if not article:
                ref = product_tmpl.base_pivot_reference or product_tmpl.default_code
                if ref:
                    article = request.env['mv.article.base'].sudo().search(
                        [('reference', '=ilike', ref.strip())], limit=1
                    )
            # Fallback 3: search by product name
            if not article:
                article = request.env['mv.article.base'].sudo().search(
                    [('reference', '=ilike', product_tmpl.name.strip())], limit=1
                )

            pos_domain = self._build_pos_domain(kw, [product_tmpl.id])
            pos_lines = request.env['pos.order.line'].sudo().search(pos_domain)

            qty_sold = int(sum(pos_lines.mapped('qty'))) if pos_lines else 0
            ca = sum(pos_lines.mapped('price_subtotal_incl')) if pos_lines else 0.0

            purchase_domain = self._build_purchase_domain(kw, [product_tmpl.id])
            po_lines = request.env['purchase.order.line'].sudo().search(purchase_domain)
            qty_purchased = int(sum(po_lines.mapped('product_qty'))) if po_lines else 0

            margin = article.margin_percent if article and hasattr(article, 'margin_percent') else 0.0

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

            dispatch_by_color = {}
            if article and article.color_line_ids:
                for cl in article.color_line_ids:
                    color_name = (cl.color or '').upper().strip()
                    if color_name:
                        dispatch_by_color[color_name] = (
                            getattr(cl, 'dispatched_total', 0)
                            or getattr(cl, 'line_total_pieces', 0)
                            or 0
                        )

            best_variants = []
            for v in product_variants:
                stat = sales_by_variant.get(v.id, {'qty': 0, 'ca': 0, 'shops': {}})

                color_name = "—"
                for attr_val in v.product_template_attribute_value_ids:
                    if attr_val.attribute_id.name and 'COULEUR' in attr_val.attribute_id.name.upper():
                        color_name = attr_val.name.upper().strip()
                        break

                dispatched = int(dispatch_by_color.get(color_name, 0))
                best_variants.append({
                    'name': v.display_name,
                    'qty': int(stat['qty']),
                    'ca': round(stat['ca'], 2),
                    'dispatched': dispatched,
                    'shops': stat['shops']
                })

            best_variants = sorted(best_variants, key=lambda x: x['ca'], reverse=True)[:10]
            for idx, v in enumerate(best_variants):
                v['rank'] = idx + 1
                v['total_pieces'] = v.get('dispatched', 0)
                v['reste'] = v.get('total_pieces', 0) - v.get('qty', 0)

            # Real stock from Odoo stock.quant
            # Query via warehouse.lot_stock_id for accuracy
            stock_by_store = []
            try:
                warehouses = request.env['stock.warehouse'].sudo().search([])
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
                        'stock': int(stock_qty),
                        'reserved': int(reserved_qty),
                        'available': int(stock_qty - reserved_qty),
                    })
            except Exception as e:
                _logger.warning(f"Erreur stock réel: {str(e)}")

            # Stock total reflects selected shop if active
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

            # Dispatch from pivot (Base Pivot → mv.article.base.color.line)
            stock_by_store_pivot = []
            if article:
                try:
                    # Use SHOP_FIELDS from the model to get correct label mapping
                    try:
                        from odoo.addons.custom_delta_gold.mv_base_pivot.models.mv_batch_shop_mapping import SHOP_FIELDS
                    except ImportError:
                        SHOP_FIELDS = [
                            ("marina_1", "Marina 1"), ("salam_1", "Salam 1"),
                            ("salam_2", "Salam 2"), ("selapark_a", "Selapark A"),
                            ("morocco_mall", "Morocco Mall"), ("californie", "Californie"),
                            ("marina_s", "Marina S"), ("tachefine", "Tachefine"),
                            ("ain_sebaa", "Aîn Sebaa"), ("mohammadia", "Mohammadia"),
                            ("selapark_t", "Selapark T"), ("agdal", "Agdal"),
                            ("r_center", "R. Center"), ("mohamed_v", "Mohamed V"),
                            ("citymall", "CityMall"), ("ibn_batouta", "Ibn Batouta"),
                            ("shop", "Shop"), ("twin_c", "TWIN C"),
                        ]

                    SHOP_FIELDS_CLEANED = [(field, label) for field, label in SHOP_FIELDS if field != 'shop']

                    for field, label in SHOP_FIELDS_CLEANED:
                        qty_store = 0
                        for line in article.color_line_ids:
                            qty_store += int(getattr(line, field, 0) or 0)
                        if qty_store > 0:
                            stock_by_store_pivot.append({
                                'field': field,
                                'name': label,
                                'qty': qty_store
                            })
                except Exception as e:
                    _logger.warning(f"Erreur dispatch pivot: {str(e)}")

            batch_data = None
            if article and article.batch_id:
                batch_data = {
                    'name': article.batch_id.name,
                    'date': article.batch_id.create_date.strftime('%Y-%m-%d') if article.batch_id.create_date else '',
                    'collection': article.collection_id.name if article.collection_id else '—'
                }

            image_url = (
                f'/web/image/mv.article.base/{article.id}/image_128'
                if article
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