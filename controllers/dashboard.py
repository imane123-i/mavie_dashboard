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

    def _get_excluded_non_retail_ids(self, kw=None):
        """Sociétés non-retail à RÉELLEMENT exclure des KPIs.

        DÉCISION UTILISATEUR (2026-08-18) : cocher une société dans le
        sélecteur standard doit toujours avoir un effet visible. Une société
        non-retail (MOD FOR LIFE) explicitement cochée n'est donc plus
        exclue — ses achats, ventes et stock s'ajoutent, exactement comme le
        ferait Odoo avec son propre filtre société.

        Conséquence assumée : quand MOD FOR LIFE est cochée EN MÊME TEMPS
        que les sociétés magasins, la même marchandise est comptée deux fois
        (une fois à l'import chez le fournisseur externe, une fois à la
        revente interne vers le magasin) — c'est aussi ce que fait l'écran
        "Analyse des achats" d'Odoo. Décocher MOD FOR LIFE redonne la vision
        retail pure, sans double comptage.

        Si un magasin précis est filtré (shop_field), l'exclusion reste
        totale : un magasin appartient forcément à une société retail.
        """
        non_retail_ids = self._get_non_retail_company_ids()
        if kw and kw.get('shop_field'):
            return non_retail_ids
        context_company_ids = self._get_context_company_ids()
        return [cid for cid in non_retail_ids if cid not in context_company_ids]

    def _get_explicit_non_retail_ids(self, kw=None):
        """Sociétés non-retail que l'utilisateur a explicitement cochées —
        complément de _get_excluded_non_retail_ids (liste vide dans le cas
        normal où seules des sociétés magasins sont sélectionnées)."""
        excluded = self._get_excluded_non_retail_ids(kw)
        return [cid for cid in self._get_non_retail_company_ids() if cid not in excluded]

    def _build_non_retail_sale_domain(self, kw, product_tmpl_ids=None):
        """Ventes inter-sociétés d'une société non-retail cochée.

        MOD FOR LIFE ne vend pas en caisse : son chiffre d'affaires vient de
        `sale.order` vers les 3 sociétés magasins (créé par
        mv_article_batch.action_generate_sale_orders). C'est le pendant, côté
        ventes, de ce que `purchase.order.line` couvre déjà côté achats.
        """
        explicit_ids = self._get_explicit_non_retail_ids(kw)
        if not explicit_ids:
            return [('id', '=', -1)]
        domain = [
            ('order_id.state', 'in', ['sale', 'done']),
            ('order_id.company_id', 'in', explicit_ids),
        ]
        domain += self._sachet_exclude_domain('product_id.product_tmpl_id.collection_id')
        if product_tmpl_ids is not None:
            domain.append(('product_id.product_tmpl_id', 'in', product_tmpl_ids))
        if kw.get('date_start'):
            domain.append(('order_id.date_order', '>=', kw['date_start'] + ' 00:00:00'))
        if kw.get('date_end'):
            domain.append(('order_id.date_order', '<=', kw['date_end'] + ' 23:59:59'))
        return domain

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

    def _pos_sales_by_product_and_config(self, pos_domain):
        """[(product_id, config_id, company_id, ca_ttc, qty)] pour ce domaine.

        ⚡ PERF : la version précédente faisait un read_group par
        (product_id, order_id) puis un search_read sur TOUTES les commandes
        pour retrouver leur magasin. Sans filtre, cela représentait ~250 000
        commandes à lire, plus le name_get qu'Odoo exécute sur chaque groupe
        many2one (le piège déjà documenté dans _group_sums) : le graphique
        "CA par Arrivage" mettait 62 s à répondre, mesuré en base.

        Le magasin est en réalité une simple jointure
        ligne → commande → session → config : on la fait directement en SQL
        et on agrège par (produit, point de vente), ce qui ramène quelques
        milliers de lignes au lieu de centaines de milliers. Les jointures
        sont ajoutées APRÈS le FROM généré par _where_calc, avec des alias
        dédiés (po_sales/ps_sales) pour ne pas entrer en collision avec ceux
        qu'Odoo génère lui-même à partir du domaine.
        """
        Line = request.env['pos.order.line'].sudo()
        query = Line._where_calc(pos_domain)
        Line._apply_ir_rules(query, 'read')
        from_clause, where_clause, params = query.get_sql()
        request.env.cr.execute(
            'SELECT "pos_order_line"."product_id", ps_sales."config_id", '
            '       po_sales."company_id", '
            '       SUM("pos_order_line"."price_subtotal_incl"), '
            '       SUM("pos_order_line"."qty") '
            'FROM %s '
            'LEFT JOIN "pos_order" AS po_sales ON po_sales."id" = "pos_order_line"."order_id" '
            'LEFT JOIN "pos_session" AS ps_sales ON ps_sales."id" = po_sales."session_id" '
            'WHERE %s '
            'GROUP BY 1, 2, 3' % (from_clause, where_clause or 'TRUE'),
            params,
        )
        return request.env.cr.fetchall()

    def _group_sums(self, model_name, domain, sum_fields, group_fields=('product_id',), agg='SUM'):
        """read_group par product_id (et éventuellement company_id) SANS le
        surcoût de libellé d'Odoo.

        ⚡ PERF : read_group groupé sur un many2one fait ensuite un name_get
        sur CHAQUE groupe pour renvoyer (id, "nom affiché"). Sur
        pos.order.line ça représente 38 394 produits à nommer : mesuré à
        12-15 s, alors que l'agrégation SQL seule prend 0,56 s (EXPLAIN
        ANALYZE). Le dashboard n'utilise QUE les ids (g['product_id'][0]),
        jamais le libellé — on exécute donc l'agrégation directement et on
        renvoie la même forme, avec un libellé vide.

        Le domaine passe par _where_calc + _apply_ir_rules : les filtres et
        les règles de sécurité restent rigoureusement identiques à ceux de
        read_group.
        """
        Model = request.env[model_name].sudo()
        query = Model._where_calc(domain)
        Model._apply_ir_rules(query, 'read')
        from_clause, where_clause, params = query.get_sql()
        table = Model._table
        cols = ', '.join('"%s"."%s"' % (table, f) for f in group_fields)
        sums = ', '.join('%s("%s"."%s")' % (agg, table, f) for f in sum_fields)
        request.env.cr.execute(
            'SELECT %s, %s FROM %s WHERE %s GROUP BY %s' % (
                cols, sums, from_clause, where_clause or 'TRUE', cols),
            params,
        )
        rows = request.env.cr.fetchall()
        n_group = len(group_fields)
        out = []
        for row in rows:
            rec = {}
            for i, f in enumerate(group_fields):
                # Même forme que read_group : (id, libellé). Le libellé n'est
                # pas utilisé par le dashboard, on évite donc de le calculer.
                rec[f] = (row[i], '') if row[i] is not None else False
            for j, f in enumerate(sum_fields):
                val = row[n_group + j]
                # Les agrégats non numériques (MAX sur une date) doivent
                # rester tels quels ; seul un total absent vaut 0.
                rec[f] = val if val is not None else (None if agg != 'SUM' else 0.0)
            out.append(rec)
        return out

    # ─────────────────────────────────────────────────────────────
    # PHOTOS PRODUIT
    #
    # VÉRIFICATION EN BASE (demande utilisateur "vérifie les photos") :
    # seules 50 fiches product.template sur ~5 000 portent réellement une
    # image, et 53 mv.article.base (Base Pivot) en portent une de leur côté.
    # Le dashboard pointait toujours /web/image/product.template/<id>/... :
    # pour tout le reste du catalogue, Odoo renvoie une image de
    # remplacement grise, indistinguable d'une vraie photo. On résout donc
    # explicitement la source disponible (produit, puis repli Base Pivot) et
    # on renvoie `has_image` pour que l'écran affiche un vrai « pas de
    # photo » plutôt qu'un carré vide trompeur, et pour que l'export CSV ne
    # contienne une URL que quand une photo existe vraiment.
    # ─────────────────────────────────────────────────────────────

    def _image_availability(self, product_tmpl_ids, size='image_128'):
        """{product_tmpl_id: 'product' | 'article' | None} en 2 requêtes."""
        result = {tid: None for tid in product_tmpl_ids}
        if not product_tmpl_ids:
            return result

        Attachment = request.env['ir.attachment'].sudo()
        tmpl_with_image = Attachment.search_read([
            ('res_model', '=', 'product.template'),
            ('res_field', '=', size),
            ('res_id', 'in', list(product_tmpl_ids)),
        ], ['res_id'])
        for row in tmpl_with_image:
            result[row['res_id']] = 'product'

        missing = [tid for tid, src in result.items() if not src]
        if missing:
            try:
                articles = request.env['mv.article.base'].sudo().search_read(
                    [('product_tmpl_id', 'in', missing), ('image_1920', '!=', False)],
                    ['id', 'product_tmpl_id'],
                )
                for art in articles:
                    tmpl_ref = art.get('product_tmpl_id')
                    if tmpl_ref:
                        result[tmpl_ref[0]] = 'article:%s' % art['id']
            except Exception as e:  # Base Pivot absent / champ renommé
                _logger.warning("Photos Base Pivot indisponibles: %s", e)
        return result

    def _image_url(self, product_tmpl_id, source, size='image_128'):
        """URL de la photo réellement disponible, ou None."""
        if not source:
            return None
        if source == 'product':
            return '/web/image/product.template/%s/%s' % (product_tmpl_id, size)
        if source.startswith('article:'):
            return '/web/image/mv.article.base/%s/image_1920' % source.split(':', 1)[1]
        return None

    def _absolute_url(self, path):
        if not path:
            return ''
        base = request.env['ir.config_parameter'].sudo().get_param('web.base.url') or ''
        return base.rstrip('/') + path

    # ─────────────────────────────────────────────────────────────
    # PRIX AFFICHÉS EN TTC
    #
    # DÉCISION UTILISATEUR (2026-08-29) : « le client ne va pas payer
    # seulement 186,75, il va payer 224,10 ». Les listes de soldes
    # affichaient le prix catalogue et le prix payé en HT (price_unit,
    # list_price) à côté d'un CA encaissé en TTC (price_subtotal_incl) —
    # trois colonnes, deux bases différentes, et un prix qui ne correspondait
    # à rien de ce qu'un responsable magasin voit au comptoir.
    #
    # Aucune taxe n'est recalculée ici : Odoo stocke déjà le montant TTC de
    # chaque ligne. On lit ce montant, et on en déduit le taux réellement
    # appliqué à CETTE ligne (price_subtotal_incl / price_subtotal) plutôt
    # que de coder un 20 % en dur — vérifié en base, 358 876 lignes sont à
    # 20 % mais 2 796 lignes sont sans taxe.
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    def _ttc_ratio(subtotal_ht, subtotal_incl):
        """Coefficient TTC/HT réellement appliqué ; 1.0 si indéterminable."""
        try:
            ht = float(subtotal_ht or 0.0)
            incl = float(subtotal_incl or 0.0)
        except (TypeError, ValueError):
            return 1.0
        if not ht:
            return 1.0
        ratio = incl / ht
        # Un ratio aberrant (données incohérentes) ne doit pas gonfler un
        # prix affiché : on retombe alors sur "pas de conversion".
        return ratio if 0.5 <= ratio <= 2.0 else 1.0

    @staticmethod
    def _unit_price_ttc(subtotal_incl, qty):
        """Prix unitaire TTC = montant encaissé / quantité.

        Passe par le montant déjà calculé par Odoo plutôt que par price_unit
        (HT) : exact, et valable aussi pour un retour (quantité négative et
        montant négatif se compensent).
        """
        try:
            qty = float(qty or 0.0)
            if not qty:
                return 0.0
            return float(subtotal_incl or 0.0) / qty
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0

    def _get_sachet_variant_ids(self):
        """Variantes (product.product) de la collection Sachet.

        ⚡ PERF : c'est LA optimisation la plus rentable du dashboard. Écrire
        ('product_id.product_tmpl_id.collection_id', 'not in', [...]) oblige
        Postgres à rejoindre product_product + product_template pour CHAQUE
        ligne scannée — mesuré à 15,1 s sur les 417 000 lignes de
        pos_order_line. La même exclusion exprimée en IDs de variantes
        ('product_id', 'not in', [...]) tombe à 1,8 s, soit 13 s gagnées sur
        un seul écran. La collection Sachet ne contient qu'une poignée de
        variantes, donc la liste d'IDs reste minuscule.
        """
        # Mémorisé pour la durée de la requête HTTP : _sachet_exclude_domain
        # est appelé une dizaine de fois par chargement du dashboard.
        cached = getattr(request, '_mavie_sachet_variant_ids', None)
        if cached is not None:
            return cached
        collection_ids = self._get_sachet_collection_ids()
        variant_ids = []
        if collection_ids:
            # active_test=False des DEUX côtés : le template "SACHET A" est
            # archivé mais ses ventes historiques existent toujours. Sans ça
            # l'exclusion le laissait passer et Qté Vendue gonflait de
            # ~24 000 pièces par rapport à l'ancien filtre relationnel, qui
            # lui ignorait le flag actif.
            tmpl_ids = request.env['product.template'].sudo().with_context(
                active_test=False
            ).search([('collection_id', 'in', collection_ids)]).ids
            if tmpl_ids:
                variant_ids = request.env['product.product'].sudo().with_context(
                    active_test=False
                ).search([('product_tmpl_id', 'in', tmpl_ids)]).ids
        try:
            request._mavie_sachet_variant_ids = variant_ids
        except AttributeError:
            pass
        return variant_ids

    def _sachet_exclude_domain(self, path='collection_id'):
        """Domaine d'exclusion de la collection Sachet.

        Sur product.template on filtre directement collection_id. Sur les
        modèles de lignes (pos.order.line, purchase.order.line, stock.quant,
        sale.order.line...), on passe par les IDs de variantes plutôt que par
        le chemin relationnel, pour la raison de performance détaillée dans
        _get_sachet_variant_ids.
        """
        if path == 'collection_id':
            ids = self._get_sachet_collection_ids()
            return [(path, 'not in', ids)] if ids else []
        variant_ids = self._get_sachet_variant_ids()
        return [('product_id', 'not in', variant_ids)] if variant_ids else []

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
        # (voir _get_excluded_non_retail_ids : une société non-retail
        # explicitement cochée par l'utilisateur n'est plus exclue)
        excluded_non_retail_ids = self._get_excluded_non_retail_ids(kw)
        if excluded_non_retail_ids:
            domain.append(('order_id.company_id', 'not in', excluded_non_retail_ids))

        if product_tmpl_ids is not None:
            domain.append(('product_id.product_tmpl_id', 'in', product_tmpl_ids))

        if kw.get('date_start'):
            domain.append(('order_id.date_order', '>=', kw['date_start'] + ' 00:00:00'))
        if kw.get('date_end'):
            domain.append(('order_id.date_order', '<=', kw['date_end'] + ' 23:59:59'))

        if kw.get('shop_field'):
            scope = self._get_shop_scope(kw['shop_field'])
            if scope:
                if scope['company_id']:
                    domain.append(('order_id.company_id', '=', scope['company_id']))
                if scope['pos_config_ids'] is not None:
                    domain.append(('order_id.session_id.config_id', 'in', scope['pos_config_ids']))
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
        # (voir _get_excluded_non_retail_ids : une société non-retail
        # explicitement cochée par l'utilisateur n'est plus exclue)
        excluded_non_retail_ids = self._get_excluded_non_retail_ids(kw)
        if excluded_non_retail_ids:
            domain.append(('order_id.company_id', 'not in', excluded_non_retail_ids))
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
            # Un point de vente en ligne partage l'entrepôt de son magasin
            # physique : les achats affichés sont donc ceux de cet entrepôt
            # (il n'existe pas d'approvisionnement propre au canal en ligne).
            scope = self._get_shop_scope(kw['shop_field'])
            if scope:
                if scope['company_id']:
                    domain.append(('order_id.company_id', '=', scope['company_id']))
                if scope['warehouse']:
                    domain.append(('order_id.picking_type_id.warehouse_id', '=', scope['warehouse'].id))
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
            scope = self._get_shop_scope(shop_field)
            if scope:
                if scope['company_id']:
                    quant_domain.append(('company_id', '=', scope['company_id']))
                if scope['warehouse'] and scope['warehouse'].lot_stock_id:
                    quant_domain.append(('location_id', 'child_of', scope['warehouse'].lot_stock_id.id))
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

    # ─────────────────────────────────────────────────────────────
    # MAGASINS EN LIGNE (points de vente e-commerce)
    #
    # DEMANDE UTILISATEUR : les magasins "online" n'apparaissaient nulle
    # part dans le dashboard. Vérifié en base : chaque magasin physique a un
    # pos.config jumeau "Online – <magasin>" qui PARTAGE le même
    # picking_type/entrepôt (ex: config 2 "MAGASIN MARINA AGADIR" et
    # config 35 "Online – MARINA AGADIR" pointent tous deux sur l'entrepôt
    # 2). Le filtre magasin ciblant l'entrepôt, les ventes en ligne étaient
    # donc silencieusement fondues dans celles du magasin physique, sans
    # aucun moyen de les isoler. S'y ajoute DIGITAL SHOP (entrepôt 20,
    # 4 900 tickets) dont le mapping magasin est désactivé : il était
    # totalement absent de tous les KPIs.
    #
    # On expose désormais ces points de vente comme des "magasins" à part
    # entière dans le filtre, sous la clé `pos:<config_id>`, et le magasin
    # physique ne compte plus que ses propres tickets — chaque ligne du
    # filtre donne ainsi un chiffre exact, sans double comptage : la somme
    # physique + online reste égale au total société.
    # ─────────────────────────────────────────────────────────────

    ONLINE_SHOP_PREFIX = 'pos:'

    def _is_online_config_name(self, name):
        normalized = (name or '').strip().lower().replace('–', '-').replace('—', '-')
        return normalized.startswith('online') or 'digital' in normalized

    def _get_online_pos_configs(self):
        """pos.config considérés comme "magasin en ligne".

        Reconnaissance par le nom (préfixe « Online » ou « Digital »), le
        seul critère fiable en base : ces configs partagent l'entrepôt et le
        type d'opération de leur magasin physique, donc rien dans les
        relations ne permet de les distinguer.
        """
        configs = request.env['pos.config'].sudo().search([])
        return configs.filtered(lambda c: self._is_online_config_name(c.name))

    def _get_online_shop_entries(self):
        """Entrées de filtre magasin pour les points de vente en ligne."""
        entries = []
        for config in self._get_online_pos_configs():
            warehouse = config.picking_type_id.warehouse_id
            entries.append({
                'field': '%s%s' % (self.ONLINE_SHOP_PREFIX, config.id),
                'name': config.name,
                'company': config.company_id.name or '—',
                'warehouse': warehouse.name if warehouse else '—',
            })
        entries.sort(key=lambda e: (e['company'], e['name']))
        return entries

    def _get_shop_scope(self, shop_field):
        """Résout un choix du filtre magasin en périmètre technique.

        Retourne None si aucun magasin n'est filtré, sinon un dict :
          - kind            : 'shop' (magasin physique) ou 'online'
          - company_id      : société du point de vente
          - warehouse       : stock.warehouse (peut être vide)
          - pos_config_ids  : configs POS à retenir pour les ventes caisse
          - label           : libellé affichable
          - mapping         : mv.batch.shop.mapping (vide pour un online)

        Point clé : pour un magasin physique, `pos_config_ids` exclut
        explicitement les configs « Online » du même entrepôt, sinon les
        ventes en ligne resteraient comptées deux fois (une fois dans le
        magasin physique, une fois dans son entrée online dédiée).
        """
        if not shop_field:
            return None

        if str(shop_field).startswith(self.ONLINE_SHOP_PREFIX):
            try:
                config_id = int(str(shop_field)[len(self.ONLINE_SHOP_PREFIX):])
            except (TypeError, ValueError):
                return None
            config = request.env['pos.config'].sudo().browse(config_id)
            if not config.exists():
                return None
            return {
                'kind': 'online',
                'company_id': config.company_id.id if config.company_id else None,
                'warehouse': config.picking_type_id.warehouse_id,
                'pos_config_ids': [config.id],
                'label': config.name,
                'mapping': request.env['mv.batch.shop.mapping'].sudo().browse(),
            }

        mapping = request.env['mv.batch.shop.mapping'].sudo().search(
            [('shop_field', '=', shop_field)], limit=1
        )
        if not mapping:
            return None

        pos_config_ids = None
        if mapping.warehouse_id:
            configs = request.env['pos.config'].sudo().search([
                ('picking_type_id.warehouse_id', '=', mapping.warehouse_id.id)
            ])
            configs = configs.filtered(lambda c: not self._is_online_config_name(c.name))
            pos_config_ids = configs.ids
        return {
            'kind': 'shop',
            'company_id': mapping.company_id.id if mapping.company_id else None,
            'warehouse': mapping.warehouse_id,
            'pos_config_ids': pos_config_ids,
            'label': (mapping.warehouse_id.name if mapping.warehouse_id
                      else (mapping.shop_label or mapping.shop_field)),
            'mapping': mapping,
        }

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
            # Magasin en ligne (clé "pos:<id>") : pas de mv.batch.shop.mapping.
            scope = self._get_shop_scope(shop_field_filter)
            return scope['label'] if scope else 'Réseau'

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

    def _resolve_magasin_batch(self, product_tmpl_ids, shop_mappings, shop_field_filter=None,
                               mode='min', qty_by_tid=None, breakdown_by_tid=None):
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
            if filt_mapping:
                default_label = (
                    filt_mapping.warehouse_id.name if filt_mapping.warehouse_id
                    else (filt_mapping.shop_label or shop_field_filter)
                )
            else:
                # Magasin en ligne (clé "pos:<id>") : pas de mv.batch.shop.mapping.
                filt_scope = self._get_shop_scope(shop_field_filter)
                default_label = filt_scope['label'] if filt_scope else 'Réseau'
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
                q_grouped = self._group_sums('stock.quant', [
                    ('product_id', 'in', all_variant_ids),
                    ('location_id', 'child_of', sm.warehouse_id.lot_stock_id.id),
                ], ['quantity'])
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
                # Quantité réellement présente DANS ce magasin — sans elle,
                # l'écran affiche un magasin à côté d'un stock TOTAL réseau
                # et on croit que tout le stock y est (ex: 68 pièces
                # affichées face à "ARRIBAT CENTER" qui n'en a que 16).
                if qty_by_tid is not None:
                    qty_by_tid[tid] = int(shop_data[field_pick][0])
                # Répartition complète (magasins non vides, du plus gros au
                # plus petit) — shop_data est déjà calculé, donc aucune
                # requête supplémentaire. Permet d'afficher où se trouve le
                # reste du stock, pas seulement le magasin principal.
                if breakdown_by_tid is not None:
                    breakdown_by_tid[tid] = [
                        {'magasin': lbl, 'qty': int(q)}
                        for q, lbl in sorted(shop_data.values(), key=lambda x: -x[0])
                        if q
                    ]

        return magasin_by_tid

    def _resolve_magasin_stagnant(self, product_tmpl_ids, shop_mappings, breakdown_by_tid):
        """Magasin où le stock d'une référence n'a plus bougé depuis le plus
        longtemps (dernier mouvement le plus ancien), parmi ceux qui en
        détiennent encore.

        Pour du stock dormant, c'est l'information utile : savoir où la
        marchandise est réellement bloquée, plutôt que simplement où il y en
        a le plus. Une requête groupée par magasin (même coût que
        _resolve_magasin_batch), pas de N+1 par référence.
        """
        result = {}
        product_tmpl_ids = [t for t in product_tmpl_ids if breakdown_by_tid.get(t)]
        if not product_tmpl_ids:
            return result

        variant_rows = request.env['product.product'].sudo().search_read(
            [('product_tmpl_id', 'in', product_tmpl_ids)], ['id', 'product_tmpl_id']
        )
        tmpl_by_variant = {v['id']: v['product_tmpl_id'][0] for v in variant_rows if v.get('product_tmpl_id')}
        all_variant_ids = list(tmpl_by_variant.keys())
        if not all_variant_ids:
            return result

        # Dernier mouvement par (référence, magasin) : on regarde les
        # mouvements terminés touchant l'emplacement du magasin, dans un sens
        # comme dans l'autre.
        last_move = {}
        for sm in shop_mappings:
            if not sm.warehouse_id or not sm.warehouse_id.lot_stock_id:
                continue
            label = sm.warehouse_id.name or sm.shop_label or sm.shop_field
            loc_id = sm.warehouse_id.lot_stock_id.id
            grouped = self._group_sums('stock.move.line', [
                ('product_id', 'in', all_variant_ids),
                ('state', '=', 'done'),
                '|', ('location_id', 'child_of', loc_id), ('location_dest_id', 'child_of', loc_id),
            ], ['date'], agg='MAX')
            for g in grouped:
                pid = g['product_id'][0] if g.get('product_id') else None
                tid = tmpl_by_variant.get(pid)
                if not tid or not g.get('date'):
                    continue
                cur = last_move.setdefault(tid, {})
                if label not in cur or g['date'] > cur[label]:
                    cur[label] = g['date']

        now = datetime.now()
        for tid in product_tmpl_ids:
            rows = breakdown_by_tid.get(tid) or []
            # Uniquement les magasins qui détiennent encore du stock positif.
            rows = [r for r in rows if r['qty'] > 0]
            if not rows:
                continue
            dates_here = last_move.get(tid) or {}
            # Le plus ancien dernier-mouvement ; à défaut de date connue, on
            # retombe sur le magasin le plus chargé (rows est déjà trié).
            candidates = [(dates_here.get(r['magasin']), r) for r in rows if dates_here.get(r['magasin'])]
            if candidates:
                candidates.sort(key=lambda x: x[0])
                dt, row = candidates[0]
                if isinstance(dt, str):
                    try:
                        dt = datetime.strptime(dt[:19], '%Y-%m-%d %H:%M:%S')
                    except ValueError:
                        dt = None
                result[tid] = {
                    'magasin': row['magasin'], 'qty': row['qty'],
                    'days': (now - dt).days if dt else None,
                    'last_move': dt.strftime('%d/%m/%Y') if dt else None,
                }
            else:
                result[tid] = {'magasin': rows[0]['magasin'], 'qty': rows[0]['qty'],
                               'days': None, 'last_move': None}
        return result

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
                'online_shops': [],
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
                # Magasins en ligne : listés à part pour que le sélecteur
                # puisse les regrouper sous leur propre en-tête, et pour que
                # les listes destinées aux transferts inter-magasins
                # continuent de n'exposer que les magasins physiques (seuls
                # à avoir un entrepôt/société propres).
                filters['online_shops'] = self._get_online_shop_entries()
            except Exception as e:
                _logger.warning(f"Erreur magasins en ligne: {str(e)}")

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
            return {'error': str(e), 'collections': [], 'batches': [], 'shops': [],
                    'online_shops': [], 'categories': []}

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
                        'vendu_avec_cout': 0, 'marge': 0,
                        'tickets': 0, 'panier_moyen': 0,
                        'qty_sold': 0, 'qty_sold_normal': 0, 'qty_sold_solde': 0,
                        'soldes_count': 0, 'soldes_list': [],
                        'qty_purchased': 0, 'stock_total': 0,
                        'sell_through': 0, 'ruptures_count': 0, 'ruptures_list': [],
                        'top_products': [], 'flop_products': [],
                        'abc_analysis': {'A': [], 'B': [], 'C': []},
                        'references_count': 0, 'total_active_skus': 0,
                        'taux_rupture': 0, 'couverture_moy': 0,
                        'stock_dormant_pct': 0, 'dormant_count': 0, 'dormant_list': [], 'precision_inventaire': 99.5,
                        'ecarts_inventaire_pct': 0.0, 'ecarts_refs_count': 0, 'ecarts_qty_manquante': 0,
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
            pos_grouped = self._group_sums(
                'pos.order.line', pos_domain,
                ['price_subtotal_incl', 'price_subtotal', 'qty'],
            )
            ca_total = sum(g.get('price_subtotal_incl') or 0.0 for g in pos_grouped)
            # CA hors taxe — utilisé pour comparer à un coût (HT lui aussi),
            # afin que la marge ne soit pas gonflée artificiellement du
            # montant de la TVA (voir ca_achat_total / vendu_avec_cout_total
            # / marge_total plus bas).
            ca_ht_total = sum(g.get('price_subtotal') or 0.0 for g in pos_grouped)
            qty_sold_total = int(sum(g.get('qty') or 0 for g in pos_grouped))

            # Ventes d'une société non-retail explicitement cochée : elles
            # passent par sale.order, pas par le POS (voir
            # _build_non_retail_sale_domain). Ajoutées au CA Vendu pour que
            # cocher MOD FOR LIFE ait le même effet que sur le CA Achat.
            # Regroupé par produit (pas seulement en total) : ces ventes
            # doivent aussi remonter dans le détail par référence
            # (sales_by_tmpl -> Top/Flop, ABC, ruptures), sinon les cartes
            # afficheraient MOD FOR LIFE mais pas les tableaux en dessous.
            so_by_product = {}
            if self._get_explicit_non_retail_ids(kw):
                so_grouped = request.env['sale.order.line'].sudo().read_group(
                    self._build_non_retail_sale_domain(kw, product_tmpl_ids),
                    ['product_uom_qty:sum', 'price_total:sum', 'price_subtotal:sum', 'product_id'],
                    ['product_id'], lazy=False
                )
                for g in so_grouped:
                    ca_total += g.get('price_total') or 0.0
                    ca_ht_total += g.get('price_subtotal') or 0.0
                    qty_sold_total += int(g.get('product_uom_qty') or 0)
                    pid = g['product_id'][0] if g.get('product_id') else None
                    if pid:
                        so_by_product[pid] = {
                            'qty': g.get('product_uom_qty') or 0.0,
                            'ca': g.get('price_total') or 0.0,
                        }

            # Qté vendue "en solde" = lignes vendues à un prix effectif
            # (remise incluse) inférieur au prix catalogue (list_price) du
            # produit — détection par prix, pas par date. read_group ne peut
            # pas comparer deux champs entre eux (price_unit*(1-discount/100)
            # vs list_price d'un modèle lié), donc agrégation SQL directe,
            # restreinte aux IDs déjà filtrés par pos_domain (pas de 2e scan
            # complet de pos.order.line).
            qty_sold_solde_total = 0
            soldes_products = []
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

                # Détail par référence pour la liste cliquable "articles
                # vendus en solde" : mêmes lignes et même critère que le
                # compteur ci-dessus (un seul scan, restreint aux mêmes IDs),
                # regroupées par produit avec le prix moyen réellement
                # encaissé pour pouvoir le comparer au prix catalogue.
                request.env.cr.execute("""
                    SELECT pt.id,
                           SUM(pol.qty) AS qty_solde,
                           SUM(pol.price_subtotal_incl) AS ca_solde,
                           MAX(pt.list_price) AS list_price,
                           SUM(pol.price_subtotal) AS sous_total_ht
                    FROM pos_order_line pol
                    JOIN product_product pp ON pp.id = pol.product_id
                    JOIN product_template pt ON pt.id = pp.product_tmpl_id
                    WHERE pol.id IN %s AND pt.list_price > 0
                      AND pol.price_unit * (1 - COALESCE(pol.discount, 0) / 100.0) < pt.list_price * 0.999
                    GROUP BY pt.id
                    HAVING SUM(pol.qty) > 0
                """, (tuple(pos_line_ids_for_solde),))
                solde_rows = request.env.cr.fetchall()
                if solde_rows:
                    solde_tmpl_ids = [r[0] for r in solde_rows]
                    solde_tmpl_data = request.env['product.template'].sudo().search_read(
                        [('id', 'in', solde_tmpl_ids)],
                        ['name', 'default_code', 'base_pivot_reference']
                    )
                    solde_tmpl_by_id = {t['id']: t for t in solde_tmpl_data}
                    for tid, qty_s, ca_s, lp, sous_total_ht in solde_rows:
                        t = solde_tmpl_by_id.get(tid, {})
                        # Prix en TTC : même base que le CA encaissé affiché
                        # à côté, et que ce que le client règle en caisse.
                        ratio = self._ttc_ratio(sous_total_ht, ca_s)
                        lp = (lp or 0.0) * ratio
                        prix_moyen = self._unit_price_ttc(ca_s, qty_s)
                        soldes_products.append({
                            'id': tid,
                            'name': t.get('name') or '—',
                            'ref': (t.get('base_pivot_reference') or t.get('default_code')
                                    or t.get('name') or '—'),
                            'qty_solde': int(qty_s or 0),
                            'ca_solde': round(ca_s or 0.0, 2),
                            'prix_catalogue': round(lp, 2),
                            'prix_moyen_paye': round(prix_moyen, 2),
                            'remise_pct': round((lp - prix_moyen) / lp * 100, 1) if lp > 0 else 0.0,
                        })
                    soldes_products.sort(key=lambda x: -x['qty_solde'])
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

            # Ventes inter-sociétés de la société non-retail cochée, injectées
            # dans le MÊME dictionnaire par référence que les ventes POS —
            # sans ça, les cartes du haut incluaient MOD FOR LIFE mais les
            # tableaux Top/Flop, ABC et ruptures affichaient encore les seules
            # ventes magasins, ce qui était contradictoire à l'écran.
            if so_by_product:
                so_prods = request.env['product.product'].sudo().search_read(
                    [('id', 'in', list(so_by_product.keys()))],
                    ['id', 'product_tmpl_id']
                )
                for p in so_prods:
                    tid = p['product_tmpl_id'][0] if p.get('product_tmpl_id') else None
                    if not tid:
                        continue
                    prod_to_tmpl.setdefault(p['id'], tid)
                    data_so = so_by_product.get(p['id']) or {}
                    if tid not in sales_by_tmpl:
                        sales_by_tmpl[tid] = {'qty': 0, 'ca': 0.0}
                    sales_by_tmpl[tid]['qty'] += data_so.get('qty') or 0
                    sales_by_tmpl[tid]['ca'] += data_so.get('ca') or 0.0

            # ✅ Idem achats : un seul scan groupé par produit, le total
            # s'obtient en sommant les groupes au lieu d'un 2e scan complet
            # (po_agg supprimé).
            purchase_domain = self._build_purchase_domain(kw, product_tmpl_ids)
            po_grouped = self._group_sums(
                'purchase.order.line', purchase_domain,
                ['product_qty', 'price_total', 'price_subtotal'],
            )
            qty_purchased_total = int(sum(g.get('product_qty') or 0 for g in po_grouped))
            # CA Achat = coût réel des marchandises achetées, tel que facturé
            # sur les bons de commande (pas une reconstruction
            # qty*prix_standard). DÉCISION UTILISATEUR (2026-08-18) : affiché
            # en TTC comme tous les autres CA du dashboard. La version HT
            # reste calculée à côté car la marge doit comparer du HT à du HT.
            ca_achat_total = sum(g.get('price_total') or 0.0 for g in po_grouped)
            ca_achat_ht_total = sum(g.get('price_subtotal') or 0.0 for g in po_grouped)

            # NB (décision utilisateur 2026-08-17) : la répartition du CA Achat
            # en externe/interne (MOD FOR LIFE) et par société magasin a été
            # retirée — le sélecteur de société standard permet déjà de voir
            # chaque société séparément, la carte doit rester un total général.
            # Les 2 read_group supplémentaires que ça demandait ont été
            # supprimés avec, pas seulement masqués côté affichage.

            # Sell-through = part du stock reçu qui a été vendue : vendu / acheté.
            # (PAS vendu / (vendu + acheté), qui donnait un chiffre bien trop bas.)
            sell_through = round((qty_sold_total / qty_purchased_total * 100), 1) if qty_purchased_total > 0 else 0.0

            po_pids = [g['product_id'][0] for g in po_grouped if g.get('product_id')]
            purchase_by_tmpl = {}
            # CA Achat par référence — déjà présent dans po_grouped
            # (price_subtotal), il suffit de l'accumuler comme les quantités
            # pour pouvoir l'afficher dans les tableaux Top/Flop.
            ca_achat_by_tmpl = {}
            ca_achat_ht_by_tmpl = {}
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
                    # TTC pour l'affichage (colonne CA Achat du Top/Flop)
                    ca_achat_by_tmpl[tid] = ca_achat_by_tmpl.get(tid, 0.0) + (g.get('price_total') or 0.0)
                    # HT pour l'estimation du coût unitaire (valorisation) :
                    # un coût de stock se raisonne hors taxes.
                    ca_achat_ht_by_tmpl[tid] = ca_achat_ht_by_tmpl.get(tid, 0.0) + (g.get('price_subtotal') or 0.0)

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
            shop_scope = self._get_shop_scope(shop_field)
            if shop_field:
                if shop_scope:
                    if shop_scope['company_id']:
                        quant_domain_base.append(('company_id', '=', shop_scope['company_id']))
                    if shop_scope['warehouse'] and shop_scope['warehouse'].lot_stock_id:
                        quant_domain_base.append(
                            ('location_id', 'child_of', shop_scope['warehouse'].lot_stock_id.id)
                        )
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

            # DÉCISION UTILISATEUR (2026-08-15) : les entrepôts hors des
            # magasins actifs configurés (DIGITAL SHOP, MAGASIN ORANGER
            # désactivé) ne doivent compter dans AUCUN total réseau retail —
            # jusqu'ici seule la fiche produit appliquait cette règle
            # (stock_by_store/retail_lot_stock_ids), le dashboard principal
            # comptait ces 2 entrepôts via le seul filtre société, créant un
            # écart vérifié de 568 pièces sur "Stock Réel Odoo" à l'échelle
            # du réseau. Même restriction que _compute_product_detail :
            # limiter aux lot_stock_id des entrepôts réellement mappés.
            # N'affecte PAS quant_domain_base (valorisation), qui doit
            # rester "toutes sociétés, tout entrepôt" par design (cf. plus haut).
            #
            # Même règle qu'en fiche produit (décision 2026-08-18) : une
            # société non-retail explicitement cochée voit son entrepôt
            # compté, sinon cocher la société n'aurait aucun effet visible.
            Warehouse = request.env['stock.warehouse'].sudo()
            excluded_non_retail_ids = self._get_excluded_non_retail_ids(kw)
            scoped_warehouses = Warehouse.search([
                ('company_id', 'not in', self._get_non_retail_company_ids()),
                ('id', 'in', self._get_active_shop_mappings().mapped('warehouse_id').ids),
            ])
            explicit_non_retail_ids = [
                cid for cid in self._get_non_retail_company_ids()
                if cid not in excluded_non_retail_ids
            ]
            if explicit_non_retail_ids:
                scoped_warehouses |= Warehouse.search([
                    ('company_id', 'in', explicit_non_retail_ids),
                ])
            # Un magasin en ligne explicitement sélectionné doit voir son
            # entrepôt compté même s'il n'a pas de mapping magasin actif
            # (cas DIGITAL SHOP, entrepôt 20) — sans quoi le filtre
            # renverrait 0 partout alors que ce point de vente a bien du
            # stock et des ventes.
            if shop_scope and shop_scope['kind'] == 'online' and shop_scope['warehouse']:
                scoped_warehouses |= shop_scope['warehouse']
            retail_lot_stock_ids = scoped_warehouses.mapped('lot_stock_id').ids

            quant_domain = quant_domain_base
            if excluded_non_retail_ids:
                quant_domain = quant_domain + [('company_id', 'not in', excluded_non_retail_ids)]
            if retail_lot_stock_ids:
                quant_domain = quant_domain + [('location_id', 'child_of', retail_lot_stock_ids)]

            quant_grouped = self._group_sums('stock.quant', quant_domain, ['quantity'])
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
                    'ca_achat': round(ca_achat_by_tmpl.get(tmpl.id, 0.0), 2),
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
                if shop_scope and shop_scope['company_id']:
                    quant_domain_refs.append(('company_id', '=', shop_scope['company_id']))
                else:
                    quant_domain_refs.append(('company_id', 'not in', self._get_non_retail_company_ids()))
                if shop_scope and shop_scope['warehouse'] and shop_scope['warehouse'].lot_stock_id:
                    quant_domain_refs.append(
                        ('location_id', 'child_of', shop_scope['warehouse'].lot_stock_id.id)
                    )
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
                    # Même règle que stock_total : un entrepôt hors magasins
                    # actifs mappés (retail_lot_stock_ids, calculé plus haut)
                    # ne doit pas suffire à compter une référence comme
                    # "présente" dans le réseau.
                    refs_domain = [
                        ('location_id.usage', '=', 'internal'),
                        ('company_id', 'in', context_company_ids),
                    ]
                    if retail_lot_stock_ids:
                        refs_domain.append(('location_id', 'child_of', retail_lot_stock_ids))
                    tmpl_ids_ctx = request.env['stock.quant'].sudo().search(refs_domain).mapped(
                        'product_id.product_tmpl_id'
                    ).ids
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

            # ── Écarts d'inventaire (remplace l'affichage "Précision") ──
            # DEMANDE UTILISATEUR : l'indicateur doit valoir 0 % quand tout
            # est sain, et être cliquable dès qu'il grimpe. Le comptage
            # d'ajustements ci-dessus donnait une "précision" à ~99 % qui
            # masquait le vrai problème : des références en stock NÉGATIF,
            # c'est-à-dire plus de sorties enregistrées que d'entrées.
            ecarts = self._inventory_anomaly_summary(kw)
            ecarts_refs_count = ecarts['refs_count']
            ecarts_qty = ecarts['qty_manquante']
            ecarts_inventaire_pct = round(
                (ecarts_refs_count / (total_active_skus or 1)) * 100, 1
            ) if ecarts_refs_count else 0.0

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
            # DÉCISION UTILISATEUR (2026-08-18) : pour du stock DORMANT, le
            # magasin utile n'est pas celui qui en a le plus, mais celui où
            # la marchandise n'a plus bougé depuis le plus longtemps — c'est
            # là que le stock est réellement bloqué. On calcule donc, par
            # référence et par magasin, la date du dernier mouvement de
            # stock, et on retient le magasin le plus ancien.
            qty_in_magasin_dormant = {}
            breakdown_dormant = {}
            self._resolve_magasin_batch(
                [d['id'] for d in dormant_products], shop_mappings, kw.get('shop_field'), mode='max',
                qty_by_tid=qty_in_magasin_dormant, breakdown_by_tid=breakdown_dormant
            )
            stagnant = self._resolve_magasin_stagnant(
                [d['id'] for d in dormant_products], shop_mappings, breakdown_dormant
            )
            for d in dormant_products:
                info = stagnant.get(d['id']) or {}
                d['magasin'] = info.get('magasin') or 'Réseau'
                # Quantité présente dans CE magasin ('stock' reste le total
                # réseau) et ancienneté du dernier mouvement qui s'y est
                # produit — les deux sont affichés séparément à l'écran.
                d['magasin_qty'] = info.get('qty')
                d['magasin_days'] = info.get('days')
                d['magasin_last_move'] = info.get('last_move')
                d['magasin_breakdown'] = breakdown_dormant.get(d['id']) or []

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
            # BUG CORRIGÉ (2026-08-18) : la valorisation gardait TOUTES les
            # sociétés en permanence, y compris MOD FOR LIFE. Résultat, sans
            # filtre société, le même écran affichait "Stock Réel Odoo"
            # = 20 841 pièces (retail seul) mais une valorisation incluant
            # les 8 361 pièces de l'entrepôt MOD FOR LIFE, avec une ligne
            # "MOD FOR LIFE" dans le tableau par magasin — deux périmètres
            # différents côte à côte, sans rien pour le signaler.
            # Elle suit désormais la même règle que partout ailleurs
            # (_get_excluded_non_retail_ids) : MOD FOR LIFE n'est comptée que
            # si l'utilisateur la coche explicitement.
            val_ht_total = 0.0
            val_cost_total = 0.0
            stock_val_by_store = []

            quant_domain_valorisation = quant_domain_base
            if excluded_non_retail_ids:
                quant_domain_valorisation = quant_domain_base + [
                    ('company_id', 'not in', excluded_non_retail_ids)
                ]

            quant_val_grouped = self._group_sums(
                'stock.quant', quant_domain_valorisation, ['quantity'],
                group_fields=('product_id', 'company_id'),
            )

            val_pids = [g['product_id'][0] for g in quant_val_grouped if g.get('product_id')]
            val_cost_estime = False
            if val_pids:
                val_prods = request.env['product.product'].sudo().search_read(
                    [('id', 'in', val_pids)],
                    ['id', 'list_price', 'standard_price', 'product_tmpl_id']
                )
                val_prod_map = {p['id']: p for p in val_prods}
                company_name_by_id = {
                    c.id: c.name
                    for c in request.env['res.company'].sudo().search([])
                }

                # DÉCISION UTILISATEUR (2026-08-18) : le champ "Coût" n'est
                # renseigné que sur 12 références sur 4972, d'où une "Valeur
                # au coût" à 0,00 quasiment partout. Quand ce champ est vide
                # mais qu'on connaît le PRIX D'ACHAT RÉELLEMENT PAYÉ sur les
                # commandes fournisseur (même source que CA Achat), on
                # l'utilise comme repli plutôt que d'afficher zéro. La valeur
                # est alors signalée comme estimée (val_cost_estime) pour ne
                # pas la confondre avec un coût réellement saisi.
                prix_achat_moyen_by_tmpl = {}
                for tid, qte in purchase_by_tmpl.items():
                    montant = ca_achat_ht_by_tmpl.get(tid) or 0.0
                    if qte and montant > 0:
                        prix_achat_moyen_by_tmpl[tid] = montant / qte

                val_by_company = {}
                for g in quant_val_grouped:
                    pid = g['product_id'][0] if g.get('product_id') else None
                    # _group_sums ne renvoie pas les libellés (voir son
                    # docstring) : on résout les noms de sociétés à part,
                    # il n'y en a qu'une poignée.
                    cid = g['company_id'][0] if g.get('company_id') else None
                    qty = g.get('quantity') or 0.0
                    if qty <= 0 or not pid or pid not in val_prod_map:
                        continue
                    p_data = val_prod_map[pid]
                    price_ht = p_data.get('list_price') or 0.0
                    cost_price = p_data.get('standard_price') or 0.0
                    if not cost_price:
                        tmpl_ref = p_data.get('product_tmpl_id')
                        tmpl_id_val = tmpl_ref[0] if tmpl_ref else None
                        fallback = prix_achat_moyen_by_tmpl.get(tmpl_id_val)
                        if fallback:
                            cost_price = fallback
                            val_cost_estime = True

                    v_ht = qty * price_ht
                    v_cost = qty * cost_price

                    val_ht_total += v_ht
                    val_cost_total += v_cost

                    if cid not in val_by_company:
                        val_by_company[cid] = {'ht': 0.0, 'cost': 0.0, 'qty': 0}
                    val_by_company[cid]['ht'] += v_ht
                    val_by_company[cid]['cost'] += v_cost
                    val_by_company[cid]['qty'] += int(qty)

                for comp_id, vdata in val_by_company.items():
                    stock_val_by_store.append({
                        # company_id permet au clic d'ouvrir le détail par
                        # magasin de CETTE société (api_valorisation_detail),
                        # calculé à la demande plutôt qu'en doublant le coût
                        # du calcul principal (le regroupement par
                        # emplacement double le nombre de groupes : 127 050
                        # → 248 528, mesuré en base).
                        'company_id': comp_id,
                        'store_name': company_name_by_id.get(comp_id, 'Société'),
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

            # Photos du Top/Flop — résolues en 2 requêtes pour l'ensemble des
            # lignes affichées, pas une par ligne.
            top_flop_ids = [p['id'] for p in top_products] + [p['id'] for p in flop_products]
            image_sources = self._image_availability(set(top_flop_ids))
            for p in list(top_products) + list(flop_products):
                src = image_sources.get(p['id'])
                p['image_url'] = self._image_url(p['id'], src)
                p['has_image'] = bool(src)

            return {
                'ca_total': round(ca_total, 2),
                'ca_ht': round(ca_ht_total, 2),
                'ca_achat': round(ca_achat_total, 2),
                'vendu_avec_cout': round(vendu_avec_cout_total, 2),
                'marge': round(marge_total, 2),
                'tickets': tickets,
                'references_count': references_count,
                'panier_moyen': round(panier_moyen, 2),
                'qty_sold': qty_sold_total,
                'qty_sold_normal': qty_sold_normal_total,
                'qty_sold_solde': qty_sold_solde_total,
                'soldes_count': len(soldes_products),
                'soldes_list': soldes_products[:500],
                'qty_purchased': qty_purchased_total,
                'stock_total': stock_total,
                'valeur_stock_ht': round(val_ht_total, 2),
                'valeur_stock_cost': round(val_cost_total, 2),
                'valeur_stock_cost_estime': val_cost_estime,
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
                'ecarts_inventaire_pct': ecarts_inventaire_pct,
                'ecarts_refs_count': ecarts_refs_count,
                'ecarts_qty_manquante': ecarts_qty,
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

            # BUG CORRIGÉ (2026-08-18) : cette vue ignorait complètement les
            # filtres Collection / Batch / Catégorie de la barre du haut —
            # sélectionner une collection laissait les 4 cartes afficher le
            # catalogue entier, sans que rien ne l'indique à l'écran. On
            # applique désormais le même filtre produit que la vue retail.
            product_tmpl_ids = None
            if kw.get('collection_id') or kw.get('batch_id') or kw.get('categ_id'):
                product_tmpl_ids = request.env['product.template'].sudo().search(
                    self._build_product_domain(kw)
                ).ids
                if not product_tmpl_ids:
                    product_tmpl_ids = [-1]

            # ── Achats fournisseurs : bons de commande de MOD FOR LIFE dont
            # le fournisseur n'est PAS une des sociétés magasins (donc un
            # vrai fournisseur externe, pas un flux inter-société).
            po_domain = [
                ('order_id.state', 'in', ['purchase', 'done']),
                ('order_id.company_id', '=', mod_for_life.id),
                ('partner_id', 'not in', retail_partner_ids),
            ]
            po_domain += self._sachet_exclude_domain('product_id.product_tmpl_id.collection_id')
            if product_tmpl_ids is not None:
                po_domain.append(('product_id.product_tmpl_id', 'in', product_tmpl_ids))
            if kw.get('date_start'):
                po_domain.append(('order_id.date_order', '>=', kw['date_start'] + ' 00:00:00'))
            if kw.get('date_end'):
                po_domain.append(('order_id.date_order', '<=', kw['date_end'] + ' 23:59:59'))

            po_grouped = request.env['purchase.order.line'].sudo().read_group(
                po_domain, ['product_qty:sum', 'price_total:sum'], [], lazy=False
            )
            qty_achats_fournisseurs = int(po_grouped[0].get('product_qty') or 0) if po_grouped else 0

            # TTC, comme tous les CA du dashboard (décision 2026-08-18).
            ca_achats_fournisseurs = round(po_grouped[0].get('price_total') or 0.0, 2) if po_grouped else 0.0

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
            if product_tmpl_ids is not None:
                so_domain.append(('product_id.product_tmpl_id', 'in', product_tmpl_ids))
            if kw.get('date_start'):
                so_domain.append(('order_id.date_order', '>=', kw['date_start'] + ' 00:00:00'))
            if kw.get('date_end'):
                so_domain.append(('order_id.date_order', '<=', kw['date_end'] + ' 23:59:59'))

            so_grouped = request.env['sale.order.line'].sudo().read_group(
                so_domain,
                ['product_uom_qty:sum', 'price_total:sum', 'order_partner_id'],
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
                ca = g.get('price_total') or 0.0
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
            if product_tmpl_ids is not None:
                quant_domain.append(('product_id.product_tmpl_id', 'in', product_tmpl_ids))
            quant_grouped = request.env['stock.quant'].sudo().read_group(
                quant_domain, ['quantity:sum'], [], lazy=False
            )
            stock_entrepot = int(sum(g.get('quantity') or 0 for g in quant_grouped)) if quant_grouped else 0

            # DÉCISION UTILISATEUR (2026-08-19) : cette vue reste en PIÈCES.
            # Une conversion en cartons avait été ajoutée, puis retirée :
            # vérifié en base, aucune donnée ne dit combien de pièces tient
            # dans un carton hors chaussures (product_packaging vide, pas
            # d'unité "carton", et sur les articles Base Pivot de famille SAC
            # la colonne « Qté Colis » contient déjà le nombre de sacs). Le
            # nombre de cartons aurait donc été égal au nombre de pièces,
            # c'est-à-dire faux.
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

    # ─────────────────────────────────────────────────────────────
    # EXPORTS EXCEL AVEC PHOTOS INCRUSTÉES
    #
    # DEMANDE UTILISATEUR : « ya pas de photo dans l'excel ».
    # Première tentative : une formule =IMAGE("url") dans un CSV. Elle
    # renvoie #NOM? (#NAME? en anglais) car la fonction IMAGE n'existe que
    # depuis Excel 365 — et même là, l'URL pointe vers Odoo, qui exige une
    # session authentifiée : Excel ne pourrait pas la charger.
    #
    # La seule façon d'avoir réellement les photos dans le fichier est de
    # les y incruster, ce qu'un CSV ne sait pas faire (c'est du texte pur).
    # On produit donc un vrai classeur .xlsx (xlsxwriter, déjà présent dans
    # l'image Odoo) avec les images embarquées. Le CSV reste accessible via
    # ?format=csv pour qui veut les données brutes.
    # ─────────────────────────────────────────────────────────────

    _XLSX_PHOTO_PX = 72          # taille d'affichage de la vignette
    _XLSX_ROW_HEIGHT = 56        # hauteur de ligne (points) pour la loger
    _XLSX_PHOTO_COL_WIDTH = 12

    def _photo_bytes_by_tmpl(self, image_sources, field='image_128'):
        """{product_tmpl_id: bytes de l'image} pour les vraies photos seulement.

        `image_sources` vient de _image_availability. Deux sources possibles,
        lues en deux requêtes groupées : la fiche produit (image_128, déjà
        à la bonne taille) et l'article Base Pivot (image_1920, redimensionné
        ici pour ne pas alourdir le classeur).
        """
        result = {}
        if not image_sources:
            return result

        tmpl_ids = [tid for tid, src in image_sources.items() if src == 'product']
        if tmpl_ids:
            for rec in request.env['product.template'].sudo().browse(tmpl_ids).read([field]):
                if rec.get(field):
                    try:
                        result[rec['id']] = base64.b64decode(rec[field])
                    except Exception:
                        continue

        article_by_tmpl = {
            tid: int(src.split(':', 1)[1])
            for tid, src in image_sources.items()
            if src and src.startswith('article:')
        }
        if article_by_tmpl:
            tmpl_by_article = {aid: tid for tid, aid in article_by_tmpl.items()}
            try:
                records = request.env['mv.article.base'].sudo().browse(
                    list(tmpl_by_article)).read(['image_1920'])
            except Exception as e:
                _logger.warning("Photos Base Pivot illisibles: %s", e)
                records = []
            for rec in records:
                if not rec.get('image_1920'):
                    continue
                raw = base64.b64decode(rec['image_1920'])
                result[tmpl_by_article[rec['id']]] = self._shrink_image(raw)
        return result

    def _shrink_image(self, raw, box=256):
        """Réduit une image pour l'export ; renvoie l'original si Pillow échoue."""
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(raw))
            img.thumbnail((box, box))
            if img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
            buf = io.BytesIO()
            img.save(buf, format='PNG')
            return buf.getvalue()
        except Exception as e:
            _logger.warning("Redimensionnement image impossible: %s", e)
            return raw

    def _xlsx_workbook(self):
        """Classeur en mémoire + jeu de formats partagé par tous les exports."""
        import xlsxwriter
        stream = io.BytesIO()
        book = xlsxwriter.Workbook(stream, {'in_memory': True})
        formats = {
            'title': book.add_format({'bold': True, 'font_size': 14, 'font_color': '#4C1D95'}),
            'meta': book.add_format({'font_color': '#64748B'}),
            'header': book.add_format({
                'bold': True, 'bg_color': '#EDE9FE', 'font_color': '#4C1D95',
                'border': 1, 'border_color': '#DDD6FE', 'align': 'center', 'valign': 'vcenter',
                'text_wrap': True,
            }),
            'cell': book.add_format({'valign': 'vcenter'}),
            'num': book.add_format({'valign': 'vcenter', 'num_format': '#,##0'}),
            'money': book.add_format({'valign': 'vcenter', 'num_format': '#,##0.00'}),
            'muted': book.add_format({'valign': 'vcenter', 'font_color': '#94A3B8'}),
        }
        return stream, book, formats

    def _xlsx_insert_photo(self, sheet, row, col, image_bytes, index):
        """Incruste une vignette centrée dans la cellule."""
        if not image_bytes:
            return
        scale = self._XLSX_PHOTO_PX / 128.0
        sheet.insert_image(row, col, 'photo_%s.png' % index, {
            'image_data': io.BytesIO(image_bytes),
            'x_scale': scale,
            'y_scale': scale,
            'x_offset': 4,
            'y_offset': 3,
            'object_position': 1,   # l'image suit la cellule (tri/filtre)
        })

    def _xlsx_response(self, stream, book, filename):
        book.close()
        payload = stream.getvalue()
        stream.close()
        return request.make_response(
            payload,
            headers=[
                ('Content-Type',
                 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
                ('Content-Disposition', 'attachment; filename="%s"' % filename),
                ('Content-Length', len(payload)),
            ],
        )

    @http.route('/mavie/api/top-flop/export', type='http', auth='user', methods=['GET'], csrf=False)
    def api_top_flop_export(self, **kw):
        """Export du Top ou Flop Produits, mêmes filtres et même limite
        (top_limit / flop_limit) qu'à l'écran.

        Classeur .xlsx par défaut, photos incrustées. `?format=csv` renvoie
        l'ancien CSV (données brutes, sans photo — un CSV est du texte pur).
        """
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
        titre = 'Top Produits' if kind != 'flop' else 'Flop Produits'
        base_name = 'flop_produits' if kind == 'flop' else 'top_produits'
        columns = ['#', 'Photo', 'Produit', 'Réf', 'CA Achat (TTC)', 'CA Vendu (TTC)',
                   'Qté achetée', 'Qté vendue', 'Reste', 'Stock']

        def _values(row):
            # Reste = acheté − vendu, même définition qu'à l'écran.
            qty_sold_row = row.get('qty_sold', row.get('qty', 0)) or 0
            qty_purchased_row = row.get('qty_purchased', 0) or 0
            return [
                row.get('rank'), row.get('name'), row.get('ref'),
                row.get('ca_achat', 0), row.get('ca', 0),
                qty_purchased_row, qty_sold_row,
                qty_purchased_row - qty_sold_row, row.get('stock', 0),
            ]

        if (kw.get('format') or 'xlsx').lower() == 'csv':
            buffer = io.StringIO()
            buffer.write(u'﻿')  # BOM pour qu'Excel détecte l'UTF-8
            writer = csv.writer(buffer, delimiter=';')
            writer.writerow([titre])
            # En CSV la photo ne peut être qu'un lien : le format ne sait pas
            # porter d'image. Le classeur .xlsx, lui, les incruste.
            writer.writerow(['#', 'URL photo', 'Produit', 'Réf', 'CA Achat (TTC)',
                             'CA Vendu (TTC)', 'Qté achetée', 'Qté vendue', 'Reste', 'Stock'])
            for row in rows:
                vals = _values(row)
                photo_url = self._absolute_url(row.get('image_url')) if row.get('has_image') else ''
                writer.writerow([vals[0], photo_url] + vals[1:])
            return request.make_response(
                buffer.getvalue(),
                headers=[
                    ('Content-Type', 'text/csv; charset=utf-8'),
                    ('Content-Disposition',
                     'attachment; filename="mavie_export_%s.csv"' % base_name),
                ],
            )

        photos = self._photo_bytes_by_tmpl(self._image_availability({r['id'] for r in rows}))
        stream, book, fmt = self._xlsx_workbook()
        sheet = book.add_worksheet(titre[:31])
        sheet.write(0, 0, titre, fmt['title'])
        sheet.write(1, 0, '%s référence(s) — %s avec photo'
                    % (len(rows), sum(1 for r in rows if photos.get(r['id']))), fmt['meta'])
        for col, label in enumerate(columns):
            sheet.write(3, col, label, fmt['header'])
        sheet.set_column(1, 1, self._XLSX_PHOTO_COL_WIDTH)
        sheet.set_column(2, 2, 34)
        sheet.set_column(3, 3, 16)
        sheet.set_column(4, 9, 15)
        sheet.freeze_panes(4, 0)

        for idx, row in enumerate(rows):
            excel_row = 4 + idx
            sheet.set_row(excel_row, self._XLSX_ROW_HEIGHT)
            vals = _values(row)
            sheet.write(excel_row, 0, vals[0], fmt['cell'])
            image_bytes = photos.get(row['id'])
            if image_bytes:
                self._xlsx_insert_photo(sheet, excel_row, 1, image_bytes, idx)
            else:
                sheet.write(excel_row, 1, 'Aucune photo', fmt['muted'])
            for offset, value in enumerate(vals[1:], start=2):
                style = fmt['money'] if offset in (4, 5) else (
                    fmt['num'] if offset >= 6 else fmt['cell'])
                sheet.write(excel_row, offset, value, style)

        return self._xlsx_response(stream, book, 'mavie_export_%s.xlsx' % base_name)

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
                    return {'daily': [], 'by_shop': []}
                product_tmpl_ids = products.ids

            pos_domain = self._build_pos_domain(kw, product_tmpl_ids)

            # Une seule agrégation SQL alimente les deux modes du graphique :
            # "Par Arrivage" (somme sur tous les points de vente) et
            # "Par Magasin" (détail par point de vente). Auparavant deux
            # read_group distincts, dont un par commande — voir
            # _pos_sales_by_product_and_config pour le coût mesuré.
            rows = self._pos_sales_by_product_and_config(pos_domain)
            if not rows:
                return {'daily': [], 'by_shop': []}

            pids = list({r[0] for r in rows if r[0]})
            prods = request.env['product.product'].sudo().search_read(
                [('id', 'in', pids)], ['id', 'product_tmpl_id']
            )
            prod_to_tmpl = {p['id']: p['product_tmpl_id'][0] for p in prods if p.get('product_tmpl_id')}

            tmpls = request.env['product.template'].sudo().search_read(
                [('id', 'in', list(set(prod_to_tmpl.values())))], ['id', 'arrivage_id']
            )
            tmpl_to_arrivage = {
                t['id']: t['arrivage_id'][1] for t in tmpls if t.get('arrivage_id')
            }

            # Libellé du magasin : nom du point de vente, repli sur la société
            # (même règle qu'auparavant). Quelques dizaines d'enregistrements,
            # résolus en deux requêtes au lieu d'une par commande.
            config_ids = list({r[1] for r in rows if r[1]})
            config_name_by_id = {
                c['id']: c['name'] for c in request.env['pos.config'].sudo().search_read(
                    [('id', 'in', config_ids)], ['id', 'name'])
            } if config_ids else {}
            company_ids = list({r[2] for r in rows if r[2]})
            company_name_by_id = {
                c['id']: c['name'] for c in request.env['res.company'].sudo().search_read(
                    [('id', 'in', company_ids)], ['id', 'name'])
            } if company_ids else {}

            sales_by_arrivage = {}
            by_shop_map = {}
            for product_id, config_id, company_id, ca, qty in rows:
                tid = prod_to_tmpl.get(product_id)
                if not tid:
                    continue
                arrivage_name = tmpl_to_arrivage.get(tid)
                if not arrivage_name:
                    continue
                ca = float(ca or 0.0)
                qty = int(qty or 0)

                stats = sales_by_arrivage.setdefault(arrivage_name, {'ca': 0.0, 'qty': 0})
                stats['ca'] += ca
                stats['qty'] += qty

                shop_name = (config_name_by_id.get(config_id)
                             or company_name_by_id.get(company_id)
                             or 'Inconnu')
                shop_stats = by_shop_map.setdefault((arrivage_name, shop_name), {'ca': 0.0, 'qty': 0})
                shop_stats['ca'] += ca
                shop_stats['qty'] += qty

            daily_sales = [
                {
                    'date': arrivage_name,
                    'label': arrivage_name,
                    'ca': round(stats['ca'], 2),
                    'qty': stats['qty'],
                    'articles': stats['qty'],
                }
                for arrivage_name, stats in sales_by_arrivage.items()
            ]
            daily_sales.sort(key=lambda x: x['ca'], reverse=True)

            by_shop_grouped = {}
            for (arrivage_name, shop_name), stats in by_shop_map.items():
                by_shop_grouped.setdefault(arrivage_name, []).append({
                    'shop': shop_name,
                    'ca': round(stats['ca'], 2),
                    'qty': stats['qty'],
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

            # Ventes d'une société non-retail explicitement cochée
            # (MOD FOR LIFE) : elle ne vend PAS en caisse, ses ventes vers les
            # sociétés magasins passent par sale.order. Sans ça, cocher
            # MOD FOR LIFE laissait CA Vendu inchangé alors que CA Achat, lui,
            # réagissait — même incohérence que celle corrigée côté achats
            # (voir _get_excluded_non_retail_ids). Ajouté uniquement quand la
            # société est cochée, donc aucun impact sur la vision retail.
            ca_so_lines = request.env['sale.order.line'].sudo().browse()
            if self._get_explicit_non_retail_ids(kw):
                ca_so_lines = request.env['sale.order.line'].sudo().search(
                    self._build_non_retail_sale_domain(kw, [product_tmpl.id])
                )
                if ca_so_lines:
                    qty_sold += int(sum(ca_so_lines.mapped('product_uom_qty')))
                    ca += sum(ca_so_lines.mapped('price_total'))
                    ca_ht += sum(ca_so_lines.mapped('price_subtotal'))

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
            # CA Achat = coût réel des achats tel que facturé, affiché en TTC
            # comme tous les CA (décision utilisateur 2026-08-18). La version
            # HT sert à la marge, qui doit comparer du HT à du HT.
            ca_achat = sum(po_lines.mapped('price_total')) if po_lines else 0.0
            ca_achat_ht = sum(po_lines.mapped('price_subtotal')) if po_lines else 0.0

            # NB : la répartition externe/interne (MOD FOR LIFE) du CA Achat a
            # été retirée ici aussi (voir _compute_kpis) — le sélecteur de
            # société joue déjà ce rôle, la carte reste un total général.

            # Marge (%) = (prix de vente - prix d'achat moyen réel) / prix de
            # vente. Le prix d'achat moyen vient des mêmes commandes
            # fournisseur que CA Achat/Qté Achetée ci-dessus (pas de nouvelle
            # source) — None (pas 0) quand la donnée n'existe pas, pour ne
            # pas laisser croire à une marge nulle.
            # list_price est HT, donc on compare avec le prix d'achat HT.
            pv_ttc_ref = product_tmpl.list_price or 0.0
            if qty_purchased > 0 and ca_achat_ht > 0 and pv_ttc_ref > 0:
                prix_achat_moyen = ca_achat_ht / qty_purchased
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
            #
            # DÉCISION UTILISATEUR (2026-08-17) : si l'utilisateur coche
            # explicitement une société non-retail (MOD FOR LIFE) dans le
            # sélecteur, son stock entrepôt DOIT devenir visible — sinon
            # cocher la société n'a aucun effet à l'écran, ce qui est
            # trompeur. Par défaut (seules des sociétés magasins cochées, ou
            # aucun filtre), on garde le périmètre retail pur.
            # Même règle unique que partout ailleurs (_get_excluded_non_retail_ids).
            non_retail_company_ids = self._get_non_retail_company_ids()
            context_company_ids = self._get_context_company_ids()
            excluded_non_retail_ids = self._get_excluded_non_retail_ids(kw)
            explicit_non_retail_ids = [
                cid for cid in non_retail_company_ids if cid not in excluded_non_retail_ids
            ]
            Warehouse = request.env['stock.warehouse'].sudo()
            scoped_warehouses = Warehouse.search([
                ('company_id', 'not in', non_retail_company_ids),
                ('id', 'in', self._get_active_shop_mappings().mapped('warehouse_id').ids),
            ])
            if explicit_non_retail_ids:
                scoped_warehouses |= Warehouse.search([
                    ('company_id', 'in', explicit_non_retail_ids),
                ])

            # BUG CORRIGÉ (2026-08-17) : le stock PAR COULEUR doit suivre le
            # filtre société actif, exactement comme stock_total en haut de
            # fiche. Sans ça, filtrer sur SALMEDO affichait 13 en carte mais
            # 25 en sommant les couleurs (le périmètre réseau complet), ce
            # qui rendait la fiche incohérente avec elle-même dès qu'une
            # société était sélectionnée. scoped_warehouses reste, lui, non
            # filtré : le tableau "Stock par Magasin" montre volontairement
            # tout le réseau (utile pour décider d'un transfert), et
            # stock_total applique le filtre société de son côté.
            variant_warehouses = scoped_warehouses
            if context_company_ids:
                variant_warehouses = scoped_warehouses.filtered(
                    lambda w: w.company_id.id in context_company_ids
                )
            retail_lot_stock_ids = variant_warehouses.mapped('lot_stock_id').ids

            # "Qté Magasin" doit être le STOCK ACTUEL de la variante dans le
            # magasin filtré — pas les ventes (voir sales_by_variant plus
            # haut, qui reste dédié à la répartition des ventes). Une seule
            # requête groupée, pas de N+1 par variante ; calculée uniquement
            # si un magasin est filtré (kw['shop_field']).
            stock_shop_by_variant = {}
            if kw.get('shop_field') and product_variants:
                scope_for_stock = self._get_shop_scope(kw['shop_field'])
                if scope_for_stock and scope_for_stock['warehouse'] and scope_for_stock['warehouse'].lot_stock_id:
                    shop_quants_grouped = request.env['stock.quant'].sudo().read_group(
                        [
                            ('product_id', 'in', product_variants.ids),
                            ('location_id', 'child_of', scope_for_stock['warehouse'].lot_stock_id.id),
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

            by_color = {}
            for v in product_variants:
                stat = sales_by_variant.get(v.id, {'qty': 0, 'ca': 0, 'shops': {}})

                # La pointure n'est plus lue ici : ce tableau agrège par
                # couleur (voir plus bas).
                color_name = resolve_variant_color_size(v)[0]
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

                # DEMANDE UTILISATEUR (2026-08-24) : ce tableau s'appelle
                # "Variantes Couleurs" et doit lister des COULEURS, pas des
                # couples couleur+pointure. On agrège donc les pointures
                # d'une même couleur (plus de lignes "KAKI, 38" / "KAKI, 39"
                # / "KAKI, 40" séparées). La pointure reste disponible là où
                # elle a du sens : le détail par couleur et la matrice de
                # transfert la résolvent eux-mêmes, variante par variante.
                #
                # Pas de fallback silencieux "dispatché = stock" : quand
                # aucune commande fournisseur ne couvre la couleur, on le
                # signale (dispatch_missing, calculé après agrégation)
                # plutôt que de fabriquer un "total pièces" à partir du stock
                # actuel, ce qui masquait le vrai problème de données.
                color_label = color_name if color_name != '—' else 'Standard'

                entry = by_color.get(color_label)
                if entry is None:
                    entry = {
                        'name': color_label,
                        'color': color_name,
                        'qty': 0,
                        'ca': 0.0,
                        'dispatched': 0,
                        'dispatch_source': None,
                        'dispatch_missing': False,
                        'stock': 0,
                        'stock_shop': 0 if kw.get('shop_field') else None,
                        'shops': {},
                    }
                    by_color[color_label] = entry

                entry['qty'] += int(stat['qty'])
                entry['ca'] += stat['ca']
                entry['dispatched'] += dispatched
                if dispatch_source:
                    entry['dispatch_source'] = dispatch_source
                entry['stock'] += var_stock
                if kw.get('shop_field'):
                    entry['stock_shop'] += int(stock_shop_by_variant.get(v.id, 0))
                for shop_name, shop_qty in (stat['shops'] or {}).items():
                    entry['shops'][shop_name] = entry['shops'].get(shop_name, 0) + shop_qty

            # `dispatch_missing` se juge sur la couleur entière, pas pointure
            # par pointure : une couleur reçue en commande fournisseur n'a
            # pas de donnée manquante, même si une pointure isolée n'y figure
            # pas.
            best_variants = []
            for entry in by_color.values():
                entry['ca'] = round(entry['ca'], 2)
                entry['dispatch_missing'] = (
                    entry['dispatched'] == 0 and (entry['stock'] > 0 or entry['qty'] > 0)
                )
                best_variants.append(entry)

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
            try:
                # Ne lister que les entrepôts qui correspondent à un magasin
                # réellement configuré/actif (mv.batch.shop.mapping) — sinon
                # cette liste, construite indépendamment sur stock.warehouse,
                # affichait aussi des entrepôts désactivés/non-magasins
                # (ex: "DIGITAL SHOP") même après avoir désactivé leur
                # mapping, puisqu'elle ne passait pas par lui.
                # scoped_warehouses (calculé plus haut) applique déjà cette
                # règle ET y ajoute l'entrepôt d'une société non-retail
                # explicitement cochée par l'utilisateur — même périmètre que
                # retail_lot_stock_ids, donc la somme par magasin correspond
                # toujours exactement à stock_total.
                for wh in scoped_warehouses:
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
                scope_detail = self._get_shop_scope(kw['shop_field'])
                if scope_detail and scope_detail['warehouse']:
                    target_wh = scope_detail['warehouse'].name
                    stock_total = sum(s['stock'] for s in stock_by_store if s['store_name'] == target_wh)
                else:
                    stock_total = sum(s['stock'] for s in stock_by_store)
            else:
                # Pas de magasin précis choisi : on retombe sur la/les
                # société(s) cochée(s) dans le sélecteur standard Odoo
                # (context_company_ids, déjà résolu plus haut).
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
                    # DEMANDE UTILISATEUR : savoir non seulement à quel
                    # magasin la référence a été dispatchée, mais aussi à
                    # quelle société ce magasin appartient.
                    'company': (mapping.company_id.name
                                if mapping and mapping.company_id else '—'),
                    'city': (mapping.city or '').strip() if mapping else '',
                    'qty': qty,
                    'dispatch_source': dispatch_src,
                })

            # Fusion avec le stock réel (stock.quant) par magasin — le label
            # du dispatch est déjà basé sur warehouse_id.name donc
            # correspond au store_name réel.
            stock_by_name = {s['store_name']: s['stock'] for s in stock_by_store}
            for row in stock_by_store_pivot:
                row['stock'] = stock_by_name.get(row['name'], 0)

            # DEMANDE UTILISATEUR : ce tableau sortait dans l'ordre arbitraire
            # du mapping magasin. Il est désormais classé du plus dispatché au
            # moins dispatché (puis par stock restant), pour lire directement
            # le top → flop des magasins sur cette référence. Les magasins
            # sans aucune commande fournisseur (qty = None) restent en bas.
            stock_by_store_pivot.sort(
                key=lambda r: (-(r['qty'] if r['qty'] is not None else -1),
                               -(r.get('stock') or 0), r['name'])
            )

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
            # (pas au chargement initial). Le repli mv.article.base est
            # rétabli : vérifié en base, 53 articles Base Pivot portent une
            # photo alors que seules 50 fiches produit en ont une, et les
            # deux ensembles ne se recouvrent pas complètement. `has_image`
            # distingue une vraie photo du visuel de remplacement d'Odoo.
            image_source = self._image_availability([product_tmpl.id], size='image_512').get(product_tmpl.id)
            image_url = self._image_url(product_tmpl.id, image_source, size='image_512')
            has_image = bool(image_source)

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
                'has_image': has_image,
            }
        except Exception as e:
            _logger.error(f"Erreur api_product_detail: {str(e)}", exc_info=True)
            return {'error': str(e)}

    @http.route('/mavie/api/product-detail/export', type='http', auth='user', methods=['GET'], csrf=False)
    def api_product_detail_export(self, **kw):
        """Export de la fiche d'une référence : KPIs, variantes couleurs et
        dispatch par société/magasin, tels qu'affichés dans le panneau détail.

        Classeur .xlsx avec la photo incrustée par défaut ; `?format=csv`
        renvoie les données brutes.
        """
        data = self._compute_product_detail(kw)
        if not data or data.get('error'):
            message = data.get('error') if data else 'Produit introuvable'
            return request.make_response(
                'Erreur : ' + message,
                headers=[('Content-Type', 'text/plain; charset=utf-8')],
                status=404,
            )

        ref_for_filename = re.sub(
            r'[^A-Za-z0-9_-]+', '_', data.get('ref') or str(data.get('id') or 'produit'))

        if (kw.get('format') or 'xlsx').lower() != 'csv':
            return self._product_detail_xlsx(data, ref_for_filename)

        buffer = io.StringIO()
        buffer.write('﻿')  # BOM pour qu'Excel détecte l'UTF-8
        writer = csv.writer(buffer, delimiter=';')

        writer.writerow(['Fiche produit', data.get('name') or ''])
        writer.writerow(['Référence', data.get('ref') or ''])
        writer.writerow(['Famille', data.get('family') or ''])
        writer.writerow(['Collection', data.get('collection_name') or ''])
        photo_url = self._absolute_url(data.get('image_url')) if data.get('has_image') else ''
        writer.writerow(['URL photo', photo_url or 'Aucune photo'])
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

        # Dispatch : classé du plus dispatché au moins dispatché, avec la
        # société propriétaire du magasin (demande utilisateur "le dispatch
        # de la référence, il est donné à qui exactement, quelle société et
        # quel magasin").
        writer.writerow(['Dispatch par société / magasin (du plus dispatché au moins dispatché)'])
        writer.writerow(['Société', 'Magasin', 'Ville', 'Qté dispatchée', 'Stock restant'])
        for s in data.get('stock_by_store') or []:
            writer.writerow([
                s.get('company') or '—', s.get('name'), s.get('city') or '—',
                s.get('qty') if s.get('qty') is not None else '—', s.get('stock', 0),
            ])

        filename = f'mavie_export_{ref_for_filename}.csv'

        return request.make_response(
            buffer.getvalue(),
            headers=[
                ('Content-Type', 'text/csv; charset=utf-8'),
                ('Content-Disposition', f'attachment; filename="{filename}"'),
            ],
        )

    def _product_detail_xlsx(self, data, ref_for_filename):
        """Fiche produit en .xlsx, photo incrustée en haut de la feuille."""
        stream, book, fmt = self._xlsx_workbook()
        sheet = book.add_worksheet('Fiche produit')
        sheet.set_column(0, 0, 42)
        sheet.set_column(1, 4, 18)

        sheet.write(0, 0, data.get('name') or 'Fiche produit', fmt['title'])
        sheet.write(1, 0, 'Référence', fmt['cell'])
        sheet.write(1, 1, data.get('ref') or '', fmt['cell'])
        sheet.write(2, 0, 'Famille', fmt['cell'])
        sheet.write(2, 1, data.get('family') or '', fmt['cell'])
        sheet.write(3, 0, 'Collection', fmt['cell'])
        sheet.write(3, 1, data.get('collection_name') or '', fmt['cell'])

        row = 5
        photos = self._photo_bytes_by_tmpl(
            self._image_availability([data['id']], size='image_512'), field='image_512'
        ) if data.get('has_image') else {}
        image_bytes = photos.get(data['id'])
        if image_bytes:
            # Photo plus grande que dans les listes : c'est la fiche d'UNE
            # référence, l'image y est l'information principale.
            sheet.set_row(row, 150)
            sheet.insert_image(row, 0, 'photo.png', {
                'image_data': io.BytesIO(image_bytes),
                'x_offset': 4, 'y_offset': 4, 'object_position': 1,
            })
            row += 2
        else:
            sheet.write(row, 0, 'Aucune photo enregistrée dans Odoo pour cette référence.',
                        fmt['muted'])
            row += 2

        sheet.write(row, 0, 'KPI', fmt['header'])
        sheet.write(row, 1, 'Valeur', fmt['header'])
        row += 1
        for label, value, style in [
            ('Qté vendue', data.get('qty_sold', 0), 'num'),
            ('Qté achetée', data.get('qty_purchased', 0), 'num'),
            ('Stock réel Odoo', data.get('stock_total', 0), 'num'),
            ('Stock théorique (achetée - vendue)', data.get('stock_theorique', 0), 'num'),
            ('Écart stock (théorique - réel)', data.get('stock_ecart', 0), 'num'),
            ('CA Vendu (TTC)', data.get('ca', 0), 'money'),
            ('CA Achat', data.get('ca_achat', 0), 'money'),
            ('Sell-through (%)', data.get('sell_through', 0), 'money'),
        ]:
            sheet.write(row, 0, label, fmt['cell'])
            sheet.write(row, 1, value, fmt[style])
            row += 1

        row += 1
        sheet.write(row, 0, 'Variantes couleurs (meilleure vente en tête)', fmt['title'])
        row += 1
        for col, label in enumerate(['#', 'Couleur', 'Total pièces', 'Qté vendue', 'Reste']):
            sheet.write(row, col, label, fmt['header'])
        row += 1
        for idx, v in enumerate(data.get('variants') or [], start=1):
            for col, value in enumerate([idx, v.get('name'), v.get('total_pieces', 0),
                                         v.get('qty', 0), v.get('reste', 0)]):
                sheet.write(row, col, value, fmt['cell'])
            row += 1

        row += 1
        sheet.write(row, 0, 'Dispatch par société / magasin (du plus dispatché au moins dispatché)',
                    fmt['title'])
        row += 1
        for col, label in enumerate(['Société', 'Magasin', 'Ville', 'Qté dispatchée',
                                     'Stock restant']):
            sheet.write(row, col, label, fmt['header'])
        row += 1
        for s in data.get('stock_by_store') or []:
            values = [
                s.get('company') or '—', s.get('name'), s.get('city') or '—',
                s.get('qty') if s.get('qty') is not None else '—', s.get('stock', 0),
            ]
            for col, value in enumerate(values):
                sheet.write(row, col, value, fmt['cell'])
            row += 1

        return self._xlsx_response(stream, book, 'mavie_export_%s.xlsx' % ref_for_filename)

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
            available = sum(max(0.0, float(q.quantity or 0.0) - float(q.reserved_quantity or 0.0)) for q in quants) if quants else 0.0
            result[m.shop_field] = available
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

            # ── Répartition et Stock pour TOUS les magasins (Dispatché, Vendu, Stock) ──
            all_stores = []
            variants = request.env['product.product'].sudo().search(
                [('product_tmpl_id', '=', product_tmpl_id)]
            )
            if color:
                color_upper = color.upper().strip()
                variants = variants.filtered(
                    lambda v: (resolve_variant_color_size(v)[0] or '').upper().strip() == color_upper
                )

            all_stock_by_field = self._stock_by_mapping_for_template(product_tmpl_id, mappings, color=color)

            sold_by_field = {}
            if variants:
                pos_lines = request.env['pos.order.line'].sudo().search([
                    ('product_id', 'in', variants.ids),
                    ('order_id.state', 'in', ['paid', 'done', 'invoiced'])
                ])
                for pline in pos_lines:
                    cfg = pline.order_id.session_id.config_id
                    if cfg and cfg.picking_type_id and cfg.picking_type_id.warehouse_id:
                        wh_id = cfg.picking_type_id.warehouse_id.id
                        for sm in mappings:
                            if sm.warehouse_id and sm.warehouse_id.id == wh_id:
                                sold_by_field[sm.shop_field] = sold_by_field.get(sm.shop_field, 0) + int(pline.qty)
                                break

            dispatched_by_field = {}
            if variants:
                po_lines = request.env['purchase.order.line'].sudo().search([
                    ('product_id', 'in', variants.ids),
                    ('order_id.state', 'in', ['purchase', 'done'])
                ])
                for pol in po_lines:
                    wh = pol.order_id.picking_type_id.warehouse_id
                    if wh:
                        for sm in mappings:
                            if sm.warehouse_id and sm.warehouse_id.id == wh.id:
                                dispatched_by_field[sm.shop_field] = dispatched_by_field.get(sm.shop_field, 0) + int(pol.product_qty)
                                break

            for m in mappings:
                if not m.warehouse_id or not m.warehouse_id.lot_stock_id:
                    continue
                stk = all_stock_by_field.get(m.shop_field, 0.0)
                disp = dispatched_by_field.get(m.shop_field, 0)
                sold = sold_by_field.get(m.shop_field, 0)
                city = (m.city or '').strip()
                all_stores.append({
                    'shop_field': m.shop_field,
                    'shop_label': m.warehouse_id.name if m.warehouse_id else (m.shop_label or m.shop_field),
                    'city': city or '—',
                    'company': m.company_id.name if m.company_id else '—',
                    'dispatched': int(disp),
                    'sold': int(sold),
                    'stock': int(stk),
                    'is_target': m.shop_field == dest_shop_field,
                    'has_responsible': bool(m.responsible_user_id),
                })

            all_stores.sort(key=lambda s: (0 if s['is_target'] else 1, -s['stock'], s['shop_label']))

            return {'suggestions': suggestions, 'dest_city': dest_city or None, 'all_stores': all_stores}
        except Exception as e:
            _logger.error(f"Erreur api_transfer_suggestions: {str(e)}", exc_info=True)
            return {'error': str(e), 'suggestions': [], 'all_stores': []}

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
            # DEMANDE UTILISATEUR : un transfert entre deux magasins d'une
            # MÊME société n'est plus bloqué. Il ne passe simplement pas par
            # le circuit inter-sociétés (avoir + commande d'achat auprès de
            # MOD FOR LIFE), qui n'aurait aucun sens ici : le modèle
            # inter.internal.transfer bascule alors sur un simple transfert
            # de stock interne d'un entrepôt à l'autre.
            if source_mapping.warehouse_id.lot_stock_id.id == dest_mapping.warehouse_id.lot_stock_id.id:
                return {'error': 'Le magasin source et le magasin cible sont le même emplacement.'}

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
                # Marque ce bon comme provenant du dashboard : c'est le seul
                # critère retenu par la section Historique (voir
                # created_from_dashboard sur inter.internal.transfer).
                'created_from_dashboard': True,
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

    # ─────────────────────────────────────────────────────────────
    # EXPORT DU STOCK DORMANT
    # ─────────────────────────────────────────────────────────────

    @http.route('/mavie/api/dormant/export', type='http', auth='user', methods=['GET'], csrf=False)
    def api_dormant_export(self, **kw):
        """Export du stock dormant, mêmes filtres qu'à l'écran.

        Classeur .xlsx avec photos incrustées par défaut ; `?format=csv`
        renvoie les données brutes (sans photo, un CSV ne peut pas en porter).
        """
        data = self._compute_kpis(kw)
        if not data or data.get('error'):
            message = data.get('error') if data else 'Erreur inconnue'
            return request.make_response(
                'Erreur : ' + message,
                headers=[('Content-Type', 'text/plain; charset=utf-8')],
                status=404,
            )

        rows = data.get('dormant_list') or []
        image_sources = self._image_availability({r['id'] for r in rows})

        def _values(idx, row):
            breakdown = ' | '.join(
                '%s : %s' % (b.get('magasin'), b.get('qty'))
                for b in (row.get('magasin_breakdown') or [])
            )
            return [
                idx,
                row.get('ref'), row.get('name'), row.get('magasin') or '—',
                row.get('magasin_qty') if row.get('magasin_qty') is not None else '—',
                row.get('magasin_days') if row.get('magasin_days') is not None else '—',
                row.get('magasin_last_move') or '—',
                row.get('stock', 0), breakdown,
            ]

        if (kw.get('format') or 'xlsx').lower() == 'csv':
            buffer = io.StringIO()
            buffer.write(u'﻿')  # BOM pour qu'Excel détecte l'UTF-8
            writer = csv.writer(buffer, delimiter=';')
            writer.writerow(['Stock dormant (aucune vente depuis 90 jours)'])
            writer.writerow(['Références concernées', data.get('dormant_count', 0)])
            writer.writerow(['Part du stock immobilisé (%)', data.get('stock_dormant_pct', 0)])
            writer.writerow([])
            writer.writerow([
                '#', 'URL photo', 'Réf', 'Produit', 'Magasin principal',
                'Qté dans ce magasin', 'Jours sans mouvement', 'Dernier mouvement',
                'Stock total', 'Répartition par magasin',
            ])
            for idx, row in enumerate(rows, start=1):
                src = image_sources.get(row['id'])
                photo_url = self._absolute_url(self._image_url(row['id'], src)) if src else ''
                vals = _values(idx, row)
                writer.writerow([vals[0], photo_url] + vals[1:])
            return request.make_response(
                buffer.getvalue(),
                headers=[
                    ('Content-Type', 'text/csv; charset=utf-8'),
                    ('Content-Disposition',
                     'attachment; filename="mavie_export_stock_dormant.csv"'),
                ],
            )

        photos = self._photo_bytes_by_tmpl(image_sources)
        stream, book, fmt = self._xlsx_workbook()
        sheet = book.add_worksheet('Stock dormant')
        sheet.write(0, 0, 'Stock dormant (aucune vente depuis 90 jours)', fmt['title'])
        sheet.write(1, 0, '%s référence(s) concernée(s) — %s%% du stock immobilisé — %s avec photo'
                    % (data.get('dormant_count', 0), data.get('stock_dormant_pct', 0),
                       sum(1 for r in rows if photos.get(r['id']))), fmt['meta'])

        columns = ['#', 'Photo', 'Réf', 'Produit', 'Magasin principal', 'Qté dans ce magasin',
                   'Jours sans mouvement', 'Dernier mouvement', 'Stock total',
                   'Répartition par magasin']
        for col, label in enumerate(columns):
            sheet.write(3, col, label, fmt['header'])
        sheet.set_column(1, 1, self._XLSX_PHOTO_COL_WIDTH)
        sheet.set_column(2, 2, 16)
        sheet.set_column(3, 3, 34)
        sheet.set_column(4, 4, 28)
        sheet.set_column(5, 8, 14)
        sheet.set_column(9, 9, 50)
        sheet.freeze_panes(4, 0)

        for idx, row in enumerate(rows, start=1):
            excel_row = 3 + idx
            sheet.set_row(excel_row, self._XLSX_ROW_HEIGHT)
            vals = _values(idx, row)
            sheet.write(excel_row, 0, vals[0], fmt['cell'])
            image_bytes = photos.get(row['id'])
            if image_bytes:
                self._xlsx_insert_photo(sheet, excel_row, 1, image_bytes, idx)
            else:
                sheet.write(excel_row, 1, 'Aucune photo', fmt['muted'])
            for offset, value in enumerate(vals[1:], start=2):
                sheet.write(excel_row, offset, value, fmt['cell'])

        return self._xlsx_response(stream, book, 'mavie_export_stock_dormant.xlsx')

    # ─────────────────────────────────────────────────────────────
    # VALORISATION — DÉTAIL PAR MAGASIN D'UNE SOCIÉTÉ
    # ─────────────────────────────────────────────────────────────

    @http.route('/mavie/api/valorisation-detail', type='json', auth='user', methods=['POST'], csrf=False)
    def api_valorisation_detail(self, **kw):
        """Détail magasin par magasin de la valorisation d'UNE société.

        Calculé à la demande (au clic sur une ligne société du tableau de
        valorisation) et non dans _compute_kpis : regrouper les quants par
        emplacement double le nombre de groupes à parcourir sur tout le
        réseau (127 050 -> 248 528, mesuré en base), alors que restreint à
        une seule société le calcul reste immédiat.
        """
        try:
            company_id = kw.get('company_id')
            if not company_id:
                return {'error': 'Société manquante.', 'magasins': []}
            company = request.env['res.company'].sudo().browse(int(company_id))
            if not company.exists():
                return {'error': 'Société introuvable.', 'magasins': []}

            product_tmpl_ids = None
            if kw.get('collection_id') or kw.get('batch_id') or kw.get('categ_id'):
                product_tmpl_ids = request.env['product.template'].sudo().search(
                    self._build_product_domain(kw)
                ).ids
                if not product_tmpl_ids:
                    product_tmpl_ids = [-1]

            quant_domain = [
                ('location_id.usage', '=', 'internal'),
                ('company_id', '=', company.id),
            ]
            quant_domain += self._sachet_exclude_domain('product_id.product_tmpl_id.collection_id')
            if product_tmpl_ids is not None:
                variant_ids = request.env['product.product'].sudo().search(
                    [('product_tmpl_id', 'in', product_tmpl_ids)]
                ).ids
                quant_domain.append(('product_id', 'in', variant_ids))

            grouped = self._group_sums(
                'stock.quant', quant_domain, ['quantity'],
                group_fields=('product_id', 'location_id'),
            )

            # Emplacement -> entrepôt : résolu une fois pour toutes, pas une
            # requête par emplacement.
            warehouses = request.env['stock.warehouse'].sudo().search([('company_id', '=', company.id)])
            wh_by_location = {}
            for wh in warehouses:
                if not wh.lot_stock_id:
                    continue
                locations = request.env['stock.location'].sudo().search([
                    ('id', 'child_of', wh.lot_stock_id.id)
                ])
                for loc in locations:
                    wh_by_location[loc.id] = wh

            pids = [g['product_id'][0] for g in grouped if g.get('product_id')]
            prod_map = {}
            if pids:
                prod_map = {
                    p['id']: p for p in request.env['product.product'].sudo().search_read(
                        [('id', 'in', pids)], ['id', 'list_price', 'standard_price', 'product_tmpl_id'])
                }

            # Repli sur le prix d'achat moyen réellement payé quand le champ
            # "Coût" est vide — même règle que la valorisation principale.
            tmpl_ids_needed = {
                p['product_tmpl_id'][0] for p in prod_map.values()
                if p.get('product_tmpl_id') and not p.get('standard_price')
            }
            prix_achat_moyen = {}
            if tmpl_ids_needed:
                po_grouped = request.env['purchase.order.line'].sudo().read_group(
                    [('order_id.state', 'in', ['purchase', 'done']),
                     ('product_id.product_tmpl_id', 'in', list(tmpl_ids_needed))],
                    ['product_qty:sum', 'price_subtotal:sum'], ['product_id'], lazy=False
                )
                po_pids = [g['product_id'][0] for g in po_grouped if g.get('product_id')]
                po_tmpl = {}
                if po_pids:
                    po_tmpl = {
                        p['id']: p['product_tmpl_id'][0]
                        for p in request.env['product.product'].sudo().search_read(
                            [('id', 'in', po_pids)], ['id', 'product_tmpl_id'])
                        if p.get('product_tmpl_id')
                    }
                accum = {}
                for g in po_grouped:
                    tid = po_tmpl.get(g['product_id'][0] if g.get('product_id') else None)
                    if not tid:
                        continue
                    entry = accum.setdefault(tid, [0.0, 0.0])
                    entry[0] += g.get('product_qty') or 0.0
                    entry[1] += g.get('price_subtotal') or 0.0
                for tid, (qte, montant) in accum.items():
                    if qte and montant > 0:
                        prix_achat_moyen[tid] = montant / qte

            cost_estime = False
            by_warehouse = {}
            for g in grouped:
                pid = g['product_id'][0] if g.get('product_id') else None
                loc_id = g['location_id'][0] if g.get('location_id') else None
                qty = g.get('quantity') or 0.0
                if qty <= 0 or pid not in prod_map:
                    continue
                wh = wh_by_location.get(loc_id)
                if not wh:
                    continue
                p_data = prod_map[pid]
                cost_price = p_data.get('standard_price') or 0.0
                if not cost_price:
                    tmpl_ref = p_data.get('product_tmpl_id')
                    fallback = prix_achat_moyen.get(tmpl_ref[0]) if tmpl_ref else None
                    if fallback:
                        cost_price = fallback
                        cost_estime = True
                entry = by_warehouse.setdefault(wh.id, {
                    'name': wh.name, 'qty': 0, 'valeur_ht': 0.0, 'valeur_cost': 0.0,
                })
                entry['qty'] += int(qty)
                entry['valeur_ht'] += qty * (p_data.get('list_price') or 0.0)
                entry['valeur_cost'] += qty * cost_price

            magasins = [
                {
                    'name': v['name'],
                    'qty': v['qty'],
                    'valeur_ht': round(v['valeur_ht'], 2),
                    'valeur_cost': round(v['valeur_cost'], 2),
                }
                for v in by_warehouse.values()
            ]
            magasins.sort(key=lambda m: -m['valeur_ht'])

            return {
                'company_name': company.name,
                'magasins': magasins,
                'total_qty': sum(m['qty'] for m in magasins),
                'total_ht': round(sum(m['valeur_ht'] for m in magasins), 2),
                'total_cost': round(sum(m['valeur_cost'] for m in magasins), 2),
                'cost_estime': cost_estime,
            }
        except Exception as e:
            _logger.error(f"Erreur api_valorisation_detail: {str(e)}", exc_info=True)
            return {'error': str(e), 'magasins': []}

    # ─────────────────────────────────────────────────────────────
    # ÉCARTS D'INVENTAIRE — DÉTECTION ET EXPLICATION
    #
    # DEMANDE UTILISATEUR : « normalement dans la partie Précision
    # Inventaire il doit afficher 0 % ; s'il affiche un pourcentage grand,
    # on doit pouvoir cliquer et voir les problèmes détectés — le moins sur
    # les références et la cause de ce moins : un magasin a 3 produits mais
    # il en a vendu 4, d'où vient le 1 ? »
    #
    # Un stock négatif est la trace exacte de ce symptôme : Odoo a
    # enregistré plus de sorties que d'entrées pour cette référence dans cet
    # entrepôt. Vérifié en base : 46 740 quants négatifs, soit 8 212 couples
    # (référence, emplacement) en anomalie — le sujet est massif et mérite
    # d'être remonté comme un indicateur à part entière plutôt que dilué
    # dans un pourcentage de « précision » proche de 100 %.
    # ─────────────────────────────────────────────────────────────

    def _negative_stock_groups(self, quant_domain):
        """[(product_id, location_id, quantité)] pour les seuls stocks négatifs.

        Agrégation + filtre HAVING exécutés par Postgres : seules les
        quelques milliers de lignes réellement en anomalie remontent en
        Python, au lieu des ~248 000 groupes que produirait un read_group
        par (produit, emplacement) sur tout le réseau.
        """
        Quant = request.env['stock.quant'].sudo()
        query = Quant._where_calc(quant_domain)
        Quant._apply_ir_rules(query, 'read')
        from_clause, where_clause, params = query.get_sql()
        request.env.cr.execute(
            'SELECT "stock_quant"."product_id", "stock_quant"."location_id", '
            'SUM("stock_quant"."quantity") '
            'FROM %s WHERE %s '
            'GROUP BY "stock_quant"."product_id", "stock_quant"."location_id" '
            'HAVING SUM("stock_quant"."quantity") < 0' % (from_clause, where_clause or 'TRUE'),
            params,
        )
        return request.env.cr.fetchall()

    def _location_to_warehouse_map(self, warehouses):
        """{location_id: warehouse} pour tous les emplacements de ces entrepôts."""
        mapping = {}
        for wh in warehouses:
            if not wh.lot_stock_id:
                continue
            locations = request.env['stock.location'].sudo().search([
                ('id', 'child_of', wh.lot_stock_id.id)
            ])
            for loc in locations:
                mapping[loc.id] = wh
        return mapping

    def _build_anomaly_quant_domain(self, kw):
        """Périmètre des quants examinés pour les écarts d'inventaire."""
        domain = [('location_id.usage', '=', 'internal')]
        domain += self._sachet_exclude_domain('product_id.product_tmpl_id.collection_id')

        scope = self._get_shop_scope(kw.get('shop_field'))
        if scope:
            if scope['company_id']:
                domain.append(('company_id', '=', scope['company_id']))
            if scope['warehouse'] and scope['warehouse'].lot_stock_id:
                domain.append(('location_id', 'child_of', scope['warehouse'].lot_stock_id.id))
        else:
            domain.append(('company_id', 'not in', self._get_excluded_non_retail_ids(kw)))
            context_company_ids = self._get_context_company_ids()
            if context_company_ids:
                domain.append(('company_id', 'in', context_company_ids))

        if kw.get('collection_id') or kw.get('batch_id') or kw.get('categ_id'):
            product_tmpl_ids = request.env['product.template'].sudo().search(
                self._build_product_domain(kw)
            ).ids or [-1]
            variant_ids = request.env['product.product'].sudo().search(
                [('product_tmpl_id', 'in', product_tmpl_ids)]
            ).ids
            domain.append(('product_id', 'in', variant_ids))
        return domain

    def _inventory_anomaly_summary(self, kw):
        """{refs_count, groups_count, qty_manquante} — version légère.

        Appelée à chaque chargement du dashboard : elle ne fait que
        l'agrégation SQL (filtrée côté Postgres) plus une seule lecture des
        variantes concernées, sans résoudre libellés ni entrepôts — ce
        travail-là n'est fait qu'à l'ouverture du détail.
        """
        try:
            rows = self._negative_stock_groups(self._build_anomaly_quant_domain(kw))
        except Exception as e:
            _logger.warning("Écarts d'inventaire indisponibles: %s", e)
            return {'refs_count': 0, 'groups_count': 0, 'qty_manquante': 0}
        if not rows:
            return {'refs_count': 0, 'groups_count': 0, 'qty_manquante': 0}
        variant_ids = list({r[0] for r in rows})
        variants = request.env['product.product'].sudo().with_context(active_test=False).search_read(
            [('id', 'in', variant_ids)], ['id', 'product_tmpl_id']
        )
        tmpl_ids = {v['product_tmpl_id'][0] for v in variants if v.get('product_tmpl_id')}
        return {
            'refs_count': len(tmpl_ids),
            'groups_count': len(rows),
            'qty_manquante': int(sum(r[2] for r in rows)),
        }

    def _compute_inventory_anomalies(self, kw, limit=500):
        """Références en stock négatif, regroupées par magasin."""
        rows = self._negative_stock_groups(self._build_anomaly_quant_domain(kw))
        if not rows:
            return {'anomalies': [], 'count': 0, 'qty_manquante': 0}

        variant_ids = list({r[0] for r in rows})
        variants = request.env['product.product'].sudo().with_context(active_test=False).search_read(
            [('id', 'in', variant_ids)], ['id', 'product_tmpl_id', 'display_name']
        )
        variant_map = {v['id']: v for v in variants}

        warehouses = request.env['stock.warehouse'].sudo().search([])
        wh_by_location = self._location_to_warehouse_map(warehouses)

        grouped = {}
        for variant_id, location_id, qty in rows:
            v = variant_map.get(variant_id)
            if not v or not v.get('product_tmpl_id'):
                continue
            wh = wh_by_location.get(location_id)
            key = (v['product_tmpl_id'][0], wh.id if wh else 0)
            entry = grouped.setdefault(key, {
                'id': v['product_tmpl_id'][0],
                'warehouse_id': wh.id if wh else None,
                'magasin': wh.name if wh else 'Emplacement hors entrepôt',
                'company': wh.company_id.name if wh and wh.company_id else '—',
                'qty_negative': 0.0,
                'variants': [],
            })
            entry['qty_negative'] += qty
            entry['variants'].append({
                'name': v.get('display_name') or '—',
                'qty': int(qty),
            })

        tmpl_ids = list({e['id'] for e in grouped.values()})
        tmpl_data = request.env['product.template'].sudo().search_read(
            [('id', 'in', tmpl_ids)], ['id', 'name', 'default_code', 'base_pivot_reference']
        )
        tmpl_map = {t['id']: t for t in tmpl_data}

        anomalies = []
        for entry in grouped.values():
            t = tmpl_map.get(entry['id'], {})
            entry['name'] = t.get('name') or '—'
            entry['ref'] = (t.get('base_pivot_reference') or t.get('default_code')
                            or t.get('name') or '—')
            entry['qty_negative'] = int(entry['qty_negative'])
            entry['variants'].sort(key=lambda v: v['qty'])
            anomalies.append(entry)

        anomalies.sort(key=lambda a: a['qty_negative'])
        return {
            'anomalies': anomalies[:limit],
            'count': len(anomalies),
            'refs_count': len(tmpl_ids),
            'qty_manquante': int(sum(a['qty_negative'] for a in anomalies)),
        }

    @http.route('/mavie/api/inventory-anomalies', type='json', auth='user', methods=['POST'], csrf=False)
    def api_inventory_anomalies(self, **kw):
        try:
            return self._compute_inventory_anomalies(kw)
        except Exception as e:
            _logger.error(f"Erreur api_inventory_anomalies: {str(e)}", exc_info=True)
            return {'error': str(e), 'anomalies': []}

    # Libellés des causes, dans l'ordre où on veut les lire à l'écran.
    _LEDGER_LABELS = {
        'achat': 'Réception fournisseur / import',
        'transfert': 'Transfert entre magasins',
        'transit': 'Transit inter-sociétés',
        'inventaire': "Ajustement d'inventaire",
        'vente': 'Vente / livraison client',
        'retour': 'Retour client',
        'production': 'Production / assemblage',
        'autre': 'Autre mouvement',
    }

    def _classify_move_line(self, move_line, inside_location_ids, direction):
        """Catégorie métier d'un mouvement, vue depuis l'entrepôt examiné."""
        other = move_line.location_id if direction == 'in' else move_line.location_dest_id
        usage = other.usage
        if usage == 'supplier':
            return 'achat' if direction == 'in' else 'autre'
        if usage == 'customer':
            return 'retour' if direction == 'in' else 'vente'
        if usage == 'inventory':
            return 'inventaire'
        if usage == 'transit':
            return 'transit'
        if usage == 'production':
            return 'production'
        if usage == 'internal':
            return 'transfert'
        return 'autre'

    @http.route('/mavie/api/inventory-anomaly-detail', type='json', auth='user', methods=['POST'], csrf=False)
    def api_inventory_anomaly_detail(self, **kw):
        """Explique un stock négatif : d'où viennent les pièces sorties.

        Reconstitue le grand livre des mouvements validés de cette référence
        pour CET entrepôt (entrées d'un côté, sorties de l'autre, classées
        par nature) et le confronte aux ventes en caisse, en distinguant les
        ventes au prix catalogue des ventes en solde — c'est la question
        posée : « le produit vendu en trop, il vient d'un transfert ou d'un
        solde ? ».
        """
        try:
            product_tmpl_id = kw.get('article_id') or kw.get('product_tmpl_id')
            warehouse_id = kw.get('warehouse_id')
            if not product_tmpl_id:
                return {'error': 'Référence manquante.'}

            product_tmpl = request.env['product.template'].sudo().browse(int(product_tmpl_id))
            if not product_tmpl.exists():
                return {'error': 'Référence introuvable.'}

            warehouse = request.env['stock.warehouse'].sudo().browse(int(warehouse_id)) if warehouse_id else False
            if not warehouse or not warehouse.exists() or not warehouse.lot_stock_id:
                return {'error': 'Magasin introuvable ou sans emplacement de stock.'}

            variants = request.env['product.product'].sudo().search([
                ('product_tmpl_id', '=', product_tmpl.id)
            ])
            if not variants:
                return {'error': 'Aucune variante pour cette référence.'}

            lot_stock = warehouse.lot_stock_id
            inside_locations = request.env['stock.location'].sudo().search([
                ('id', 'child_of', lot_stock.id)
            ])
            inside_ids = set(inside_locations.ids)

            MoveLine = request.env['stock.move.line'].sudo()
            lines_in = MoveLine.search([
                ('state', '=', 'done'),
                ('product_id', 'in', variants.ids),
                ('location_dest_id', 'in', list(inside_ids)),
                '!', ('location_id', 'in', list(inside_ids)),
            ])
            lines_out = MoveLine.search([
                ('state', '=', 'done'),
                ('product_id', 'in', variants.ids),
                ('location_id', 'in', list(inside_ids)),
                '!', ('location_dest_id', 'in', list(inside_ids)),
            ])

            def _accumulate(lines, direction):
                buckets = {}
                for ml in lines:
                    category = self._classify_move_line(ml, inside_ids, direction)
                    bucket = buckets.setdefault(category, {
                        'categorie': category,
                        'label': self._LEDGER_LABELS.get(category, category),
                        'sens': 'Entrée' if direction == 'in' else 'Sortie',
                        'qty': 0.0,
                        'nb_mouvements': 0,
                        'derniere_date': None,
                        'exemples': [],
                    })
                    bucket['qty'] += ml.quantity
                    bucket['nb_mouvements'] += 1
                    if ml.date and (not bucket['derniere_date'] or str(ml.date) > bucket['derniere_date']):
                        bucket['derniere_date'] = str(ml.date)
                    if len(bucket['exemples']) < 5:
                        doc = ml.reference or (ml.picking_id.name if ml.picking_id else '') or (
                            ml.move_id.origin if ml.move_id else '')
                        contrepartie = (ml.location_id if direction == 'in' else ml.location_dest_id)
                        bucket['exemples'].append({
                            'document': doc or '—',
                            'origine': ml.move_id.origin if ml.move_id else '',
                            'emplacement': contrepartie.complete_name if contrepartie else '—',
                            'qty': int(ml.quantity),
                            'date': str(ml.date)[:10] if ml.date else '—',
                        })
                for bucket in buckets.values():
                    bucket['qty'] = int(round(bucket['qty']))
                return buckets

            in_buckets = _accumulate(lines_in, 'in')
            out_buckets = _accumulate(lines_out, 'out')

            entrees = sorted(in_buckets.values(), key=lambda b: -b['qty'])
            sorties = sorted(out_buckets.values(), key=lambda b: -b['qty'])
            total_in = sum(b['qty'] for b in entrees)
            total_out = sum(b['qty'] for b in sorties)

            quants = request.env['stock.quant'].sudo().search([
                ('product_id', 'in', variants.ids),
                ('location_id', 'in', list(inside_ids)),
            ])
            stock_reel = int(sum(quants.mapped('quantity'))) if quants else 0

            # Ventes en caisse de ce magasin, séparées normal / solde : c'est
            # la question posée par l'utilisateur (« d'où vient le 1 vendu en
            # trop : un transfert ou un solde ? »).
            pos_configs = request.env['pos.config'].sudo().search([
                ('picking_type_id.warehouse_id', '=', warehouse.id)
            ])
            qty_vendue = qty_vendue_solde = 0
            if pos_configs:
                request.env.cr.execute("""
                    SELECT COALESCE(SUM(pol.qty), 0),
                           COALESCE(SUM(CASE WHEN pol.price_unit * (1 - COALESCE(pol.discount, 0) / 100.0)
                                                  < pt.list_price * 0.999
                                             THEN pol.qty ELSE 0 END), 0)
                    FROM pos_order_line pol
                    JOIN pos_order po ON po.id = pol.order_id
                    JOIN pos_session ps ON ps.id = po.session_id
                    JOIN product_product pp ON pp.id = pol.product_id
                    JOIN product_template pt ON pt.id = pp.product_tmpl_id
                    WHERE ps.config_id IN %s
                      AND pp.product_tmpl_id = %s
                      AND po.state IN ('paid', 'done', 'invoiced')
                """, (tuple(pos_configs.ids), product_tmpl.id))
                row = request.env.cr.fetchone()
                qty_vendue = int(row[0] or 0)
                qty_vendue_solde = int(row[1] or 0)

            # Transferts inter-magasins passés par le module dédié : on cite
            # les bons concernés, la piste la plus actionnable pour retrouver
            # l'origine d'une sortie non couverte par une entrée.
            transferts = []
            try:
                Transfer = request.env['inter.internal.transfer'].sudo()
                related = Transfer.search([
                    ('line_ids.product_id', 'in', variants.ids),
                    '|', ('location_source_id', 'in', list(inside_ids)),
                         ('location_target_id', 'in', list(inside_ids)),
                ], order='id desc', limit=20)
                for tr in related:
                    qty = sum(
                        line.quantity for line in tr.line_ids
                        if line.product_id.id in set(variants.ids)
                    )
                    transferts.append({
                        'name': tr.name,
                        'date': str(tr.create_date)[:10] if tr.create_date else '—',
                        'sens': 'Sortie' if tr.location_source_id.id in inside_ids else 'Entrée',
                        'source': tr.location_source_id.complete_name,
                        'cible': tr.location_target_id.complete_name,
                        'qty': int(qty),
                        'state': tr.state,
                    })
            except Exception as e:
                _logger.warning("Transferts internes indisponibles: %s", e)

            manquant = total_in - total_out - stock_reel
            return {
                'ref': product_tmpl.base_pivot_reference or product_tmpl.default_code or product_tmpl.name,
                'name': product_tmpl.name,
                'magasin': warehouse.name,
                'societe': warehouse.company_id.name if warehouse.company_id else '—',
                'entrees': entrees,
                'sorties': sorties,
                'total_entrees': total_in,
                'total_sorties': total_out,
                'stock_theorique': total_in - total_out,
                'stock_reel': stock_reel,
                # Non nul = des quants ont été écrits sans mouvement de stock
                # correspondant (import, correction directe en base).
                'ecart_non_explique': int(manquant),
                'qty_vendue_caisse': qty_vendue,
                'qty_vendue_solde': qty_vendue_solde,
                'qty_vendue_normale': qty_vendue - qty_vendue_solde,
                'transferts': transferts,
            }
        except Exception as e:
            _logger.error(f"Erreur api_inventory_anomaly_detail: {str(e)}", exc_info=True)
            return {'error': str(e)}

    # ─────────────────────────────────────────────────────────────
    # HISTORIQUE DES TRANSFERTS ET DES SOLDES
    #
    # DEMANDE UTILISATEUR : disposer, sous les cartes, de l'historique de
    # tout ce qui a été fait — les transferts entre magasins d'un côté, les
    # ventes en solde de l'autre. Les deux listes partagent les filtres de
    # la barre du haut (période, magasin) et s'exportent en CSV.
    # ─────────────────────────────────────────────────────────────

    def _transfer_history_rows(self, kw, limit=300):
        # Uniquement les bons lancés depuis le dashboard (décision
        # utilisateur) : l'historique repart de zéro et ne reprend pas les
        # transferts créés auparavant par d'autres canaux.
        domain = [('created_from_dashboard', '=', True)]
        if kw.get('date_start'):
            domain.append(('create_date', '>=', kw['date_start'] + ' 00:00:00'))
        if kw.get('date_end'):
            domain.append(('create_date', '<=', kw['date_end'] + ' 23:59:59'))

        scope = self._get_shop_scope(kw.get('shop_field'))
        if scope and scope['warehouse'] and scope['warehouse'].lot_stock_id:
            lot_stock_id = scope['warehouse'].lot_stock_id.id
            domain += ['|', ('location_source_id', '=', lot_stock_id),
                            ('location_target_id', '=', lot_stock_id)]
        else:
            context_company_ids = self._get_context_company_ids()
            if context_company_ids:
                domain += ['|', ('company_source_id', 'in', context_company_ids),
                                ('company_target_id', 'in', context_company_ids)]

        transfers = request.env['inter.internal.transfer'].sudo().search(
            domain, order='id desc', limit=limit
        )
        state_labels = {'draft': 'Brouillon', 'submitted': 'En attente de validation', 'done': 'Fait'}
        rows = []
        for tr in transfers:
            qty = sum(tr.line_ids.mapped('quantity'))
            refs = tr.line_ids.mapped('product_id.product_tmpl_id')
            rows.append({
                'id': tr.id,
                'name': tr.name,
                'date': str(tr.create_date)[:16] if tr.create_date else '—',
                'state': tr.state,
                'state_label': state_labels.get(tr.state, tr.state),
                'source_magasin': tr.emetteur_display or '—',
                'source_societe': tr.company_source_id.name or '—',
                'dest_magasin': tr.recepteur_display or '—',
                'dest_societe': tr.company_target_id.name or '—',
                # Un transfert au sein d'une même société ne passe plus par
                # le circuit inter-sociétés : on le signale explicitement.
                'intra_societe': tr.company_source_id.id == tr.company_target_id.id,
                'nb_references': len(refs),
                'nb_lignes': len(tr.line_ids),
                'qty': int(qty),
                'group_ref': tr.group_ref or '',
            })
        return rows

    def _solde_history_rows(self, kw, limit=500):
        """Lignes de caisse vendues sous le prix catalogue, les plus récentes."""
        pos_domain = self._build_pos_domain(kw, None)
        line_ids = request.env['pos.order.line'].sudo().search(pos_domain).ids
        if not line_ids:
            return []
        request.env.cr.execute("""
            SELECT po.date_order,
                   po.name,
                   pc.name AS magasin,
                   pt.id AS tmpl_id,
                   COALESCE(pt.base_pivot_reference, pt.default_code, pt.name->>'en_US') AS ref,
                   pt.name->>'en_US' AS produit,
                   pol.qty,
                   pt.list_price,
                   pol.price_subtotal,
                   pol.price_subtotal_incl
            FROM pos_order_line pol
            JOIN pos_order po ON po.id = pol.order_id
            JOIN pos_session ps ON ps.id = po.session_id
            JOIN pos_config pc ON pc.id = ps.config_id
            JOIN product_product pp ON pp.id = pol.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
            WHERE pol.id IN %s
              AND pt.list_price > 0
              AND pol.price_unit * (1 - COALESCE(pol.discount, 0) / 100.0) < pt.list_price * 0.999
            ORDER BY po.date_order DESC
            LIMIT %s
        """, (tuple(line_ids), limit))

        rows = []
        for (date_order, ticket, magasin, tmpl_id, ref, produit,
             qty, list_price, sous_total_ht, ca) in request.env.cr.fetchall():
            # Prix affichés en TTC : c'est ce que le client paie réellement
            # au comptoir, et c'est déjà la base du CA encaissé.
            ratio = self._ttc_ratio(sous_total_ht, ca)
            catalogue_ttc = float(list_price or 0.0) * ratio
            paye_ttc = self._unit_price_ttc(ca, qty)
            rows.append({
                'id': tmpl_id,
                'date': str(date_order)[:16] if date_order else '—',
                'ticket': ticket or '—',
                'magasin': magasin or '—',
                'ref': ref or '—',
                'name': produit or '—',
                'qty': int(qty or 0),
                'prix_catalogue': round(catalogue_ttc, 2),
                'prix_paye': round(paye_ttc, 2),
                'remise_pct': (
                    round((catalogue_ttc - paye_ttc) / catalogue_ttc * 100, 1)
                    if catalogue_ttc > 0 else 0.0
                ),
                'ca': round(float(ca or 0.0), 2),
            })
        return rows

    @http.route('/mavie/api/history', type='json', auth='user', methods=['POST'], csrf=False)
    def api_history(self, **kw):
        try:
            transfers = self._transfer_history_rows(kw)
            soldes = self._solde_history_rows(kw)
            return {
                'transfers': transfers,
                'transfers_count': len(transfers),
                'transfers_qty': sum(t['qty'] for t in transfers),
                'soldes': soldes,
                'soldes_count': len(soldes),
                'soldes_qty': sum(s['qty'] for s in soldes),
                'soldes_ca': round(sum(s['ca'] for s in soldes), 2),
            }
        except Exception as e:
            _logger.error(f"Erreur api_history: {str(e)}", exc_info=True)
            return {'error': str(e), 'transfers': [], 'soldes': []}

    @http.route('/mavie/api/history/export', type='http', auth='user', methods=['GET'], csrf=False)
    def api_history_export(self, **kw):
        kind = kw.get('kind') or 'transferts'
        buffer = io.StringIO()
        buffer.write(u'﻿')  # BOM pour qu'Excel détecte l'UTF-8
        writer = csv.writer(buffer, delimiter=';')

        try:
            if kind == 'soldes':
                writer.writerow(['Historique des ventes en solde'])
                writer.writerow([])
                # Même ordre qu'à l'écran (catalogue → remise → prix payé).
                # Le CA encaissé, retiré de l'écran, reste dans l'export :
                # un extrait a vocation à être complet.
                writer.writerow(['Date', 'Ticket', 'Magasin', 'Réf', 'Produit', 'Qté',
                                 'Prix catalogue (TTC)', 'Remise (%)', 'Prix payé (TTC)',
                                 'CA encaissé (TTC)'])
                for row in self._solde_history_rows(kw):
                    writer.writerow([
                        row['date'], row['ticket'], row['magasin'], row['ref'], row['name'],
                        row['qty'], row['prix_catalogue'], row['remise_pct'],
                        row['prix_paye'], row['ca'],
                    ])
                filename = 'mavie_historique_soldes.csv'
            else:
                writer.writerow(['Historique des transferts entre magasins'])
                writer.writerow([])
                writer.writerow(['Bon', 'Date', 'État', 'Société source', 'Magasin source',
                                 'Société cible', 'Magasin cible', 'Type', 'Références',
                                 'Lignes', 'Qté totale', 'Groupe'])
                for row in self._transfer_history_rows(kw):
                    writer.writerow([
                        row['name'], row['date'], row['state_label'],
                        row['source_societe'], row['source_magasin'],
                        row['dest_societe'], row['dest_magasin'],
                        'Intra-société' if row['intra_societe'] else 'Inter-sociétés',
                        row['nb_references'], row['nb_lignes'], row['qty'], row['group_ref'],
                    ])
                filename = 'mavie_historique_transferts.csv'
        except Exception as e:
            _logger.error(f"Erreur api_history_export: {str(e)}", exc_info=True)
            return request.make_response(
                'Erreur : ' + str(e),
                headers=[('Content-Type', 'text/plain; charset=utf-8')],
                status=500,
            )

        return request.make_response(
            buffer.getvalue(),
            headers=[
                ('Content-Type', 'text/csv; charset=utf-8'),
                ('Content-Disposition', f'attachment; filename="{filename}"'),
            ],
        )


    # ─────────────────────────────────────────────────────────────
    # HISTORIQUE D'UNE RÉFÉRENCE (transferts + soldes)
    #
    # DEMANDE UTILISATEUR : depuis la fiche produit, un bouton qui montre
    # tout ce qui a été fait sur CETTE référence — transferts et ventes en
    # solde, avec la date, le magasin et l'état.
    #
    # Différence assumée avec la section "Historique" du tableau de bord :
    # celle-ci ne liste que les transferts lancés depuis le dashboard
    # (created_from_dashboard), pour repartir de zéro. Ici on veut au
    # contraire l'histoire COMPLÈTE de la référence, quel que soit le canal
    # par lequel le bon a été créé — c'est le sens de "tout ce qui est fait
    # pour ce produit".
    # ─────────────────────────────────────────────────────────────

    @http.route('/mavie/api/product-history', type='json', auth='user', methods=['POST'], csrf=False)
    def api_product_history(self, **kw):
        try:
            product_tmpl_id = kw.get('article_id') or kw.get('product_tmpl_id')
            if not product_tmpl_id:
                return {'error': 'Référence manquante.', 'transfers': [], 'soldes': []}
            product_tmpl_id = int(product_tmpl_id)

            product_tmpl = request.env['product.template'].sudo().browse(product_tmpl_id)
            if not product_tmpl.exists():
                return {'error': 'Référence introuvable.', 'transfers': [], 'soldes': []}

            # active_test=False : une variante archivée reste présente dans
            # l'historique (un transfert passé la référence toujours).
            variants = request.env['product.product'].sudo().with_context(
                active_test=False
            ).search([('product_tmpl_id', '=', product_tmpl_id)])
            if not variants:
                return {'transfers': [], 'soldes': [], 'ref': product_tmpl.default_code or ''}

            # ── Transferts inter-magasins ──
            state_labels = {
                'draft': 'Brouillon',
                'submitted': 'En attente de validation',
                'done': 'Fait',
            }
            transfers = []
            try:
                lines = request.env['inter.internal.transfer.line'].sudo().search(
                    [('product_id', 'in', variants.ids)], order='id desc', limit=500
                )
                # Le magasin réel (et non la société) est déjà résolu par les
                # champs stockés emetteur_display / recepteur_display, ajoutés
                # à inter.internal.transfer par ce module.
                for line in lines:
                    transfer = line.transfer_id
                    if not transfer:
                        continue
                    color, size = resolve_variant_color_size(line.product_id)
                    transfers.append({
                        'transfer_id': transfer.id,
                        'name': transfer.name,
                        'date': str(transfer.create_date)[:16] if transfer.create_date else '—',
                        'state': transfer.state,
                        'state_label': state_labels.get(transfer.state, transfer.state),
                        'source_magasin': transfer.emetteur_display or '—',
                        'source_societe': transfer.company_source_id.name or '—',
                        'dest_magasin': transfer.recepteur_display or '—',
                        'dest_societe': transfer.company_target_id.name or '—',
                        'intra_societe': transfer.company_source_id.id == transfer.company_target_id.id,
                        'depuis_dashboard': bool(transfer.created_from_dashboard),
                        'couleur': (color or '—').upper() if color else '—',
                        'taille': size or '—',
                        'qty': int(line.quantity or 0),
                    })
            except Exception as e:
                _logger.warning("Historique transferts indisponible pour %s: %s", product_tmpl_id, e)

            # ── Ventes en solde (sous le prix catalogue) ──
            soldes = []
            has_promo_campaign = 'promo_campaign_id' in request.env['pos.order']._fields
            has_reward_id = 'reward_id' in request.env['pos.order.line']._fields

            promo_col = "po.promo_campaign_id" if has_promo_campaign else "NULL AS promo_campaign_id"
            reward_col = "pol.reward_id" if has_reward_id else "NULL AS reward_id"

            query = f"""
                SELECT po.date_order,
                       po.name,
                       pc.name AS magasin,
                       rc.name AS societe,
                       pol.product_id,
                       pol.qty,
                       pt.list_price,
                       pol.price_subtotal,
                       pol.price_subtotal_incl,
                       COALESCE(pol.discount, 0) AS line_discount,
                       pl.name AS pricelist_name,
                       {promo_col},
                       {reward_col}
                FROM pos_order_line pol
                JOIN pos_order po ON po.id = pol.order_id
                JOIN pos_session ps ON ps.id = po.session_id
                JOIN pos_config pc ON pc.id = ps.config_id
                LEFT JOIN res_company rc ON rc.id = po.company_id
                LEFT JOIN product_pricelist pl ON pl.id = po.pricelist_id
                JOIN product_product pp ON pp.id = pol.product_id
                JOIN product_template pt ON pt.id = pp.product_tmpl_id
                WHERE pp.product_tmpl_id = %s
                  AND po.state IN ('paid', 'done', 'invoiced')
                  AND pol.is_reward_line IS NOT TRUE
                  AND pt.list_price > 0
                  AND pol.price_unit * (1 - COALESCE(pol.discount, 0) / 100.0) < pt.list_price * 0.999
                ORDER BY po.date_order DESC
                LIMIT 500
            """
            request.env.cr.execute(query, (product_tmpl_id,))

            variant_by_id = {v.id: v for v in variants}
            for (date_order, ticket, magasin, societe, pid, qty,
                 list_price, sous_total_ht, ca, line_discount, pricelist_name,
                 promo_campaign_id, reward_id) in request.env.cr.fetchall():
                qty = int(qty or 0)
                ratio = self._ttc_ratio(sous_total_ht, ca)
                list_price = float(list_price or 0.0) * ratio
                prix_paye = self._unit_price_ttc(ca, qty)
                variant = variant_by_id.get(pid)
                color, size = resolve_variant_color_size(variant) if variant else (None, None)

                est_retour = qty < 0 or prix_paye < 0
                is_from_promo_campaign = bool(promo_campaign_id) or bool(reward_id)
                is_from_special_pricelist = False
                if pricelist_name:
                    pl_name_lower = str(pricelist_name).lower().strip()
                    if 'par défaut' not in pl_name_lower and 'public pricelist' not in pl_name_lower:
                        is_from_special_pricelist = True

                if est_retour:
                    solde_kind = 'retour'
                elif is_from_promo_campaign or is_from_special_pricelist or (float(line_discount or 0.0) <= 0.01 and prix_paye < list_price * 0.999):
                    solde_kind = 'solde_lance'
                else:
                    solde_kind = 'remise_magasin'

                soldes.append({
                    'date': str(date_order)[:16] if date_order else '—',
                    'ticket': ticket or '—',
                    'magasin': magasin or '—',
                    'societe': societe or '—',
                    'couleur': (color or '—').upper() if color else '—',
                    'taille': size or '—',
                    'qty': qty,
                    'type': 'retour' if est_retour else solde_kind,
                    'solde_kind': solde_kind,
                    'line_discount': round(float(line_discount or 0.0), 1),
                    'prix_catalogue': round(list_price, 2),
                    'prix_paye': round(prix_paye, 2),
                    'remise_pct': (
                        round((list_price - prix_paye) / list_price * 100, 1)
                        if list_price > 0 and not est_retour else None
                    ),
                    'ca': round(float(ca or 0.0), 2),
                })

            lances_items = [s for s in soldes if s['solde_kind'] == 'solde_lance']
            remises_items = [s for s in soldes if s['solde_kind'] == 'remise_magasin']
            retours_items = [s for s in soldes if s['solde_kind'] == 'retour']

            return {
                'ref': product_tmpl.base_pivot_reference or product_tmpl.default_code or product_tmpl.name,
                'name': product_tmpl.name,
                'transfers': transfers,
                'transfers_count': len(transfers),
                'transfers_qty': sum(t['qty'] for t in transfers),
                'transfers_bons': len({t['transfer_id'] for t in transfers}),
                'soldes': soldes,
                'soldes_count': len(soldes),
                'soldes_qty': sum(s['qty'] for s in soldes),
                'soldes_ca': round(sum(s['ca'] for s in soldes), 2),
                'soldes_lances_count': len(lances_items),
                'soldes_lances_qty': sum(s['qty'] for s in lances_items),
                'soldes_lances_ca': round(sum(s['ca'] for s in lances_items), 2),
                'remises_magasin_count': len(remises_items),
                'remises_magasin_qty': sum(s['qty'] for s in remises_items),
                'remises_magasin_ca': round(sum(s['ca'] for s in remises_items), 2),
                'retours_count': len(retours_items),
            }
        except Exception as e:
            _logger.error(f"Erreur api_product_history: {str(e)}", exc_info=True)
            return {'error': str(e), 'transfers': [], 'soldes': []}
