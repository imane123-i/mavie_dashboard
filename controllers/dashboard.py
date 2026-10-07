# -*- coding: utf-8 -*-
from odoo import http, fields
from odoo.http import request, Response
from odoo.exceptions import UserError
import json
import re
import base64
import os
import time
import csv
import io
from datetime import datetime, timedelta, date
import logging
import math
import heapq
from markupsafe import Markup, escape

from ..models.mv_batch_shop_mapping_ext import CITY_PROXIMITY

_logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# CACHE COURT DES ÉCRANS LOURDS (A18, 2026-09-24)
#
# Constat : le premier affichage demande 5 à 30 secondes selon la taille
# de la base (mesuré : 5,6 s sur Elite, 33 s sur MaVie), parce que chaque
# rafraîchissement refait la totalité des agrégats ventes/achats/stock.
# Changer de page ou revenir en arrière relançait tout.
#
# On garde donc le résultat quelques secondes, par base, par utilisateur
# et par jeu de filtres. Deux garde-fous pour ne jamais afficher un
# chiffre périmé après une action :
#   • toute écriture faite depuis le dashboard (transfert, solde,
#     réassort) vide le cache immédiatement (_vider_cache_dashboard) ;
#   • au-delà de CACHE_TTL secondes, l'entrée est recalculée de toute
#     façon.
# ─────────────────────────────────────────────────────────────
# En dessous de ce pourcentage de pièces ayant un coût réellement saisi
# dans Odoo, la « valeur au coût » ne repose sur rien : les trois écrans qui
# l'affichent (carte, tableau par société, pop-up par magasin) annoncent
# alors « non disponible » au lieu d'un montant estimé. Une seule règle, pour
# qu'ils ne se contredisent jamais (constaté le 2026-09-25 : la carte disait
# « non disponible » pendant que le tableau affichait 754 220,93).
COUT_COUVERTURE_MIN = 5.0   # %

CACHE_TTL = 90          # secondes
CACHE_MAX = 40          # entrées gardées au maximum
_CACHE = {}


def _vider_cache_dashboard():
    """Après une écriture : le prochain affichage doit tout recalculer."""
    _CACHE.clear()


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


def _arrondi_prix_solde(p):
    """Prix soldé arrondi à l'entier : sous 0,50 on descend (99,30 -> 99), à partir de 0,50 on monte (99,60 -> 100)."""
    n = int(p // 1)
    return float(n) if round(p - n, 2) < 0.5 else float(n + 1)


def _fr_nombre(valeur):
    """Nombre a la francaise : espace pour les milliers, virgule pour les
    decimales. Les reponses de l'assistant sont lues telles quelles."""
    try:
        v = float(valeur or 0)
    except (TypeError, ValueError):
        return str(valeur)
    txt = '{:,.0f}'.format(v) if v == int(v) else '{:,.2f}'.format(v)
    entier, _, decimales = txt.partition('.')
    entier = entier.replace(',', ' ')
    return (entier + ',' + decimales) if decimales else entier


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

    def _cache_cle(self, nom, kw):
        """Clé du cache : base, utilisateur, sociétés cochées, filtres."""
        try:
            filtres = tuple(sorted(
                (k, str(v)) for k, v in (kw or {}).items()
                if k not in ('_', 'callback')))
            return (request.env.cr.dbname, request.env.uid,
                    tuple(sorted(self._get_context_company_ids())), nom, filtres)
        except Exception:
            return None

    def _cache_lire(self, cle):
        if not cle:
            return None
        entree = _CACHE.get(cle)
        if not entree:
            return None
        pose_a, valeur = entree
        if time.time() - pose_a > CACHE_TTL:
            _CACHE.pop(cle, None)
            return None
        return valeur

    def _cache_ecrire(self, cle, valeur):
        if not cle or not isinstance(valeur, dict) or valeur.get('error'):
            return valeur
        if len(_CACHE) >= CACHE_MAX:
            # On retire la plus ancienne plutôt que de laisser grossir.
            plus_vieille = min(_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _CACHE.pop(plus_vieille, None)
        _CACHE[cle] = (time.time(), valeur)
        return valeur

    def _societe_depot(self):
        """La société entrepôt/importateur : celle qui achète aux vrais
        fournisseurs puis revend aux sociétés magasin.

        Elle s'appelle MOD FOR LIFE chez MaVie, mais pas ailleurs (sur la
        base Elite c'est STE XD MAX AMUSEMENT TECHNOLOGY). On lit donc
        d'abord le paramètre de Base Pivot — Paramètres généraux → Base
        Pivot, « Société importatrice », le même que celui utilisé pour
        générer les achats et les ventes inter-sociétés — et on retombe sur
        le nom historique quand ce paramètre n'existe pas, pour que rien ne
        change sur la base MaVie.
        """
        Company = request.env['res.company'].sudo()
        param = request.env['ir.config_parameter'].sudo().get_param(
            'mv_base_pivot.default_importer_company_id')
        if param:
            try:
                depot = Company.browse(int(param)).exists()
            except (TypeError, ValueError):
                depot = Company
            if depot:
                return depot
        return Company.search([('name', '=', 'MOD FOR LIFE')], limit=1)

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
        # Le dépôt ne s'appelle pas MOD FOR LIFE partout : on l'ajoute par
        # son identifiant, lu dans le paramètre de Base Pivot.
        return list({c.id for c in companies} | set(self._societe_depot().ids))

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

        # A02 (2026-09-25) : le dépôt achète au fournisseur, revend aux
        # magasins, qui revendent au client. Quand il est coché EN MÊME
        # TEMPS que des magasins, additionner les deux compte la même
        # marchandise deux fois. Mesuré sur Elite avec les trois sociétés
        # cochées — l'état par défaut du sélecteur Odoo :
        #   CA        2 989 731,90  ->  4 056 357,12
        #   vendues        15 673   ->      111 015
        #   achetées       81 347   ->      169 290
        #   sell-through     19,3 % ->         65,6 %
        # Le dépôt reste donc exclu tant qu'un magasin est coché. Coché
        # SEUL, il bascule sur sa vue dédiée (_compute_kpis_modforlife),
        # qui montre ses achats fournisseurs et ses dispatchs.
        magasins_coches = [cid for cid in context_company_ids
                           if cid not in non_retail_ids]
        if magasins_coches:
            return non_retail_ids
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
        company = self._societe_depot()
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
        # Page Action (et son bouton Réassort) : menu « Société » propre à la
        # page. Historique (2026-09-22) : avec plusieurs sociétés cochées,
        # cliquer le nom d'une société dans Odoo ne décoche pas les autres
        # (elle passe seulement en tête du cookie) ; « société courante seule »
        # a été essayé puis refusé (« j'ai tout coché mais je n'ai que
        # SALMEDO ») : Odoo ne distingue pas les deux gestes. La page envoie
        # donc la société voulue, limitée aux sociétés cochées.
        forcee = getattr(request, '_mavie_societe_id', None)
        if forcee and (forcee in ids or not ids):
            return [forcee]
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

    def _group_sums(self, model_name, domain, sum_fields, group_fields=('product_id',), agg='SUM', ctx=None):
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
        if ctx:
            Model = Model.with_context(**ctx)
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

    def _fichiers_images_presents(self, attachments):
        """Ne garde que les pièces jointes dont le FICHIER existe vraiment.

        Constaté sur la base Elite (2026-09-24) : la base référence 12 532
        photos, mais l'export est arrivé sans les fichiers. Odoo annonçait
        donc une photo pour des fiches qui n'en affichaient qu'une icône
        cassée. On vérifie la présence du fichier avant de promettre une
        image ; un fichier absent est traité comme « pas de photo ».
        """
        if not attachments:
            return []
        try:
            racine = request.env['ir.attachment'].sudo()._filestore()
        except Exception:
            return attachments
        gardes = []
        for row in attachments:
            nom = row.get('store_fname')
            # Pièce jointe stockée en base (db_datas) : pas de fichier à
            # vérifier, on la garde.
            if not nom or os.path.exists(os.path.join(racine, nom)):
                gardes.append(row)
        return gardes

    def _ruptures_par_magasin(self, tmpl_ids, kw=None):
        """Références vendues qui sont à zéro dans AU MOINS un magasin.

        Le compte « ruptures » historique raisonne toutes boutiques
        confondues : une référence présente ailleurs n'y apparaît pas, même
        si le magasin qui la vend est vide (A08). On compte ici les couples
        référence × magasin manquants, sur le périmètre affiché.
        """
        vide = {'count': 0, 'refs': 0, 'lignes': []}
        if not tmpl_ids:
            return vide
        mappings = self._get_active_shop_mappings()
        wh_ids = [m.warehouse_id.id for m in mappings if m.warehouse_id]
        if kw and kw.get('shop_field'):
            choisi = [m.warehouse_id.id for m in mappings
                      if m.shop_field == kw.get('shop_field') and m.warehouse_id]
            wh_ids = choisi or wh_ids
        exclues = self._get_excluded_non_retail_ids(kw)
        wh_ids = [w for w in wh_ids if w] or []
        if not wh_ids:
            return vide
        request.env.cr.execute("""
            WITH mag AS (
                SELECT w.id, w.name, w.view_location_id, w.company_id
                  FROM stock_warehouse w
                 WHERE w.id IN %(wh)s
                   AND (%(nb_exclues)s = 0 OR w.company_id <> ALL(%(exclues)s))
            ), stk AS (
                SELECT pp.product_tmpl_id AS tid, m.id AS wh_id, SUM(q.quantity) AS qte
                  FROM stock_quant q
                  JOIN stock_location l ON l.id = q.location_id
                  JOIN mag m ON l.parent_path LIKE '%%/' || m.view_location_id || '/%%'
                  JOIN product_product pp ON pp.id = q.product_id
                 WHERE l.usage = 'internal' AND pp.product_tmpl_id IN %(tids)s
                 GROUP BY 1, 2
            )
            SELECT t.tid, m.name,
                   COALESCE(NULLIF(pt.base_pivot_reference, ''), NULLIF(pt.default_code, ''),
                            pt.name->>'fr_FR', pt.name->>'en_US')
              FROM (SELECT UNNEST(%(tids_arr)s) AS tid) t
              CROSS JOIN mag m
              LEFT JOIN stk ON stk.tid = t.tid AND stk.wh_id = m.id
              JOIN product_template pt ON pt.id = t.tid
             WHERE COALESCE(stk.qte, 0) <= 0
             ORDER BY 3, 2
        """, {'wh': tuple(wh_ids), 'tids': tuple(tmpl_ids),
               'tids_arr': list(tmpl_ids), 'exclues': exclues or [0],
               'nb_exclues': len(exclues or [])})
        lignes = [{'id': tid, 'magasin': mag, 'ref': ref or '—'}
                  for tid, mag, ref in request.env.cr.fetchall()]
        return {'count': len(lignes), 'refs': len({l['id'] for l in lignes}),
                'lignes': lignes[:500]}

    def _alerte_rupture_par_magasin(self, kw, tmpl_ids, tmpl_by_id,
                                    days_in_period, limite_jours=30):
        """Références qui vont manquer DANS UN MAGASIN sous 30 jours.

        Une ligne = une référence dans un magasin : le stock affiché est
        celui de ce magasin, la vitesse de vente est celle de ses caisses.
        C'est la seule façon d'avoir un « jours restants » vrai — le stock
        du réseau ne dit rien de la boutique qui va se retrouver vide.
        """
        if not tmpl_ids:
            return []
        mappings = [m for m in self._get_active_shop_mappings() if m.warehouse_id]
        if kw.get('shop_field'):
            mappings = [m for m in mappings if m.shop_field == kw['shop_field']] or mappings
        wh_ids = [m.warehouse_id.id for m in mappings]
        noms = {m.warehouse_id.id: m.warehouse_id.name for m in mappings}
        champs = {m.warehouse_id.id: m.shop_field for m in mappings}
        if not wh_ids:
            return []

        # Stock par (référence, entrepôt).
        request.env.cr.execute("""
            SELECT pp.product_tmpl_id, w.id, SUM(q.quantity)
              FROM stock_quant q
              JOIN stock_location l ON l.id = q.location_id
              JOIN stock_warehouse w ON l.parent_path LIKE '%%/' || w.view_location_id || '/%%'
              JOIN product_product pp ON pp.id = q.product_id
             WHERE l.usage = 'internal' AND w.id = ANY(%(wh)s)
               AND pp.product_tmpl_id = ANY(%(tids)s)
             GROUP BY 1, 2
            HAVING SUM(q.quantity) > 0
        """, {'wh': wh_ids, 'tids': list(tmpl_ids)})
        stock = {(t, w): q for t, w, q in request.env.cr.fetchall()}
        if not stock:
            return []

        # Ventes par (référence, entrepôt) sur la même fenêtre que la
        # vitesse générale : la période choisie, sinon 90 jours glissants.
        debut = kw.get('date_start')
        fin = kw.get('date_end')
        if not debut:
            debut = (datetime.now() - timedelta(days=days_in_period)).strftime('%Y-%m-%d')
        params = {'wh': wh_ids, 'tids': list(tmpl_ids), 'debut': debut + ' 00:00:00'}
        borne_fin = " AND o.date_order <= %(fin)s" if fin else ""
        if fin:
            params['fin'] = fin + ' 23:59:59'
        request.env.cr.execute("""
            SELECT pp.product_tmpl_id, w.id, SUM(pol.qty)
              FROM pos_order_line pol
              JOIN pos_order o ON o.id = pol.order_id
              JOIN pos_session ps ON ps.id = o.session_id
              JOIN pos_config pc ON pc.id = ps.config_id
              JOIN stock_picking_type spt ON spt.id = pc.picking_type_id
              JOIN stock_warehouse w ON w.id = spt.warehouse_id
              JOIN product_product pp ON pp.id = pol.product_id
             WHERE o.state IN ('paid', 'done', 'invoiced')
               -- is_reward_line vaut NULL sur les lignes jamais touchées
               -- par la fidélité (15 362 sur 15 365 ici) : « = false » les
               -- écartait toutes et l'alerte sortait vide.
               AND COALESCE(pol.is_reward_line, false) = false
               AND w.id = ANY(%(wh)s) AND pp.product_tmpl_id = ANY(%(tids)s)
               AND o.date_order >= %(debut)s""" + borne_fin + """
             GROUP BY 1, 2
        """, params)
        ventes = {(t, w): q for t, w, q in request.env.cr.fetchall()}

        lignes = []
        for (tid, wh_id), stk in stock.items():
            vendu = ventes.get((tid, wh_id)) or 0
            if vendu <= 0:
                continue
            par_jour = vendu / float(days_in_period or 1)
            jours = round(stk / par_jour, 1)
            if jours > limite_jours:
                continue
            t = tmpl_by_id.get(tid, {})
            lignes.append({
                'id': tid,
                'name': t.get('name') or '—',
                'ref': (t.get('base_pivot_reference') or t.get('default_code')
                        or t.get('name') or '—'),
                'magasin': noms.get(wh_id) or '—',
                # Permet d'ouvrir la fiche produit filtrée sur CE magasin :
                # sinon le pop-up montre le réseau entier et ses chiffres ne
                # correspondent pas à la ligne cliquée.
                'shop_field': champs.get(wh_id),
                'stock': int(stk),
                'qty_sold': int(vendu),
                'daily_rate': round(par_jour, 2),
                'days_left': jours,
            })
        lignes.sort(key=lambda x: x['days_left'])
        return lignes[:200]

    def _receptions_recentes(self, tmpl_ids, jours=90):
        """{product_tmpl_id: 'AAAA-MM-JJ'} des articles reçus récemment.

        Réception = mouvement validé entrant depuis un fournisseur ou une
        autre société. Sert à ne pas traiter en « stock dormant » un article
        arrivé il y a quelques jours (A09).
        """
        if not tmpl_ids:
            return {}
        depuis = (datetime.now() - timedelta(days=jours)).strftime('%Y-%m-%d 00:00:00')
        request.env.cr.execute("""
            SELECT pp.product_tmpl_id, MAX(sml.date)::date
              FROM stock_move_line sml
              JOIN product_product pp ON pp.id = sml.product_id
              JOIN stock_location src ON src.id = sml.location_id
              JOIN stock_location dst ON dst.id = sml.location_dest_id
             WHERE sml.state = 'done' AND sml.date >= %s
               AND dst.usage = 'internal' AND src.usage <> 'internal'
               AND pp.product_tmpl_id IN %s
             GROUP BY 1
        """, (depuis, tuple(tmpl_ids)))
        return {tid: str(d) for tid, d in request.env.cr.fetchall()}

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
        ], ['res_id', 'store_fname'])
        for row in self._fichiers_images_presents(tmpl_with_image):
            result[row['res_id']] = 'product'

        missing = [tid for tid, src in result.items() if not src]
        if missing:
            try:
                articles = request.env['mv.article.base'].sudo().search_read(
                    [('product_tmpl_id', 'in', missing), ('image_1920', '!=', False)],
                    ['id', 'product_tmpl_id'],
                )
                presents = {r['res_id'] for r in self._fichiers_images_presents(
                    request.env['ir.attachment'].sudo().search_read([
                        ('res_model', '=', 'mv.article.base'),
                        ('res_field', '=', 'image_1920'),
                        ('res_id', 'in', [a['id'] for a in articles]),
                    ], ['res_id', 'store_fname']))}
                articles = [a for a in articles if a['id'] in presents]
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

    def _categories_choisies(self, kw):
        """Catégories cochées dans le filtre, en IDs.

        Le filtre acceptait une seule catégorie (`categ_id`). Il en accepte
        maintenant plusieurs (`categ_ids`) — l'ancienne clé reste comprise
        pour ne rien casser ailleurs.
        """
        brut = (kw or {}).get('categ_ids')
        if brut in (None, '', []):
            brut = (kw or {}).get('categ_id')
        if brut in (None, '', []):
            return []
        if not isinstance(brut, (list, tuple, set)):
            brut = [x for x in str(brut).split(',') if x.strip()]
        ids = []
        for v in brut:
            try:
                ids.append(int(v))
            except (TypeError, ValueError):
                continue
        return ids

    def _filtre_produit_actif(self, kw):
        """Un filtre catégorie / collection / arrivage est-il posé ?"""
        return bool((kw or {}).get('collection_id') or (kw or {}).get('batch_id')
                    or self._categories_choisies(kw))

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

        categ_ids = self._categories_choisies(kw)
        if categ_ids:
            try:
                categ_id = categ_ids if len(categ_ids) > 1 else categ_ids[0]
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

        # DEMANDE UTILISATRICE (2026-09-25) : « Qté achetée et CA Achat
        # doivent être fixes, même si je change la date ». La marchandise
        # est achetée en une fois puis vendue sur des mois : filtrer les
        # achats sur la même période que les ventes faisait tomber la
        # quantité achetée à presque rien et rendait le sell-through
        # illisible. Le filtre Période ne s'applique donc plus aux achats.

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
                # L'entrepôt du magasin n'est ajouté que s'il reçoit
                # réellement des bons d'achat. Sur Elite, les 118 bons des
                # magasins sont tous réceptionnés dans l'entrepôt GÉNÉRIQUE
                # de la société (« SQUARE TARGA »), jamais dans celui de la
                # boutique : ce filtre vidait donc la Qté achetée dès qu'on
                # choisissait un magasin, et la fiche produit affichait
                # « aucune commande fournisseur » avec des « — » partout.
                # Sur MaVie, où les bons visent bien l'entrepôt du magasin
                # (MARINA AGADIR : 299 bons), le filtre s'applique comme
                # avant.
                if scope['warehouse'] and self._entrepot_recoit_des_achats(scope['warehouse'].id):
                    domain.append(('order_id.picking_type_id.warehouse_id', '=', scope['warehouse'].id))
        else:
            # Pas de magasin précis choisi dans le dashboard : on retombe sur
            # la/les société(s) cochée(s) dans le sélecteur standard Odoo.
            context_company_ids = self._get_context_company_ids()
            if context_company_ids:
                domain.append(('order_id.company_id', 'in', context_company_ids))
        return domain

    def _achats_depot_externes_domain(self, kw, product_tmpl_ids):
        """Achats du dépôt chez de VRAIS fournisseurs (hors inter-sociétés).

        Constaté sur Elite le 2026-09-25 : les magasins n'achètent rien à
        l'extérieur, la totalité de leurs bons vient de la société dépôt.
        L'argent réellement dépensé chez les fournisseurs est donc porté par
        le dépôt — 9 504 DH, dont toute la catégorie BALLERINES. Sans ces
        lignes, « Analyse des achats » d'Odoo affichait 1 247 499,87 et la
        carte CA Achat 1 237 995,87, et une catégorie achetée uniquement par
        le dépôt tombait à zéro.

        Les quantités de ce domaine ne sont PAS ajoutées : les pièces sont
        déjà comptées à la réception des magasins (A02, double comptage).
        """
        depot = self._societe_depot()
        if not depot or depot.id not in self._get_context_company_ids():
            return None
        if kw.get('shop_field'):
            # Un magasin précis est demandé : le dépôt n'en est pas un.
            return None
        partenaires_societes = [
            c.partner_id.id for c in request.env['res.company'].sudo().search([])
            if c.partner_id
        ]
        domain = [
            ('order_id.state', 'in', ['purchase', 'done']),
            ('order_id.company_id', '=', depot.id),
        ]
        if partenaires_societes:
            domain.append(('order_id.partner_id', 'not in', partenaires_societes))
        domain += self._sachet_exclude_domain('product_id.product_tmpl_id.collection_id')
        if product_tmpl_ids is not None:
            domain.append(('product_id.product_tmpl_id', 'in', product_tmpl_ids))
        # Comme pour les achats des magasins : pas de filtre Période.
        return domain

    def _entrepot_recoit_des_achats(self, warehouse_id):
        """Cet entrepôt est-il la destination d'au moins un bon d'achat ?

        Sert à savoir si filtrer les achats par magasin a un sens sur cette
        base. Le résultat est gardé le temps de la requête HTTP : la
        question revient pour chaque écran.
        """
        if not warehouse_id:
            return False
        cache = getattr(request, '_mavie_wh_achats', None)
        if cache is None:
            cache = {}
            setattr(request, '_mavie_wh_achats', cache)
        if warehouse_id not in cache:
            cache[warehouse_id] = bool(request.env['purchase.order'].sudo().search_count([
                ('state', 'in', ['purchase', 'done']),
                ('picking_type_id.warehouse_id', '=', warehouse_id),
            ]))
        return cache[warehouse_id]

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
        """Liste des magasins configurés et actifs.

        DEMANDE UTILISATEUR (2026-09-07) : « je dois avoir tout ce qui est
        dans la base et n'enlever rien ». L'exclusion en dur de
        `shop_field = 'shop'` (DIGITAL SHOP) a donc été retirée. Elle datait
        d'une époque où cette ligne était considérée comme un libellé
        générique ; vérifié en base, c'est un vrai magasin — 2 196 pièces en
        stock, 8 187 vendues en caisse, 7 596 réceptionnées. Le laisser
        dehors était la première cause des lignes « reçu ailleurs qu'ici » /
        « vendu depuis un autre entrepôt » du pop-up de réconciliation :
        ses achats et ses ventes étaient comptés (filtre par société) mais
        pas son stock (filtre par entrepôt).

        Le seul filtre restant est `active` — c'est la case à cocher du
        modèle, donc la décision revient à l'utilisateur dans Odoo, plus au
        code.

        DOUBLONS D'ENTREPÔT (constaté sur la base Elite le 2026-09-24) :
        deux mappings actifs peuvent pointer le MÊME entrepôt — « ELITE 01 »
        et « TARGA » désignent tous deux l'entrepôt Elite Carrefour Targa.
        Le magasin sortait alors deux fois dans les listes et son stock
        était compté deux fois dans les totaux par magasin. On n'en garde
        qu'un par entrepôt (le plus ancien, celui qui sert déjà de
        référence), sans rien modifier dans Base Pivot.
        """
        mappings = request.env['mv.batch.shop.mapping'].sudo().search(
            [('active', '=', True)], order='id')
        vus = set()
        gardes = mappings.browse()
        for m in mappings:
            cle = m.warehouse_id.id
            if cle and cle in vus:
                continue
            if cle:
                vus.add(cle)
            gardes |= m
        return gardes

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
        """Caisse e-commerce, reconnue à son nom.

        A16 (2026-09-24) : seuls les noms commençant par « Online » étaient
        reconnus. Sur Elite les caisses s'appellent « Elite Menara Mall —
        En ligne » : aucune n'était détectée. On accepte donc aussi « en
        ligne » et « e-commerce », où qu'ils soient dans le nom.
        """
        normalized = (name or '').strip().lower().replace('–', '-').replace('—', '-')
        return (normalized.startswith('online') or 'digital' in normalized
                or 'en ligne' in normalized or 'e-commerce' in normalized
                or 'ecommerce' in normalized)

    def _get_online_pos_configs(self):
        """pos.config considérés comme "magasin en ligne".

        Reconnaissance par le nom (préfixe « Online » ou « Digital »), le
        seul critère fiable en base : ces configs partagent l'entrepôt et le
        type d'opération de leur magasin physique, donc rien dans les
        relations ne permet de les distinguer.

        NUANCE AJOUTÉE (2026-09-07) : un point de vente n'est « en ligne »
        que s'il DOUBLE un magasin physique sur le même entrepôt — c'est
        tout l'intérêt de l'entrée dédiée, séparer les deux canaux. Quand
        une caisse au nom « online/digital » est la SEULE de son entrepôt
        (cas de DIGITAL SHOP, désormais réintégré comme magasin à part
        entière), elle EST le magasin : la traiter comme un doublon en ligne
        la ferait apparaître deux fois dans le filtre et viderait le magasin
        physique de toutes ses ventes.
        """
        configs = request.env['pos.config'].sudo().search([])
        online = configs.filtered(lambda c: self._is_online_config_name(c.name))
        physical_warehouse_ids = {
            c.picking_type_id.warehouse_id.id
            for c in (configs - online)
            if c.picking_type_id.warehouse_id
        }
        return online.filtered(
            lambda c: c.picking_type_id.warehouse_id.id in physical_warehouse_ids
        )

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

        # shop_field peut être un champ calculé (Base Pivot « magasins
        # dynamiques ») : on filtre au lieu de chercher en base.
        mapping = self._get_active_shop_mappings().filtered(
            lambda m: m.shop_field == shop_field)[:1]
        if not mapping:
            return None

        pos_config_ids = None
        if mapping.warehouse_id:
            configs = request.env['pos.config'].sudo().search([
                ('picking_type_id.warehouse_id', '=', mapping.warehouse_id.id)
            ])
            # On retire les caisses qui ont leur propre entrée « en ligne »
            # dans le filtre, pour ne pas compter leurs ventes deux fois.
            # On passe par _get_online_pos_configs (et non par le test sur
            # le nom) : une caisse au nom « digital » qui est la seule de son
            # entrepôt n'est PAS un doublon en ligne, c'est le magasin
            # lui-même — sans ça, sélectionner DIGITAL SHOP affichait son
            # stock mais zéro vente.
            online_ids = set(self._get_online_pos_configs().ids)
            configs = configs.filtered(lambda c: c.id not in online_ids)
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
        cle = self._cache_cle('kpis', kw)
        garde = self._cache_lire(cle)
        if garde is not None:
            return garde
        return self._cache_ecrire(cle, self._compute_kpis(kw))

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
                mod_for_life = self._societe_depot()
                if mod_for_life and self._get_context_company_ids() == [mod_for_life.id]:
                    return self._compute_kpis_modforlife(kw, mod_for_life)

            is_filtered = bool(self._filtre_produit_actif(kw))

            product_tmpl_ids = None
            if is_filtered:
                domain = self._build_product_domain(kw)
                # Les articles ARCHIVÉS gardent leurs achats, leurs ventes et leur
                # stock : « Analyse des achats » d'Odoo les compte, le
                # dashboard les perdait dès qu'on filtrait sur une
                # catégorie. Vérifié sur Elite : la catégorie BALLERINES
                # affichait 0 au lieu de 9 504 DH, tout l'achat étant sur
                # une référence archivée (AP3689-022).
                ProductTemplate = request.env['product.template'].sudo().with_context(
                    active_test=False)
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
                ['product_qty', 'qty_received', 'price_total', 'price_subtotal'],
            )
            # QTÉ ACHETÉE = quantité RÉELLEMENT REÇUE (qty_received), pas la
            # quantité commandée (product_qty). Odoo tient déjà qty_received
            # net des retours fournisseur : une réception suivie d'un retour
            # au fournisseur repasse le compteur à 0. En lisant product_qty,
            # le dashboard comptait comme "achetées" (1) les lignes de bons
            # confirmés jamais réceptionnées et (2) les marchandises
            # renvoyées au fournisseur — vérifié en base : 6 065 pièces dans
            # le premier cas, 7 748 dans le second, qui gonflaient
            # mécaniquement le stock théorique et donc l'écart affiché.
            # product_qty reste lu à côté : le prix d'achat moyen se calcule
            # sur la quantité COMMANDÉE, car price_subtotal porte lui aussi
            # sur la ligne commandée.
            qty_purchased_total = int(sum(g.get('qty_received') or 0 for g in po_grouped))
            qty_ordered_total = int(sum(g.get('product_qty') or 0 for g in po_grouped))
            # CA Achat = coût réel des marchandises achetées, tel que facturé
            # sur les bons de commande (pas une reconstruction
            # qty*prix_standard). DÉCISION UTILISATEUR (2026-08-18) : affiché
            # en TTC comme tous les autres CA du dashboard. La version HT
            # reste calculée à côté car la marge doit comparer du HT à du HT.
            ca_achat_total = sum(g.get('price_total') or 0.0 for g in po_grouped)
            ca_achat_ht_total = sum(g.get('price_subtotal') or 0.0 for g in po_grouped)

            # Achats du dépôt chez de vrais fournisseurs : c'est la dépense
            # réelle du groupe, et Odoo la compte dans « Analyse des achats ».
            # Montant seulement — les pièces sont déjà comptées côté magasins.
            ca_achat_depot = 0.0
            dom_depot = self._achats_depot_externes_domain(kw, product_tmpl_ids)
            if dom_depot:
                for g in self._group_sums('purchase.order.line', dom_depot,
                                          ['price_total', 'price_subtotal']):
                    ca_achat_depot += g.get('price_total') or 0.0
                    ca_achat_total += g.get('price_total') or 0.0
                    ca_achat_ht_total += g.get('price_subtotal') or 0.0


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
            # Quantité COMMANDÉE par référence — sert uniquement à diviser
            # price_subtotal pour obtenir un prix unitaire juste (le montant
            # facturé porte sur la ligne commandée, pas sur ce qui a été
            # réceptionné). Ne jamais l'utiliser comme "Qté achetée".
            ordered_by_tmpl = {}
            # CA Achat par référence — déjà présent dans po_grouped
            # (price_subtotal), il suffit de l'accumuler comme les quantités
            # pour pouvoir l'afficher dans les tableaux Top/Flop.
            ca_achat_by_tmpl = {}
            ca_achat_ht_by_tmpl = {}
            if po_pids:
                # active_test=False : une variante archivee garde ses achats,
                # comme dans « Analyse des achats » d'Odoo. Sans ca, le CA
                # Achat par reference perdait ces lignes.
                po_prods = request.env['product.product'].sudo().with_context(
                    active_test=False).search_read(
                    [('id', 'in', po_pids)],
                    ['id', 'product_tmpl_id']
                )
                po_prod_to_tmpl = {p['id']: p['product_tmpl_id'][0] for p in po_prods if p.get('product_tmpl_id')}
                for g in po_grouped:
                    pid = g['product_id'][0] if g.get('product_id') else None
                    tid = po_prod_to_tmpl.get(pid)
                    if not tid:
                        continue
                    purchase_by_tmpl[tid] = purchase_by_tmpl.get(tid, 0) + int(g.get('qty_received') or 0)
                    ordered_by_tmpl[tid] = ordered_by_tmpl.get(tid, 0) + int(g.get('product_qty') or 0)
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

            # STOCK PRESENT (negatifs exclus) — meme domaine, mais groupe
            # aussi par emplacement pour juger la positivite magasin par
            # magasin. Les stocks negatifs de cette base (59 650 pieces) ne
            # peuvent pas etre corriges dans Odoo par l'utilisateur : le
            # dashboard doit donc afficher le stock reellement en rayon, et
            # signaler les negatifs a cote au lieu de les soustraire.
            quant_by_loc = self._group_sums(
                'stock.quant', quant_domain, ['quantity'],
                group_fields=('product_id', 'location_id'),
            )
            stock_present, stock_negatif, nb_magasins_negatifs = self._split_positive_stock(
                quant_by_loc, self._location_to_warehouse(retail_lot_stock_ids)
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

                # Une référence ARCHIVÉE n'est pas « en rupture » : elle est
                # retirée du catalogue. Ses achats comptent bien dans le CA
                # Achat (comme dans Odoo), mais elle n'a rien à faire dans
                # les alertes. tmpl_by_id ne contient que les actives : on
                # s'appuie dessus plutôt que sur relevant_tmpl_ids, qui les
                # inclut depuis qu'on lit aussi les variantes archivées.
                for tid in tmpl_by_id:
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
            # A08 (2026-09-24) : une référence n'était « en rupture » que si
            # elle manquait dans TOUS les magasins à la fois. Un article
            # absent d'une seule boutique — le cas qui intéresse le magasin —
            # n'apparaissait nulle part. On ajoute le compte par magasin, le
            # total toutes boutiques confondues reste affiché à côté.
            ruptures_magasin = self._ruptures_par_magasin(
                set(sales_by_tmpl.keys()), kw)
            ruptures_list = sorted(all_ruptures, key=lambda a: a['qty_sold'], reverse=True)[:500]

            # DEMANDE UTILISATRICE (2026-09-25) : « je dois avoir TOUTES les
            # références, pas seulement celles vendues ou achetées ». La
            # carte comptait les références présentes en stock dans les
            # sociétés cochées — 1 215 sur un catalogue de 3 570. Elle compte
            # désormais le catalogue actif, restreint seulement par le filtre
            # catégorie / collection / arrivage. Le nombre de références qui
            # ont réellement bougé reste affiché juste en dessous.
            # Le filtre purchase_ok est retiré : sur Elite il ne laissait que
            # 1 380 fiches sur 3 570, alors que les autres se vendent bien.
            references_actives = len(relevant_tmpl_ids)
            if is_filtered and product_tmpl_ids:
                # ProductTemplate est ici en active_test=False (le filtre
                # doit retrouver les achats des articles archivés) : on
                # recompte sur les seuls articles actifs, comme le total
                # sans filtre.
                references_count = ProductTemplate.search_count(
                    self._sachet_exclude_domain('collection_id')
                    + [('id', 'in', product_tmpl_ids), ('active', '=', True)])
            else:
                references_count = ProductTemplate.search_count(
                    self._sachet_exclude_domain('collection_id'))

            # DEMANDE UTILISATRICE (2026-09-26) : la carte Références compte
            # tout le catalogue (3 570), les taux doivent se calculer sur la
            # même base, sinon deux dénominateurs coexistent à l'écran.
            # `references_actives` reste affiché à côté pour savoir combien
            # de références tournent réellement.
            total_active_skus = references_count or 1
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
            # On releve les QUANTITES, pas seulement la liste des articles
            # vendus : sans elles, impossible de savoir si une reference
            # vend assez vite pour ne pas dormir.
            pos_90d_grouped = request.env['pos.order.line'].sudo().read_group(
                pos_90d_domain,
                ['product_id', 'qty'],
                ['product_id'],
                lazy=False
            )
            qty_90d_by_pid = {g['product_id'][0]: float(g.get('qty') or 0.0)
                              for g in pos_90d_grouped if g.get('product_id')}
            pids_90d = list(qty_90d_by_pid)
            sold_90d_tmpl_ids = set()
            qty_90d_by_tmpl = {}
            if pids_90d:
                p90_prods = request.env['product.product'].sudo().search_read(
                    [('id', 'in', pids_90d)],
                    ['product_tmpl_id']
                )
                for prod in p90_prods:
                    if not prod.get('product_tmpl_id'):
                        continue
                    tid = prod['product_tmpl_id'][0]
                    sold_90d_tmpl_ids.add(tid)
                    qty_90d_by_tmpl[tid] = (qty_90d_by_tmpl.get(tid, 0.0)
                                            + qty_90d_by_pid.get(prod['id'], 0.0))

            # ✅ CORRECTION : le pourcentage pouvait dépasser 100% quand des
            # stock.quant négatifs (écarts d'inventaire) faisaient chuter
            # stock_total en dessous de la somme des stocks POSITIFS dormants
            # (celle-ci ignorant volontairement les négatifs). On calcule donc
            # le dénominateur de la même façon que le numérateur : uniquement
            # sur les stocks positifs, pour que le ratio reste borné à 100%.
            # A09 (2026-09-24) : un article RÉCEPTIONNÉ il y a moins de 90
            # jours n'a pas encore eu le temps de se vendre — le classer
            # « dormant » avec ceux qui traînent depuis un an est trompeur
            # (constaté sur Elite : MRC-8830 reçu la veille, 72 pièces par
            # magasin, compté dormant). On récupère la date de dernière
            # réception et on les met de côté, en les comptant à part.
            recus_recemment = self._receptions_recentes(relevant_tmpl_ids, jours=90)
            dormant_stock_total = 0
            positive_stock_total = 0
            dormant_products = []
            recents_count = 0
            recents_stock = 0
            lents_count = 0
            for tid in relevant_tmpl_ids:
                stock = stock_by_tmpl.get(tid, 0)
                if stock <= 0:
                    continue
                positive_stock_total += stock
                vendu_90j = qty_90d_by_tmpl.get(tid, 0.0)
                couverture = None
                if vendu_90j <= 0:
                    # Aucune vente. Une reference qui vient d'arriver n'a pas
                    # eu le temps : on la met de cote et on la compte a part.
                    if tid in recus_recemment:
                        recents_count += 1
                        recents_stock += stock
                        continue
                else:
                    # Elle vend : sa couverture est mesurable, la date de
                    # reception ne l'excuse plus.
                    # On arrondit AVANT de comparer : la liste affiche des
                    # jours entiers, la regle doit porter sur ce nombre-la.
                    couverture = round(stock / (vendu_90j / 90.0))
                    if couverture <= self.COUVERTURE_DORMANTE_JOURS:
                        continue
                    lents_count += 1
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
                    'stock': stock,
                    'recu_le': recus_recemment.get(tid) or '',
                    'vendu_90j': int(round(vendu_90j)),
                    'couverture_jours': (int(round(couverture))
                                         if couverture is not None else None),
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

            retours = []
            if page == 'stock':
                top_products = sorted(product_stats, key=lambda a: a['stock'], reverse=True)[:top_limit]
                flop_products = sorted(product_stats, key=lambda a: a['stock'])[:flop_limit]
            elif page == 'commandes':
                top_products = sorted(product_stats, key=lambda a: a['qty_purchased'], reverse=True)[:top_limit]
                flop_products = sorted(product_stats, key=lambda a: a['qty_purchased'])[:flop_limit]
            else:
                top_products = sorted(product_stats, key=lambda a: a['ca'], reverse=True)[:top_limit]
                # A10 (2026-09-24) : le Flop remontait d'abord les RETOURS —
                # une référence rendue par un client finit à quantité et CA
                # négatifs, donc tout en haut d'un tri croissant. Ce n'est
                # pas un produit qui se vend mal, c'est une vente annulée.
                # Le Flop ne garde donc que les références réellement
                # vendues ; les retours nets sont listés à part.
                retours = sorted([p for p in product_stats if (p.get('qty_sold') or 0) < 0],
                                 key=lambda a: a['qty_sold'])[:flop_limit]
                vendus = [p for p in product_stats if (p.get('qty_sold') or 0) > 0]
                flops_avec_stock = sorted([p for p in vendus if p['stock'] > 0], key=lambda a: a['ca'])
                if flops_avec_stock:
                    flop_products = flops_avec_stock[:flop_limit]
                else:
                    flop_products = sorted(vendus, key=lambda a: a['ca'])[:flop_limit]

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

            # DEMANDE UTILISATRICE (2026-09-23) : « je dois avoir les mêmes
            # quantités que dans Odoo ». L'écran Inventaire → Opérations →
            # Ajustements → Inventaire physique additionne TOUT : sachets,
            # articles archivés et stocks négatifs (Carrefour Agadir 8 475 =
            # 5 147 hors sachets + 3 328 sachets). Cette table suit donc les
            # quants bruts, sans les exclusions du reste du dashboard.
            # On retire l'exclusion des sachets (posée sur product_id par
            # _sachet_exclude_domain) : Odoo les compte.
            quant_domain_valorisation = [
                d for d in quant_domain_base
                if not (isinstance(d, (list, tuple)) and len(d) == 3
                        and d[0] == 'product_id' and d[1] == 'not in')
            ]
            if excluded_non_retail_ids:
                quant_domain_valorisation = quant_domain_valorisation + [
                    ('company_id', 'not in', excluded_non_retail_ids)
                ]

            # active_test=False : les articles archivés ont encore du stock et
            # Odoo les compte dans son écran.
            quant_val_grouped = self._group_sums(
                'stock.quant', quant_domain_valorisation, ['quantity'],
                group_fields=('product_id', 'company_id'), ctx={'active_test': False},
            )

            val_pids = [g['product_id'][0] for g in quant_val_grouped if g.get('product_id')]
            val_cost_estime = False
            val_qty_total = 0.0
            val_qty_avec_cout = 0.0
            if val_pids:
                val_prods = request.env['product.product'].sudo().with_context(active_test=False).search_read(
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
                # Même calcul que le pop-up de valorisation : un seul
                # endroit, sinon les deux écrans divergent (constaté :
                # 714 749,55 contre 357 243,25 pour SQUARE TARGA).
                tmpl_sans_cout = {
                    p.get('product_tmpl_id')[0] for p in val_prod_map.values()
                    if p.get('product_tmpl_id') and not p.get('standard_price')
                }
                prix_achat_moyen_by_tmpl = self._prix_achat_moyen_par_reference(
                    tmpl_sans_cout, kw)

                val_by_company = {}
                for g in quant_val_grouped:
                    pid = g['product_id'][0] if g.get('product_id') else None
                    # _group_sums ne renvoie pas les libellés (voir son
                    # docstring) : on résout les noms de sociétés à part,
                    # il n'y en a qu'une poignée.
                    cid = g['company_id'][0] if g.get('company_id') else None
                    qty = g.get('quantity') or 0.0
                    # Stocks négatifs gardés : Odoo les additionne aussi dans
                    # son écran d'inventaire (demande du 2026-09-23).
                    if not qty or not pid or pid not in val_prod_map:
                        continue
                    p_data = val_prod_map[pid]
                    price_ht = p_data.get('list_price') or 0.0
                    cost_price = p_data.get('standard_price') or 0.0
                    # A07 : on mesure la part des pièces dont le coût est
                    # VRAIMENT saisi, pour pouvoir dire sur quoi porte la
                    # « valeur au coût » au lieu d'afficher un total qui
                    # repose sur quatre articles.
                    val_qty_total += qty
                    if cost_price:
                        val_qty_avec_cout += qty
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
            #
            # CORRIGÉ le 2026-09-26 : l'alerte comparait le stock du RÉSEAU
            # à la vitesse de vente du RÉSEAU, puis collait à côté le nom
            # d'UN magasin choisi à part. Les deux ne parlaient donc pas du
            # même endroit : 98-73 s'affichait « Elite Carrefour Targa,
            # stock 5 » alors que les 5 pièces sont à Elite Auderby, et
            # EL1520 « Elite Menara Mall, stock 1 » alors que ce magasin est
            # à −1. Le calcul se fait maintenant magasin par magasin : une
            # ligne = une référence DANS un magasin, avec le stock de ce
            # magasin et ses ventes à lui.
            proches_rupture_30j = self._alerte_rupture_par_magasin(
                kw, relevant_tmpl_ids, tmpl_by_id, days_in_period)

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
                'ca_achat_depot': round(ca_achat_depot, 2),
                'vendu_avec_cout': round(vendu_avec_cout_total, 2),
                'marge': round(marge_total, 2),
                'tickets': tickets,
                'references_count': references_count,
                # Le compte porte sur les références AYANT UNE ACTIVITÉ
                # (vendue, achetée ou en stock). On renvoie aussi la taille
                # du catalogue : 1 215 sur 3 570 sur Elite, et l'écart
                # surprenait (remarque du 2026-09-25).
                'panier_moyen': round(panier_moyen, 2),
                'qty_sold': qty_sold_total,
                'qty_sold_normal': qty_sold_normal_total,
                'qty_sold_solde': qty_sold_solde_total,
                'soldes_count': len(soldes_products),
                'soldes_list': soldes_products[:500],
                'qty_purchased': qty_purchased_total,
                # stock_total = comptable (negatifs inclus), conserve pour
                # tous les calculs derives ; stock_present = ce qui est
                # reellement en rayon, c'est lui que la carte affiche.
                'stock_total': stock_total,
                'stock_present': stock_present,
                'stock_negatif': stock_negatif,
                'nb_magasins_negatifs': nb_magasins_negatifs,
                'valeur_stock_ht': round(val_ht_total, 2),
                'valeur_stock_cost': round(val_cost_total, 2),
                # A07 / A15 : part des pièces dont le coût est réellement
                # renseigné. En dessous, la valeur au coût et le GMROI ne
                # veulent pas dire grand-chose : l'écran le signale.
                'valeur_cost_couverture': (round(val_qty_avec_cout / val_qty_total * 100, 1)
                                           if val_qty_total else 0.0),
                'valeur_cost_disponible': bool(
                    val_qty_total and
                    val_qty_avec_cout / val_qty_total * 100 >= COUT_COUVERTURE_MIN),
                'valeur_stock_cost_estime': val_cost_estime,
                'stock_val_by_store': stock_val_by_store,
                'sell_through': sell_through,
                'ruptures_count': ruptures_count,
                'ruptures_list': ruptures_list,
                'top_products': [dict(p, rank=idx + 1) for idx, p in enumerate(top_products)],
                'flop_products': [dict(p, rank=idx + 1) for idx, p in enumerate(flop_products)],
                # A10 : les références en retour net, sorties du Flop.
                'retours_products': [dict(p, rank=idx + 1) for idx, p in enumerate(retours)],
                'abc_analysis': {
                    'A': abc['A'][:10],
                    'B': abc['B'][:10],
                    'C': abc['C'][:10],
                },
                'taux_rupture': taux_rupture,
                'ruptures_magasin_count': ruptures_magasin['count'],
                'ruptures_magasin_list': ruptures_magasin['lignes'],
                'ruptures_magasin_refs': ruptures_magasin['refs'],
                'total_active_skus': total_active_skus,
                'couverture_moy': couverture_moy,
                'stock_dormant_pct': stock_dormant_pct,
                'dormant_count': len(dormant_products),
                # A09 : références sans vente mais reçues depuis moins de
                # 90 jours — écartées du dormant, signalées à part.
                'dormant_recents_count': recents_count,
                'dormant_recents_stock': recents_stock,
                # Combien dorment parce qu'elles vendent trop lentement,
                # et non parce qu'elles ne vendent rien du tout.
                'dormant_lents_count': lents_count,
                'dormant_seuil_couverture': self.COUVERTURE_DORMANTE_JOURS,
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

    # ─────────────────────────────────────────────────────────────
    # MOD FOR LIFE — vue entrepôt / importateur
    #
    # DEMANDE UTILISATEUR (2026-09-17) : « la quantité achetée, c'est ce qui
    # est entré en stock et ça ne doit pas bouger ; elle vient des bons
    # d'achat validés ET réceptionnés sans retour, chez les fournisseurs qui
    # ont vendu à MOD FOR LIFE. Je dois avoir le nombre exact de ce qui est
    # en stock + de ce qui est vendu et dispatché, puis le dispatch par
    # société, et dans chaque société chaque magasin qui a reçu, avec les
    # références exactes, la couleur et la quantité. »
    #
    # Les deux sources sont celles de Base Pivot (écran Batch, boutons
    # « Bons d'achat » / « Bons de vente ») :
    #   • acheté = purchase.order de MOD FOR LIFE chez un fournisseur externe
    #              -> mv_article_batch.action_generate_purchase_orders
    #   • vendu  = sale.order de MOD FOR LIFE vers une société magasin,
    #              1 bon de vente PAR MAGASIN
    #              -> mv_article_batch.action_generate_sale_orders
    #
    # Le magasin destinataire n'est PAS stocké sur le bon de vente : il est
    # porté par le bon d'achat MIROIR créé dans la société cible par
    # sale_purchase_inter_company_rules (son `origin` contient le nom du bon
    # de vente, son `picking_type_id.warehouse_id` est l'entrepôt du
    # magasin). C'est exactement le « ça vient aussi dans les bons d'achat »
    # de la demande. Vérifié en base : 37 bons de vente sur 41 se résolvent
    # ainsi, contre 16/41 par le libellé magasin recopié dans `origin`.
    # ─────────────────────────────────────────────────────────────

    def _modforlife_shop_by_warehouse(self):
        """{entrepôt -> nom du magasin}.

        On affiche le NOM DE L'ENTREPÔT (« Elite Auderby »), pas le libellé
        technique de Base Pivot (« ELITE 04 ») : c'est celui que montre le
        sélecteur Magasin en haut de l'écran, et deux noms différents pour
        le même magasin d'un bloc à l'autre prêtaient à confusion
        (remarque utilisatrice du 2026-09-25).
        """
        labels = {}
        for m in self._get_active_shop_mappings():
            if m.warehouse_id:
                labels[m.warehouse_id.id] = m.warehouse_id.name or m.shop_label
        return labels

    def _modforlife_code_by_warehouse(self):
        """{entrepôt -> code Base Pivot, « ELITE 04 »}.

        Le nom de l'entrepôt reste le libellé affiché — c'est celui du
        sélecteur Magasin. Mais Odoo n'écrit que ce code-là dans le
        « Document d'origine » des bons de vente inter-sociétés : on le
        montre à côté du nom pour que la ligne du dashboard et le bon
        d'Odoo se rapprochent sans table de correspondance.
        """
        codes = {}
        for m in self._get_active_shop_mappings():
            if m.warehouse_id and m.shop_field:
                codes[m.warehouse_id.id] = m.shop_field.replace('_', ' ').upper()
        return codes

    def _modforlife_order_to_shop(self, mod_for_life, retail_companies, sale_orders):
        """{sale_order_id -> (warehouse_id, libellé magasin)}.

        Résolution par le bon d'achat miroir de la société cible : c'est lui
        qui porte l'entrepôt de réception, donc le magasin. Repli sur le
        libellé magasin recopié dans `origin` par
        action_generate_sale_orders, puis sur rien du tout — le bon de vente
        tombe alors dans une ligne « Magasin non identifié » de sa société,
        jamais silencieusement écarté du total.
        """
        result = {}
        if not sale_orders:
            return result

        shop_labels = self._modforlife_shop_by_warehouse()
        so_by_name = {so.name: so.id for so in sale_orders if so.name}

        if so_by_name:
            request.env.cr.execute("""
                SELECT po.origin, sw.id, sw.name
                  FROM purchase_order po
                  JOIN stock_picking_type spt ON spt.id = po.picking_type_id
                  JOIN stock_warehouse sw ON sw.id = spt.warehouse_id
                 WHERE po.partner_id = %(partner)s
                   AND po.company_id = ANY(%(companies)s)
                   AND po.origin IS NOT NULL
              ORDER BY po.id
            """, {
                'partner': mod_for_life.partner_id.id,
                'companies': retail_companies.ids,
            })
            # Le bon d'achat miroir ne pointe le magasin que si son type
            # d'opération est celui d'un entrepôt MAPPÉ. Sur Elite il pointe
            # l'entrepôt générique de la société (« SQUARE TARGA »), et le
            # dispatch affichait alors le nom de la société à la place du
            # magasin — un seul magasin visible sur sept. Dans ce cas on
            # laisse la main au repli, qui lit le libellé magasin écrit par
            # Base Pivot dans l'origine du bon de vente (« … — ELITE 05 »).
            entrepots_magasins = {
                m.warehouse_id.id for m in self._get_active_shop_mappings()
                if m.warehouse_id
            }
            for origin, wh_id, wh_name in request.env.cr.fetchall():
                if wh_id not in entrepots_magasins:
                    continue
                for so_name, so_id in so_by_name.items():
                    if so_id not in result and so_name in origin:
                        result[so_id] = (wh_id, shop_labels.get(wh_id) or wh_name)

        # Repli : « <batch> — <libellé magasin> » écrit dans l'origine du
        # bon de vente par Base Pivot.
        restants = [so for so in sale_orders if so.id not in result]
        if restants:
            # Mots distinctifs du nom de chaque entrepôt : « Elite Carrefour
            # Targa » -> CARREFOUR, TARGA. On écarte les mots partagés par
            # plusieurs magasins (ELITE), qui ne distinguent rien.
            by_mot = {}
            compte_mots = {}
            mags = [m for m in self._get_active_shop_mappings() if m.warehouse_id]
            for m in mags:
                for mot in (m.warehouse_id.name or '').upper().split():
                    if len(mot) >= 4:
                        compte_mots[mot] = compte_mots.get(mot, 0) + 1
            for m in mags:
                for mot in (m.warehouse_id.name or '').upper().split():
                    if len(mot) >= 4 and compte_mots.get(mot) == 1:
                        by_mot[mot] = (m.warehouse_id.id, m.warehouse_id.name)

            by_label = {}
            for m in self._get_active_shop_mappings():
                if m.shop_label and m.warehouse_id:
                    # On reconnaît le magasin par son libellé Base Pivot
                    # (« ELITE 04 », écrit dans l'origine du bon de vente),
                    # mais on l'affiche sous son nom d'entrepôt.
                    by_label[m.shop_label.strip().upper()] = (
                        m.warehouse_id.id, m.warehouse_id.name or m.shop_label)
            for so in restants:
                origin = (so.origin or '').strip().upper()
                if not origin:
                    continue
                # Le libellé est écrit en fin d'origine (« … — ELITE 05 »),
                # mais on accepte aussi qu'il soit suivi d'autre chose : on
                # prend le libellé le plus long qui correspond, pour ne pas
                # confondre « ELITE 0 » avec « ELITE 05 ».
                candidats = [
                    (label, val) for label, val in by_label.items()
                    if origin.endswith(label) or ('— ' + label) in origin
                    or (' ' + label) in origin
                ]
                if not candidats:
                    # Certains bons portent une origine libre, saisie à la
                    # main : « UNIFORMES SQUARE 11/09/2026 »,
                    # « VALISES-AUDERBY-2026-08-10 ». Le nom du magasin y est,
                    # mais pas sous la forme « — ELITE 02 ». On cherche donc
                    # les mots du nom de l'entrepôt (SQUARE, TARGA, AUDERBY…),
                    # en ignorant ceux communs à tous (ELITE).
                    candidats = [
                        (mot, val) for mot, val in by_mot.items() if mot in origin
                    ]
                if candidats:
                    label, val = max(candidats, key=lambda c: len(c[0]))
                    result[so.id] = val
        return result

    # Articles de TEST exclus de toute la vue MOD FOR LIFE (achats, stock,
    # dispatch, réassort) — DEMANDE UTILISATEUR 2026-09-21. Cas trouvé :
    # « TEST 2026 », créé le 20/02/2026 à 1,00 HT (1,20 TTC), archivé, avec
    # ses propres bons de test (achats P01471/P01539, ventes S00109 à
    # S00112 et S00144). Il faussait les cartes (296 pièces achetées, 295 en
    # stock) et produisait la ligne « Magasin non identifié » à 1,20 MAD.
    # Règle choisie par l'utilisatrice : le NOM contient « TEST » (plutôt
    # que « article archivé »). Exclu partout à la fois, pour que acheté −
    # dispatché = stock reste juste (mesuré : 21 320 − 13 254 = 8 066).
    def _mfl_sans_test_sql(self, pt_alias):
        # « %% » : la requête passe par psycopg2 avec des paramètres.
        return (" AND UPPER(COALESCE(" + pt_alias + ".name->>'fr_FR', "
                + pt_alias + ".name->>'en_US', '')) NOT LIKE '%%TEST%%'")

    def _mfl_sans_test_domain(self, name_path):
        return [(name_path, 'not ilike', 'test')]

    def _modforlife_purchase_sql(self, mod_for_life, retail_companies,
                                 product_tmpl_ids, date_start, date_end,
                                 stockables):
        """Filtres communs des lignes d'achat fournisseur de MOD FOR LIFE."""
        params = {
            'mfl': mod_for_life.id,
            'rp': retail_companies.mapped('partner_id').ids or [-1],
        }
        filters = " AND pt.type = 'product'" if stockables else " AND pt.type <> 'product'"
        filters += self._mfl_sans_test_sql('pt')
        if product_tmpl_ids is not None:
            filters += ' AND pp.product_tmpl_id = ANY(%(tmpls)s)'
            params['tmpls'] = list(product_tmpl_ids)
        sachet_variants = self._get_sachet_variant_ids()
        if sachet_variants:
            filters += ' AND NOT (pol.product_id = ANY(%(sachet)s))'
            params['sachet'] = list(sachet_variants)
        if date_start:
            filters += ' AND po.date_order >= %(ds)s'
            params['ds'] = date_start + ' 00:00:00'
        if date_end:
            filters += ' AND po.date_order <= %(de)s'
            params['de'] = date_end + ' 23:59:59'
        return filters, params

    # Quantité reçue CONVERTIE EN PIÈCES : `qty_received` est exprimé dans
    # l'unité du bon d'achat, pas dans celle de l'article. Cas trouvé en
    # base : P00001 (fournisseur ABC) achète « 1 Douzaine » — qty_received
    # = 1 alors que 12 pièces sont réellement entrées (mouvement MOD F/IN/
    # 00001 = 12). Sans conversion, la Qté achetée comptait 1 au lieu de 12.
    # Même règle que Odoo (uom._compute_quantity) : quantité ÷ facteur de
    # l'unité du bon × facteur de l'unité de l'article.
    _MFL_QTY_PIECES = (
        "SUM(pol.qty_received / NULLIF(pu.factor, 0) * tu.factor)"
    )

    def _modforlife_purchased_variants(self, mod_for_life, retail_companies,
                                       product_tmpl_ids=None, date_start=None,
                                       date_end=None):
        """LE PÉRIMÈTRE : les variantes réellement achetées par MOD FOR LIFE.

        RÈGLE UTILISATEUR (2026-09-17) : « ne fais pas ajouter au calcul ce
        qui n'est pas noté dans les bons ; tout doit avoir des bons, validés
        et aussi livrés ». Une première version ajoutait au compte une ligne
        « entré sans bon d'achat » pour équilibrer — refusée, et à juste
        titre : c'était une quantité inventée.

        Le périmètre est donc défini par les BONS D'ACHAT FOURNISSEUR, et
        eux seuls : un bon d'achat confirmé (`purchase`/`done`), passé par la
        société MOD FOR LIFE chez un **vrai fournisseur externe** — Tom&Eva,
        DIVERS, TOM & EVA, ABC dans cette base — et **réceptionné**
        (`qty_received > 0`, net des retours). Les bons d'achat portant MOD
        FOR LIFE comme fournisseur sont les miroirs inter-sociétés du sens
        inverse (MFL -> magasins) : ils sont exclus, ce n'est pas un achat.

        Seuls les articles STOCKABLES en font partie (vérification du
        2026-09-18 sur l'écart d'1 pièce restant) : pour un consommable ou un
        service, Odoo ne tient AUCUN stock — la réception est validée mais
        aucun stock.quant n'est créé. Le comparer au stock ne pourra jamais
        tomber juste. Ces articles ne sont pas cachés pour autant : voir
        `_modforlife_non_stockables`, affiché à part.

        Tout ce qui n'a pas de bon sort du compte — pas silencieusement,
        mais dans un bloc « hors compte » qui le nomme (voir
        `_modforlife_dispatch`). Mesuré en base : une seule référence
        concernée, 24P-6015, dispatchée 1 728 pièces vers les 16 magasins
        sans qu'aucun fournisseur ne l'ait jamais vendue à MOD FOR LIFE.

        Sans dates : périmètre sur TOUT l'historique (une référence achetée
        l'an dernier et dispatchée cette année reste une référence
        achetée). Avec dates : les quantités de la période, pour la carte.
        """
        filters, params = self._modforlife_purchase_sql(
            mod_for_life, retail_companies, product_tmpl_ids,
            date_start, date_end, stockables=True)
        request.env.cr.execute("""
            SELECT pol.product_id, {QTY}
              FROM purchase_order_line pol
              JOIN purchase_order po ON po.id = pol.order_id
              JOIN product_product pp ON pp.id = pol.product_id
              JOIN product_template pt ON pt.id = pp.product_tmpl_id
              JOIN uom_uom pu ON pu.id = pol.product_uom
              JOIN uom_uom tu ON tu.id = pt.uom_id
             WHERE po.state IN ('purchase', 'done')
               AND po.company_id = %(mfl)s
               AND NOT (po.partner_id = ANY(%(rp)s))
               {FILTERS}
             GROUP BY 1
            HAVING {QTY} > 0
        """.replace('{QTY}', self._MFL_QTY_PIECES).replace('{FILTERS}', filters), params)
        return {r[0]: float(r[1] or 0.0) for r in request.env.cr.fetchall()}

    def _modforlife_non_stockables(self, mod_for_life, retail_companies,
                                   product_tmpl_ids=None):
        """Achats fournisseur d'articles NON STOCKABLES (consommable, service).

        Ils ont bien un bon d'achat réceptionné, mais Odoo ne tient pas leur
        stock : ils ne peuvent pas entrer dans « acheté = stock + dispatché ».
        On les montre à part, nommés, avec le bon d'achat derrière — c'est
        souvent un article mal paramétré dans Odoo (type à passer en
        « Article stockable »). Cas de la base de test : 23-154 MD-A50530,
        12 pièces sur P00001, qui expliquait à lui seul l'écart d'1 pièce.
        """
        filters, params = self._modforlife_purchase_sql(
            mod_for_life, retail_companies, product_tmpl_ids,
            None, None, stockables=False)
        request.env.cr.execute("""
            SELECT pt.id,
                   COALESCE(NULLIF(pt.base_pivot_reference, ''),
                            NULLIF(pt.default_code, ''),
                            NULLIF(pp.default_code, ''),
                            pt.name->>'en_US') AS ref,
                   COALESCE(pt.name->>'fr_FR', pt.name->>'en_US') AS produit,
                   pt.type,
                   {QTY},
                   STRING_AGG(DISTINCT po.name, ', ')
              FROM purchase_order_line pol
              JOIN purchase_order po ON po.id = pol.order_id
              JOIN product_product pp ON pp.id = pol.product_id
              JOIN product_template pt ON pt.id = pp.product_tmpl_id
              JOIN uom_uom pu ON pu.id = pol.product_uom
              JOIN uom_uom tu ON tu.id = pt.uom_id
             WHERE po.state IN ('purchase', 'done')
               AND po.company_id = %(mfl)s
               AND NOT (po.partner_id = ANY(%(rp)s))
               {FILTERS}
             GROUP BY 1, 2, 3, 4
            HAVING {QTY} > 0
             ORDER BY 5 DESC
        """.replace('{QTY}', self._MFL_QTY_PIECES).replace('{FILTERS}', filters), params)
        types = {'consu': 'Consommable', 'service': 'Service'}
        return [{
            'article_id': r[0],
            'reference': r[1] or '—',
            'produit': r[2] or '—',
            'type': types.get(r[3], r[3]),
            'qty': int(round(r[4] or 0)),
            'bons': r[5] or '',
        } for r in request.env.cr.fetchall()]

    def _modforlife_dispatch(self, kw, mod_for_life, retail_companies,
                             product_tmpl_ids, scope_variant_ids):
        """Arbre société -> magasin -> référence/couleur du dispatch MFL.

        Quantité = `qty_delivered` (réellement sortie de l'entrepôt, nette
        des retours), jamais `product_uom_qty` : mesuré en base, 15 901
        pièces commandées pour 14 983 livrées, dont 686 revenues par bon de
        retour — comptées comme dispatchées, elles créaient de la sortie
        fantôme et donc de l'écart de stock.

        Les lignes dont la variante n'a AUCUN bon d'achat fournisseur
        (`scope_variant_ids`) ne rentrent pas dans l'arbre ni dans les
        totaux : elles partent dans `hors_perimetre`, affiché à part.
        """
        retail_partner_ids = retail_companies.mapped('partner_id').ids
        so_domain = [
            ('state', 'in', ['sale', 'done']),
            ('company_id', '=', mod_for_life.id),
            ('partner_id', 'in', retail_partner_ids),
        ]
        if kw.get('date_start'):
            so_domain.append(('date_order', '>=', kw['date_start'] + ' 00:00:00'))
        if kw.get('date_end'):
            so_domain.append(('date_order', '<=', kw['date_end'] + ' 23:59:59'))
        sale_orders = request.env['sale.order'].sudo().search(so_domain)
        so_names = {so.id: so.name for so in sale_orders}

        vide = {
            'par_societe': [], 'qty_total': 0, 'ca_total': 0.0,
            'nb_magasins': 0, 'nb_bons_vente': 0, 'qty_non_identifiee': 0,
            'hors_perimetre': [], 'hors_perimetre_qty': 0,
            'hors_perimetre_nb_refs': 0, 'hors_perimetre_nb_bons': 0,
        }
        if not sale_orders:
            return vide

        shop_by_order = self._modforlife_order_to_shop(
            mod_for_life, retail_companies, sale_orders)

        # Tous les bons rattaches a un magasin, et ceux qui n'ont rien
        # livre : sert a expliquer, sous la ligne du magasin, pourquoi
        # Odoo compte parfois plus de bons que le tableau.
        bons_du_magasin = {}
        for so_id, (wh_id_, _lab) in shop_by_order.items():
            bons_du_magasin.setdefault(wh_id_, set()).add(so_id)
        request.env.cr.execute("""
            SELECT so.id, COALESCE(SUM(sol.qty_delivered), 0)
              FROM sale_order so
              LEFT JOIN sale_order_line sol ON sol.order_id = so.id
             WHERE so.id = ANY(%s)
             GROUP BY so.id
        """, (sale_orders.ids,))
        bons_sans_livraison = {i for i, q in request.env.cr.fetchall()
                               if not float(q or 0.0)}
        # Bons saisis sans aucun prix : ce sont eux qui remplissent la
        # colonne « Montant TTC » de zeros.
        request.env.cr.execute("""
            SELECT so.id FROM sale_order so
             WHERE so.id = ANY(%s)
               AND NOT EXISTS (SELECT 1 FROM sale_order_line l
                                WHERE l.order_id = so.id AND l.price_unit > 0)
        """, (sale_orders.ids,))
        bons_sans_prix = {i for (i,) in request.env.cr.fetchall()}

        params = {'orders': sale_orders.ids}
        filters = self._mfl_sans_test_sql('pt')
        if product_tmpl_ids is not None:
            filters += ' AND pt.id = ANY(%(tmpls)s)'
            params['tmpls'] = list(product_tmpl_ids)
        sachet_variants = self._get_sachet_variant_ids()
        if sachet_variants:
            filters += ' AND NOT (sol.product_id = ANY(%(sachet)s))'
            params['sachet'] = list(sachet_variants)

        # Couleur et taille viennent des valeurs d'attribut de la variante —
        # c'est le grain exact du bon de vente, qui porte une ligne par
        # variante (couleur x taille). Les noms d'attributs de cette base ne
        # sont pas normalisés (« COULEURS », « COULEURSS », « Couleur »,
        # « POINTURES », « POINTURE », « TAILLES ») : on matche donc sur un
        # préfixe, pas sur une égalité.
        request.env.cr.execute("""
            WITH attr AS (
                SELECT pvc.product_product_id AS pid,
                       MAX(CASE WHEN UPPER(pa.name->>'en_US') LIKE 'COULEUR%%'
                                THEN pav.name->>'en_US' END) AS couleur,
                       MAX(CASE WHEN UPPER(pa.name->>'en_US') LIKE 'POINTURE%%'
                                  OR UPPER(pa.name->>'en_US') LIKE 'TAILLE%%'
                                THEN pav.name->>'en_US' END) AS taille
                  FROM product_variant_combination pvc
                  JOIN product_template_attribute_value ptav
                    ON ptav.id = pvc.product_template_attribute_value_id
                  JOIN product_attribute pa ON pa.id = ptav.attribute_id
                  JOIN product_attribute_value pav
                    ON pav.id = ptav.product_attribute_value_id
                 GROUP BY 1
            )
            SELECT sol.order_id,
                   so.partner_id,
                   pt.id,
                   COALESCE(NULLIF(pt.base_pivot_reference, ''),
                            NULLIF(pt.default_code, ''),
                            NULLIF(pp.default_code, ''),
                            pt.name->>'en_US') AS ref,
                   COALESCE(pt.name->>'fr_FR', pt.name->>'en_US') AS produit,
                   COALESCE(attr.couleur, '') AS couleur,
                   COALESCE(attr.taille, '') AS taille,
                   COALESCE(SUM(sol.qty_delivered / NULLIF(su.factor, 0) * tu.factor), 0),
                   COALESCE(SUM(sol.price_total), 0),
                   pp.id
              FROM sale_order_line sol
              JOIN sale_order so ON so.id = sol.order_id
              JOIN product_product pp ON pp.id = sol.product_id
              JOIN product_template pt ON pt.id = pp.product_tmpl_id
              JOIN uom_uom su ON su.id = sol.product_uom
              JOIN uom_uom tu ON tu.id = pt.uom_id
              LEFT JOIN attr ON attr.pid = pp.id
             WHERE sol.order_id = ANY(%(orders)s)
               {FILTERS}
             GROUP BY 1, 2, 3, 4, 5, 6, 7, 10
        """.replace('{FILTERS}', filters), params)
        rows = request.env.cr.fetchall()
        # Bons de vente comptés = ceux qui portent au moins un article hors
        # test : les lignes renvoyées ci-dessus excluent déjà les articles
        # « TEST ». Sans ça, les 5 bons de test (S00109 à S00112, S00144)
        # restaient comptés alors que leurs articles ne l'étaient plus.
        nb_bons_vente = len({r[0] for r in rows})

        partner_to_company = {
            c.partner_id.id: c for c in retail_companies if c.partner_id
        }
        scope = set(scope_variant_ids or ())

        societes = {}
        hors = {}
        hors_orders = set()
        qty_total = ca_total = qty_non_identifiee = hors_qty = 0.0
        for (so_id, partner_id, tmpl_id, ref, produit, couleur, taille,
             qty, ca, variant_id) in rows:
            qty = float(qty or 0.0)
            ca = float(ca or 0.0)
            if qty <= 0:
                continue
            company = partner_to_company.get(partner_id)
            wh_id, shop_label = shop_by_order.get(
                so_id, (0, 'Magasin non identifié'))
            couleur = (couleur or '').strip() or '—'
            soc_nom = company.name if company else '—'

            # Pas de bon d'achat fournisseur derrière cette variante : hors
            # compte. Jamais fondu dans les totaux, jamais caché non plus.
            if variant_id not in scope:
                hors_qty += qty
                hors_orders.add(so_id)
                k = (tmpl_id, soc_nom, shop_label)
                h = hors.setdefault(k, {
                    'article_id': tmpl_id,
                    'reference': ref or '—',
                    'produit': produit or '—',
                    'societe': soc_nom,
                    'magasin': shop_label,
                    'qty': 0.0,
                })
                h['qty'] += qty
                continue

            soc_key = company.id if company else partner_id
            soc = societes.setdefault(soc_key, {
                'societe': soc_nom, 'qty': 0.0, 'ca': 0.0, 'magasins': {},
            })
            if not wh_id:
                qty_non_identifiee += qty
            mag = soc['magasins'].setdefault(wh_id, {
                'magasin': shop_label, 'warehouse_id': wh_id,
                'qty': 0.0, 'ca': 0.0, 'refs': {}, 'bons': set(),
            })
            mag['bons'].add(so_names.get(so_id))
            ligne = mag['refs'].setdefault((tmpl_id, couleur), {
                'article_id': tmpl_id,
                'reference': ref or '—',
                'produit': produit or '—',
                'couleur': couleur,
                'qty': 0.0, 'ca': 0.0, 'tailles': {},
            })
            ligne['qty'] += qty
            ligne.setdefault('pids', set()).add(variant_id)
            ligne['ca'] += ca
            if taille:
                ligne['tailles'][taille] = ligne['tailles'].get(taille, 0.0) + qty
            mag['qty'] += qty
            mag['ca'] += ca
            soc['qty'] += qty
            soc['ca'] += ca
            qty_total += qty
            ca_total += ca

        def _tailles_txt(tailles):
            if not tailles:
                return ''
            try:
                ordered = sorted(tailles.items(),
                                 key=lambda kv: (float(kv[0]), kv[0]))
            except (TypeError, ValueError):
                ordered = sorted(tailles.items())
            return ' · '.join('%s : %d' % (t, int(round(q)))
                              for t, q in ordered)

        # Qté vendue en caisse par le magasin (demande utilisatrice
        # 2026-09-22), mêmes variantes que la ligne, sur la période choisie.
        variantes_vues = {pid for s in societes.values()
                          for m in s['magasins'].values()
                          for l in m['refs'].values() for pid in l.get('pids', ())}
        ventes = self._ventes_caisse_par_magasin(kw, variantes_vues)

        # Ce qu'il reste de chaque variante DANS L'ENTREPOT DU DEPOT, pour
        # que la ligne dise aussi si le magasin peut encore etre servi.
        stock_depot = {}
        if variantes_vues:
            request.env.cr.execute("""
                SELECT q.product_id, SUM(q.quantity)
                  FROM stock_quant q
                  JOIN stock_location l ON l.id = q.location_id
                 WHERE l.usage = 'internal' AND l.company_id = %s
                   AND q.product_id = ANY(%s)
                 GROUP BY q.product_id
            """, (mod_for_life.id, list(variantes_vues)))
            stock_depot = {pid: float(q or 0.0)
                           for pid, q in request.env.cr.fetchall()}

        codes_magasin = self._modforlife_code_by_warehouse()
        par_societe = []
        nb_magasins = 0
        for soc in societes.values():
            magasins = []
            for mag in soc['magasins'].values():
                refs = []
                for ligne in mag['refs'].values():
                    refs.append({
                        'article_id': ligne['article_id'],
                        'reference': ligne['reference'],
                        'produit': ligne['produit'],
                        'couleur': ligne['couleur'],
                        'tailles': _tailles_txt(ligne['tailles']),
                        'qty': int(round(ligne['qty'])),
                        'ca': round(ligne['ca'], 2),
                        'depot': int(round(sum(stock_depot.get(pid, 0.0)
                                               for pid in ligne.get('pids', ())))),
                        'vendu': int(round(sum(ventes.get((mag['warehouse_id'], pid), 0.0)
                                               for pid in ligne.get('pids', ())))),
                    })
                refs.sort(key=lambda r: (-r['qty'], r['reference']))
                # Bons rattaches au magasin mais dont rien n'est retenu :
                # soit ils n'ont rien livre, soit ils ne portent que de la
                # marchandise sans bon d'achat fournisseur derriere.
                tous = bons_du_magasin.get(mag['warehouse_id'], set())
                retenus = {i for i in tous if so_names.get(i) in mag['bons']}
                ecartes = tous - retenus
                magasins.append({
                    'magasin': mag['magasin'],
                    'code': codes_magasin.get(mag['warehouse_id'], ''),
                    'bons_ecartes': len(ecartes),
                    'bons_sans_livraison': len(ecartes & bons_sans_livraison),
                    # Combien de bons retenus n'ont aucun prix, et combien de
                    # pieces ils emportent : sans ce compte, une colonne de
                    # zeros passe pour une panne du tableau de bord.
                    'bons_sans_prix': len(retenus & bons_sans_prix),
                    'qty_sans_prix': int(round(sum(
                        l['qty'] for l in mag['refs'].values() if not l['ca']))),
                    'warehouse_id': mag['warehouse_id'],
                    'qty': int(round(mag['qty'])),
                    'ca': round(mag['ca'], 2),
                    'vendu': sum(r['vendu'] for r in refs),
                    'nb_references': len({r['reference'] for r in refs}),
                    # Les bons de vente derrière la ligne : indispensable sur
                    # « Magasin non identifié », où la seule façon de savoir
                    # ce qui est parti est d'ouvrir le bon dans Odoo.
                    'bons': sorted(b for b in mag['bons'] if b)[:60],
                    'nb_bons': len(mag['bons']),
                    'references': refs,
                })
            magasins.sort(key=lambda m: -m['qty'])
            nb_magasins += len([m for m in magasins if m['warehouse_id']])
            par_societe.append({
                'societe': soc['societe'],
                'qty': int(round(soc['qty'])),
                'ca': round(soc['ca'], 2),
                'nb_magasins': len(magasins),
                'magasins': magasins,
            })
        par_societe.sort(key=lambda s: -s['qty'])

        hors_rows = sorted(hors.values(), key=lambda r: -r['qty'])
        for r in hors_rows:
            r['qty'] = int(round(r['qty']))

        return {
            'par_societe': par_societe,
            'qty_total': int(round(qty_total)),
            'ca_total': round(ca_total, 2),
            'nb_magasins': nb_magasins,
            'nb_bons_vente': nb_bons_vente,
            'qty_non_identifiee': int(round(qty_non_identifiee)),
            'hors_perimetre': hors_rows[:200],
            'hors_perimetre_qty': int(round(hors_qty)),
            'hors_perimetre_nb_refs': len({r['reference'] for r in hors_rows}),
            'hors_perimetre_nb_bons': len(hors_orders),
        }

    def _ventes_caisse_par_magasin(self, kw, product_ids):
        """{(warehouse_id, product_id): qté vendue en caisse}, nette des
        retours, sur la période des filtres. Le magasin est celui de la
        caisse (type d'opération du point de vente). Sert à la colonne
        « Qté vendue » du dispatch MOD FOR LIFE (demande 2026-09-22)."""
        product_ids = [p for p in (product_ids or ()) if p]
        if not product_ids:
            return {}
        params = {'pids': product_ids}
        dates = ''
        if kw.get('date_start'):
            dates += ' AND po.date_order >= %(ds)s'
            params['ds'] = kw['date_start'] + ' 00:00:00'
        if kw.get('date_end'):
            dates += ' AND po.date_order <= %(de)s'
            params['de'] = kw['date_end'] + ' 23:59:59'
        request.env.cr.execute("""
            SELECT spt.warehouse_id, pol.product_id, SUM(pol.qty)
              FROM pos_order_line pol
              JOIN pos_order po ON po.id = pol.order_id
              JOIN pos_session ps ON ps.id = po.session_id
              JOIN pos_config pc ON pc.id = ps.config_id
              JOIN stock_picking_type spt ON spt.id = pc.picking_type_id
             WHERE po.state IN ('paid', 'done', 'invoiced')
               AND pol.product_id = ANY(%(pids)s)
               {DATES}
             GROUP BY 1, 2
        """.replace('{DATES}', dates), params)
        return {(wh, pid): float(q or 0.0) for wh, pid, q in request.env.cr.fetchall()}

    def _modforlife_achats_directs(self, kw, mod_for_life, retail_companies,
                                   product_tmpl_ids, company_id=None, warehouse_id=None):
        """Articles que les MAGASINS ont achetés chez MOD FOR LIFE sans bon
        de vente MFL derrière, surtout les chaussures.

        DEMANDE UTILISATEUR (2026-09-21) : « il n'y a que des sacs dans le
        dispatch, pas de chaussures, ce n'est pas logique ». Vérifié en base :
        MOD FOR LIFE n'a jamais vendu de chaussures dans Odoo (ses 41 bons
        de vente = sacs, chaussettes, culottes). Les chaussures arrivent par
        des bons d'achat saisis À LA MAIN par les magasins, fournisseur
        « MOD FOR LIFE » (879 bons, 270 591 pièces, 02/2025 à 05/2026), sans
        bon de vente MFL ni achat fournisseur MFL.

        Solution choisie (1) : les montrer dans l'arbre du dispatch, marquées
        « achat magasin », mais HORS des totaux et du compte acheté moins
        dispatché = stock (MOD FOR LIFE ne les a jamais eues en stock dans
        Odoo : les ajouter casserait un compte qui tombe juste).

        Un bon d'achat magasin né d'un bon de vente MFL porte ce bon dans son
        origine (flux inter-sociétés) : ceux-là sont déjà dans le dispatch et
        sont écartés ici. Quantité = réceptionnée par le magasin, en unité de
        l'article ; montant = total TTC du bon d'achat au prorata du reçu.
        Sachets et articles « TEST » exclus, comme dans le reste de la vue.
        """
        params = {
            'partner': mod_for_life.partner_id.id,
            'mfl': mod_for_life.id,
            'companies': retail_companies.ids or [-1],
        }
        filters = self._mfl_sans_test_sql('pt')
        if product_tmpl_ids is not None:
            filters += ' AND pt.id = ANY(%(tmpls)s)'
            params['tmpls'] = list(product_tmpl_ids)
        sachet_variants = self._get_sachet_variant_ids()
        if sachet_variants:
            filters += ' AND NOT (pol.product_id = ANY(%(sachet)s))'
            params['sachet'] = list(sachet_variants)
        if kw.get('date_start'):
            filters += ' AND po.date_order >= %(ds)s'
            params['ds'] = kw['date_start'] + ' 00:00:00'
        if kw.get('date_end'):
            filters += ' AND po.date_order <= %(de)s'
            params['de'] = kw['date_end'] + ' 23:59:59'
        # Chargement d'un seul magasin (dépliage dans l'arbre).
        if company_id:
            filters += ' AND po.company_id = %(cid)s'
            params['cid'] = int(company_id)
        if warehouse_id is not None:
            filters += ' AND COALESCE(spt.warehouse_id, 0) = %(wid)s'
            params['wid'] = int(warehouse_id)

        request.env.cr.execute("""
            WITH attr AS (
                SELECT pvc.product_product_id AS pid,
                       MAX(CASE WHEN UPPER(pa.name->>'en_US') LIKE 'COULEUR%%'
                                THEN pav.name->>'en_US' END) AS couleur,
                       MAX(CASE WHEN UPPER(pa.name->>'en_US') LIKE 'POINTURE%%'
                                  OR UPPER(pa.name->>'en_US') LIKE 'TAILLE%%'
                                THEN pav.name->>'en_US' END) AS taille
                  FROM product_variant_combination pvc
                  JOIN product_template_attribute_value ptav
                    ON ptav.id = pvc.product_template_attribute_value_id
                  JOIN product_attribute pa ON pa.id = ptav.attribute_id
                  JOIN product_attribute_value pav
                    ON pav.id = ptav.product_attribute_value_id
                 GROUP BY 1
            )
            SELECT po.company_id,
                   COALESCE(spt.warehouse_id, 0),
                   po.name,
                   pt.id,
                   COALESCE(NULLIF(pt.base_pivot_reference, ''),
                            NULLIF(pt.default_code, ''),
                            NULLIF(pp.default_code, ''),
                            pt.name->>'en_US') AS ref,
                   COALESCE(pt.name->>'fr_FR', pt.name->>'en_US') AS produit,
                   COALESCE(attr.couleur, '') AS couleur,
                   COALESCE(attr.taille, '') AS taille,
                   COALESCE(SUM(pol.qty_received / NULLIF(su.factor, 0) * tu.factor), 0),
                   COALESCE(SUM(pol.price_total
                                * pol.qty_received / NULLIF(pol.product_qty, 0)), 0),
                   pp.id
              FROM purchase_order_line pol
              JOIN purchase_order po ON po.id = pol.order_id
              LEFT JOIN stock_picking_type spt ON spt.id = po.picking_type_id
              JOIN product_product pp ON pp.id = pol.product_id
              JOIN product_template pt ON pt.id = pp.product_tmpl_id
              JOIN uom_uom su ON su.id = pol.product_uom
              JOIN uom_uom tu ON tu.id = pt.uom_id
              LEFT JOIN attr ON attr.pid = pp.id
             WHERE po.partner_id = %(partner)s
               AND po.company_id = ANY(%(companies)s)
               AND po.state IN ('purchase', 'done')
               AND NOT EXISTS (
                     SELECT 1 FROM sale_order so
                      WHERE so.company_id = %(mfl)s
                        AND po.origin LIKE '%%' || so.name || '%%')
               {FILTERS}
             GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 11
        """.replace('{FILTERS}', filters), params)
        return request.env.cr.fetchall()

    @http.route('/mavie/api/mfl-stock-entrepot', type='json', auth='user',
                methods=['POST'], csrf=False)
    def api_mfl_stock_entrepot(self, **kw):
        """Ce qui reste dans l'entrepôt du dépôt, ligne par ligne.

        La carte affiche deux totaux : le stock des références que le dépôt
        a lui-même achetées (celui qui équilibre « acheté − dispatché ») et
        le stock réel de l'entrepôt, retours de magasins compris. Le
        détail nomme chaque ligne et dit à laquelle des deux elle compte,
        sinon l'écart entre les deux chiffres reste inexplicable.
        """
        try:
            depot = self._societe_depot()
            if not depot:
                return {'error': "Société dépôt introuvable."}
            request.env.cr.execute("""
                SELECT DISTINCT pol.product_id
                  FROM purchase_order_line pol
                  JOIN purchase_order po ON po.id = pol.order_id
                 WHERE po.company_id = %s AND po.state IN ('purchase', 'done')
            """, (depot.id,))
            achetees = {r[0] for r in request.env.cr.fetchall()}

            request.env.cr.execute("""
                SELECT pp.id,
                       COALESCE(pt.default_code, pt.name->>'fr_FR',
                                pt.name->>'en_US') AS reference,
                       COALESCE(pt.name->>'fr_FR', pt.name->>'en_US') AS produit,
                       pt.active,
                       l.complete_name,
                       SUM(q.quantity) AS qte
                  FROM stock_quant q
                  JOIN stock_location l ON l.id = q.location_id
                  JOIN product_product pp ON pp.id = q.product_id
                  JOIN product_template pt ON pt.id = pp.product_tmpl_id
                 WHERE l.usage = 'internal' AND l.company_id = %s AND pt.active
                 GROUP BY 1, 2, 3, 4, 5
                HAVING SUM(q.quantity) <> 0
                 ORDER BY SUM(q.quantity) DESC
            """, (depot.id,))
            brut = request.env.cr.fetchall()

            variantes = request.env['product.product'].sudo().with_context(
                active_test=False).browse([r[0] for r in brut])
            noms = {v.id: v for v in variantes}
            rows = []
            total = total_achetees = total_negatif = 0.0
            for pid, ref, produit, actif, emplacement, qte in brut:
                v = noms.get(pid)
                qte = float(qte or 0.0)
                total += qte
                if pid in achetees:
                    total_achetees += qte
                if qte < 0:
                    total_negatif += qte
                rows.append({
                    'product_id': pid,
                    'reference': ref or '—',
                    'produit': produit or '—',
                    'variante': v.display_name if v else (produit or '—'),
                    'couleur': ' / '.join(
                        v.product_template_attribute_value_ids.mapped('name')) if v else '',
                    'emplacement': emplacement or '—',
                    'qty': int(round(qte)),
                    'achetee': pid in achetees,
                    'archive': not actif,
                })
            # Articles archivés : hors de la liste, mais comptés dans la carte.
            # Seuls leurs totaux sont renvoyés, pour que la décomposition
            # tombe juste sans les afficher.
            request.env.cr.execute("""
                SELECT pp.id, SUM(q.quantity)
                  FROM stock_quant q
                  JOIN stock_location l ON l.id = q.location_id
                  JOIN product_product pp ON pp.id = q.product_id
                  JOIN product_template pt ON pt.id = pp.product_tmpl_id
                 WHERE l.usage = 'internal' AND l.company_id = %s AND NOT pt.active
                 GROUP BY pp.id
            """, (depot.id,))
            archives = request.env.cr.fetchall()
            archives_total = sum(float(q or 0.0) for _pid, q in archives)
            archives_achetees = sum(float(q or 0.0) for pid, q in archives if pid in achetees)
            return {
                'rows': rows,
                'societe': depot.name,
                'total_non_achetees_actifs': int(round(total - total_achetees)),
                'total_archives': int(round(archives_total)),
                'total_archives_achetees': int(round(archives_achetees)),
                'total': int(round(total)),
                'total_achetees': int(round(total_achetees)),
                'total_negatif': int(round(total_negatif)),
                'nb_lignes': len(rows),
                'nb_references': len({r['reference'] for r in rows}),
            }
        except Exception as e:  # noqa: BLE001
            _logger.error("Erreur api_mfl_stock_entrepot: %s", e, exc_info=True)
            return {'error': str(e)}

    @http.route('/mavie/api/mfl-achats-directs', type='json', auth='user', methods=['POST'], csrf=False)
    def api_mfl_achats_directs(self, **kw):
        """Lignes « achat magasin » d'UN magasin, chargées au dépliage dans
        l'arbre du dispatch MOD FOR LIFE (voir _modforlife_achats_directs).
        Mêmes filtres que la vue : dates, collection / batch / catégorie."""
        try:
            mod_for_life = self._societe_depot()
            if not mod_for_life:
                return {'error': "Société entrepôt introuvable : renseignez-la dans Paramètres généraux → Base Pivot (Société importatrice)."}
            retail_companies = request.env['res.company'].sudo().search([
                ('id', '!=', mod_for_life.id), ('name', 'not in', ['PAIE'])])
            product_tmpl_ids = None
            if self._filtre_produit_actif(kw):
                product_tmpl_ids = request.env['product.template'].sudo().with_context(
                    active_test=False).search(
                    self._build_product_domain(kw)).ids or [-1]
            rows = self._modforlife_achats_directs(
                kw, mod_for_life, retail_companies, product_tmpl_ids,
                company_id=kw.get('company_id'),
                warehouse_id=int(kw.get('warehouse_id') or 0))
            vide = {'par_societe': []}
            self._modforlife_fusion_directs(vide, rows, retail_companies, kw=kw)
            for soc in vide['par_societe']:
                for mag in soc['magasins']:
                    return {'references_directes': mag.get('references_directes', [])}
            return {'references_directes': []}
        except Exception as e:
            _logger.error(f"Erreur api_mfl_achats_directs: {str(e)}", exc_info=True)
            return {'error': str(e)}

    def _modforlife_fusion_directs(self, dispatch, rows, retail_companies,
                                   avec_lignes=True, kw=None):
        """Range les achats directs des magasins (voir
        _modforlife_achats_directs) dans l'arbre du dispatch, à part des
        pièces MFL : `references_directes`, `qty_direct`, `ca_direct`,
        `bons_directs` sur chaque magasin, `qty_direct` / `ca_direct` sur
        chaque société. Les champs `qty` / `ca` existants ne bougent pas."""
        shop_labels = self._modforlife_shop_by_warehouse()
        companies = {c.id: c for c in retail_companies}
        wh_names = {w.id: w.name for w in request.env['stock.warehouse'].sudo().browse(
            list({r[1] for r in rows if r[1]})).exists()}
        socs = {s['societe']: s for s in dispatch['par_societe']}
        acc = {}
        total_qty = total_ca = 0.0
        bons_total = set()
        for (company_id, wh_id, po_name, tmpl_id, ref, produit, couleur, taille,
             qty, ca, variant_id) in rows:
            qty = float(qty or 0.0)
            ca = float(ca or 0.0)
            if qty <= 0:
                continue
            company = companies.get(company_id)
            soc_nom = company.name if company else '—'
            soc = socs.get(soc_nom)
            if soc is None:
                soc = socs[soc_nom] = {'societe': soc_nom, 'qty': 0, 'ca': 0.0,
                                       'nb_magasins': 0, 'magasins': []}
                dispatch['par_societe'].append(soc)
            key = (soc_nom, wh_id)
            if key not in acc:
                mag = next((m for m in soc['magasins'] if m['warehouse_id'] == wh_id), None)
                if mag is None:
                    mag = {'magasin': shop_labels.get(wh_id) or wh_names.get(wh_id) or 'Magasin non identifié',
                           'warehouse_id': wh_id, 'qty': 0, 'ca': 0.0,
                           'nb_references': 0, 'bons': [], 'nb_bons': 0, 'references': []}
                    soc['magasins'].append(mag)
                acc[key] = {'mag': mag, 'soc': soc, 'refs': {}, 'bons': set(),
                            'qty': 0.0, 'ca': 0.0, 'company_id': company_id}
            a = acc[key]
            a['bons'].add(po_name)
            bons_total.add(po_name)
            couleur = (couleur or '').strip() or '—'
            ligne = a['refs'].setdefault((tmpl_id, couleur), {
                'article_id': tmpl_id, 'reference': ref or '—',
                'produit': produit or '—', 'couleur': couleur,
                'qty': 0.0, 'ca': 0.0, 'tailles': {},
            })
            ligne.setdefault('pids', set()).add(variant_id)
            ligne['qty'] += qty
            ligne['ca'] += ca
            if taille:
                ligne['tailles'][taille] = ligne['tailles'].get(taille, 0.0) + qty
            a['qty'] += qty
            a['ca'] += ca
            total_qty += qty
            total_ca += ca

        def _tailles_txt(tailles):
            try:
                ordered = sorted(tailles.items(), key=lambda kv: (float(kv[0]), kv[0]))
            except (TypeError, ValueError):
                ordered = sorted(tailles.items())
            return ' · '.join('%s : %d' % (t, int(round(q))) for t, q in ordered)

        # Qté vendue en caisse (lignes chargées seulement : inutile pour les
        # totaux d'en-tête).
        ventes = self._ventes_caisse_par_magasin(
            kw or {}, {pid for a in acc.values() for l in a['refs'].values()
                       for pid in l.get('pids', ())}) if avec_lignes else {}

        for a in acc.values():
            refs = [{
                'article_id': l['article_id'], 'reference': l['reference'],
                'produit': l['produit'], 'couleur': l['couleur'],
                'tailles': _tailles_txt(l['tailles']),
                'qty': int(round(l['qty'])), 'ca': round(l['ca'], 2),
                'vendu': int(round(sum(ventes.get((a['mag']['warehouse_id'], pid), 0.0)
                                       for pid in l.get('pids', ())))),
            } for l in a['refs'].values()]
            refs.sort(key=lambda r: (-r['qty'], r['reference']))
            mag = a['mag']
            mag['company_id_direct'] = a['company_id']
            if avec_lignes:
                mag['references_directes'] = refs
            else:
                # Mesuré : 33 982 lignes, 5,8 Mo de JSON à chaque ouverture
                # de la vue. Les lignes sont chargées au dépliage du magasin
                # (/mavie/api/mfl-achats-directs) ; on garde ici de quoi
                # faire marcher la recherche : références et couleurs.
                mag['references_directes'] = []
                mag['directes_a_charger'] = True
                mag['recherche_directe'] = ' '.join(sorted(
                    {r['reference'].lower() for r in refs}
                    | {r['couleur'].lower() for r in refs}))
            mag['qty_direct'] = int(round(a['qty']))
            mag['ca_direct'] = round(a['ca'], 2)
            mag['nb_references_directes'] = len({r['reference'] for r in refs})
            mag['bons_directs'] = sorted(a['bons'])[:20]
            mag['nb_bons_directs'] = len(a['bons'])
            soc = a['soc']
            soc['qty_direct'] = soc.get('qty_direct', 0) + mag['qty_direct']
            soc['ca_direct'] = round(soc.get('ca_direct', 0.0) + mag['ca_direct'], 2)

        for soc in dispatch['par_societe']:
            soc['nb_magasins'] = len(soc['magasins'])
            soc['magasins'].sort(key=lambda m: -(m['qty'] + m.get('qty_direct', 0)))
        dispatch['par_societe'].sort(key=lambda s: -(s['qty'] + s.get('qty_direct', 0)))
        dispatch['direct_qty_total'] = int(round(total_qty))
        dispatch['direct_ca_total'] = round(total_ca, 2)
        dispatch['direct_nb_bons'] = len(bons_total)
        return dispatch

    def _modforlife_balance(self, mod_for_life, achats_par_variante,
                            qty_dispatchee, stock_reel, retail_partner_ids):
        """Le compte, UNIQUEMENT sur documents : acheté − dispatché = stock.

        Aucune quantité n'est ajoutée pour faire tomber le compte : les trois
        chiffres viennent des bons (bons d'achat fournisseur réceptionnés,
        bons de vente livrés) et du stock réel de l'entrepôt. Ce qui reste
        est un ÉCART, affiché comme tel, avec les références derrière —
        jamais absorbé dans un total.

        L'écart est décomposé variante par variante pour qu'il soit
        actionnable : `acheté − dispatché − stock` par variante, puis les
        références triées par poids. Mesuré en base : une seule variante,
        MOUCASSIN MD-A50530, 1 pièce achetée sur bon et jamais retrouvée.
        """
        variant_ids = list(achats_par_variante.keys())
        if not variant_ids:
            return {
                'qty_achetee': 0, 'qty_dispatchee': int(round(qty_dispatchee)),
                'stock_reel': int(round(stock_reel)), 'stock_theorique': 0,
                'ecart': 0, 'refs_ecart': [], 'nb_refs_ecart': 0,
            }

        request.env.cr.execute("""
            WITH disp AS (
                -- Même règle que la carte « dispatché » : vers les sociétés
                -- magasins uniquement, quantité livrée convertie en pièces.
                SELECT sol.product_id AS pid,
                       SUM(sol.qty_delivered / NULLIF(su.factor, 0) * tu.factor) AS q
                  FROM sale_order_line sol
                  JOIN sale_order so ON so.id = sol.order_id
                  JOIN product_product dpp ON dpp.id = sol.product_id
                  JOIN product_template dpt ON dpt.id = dpp.product_tmpl_id
                  JOIN uom_uom su ON su.id = sol.product_uom
                  JOIN uom_uom tu ON tu.id = dpt.uom_id
                 WHERE so.state IN ('sale', 'done')
                   AND so.company_id = %(mfl)s
                   AND so.partner_id = ANY(%(rp)s)
                   AND sol.product_id = ANY(%(variants)s)
                 GROUP BY 1
            ), stk AS (
                SELECT sq.product_id AS pid, SUM(sq.quantity) AS q
                  FROM stock_quant sq
                  JOIN stock_location sl ON sl.id = sq.location_id
                 WHERE sl.usage = 'internal'
                   AND sl.company_id = %(mfl)s
                   AND sq.product_id = ANY(%(variants)s)
                 GROUP BY 1
            )
            SELECT pp.id, pp.product_tmpl_id,
                   COALESCE(NULLIF(pt.base_pivot_reference, ''),
                            NULLIF(pt.default_code, ''),
                            NULLIF(pp.default_code, ''),
                            pt.name->>'en_US') AS ref,
                   COALESCE(pt.name->>'fr_FR', pt.name->>'en_US') AS produit,
                   COALESCE(disp.q, 0), COALESCE(stk.q, 0)
              FROM product_product pp
              JOIN product_template pt ON pt.id = pp.product_tmpl_id
              LEFT JOIN disp ON disp.pid = pp.id
              LEFT JOIN stk ON stk.pid = pp.id
             WHERE pp.id = ANY(%(variants)s)
        """, {'mfl': mod_for_life.id, 'variants': variant_ids,
              'rp': list(retail_partner_ids) or [-1]})

        ecarts = {}
        for pid, tmpl_id, ref, produit, d, s in request.env.cr.fetchall():
            a = achats_par_variante.get(pid, 0.0)
            ecart = a - float(d or 0) - float(s or 0)
            if abs(ecart) < 0.001:
                continue
            e = ecarts.setdefault(tmpl_id, {
                'article_id': tmpl_id,
                'reference': ref or '—',
                'produit': produit or '—',
                'qty': 0.0,
                'nb_variantes': 0,
            })
            e['qty'] += ecart
            e['nb_variantes'] += 1

        refs_ecart = sorted(ecarts.values(), key=lambda r: -abs(r['qty']))
        qty_achetee = sum(achats_par_variante.values())
        stock_theorique = qty_achetee - qty_dispatchee
        return {
            'qty_achetee': int(round(qty_achetee)),
            'qty_dispatchee': int(round(qty_dispatchee)),
            'stock_theorique': int(round(stock_theorique)),
            'stock_reel': int(round(stock_reel)),
            'ecart': int(round(stock_theorique - stock_reel)),
            'refs_ecart': [{
                'article_id': r['article_id'],
                'reference': r['reference'],
                'produit': r['produit'],
                'qty': int(round(r['qty'])),
                'nb_variantes': r['nb_variantes'],
            } for r in refs_ecart[:50]],
            'nb_refs_ecart': len(refs_ecart),
        }

    def _compute_kpis_modforlife(self, kw, mod_for_life):
        """Calcul dédié pour MOD FOR LIFE : pas de vente en caisse ni
        d'alertes rupture retail (ce n'est pas un magasin), donc pas la même
        forme que _compute_kpis.

        TOUT part des BONS, et rien d'autre (règle utilisateur 2026-09-17) :
        bons d'achat fournisseur confirmés et réceptionnés d'un côté, bons de
        vente inter-sociétés confirmés et livrés de l'autre, stock réel de
        l'entrepôt au milieu. Aucune quantité n'est ajoutée pour faire
        tomber le compte ; ce qui ne rentre pas dans les bons est montré à
        part, nommé, et exclu des totaux.
        """
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
            if self._filtre_produit_actif(kw):
                product_tmpl_ids = request.env['product.template'].sudo().with_context(
                    active_test=False).search(
                    self._build_product_domain(kw)
                ).ids
                if not product_tmpl_ids:
                    product_tmpl_ids = [-1]

            # ── LE PÉRIMÈTRE : ce que de vrais fournisseurs (Tom&Eva,
            # DIVERS, ABC…) ont vendu à MOD FOR LIFE, sur bon d'achat
            # confirmé ET réceptionné. Rien d'autre ne compte.
            achats_par_variante = self._modforlife_purchased_variants(
                mod_for_life, retail_companies, product_tmpl_ids)
            scope_variant_ids = list(achats_par_variante.keys())
            qty_achats_total = int(round(sum(achats_par_variante.values())))

            po_domain = [
                ('order_id.state', 'in', ['purchase', 'done']),
                ('order_id.company_id', '=', mod_for_life.id),
                ('partner_id', 'not in', retail_partner_ids),
            ]
            po_domain += self._sachet_exclude_domain('product_id.product_tmpl_id.collection_id')
            po_domain += self._mfl_sans_test_domain('product_id.product_tmpl_id.name')
            if product_tmpl_ids is not None:
                po_domain.append(('product_id.product_tmpl_id', 'in', product_tmpl_ids))
            # Même règle que les cartes du tableau de bord (demande du
            # 2026-09-25) : les achats ne suivent pas le filtre Période. La
            # marchandise est achetée en une fois puis dispatchée sur des
            # mois ; borner les achats à la même fenêtre que les ventes
            # faisait tomber « Qté achetée » de 87 943 à 51 308 et les
            # « Achats fournisseurs » de 9 504 à 0.
            po_grouped = request.env['purchase.order.line'].sudo().read_group(
                po_domain, ['price_total:sum'], [], lazy=False
            )
            # Quantité RÉELLEMENT REÇUE (nette des retours fournisseur), EN
            # PIÈCES et sur les seuls articles STOCKABLES : exactement le même
            # calcul que le bilan plus bas (même fonction, bornée à la
            # période), pour que la carte et le bilan ne puissent jamais
            # diverger. Un bon d'achat confirmé mais jamais réceptionné, ou
            # reçu puis renvoyé, ne compte pas. C'est ce qui est entré en
            # stock, donc un chiffre qui ne bouge plus quand la marchandise
            # repart.
            # Idem : la quantité reçue porte sur tout l'historique.
            achats_periode = achats_par_variante
            qty_achats_fournisseurs = int(round(sum(achats_periode.values())))
            # Le montant, lui, reste celui de TOUS les bons d'achat
            # fournisseur : c'est de l'argent réellement engagé, y compris
            # sur un article non stockable.
            ca_achats_fournisseurs = round(po_grouped[0].get('price_total') or 0.0, 2) if po_grouped else 0.0

            po_tickets_agg = request.env['purchase.order.line'].sudo().read_group(
                po_domain, ['order_id:count_distinct'], []
            )
            nb_commandes_fournisseurs = (po_tickets_agg[0].get('order_id') or 0) if po_tickets_agg else 0

            nb_references_achetees = len(achats_periode)

            # Combien de bons portent reellement un prix ? Sans ce compte,
            # « 9 504,00 MAD pour 87 943 pieces » se lit comme un calcul
            # faux alors que c'est la saisie qui manque : verifie en base,
            # 271 des 272 bons d'achat du depot n'ont aucun prix unitaire.
            request.env.cr.execute("""
                SELECT COUNT(*) FROM purchase_order po
                 WHERE po.company_id = %s AND po.state IN ('purchase', 'done')
                   AND NOT EXISTS (SELECT 1 FROM purchase_order_line l
                                    WHERE l.order_id = po.id AND l.price_unit > 0)
            """, (mod_for_life.id,))
            achats_bons_sans_prix = request.env.cr.fetchone()[0] or 0
            request.env.cr.execute("""
                SELECT COUNT(*) FROM sale_order so
                 WHERE so.company_id = %s AND so.state IN ('sale', 'done')
                   AND NOT EXISTS (SELECT 1 FROM sale_order_line l
                                    WHERE l.order_id = so.id AND l.price_unit > 0)
            """, (mod_for_life.id,))
            ventes_bons_sans_prix = request.env.cr.fetchone()[0] or 0

            # Achats réceptionnés d'articles dont Odoo ne tient pas le stock :
            # hors du compte « acheté = stock + dispatché », affichés à part.
            non_stockables = self._modforlife_non_stockables(
                mod_for_life, retail_companies, product_tmpl_ids)

            # ── Dispatch : société -> magasin -> référence x couleur,
            # restreint au périmètre des références achetées sur bon.
            dispatch = self._modforlife_dispatch(
                kw, mod_for_life, retail_companies, product_tmpl_ids,
                scope_variant_ids)

            # ── Stock entrepôt, sur le même périmètre : un stock sans bon
            # d'achat derrière n'entre pas dans le compte non plus.
            # Vérifié en base : il n'y en a aucun aujourd'hui.
            quant_domain = [
                ('location_id.usage', '=', 'internal'),
                ('company_id', '=', mod_for_life.id),
                ('product_id', 'in', scope_variant_ids or [-1]),
            ]
            quant_grouped = request.env['stock.quant'].sudo().read_group(
                quant_domain, ['quantity:sum'], [], lazy=False
            )
            stock_entrepot = int(sum(g.get('quantity') or 0 for g in quant_grouped)) if quant_grouped else 0

            # Le chiffre ci-dessus ne porte que sur les variantes ACHETÉES
            # par le dépôt : c'est ce qu'il faut pour que le compte
            # « acheté − dispatché = stock » tombe juste. Mais l'entrepôt
            # contient aussi des pièces arrivées autrement (retours de
            # magasins). Vérifié sur Elite : 6 901 sur le périmètre acheté,
            # 6 448 en tout. On renvoie les deux plutôt que d'en afficher un
            # seul sous une étiquette qui promet l'autre.
            stock_entrepot_total = int(sum(
                g.get('quantity') or 0 for g in request.env['stock.quant'].sudo().read_group(
                    [('location_id.usage', '=', 'internal'),
                     ('company_id', '=', mod_for_life.id)],
                    ['quantity:sum'], [], lazy=False)) or 0)

            # ── Le compte : acheté − dispatché = stock. Comparé au stock
            # réel, et l'écart reste un écart (jamais absorbé).
            if kw.get('date_start') or kw.get('date_end'):
                kw_sans_dates = dict(kw)
                kw_sans_dates.pop('date_start', None)
                kw_sans_dates.pop('date_end', None)
                dispatch_total = self._modforlife_dispatch(
                    kw_sans_dates, mod_for_life, retail_companies,
                    product_tmpl_ids, scope_variant_ids)['qty_total']
            else:
                dispatch_total = dispatch['qty_total']

            balance = self._modforlife_balance(
                mod_for_life, achats_par_variante, dispatch_total, stock_entrepot,
                retail_partner_ids)

            # Achats directs des magasins chez MOD FOR LIFE (chaussures…),
            # ajoutés à l'arbre APRÈS le compte : ils n'y entrent pas.
            self._modforlife_fusion_directs(
                dispatch,
                self._modforlife_achats_directs(
                    kw, mod_for_life, retail_companies, product_tmpl_ids),
                retail_companies, avec_lignes=False)


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
                'qty_achats_total': qty_achats_total,
                'nb_commandes_fournisseurs': nb_commandes_fournisseurs,
                'achats_bons_sans_prix': achats_bons_sans_prix,
                'ventes_bons_sans_prix': ventes_bons_sans_prix,
                'nb_references_achetees': nb_references_achetees,
                'non_stockables': non_stockables,
                'ca_ventes_societes': dispatch['ca_total'],
                'qty_ventes_societes': dispatch['qty_total'],
                'dispatch_par_societe': dispatch['par_societe'],
                'dispatch_nb_magasins': dispatch['nb_magasins'],
                'dispatch_nb_bons_vente': dispatch['nb_bons_vente'],
                'direct_qty_total': dispatch.get('direct_qty_total', 0),
                'direct_ca_total': dispatch.get('direct_ca_total', 0.0),
                'direct_nb_bons': dispatch.get('direct_nb_bons', 0),
                'dispatch_qty_non_identifiee': dispatch['qty_non_identifiee'],
                'hors_perimetre': dispatch['hors_perimetre'],
                'hors_perimetre_qty': dispatch['hors_perimetre_qty'],
                'hors_perimetre_nb_refs': dispatch['hors_perimetre_nb_refs'],
                'hors_perimetre_nb_bons': dispatch['hors_perimetre_nb_bons'],
                'stock_entrepot': stock_entrepot,
                'stock_entrepot_total': stock_entrepot_total,
                'balance_mfl': balance,
                'periode_filtree': bool(kw.get('date_start') or kw.get('date_end')),
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
            is_filtered = bool(self._filtre_produit_actif(kw))

            product_tmpl_ids = None
            if is_filtered:
                domain = self._build_product_domain(kw)
                products = request.env['product.template'].sudo().with_context(
                    active_test=False).search(domain)
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
                # A12 (2026-09-24) : les articles qui ne sont rattachés à
                # aucun arrivage étaient simplement ignorés. Le graphique
                # ne montrait donc qu'un arrivage valant 3,5 % du CA, sans
                # que rien n'explique le reste. On les regroupe désormais
                # sous « Non rattaché » pour que le total du graphique
                # corresponde au CA de la période.
                arrivage_name = tmpl_to_arrivage.get(tid) or 'Non rattaché'
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
                # Même repli que partout ailleurs (page Action, Top/Flop) :
                # sans code interne, la référence affichée est le nom. Sur
                # Elite, 27 articles n'ont aucun code et sortaient « — ».
                'ref': p.base_pivot_reference or p.default_code or p.name or '—',
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

    # RECONCILIATION DU STOCK
    #
    # DEMANDE UTILISATEUR (2026-09-07) : "je ne dois pas avoir d'ecarts".
    # L'ancienne carte affichait "ecart = (achete - vendu) - stock reel",
    # une soustraction entre des DOCUMENTS (bons de commande, lignes de
    # caisse) et un STOCK PHYSIQUE. Elle ne pouvait donc jamais tomber a
    # zero : tout mouvement reel qui n'est ni un achat ni une vente (perte
    # d'inventaire, sortie sans ticket, transit) restait hors du calcul et
    # ressortait en "ecart" anonyme.
    #
    # On reconstruit ici le stock attendu a partir des MOUVEMENTS VALIDES
    # (stock.move.line) qui franchissent la frontiere du perimetre affiche,
    # chaque poste etant nomme. Verifie en base sur les 40 876 variantes
    # ayant des mouvements : 40 443 (98,9 %) tombent exactement a zero. Le
    # reliquat (433 variantes, ~2 790 pieces) correspond a des quants ecrits
    # sans mouvement : une vraie anomalie de donnees, et c'est justement ce
    # que la carte doit signaler au lieu d'alerter sur toutes les fiches.

    def _stock_ledger_buckets(self, variant_ids, inside_ids):
        """Mouvements valides franchissant la frontiere du perimetre.

        Renvoie {('in'|'out', usage_de_la_contrepartie): quantite}. Un
        mouvement interne au perimetre (magasin vers magasin du meme
        perimetre) ne franchit rien et est volontairement ignore : il ne
        change pas le stock total affiche.
        """
        if not variant_ids or not inside_ids:
            return {}
        request.env.cr.execute("""
            SELECT CASE WHEN sml.location_dest_id = ANY(%(inside)s) THEN 'in' ELSE 'out' END,
                   CASE WHEN sml.location_dest_id = ANY(%(inside)s) THEN src.usage ELSE dst.usage END,
                   COALESCE(SUM(sml.quantity), 0)
              FROM stock_move_line sml
              JOIN stock_location src ON src.id = sml.location_id
              JOIN stock_location dst ON dst.id = sml.location_dest_id
             WHERE sml.state = 'done'
               AND sml.product_id = ANY(%(variants)s)
               AND ((sml.location_dest_id = ANY(%(inside)s)
                     AND NOT (sml.location_id = ANY(%(inside)s)))
                 OR (sml.location_id = ANY(%(inside)s)
                     AND NOT (sml.location_dest_id = ANY(%(inside)s))))
             GROUP BY 1, 2
        """, {'inside': list(inside_ids), 'variants': list(variant_ids)})
        return {(r[0], r[1]): float(r[2] or 0.0) for r in request.env.cr.fetchall()}

    def _location_to_warehouse(self, lot_stock_ids):
        """{emplacement -> entrepot} pour tous les emplacements sous ces
        entrepots. Sert a juger la positivite du stock magasin par magasin
        sans refaire une requete par entrepot."""
        if not lot_stock_ids:
            return {}
        request.env.cr.execute("""
            SELECT l.id, w.id
              FROM stock_location wl
              JOIN stock_warehouse w ON w.lot_stock_id = wl.id
              JOIN stock_location l ON l.parent_path LIKE wl.parent_path || '%%'
             WHERE wl.id = ANY(%(lots)s)
        """, {'lots': list(lot_stock_ids)})
        return {row[0]: row[1] for row in request.env.cr.fetchall()}

    def _split_positive_stock(self, quant_rows, loc_to_wh):
        """(present, negatif, nb_magasins_negatifs) a partir de lignes
        groupees par (product_id, location_id).

        La positivite se juge au grain (variante x magasin) : c'est le grain
        du terrain. Une taille a +3 dans un magasin ne doit pas etre annulee
        par une autre taille a -3 ailleurs.
        """
        par_variante_magasin = {}
        for g in quant_rows:
            pid = g['product_id'][0] if g.get('product_id') else None
            lid = g['location_id'][0] if g.get('location_id') else None
            wh_id = loc_to_wh.get(lid)
            if not pid or not wh_id:
                continue
            key = (pid, wh_id)
            par_variante_magasin[key] = par_variante_magasin.get(key, 0.0) + (g.get('quantity') or 0.0)
        present = sum(q for q in par_variante_magasin.values() if q > 0)
        negatif = sum(q for q in par_variante_magasin.values() if q < 0)
        magasins = {wh for (_pid, wh), q in par_variante_magasin.items() if q < 0}
        return int(round(present)), int(round(negatif)), len(magasins)

    def _positive_stock(self, variant_ids, warehouse_ids):
        """Stock REELLEMENT PRESENT en rayon, negatifs isoles.

        CONTRAINTE UTILISATEUR (2026-09-07) : les stocks negatifs de cette
        base (59 650 pieces sur 2 234 references) ne seront PAS corriges dans
        Odoo — l'utilisateur ne peut modifier que le module dashboard, pas
        l'inventaire. Le dashboard doit donc afficher un stock exploitable
        malgre eux.

        Un stock negatif n'est pas du stock : on ne peut pas avoir -1 piece
        en rayon. C'est la trace d'une vente sur de la marchandise jamais
        entree dans ce magasin (transfert non enregistre, le plus souvent).
        On somme donc uniquement les positifs, en jugeant la positivite au
        niveau (variante x magasin) — le grain reel du terrain : une taille
        en +3 dans un magasin ne doit pas etre annulee par une autre taille
        a -3 ailleurs.

        Renvoie (positif_par_variante, total_present, total_negatif,
        nb_magasins_negatifs, net_par_variante). `total_present +
        total_negatif` redonne le total comptable Odoo, qui reste la
        reference de la reconciliation ; `net_par_variante` (negatifs
        inclus) sert a detecter les quantites ecrites sans mouvement, un
        controle qui doit rester compare a du comptable.
        """
        vide = ({}, 0.0, 0.0, 0, {})
        if not variant_ids or not warehouse_ids:
            return vide
        request.env.cr.execute("""
            SELECT sq.product_id, w.id, COALESCE(SUM(sq.quantity), 0)
              FROM stock_quant sq
              JOIN stock_location l ON l.id = sq.location_id
              JOIN stock_warehouse w ON w.id = ANY(%(wh)s)
              JOIN stock_location wl ON wl.id = w.lot_stock_id
             WHERE sq.product_id = ANY(%(variants)s)
               AND l.parent_path LIKE wl.parent_path || '%%'
             GROUP BY 1, 2
        """, {'wh': list(warehouse_ids), 'variants': list(variant_ids)})
        positif_par_variante = {}
        net_par_variante = {}
        total_present = total_negatif = 0.0
        magasins_negatifs = set()
        for pid, wh_id, qty in request.env.cr.fetchall():
            qty = float(qty or 0.0)
            net_par_variante[pid] = net_par_variante.get(pid, 0.0) + qty
            if qty > 0:
                positif_par_variante[pid] = positif_par_variante.get(pid, 0.0) + qty
                total_present += qty
            elif qty < 0:
                total_negatif += qty
                magasins_negatifs.add(wh_id)
        return (positif_par_variante, total_present, total_negatif,
                len(magasins_negatifs), net_par_variante)

    def _stock_ledger_by_variant(self, variant_ids, inside_ids):
        """Stock attendu par variante d'apres les mouvements valides.

        Meme principe que _stock_ledger_buckets, mais groupe par variante :
        sert au tableau "Variantes Couleurs" et au pop-up couleur, pour que
        la colonne "Reste" soit comparable au stock reel au lieu d'etre une
        estimation papier (achats commandes - ventes) qui ne pouvait jamais
        y correspondre.
        """
        if not variant_ids or not inside_ids:
            return {}
        request.env.cr.execute("""
            SELECT sml.product_id,
                   COALESCE(SUM(CASE WHEN sml.location_dest_id = ANY(%(inside)s)
                                     THEN sml.quantity ELSE -sml.quantity END), 0)
              FROM stock_move_line sml
             WHERE sml.state = 'done'
               AND sml.product_id = ANY(%(variants)s)
               AND ((sml.location_dest_id = ANY(%(inside)s)
                     AND NOT (sml.location_id = ANY(%(inside)s)))
                 OR (sml.location_id = ANY(%(inside)s)
                     AND NOT (sml.location_dest_id = ANY(%(inside)s))))
             GROUP BY 1
        """, {'inside': list(inside_ids), 'variants': list(variant_ids)})
        return {r[0]: float(r[1] or 0.0) for r in request.env.cr.fetchall()}

    def _reconciliation_scope(self, kw):
        """Entrepots + emplacements internes reellement additionnes dans la
        carte "Stock Reel Odoo" de la fiche produit.

        Reproduit a l'identique la regle de _compute_product_detail : magasin
        choisi dans la fiche s'il y en a un, sinon les societes cochees dans
        le selecteur Odoo, sinon tout le reseau des magasins actifs.
        """
        non_retail_company_ids = self._get_non_retail_company_ids()
        context_company_ids = self._get_context_company_ids()
        excluded = self._get_excluded_non_retail_ids(kw)
        explicit_non_retail = [c for c in non_retail_company_ids if c not in excluded]
        Warehouse = request.env['stock.warehouse'].sudo()
        warehouses = Warehouse.search([
            ('company_id', 'not in', non_retail_company_ids),
            ('id', 'in', self._get_active_shop_mappings().mapped('warehouse_id').ids),
        ])
        if explicit_non_retail:
            warehouses |= Warehouse.search([('company_id', 'in', explicit_non_retail)])
        if kw.get('shop_field'):
            scope = self._get_shop_scope(kw['shop_field'])
            if scope and scope['warehouse']:
                warehouses = scope['warehouse']
        elif context_company_ids:
            warehouses = warehouses.filtered(
                lambda w: w.company_id.id in context_company_ids
            )
        lot_stock_ids = warehouses.mapped('lot_stock_id').ids
        inside_ids = request.env['stock.location'].sudo().search([
            ('id', 'child_of', lot_stock_ids)
        ]).ids if lot_stock_ids else []
        return warehouses, inside_ids

    def _stock_reconciliation(self, variant_ids, inside_ids, qty_purchased,
                              qty_sold, stock_reel):
        """Decompose "stock attendu vs stock reel" en postes nommes.

        `qty_purchased` / `qty_sold` viennent des DOCUMENTS (bons de commande
        recus, lignes de caisse + bons de vente livres) : ce sont les deux
        cartes du haut de fiche, donc la reconciliation part d'elles pour
        rester lisible. Tout ce que les mouvements montrent en plus est isole
        sur sa propre ligne, jamais fondu dans un "ecart".
        """
        buckets = self._stock_ledger_buckets(variant_ids, inside_ids)

        def q(sens, usage):
            return buckets.get((sens, usage), 0.0)

        # BLOC 1 — le stock expliqué par les SEULS mouvements. Chaque ligne
        # est une somme brute d'un type de mouvement, jamais un reste
        # calculé : c'est ce qui garantit que le total retombe toujours sur
        # le stock réel quand la traçabilité est complète.
        autres_in = autres_out = 0.0
        for (sens, usage), qty in buckets.items():
            if usage in ('supplier', 'customer', 'inventory'):
                continue
            if sens == 'in':
                autres_in += qty
            else:
                autres_out += qty

        stock_attendu = (
            q('in', 'supplier') - q('out', 'supplier')
            + q('in', 'customer') - q('out', 'customer')
            + q('in', 'inventory') - q('out', 'inventory')
            + autres_in - autres_out
        )

        # BLOC 2 — pourquoi les cartes du haut (qui viennent des DOCUMENTS)
        # ne donnent pas le meme chiffre que les mouvements. Ce n'est PAS
        # une composante du stock : c'est une comparaison, affichee a part
        # pour ne pas laisser croire a un calcul bancal.
        recu_mouvements = q('in', 'supplier') - q('out', 'supplier')
        sorties_client = q('out', 'customer') - q('in', 'customer')

        return {
            # Mouvements bruts
            'recept_fournisseur': int(round(q('in', 'supplier'))),
            'retour_fournisseur': int(round(q('out', 'supplier'))),
            'sortie_client': int(round(q('out', 'customer'))),
            'retour_client': int(round(q('in', 'customer'))),
            'inventaire_gain': int(round(q('in', 'inventory'))),
            'inventaire_perte': int(round(q('out', 'inventory'))),
            'autres_in': int(round(autres_in)),
            'autres_out': int(round(autres_out)),
            'stock_attendu': int(round(stock_attendu)),
            'stock_reel': int(round(stock_reel)),
            'ecart': int(round(stock_attendu - stock_reel)),
            # Comparaison documents vs mouvements
            'qty_purchased_doc': int(round(qty_purchased)),
            'recu_mouvements': int(round(recu_mouvements)),
            'reception_hors_bon': int(round(recu_mouvements - qty_purchased)),
            'qty_sold_doc': int(round(qty_sold)),
            'sorties_client_mvt': int(round(sorties_client)),
            'sortie_hors_vente': int(round(sorties_client - qty_sold)),
            # Conservés pour les exports CSV/XLSX
            'achats_recus': int(round(qty_purchased)),
            'ventes_livrees': int(round(qty_sold)),
            'inventaire_net': int(round(q('out', 'inventory') - q('in', 'inventory'))),
            'autres_net': int(round(autres_out - autres_in)),
        }

    @http.route('/mavie/api/product-stock-detail', type='json', auth='user',
                methods=['POST'], csrf=False)
    def api_product_stock_detail(self, **kw):
        """Pieces justificatives de chaque ligne de la reconciliation.

        DEMANDE UTILISATEUR : en cliquant sur la carte "Stock Reel Odoo",
        voir le tableau qui explique le stock ligne par ligne AVEC les
        documents derriere chaque ligne (quel bon de commande, quel bon de
        retour, quel ajustement). Les totaux, eux, arrivent deja avec la
        fiche produit (cle `reconciliation`) : cette route ne renvoie que le
        detail, pour ne pas ralentir l'ouverture de la fiche.
        """
        try:
            article_id = kw.get('article_id')
            if not article_id:
                return {'error': 'Reference manquante.'}
            product_tmpl = request.env['product.template'].sudo().browse(int(article_id))
            if not product_tmpl.exists():
                return {'error': 'Reference introuvable.'}
            variants = request.env['product.product'].sudo().with_context(
                active_test=False
            ).search([('product_tmpl_id', '=', product_tmpl.id)])
            if not variants:
                return {'error': 'Aucune variante pour cette reference.'}

            warehouses, inside_ids = self._reconciliation_scope(kw)
            if not inside_ids:
                return {'error': 'Aucun magasin dans le perimetre selectionne.'}

            MoveLine = request.env['stock.move.line'].sudo()

            def _doc_name(ml):
                if ml.picking_id:
                    return ml.picking_id.name
                return ml.reference or (ml.move_id.origin if ml.move_id else '') or '—'

            # ── 1. Achats : bons de commande du perimetre, commande vs recu.
            po_lines = request.env['purchase.order.line'].sudo().search(
                self._build_purchase_domain(kw, [product_tmpl.id])
            )
            achats_by_order = {}
            for pol in po_lines:
                order = pol.order_id
                row = achats_by_order.setdefault(order.id, {
                    'bon': order.name,
                    'date': str(order.date_order)[:10] if order.date_order else '—',
                    'fournisseur': order.partner_id.name or '—',
                    'commande': 0.0,
                    'recu': 0.0,
                })
                row['commande'] += pol.product_qty
                row['recu'] += pol.qty_received
            achats = []
            for row in achats_by_order.values():
                row['commande'] = int(round(row['commande']))
                row['recu'] = int(round(row['recu']))
                row['ecart'] = row['commande'] - row['recu']
                achats.append(row)
            achats.sort(key=lambda r: (-r['ecart'], r['date']))

            # ── 2. Retours au fournisseur : sorties vers un emplacement
            # fournisseur, regroupees par bon.
            retours_fournisseur = {}
            for ml in MoveLine.search([
                ('state', '=', 'done'),
                ('product_id', 'in', variants.ids),
                ('location_id', 'in', inside_ids),
                ('location_dest_id.usage', '=', 'supplier'),
            ]):
                key = _doc_name(ml)
                row = retours_fournisseur.setdefault(key, {
                    'bon': key,
                    'origine': (ml.picking_id.origin if ml.picking_id else '') or '—',
                    'date': str(ml.date)[:10] if ml.date else '—',
                    'qty': 0.0,
                })
                row['qty'] += ml.quantity
                if ml.date and str(ml.date)[:10] > row['date']:
                    row['date'] = str(ml.date)[:10]
            retours_fournisseur = sorted(
                ({**r, 'qty': int(round(r['qty']))} for r in retours_fournisseur.values()),
                key=lambda r: -r['qty'],
            )

            # ── 3. Sorties vers les clients, par type de document : c'est ce
            # qui permet de voir qu'une sortie est passee par un bon de
            # livraison et non par la caisse (donc jamais comptee en vente).
            def _type_doc(ml):
                picking_type = ml.picking_id.picking_type_id if ml.picking_id else False
                if picking_type:
                    return picking_type.display_name or picking_type.name or 'Autre'
                return 'Mouvement sans bon'

            ventes_docs = {}
            for direction, domain in (
                ('sortie', [('location_id', 'in', inside_ids),
                            ('location_dest_id.usage', '=', 'customer')]),
                ('retour', [('location_dest_id', 'in', inside_ids),
                            ('location_id.usage', '=', 'customer')]),
            ):
                for ml in MoveLine.search([
                    ('state', '=', 'done'),
                    ('product_id', 'in', variants.ids),
                ] + domain):
                    label = _type_doc(ml)
                    row = ventes_docs.setdefault(label, {
                        'type_document': label, 'sortie': 0.0, 'retour': 0.0,
                    })
                    row[direction] += ml.quantity
            ventes_documents = sorted(
                ({'type_document': r['type_document'],
                  'sortie': int(round(r['sortie'])),
                  'retour': int(round(r['retour'])),
                  'net': int(round(r['sortie'] - r['retour']))}
                 for r in ventes_docs.values()),
                key=lambda r: -r['net'],
            )

            # ── 4. Retours sur bon de vente (return_id pointant sur une
            # livraison), a distinguer d'un retour de caisse.
            retours_vente_by_doc = {}
            for ml in MoveLine.search([
                ('state', '=', 'done'),
                ('product_id', 'in', variants.ids),
                ('location_dest_id', 'in', inside_ids),
                ('location_id.usage', '=', 'customer'),
                ('picking_id.return_id', '!=', False),
            ]):
                origine = ml.picking_id.return_id
                if origine.picking_type_id.code != 'outgoing':
                    continue
                # Un bon de retour porte une ligne par variante : on regroupe
                # par document, sinon la meme reference ressort 4 fois.
                row = retours_vente_by_doc.setdefault(ml.picking_id.id, {
                    'bon': ml.picking_id.name,
                    'livraison': origine.name,
                    'bon_vente': origine.origin or '—',
                    'date': str(ml.date)[:10] if ml.date else '—',
                    'qty': 0.0,
                })
                row['qty'] += ml.quantity
            retours_vente = sorted(
                ({**r, 'qty': int(round(r['qty']))} for r in retours_vente_by_doc.values()),
                key=lambda r: -r['qty'],
            )

            # DEMANDE UTILISATEUR (2026-09-07) : le detail mensuel des
            # ajustements d'inventaire a ete retire du pop-up. Les totaux
            # pertes/gains restent affiches dans le bloc 1 (reconciliation),
            # ou ils comptent vraiment ; le decoupage par mois n'ajoutait
            # rien et allongeait la page. On ne le calcule donc plus.

            return {
                'ref': product_tmpl.base_pivot_reference or product_tmpl.default_code or product_tmpl.name,
                'name': product_tmpl.name,
                'perimetre': ', '.join(warehouses.mapped('name')) or '—',
                'achats': achats[:60],
                'achats_total': len(achats),
                'retours_fournisseur': retours_fournisseur[:40],
                'ventes_documents': ventes_documents,
                'retours_vente': retours_vente[:40],
            }
        except Exception as e:
            _logger.error("Erreur api_product_stock_detail: %s", e, exc_info=True)
            return {'error': str(e)}

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
                    # qty_delivered (net des retours sur bon de vente), pas
                    # product_uom_qty : une commande livrée puis retournée en
                    # totalité ne doit pas rester comptée comme vendue.
                    qty_sold += int(sum(ca_so_lines.mapped('qty_delivered')))
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
            # Qté achetée = quantité RÉELLEMENT REÇUE. Odoo tient
            # qty_received net des retours fournisseur (vérifié en base :
            # P01085 = 4 950 commandé / 0 reçu, tout ayant été renvoyé).
            # product_qty (commandé) reste calculé à côté pour le prix
            # d'achat moyen uniquement.
            qty_purchased = int(sum(po_lines.mapped('qty_received'))) if po_lines else 0
            # Un magasin est demandé mais les bons d'achat de cette base ne
            # visent pas les entrepôts magasin : les achats affichés sont
            # ceux de la société. L'écran le dit en info-bulle.
            scope_achat = self._get_shop_scope(kw.get('shop_field'))
            achats_perimetre = 'magasin'
            if (kw.get('shop_field') and scope_achat and scope_achat.get('warehouse')
                    and not self._entrepot_recoit_des_achats(scope_achat['warehouse'].id)):
                achats_perimetre = 'societe'
            qty_ordered = int(sum(po_lines.mapped('product_qty'))) if po_lines else 0
            # Dans Odoo, la liste des lignes de commande filtree sur le
            # produit additionne TOUTES les societes : MRC-3313 y totalise
            # 2 321 recues la ou cette carte en montre 1 120. Les 1 201
            # pieces d'ecart sont celles que le depot a recues du
            # fournisseur externe avant de les revendre aux magasins : les
            # additionner reviendrait a compter deux fois la meme
            # marchandise. On les calcule a part pour que l'ecran puisse
            # afficher le rapprochement, sans toucher au total.
            qty_purchased_depot = 0
            depot_achats = self._societe_depot()
            if depot_achats and depot_achats.id not in po_lines.mapped(
                    'order_id.company_id').ids:
                lignes_depot = request.env['purchase.order.line'].sudo().search([
                    ('product_id.product_tmpl_id', '=', product_tmpl.id),
                    ('order_id.company_id', '=', depot_achats.id),
                    ('order_id.state', 'in', ['purchase', 'done']),
                ])
                qty_purchased_depot = int(sum(lignes_depot.mapped('qty_received')))
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
            if qty_ordered > 0 and ca_achat_ht > 0 and pv_ttc_ref > 0:
                prix_achat_moyen = ca_achat_ht / qty_ordered
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
            # qty_received, comme la carte "Qté achetée" : une commande
            # confirmee mais jamais receptionnee, ou receptionnee puis
            # renvoyee au fournisseur, ne doit pas gonfler "Total pieces".
            purchased_by_variant = {}
            for pol in po_lines:
                purchased_by_variant[pol.product_id.id] = purchased_by_variant.get(pol.product_id.id, 0) + pol.qty_received

            # Stock attendu par variante d'apres les mouvements, sur le MEME
            # perimetre que var_stock ci-dessous (retail_lot_stock_ids) :
            # c'est ce qui remplace l'ancien "Reste = achats - ventes", une
            # estimation papier qui ne pouvait structurellement pas tomber
            # sur le stock reel.
            retail_inside_ids = request.env['stock.location'].sudo().search([
                ('id', 'child_of', retail_lot_stock_ids)
            ]).ids if retail_lot_stock_ids else []
            attendu_by_variant = self._stock_ledger_by_variant(
                product_variants.ids, retail_inside_ids
            )

            # Stock present par variante (negatifs exclus, positivite jugee
            # magasin par magasin) : la colonne Stock du tableau des couleurs
            # doit suivre la meme regle que la carte du haut, sinon la somme
            # des couleurs ne correspond plus au total affiche.
            _pv = self._positive_stock(product_variants.ids, variant_warehouses.ids)
            positif_par_variante, net_par_variante = _pv[0], _pv[4]

            prix_ratio = self._solde_tax_ratio(product_tmpl, request.env.company)
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
                # Stock present (negatifs exclus), meme regle que la carte.
                var_stock = int(round(positif_par_variante.get(v.id, 0.0)))
                # Stock comptable (negatifs inclus) : sert uniquement au
                # controle "quantite ecrite sans mouvement", qui doit se
                # comparer a du comptable, pas au stock present.
                var_stock_comptable = int(round(net_par_variante.get(v.id, 0.0)))

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
                        'stock_comptable': 0,
                        'attendu': 0,
                        'stock_shop': 0 if kw.get('shop_field') else None,
                        'shops': {},
                    }
                    by_color[color_label] = entry

                # DEMANDE UTILISATEUR (2026-09-21) : afficher le prix de vente
                # de chaque couleur avant la quantité vendue. Prix catalogue
                # TTC de la variante (prix de vente + supplément de variante,
                # taxes de l'article) ; min/max si les tailles d'une même
                # couleur n'ont pas toutes le même prix.
                v_prix = round(v.lst_price * prix_ratio, 2)
                entry['prix_min'] = v_prix if entry.get('prix_min') is None else min(entry['prix_min'], v_prix)
                entry['prix_max'] = v_prix if entry.get('prix_max') is None else max(entry['prix_max'], v_prix)
                entry['qty'] += int(stat['qty'])
                entry['ca'] += stat['ca']
                entry['dispatched'] += dispatched
                if dispatch_source:
                    entry['dispatch_source'] = dispatch_source
                entry['stock'] += var_stock
                entry['stock_comptable'] += var_stock_comptable
                entry['attendu'] += int(round(attendu_by_variant.get(v.id, 0.0)))
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

                # Total pièces = quantité réellement RÉCEPTIONNÉE sur les
                # commandes fournisseur pour cette couleur (qty_received),
                # nette des retours au fournisseur.
                v['total_pieces'] = dispatched_v

                # "Reste" ne vaut plus "Total pièces − Vendu" : cette
                # soustraction mélangeait des DOCUMENTS (achats, tickets) et
                # ne pouvait donc jamais retomber sur le stock physique — sur
                # LQ-119 NOIR elle affichait 17 face à 9 en stock réel, sans
                # qu'aucune des deux valeurs ne soit fausse.
                # Reste = stock attendu d'après TOUS les mouvements validés
                # de cette couleur, sur le même périmètre que la colonne
                # Stock. Il est donc directement comparable, et l'écart qui
                # subsiste est une vraie anomalie (quantité écrite sans
                # mouvement), pas un artefact de calcul.
                attendu_v = v.get('attendu', 0)
                v['reste'] = attendu_v
                # La discordance se juge contre le stock COMPTABLE : c'est le
                # seul chiffre auquel les mouvements peuvent retomber. La
                # comparer au stock present ferait sonner l'alerte sur toutes
                # les couleurs ayant un magasin en negatif, ce qui est une
                # autre anomalie, signalee separement.
                stock_comptable_v = v.get('stock_comptable', 0)
                v['discordance'] = abs(attendu_v - stock_comptable_v) > 0.01
                v['discordance_detail'] = (
                    f"Stock attendu d'après les mouvements: {attendu_v}, "
                    f"stock comptable Odoo: {stock_comptable_v}"
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
            # Entrepots reellement additionnes dans stock_total : la
            # reconciliation doit porter EXACTEMENT sur le meme perimetre,
            # sinon le "stock attendu" ne peut pas retomber sur le stock
            # affiche.
            recon_warehouses = scoped_warehouses
            if kw.get('shop_field'):
                scope_detail = self._get_shop_scope(kw['shop_field'])
                if scope_detail and scope_detail['warehouse']:
                    target_wh = scope_detail['warehouse'].name
                    stock_total = sum(s['stock'] for s in stock_by_store if s['store_name'] == target_wh)
                    recon_warehouses = scope_detail['warehouse']
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
                    recon_warehouses = scoped_warehouses.filtered(
                        lambda w: w.company_id.id in context_company_ids
                    )
                else:
                    stock_total = sum(s['stock'] for s in stock_by_store)

            # RECONCILIATION : stock attendu (mouvements valides) vs stock
            # reel (stock.quant), poste par poste. Remplace l'ancien
            # "stock theorique = achete - vendu" qui ne pouvait pas boucler.
            recon_lot_stock_ids = recon_warehouses.mapped('lot_stock_id').ids
            recon_inside_ids = request.env['stock.location'].sudo().search([
                ('id', 'child_of', recon_lot_stock_ids)
            ]).ids if recon_lot_stock_ids else []
            reconciliation = self._stock_reconciliation(
                product_variants.ids, recon_inside_ids,
                qty_purchased, qty_sold, stock_total,
            )

            # Stock REELLEMENT PRESENT en rayon : les stocks negatifs ne
            # comptent pas comme du stock. La reconciliation, elle, reste
            # ancree sur le total comptable (stock_total) — c'est le seul
            # chiffre auquel les mouvements peuvent retomber.
            (_pos_by_variant, stock_present, stock_negatif,
             nb_magasins_negatifs, _net_by_variant) = self._positive_stock(
                product_variants.ids, recon_warehouses.ids
            )
            reconciliation['stock_present'] = int(round(stock_present))
            reconciliation['stock_negatif'] = int(round(stock_negatif))
            reconciliation['nb_magasins_negatifs'] = nb_magasins_negatifs

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
                # qty_received, pour rester cohérent avec la carte
                # "Qté achetée" (dont le total par magasin doit correspondre).
                purchased_by_warehouse[wh.id] = purchased_by_warehouse.get(wh.id, 0) + pol.qty_received

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
                'qty_purchased_depot': qty_purchased_depot,
                'depot_nom': depot_achats.name if depot_achats else '',
                'achats_perimetre': achats_perimetre,
                # Stock comptable Odoo (negatifs inclus) — sert de reference
                # a la reconciliation, qui ne peut retomber que sur lui.
                'stock_total': stock_total,
                # Stock REELLEMENT PRESENT en rayon (negatifs exclus) :
                # c'est ce que la carte affiche, les stocks negatifs de cette
                # base ne pouvant pas etre corriges dans Odoo.
                'stock_present': int(round(stock_present)),
                'stock_negatif': int(round(stock_negatif)),
                'nb_magasins_negatifs': nb_magasins_negatifs,
                # Stock attendu = reconstitue a partir des mouvements valides
                # (achats recus, ventes livrees, sorties hors vente,
                # inventaire, autres). L'ecart qui subsiste apres ca n'est
                # explique par AUCUN mouvement : c'est une anomalie de
                # donnees, pas un poste metier oublie.
                'stock_theorique': reconciliation['stock_attendu'],
                'stock_ecart': reconciliation['ecart'],
                'reconciliation': reconciliation,
                'qty_ordered': qty_ordered,
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
                'prix_vente_ttc': round(product_tmpl.list_price * prix_ratio, 2),
                'has_base_pivot_data': has_base_pivot_data,
                'stock_by_store': stock_by_store_pivot,
                'real_stock_by_store': stock_by_store,
                'verification': {
                    'by_color': verif_by_color,
                    'by_magasin': verif_by_magasin,
                },
                'image_url': image_url,
                'actions_detail': self._actions_detail(product_tmpl.id),
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
        writer.writerow(['Stock attendu (mouvements validés)', data.get('stock_theorique', 0)])
        writer.writerow(['Écart inexpliqué (attendu - réel)', data.get('stock_ecart', 0)])
        writer.writerow([])
        # Le detail de la reconciliation, dans le meme ordre que le pop-up
        # de la carte "Stock Reel Odoo".
        recon = data.get('reconciliation') or {}
        if recon:
            writer.writerow(['1. Le stock expliqué par les mouvements', 'Pièces'])
            writer.writerow(['Réceptions fournisseur', recon.get('recept_fournisseur', 0)])
            writer.writerow(['Retours au fournisseur', -recon.get('retour_fournisseur', 0)])
            writer.writerow(['Retours clients', recon.get('retour_client', 0)])
            writer.writerow(['Sorties vers les clients', -recon.get('sortie_client', 0)])
            writer.writerow(["Gains d'inventaire", recon.get('inventaire_gain', 0)])
            writer.writerow(["Pertes d'inventaire", -recon.get('inventaire_perte', 0)])
            writer.writerow(['Autres entrées', recon.get('autres_in', 0)])
            writer.writerow(['Autres sorties', -recon.get('autres_out', 0)])
            writer.writerow(['= Stock attendu', recon.get('stock_attendu', 0)])
            writer.writerow(['Stock réel Odoo', recon.get('stock_reel', 0)])
            writer.writerow(['Écart inexpliqué', recon.get('ecart', 0)])
            writer.writerow([])
            writer.writerow(['2. Documents vs mouvements', 'Pièces'])
            writer.writerow(['Qté achetée affichée (bons de commande reçus)', recon.get('qty_purchased_doc', 0)])
            writer.writerow(['Réceptions réellement entrées ici', recon.get('recu_mouvements', 0)])
            writer.writerow(['Différence achats', recon.get('reception_hors_bon', 0)])
            writer.writerow(['Qté vendue affichée (caisse + bons de vente livrés)', recon.get('qty_sold_doc', 0)])
            writer.writerow(['Sorties clients réellement constatées ici', recon.get('sorties_client_mvt', 0)])
            writer.writerow(['Différence ventes', recon.get('sortie_hors_vente', 0)])
            writer.writerow([])
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
            ('Stock attendu (mouvements validés)', data.get('stock_theorique', 0), 'num'),
            ('Écart inexpliqué (attendu - réel)', data.get('stock_ecart', 0), 'num'),
            ('CA Vendu (TTC)', data.get('ca', 0), 'money'),
            ('CA Achat', data.get('ca_achat', 0), 'money'),
            ('Sell-through (%)', data.get('sell_through', 0), 'money'),
        ]:
            sheet.write(row, 0, label, fmt['cell'])
            sheet.write(row, 1, value, fmt[style])
            row += 1

        # Reconciliation du stock, dans le meme ordre que le pop-up de la
        # carte "Stock Reel Odoo" : chaque poste nomme, l'ecart en bas.
        recon = data.get('reconciliation') or {}
        if recon:
            row += 1
            sheet.write(row, 0, 'Réconciliation du stock', fmt['title'])
            row += 1
            sheet.write(row, 0, 'Poste', fmt['header'])
            sheet.write(row, 1, 'Pièces', fmt['header'])
            row += 1
            for label, value in [
                ('Réceptions fournisseur', recon.get('recept_fournisseur', 0)),
                ('Retours au fournisseur', -recon.get('retour_fournisseur', 0)),
                ('Retours clients', recon.get('retour_client', 0)),
                ('Sorties vers les clients', -recon.get('sortie_client', 0)),
                ("Gains d'inventaire", recon.get('inventaire_gain', 0)),
                ("Pertes d'inventaire", -recon.get('inventaire_perte', 0)),
                ('Autres entrées', recon.get('autres_in', 0)),
                ('Autres sorties', -recon.get('autres_out', 0)),
                ('= Stock attendu', recon.get('stock_attendu', 0)),
                ('Stock réel Odoo', recon.get('stock_reel', 0)),
                ('Écart inexpliqué', recon.get('ecart', 0)),
                ('— Documents vs mouvements —', ''),
                ('Qté achetée affichée', recon.get('qty_purchased_doc', 0)),
                ('Réceptions réellement entrées ici', recon.get('recu_mouvements', 0)),
                ('Différence achats', recon.get('reception_hors_bon', 0)),
                ('Qté vendue affichée', recon.get('qty_sold_doc', 0)),
                ('Sorties clients réellement constatées ici', recon.get('sorties_client_mvt', 0)),
                ('Différence ventes', recon.get('sortie_hors_vente', 0)),
            ]:
                sheet.write(row, 0, label, fmt['cell'])
                sheet.write(row, 1, value, fmt['num'])
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

    def _transfer_pdf_attachment(self, transfer):
        """Le bon de transfert en PDF — le même que « Imprimer le bon » du
        dashboard — sous forme de pièce jointe (nom, contenu brut)."""
        try:
            pdf, _fmt = request.env['ir.actions.report'].sudo()._render_qweb_pdf(
                'mavie_dashboard.action_report_transfer', [transfer.id]
            )
        except Exception as e:
            _logger.warning(f"PDF du transfert {transfer.name} non généré : {e}", exc_info=True)
            return None
        fname = re.sub(r'[^\w.-]+', '_', f"Bon_transfert_{transfer.name}") + '.pdf'
        return (fname, pdf)

    # Bandeau des notifications de TEST : impossible de le confondre avec un
    # vrai bon à préparer.
    BANDEAU_TEST = (
        '<div style="background:#DC2626;color:#FFFFFF;font-size:16px;font-weight:800;'
        'padding:14px 16px;border-radius:8px;margin-bottom:14px;text-align:center;">'
        '⚠️ CECI EST UN TEST — NE FAITES RIEN<br/>'
        '<span style="font-weight:600;font-size:13px;">Les magasins n\'ont rien reçu.</span></div>'
    )

    @http.route('/mavie/api/transfer-notif-test', type='json', auth='user', methods=['POST'], csrf=False)
    def api_transfer_notif_test(self, **kw):
        """DEMANDE UTILISATRICE (2026-09-23) : voir à quoi ressemble la
        notification d'un transfert sans déranger les responsables de
        magasin. On renvoie EXACTEMENT le même message (bon PDF compris),
        mais au seul utilisateur connecté et coiffé d'un bandeau rouge
        « ceci est un test ». Rien n'est créé ni modifié dans Odoo."""
        try:
            transfer = request.env['inter.internal.transfer'].sudo().browse(
                int(kw.get('transfer_id') or 0)).exists()
            if not transfer:
                return {'error': 'Transfert introuvable.'}
            mappings = self._get_active_shop_mappings()

            def mapping_de(location):
                wh = location.warehouse_id
                return mappings.filtered(lambda m: m.warehouse_id == wh)[:1] if wh else mappings.browse()

            source = mapping_de(transfer.location_source_id)
            dest = mapping_de(transfer.location_target_id)
            if not source or not dest:
                return {'error': "Ce bon ne pointe pas sur deux magasins connus du dashboard."}
            notified, warning = self._notify_transfer_responsible(
                transfer, source, dest, test_partner=request.env.user.partner_id)
            return {'ok': True, 'transfert': transfer.name, 'destinataire': request.env.user.name,
                    'messages': len(notified.get('source') or []) + len(notified.get('dest') or []),
                    'warning': warning}
        except Exception as e:
            _logger.error(f"Erreur api_transfer_notif_test: {str(e)}", exc_info=True)
            return {'error': str(e)}

    def _notify_transfer_responsible(self, transfer, source_mapping, dest_mapping,
                                     test_partner=None):
        """Notifie les responsables des deux magasins dans Odoo UNIQUEMENT
        (boîte de réception, jamais d'email — voir mail_thread_ext.py), bon de
        transfert PDF en pièce jointe.

        Responsables = poste « Manager » + magasin dans leurs POS autorisés
        (voir MvBatchShopMappingExt._get_store_managers) :
          • magasin source : prépare la marchandise et valide l'opération ;
          • magasin cible  : est prévenu de l'arrivée, pour contrôler la
            réception.

        Retourne (notified, warning) : noms notifiés par rôle, et message à
        afficher dans le dashboard pour ce qui n'a pas pu être envoyé.
        """
        def _label(mapping):
            return mapping.warehouse_id.name or mapping.shop_label or mapping.shop_field

        source_managers = source_mapping._get_store_managers()
        dest_all = dest_mapping._get_store_managers()
        if test_partner:
            # Mode test : personne d'autre ne reçoit rien, et les deux
            # messages (préparer / réceptionner) partent à l'utilisateur.
            source_managers = dest_all = request.env.user
        # Un même utilisateur responsable des deux magasins ne reçoit que la
        # notification « à préparer », qui contient déjà tout.
        dest_managers = dest_all if test_partner else dest_all - source_managers

        notified = {'source': [], 'dest': []}
        warnings = [
            f"Aucun responsable trouvé pour {_label(m)} (poste « Manager » avec ce "
            f"magasin dans ses POS autorisés) : notification non envoyée."
            for m, found in ((source_mapping, source_managers), (dest_mapping, dest_all))
            if not found
        ]
        if not source_managers and not dest_managers:
            return notified, ' '.join(warnings)

        # Le bon PDF en premier : c'est la pièce que le responsable imprime
        # pour préparer (source) ou contrôler (cible) la marchandise.
        attachments = []
        pdf = self._transfer_pdf_attachment(transfer)
        if pdf:
            attachments.append(pdf)
        else:
            warnings.append("Le bon PDF n'a pas pu être généré : notification envoyée sans pièce jointe.")

        lines_txt = []
        photos = 0
        for line in transfer.line_ids:
            # escape() sur les valeurs venant des données : un nom de produit
            # contenant < ou & casserait sinon le corps du message.
            lines_txt.append(
                f"<li>{escape(line.product_id.display_name)} — "
                f"Réf : {escape(line.reference or '—')} — Qté : {int(line.quantity)}</li>"
            )
            image = line.product_id.image_1920 or line.product_id.product_tmpl_id.image_1920
            if image and photos < 5:
                fname = f"{line.product_id.default_code or line.product_id.id}.png"
                try:
                    attachments.append((fname, base64.b64decode(image)))
                    photos += 1
                except Exception:
                    pass

        source_label = escape(_label(source_mapping))
        dest_label = escape(_label(dest_mapping))

        # La notification doit pointer sur le document que le responsable va
        # réellement ouvrir pour collecter et valider — et il n'est pas au
        # même endroit selon les sociétés (voir
        # InterInternalTransferExt.action_submit) :
        #   • même société      → l'opération interne, dans l'Inventaire ;
        #   • sociétés ≠        → le bon lui-même, dans le module Transferts.
        picking = transfer.picking_id
        if picking:
            model, res_id = 'stock.picking', picking.id
            where_txt = (
                f"à collecter puis valider dans <strong>Inventaire → Transferts → "
                f"Interne</strong> (opération <strong>{escape(picking.name)}</strong>)"
            )
        else:
            model, res_id = 'inter.internal.transfer', transfer.id
            where_txt = (
                "à collecter puis valider dans le <strong>module Transferts</strong> "
                "(transfert entre deux sociétés)"
            )

        route_txt = (
            f"<p><strong>Transfert {escape(transfer.name)}</strong> : "
            f"<strong>{source_label}</strong> → <strong>{dest_label}</strong></p>"
        )
        if test_partner:
            route_txt = self.BANDEAU_TEST + route_txt
        items_txt = f"<ul>{''.join(lines_txt)}</ul>"
        # Boîte de réception Odoo uniquement, même pour les utilisateurs réglés
        # sur « Notification par email » (demande utilisateur).
        mail_thread = request.env['mail.thread'].sudo().with_context(
            mavie_notify_inbox_only=True,
            # Mode test : l'utilisatrice est à la fois à l'origine du message et
            # sa destinataire. Odoo n'envoie jamais à l'utilisateur courant
            # (`real_author_id` dans mail_thread._notify_get_recipients), d'où
            # « je n'ai rien reçu » le 2026-09-23 : on lève ce garde-fou, mais
            # seulement pour le test.
            mail_notify_author=bool(test_partner),
        )
        # Mode test : l'utilisatrice est destinataire. Odoo ne notifie jamais
        # l'auteur de son propre message — sans changer d'auteur, le message
        # était créé mais n'apparaissait pas dans la cloche (constaté le
        # 2026-09-23 : « je n'ai rien reçu »). On le fait donc signer par
        # OdooBot.
        extra = {'author_id': request.env.ref('base.partner_root').id} if test_partner else {}

        # Markup() : sans ça, Odoo traite le corps comme du texte brut et
        # échappe les balises — le responsable reçoit « &lt;p&gt;… » au lieu
        # du message mis en forme.
        if source_managers:
            mail_thread.message_notify(
                partner_ids=source_managers.mapped('partner_id').ids,
                subject=('[TEST] ' if test_partner else '') + f"Transfert {transfer.name} à préparer — {_label(source_mapping)}",
                body=Markup(
                    f"{route_txt}"
                    f"<p>Marchandise {where_txt}. Le bon de transfert est joint en PDF.</p>"
                    f"{items_txt}"
                ),
                model=model,
                res_id=res_id,
                attachments=attachments,
                **extra,
            )
            notified['source'] = source_managers.mapped('name')
        if dest_managers:
            mail_thread.message_notify(
                partner_ids=dest_managers.mapped('partner_id').ids,
                subject=('[TEST] ' if test_partner else '') + f"Transfert {transfer.name} à réceptionner — {_label(dest_mapping)}",
                body=Markup(
                    f"{route_txt}"
                    f"<p>Marchandise en route vers votre magasin : à contrôler à la "
                    f"réception avec le bon de transfert joint en PDF.</p>"
                    f"{items_txt}"
                ),
                model=model,
                res_id=res_id,
                attachments=attachments,
                **extra,
            )
            notified['dest'] = dest_managers.mapped('name')

        # CONSTAT UTILISATRICE (2026-09-23) : en ouvrant le bon depuis la
        # cloche, sa conversation était VIDE. C'est Odoo : message_notify
        # crée des messages de type « user_notification », que le chatter
        # d'un document n'affiche jamais (domaine de message_ids). On écrit
        # donc aussi une note sur le bon lui-même, bon PDF joint : le
        # responsable retrouve la consigne là où il travaille. Sans
        # destinataire : aucune notification en double.
        try:
            document = request.env[model].sudo().browse(res_id).exists()
            if document and hasattr(document, 'message_post'):
                document.with_context(mail_notify_author=False).message_post(
                    body=Markup(
                        f"{route_txt}"
                        f"<p>Marchandise {where_txt}. Bon de transfert en PDF ci-joint.</p>"
                        f"{items_txt}"
                    ),
                    subject=('[TEST] ' if test_partner else '') + f"Transfert {transfer.name}",
                    subtype_xmlid='mail.mt_note',
                    attachments=attachments,
                )
        except Exception as e:  # une note manquante ne doit jamais bloquer
            _logger.warning("Note sur le bon de transfert impossible : %s", e)
        return notified, ' '.join(warnings) or None

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
                    'has_responsible': bool(m._get_store_managers()),
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
                    'has_responsible': bool(m._get_store_managers()),
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

    def _mouvements_couleur(self, variant_ids):
        """Ce qui s'est passé sur ces variantes, magasin par magasin.

        DEMANDE UTILISATRICE (2026-09-23) : dans « Stock par magasin — cette
        couleur », voir d'où vient le stock au lieu d'un nombre gris —
        transfert reçu ou envoyé, réassort, vente en solde.

        {wh_id: {'entree': n, 'sortie': n, 'reassort': n, 'attente': n,
                 'solde': n, 'details': [texte, …]}}
          - entree / sortie : pièces d'un bon de transfert DÉJÀ FAIT
            (stock déplacé). 'attente' compte les pièces d'un bon encore à
            valider : le stock n'a pas encore bougé, c'est dit en clair.
          - reassort : transfert lancé depuis la fenêtre Réassort.
          - solde : pièces vendues en caisse sous le prix catalogue.
        """
        variant_ids = list(variant_ids or [])
        out = {}
        if not variant_ids:
            return out

        def acc(wh):
            return out.setdefault(wh, {'entree': 0, 'sortie': 0, 'reassort': 0,
                                       'attente': 0, 'solde': 0, 'details': [], 'lignes': []})

        request.env.cr.execute("""
            SELECT t.name, t.state, COALESCE(t.origin, '') = %(orig)s,
                   ls.warehouse_id, ld.warehouse_id,
                   SUM(l.quantity), MAX(t.create_date),
                   MAX(ws.name), MAX(wd.name),
                   MAX(po.name), MAX(pk.name)
              FROM inter_internal_transfer_line l
              JOIN inter_internal_transfer t ON t.id = l.transfer_id
              LEFT JOIN stock_location ls ON ls.id = t.location_source_id
              LEFT JOIN stock_location ld ON ld.id = t.location_target_id
              LEFT JOIN stock_warehouse ws ON ws.id = ls.warehouse_id
              LEFT JOIN stock_warehouse wd ON wd.id = ld.warehouse_id
              LEFT JOIN purchase_order po ON po.id = t.purchase_id
              LEFT JOIN stock_picking pk ON pk.id = t.picking_id
             WHERE l.product_id = ANY(%(pids)s)
               AND COALESCE(t.state, 'draft') != 'draft'
             GROUP BY 1, 2, 3, 4, 5
             ORDER BY 7 DESC
        """, {'pids': variant_ids, 'orig': self.REASSORT_ORIGINE})
        for (nom, etat, est_reassort, wh_src, wh_dst, qte, date,
             nom_src, nom_dst, nom_po, nom_picking) in request.env.cr.fetchall():
            qte = int(round(qte or 0))
            fait = etat == 'done'
            quoi = 'Réassort' if est_reassort else 'Transfert'
            quand = str(date)[:10] if date else ''
            for wh, signe in ((wh_dst, 1), (wh_src, -1)):
                if not wh:
                    continue
                a = acc(wh)
                if not fait:
                    a['attente'] += qte
                elif signe > 0:
                    a['reassort' if est_reassort else 'entree'] += qte
                else:
                    a['sortie'] += qte
                # Les anciens bons inter-sociétés n'ont pas de numéro
                # (name = « New ») : ne pas l'afficher tel quel.
                # Les anciens bons inter-sociétés n'ont pas de numéro propre
                # (name = « New ») : on montre alors le document Odoo qui
                # porte le mouvement — bon d'achat miroir ou opération.
                libelle_bon = (nom if nom and nom != 'New' else None) or nom_po or nom_picking or 'sans numéro'
                a['details'].append('%s %s%s pcs · %s · %s%s' % (
                    quoi, '+' if signe > 0 else '−', qte, libelle_bon, quand,
                    '' if fait else ' · EN ATTENTE de validation, stock pas encore déplacé'))
                # Lignes détaillées pour le tableau dépliable du pop-up
                # couleur (demande utilisatrice 2026-09-23 : « je dois avoir
                # le détail : date, reçu ou envoyé, magasin »).
                a['lignes'].append({
                    'date': quand,
                    'sens': 'recu' if signe > 0 else 'envoye',
                    'quoi': quoi,
                    'qty': qte,
                    'bon': libelle_bon,
                    'avec': (nom_src if signe > 0 else nom_dst) or '—',
                    'etat': 'Fait' if fait else 'En attente de validation',
                    'fait': fait,
                })

        # Vendu en solde : prix réellement payé sous le prix catalogue.
        request.env.cr.execute("""
            SELECT spt.warehouse_id, SUM(pol.qty), MAX(po.date_order)
              FROM pos_order_line pol
              JOIN pos_order po ON po.id = pol.order_id
              JOIN pos_session ps ON ps.id = po.session_id
              JOIN pos_config pc ON pc.id = ps.config_id
              JOIN stock_picking_type spt ON spt.id = pc.picking_type_id
              JOIN product_product pp ON pp.id = pol.product_id
              JOIN product_template pt ON pt.id = pp.product_tmpl_id
             WHERE pol.product_id = ANY(%(pids)s)
               AND po.state IN ('paid', 'done', 'invoiced')
               AND pt.list_price > 0
               AND pol.price_unit * (1 - COALESCE(pol.discount, 0) / 100.0) < pt.list_price * 0.999
             GROUP BY 1
        """, {'pids': variant_ids})
        for wh, qte, derniere in request.env.cr.fetchall():
            if not wh:
                continue
            a = acc(wh)
            a['solde'] = int(round(qte or 0))
            if not a['solde']:
                # Ventes en solde annulées par des retours : rien à montrer.
                continue
            a['details'].append('Vendu en solde %s pcs · dernière vente %s' % (
                a['solde'], str(derniere)[:10] if derniere else '—'))
            a['lignes'].append({'date': str(derniere)[:10] if derniere else '',
                                'sens': 'solde', 'quoi': 'Vendu en solde', 'qty': a['solde'],
                                'bon': '—', 'avec': 'caisse', 'etat': 'Fait', 'fait': True})
        # Infobulle lisible : les 8 mouvements les plus récents suffisent.
        for a in out.values():
            a['lignes'].sort(key=lambda x: x['date'], reverse=True)
            a['lignes'] = a['lignes'][:40]
            if len(a['details']) > 8:
                reste = len(a['details']) - 8
                a['details'] = a['details'][:8] + ['… et %d autre%s mouvement%s' % (
                    reste, 's' if reste > 1 else '', 's' if reste > 1 else '')]
        return out

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

            # BUG CORRIGE (2026-09-07) : ce tableau listait TOUS les magasins
            # mappes sans tenir compte du selecteur de societe, alors que la
            # carte "Stock total" du meme pop-up, elle, l'applique (via
            # retail_lot_stock_ids). Les deux ne pouvaient donc pas se
            # recouper des qu'une societe etait cochee. On reprend ici le
            # meme perimetre. Le filtre MAGASIN de la fiche n'est
            # volontairement PAS applique : ce tableau sert justement a voir
            # ou se trouve la marchandise sur tout le reseau pour decider
            # d'un transfert, et la carte "Stock total" ne l'applique pas non
            # plus.
            _warehouses, inside_ids = self._reconciliation_scope(
                {k: val for k, val in kw.items() if k != 'shop_field'}
            )
            inside_set = set(inside_ids)

            # Ce qui s'est passé dans chaque magasin pour cette couleur.
            mouvements = self._mouvements_couleur(color_variants.ids)
            mappings = self._get_active_shop_mappings()
            # CORRIGÉ (2026-09-23, « il n'affiche plus le stock ») : avec
            # MOD FOR LIFE seule cochée, aucun magasin n'entre dans le
            # périmètre et le tableau restait vide, alors que la carte
            # « Stock total » affichait les pièces du dépôt. Aucun magasin
            # en périmètre : on montre tout le réseau et on le dit.
            note = ''
            if not any(m.warehouse_id and m.warehouse_id.lot_stock_id
                       and m.warehouse_id.lot_stock_id.id in inside_set for m in mappings):
                inside_set |= {m.warehouse_id.lot_stock_id.id for m in mappings
                               if m.warehouse_id and m.warehouse_id.lot_stock_id}
                note = 'Aucun magasin dans cette société : tout le réseau est affiché.'
            stores = []
            total_reseau = 0.0
            # DEMANDE UTILISATEUR (2026-09-07) : un stock negatif n'est pas
            # du stock — on ne peut pas avoir -1 piece en rayon. Le total
            # "ce qu'il y a reellement en magasin" ne doit donc additionner
            # que les stocks positifs. Le total comptable Odoo (negatifs
            # inclus) reste renvoye a cote : c'est lui qui sert de reference
            # a la reconciliation de la fiche, les deux doivent rester
            # lisibles sans se contredire.
            total_present = 0.0
            total_negatif = 0.0
            nb_magasins_negatifs = 0
            for m in mappings:
                if not m.warehouse_id or not m.warehouse_id.lot_stock_id:
                    continue
                if m.warehouse_id.lot_stock_id.id not in inside_set:
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
                total_reseau += total
                if total > 0:
                    total_present += total
                elif total < 0:
                    total_negatif += total
                    nb_magasins_negatifs += 1
                # Un magasin a stock nul n'apporte rien a la lecture, mais un
                # stock NEGATIF doit rester visible : c'est justement lui qui
                # explique qu'un total soit plus bas que la somme apparente
                # des lignes positives.
                if abs(total) < 0.01 and not by_size:
                    continue
                stores.append({
                    'shop_field': m.shop_field,
                    'shop_label': m.warehouse_id.name or m.shop_label or m.shop_field,
                    'city': m.city or '—',
                    'stock_total': int(round(total)),
                    'by_size': {k: int(round(v)) for k, v in by_size.items() if abs(v) >= 0.01},
                    'mouvements': mouvements.get(m.warehouse_id.id) or {},
                })

            # Plus gros stock en premier — facilite le choix d'un magasin
            # source pour un futur transfert de cette couleur.
            stores.sort(key=lambda s: -s['stock_total'])

            # Le dépôt MOD FOR LIFE n'est pas un magasin (aucun mapping) mais
            # c'est lui qui alimente le réassort : sa ligne manquait.
            mfl = self._societe_depot()
            if mfl:
                depot_quants = request.env['stock.quant'].sudo().search([
                    ('product_id', 'in', color_variants.ids),
                    ('location_id.usage', '=', 'internal'),
                    ('company_id', '=', mfl.id),
                ])
                depot_total = sum(depot_quants.mapped('quantity'))
                if depot_total:
                    depot_sizes = {}
                    for q in depot_quants:
                        t = size_by_variant.get(q.product_id.id, '—')
                        depot_sizes[t] = depot_sizes.get(t, 0.0) + q.quantity
                    stores.insert(0, {
                        'shop_field': None,
                        # Le dépôt ne s'appelle pas MOD FOR LIFE partout
                        # (STE XD MAX sur Elite) : on prend son vrai nom.
                        'shop_label': 'DÉPÔT %s' % (mfl.name or '').upper(),
                        'city': '—',
                        'stock_total': int(round(depot_total)),
                        'by_size': {k: int(round(v)) for k, v in depot_sizes.items() if abs(v) >= 0.01},
                        'mouvements': {},
                        'depot': True,
                    })

            return {
                'color': color,
                'stores': stores,
                'note': note,
                # Ce qu'il y a vraiment en rayon : somme des stocks positifs.
                'stock_present': int(round(total_present)),
                # Les stocks negatifs, isoles : ce sont des anomalies
                # (marchandise sortie sans jamais avoir ete recue dans ce
                # magasin), pas du stock a soustraire du rayon.
                'stock_negatif': int(round(total_negatif)),
                'nb_magasins_negatifs': nb_magasins_negatifs,
                # Total comptable Odoo (negatifs inclus) : c'est la valeur de
                # la carte "Stock total" et de la reconciliation.
                'stock_total': int(round(total_reseau)),
            }
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

            transfer = request.env['inter.internal.transfer'].sudo().with_context(
                mavie_intra_societe=True).create({
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
                # Lancé depuis la fenêtre Réassort de la page Action : marqué
                # pour être compté comme « réassort » (pastille verte) et non
                # comme un transfert ordinaire (voir _actions_references).
                'origin': self.REASSORT_ORIGINE if kw.get('reassort') else False,
            })
            # action_submit aiguille le bon vers l'endroit où il sera
            # collecté : Inventaire → Transferts → Interne si les deux
            # magasins appartiennent à la même société (l'opération y est
            # créée, réservée, prête à valider), module Transferts sinon.
            transfer.sudo().action_submit()
            picking = transfer.sudo().picking_id

            # La notification ne doit jamais faire échouer un transfert déjà
            # créé (et, en intra-société, déjà réservé dans l'Inventaire) :
            # le dashboard afficherait une erreur alors que le bon existe.
            # Le savepoint garde la transaction utilisable si l'envoi plante.
            try:
                with request.env.cr.savepoint():
                    notified, notif_warning = self._notify_transfer_responsible(
                        transfer, source_mapping, dest_mapping
                    )
            except Exception as e:
                _logger.error(f"Notification du transfert {transfer.name} échouée : {e}", exc_info=True)
                notified = {'source': [], 'dest': []}
                notif_warning = f"Transfert créé, mais notification non envoyée : {e}"

            _vider_cache_dashboard()
            return {
                'transfer_id': transfer.id,
                'transfer_name': transfer.name,
                'group_ref': transfer.group_ref,
                'warning': warning,
                'notif_warning': notif_warning,
                'notified': notified,
                'intra_societe': source_mapping.company_id.id == dest_mapping.company_id.id,
                'picking_id': picking.id if picking else None,
                'picking_name': picking.name if picking else None,
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

    def _prix_achat_moyen_par_reference(self, tmpl_ids, kw=None):
        """Prix d'achat moyen HT par référence, sur le périmètre du CA Achat.

        Sert de repli quand le champ « Coût » d'Odoo est vide. Le tableau de
        valorisation et son pop-up le calculaient chacun de leur côté, avec
        des périmètres différents : le tableau sur les sociétés cochées, le
        pop-up sur TOUTES les sociétés. Pour SQUARE TARGA la même valeur au
        coût sortait à 714 749,55 d'un côté et 357 243,25 de l'autre
        (constaté le 2026-09-25). Un seul calcul, donc.
        """
        if not tmpl_ids:
            return {}
        domain = self._build_purchase_domain(kw or {}, list(tmpl_ids))
        groupes = request.env['purchase.order.line'].sudo().read_group(
            domain, ['product_qty:sum', 'price_subtotal:sum'], ['product_id'], lazy=False)
        pids = [g['product_id'][0] for g in groupes if g.get('product_id')]
        tmpl_par_variante = {}
        if pids:
            tmpl_par_variante = {
                p['id']: p['product_tmpl_id'][0]
                for p in request.env['product.product'].sudo().with_context(
                    active_test=False).search_read([('id', 'in', pids)], ['id', 'product_tmpl_id'])
                if p.get('product_tmpl_id')
            }
        cumul = {}
        for g in groupes:
            tid = tmpl_par_variante.get(g['product_id'][0] if g.get('product_id') else None)
            if not tid:
                continue
            e = cumul.setdefault(tid, [0.0, 0.0])
            e[0] += g.get('product_qty') or 0.0
            e[1] += g.get('price_subtotal') or 0.0
        return {tid: montant / qte for tid, (qte, montant) in cumul.items()
                if qte and montant > 0}

    @http.route('/mavie/api/valorisation-detail', type='json', auth='user',
                methods=['POST'], csrf=False)
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
            if self._filtre_produit_actif(kw):
                product_tmpl_ids = request.env['product.template'].sudo().with_context(
                    active_test=False).search(
                    self._build_product_domain(kw)
                ).ids
                if not product_tmpl_ids:
                    product_tmpl_ids = [-1]

            quant_domain = [
                ('location_id.usage', '=', 'internal'),
                ('company_id', '=', company.id),
            ]
            # A13 (2026-09-24) : le pop-up ignorait le filtre magasin de la
            # barre du haut et affichait toute la société. Cliquer une ligne
            # en ayant choisi un magasin donnait donc un total qui ne
            # correspondait pas à l'écran.
            scope = self._get_shop_scope(kw.get('shop_field'))
            if scope and scope.get('warehouse') and scope['warehouse'].lot_stock_id:
                quant_domain.append(
                    ('location_id', 'child_of', scope['warehouse'].lot_stock_id.id))
            # Même règle que la table principale : on compte comme Odoo
            # (sachets, articles archivés et stocks négatifs compris).
            if product_tmpl_ids is not None:
                variant_ids = request.env['product.product'].sudo().search(
                    [('product_tmpl_id', 'in', product_tmpl_ids)]
                ).ids
                quant_domain.append(('product_id', 'in', variant_ids))

            grouped = self._group_sums(
                'stock.quant', quant_domain, ['quantity'],
                group_fields=('product_id', 'location_id'), ctx={'active_test': False},
            )
            autres = {'qty': 0, 'valeur_ht': 0.0, 'valeur_cost': 0.0}

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
                    p['id']: p for p in request.env['product.product'].sudo().with_context(
                        active_test=False).search_read(
                        [('id', 'in', pids)], ['id', 'list_price', 'standard_price', 'product_tmpl_id'])
                }

            # Repli sur le prix d'achat moyen réellement payé quand le champ
            # "Coût" est vide — même règle que la valorisation principale.
            tmpl_ids_needed = {
                p['product_tmpl_id'][0] for p in prod_map.values()
                if p.get('product_tmpl_id') and not p.get('standard_price')
            }
            prix_achat_moyen = self._prix_achat_moyen_par_reference(tmpl_ids_needed, kw)

            cost_estime = False
            qty_vue_total = 0.0
            qty_vue_avec_cout = 0.0
            by_warehouse = {}
            for g in grouped:
                pid = g['product_id'][0] if g.get('product_id') else None
                loc_id = g['location_id'][0] if g.get('location_id') else None
                qty = g.get('quantity') or 0.0
                if not qty or pid not in prod_map:
                    continue
                wh = wh_by_location.get(loc_id)
                if not wh:
                    # Emplacement hors entrepôt suivi (ex. « MMV/MAGASIN
                    # MARINA AGAD - VETEMENTS », −1 628) : gardé à part, sinon
                    # le détail ne retombe pas sur le total de la société.
                    autres['qty'] += int(qty)
                    p_data = prod_map[pid]
                    autres['valeur_ht'] += qty * (p_data.get('list_price') or 0.0)
                    autres['valeur_cost'] += qty * (p_data.get('standard_price') or 0.0)
                    continue
                p_data = prod_map[pid]
                cost_price = p_data.get('standard_price') or 0.0
                # Part des pièces dont le coût est VRAIMENT saisi : la carte
                # et le tableau du dessus masquent le montant quand elle est
                # nulle, ce pop-up doit dire la même chose.
                qty_vue_total += qty
                if cost_price:
                    qty_vue_avec_cout += qty
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
            if autres['qty'] or autres['valeur_ht']:
                magasins.append({'name': 'Autres emplacements',
                                 'qty': autres['qty'],
                                 'valeur_ht': round(autres['valeur_ht'], 2),
                                 'valeur_cost': round(autres['valeur_cost'], 2)})
            magasins.sort(key=lambda m: -m['valeur_ht'])

            return {
                'company_name': company.name,
                'magasins': magasins,
                'total_qty': sum(m['qty'] for m in magasins),
                'total_ht': round(sum(m['valeur_ht'] for m in magasins), 2),
                'total_cost': round(sum(m['valeur_cost'] for m in magasins), 2),
                'cost_estime': cost_estime,
                'cost_couverture': (round(qty_vue_avec_cout / qty_vue_total * 100, 1)
                                    if qty_vue_total else 0.0),
                'cost_disponible': bool(
                    qty_vue_total and
                    qty_vue_avec_cout / qty_vue_total * 100 >= COUT_COUVERTURE_MIN),
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

        if self._filtre_produit_actif(kw):
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
        depot = self._societe_depot()
        achetes_depot = set()
        if depot:
            request.env.cr.execute("""
                SELECT DISTINCT pp.product_tmpl_id
                  FROM purchase_order_line pol
                  JOIN purchase_order po ON po.id = pol.order_id
                  JOIN product_product pp ON pp.id = pol.product_id
                 WHERE po.company_id = %s AND po.state IN ('purchase', 'done')
            """, (depot.id,))
            achetes_depot = {r[0] for r in request.env.cr.fetchall()}
        tmpl_data = request.env['product.template'].sudo().with_context(active_test=False).search_read(
            [('id', 'in', tmpl_ids)], ['id', 'name', 'default_code', 'base_pivot_reference', 'active']
        )
        tmpl_map = {t['id']: t for t in tmpl_data}

        anomalies = []
        for entry in grouped.values():
            t = tmpl_map.get(entry['id'], {})
            # Article archivé : hors des écarts, comme demandé.
            if t.get('active') is False:
                continue
            entry['name'] = t.get('name') or '—'
            entry['archive'] = False
            entry['achat_depot'] = entry['id'] in achetes_depot
            entry['ref'] = (t.get('base_pivot_reference') or t.get('default_code')
                            or t.get('name') or '—')
            entry['qty_negative'] = int(entry['qty_negative'])
            entry['variants'].sort(key=lambda v: v['qty'])
            anomalies.append(entry)

        anomalies.sort(key=lambda a: a['qty_negative'])
        return {
            'anomalies': anomalies[:limit],
            'count': len(anomalies),
            'refs_count': len({a['id'] for a in anomalies}),
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
            # Chaque ligne de stock négative : le mouvement qui l'a fait passer
            # sous zéro, en rejouant l'historique de cet emplacement et de ce lot.
            lignes_negatives = []
            negatifs = request.env['stock.quant'].sudo().search([
                ('product_id', 'in', variants.ids),
                ('location_id', 'in', list(inside_ids)),
                ('quantity', '<', 0),
            ])
            for q in negatifs:
                mouvements = MoveLine.search([
                    ('state', '=', 'done'),
                    ('product_id', '=', q.product_id.id),
                    '|', ('location_id', '=', q.location_id.id), ('location_dest_id', '=', q.location_id.id),
                ], order='date asc, id asc')
                if q.lot_id:
                    mouvements = mouvements.filtered(lambda ml: ml.lot_id == q.lot_id)
                solde = 0.0
                origine = None
                ventes = []
                for ml in mouvements:
                    if ml.location_dest_id == q.location_id and ml.location_id != q.location_id:
                        solde += ml.quantity
                        sens_ml = 'in'
                    elif ml.location_id == q.location_id and ml.location_dest_id != q.location_id:
                        solde -= ml.quantity
                        sens_ml = 'out'
                    else:
                        continue
                    if solde < 0 and (origine is None or origine['solde_apres'] >= 0):
                        categorie = self._classify_move_line(ml, inside_ids, sens_ml)
                        contrepartie = ml.location_id if sens_ml == 'in' else ml.location_dest_id
                        origine = {
                            'date': str(ml.date)[:16] if ml.date else '—',
                            'document': ml.reference or (ml.picking_id.name if ml.picking_id else '') or (
                                ml.move_id.origin if ml.move_id else '') or '—',
                            'nature': self._LEDGER_LABELS.get(categorie, categorie),
                            'sens': 'Entrée' if sens_ml == 'in' else 'Sortie',
                            'qty': int(ml.quantity),
                            'contrepartie': contrepartie.complete_name if contrepartie else '—',
                            'picking_id': ml.picking_id.id if ml.picking_id else False,
                            'solde_apres': solde,
                        }
                    if solde < 0 and sens_ml == 'out':
                        pick = ml.picking_id
                        pos = pick.pos_order_id.sudo() if pick and pick.pos_order_id else request.env['pos.order'].sudo()
                        ventes.append({
                            'date': fields.Datetime.to_string(pos.date_order if pos else ml.date),
                            'magasin': q.location_id.warehouse_id.name or q.location_id.complete_name,
                            'document': ml.reference or (pick.name if pick else '') or '—',
                            'picking_id': pick.id if pick else False,
                            'qty': int(ml.quantity),
                            'solde_apres': solde,
                            'caisse': pos.session_id.config_id.name if pos else 'Hors caisse : aucune commande de caisse liée',
                            'vendeur': pos.user_id.name if pos and pos.user_id else 'Hors caisse',
                            'utilisateur': ml.create_uid.name or '—',
                            'move_line_id': ml.id,
                            'origine_saisie': (pick.origin or '') if pick else '',
                        })
                    elif solde >= 0:
                        origine = None
                lignes_negatives.append({
                    'variante': q.product_id.display_name or '—',
                    'emplacement': q.location_id.complete_name or '—',
                    'lot': q.lot_id.name if q.lot_id else '—',
                    'quantite': int(q.quantity),
                    'origine': origine,
                    'ventes': ventes[:30],
                })
            lignes_negatives.sort(key=lambda x: x['quantite'])
            quants_positifs = request.env['stock.quant'].sudo().search([
                ('product_id', 'in', variants.ids),
                ('location_id', 'in', list(inside_ids)),
                ('quantity', '>', 0),
            ])
            stock_positif = int(sum(quants_positifs.mapped('quantity')))

            return {
                'ref': product_tmpl.base_pivot_reference or product_tmpl.default_code or product_tmpl.name,
                'name': product_tmpl.name,
                'magasin': warehouse.name,
                'societe': warehouse.company_id.name if warehouse.company_id else '—',
                'entrees': entrees,
                'lignes_negatives': lignes_negatives,
                'stock_positif': stock_positif,
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
        domain = []
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
        state_labels = {
            'draft': 'Brouillon',
            'submitted': 'En attente de validation',
            'transmitted': "Transmis à l'inventaire",
            'done': 'Fait',
            'cancelled': 'Annulé',
        }
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
                # Où le bon se trouve maintenant : pour un transfert
                # intra-société, il a été transmis à l'Inventaire et c'est
                # cette opération-là que le responsable doit valider — sans
                # son nom, l'historique dit « transmis » sans dire à quoi.
                'picking_name': tr.picking_id.name or '',
                # Permet d'ouvrir l'opération dans Odoo depuis l'historique.
                'picking_id': tr.picking_id.id or None,
                'nb_references': len(refs),
                'nb_lignes': len(tr.line_ids),
                'qty': int(qty),
                'group_ref': tr.group_ref or '',
            })
        return rows

    # Condition SQL « vendu sous le prix catalogue » — partagée par la liste
    # et par le comptage, pour que les deux parlent bien de la même chose.
    SOLDE_SQL_WHERE = """
              AND pt.list_price > 0
              AND pol.price_unit * (1 - COALESCE(pol.discount, 0) / 100.0) < pt.list_price * 0.999
    """

    def _solde_history_totaux(self, kw):
        """Vrai total des ventes en solde, sans la limite d'affichage.

        A11 (2026-09-24) : la carte affichait 500, c'est-à-dire la limite de
        la liste, comme si c'était le nombre total de ventes soldées.
        """
        pos_domain = self._build_pos_domain(kw, None)
        line_ids = request.env['pos.order.line'].sudo().search(pos_domain).ids
        if not line_ids:
            return {'count': 0, 'qty': 0, 'ca': 0.0}
        request.env.cr.execute("""
            SELECT COUNT(*), COALESCE(SUM(pol.qty), 0), COALESCE(SUM(pol.price_subtotal_incl), 0)
              FROM pos_order_line pol
              JOIN product_product pp ON pp.id = pol.product_id
              JOIN product_template pt ON pt.id = pp.product_tmpl_id
             WHERE pol.id IN %s
        """ + self.SOLDE_SQL_WHERE, (tuple(line_ids),))
        n, qty, ca = request.env.cr.fetchone()
        return {'count': int(n or 0), 'qty': int(qty or 0), 'ca': round(float(ca or 0.0), 2)}

    def _solde_posees_rows(self, kw):
        """Les remises posées, dès leur pose, même sans vente encore."""
        rows = []
        for r in self.api_soldes_journal().get('rows') or []:
            pose = (r.get('pose_le') or '').strip()
            jour = pose[:10]
            if not jour:
                continue
            if kw.get('date_start') and jour < kw['date_start']:
                continue
            if kw.get('date_end') and jour > kw['date_end']:
                continue
            rows.append({
                'id': r.get('article_id'),
                'date': pose[:16],
                'ticket': 'Remise posée',
                'magasin': r.get('magasin') or '—',
                'ref': r.get('reference') or '—',
                'name': r.get('produit') or '—',
                'qty': 0,
                'prix_catalogue': round(float(r.get('prix_catalogue') or 0), 2),
                'prix_paye': round(float(r.get('prix_solde') or 0), 2),
                'remise_pct': round(float(r.get('remise') or 0), 1),
                'ca': 0.0,
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
            soldes = sorted(self._solde_history_rows(kw) + self._solde_posees_rows(kw),
                            key=lambda x: x['date'], reverse=True)[:500]
            # A11 : les totaux viennent du comptage complet, la liste reste
            # limitée aux 500 ventes les plus récentes (affichage).
            totaux = self._solde_history_totaux(kw)
            return {
                'transfers': transfers,
                'transfers_count': len(transfers),
                'transfers_qty': sum(t['qty'] for t in transfers),
                'soldes': soldes,
                'soldes_count': totaux['count'],
                'soldes_qty': totaux['qty'],
                'soldes_ca': totaux['ca'],
                'soldes_affiches': len(soldes),
                'soldes_tronque': totaux['count'] > len(soldes),
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
                writer.writerow(['Bon', 'Date', 'État', 'Opération', 'Société source',
                                 'Magasin source', 'Société cible', 'Magasin cible',
                                 'Type', 'Références', 'Lignes', 'Qté totale', 'Groupe'])
                for row in self._transfer_history_rows(kw):
                    writer.writerow([
                        row['name'], row['date'], row['state_label'], row['picking_name'],
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
                'transmitted': "Transmis à l'inventaire",
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
                        # Opération d'inventaire à collecter, pour un
                        # transfert intra-société transmis à l'Inventaire.
                        'picking_name': transfer.picking_id.name or '',
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
                'soldes_programmees': self._solde_history(product_tmpl, variants),
            }
        except Exception as e:
            _logger.error(f"Erreur api_product_history: {str(e)}", exc_info=True)
            return {'error': str(e), 'transfers': [], 'soldes': []}

    # ─────────────────────────────────────────────────────────────
    # RÉASSORT — proposition d'envoi du dépôt MOD FOR LIFE vers les magasins
    #
    # DEMANDE UTILISATEUR (2026-09-18) : savoir à quel magasin envoyer un
    # article, d'après ce qu'il a reçu, ce qu'il a vendu, depuis combien de
    # temps, et ce que le dépôt a réellement en stock. Règle métier de
    # l'utilisateur : alerter quand il ne reste plus que 10 % de ce qu'un
    # magasin a reçu. Source des envois : le dépôt MOD FOR LIFE UNIQUEMENT
    # (les transferts magasin -> magasin ont déjà leur propre écran).
    #
    # Montage validé avec l'utilisateur :
    #   • alerte principale = sa règle des 10 % (stock <= 10 % du reçu) ;
    #   • une colonne « jours restants » (stock ÷ vitesse de vente) pour
    #     trier ces alertes par urgence réelle ;
    #   • une alerte secondaire « vend vite » : il reste plus de 10 %, mais
    #     au rythme actuel ça ne tiendra pas le délai de réappro. Sans elle,
    #     ce sont les meilleures ventes qui tombent en rupture sans prévenir ;
    #   • une quantité proposée calculée sur la vitesse, plafonnée par ce que
    #     le dépôt possède, et répartie au prorata de la vitesse quand le
    #     dépôt n'en a pas assez pour tout le monde.
    #
    # Grain = (magasin x VARIANTE) : couleur ET taille. Une référence « en
    # stock » peut être morte en magasin parce qu'il ne reste que du 41.
    # ─────────────────────────────────────────────────────────────

    REASSORT_MAX_ROWS = 3000

    def _reassort_params(self, kw):
        """Paramètres réglables à l'écran, bornés pour rester raisonnables."""
        def _int(name, default, lo, hi):
            try:
                v = int(kw.get(name) or default)
            except (TypeError, ValueError):
                v = default
            return max(lo, min(hi, v))
        return {
            'fenetre': _int('fenetre', 90, 7, 365),
            'seuil_pct': _int('seuil_pct', 10, 1, 90),
            'delai': _int('delai', 21, 1, 180),
            'cible': _int('cible', 30, 1, 365),
        }

    def _reassort_warehouses(self, kw):
        """Magasins physiques retenus : filtre magasin, sinon sociétés
        cochées dans le sélecteur Odoo, sinon tout le réseau actif. MOD FOR
        LIFE et PAIE n'en font jamais partie : ce ne sont pas des magasins."""
        non_retail = self._get_non_retail_company_ids()
        mappings = self._get_active_shop_mappings().filtered(
            lambda m: m.warehouse_id and m.warehouse_id.company_id.id not in non_retail
        )
        if kw.get('shop_field'):
            scope = self._get_shop_scope(kw['shop_field'])
            if scope and scope.get('warehouse'):
                wh = scope['warehouse']
                return wh, {wh.id: scope.get('label') or wh.name}
        warehouses = mappings.mapped('warehouse_id')
        context_ids = [c for c in self._get_context_company_ids() if c not in non_retail]
        if context_ids:
            filtered = warehouses.filtered(lambda w: w.company_id.id in context_ids)
            if filtered:
                warehouses = filtered
        labels = {}
        for m in mappings:
            if m.warehouse_id.id in warehouses.ids:
                labels[m.warehouse_id.id] = m.warehouse_id.name
        return warehouses, labels

    def _reassort_reference_date(self, warehouse_ids):
        """Date de référence de la fenêtre de vente.

        Calée sur la DERNIÈRE VENTE PRÉSENTE EN BASE (bornée à aujourd'hui),
        jamais aveuglément sur la date du jour. Mesuré sur la base de test :
        les ventes caisse s'arrêtent au 2026-08-01 ; une fenêtre « 90
        derniers jours » glissante n'y trouvait que 93 lignes et toutes les
        vitesses tombaient à 0 — écran vide. En production, alimentée au
        jour le jour, les deux dates se confondent.
        """
        request.env.cr.execute("""
            SELECT MAX(po.date_order)::date
              FROM pos_order po
              JOIN pos_session ps ON ps.id = po.session_id
              JOIN pos_config pc ON pc.id = ps.config_id
              JOIN stock_picking_type spt ON spt.id = pc.picking_type_id
             WHERE po.state IN ('paid', 'done', 'invoiced')
               AND spt.warehouse_id = ANY(%(wh)s)
        """, {'wh': list(warehouse_ids)})
        last = request.env.cr.fetchone()[0]
        today = date.today()
        if not last or last > today:
            return today
        return last

    # ─────────────────────────────────────────────────────────────
    # ACTIONS FAITES SUR UNE RÉFÉRENCE (2026-09-22)
    #
    # Demande utilisatrice : voir à côté de la référence (page Action) ce
    # qu'elle a déjà eu comme action, et dans la fiche produit colorer le
    # stock de chaque magasin / couleur : bleu = transféré, rouge = soldé,
    # vert = réassort. Choix : « réassort » = transfert lancé depuis la
    # fenêtre Réassort (marqué à la création, origin REASSORT_ORIGINE), pas
    # une simple proposition.
    #   - Transfert : bon inter.internal.transfer non brouillon portant
    #     l'article (le magasin compte comme émetteur OU récepteur).
    #   - Solde : règle de prix ACTIVE (liste active, pas encore finie)
    #     posée sur l'article ou une de ses variantes, dans une liste
    #     rattachée à la caisse du magasin. Les remises globales ou par
    #     catégorie (ex. « REMISE 20% » sur tout) ne comptent pas : ce ne
    #     sont pas des soldes de CET article.
    # ─────────────────────────────────────────────────────────────

    REASSORT_ORIGINE = 'Réassort (dashboard)'

    def _actions_references(self, tmpl_ids):
        """{tmpl_id: {'transfert': {ids}, 'reassort': {ids}, 'solde': {wh},
        'wh': {wh_id: {actions}}, 'couleur': {couleur: {actions}}}}"""
        cr = request.env.cr
        out = {}
        tmpl_ids = [t for t in tmpl_ids if t]
        if not tmpl_ids:
            return out

        def acc(tid):
            return out.setdefault(tid, {'transfert': set(), 'reassort': set(), 'solde': set(),
                                        'wh': {}, 'couleur': {}})

        cr.execute("""
            SELECT pp.product_tmpl_id, pp.id, t.id,
                   COALESCE(t.origin, '') = %(orig)s,
                   ls.warehouse_id, ld.warehouse_id
              FROM inter_internal_transfer_line l
              JOIN inter_internal_transfer t ON t.id = l.transfer_id
              JOIN product_product pp ON pp.id = l.product_id
              LEFT JOIN stock_location ls ON ls.id = t.location_source_id
              LEFT JOIN stock_location ld ON ld.id = t.location_target_id
             WHERE pp.product_tmpl_id = ANY(%(tmpls)s)
               AND COALESCE(t.state, 'draft') != 'draft'
        """, {'tmpls': tmpl_ids, 'orig': self.REASSORT_ORIGINE})
        lignes = cr.fetchall()

        Item = request.env['product.pricelist.item'].sudo()
        now = fields.Datetime.now()
        items = Item.search([
            ('pricelist_id.active', '=', True),
            '|', ('date_end', '=', False), ('date_end', '>=', now),
            '|',
            '&', ('applied_on', '=', '1_product'), ('product_tmpl_id', 'in', tmpl_ids),
            '&', ('applied_on', '=', '0_product_variant'), ('product_id.product_tmpl_id', 'in', tmpl_ids),
        ])
        wh_par_liste = {}
        if items:
            listes = items.mapped('pricelist_id')
            configs = request.env['pos.config'].sudo().search([
                '|', ('available_pricelist_ids', 'in', listes.ids), ('pricelist_id', 'in', listes.ids)])
            for cfg in configs:
                wh = cfg.picking_type_id.warehouse_id.id
                if not wh:
                    continue
                for pl in (cfg.available_pricelist_ids | cfg.pricelist_id) & listes:
                    wh_par_liste.setdefault(pl.id, set()).add(wh)

        pids = {r[1] for r in lignes} | set(items.mapped('product_id').ids)
        couleurs = {}
        if pids:
            cr.execute("""
                SELECT pvc.product_product_id, MAX(pav.name->>'en_US')
                  FROM product_variant_combination pvc
                  JOIN product_template_attribute_value ptav
                    ON ptav.id = pvc.product_template_attribute_value_id
                  JOIN product_attribute pa ON pa.id = ptav.attribute_id
                  JOIN product_attribute_value pav ON pav.id = ptav.product_attribute_value_id
                 WHERE pvc.product_product_id = ANY(%s)
                   AND UPPER(pa.name->>'en_US') LIKE 'COULEUR%%'
                 GROUP BY 1
            """, (list(pids),))
            couleurs = {pid: (c or '').strip() for pid, c in cr.fetchall()}

        for tid, pid, trid, est_reassort, wh_src, wh_dst in lignes:
            a = acc(tid)
            action = 'reassort' if est_reassort else 'transfert'
            a[action].add(trid)
            for wh in (wh_src, wh_dst):
                if wh:
                    a['wh'].setdefault(wh, set()).add(action)
            c = couleurs.get(pid)
            if c:
                a['couleur'].setdefault(c, set()).add(action)

        for it in items:
            tid = it.product_tmpl_id.id or it.product_id.product_tmpl_id.id
            a = acc(tid)
            whs = wh_par_liste.get(it.pricelist_id.id, set())
            if not whs:
                continue
            a['solde'] |= whs
            for wh in whs:
                a['wh'].setdefault(wh, set()).add('solde')
            if it.applied_on == '0_product_variant':
                c = couleurs.get(it.product_id.id)
                if c:
                    a['couleur'].setdefault(c, set()).add('solde')
            else:
                a['couleur'].setdefault('*', set()).add('solde')
        return out

    def _actions_detail(self, tmpl_id):
        """Pour la fiche produit : actions par magasin (clé shop_field, comme
        les lignes du tableau des magasins) et par couleur. '*' = solde posée
        sur l'article entier, donc valable pour toutes les couleurs."""
        a = self._actions_references([tmpl_id]).get(tmpl_id)
        if not a:
            return {'magasins': {}, 'couleurs': {}}
        par_wh = {m.warehouse_id.id: m.shop_field for m in self._get_active_shop_mappings() if m.warehouse_id}
        magasins = {}
        for wh, acts in a['wh'].items():
            field = par_wh.get(wh)
            if field:
                magasins.setdefault(field, set()).update(acts)
        return {
            'magasins': {k: sorted(v) for k, v in magasins.items()},
            'couleurs': {k: sorted(v) for k, v in a['couleur'].items()},
        }

    def _actions_resume(self, a):
        """Résumé pour la page Action : nombre de bons / de magasins."""
        if not a:
            return {'transfert': 0, 'reassort': 0, 'solde': 0}
        return {'transfert': len(a['transfert']), 'reassort': len(a['reassort']), 'solde': len(a['solde'])}

    # ─────────────────────────────────────────────────────────────
    # PAGE « ACTION » (menu Dashboard → Action)
    #
    # DEMANDE UTILISATEUR (2026-09-22) : une page à côté de « Stock &
    # ruptures » avec un grand tableau reprenant les informations du
    # Top/Flop Produits, trié du top au flop, une ligne par référence et
    # ses couleurs dessous ; colonnes Catégorie et Prix après la référence,
    # sans la colonne Produit ; Qté en dépôt puis trois boutons à la fin
    # (Transférer, Prix, Réassort). En haut : nombre de lignes à afficher,
    # choix des colonnes, et une barre de recherche avec Filtres /
    # Regrouper par / Favoris.
    #
    # Les chiffres sont calculés avec EXACTEMENT les mêmes domaines que le
    # Top/Flop (_build_pos_domain, _build_purchase_domain, même périmètre
    # de stock) : une référence doit afficher les mêmes valeurs sur les deux
    # écrans. « Dépôt » = stock interne de MOD FOR LIFE, comme le réassort.
    # ─────────────────────────────────────────────────────────────

    ACTION_FILTRES = {
        'vendus': lambda t: t['qty_sold'] > 0,
        'non_vendus': lambda t: t['qty_sold'] <= 0,
        'rupture': lambda t: t['stock'] <= 0,
        'en_stock': lambda t: t['stock'] > 0,
        'depot': lambda t: t['depot'] > 0,
        'depot_vide': lambda t: t['depot'] <= 0,
        'reste_negatif': lambda t: t['qty_purchased'] - t['qty_sold'] < 0,
    }

    def _action_quant_domain(self, kw, product_tmpl_ids):
        """Domaine stock.quant du Top/Flop (copie fidèle du bloc de
        _compute_kpis) : magasins actifs, société / magasin choisis,
        sachets exclus. Gardé à part pour ne pas toucher au calcul
        principal ; toute évolution de l'un doit être reportée dans l'autre."""
        domain = [('location_id.usage', '=', 'internal')]
        domain += self._sachet_exclude_domain('product_id.product_tmpl_id.collection_id')
        shop_field = kw.get('shop_field')
        shop_scope = self._get_shop_scope(shop_field)
        if shop_field:
            if shop_scope:
                if shop_scope['company_id']:
                    domain.append(('company_id', '=', shop_scope['company_id']))
                if shop_scope['warehouse'] and shop_scope['warehouse'].lot_stock_id:
                    domain.append(('location_id', 'child_of', shop_scope['warehouse'].lot_stock_id.id))
        else:
            context_company_ids = self._get_context_company_ids()
            if context_company_ids:
                domain.append(('company_id', 'in', context_company_ids))
        if product_tmpl_ids is not None:
            variants = request.env['product.product'].sudo().with_context(active_test=False).search(
                [('product_tmpl_id', 'in', product_tmpl_ids)])
            domain.append(('product_id', 'in', variants.ids))

        Warehouse = request.env['stock.warehouse'].sudo()
        excluded_non_retail_ids = self._get_excluded_non_retail_ids(kw)
        scoped = Warehouse.search([
            ('company_id', 'not in', self._get_non_retail_company_ids()),
            ('id', 'in', self._get_active_shop_mappings().mapped('warehouse_id').ids),
        ])
        explicit = [cid for cid in self._get_non_retail_company_ids()
                    if cid not in excluded_non_retail_ids]
        if explicit:
            scoped |= Warehouse.search([('company_id', 'in', explicit)])
        if shop_scope and shop_scope['kind'] == 'online' and shop_scope['warehouse']:
            scoped |= shop_scope['warehouse']
        lot_ids = scoped.mapped('lot_stock_id').ids
        if excluded_non_retail_ids:
            domain.append(('company_id', 'not in', excluded_non_retail_ids))
        if lot_ids:
            domain.append(('location_id', 'child_of', lot_ids))
        return domain

    # Régions : regroupement de villes, propre au dashboard (aucun champ
    # « région » n'existe sur les magasins — vérifié en base le 2026-09-23).
    ACTION_REGIONS = {
        'Grand Casablanca': ['Casablanca', 'Mohammadia'],
        'Rabat-Salé': ['Rabat', 'Témara'],
        'Souss (Agadir)': ['Agadir'],
        'Nord (Tanger)': ['Tanger'],
    }

    def _action_magasins_filtres(self, kw):
        """Magasins retenus par les filtres ville / région / magasin."""
        mappings = self._get_active_shop_mappings()
        villes = [v for v in (kw.get('villes') or []) if v]
        regions = [r for r in (kw.get('regions') or []) if r]
        champs = [m for m in (kw.get('magasins') or []) if m]
        for region in regions:
            villes += self.ACTION_REGIONS.get(region, [])
        if not villes and not champs:
            return {'actif': False, 'config_ids': [], 'wh_ids': [], 'lot_ids': [], 'libelle': ''}
        choisis = mappings.filtered(
            lambda m: (m.shop_field in champs) or ((m.city or '').strip() in villes))
        configs = request.env['pos.config'].sudo().browse()
        for m in choisis:
            configs |= self._solde_store_configs(m)
        libelle = ', '.join(regions + [v for v in villes if v not in sum(
            [self.ACTION_REGIONS.get(r, []) for r in regions], [])]
            + [m.shop_label or m.warehouse_id.name for m in choisis if m.shop_field in champs])
        return {
            'actif': True,
            'config_ids': configs.ids,
            'wh_ids': choisis.mapped('warehouse_id').ids,
            'lot_ids': choisis.mapped('warehouse_id.lot_stock_id').ids,
            'libelle': libelle,
        }

    def _action_lieux(self):
        """Villes, régions et magasins proposés dans la barre de recherche."""
        mappings = self._get_active_shop_mappings()
        villes = sorted({(m.city or '').strip() for m in mappings if (m.city or '').strip()})
        return {
            'regions': [r for r, v in self.ACTION_REGIONS.items()
                        if any(ville in villes for ville in v)],
            'villes': villes,
            'magasins': [{'shop_field': m.shop_field,
                          'nom': m.warehouse_id.name or m.shop_label or m.shop_field,
                          'ville': (m.city or '').strip()}
                         for m in mappings.sorted(lambda m: m.warehouse_id.name or '')],
        }

    def _transferts_references(self, tmpl_ids, wh_ids=None):
        """Pièces reçues et envoyées par transfert, par référence.

        DEMANDE UTILISATRICE (2026-09-23) : une colonne « Transferts » dans
        le tableau Action, avec le reçu et l'envoyé, et l'historique complet
        au clic. Quand un magasin (ou une ville / région) est filtré, on ne
        compte que ses entrées et sorties ; sans filtre, on compte tout le
        réseau — reçu et envoyé sont alors égaux, puisque chaque bon a un
        expéditeur et un destinataire.
        """
        tmpl_ids = [t for t in tmpl_ids if t]
        if not tmpl_ids:
            return {}
        params = {'tmpls': tmpl_ids}
        filtre_wh = ''
        if wh_ids:
            filtre_wh = ' AND (ls.warehouse_id = ANY(%(wh)s) OR ld.warehouse_id = ANY(%(wh)s))'
            params['wh'] = list(wh_ids)
        request.env.cr.execute("""
            SELECT pp.product_tmpl_id, ls.warehouse_id, ld.warehouse_id, SUM(l.quantity)
              FROM inter_internal_transfer_line l
              JOIN inter_internal_transfer t ON t.id = l.transfer_id
              JOIN product_product pp ON pp.id = l.product_id
              LEFT JOIN stock_location ls ON ls.id = t.location_source_id
              LEFT JOIN stock_location ld ON ld.id = t.location_target_id
             WHERE pp.product_tmpl_id = ANY(%(tmpls)s)
               AND COALESCE(t.state, 'draft') != 'draft'
               {WH}
             GROUP BY 1, 2, 3
        """.replace('{WH}', filtre_wh), params)
        scope = set(wh_ids or ())
        out = {}
        for tid, wh_src, wh_dst, qte in request.env.cr.fetchall():
            d = out.setdefault(tid, {'recu': 0, 'envoye': 0, 'pieces': 0,
                                     'magasins_recu': set(), 'magasins_envoye': set(),
                                     'scope': bool(scope)})
            qte = int(round(qte or 0))
            d['pieces'] += qte
            if wh_dst:
                d['magasins_recu'].add(wh_dst)
            if wh_src:
                d['magasins_envoye'].add(wh_src)
            # CORRIGÉ (2026-09-24) : sans magasin filtré, reçu et envoyé
            # étaient forcément égaux — tout bon a un départ ET une arrivée
            # dans le réseau (vérifié : 3 580 lignes, aucune hors réseau).
            # On ne les calcule donc que dans un périmètre choisi.
            if scope:
                if wh_dst and wh_dst in scope:
                    d['recu'] += qte
                if wh_src and wh_src in scope:
                    d['envoye'] += qte
        for d in out.values():
            d['nb_magasins_recu'] = len(d.pop('magasins_recu'))
            d['nb_magasins_envoye'] = len(d.pop('magasins_envoye'))
        return out

    @http.route('/mavie/api/transferts-reference', type='json', auth='user', methods=['POST'], csrf=False)
    def api_transferts_reference(self, **kw):
        """Historique complet des transferts d'une référence : un bon par
        ligne, avec date, magasins, quantité, état et document Odoo."""
        try:
            tmpl = request.env['product.template'].sudo().browse(int(kw.get('article_id') or 0))
            if not tmpl.exists():
                return {'error': 'Référence introuvable.'}
            etats = {'draft': 'Brouillon', 'submitted': 'En attente de validation',
                     'transmitted': "Transmis à l'inventaire", 'done': 'Fait'}
            request.env.cr.execute("""
                SELECT t.id, t.name, t.state, t.create_date, COALESCE(t.origin, '') = %(orig)s,
                       ws.name, wd.name, SUM(l.quantity), po.name, pk.name,
                       cs.name, ct.name
                  FROM inter_internal_transfer_line l
                  JOIN inter_internal_transfer t ON t.id = l.transfer_id
                  JOIN product_product pp ON pp.id = l.product_id
                  LEFT JOIN stock_location ls ON ls.id = t.location_source_id
                  LEFT JOIN stock_location ld ON ld.id = t.location_target_id
                  LEFT JOIN stock_warehouse ws ON ws.id = ls.warehouse_id
                  LEFT JOIN stock_warehouse wd ON wd.id = ld.warehouse_id
                  LEFT JOIN purchase_order po ON po.id = t.purchase_id
                  LEFT JOIN stock_picking pk ON pk.id = t.picking_id
                  LEFT JOIN res_company cs ON cs.id = t.company_source_id
                  LEFT JOIN res_company ct ON ct.id = t.company_target_id
                 WHERE pp.product_tmpl_id = %(tmpl)s
                 GROUP BY t.id, t.name, t.state, t.create_date, t.origin,
                          ws.name, wd.name, po.name, pk.name, cs.name, ct.name
                 ORDER BY t.create_date DESC
                 LIMIT 300
            """, {'tmpl': tmpl.id, 'orig': self.REASSORT_ORIGINE})
            lignes = []
            for (tid, nom, etat, date, est_reassort, src, dst, qte, po, pk,
                 soc_src, soc_dst) in request.env.cr.fetchall():
                lignes.append({
                    'id': tid,
                    'bon': (nom if nom and nom != 'New' else None) or po or pk or 'sans numéro',
                    'date': str(date)[:10] if date else '',
                    'source': src or '—',
                    'dest': dst or '—',
                    'societes': ('%s → %s' % (soc_src or '—', soc_dst or '—')),
                    'qty': int(round(qte or 0)),
                    'etat': etats.get(etat, etat or ''),
                    'fait': etat == 'done',
                    # Un bon en brouillon n'a rien déplacé : la colonne
                    # Transferts de la page Action ne le compte pas, le
                    # pop-up doit donc le distinguer au lieu de le mélanger
                    # aux autres (écart constaté le 2026-09-26 : 6 pièces
                    # dans le tableau, 42 dans le pop-up).
                    'brouillon': etat == 'draft',
                    'reassort': bool(est_reassort),
                })
            # Ce qui s'est vraiment passé, magasin par magasin : un magasin
            # peut recevoir sans jamais envoyer, et l'inverse (demande
            # utilisatrice 2026-09-24).
            par_magasin = {}
            for l in lignes:
                if l.get('brouillon'):
                    continue   # rien n'a bougé, on ne l'additionne pas
                if l['dest'] and l['dest'] != '—':
                    par_magasin.setdefault(l['dest'], {'magasin': l['dest'], 'recu': 0, 'envoye': 0})['recu'] += l['qty']
                if l['source'] and l['source'] != '—':
                    par_magasin.setdefault(l['source'], {'magasin': l['source'], 'recu': 0, 'envoye': 0})['envoye'] += l['qty']
            magasins = sorted(par_magasin.values(), key=lambda m: -(m['recu'] + m['envoye']))
            for m in magasins:
                m['net'] = m['recu'] - m['envoye']
            return {
                'reference': tmpl.base_pivot_reference or tmpl.default_code or tmpl.name,
                'nom': tmpl.name,
                'lignes': lignes,
                'magasins': magasins,
                # Totaux sur ce qui a RÉELLEMENT bougé, comme la colonne du
                # tableau. Les brouillons sont comptés à part.
                'total_pieces': sum(l['qty'] for l in lignes if not l.get('brouillon')),
                'nb_bons': sum(1 for l in lignes if not l.get('brouillon')),
                'brouillons_nb': sum(1 for l in lignes if l.get('brouillon')),
                'brouillons_pieces': sum(l['qty'] for l in lignes if l.get('brouillon')),
            }
        except Exception as e:
            _logger.error(f"Erreur api_transferts_reference: {str(e)}", exc_info=True)
            return {'error': str(e)}

    @http.route('/mavie/api/actions', type='json', auth='user', methods=['POST'], csrf=False)
    def api_actions(self, **kw):
        try:
            request._mavie_societe_id = self._action_societe_id(kw)
            cle = self._cache_cle('actions', kw)
            garde = self._cache_lire(cle)
            if garde is not None:
                return garde
            return self._cache_ecrire(cle, self._compute_actions(kw))
        except Exception as e:
            _logger.error(f"Erreur api_actions: {str(e)}", exc_info=True)
            return {'error': str(e)}

    def _compute_actions(self, kw):
        cr = request.env.cr
        try:
            limit = max(1, min(int(kw.get('limit') or 20), 500))
        except (TypeError, ValueError):
            limit = 20
        q = (kw.get('q') or '').strip().lower()
        filtres = [f for f in (kw.get('filtres') or []) if f in self.ACTION_FILTRES]

        product_tmpl_ids = None
        if self._filtre_produit_actif(kw):
            product_tmpl_ids = request.env['product.template'].sudo().with_context(
                active_test=False).search(
                self._build_product_domain(kw)).ids
            if not product_tmpl_ids:
                return {'rows': [], 'total': 0, 'limit': limit}

        # Filtres ville / région / magasin de la barre de recherche
        # (demande utilisatrice 2026-09-23) : ils restreignent les ventes,
        # les achats et le stock aux magasins retenus.
        mags = self._action_magasins_filtres(kw)
        pos_dom = self._build_pos_domain(kw, product_tmpl_ids)
        achat_dom = self._build_purchase_domain(kw, product_tmpl_ids)
        quant_dom = self._action_quant_domain(kw, product_tmpl_ids)

        # A02 (2026-09-24) : le dépôt achète au fournisseur PUIS revend aux
        # magasins, qui enregistrent à leur tour une réception. Additionner
        # les deux compte chaque pièce deux fois. Cette page part de toutes
        # les sociétés cochées, le doublon était donc permanent : POLO
        # MANCHES COURTES affichait 2 954 pièces achetées pour 1 477
        # réellement reçues en magasin. On ne garde que les réceptions des
        # sociétés magasin — sauf quand le dépôt est justement la société
        # choisie dans le sélecteur, où c'est bien son achat qu'on regarde.
        depot = self._societe_depot()
        if depot and self._action_societe_id(kw) != depot.id:
            achat_dom = achat_dom + [('order_id.company_id', '!=', depot.id)]
            quant_dom = quant_dom + [('company_id', '!=', depot.id)]
        if mags['actif']:
            pos_dom = pos_dom + [('order_id.session_id.config_id', 'in', mags['config_ids'] or [-1])]
            achat_dom = achat_dom + [('order_id.picking_type_id.warehouse_id', 'in', mags['wh_ids'] or [-1])]
            quant_dom = quant_dom + [('location_id', 'child_of', mags['lot_ids'] or [-1])]

        # ── Par variante : ventes, achats, stock magasins, stock dépôt.
        par_variante = {}

        def v(pid):
            return par_variante.setdefault(pid, {
                'qty_sold': 0.0, 'ca': 0.0, 'qty_purchased': 0.0,
                'ca_achat': 0.0, 'stock': 0.0, 'depot': 0.0})

        for g in self._group_sums('pos.order.line', pos_dom,
                                  ['price_subtotal_incl', 'qty']):
            if g.get('product_id'):
                x = v(g['product_id'][0])
                x['qty_sold'] += g.get('qty') or 0.0
                x['ca'] += g.get('price_subtotal_incl') or 0.0
        # Ventes sur bon de vente des sociétés non retail explicitement
        # cochées : ajoutées au vendu, comme dans le Top/Flop.
        if self._get_explicit_non_retail_ids(kw):
            for g in self._group_sums('sale.order.line',
                                      self._build_non_retail_sale_domain(kw, product_tmpl_ids),
                                      ['product_uom_qty', 'price_total']):
                if g.get('product_id'):
                    x = v(g['product_id'][0])
                    x['qty_sold'] += g.get('product_uom_qty') or 0.0
                    x['ca'] += g.get('price_total') or 0.0
        for g in self._group_sums('purchase.order.line', achat_dom,
                                  ['qty_received', 'price_total']):
            if g.get('product_id'):
                x = v(g['product_id'][0])
                x['qty_purchased'] += g.get('qty_received') or 0.0
                x['ca_achat'] += g.get('price_total') or 0.0
        for g in self._group_sums('stock.quant', quant_dom, ['quantity']):
            if g.get('product_id'):
                v(g['product_id'][0])['stock'] += g.get('quantity') or 0.0

        mfl = self._societe_depot()
        if mfl:
            cr.execute("""
                SELECT sq.product_id, SUM(sq.quantity)
                  FROM stock_quant sq
                  JOIN stock_location sl ON sl.id = sq.location_id
                 WHERE sl.usage = 'internal' AND sl.company_id = %s
                 GROUP BY 1
            """, (mfl.id,))
            sachet = set(self._get_sachet_variant_ids())
            for pid, qte in cr.fetchall():
                if pid in sachet:
                    continue
                # Stock dépôt seul (aucune vente, achat ni stock magasin) :
                # on ne fait pas entrer la référence dans le tableau pour ça.
                if pid in par_variante:
                    par_variante[pid]['depot'] += qte or 0.0

        if not par_variante:
            return {'rows': [], 'total': 0, 'limit': limit}

        # ── Infos article (une requête), puis agrégat par référence.
        cr.execute("""
            SELECT pp.id, pt.id,
                   COALESCE(NULLIF(pt.base_pivot_reference, ''), NULLIF(pt.default_code, ''),
                            pt.name->>'fr_FR', pt.name->>'en_US') AS ref,
                   COALESCE(pt.name->>'fr_FR', pt.name->>'en_US') AS nom,
                   COALESCE(pc.name, '') AS categorie,
                   pt.list_price,
                   pt.collection_id
              FROM product_product pp
              JOIN product_template pt ON pt.id = pp.product_tmpl_id
              LEFT JOIN product_category pc ON pc.id = pt.categ_id
             -- Une référence ARCHIVÉE ne doit jamais apparaître dans le
             -- tableau ni dans le compteur (demande du 2026-09-26), même si
             -- elle garde des ventes ou du stock.
             WHERE pp.id = ANY(%s) AND pt.active
        """, (list(par_variante.keys()),))
        refs = {}
        for pid, tid, ref, nom, categorie, list_price, collection_id in cr.fetchall():
            t = refs.setdefault(tid, {
                'id': tid, 'ref': ref or '—', 'name': nom or '—', 'categorie': categorie,
                'list_price': list_price or 0.0, 'collection_id': collection_id,
                'qty_sold': 0.0, 'ca': 0.0, 'qty_purchased': 0.0, 'ca_achat': 0.0,
                'stock': 0.0, 'depot': 0.0, 'variantes': [],
            })
            t['variantes'].append(pid)
            for k, val in par_variante[pid].items():
                t[k] += val

        # DEMANDE UTILISATRICE (2026-09-26) : la page doit couvrir TOUT le
        # catalogue, comme la carte Références (3 570), et pas seulement les
        # références qui ont déjà vendu, acheté ou du stock (1 234). Celles
        # qui n'ont jamais bougé s'ajoutent ici à zéro ; elles finissent
        # naturellement en bas du classement.
        manquantes = [t for t in self._action_references_sans_activite(
            kw, set(refs.keys())) if t['id'] not in refs]
        for t in manquantes:
            refs[t['id']] = t

        # Du top au flop : chiffre d'affaires vendu, puis quantité vendue,
        # puis stock (une référence sans vente mais avec du stock est un
        # flop plus urgent qu'une référence vide).
        ordre = sorted(refs.values(), key=lambda t: (-t['ca'], -t['qty_sold'], -t['stock'], t['ref']))
        # Niveau (flèche de couleur, demande utilisatrice 2026-09-22) :
        # découpage ABC du chiffre d'affaires, comme l'Analyse ABC du
        # dashboard. Top = les références qui font les 80 premiers % du CA,
        # moyen = jusqu'à 95 %, flop = le reste (dont tout ce qui n'a rien
        # vendu).
        ca_total = sum(max(t['ca'], 0.0) for t in ordre) or 1.0
        cumul = 0.0
        for rang, t in enumerate(ordre, 1):
            t['rang'] = rang
            part_avant = cumul / ca_total
            cumul += max(t['ca'], 0.0)
            if t['ca'] <= 0:
                t['niveau'] = 'flop'
            elif part_avant < 0.80:
                t['niveau'] = 'top'
            elif part_avant < 0.95:
                t['niveau'] = 'moyen'
            else:
                t['niveau'] = 'flop'
        # Bouton « Top → Flop / Flop → Top » : même classement, lu à l'envers.
        if kw.get('ordre') == 'flop':
            ordre.reverse()

        if q:
            ordre = [t for t in ordre
                     if q in t['ref'].lower() or q in t['name'].lower() or q in t['categorie'].lower()]
        for f in filtres:
            ordre = [t for t in ordre if self.ACTION_FILTRES[f](t)]
        total = len(ordre)
        page = ordre[:limit]

        # ── Couleur de chaque variante affichée.
        pids = [pid for t in page for pid in t['variantes']]
        couleurs = {}
        if pids:
            cr.execute("""
                SELECT pvc.product_product_id,
                       MAX(CASE WHEN UPPER(pa.name->>'en_US') LIKE 'COULEUR%%'
                                THEN pav.name->>'en_US' END)
                  FROM product_variant_combination pvc
                  JOIN product_template_attribute_value ptav
                    ON ptav.id = pvc.product_template_attribute_value_id
                  JOIN product_attribute pa ON pa.id = ptav.attribute_id
                  JOIN product_attribute_value pav ON pav.id = ptav.product_attribute_value_id
                 WHERE pvc.product_product_id = ANY(%s)
                 GROUP BY 1
            """, (pids,))
            couleurs = {pid: (c or '').strip() for pid, c in cr.fetchall()}

        collections = {}
        coll_ids = list({t['collection_id'] for t in page if t['collection_id']})
        if coll_ids and 'collection_id' in request.env['product.template']._fields:
            comodel = request.env['product.template']._fields['collection_id'].comodel_name
            collections = {c.id: c.display_name for c in request.env[comodel].sudo().browse(coll_ids).exists()}

        tmpls = request.env['product.template'].sudo().browse([t['id'] for t in page])
        tmpl_by_id = {t.id: t for t in tmpls}
        images = self._image_availability({t['id'] for t in page})
        actions = self._actions_references([t['id'] for t in page])
        transferts = self._transferts_references(
            [t['id'] for t in page], mags['wh_ids'] if mags['actif'] else None)
        company = request.env.company

        def arrondi(d):
            return {
                'qty_sold': int(round(d['qty_sold'])), 'ca': round(d['ca'], 2),
                'qty_purchased': int(round(d['qty_purchased'])), 'ca_achat': round(d['ca_achat'], 2),
                'stock': int(round(d['stock'])), 'depot': int(round(d['depot'])),
            }

        rows = []
        for t in page:
            par_couleur = {}
            for pid in t['variantes']:
                c = couleurs.get(pid) or '—'
                acc = par_couleur.setdefault(c, {'couleur': c, 'qty_sold': 0.0, 'ca': 0.0,
                                                 'qty_purchased': 0.0, 'ca_achat': 0.0,
                                                 'stock': 0.0, 'depot': 0.0})
                for k, val in par_variante[pid].items():
                    acc[k] += val
            variantes = []
            for acc in par_couleur.values():
                d = arrondi(acc)
                d['couleur'] = acc['couleur']
                ac = actions.get(t['id']) or {}
                d['actions'] = sorted((ac.get('couleur') or {}).get(acc['couleur'], set())
                                      | (ac.get('couleur') or {}).get('*', set()))
                variantes.append(d)
            variantes.sort(key=lambda d: (-d['ca'], -d['qty_sold'], -d['stock'], d['couleur']))
            tmpl = tmpl_by_id.get(t['id'])
            ratio = self._solde_tax_ratio(tmpl, company) if tmpl else 1.0
            src = images.get(t['id'])
            row = arrondi(t)
            row.update({
                'id': t['id'], 'rang': t['rang'], 'ref': t['ref'], 'name': t['name'],
                'categorie': t['categorie'] or '—',
                'collection': collections.get(t['collection_id']) or '—',
                'prix': round(t['list_price'] * ratio, 2),
                'image_url': self._image_url(t['id'], src), 'has_image': bool(src),
                'variantes': variantes,
                'niveau': t['niveau'],
                'actions': self._actions_resume(actions.get(t['id'])),
                'transferts': transferts.get(t['id']) or {
                    'recu': 0, 'envoye': 0, 'pieces': 0, 'scope': mags['actif'],
                    'nb_magasins_recu': 0, 'nb_magasins_envoye': 0},
            })
            rows.append(row)
        return {'rows': rows, 'total': total, 'nb_references': len(refs), 'limit': limit,
                'perimetre': self._action_perimetre(kw) + (' · ' + mags['libelle'] if mags['actif'] else ''),
                'lieux': self._action_lieux(),
                'societes': self._action_societes_cochees(),
                # Le dépôt ne s'appelle pas MOD FOR LIFE partout : la légende
                # du tableau affiche le nom réel de la société entrepôt.
                'depot_societe': self._societe_depot().name or '',
                'societe_id': getattr(request, '_mavie_societe_id', None)}

    def _action_references_sans_activite(self, kw, deja_vues):
        """Références du catalogue absentes du classement, à zéro.

        Le tableau se construit à partir des ventes, achats et stocks : une
        fiche créée mais jamais approvisionnée n'y apparaissait pas. On les
        ajoute pour que le compteur de la page corresponde au catalogue.
        """
        domaine = [('active', '=', True)]
        domaine += self._sachet_exclude_domain('collection_id')
        if self._filtre_produit_actif(kw):
            ids_filtre = request.env['product.template'].sudo().with_context(
                active_test=False).search(self._build_product_domain(kw)).ids
            domaine.append(('id', 'in', ids_filtre or [-1]))
        if deja_vues:
            domaine.append(('id', 'not in', list(deja_vues)))
        lignes = request.env['product.template'].sudo().search_read(
            domaine, ['name', 'default_code', 'base_pivot_reference', 'list_price',
                      'categ_id', 'collection_id'])
        out = []
        for t in lignes:
            out.append({
                'id': t['id'],
                'ref': (t.get('base_pivot_reference') or t.get('default_code')
                        or t.get('name') or '—'),
                'name': t.get('name') or '—',
                'categorie': (t['categ_id'][1] if t.get('categ_id') else ''),
                'list_price': t.get('list_price') or 0.0,
                'collection_id': (t['collection_id'][0] if t.get('collection_id') else None),
                'qty_sold': 0.0, 'ca': 0.0, 'qty_purchased': 0.0, 'ca_achat': 0.0,
                'stock': 0.0, 'depot': 0.0, 'variantes': [],
            })
        return out

    def _action_societe_id(self, kw):
        try:
            return int(kw.get('societe_id') or 0) or None
        except (TypeError, ValueError):
            return None

    def _action_societes_cochees(self):
        """Sociétés proposées dans le menu « Société » de la page : celles
        cochées dans Odoo (sans le drapeau de la page), hors PAIE."""
        forcee = getattr(request, '_mavie_societe_id', None)
        request._mavie_societe_id = None
        ids = self._get_context_company_ids()
        request._mavie_societe_id = forcee
        Company = request.env['res.company'].sudo()
        societes = Company.browse(ids).exists() if ids else Company.search([])
        return [{'id': c.id, 'name': c.name} for c in societes if c.name != 'PAIE']

    def _action_perimetre(self, kw):
        """Sur quoi porte le classement, affiché au-dessus du tableau.

        Vérifié le 2026-09-22 (« toutes les sociétés ont le même classement ») :
        le calcul suit bien le sélecteur de sociétés d'Odoo (SALMEDO : TC1
        premier, BLACK AND GOLD : TC28, DELTA-GOLD : Valise LK-02). Mais
        quand plusieurs sociétés sont cochées, cliquer le nom d'une société
        ne décoche pas les autres (Odoo la passe seulement en tête) : le
        classement reste celui de toutes les sociétés cochées. On l'écrit
        donc en clair."""
        if kw.get('shop_field'):
            scope = self._get_shop_scope(kw['shop_field'])
            if scope and scope.get('warehouse'):
                return 'magasin ' + scope['warehouse'].name
        ids = self._get_context_company_ids()
        noms = [n for n in request.env['res.company'].sudo().browse(ids).exists().mapped('name') if n != 'PAIE']
        if not noms:
            return 'toutes les sociétés'
        if len(noms) == 1:
            return 'société ' + noms[0]
        return 'toutes les sociétés cochées (%d)' % len(noms)

    # ─────────────────────────────────────────────────────────────
    # RÉASSORT D'UNE RÉFÉRENCE (bouton de la page Action)
    #
    # DEMANDE UTILISATRICE (2026-09-24) : « je dois faire le réassort depuis
    # Action ». La fenêtre ne montrait que les magasins EN ALERTE — souvent
    # aucun, d'où un écran vide. Elle montre désormais TOUS les magasins,
    # avec leur stock, leurs ventes et ce que le dépôt peut envoyer, et deux
    # boutons qui préparent les documents Odoo :
    #   • dépôt vide      → BON D'ACHAT fournisseur dans MOD FOR LIFE ;
    #   • dépôt servi     → BON DE VENTE inter-sociétés MOD FOR LIFE → société.
    # Les deux sont créés EN BROUILLON (devis) : rien n'est confirmé ni
    # livré sans validation dans Odoo.
    # ─────────────────────────────────────────────────────────────

    # Plafond de couverture du mode « sortir le stock du dépôt » : on
    # n'envoie jamais à un magasin plus que ce nombre de jours de vente.
    A_PLACER_PLAFOND_JOURS = 90
    A_PLACER_MAX_LIGNES = 2000

    @http.route('/mavie/api/reassort-a-placer', type='json', auth='user',
                methods=['POST'], csrf=False)
    def api_reassort_a_placer(self, **kw):
        """Ce qui dort au dépôt et pourrait partir en magasin.

        L'autre calcul attend la rupture ; celui-ci part du stock et le
        place là où il se vend. Voir le commentaire en tête du patch.
        """
        try:
            depot = self._societe_depot()
            if not depot:
                return {'error': "Société dépôt introuvable."}
            p = self._reassort_params(kw)
            # Plafond de couverture : c'est lui qui borne le resultat. A 90
            # jours on place 543 pieces ; le monter fait sortir davantage de
            # stock, au risque de charger les magasins.
            try:
                plafond = int(kw.get('plafond') or self.A_PLACER_PLAFOND_JOURS)
            except (TypeError, ValueError):
                plafond = self.A_PLACER_PLAFOND_JOURS
            plafond = max(7, min(365, plafond))
            p = dict(p, plafond=plafond)
            warehouses, wh_labels = self._reassort_warehouses(kw)
            if not warehouses:
                return {'error': "Aucun magasin actif."}
            wh_ids = warehouses.ids
            ref_date = self._reassort_reference_date(wh_ids)
            date_debut = ref_date - timedelta(days=p['fenetre'])

            # 1. Ce que le dépôt a en stock, variante par variante.
            request.env.cr.execute("""
                SELECT q.product_id, SUM(q.quantity)
                  FROM stock_quant q
                  JOIN stock_location l ON l.id = q.location_id
                  JOIN product_product pp ON pp.id = q.product_id
                  JOIN product_template pt ON pt.id = pp.product_tmpl_id
                 WHERE l.usage = 'internal' AND l.company_id = %(dep)s
                   AND pt.active AND pp.active
                 GROUP BY q.product_id
                HAVING SUM(q.quantity) > 0
            """, {'dep': depot.id})
            stock_depot = {pid: float(q or 0.0) for pid, q in request.env.cr.fetchall()}
            if not stock_depot:
                return {'rows': [], 'kpis': {}, 'params': p}
            pids = list(stock_depot)

            # 2. Ce que chaque magasin vend de ces variantes sur la fenêtre.
            request.env.cr.execute("""
                SELECT spt.warehouse_id, pol.product_id, SUM(pol.qty)
                  FROM pos_order_line pol
                  JOIN pos_order po ON po.id = pol.order_id
                  JOIN pos_session ps ON ps.id = po.session_id
                  JOIN pos_config pc ON pc.id = ps.config_id
                  JOIN stock_picking_type spt ON spt.id = pc.picking_type_id
                 WHERE po.state IN ('paid', 'done', 'invoiced')
                   AND spt.warehouse_id = ANY(%(wh)s)
                   AND pol.product_id = ANY(%(pids)s)
                   AND po.date_order >= %(d1)s AND po.date_order <= %(d2)s
                 GROUP BY 1, 2
                HAVING SUM(pol.qty) > 0
            """, {'wh': wh_ids, 'pids': pids,
                  'd1': str(date_debut) + ' 00:00:00',
                  'd2': str(ref_date) + ' 23:59:59'})
            ventes = {(w, pid): float(q or 0.0) for w, pid, q in request.env.cr.fetchall()}
            if not ventes:
                return {'rows': [], 'kpis': {}, 'params': p}

            # 3. Ce que les magasins ont déjà en rayon.
            request.env.cr.execute("""
                SELECT l.warehouse_id, q.product_id, SUM(q.quantity)
                  FROM stock_quant q
                  JOIN stock_location l ON l.id = q.location_id
                 WHERE l.usage = 'internal' AND l.warehouse_id = ANY(%(wh)s)
                   AND q.product_id = ANY(%(pids)s)
                 GROUP BY 1, 2
            """, {'wh': wh_ids, 'pids': pids})
            # SUM(quantity) peut renvoyer NULL : sans le repli, le bloc
            # plante sur les bases où le cas existe (constaté sur MaVie).
            stock_mag = {(w, pid): float(q or 0.0)
                         for w, pid, q in request.env.cr.fetchall()}

            variantes = request.env['product.product'].sudo().browse(pids)
            infos = {}
            for v in variantes:
                tmpl = v.product_tmpl_id
                couleur, taille = '', ''
                for val in v.product_template_attribute_value_ids:
                    nom = (val.attribute_id.name or '').lower()
                    if 'couleur' in nom or 'color' in nom:
                        couleur = val.name
                    elif 'taille' in nom or 'pointure' in nom or 'size' in nom:
                        taille = val.name
                infos[v.id] = {
                    'reference': (getattr(tmpl, 'base_pivot_reference', False)
                                  or tmpl.default_code or tmpl.name or '—'),
                    'produit': tmpl.name or '—',
                    'couleur': couleur or '—',
                    'taille': taille or '—',
                    'tmpl_id': tmpl.id,
                }

            fenetre = float(p['fenetre']) or 1.0
            rows = []
            pieces_total = 0.0
            for pid, dispo in stock_depot.items():
                candidats = []
                for w in wh_ids:
                    vendu = ventes.get((w, pid), 0.0)
                    if vendu <= 0:
                        continue
                    vitesse = vendu / fenetre
                    en_rayon = max(0.0, stock_mag.get((w, pid), 0.0))
                    # Un magasin SOUS sa couverture cible est en alerte : il
                    # appartient au bloc « À envoyer maintenant », pas ici.
                    # Sans ce filtre les deux blocs proposaient la même
                    # ligne (constaté sur MRC-3314 NOIR 37 / Carré Eden,
                    # stock 0 et 7 ventes : une rupture, pas un surplus).
                    if en_rayon < p['cible'] * vitesse:
                        continue
                    candidats.append({
                        'wh_id': w, 'vendu': vendu, 'vitesse': vitesse,
                        'stock': en_rayon,
                        'plafond': max(0.0, plafond * vitesse - en_rayon),
                        'envoi': 0.0,
                    })
                if not candidats:
                    continue

                # On ne distribue que ce qui dépasse la couverture cible, au
                # prorata de la vitesse de vente et sans dépasser le plafond.
                reste = dispo
                total_v = sum(c['vitesse'] for c in candidats) or 1.0
                for c in sorted(candidats, key=lambda x: -x['vitesse']):
                    if reste <= 0:
                        break
                    marge = max(0.0, c['plafond'] - c['envoi'])
                    part = min(reste, marge, round(dispo * c['vitesse'] / total_v))
                    part = max(0.0, part)
                    c['envoi'] += part
                    reste -= part

                for c in candidats:
                    if c['envoi'] <= 0:
                        continue
                    info = infos.get(pid, {})
                    pieces_total += c['envoi']
                    rows.append({
                        'product_id': pid,
                        'article_id': info.get('tmpl_id'),
                        'reference': info.get('reference', '—'),
                        'produit': info.get('produit', '—'),
                        'couleur': info.get('couleur', '—'),
                        'taille': info.get('taille', '—'),
                        'magasin': wh_labels.get(c['wh_id'], '—'),
                        'wh_id': c['wh_id'],
                        'depot': int(round(dispo)),
                        'stock': int(round(c['stock'])),
                        'vendu': int(round(c['vendu'])),
                        'vitesse_jour': round(c['vitesse'], 3),
                        'jours_couverts': (round(c['stock'] / c['vitesse'], 1)
                                           if c['vitesse'] else None),
                        'envoi': int(round(c['envoi'])),
                    })

            rows.sort(key=lambda r: (-r['envoi'], -r['vitesse_jour'], r['reference']))
            tronque = len(rows) > self.A_PLACER_MAX_LIGNES
            return {
                'params': p,
                'date_debut': date_debut.isoformat(),
                'date_reference': ref_date.isoformat(),
                'plafond_jours': plafond,
                'rows': rows[:self.A_PLACER_MAX_LIGNES],
                'tronque': tronque,
                'nb_lignes_total': len(rows),
                'kpis': {
                    'pieces': int(round(pieces_total)),
                    'nb_lignes': len(rows),
                    'nb_references': len({r['reference'] for r in rows}),
                    'nb_magasins': len({r['wh_id'] for r in rows}),
                    'stock_depot': int(round(sum(stock_depot.values()))),
                },
            }
        except Exception as e:  # noqa: BLE001
            _logger.error("Erreur api_reassort_a_placer: %s", e, exc_info=True)
            return {'error': str(e)}

    def _reassort_variantes(self, tmpl, couleur, taille=None):
        """Variantes concernées par un réassort.

        La taille est facultative : le bloc Réassort raisonne par pointure
        (« MRC-3314 NOIR 37 »), alors que la page Action raisonne par
        couleur. Sans ce filtre, la fenêtre ouverte depuis une ligne du
        bloc additionnait toutes les pointures et annonçait un besoin nul
        là où la 37 était en rupture.
        """
        variantes = (self._solde_variantes_couleur(tmpl, couleur) if couleur
                     else tmpl.product_variant_ids)
        taille = (taille or '').strip()
        if taille and taille != '—':
            cible = taille.upper()
            filtrees = variantes.filtered(
                lambda v: (resolve_variant_color_size(v)[1] or '').strip().upper() == cible)
            if filtrees:
                return filtrees
        return variantes

    @http.route('/mavie/api/reassort-article', type='json', auth='user', methods=['POST'], csrf=False)
    def api_reassort_article(self, **kw):
        """Tous les magasins pour cette référence (ou cette couleur) :
        stock, vendu sur la fenêtre, et proposition d'envoi depuis le dépôt."""
        return self._compute_reassort_article(kw)

    def _compute_reassort_article(self, kw):
        """Le calcul de la fenêtre de réassort, partagé avec le bon PDF :
        les deux doivent annoncer exactement les mêmes quantités."""
        try:
            tmpl = request.env['product.template'].sudo().browse(int(kw.get('article_id') or 0))
            if not tmpl.exists():
                return {'error': 'Référence introuvable.'}
            couleur = (kw.get('couleur') or '').strip()
            taille = (kw.get('taille') or '').strip()
            try:
                plafond_jours = int(kw.get('plafond') or self.A_PLACER_PLAFOND_JOURS)
            except (TypeError, ValueError):
                plafond_jours = self.A_PLACER_PLAFOND_JOURS
            plafond_jours = max(7, min(365, plafond_jours))
            variantes = self._reassort_variantes(tmpl, couleur, taille)
            if not variantes:
                return {'error': 'Aucune variante pour cette référence.'}
            p = self._reassort_params(kw)
            warehouses, wh_labels = self._reassort_warehouses(kw)
            mfl = self._societe_depot()
            ref_date = self._reassort_reference_date(warehouses.ids)
            debut = ref_date - timedelta(days=p['fenetre'] - 1)
            params = {
                'vids': variantes.ids,
                'wh': warehouses.ids or [-1],
                'debut': datetime.combine(debut, datetime.min.time()),
                'fin': datetime.combine(ref_date, datetime.max.time()),
                'mfl': mfl.id if mfl else -1,
            }
            request.env.cr.execute("""
                WITH stock AS (
                    SELECT w.id AS wh_id, SUM(sq.quantity) AS q
                      FROM stock_quant sq
                      JOIN stock_location sl ON sl.id = sq.location_id
                      JOIN stock_warehouse w ON w.id = ANY(%(wh)s)
                      JOIN stock_location wl ON wl.id = w.lot_stock_id
                     WHERE sq.product_id = ANY(%(vids)s)
                       AND sl.parent_path LIKE wl.parent_path || '%%'
                     GROUP BY 1
                ), vendu AS (
                    SELECT spt.warehouse_id AS wh_id, SUM(pol.qty) AS q
                      FROM pos_order_line pol
                      JOIN pos_order po ON po.id = pol.order_id
                      JOIN pos_session ps ON ps.id = po.session_id
                      JOIN pos_config pc ON pc.id = ps.config_id
                      JOIN stock_picking_type spt ON spt.id = pc.picking_type_id
                     WHERE pol.product_id = ANY(%(vids)s)
                       AND po.state IN ('paid', 'done', 'invoiced')
                       AND po.date_order BETWEEN %(debut)s AND %(fin)s
                     GROUP BY 1
                ), recu AS (
                    SELECT spt.warehouse_id AS wh_id, SUM(pol.qty_received) AS q
                      FROM purchase_order_line pol
                      JOIN purchase_order po ON po.id = pol.order_id
                      JOIN stock_picking_type spt ON spt.id = po.picking_type_id
                     WHERE pol.product_id = ANY(%(vids)s)
                       AND po.state IN ('purchase', 'done')
                     GROUP BY 1
                )
                SELECT w.id, COALESCE(s.q, 0), COALESCE(v.q, 0), COALESCE(r.q, 0)
                  FROM stock_warehouse w
                  LEFT JOIN stock s ON s.wh_id = w.id
                  LEFT JOIN vendu v ON v.wh_id = w.id
                  LEFT JOIN recu r ON r.wh_id = w.id
                 WHERE w.id = ANY(%(wh)s)
            """, params)
            lignes_sql = request.env.cr.fetchall()

            depot = 0
            if mfl:
                request.env.cr.execute("""
                    SELECT COALESCE(SUM(sq.quantity), 0)
                      FROM stock_quant sq
                      JOIN stock_location sl ON sl.id = sq.location_id
                     WHERE sq.product_id = ANY(%(vids)s)
                       AND sl.usage = 'internal' AND sl.company_id = %(mfl)s
                """, params)
                depot = int(round(request.env.cr.fetchone()[0] or 0))

            shop_par_wh = {m.warehouse_id.id: m for m in self._get_active_shop_mappings() if m.warehouse_id}
            magasins = []
            for wh_id, stock, vendu, recu in lignes_sql:
                m = shop_par_wh.get(wh_id)
                stock = int(round(stock or 0))
                vendu = int(round(vendu or 0))
                recu = int(round(recu or 0))
                vitesse = vendu / float(p['fenetre']) if vendu else 0.0
                # Besoin = de quoi tenir le nombre de jours visé au rythme
                # actuel. Appelée depuis le bloc Réassort (base='cible'), la
                # fenêtre applique la même couverture que la ligne cliquée ;
                # depuis la page Action, elle garde le délai de réappro.
                # Même base et même arrondi que la ligne cliquée, sinon la
                # fenêtre annonce 2 là où le bloc dit 3, ou 0 là où le bloc
                # dit 7 : le bloc « À placer » vise le PLAFOND, pas la cible.
                base = kw.get('base')
                if base == 'plafond':
                    jours = plafond_jours
                elif base == 'cible':
                    jours = p['cible']
                else:
                    jours = p['delai']
                besoin = max(0, int(math.ceil(vitesse * jours - max(stock, 0) - 1e-9)))
                magasins.append({
                    'wh_id': wh_id,
                    'magasin': wh_labels.get(wh_id) or (m.warehouse_id.name if m else '—'),
                    'shop_field': m.shop_field if m else None,
                    'societe': m.company_id.name if m else '',
                    'societe_id': m.company_id.id if m else None,
                    'stock': stock,
                    'vendu': vendu,
                    'recu': recu,
                    'besoin': besoin,
                })
            magasins.sort(key=lambda x: (-x['besoin'], -x['vendu'], x['magasin']))

            # Répartition de ce que le dépôt peut réellement envoyer : les
            # magasins qui vendent le plus vite d'abord.
            reste = depot
            for m in magasins:
                envoi = min(m['besoin'], reste) if reste > 0 else 0
                m['propose'] = envoi
                reste -= envoi

            fournisseur = self._reassort_fournisseur(tmpl)
            return {
                'taille': taille,
                'jours_couverture': (plafond_jours if kw.get('base') == 'plafond'
                                     else (p['cible'] if kw.get('base') == 'cible'
                                           else p['delai'])),
                'article_id': tmpl.id,
                'reference': tmpl.base_pivot_reference or tmpl.default_code or tmpl.name,
                'nom': tmpl.name,
                'couleur': couleur,
                'depot': depot,
                'fenetre': p['fenetre'],
                'delai': p['delai'],
                'magasins': magasins,
                'besoin_total': sum(m['besoin'] for m in magasins),
                'propose_total': sum(m['propose'] for m in magasins),
                'manque_depot': max(0, sum(m['besoin'] for m in magasins) - depot),
                'fournisseur': fournisseur['nom'],
                'fournisseur_id': fournisseur['id'],
            }
        except Exception as e:
            _logger.error(f"Erreur api_reassort_article: {str(e)}", exc_info=True)
            return {'error': str(e)}

    def _reassort_repartition_couleurs(self, tmpl, couleur, quantites_par_shop):
        """{couleur: {shop_field: qté}} pour les lignes couleur du batch.

        Une couleur choisie : tout va dessus. Sinon on répartit ce qui est
        demandé sur les couleurs que le dépôt possède réellement (c'est lui
        qui expédie), la mieux fournie d'abord."""
        quantites = {k: v for k, v in quantites_par_shop.items() if v > 0}
        if couleur:
            return {couleur: quantites}
        mfl = self._societe_depot()
        stock_couleur = []
        if mfl:
            request.env.cr.execute("""
                SELECT MAX(pav.name->>'en_US') AS couleur, SUM(sq.quantity) AS q
                  FROM stock_quant sq
                  JOIN stock_location sl ON sl.id = sq.location_id
                  JOIN product_product pp ON pp.id = sq.product_id
                  JOIN product_variant_combination pvc ON pvc.product_product_id = pp.id
                  JOIN product_template_attribute_value ptav
                    ON ptav.id = pvc.product_template_attribute_value_id
                  JOIN product_attribute pa ON pa.id = ptav.attribute_id
                  JOIN product_attribute_value pav ON pav.id = ptav.product_attribute_value_id
                 WHERE pp.product_tmpl_id = %(tmpl)s
                   AND sl.usage = 'internal' AND sl.company_id = %(mfl)s
                   AND UPPER(pa.name->>'en_US') LIKE 'COULEUR%%'
                 GROUP BY ptav.id
                HAVING SUM(sq.quantity) > 0
                ORDER BY 2 DESC
            """, {'tmpl': tmpl.id, 'mfl': mfl.id})
            stock_couleur = [(c.strip(), q) for c, q in request.env.cr.fetchall() if c]
        if not stock_couleur:
            # Le dépôt n'a rien (cas d'un achat fournisseur) : on prend les
            # couleurs de la référence, la première suffit.
            noms = []
            for v in tmpl.product_variant_ids:
                nom = (resolve_variant_color_size(v)[0] or '').strip()
                if nom and nom not in noms:
                    noms.append(nom)
            return {noms[0] if noms else '—': quantites}
        out = {}
        for shop_field, qte in quantites.items():
            reste = qte
            for nom, dispo in stock_couleur:
                if reste <= 0:
                    break
                part = min(int(dispo), reste)
                if part <= 0:
                    continue
                out.setdefault(nom, {})[shop_field] = out.setdefault(nom, {}).get(shop_field, 0) + part
                reste -= part
            if reste > 0:  # plus de stock dépôt : le solde va sur la 1re couleur
                nom = stock_couleur[0][0]
                out.setdefault(nom, {})[shop_field] = out.setdefault(nom, {}).get(shop_field, 0) + reste
        return out

    def _reassort_code_pointures(self, tmpl, nom_couleur, qte, taille=None):
        """Code « 0 6 12 » d'une cellule magasin, Base Pivot « magasins
        dynamiques ».

        Pour un article à pointures, Base Pivot ne lit PAS la quantité de la
        cellule : il décode `qty_raw_code`, un nombre par pointure, dans
        l'ordre des pointures croissantes (_decode_shop_cell_pointures).
        Sans ce code, la vente inter-sociétés ignore la cellule. Le réassort
        raisonne en pièces par magasin : on les répartit sur les pointures
        de la couleur avec la répartition de Base Pivot elle-même, pour que
        l'ordre soit exactement le sien.
        """
        if 'mv.color.line.dispatch' not in request.env:
            return ''
        attr = request.env['product.attribute'].sudo().search(
            [('name', '=', 'POINTURES')], limit=1)
        if not attr:
            return ''
        coul = (nom_couleur or '').strip().upper()
        variantes = []
        for v in tmpl.sudo().product_variant_ids:
            vals = v.product_template_attribute_value_ids
            if not vals.filtered(lambda av, a=attr: av.attribute_id.id == a.id):
                continue
            noms = [(av.name or '').strip().upper() for av in vals
                    if (av.attribute_id.name or '').upper().startswith('COULEUR')]
            if coul and noms and coul not in noms:
                continue
            variantes.append(v)
        if len(variantes) <= 1:
            return ''
        Batch = request.env['mv.article.batch'].sudo()
        try:
            triees = Batch._sorted_variants_by_pointure(variantes, attr)
            cible = (taille or '').strip().upper()
            if cible and cible != '—':
                # Réassort d'une pointure precise : toute la quantité va sur
                # elle, zéro sur les autres. Sans cela Base Pivot étalait
                # l'envoi sur toutes les pointures de la couleur, y compris
                # celles qui n'ont aucun besoin.
                parts = []
                place = False
                for v in triees:
                    nom = (resolve_variant_color_size(v)[1] or '').strip().upper()
                    if not place and nom == cible:
                        parts.append(int(qte))
                        place = True
                    else:
                        parts.append(0)
                if not place:
                    parts = Batch._distribute_pieces_round_robin(qte, len(triees))
            else:
                parts = Batch._distribute_pieces_round_robin(qte, len(triees))
        except Exception:
            return ''
        return ' '.join(str(int(p)) for p in parts)

    def _reassort_collection_arrivage(self, tmpl):
        """(collection, arrivage) à poser sur un batch de réassort.

        La vue formulaire de Base Pivot exige « Collection dominante » et
        « Arrivage dominant » : sans eux le batch s'ouvre en « Champs
        invalides » et ne peut pas être validé. Ils ne sont pas toujours
        sur la fiche article (vérifié : MRC-3312 et MRC-3314 les ont
        vides), mais le batch d'origine de l'article les porte. On remonte
        donc de la fiche au batch d'origine, puis au dernier batch complet.
        """
        Batch = request.env['mv.article.batch'].sudo()
        collection = getattr(tmpl, 'collection_id', False)
        arrivage = getattr(tmpl, 'arrivage_id', False)
        if collection and arrivage:
            return collection, arrivage

        if 'mv.article.base' in request.env:
            bases = request.env['mv.article.base'].sudo().search(
                [('product_tmpl_id', '=', tmpl.id)], order='id desc', limit=20)
            for base in bases:
                collection = collection or base.collection_id
                arrivage = arrivage or base.arrivage_id
                lot = base.batch_id
                if lot:
                    collection = collection or lot.collection_id
                    arrivage = arrivage or lot.arrivage_id
                if collection and arrivage:
                    return collection, arrivage

        dernier = Batch.search(
            [('collection_id', '!=', False), ('arrivage_id', '!=', False)],
            order='id desc', limit=1)
        if dernier:
            collection = collection or dernier.collection_id
            arrivage = arrivage or dernier.arrivage_id
        return collection, arrivage

    def _reassort_batch_base_pivot(self, tmpl, couleur, quantites_par_shop, suffixe,
                                   taille=None):
        """Crée un batch Base Pivot pour ce réassort.

        DEMANDE UTILISATRICE (2026-09-24) : « le réassort doit se faire dans
        Base Pivot — générer achat fournisseur, ou générer ventes
        inter-sociétés ». On ne recrée donc pas les documents à la main : on
        prépare un batch (mv.article.batch) avec la référence en état
        « Réassort » et les quantités par magasin sur les lignes couleur,
        puis on appelle les boutons de Base Pivot.

        Les champs magasin des lignes couleur portent le même nom que le
        `shop_field` des mappings (salam_2, citymall, shop…), voir
        SHOP_FIELDS dans mv_batch_shop_mapping.
        """
        # Deux versions de Base Pivot coexistent : l'historique, où la ligne
        # couleur porte une colonne Float par magasin (salam_2, citymall…),
        # et celle des « magasins dynamiques », où le dispatch est une ligne
        # de mv.color.line.dispatch (color_line_id, shop_id, qty). On écrit
        # dans l'une ou l'autre, le reste du réassort ne change pas.
        ColorLine = request.env['mv.article.base.color.line'].sudo()
        dispatch_dynamique = 'dispatch_ids' in ColorLine._fields
        shops_par_cle = {}
        if dispatch_dynamique:
            for m in self._get_active_shop_mappings():
                if m.shop_field and m.shop_id:
                    shops_par_cle.setdefault(m.shop_field, m.shop_id.id)
        Batch = request.env['mv.article.batch'].sudo()
        fournisseur = self._reassort_fournisseur(tmpl)
        ref_txt = tmpl.base_pivot_reference or tmpl.default_code or tmpl.name

        # Prix d'achat déjà connu dans Base Pivot pour cette référence.
        cout = 0.0
        art = request.env['mv.article.base'].sudo().search(
            [('product_tmpl_id', '=', tmpl.id)], order='id desc', limit=1)
        if art:
            cout = art.purchase_cost_dh_ht or 0.0
        if not cout:
            cout = tmpl.standard_price or 0.0

        # Une ligne couleur par couleur, avec la quantité de chaque magasin
        # dans sa colonne. PIÈGE : `line_total_pieces` de Base Pivot vaut
        # `colis_qty` — sans lui, total_reference reste à 0 et la génération
        # des bons d'achat ne trouve « aucune ligne éligible ».
        # Le nom de la couleur doit correspondre à la valeur de l'attribut
        # COULEURS, sinon les ventes inter-sociétés ne trouvent aucune
        # variante.
        couleurs = self._reassort_repartition_couleurs(tmpl, couleur, quantites_par_shop)
        lignes_couleur = []
        for nom_couleur, par_shop in couleurs.items():
            vals = {'color': nom_couleur, 'colis_qty': sum(par_shop.values())}
            if dispatch_dynamique:
                # Magasins dynamiques : line_total_pieces se calcule à partir
                # des lignes de dispatch, pas de colis_qty.
                dispatchs = []
                for cle, qte in par_shop.items():
                    if qte <= 0 or cle not in shops_par_cle:
                        continue
                    vals_d = {'shop_id': shops_par_cle[cle], 'qty': qte}
                    code = self._reassort_code_pointures(tmpl, nom_couleur, qte, taille)
                    if code:
                        vals_d['qty_raw_code'] = code
                    dispatchs.append((0, 0, vals_d))
                if not dispatchs:
                    raise UserError(
                        "Aucun des magasins choisis n'est relié à un magasin "
                        "de Base Pivot (Base Pivot → Configuration → Mapping "
                        "Magasins).")
                vals['dispatch_ids'] = dispatchs
            else:
                vals.update({champ: qte for champ, qte in par_shop.items()})
            lignes_couleur.append((0, 0, vals))

        # Base Pivot exige « Collection dominante » et « Arrivage dominant »
        # sur son formulaire : sans eux le batch s'ouvre en « Champs
        # invalides » et ne peut pas être validé à la main.
        collection, arrivage = self._reassort_collection_arrivage(tmpl)

        vals_batch = {
            # Le type en TÊTE : la colonne « Nom du batch » est étroite dans
            # Base Pivot et coupait la fin du nom, on ne distinguait plus
            # l'achat fournisseur de la vente inter-sociétés (2026-09-24).
            'name': '%s — Réassort %s%s' % (
                'ACHAT fournisseur' if suffixe == 'achat' else 'VENTE inter-sociétés',
                ref_txt, (' ' + couleur) if couleur else ''),
            'date': fields.Date.context_today(request.env.user),
            'reference_ids': [(0, 0, {
                'reference': ref_txt,
                'designation_odoo': tmpl.name,
                'supplier_id': fournisseur['id'] or False,
                'product_tmpl_id': tmpl.id,
                'article_state': 'reassort',
                'article_created': True,
                'purchase_cost_dh_ht': cout,
                'pv_ttc': tmpl.list_price or 0.0,
                'collection_id': collection.id if collection else False,
                'color_line_ids': lignes_couleur,
            })],
        }
        if collection and 'collection_id' in Batch._fields:
            vals_batch['collection_id'] = collection.id
        if arrivage and 'arrivage_id' in Batch._fields:
            vals_batch['arrivage_id'] = arrivage.id
        # Le fournisseur n'est porté par le batch que dans la version
        # historique de Base Pivot ; ailleurs il reste sur la référence.
        if 'fournisseur_id' in Batch._fields:
            vals_batch['fournisseur_id'] = fournisseur['id'] or False
        batch = Batch.create(vals_batch)
        return batch

    @http.route('/mavie/reassort/bon', type='http', auth='user')
    def reassort_bon_pdf(self, **kw):
        """Le bon de réassort en PDF, avant la génération Base Pivot.

        DEMANDE UTILISATRICE (2026-09-28) : « quand je clique sur envoyer il
        doit afficher le bon en PDF avec les détails, puis lancer le
        réassort dans Base Pivot ». Le bon décrit donc ce qui VA partir : il
        ne s'appuie sur aucun document, mais sur les quantités affichées
        dans la fenêtre au moment du clic.

        `lignes` arrive sous la forme « entrepot:qté,entrepot:qté ».
        """
        try:
            tmpl = request.env['product.template'].sudo().browse(
                int(kw.get('article_id') or 0))
            if not tmpl.exists():
                return request.not_found()
            couleur = (kw.get('couleur') or '').strip()
            taille = (kw.get('taille') or '').strip()
            mode = 'achat' if kw.get('mode') == 'achat' else 'vente'

            demande = {}
            for morceau in (kw.get('lignes') or '').split(','):
                if ':' not in morceau:
                    continue
                wh, qte = morceau.split(':', 1)
                try:
                    wh, qte = int(wh), int(qte)
                except (TypeError, ValueError):
                    continue
                if qte > 0:
                    demande[wh] = demande.get(wh, 0) + qte
            # Achat sans répartition : une quantité globale suffit, le bon
            # porte alors une seule ligne « à commander ».
            try:
                quantite_globale = int(kw.get('quantite') or 0)
            except (TypeError, ValueError):
                quantite_globale = 0
            if not demande and not (mode == 'achat' and quantite_globale > 0):
                return request.make_response(
                    "Aucune quantité à imprimer.",
                    [('Content-Type', 'text/plain; charset=utf-8')])

            # Les mêmes chiffres que la fenêtre : on rejoue son calcul.
            detail = self._compute_reassort_article(dict(
                kw, article_id=tmpl.id, couleur=couleur, taille=taille))
            par_wh = {m['wh_id']: m for m in (detail.get('magasins') or [])}

            lignes = []
            for wh_id, qte in demande.items():
                m = par_wh.get(wh_id) or {}
                lignes.append({
                    'magasin': m.get('magasin') or '—',
                    'societe': m.get('societe') or '',
                    'stock': int(m.get('stock') or 0),
                    'vendu': int(m.get('vendu') or 0),
                    'besoin': int(m.get('besoin') or 0),
                    'qty': int(qte),
                })
            if not lignes and quantite_globale > 0:
                # Aucune répartition par magasin : le bon porte une seule
                # ligne, la quantité à faire entrer au dépôt.
                lignes.append({
                    'magasin': 'Réapprovisionnement du dépôt',
                    'societe': detail.get('fournisseur') or '',
                    'stock': 0,
                    'vendu': int(sum(m.get('vendu') or 0
                                     for m in (detail.get('magasins') or []))),
                    'besoin': int(detail.get('besoin_total') or 0),
                    'qty': quantite_globale,
                })
            lignes.sort(key=lambda l: -l['qty'])
            total = sum(l['qty'] for l in lignes)
            depot_qty = int(detail.get('depot') or 0)
            depot = self._societe_depot()

            valeurs = {
                'reference': (getattr(tmpl, 'base_pivot_reference', False)
                              or tmpl.default_code or tmpl.name or '—'),
                'produit': tmpl.name or '',
                'couleur': couleur,
                'taille': taille,
                'fournisseur': (detail.get('fournisseur') or ''),
                'depot_nom': depot.name if depot else '—',
                'depot_qty': depot_qty,
                'manque': max(0, total - depot_qty),
                'fenetre': detail.get('fenetre') or 90,
                'jours': detail.get('jours_couverture') or detail.get('fenetre') or 30,
                'lignes': lignes,
                'total': total,
                'mode': mode,
                'edite_le': fields.Datetime.context_timestamp(
                    request.env.user, fields.Datetime.now()).strftime('%d/%m/%Y %H:%M'),
                'edite_par': request.env.user.name,
            }
            html = request.env['ir.qweb']._render(
                'mavie_dashboard.report_reassort_template', valeurs)
            pdf = request.env['ir.actions.report'].sudo()._run_wkhtmltopdf(
                [html], landscape=False,
                specific_paperformat_args={'data-report-margin-top': 10})
            nom = re.sub(r'[^\w.-]+', '_', '%s_%s_%s_%s' % (
                'Demande_achat' if mode == 'achat' else 'Bon_reassort',
                valeurs['reference'], couleur or 'toutes', taille or 'toutes')) + '.pdf'
            return request.make_response(pdf, headers=[
                ('Content-Type', 'application/pdf'),
                ('Content-Length', len(pdf)),
                ('Content-Disposition', 'inline; filename="%s"' % nom),
            ])
        except Exception as e:  # noqa: BLE001
            _logger.error("Erreur reassort_bon_pdf: %s", e, exc_info=True)
            return request.make_response(
                "Le bon n'a pas pu etre genere : %s" % e,
                [('Content-Type', 'text/plain; charset=utf-8')])

    @http.route('/mavie/api/reassort-generer', type='json', auth='user', methods=['POST'], csrf=False)
    def api_reassort_generer(self, **kw):
        """Fait le réassort DANS BASE PIVOT (demande utilisatrice
        2026-09-24) : on prépare un batch avec la référence en « Réassort »
        et les quantités par magasin, puis on appelle le bouton de Base
        Pivot correspondant :

          • mode « achat » → action_generate_purchase_orders()
            (bons d'achat fournisseur dans MOD FOR LIFE) ;
          • mode « vente » → action_generate_sale_orders()
            (bons de vente inter-sociétés MOD FOR LIFE → sociétés, confirmés
            et livrés par Base Pivot).

        Le dashboard ne recrée plus de documents à la main : c'est le même
        chemin que le bouton de l'écran Base Pivot.
        """
        try:
            tmpl = request.env['product.template'].sudo().browse(int(kw.get('article_id') or 0))
            if not tmpl.exists():
                return {'error': 'Référence introuvable.'}
            mode = kw.get('mode')
            couleur = (kw.get('couleur') or '').strip()
            if mode not in ('achat', 'vente'):
                return {'error': 'Mode inconnu.'}

            mappings = {m.warehouse_id.id: m for m in self._get_active_shop_mappings() if m.warehouse_id}
            quantites = {}
            for l in (kw.get('lignes') or []):
                try:
                    q = int(l.get('qty') or 0)
                    wh_id = int(l.get('wh_id') or 0)
                except (TypeError, ValueError):
                    continue
                m = mappings.get(wh_id)
                if q > 0 and m and m.shop_field:
                    quantites[m.shop_field] = quantites.get(m.shop_field, 0) + q

            if mode == 'achat' and not quantites:
                # Achat fournisseur sans répartition : on commande pour le
                # dépôt, la quantité saisie est portée par un magasin fictif
                # « shop » seulement si l'utilisatrice n'a rien réparti.
                try:
                    q = int(kw.get('quantite') or 0)
                except (TypeError, ValueError):
                    q = 0
                if q <= 0:
                    return {'error': 'Indiquez les quantités à commander.'}
                quantites = {'shop': q}
            if not quantites:
                return {'error': 'Choisissez au moins une quantité.'}

            fournisseur = self._reassort_fournisseur(tmpl)
            if mode == 'achat' and not fournisseur['id']:
                return {'error': "Cette référence n'a aucun fournisseur dans Odoo ni dans Base Pivot."}

            batch = self._reassort_batch_base_pivot(
                tmpl, couleur, quantites, 'achat' if mode == 'achat' else 'vente',
                (kw.get('taille') or '').strip())

            if mode == 'achat':
                retour_bp = batch.action_generate_purchase_orders()
                docs = [{'document': po.name, 'id': po.id, 'modele': 'purchase.order',
                         'societe': po.company_id.name, 'partenaire': po.partner_id.name,
                         'etat': po.state,
                         'quantite': int(round(sum(po.order_line.mapped('product_qty'))))}
                        for po in batch.purchase_order_ids]
            else:
                # LIMITE DE BASE PIVOT (constatée le 2026-09-24) :
                # action_generate_sale_orders cherche l'attribut nommé
                # exactement « COULEURS ». Les 881 articles en « COULEURSS »
                # et les 123 en « Couleur » n'y trouvent aucune variante, et
                # la génération sort « Aucun dispatch vers des magasins
                # mappés ». On le dit clairement plutôt que de laisser un
                # écran vide.
                noms_attr = tmpl.attribute_line_ids.mapped('attribute_id.name')
                couleurs_attr = [n for n in noms_attr if (n or '').upper().startswith('COULEUR')]
                if couleurs_attr and 'COULEURS' not in couleurs_attr:
                    return {'error': "Base Pivot ne sait générer les ventes inter-sociétés que pour "
                                     "l'attribut « COULEURS ». Cette référence utilise « %s » : "
                                     "renommez l'attribut dans Odoo, ou lancez la vente depuis "
                                     "l'écran Base Pivot." % couleurs_attr[0]}
                retour_bp = batch.action_generate_sale_orders()
                docs = [{'document': so.name, 'id': so.id, 'modele': 'sale.order',
                         'societe': so.company_id.name, 'partenaire': so.partner_id.name,
                         'etat': so.state,
                         'quantite': int(round(sum(so.order_line.mapped('product_uom_qty'))))}
                        for so in batch.sale_order_ids]

            if not docs:
                # Rien généré : on retire le batch vide pour ne pas encombrer
                # la liste de Base Pivot (constat 2026-09-24).
                batch.sudo().unlink()
                # Base Pivot explique lui-même pourquoi (garde-fou
                # achat/vente, mapping manquant…) : on relaie son message
                # plutôt qu'un texte générique.
                msg = ''
                try:
                    msg = (retour_bp or {}).get('params', {}).get('message') or ''
                except Exception:
                    msg = ''
                return {'error': msg or ("Base Pivot n'a créé aucun document : vérifiez le fournisseur, "
                                         "les variantes de la référence et le mapping des magasins.")}
            _vider_cache_dashboard()
            _logger.info("Réassort Base Pivot (%s) par %s : batch %s → %s",
                         mode, request.env.user.login, batch.name,
                         ', '.join(d['document'] for d in docs))
            return {'ok': True, 'mode': mode, 'batch': batch.name, 'batch_id': batch.id,
                    'documents': docs}
        except Exception as e:
            _logger.error(f"Erreur api_reassort_generer: {str(e)}", exc_info=True)
            request.env.cr.rollback()
            return {'error': str(e)}

    def _reassort_fournisseur(self, tmpl):
        """Fournisseur de la référence : celui de Base Pivot s'il existe,
        sinon le premier fournisseur renseigné sur l'article."""
        try:
            art = request.env['mv.article.base'].sudo().search(
                [('product_tmpl_id', '=', tmpl.id), ('supplier_id', '!=', False)], limit=1)
            if art and art.supplier_id:
                return {'id': art.supplier_id.id, 'nom': art.supplier_id.name}
        except Exception:
            pass
        seller = tmpl.sudo().seller_ids[:1]
        if seller and seller.partner_id:
            return {'id': seller.partner_id.id, 'nom': seller.partner_id.name}
        return {'id': None, 'nom': ''}

    @http.route('/mavie/api/reassort', type='json', auth='user', methods=['POST'], csrf=False)
    def api_reassort(self, **kw):
        try:
            # Ouvert depuis la page Action : même périmètre société qu'elle.
            if kw.get('societe_id'):
                request._mavie_societe_id = self._action_societe_id(kw)
            return self._compute_reassort(kw)
        except Exception as e:
            _logger.error(f"Erreur api_reassort: {str(e)}", exc_info=True)
            return {'error': str(e)}

    def _compute_reassort(self, kw):
        p = self._reassort_params(kw)
        warehouses, wh_labels = self._reassort_warehouses(kw)
        # Magasin cible du bouton « Transférer » de la fenêtre Réassort.
        shop_par_wh = {m.warehouse_id.id: m.shop_field
                       for m in self._get_active_shop_mappings() if m.warehouse_id}
        mod_for_life = self._societe_depot()
        if not warehouses or not mod_for_life:
            return {'rows': [], 'kpis': {}, 'params': p, 'magasins': []}

        wh_ids = warehouses.ids
        wh_societes = {w.id: w.company_id.name or '' for w in warehouses}
        ref_date = self._reassort_reference_date(wh_ids)
        date_debut = ref_date - timedelta(days=p['fenetre'] - 1)

        product_tmpl_ids = None
        if self._filtre_produit_actif(kw):
            product_tmpl_ids = request.env['product.template'].sudo().search(
                self._build_product_domain(kw)).ids or [-1]
        # Bouton « Réassort » de la page Action : une seule référence.
        if kw.get('article_id'):
            product_tmpl_ids = [int(kw['article_id'])]

        params = {
            'wh': wh_ids,
            'debut': datetime.combine(date_debut, datetime.min.time()),
            'fin': datetime.combine(ref_date, datetime.max.time()),
            'mfl': mod_for_life.id,
        }
        prod_filter = self._mfl_sans_test_sql('pt')
        if product_tmpl_ids is not None:
            prod_filter += ' AND pp.product_tmpl_id = ANY(%(tmpls)s)'
            params['tmpls'] = list(product_tmpl_ids)
        sachet = self._get_sachet_variant_ids()
        if sachet:
            prod_filter += ' AND NOT (pp.id = ANY(%(sachet)s))'
            params['sachet'] = list(sachet)

        # Une seule requête : pour chaque (magasin x variante) ACTIF sur la
        # fenêtre — il a reçu quelque chose ou vendu quelque chose —, le
        # reçu, le vendu, le stock magasin et le stock dépôt. Un couple sans
        # réception ni vente ne peut déclencher aucune des deux alertes : on
        # ne le charge pas.
        #
        #  • REÇU : mouvements validés ENTRANT dans le magasin depuis un
        #    fournisseur (réception du bon d'achat miroir de MOD FOR LIFE) ou
        #    depuis un autre entrepôt (transfert). Les ajustements
        #    d'inventaire et les retours clients caisse ne sont PAS de la
        #    marchandise reçue : exclus.
        #  • VENDU : lignes de caisse de TOUS les points de vente de
        #    l'entrepôt (physique + online partagent le même stock : pour le
        #    réassort c'est la sortie de stock qui compte), nettes des
        #    retours, planchers à 0 plus bas.
        #  • STOCK : stock.quant de l'entrepôt, sous-emplacements compris.
        #  • DÉPÔT : stock positif de MOD FOR LIFE pour la variante exacte.
        #  • Seuls les articles STOCKABLES comptent, et les lignes de
        #    récompense de caisse (remises fidélité, produit fictif « All 50%
        #    SUR LE… ») sont exclues des ventes : sans ça, une remise remontait
        #    en tête de liste comme un article en rupture à réassortir.
        request.env.cr.execute("""
            WITH wh AS (
                SELECT w.id, wl.parent_path
                  FROM stock_warehouse w
                  JOIN stock_location wl ON wl.id = w.lot_stock_id
                 WHERE w.id = ANY(%(wh)s)
            ), loc AS (
                SELECT l.id AS loc_id, wh.id AS wh_id
                  FROM stock_location l
                  JOIN wh ON l.parent_path LIKE wh.parent_path || '%%'
            ), recu AS (
                -- NET DES RETOURS FOURNISSEUR : ce que le magasin a
                -- réellement gardé. Sans cela, une référence reçue à 6 puis
                -- renvoyée à 2 affichait « 0 / 6 » avec 4 ventes, et les
                -- deux pièces manquantes semblaient s'être volatilisées
                -- (constaté sur MRC-1103 NOIR 41 chez Elite Auderby).
                SELECT wh_id, product_id, SUM(q) AS q FROM (
                    SELECT dst.wh_id, sml.product_id, SUM(sml.quantity) AS q
                      FROM stock_move_line sml
                      JOIN loc dst ON dst.loc_id = sml.location_dest_id
                      JOIN stock_location src ON src.id = sml.location_id
                      LEFT JOIN loc srcw ON srcw.loc_id = sml.location_id
                     WHERE sml.state = 'done'
                       AND sml.date BETWEEN %(debut)s AND %(fin)s
                       AND src.usage IN ('supplier', 'internal', 'transit')
                       AND (srcw.wh_id IS NULL OR srcw.wh_id <> dst.wh_id)
                     GROUP BY 1, 2
                    UNION ALL
                    SELECT srcw.wh_id, sml.product_id, -SUM(sml.quantity) AS q
                      FROM stock_move_line sml
                      JOIN loc srcw ON srcw.loc_id = sml.location_id
                      JOIN stock_location dst ON dst.id = sml.location_dest_id
                      LEFT JOIN loc dstw ON dstw.loc_id = sml.location_dest_id
                     WHERE sml.state = 'done'
                       AND sml.date BETWEEN %(debut)s AND %(fin)s
                       AND dst.usage = 'supplier'
                       AND (dstw.wh_id IS NULL OR dstw.wh_id <> srcw.wh_id)
                     GROUP BY 1, 2
                ) mouvements
                 GROUP BY 1, 2
            ), vendu AS (
                SELECT spt.warehouse_id AS wh_id, pol.product_id, SUM(pol.qty) AS q
                  FROM pos_order_line pol
                  JOIN pos_order po ON po.id = pol.order_id
                  JOIN pos_session ps ON ps.id = po.session_id
                  JOIN pos_config pc ON pc.id = ps.config_id
                  JOIN stock_picking_type spt ON spt.id = pc.picking_type_id
                 WHERE po.state IN ('paid', 'done', 'invoiced')
                   AND po.date_order BETWEEN %(debut)s AND %(fin)s
                   AND spt.warehouse_id = ANY(%(wh)s)
                   AND NOT COALESCE(pol.is_reward_line, FALSE)
                 GROUP BY 1, 2
            ), actifs AS (
                SELECT wh_id, product_id FROM recu WHERE q > 0
                UNION
                SELECT wh_id, product_id FROM vendu WHERE q > 0
            ), deja AS (
                -- Au moins une réception de la variante dans ce magasin, sur
                -- TOUT l'historique (même règle que « reçu », sans fenêtre).
                SELECT DISTINCT dst.wh_id, sml.product_id
                  FROM stock_move_line sml
                  JOIN loc dst ON dst.loc_id = sml.location_dest_id
                  JOIN actifs a ON a.wh_id = dst.wh_id AND a.product_id = sml.product_id
                  JOIN stock_location src ON src.id = sml.location_id
                  LEFT JOIN loc srcw ON srcw.loc_id = sml.location_id
                 WHERE sml.state = 'done'
                   AND src.usage IN ('supplier', 'internal', 'transit')
                   AND (srcw.wh_id IS NULL OR srcw.wh_id <> dst.wh_id)
            ), stk AS (
                SELECT loc.wh_id, sq.product_id, SUM(sq.quantity) AS q
                  FROM stock_quant sq
                  JOIN loc ON loc.loc_id = sq.location_id
                  JOIN actifs a ON a.wh_id = loc.wh_id AND a.product_id = sq.product_id
                 GROUP BY 1, 2
            ), depot AS (
                SELECT sq.product_id, SUM(sq.quantity) AS q
                  FROM stock_quant sq
                  JOIN stock_location sl ON sl.id = sq.location_id
                 WHERE sl.usage = 'internal' AND sl.company_id = %(mfl)s
                 GROUP BY 1
            ), attr AS (
                SELECT pvc.product_product_id AS pid,
                       MAX(CASE WHEN UPPER(pa.name->>'en_US') LIKE 'COULEUR%%'
                                THEN pav.name->>'en_US' END) AS couleur,
                       MAX(CASE WHEN UPPER(pa.name->>'en_US') LIKE 'POINTURE%%'
                                  OR UPPER(pa.name->>'en_US') LIKE 'TAILLE%%'
                                THEN pav.name->>'en_US' END) AS taille
                  FROM product_variant_combination pvc
                  JOIN product_template_attribute_value ptav
                    ON ptav.id = pvc.product_template_attribute_value_id
                  JOIN product_attribute pa ON pa.id = ptav.attribute_id
                  JOIN product_attribute_value pav
                    ON pav.id = ptav.product_attribute_value_id
                 GROUP BY 1
            )
            SELECT a.wh_id, pp.id, pt.id,
                   COALESCE(NULLIF(pt.base_pivot_reference, ''),
                            NULLIF(pt.default_code, ''),
                            NULLIF(pp.default_code, ''),
                            pt.name->>'en_US') AS ref,
                   COALESCE(pt.name->>'fr_FR', pt.name->>'en_US') AS produit,
                   COALESCE(attr.couleur, '') AS couleur,
                   COALESCE(attr.taille, '') AS taille,
                   COALESCE(recu.q, 0), COALESCE(vendu.q, 0),
                   COALESCE(stk.q, 0), COALESCE(depot.q, 0),
                   (deja.product_id IS NOT NULL) AS deja_recu
              FROM actifs a
              JOIN product_product pp ON pp.id = a.product_id
              JOIN product_template pt ON pt.id = pp.product_tmpl_id
              LEFT JOIN recu  ON recu.wh_id  = a.wh_id AND recu.product_id  = a.product_id
              LEFT JOIN vendu ON vendu.wh_id = a.wh_id AND vendu.product_id = a.product_id
              LEFT JOIN stk   ON stk.wh_id   = a.wh_id AND stk.product_id   = a.product_id
              LEFT JOIN depot ON depot.product_id = a.product_id
              LEFT JOIN attr  ON attr.pid = pp.id
              LEFT JOIN deja  ON deja.wh_id  = a.wh_id AND deja.product_id  = a.product_id
             WHERE pt.type = 'product' {PROD_FILTER}
        """.replace('{PROD_FILTER}', prod_filter), params)
        raw = request.env.cr.fetchall()

        fenetre = float(p['fenetre'])
        seuil = p['seuil_pct'] / 100.0
        inclure_negatifs = bool(kw.get('inclure_negatifs'))
        rows = []
        nb_negatifs = 0
        nb_jamais_recu = 0
        for (wh_id, pid, tmpl_id, ref, produit, couleur, taille,
             recu, vendu, stock, depot, deja_recu) in raw:
            recu = float(recu or 0)
            vendu = max(0.0, float(vendu or 0))   # net des retours, plancher 0
            stock_brut = float(stock or 0)
            stock_mag = max(0.0, stock_brut)
            depot = max(0.0, float(depot or 0))

            vitesse = vendu / fenetre
            jours = (stock_mag / vitesse) if vitesse > 0 else None
            reste_pct = (stock_mag / recu * 100.0) if recu > 0 else None

            alerte = None
            if recu > 0 and stock_mag <= seuil * recu:
                alerte = 'pct'          # la règle des 10 % de l'utilisateur
            elif jours is not None and jours < p['delai']:
                alerte = 'vitesse'      # reste > 10 %, mais part trop vite
            if not alerte:
                continue

            # Un stock négatif n'est pas une rupture à réassortir : c'est un
            # stock Odoo faux. Cas vérifié en base : SAC 24P-6065 NOIR à
            # Californie a reçu 14 pièces le 2026-05-05, n'a rien vendu
            # depuis, et stock.quant affiche -1. Le ramener à 0 fabriquerait
            # une fausse rupture — et la base en compte des dizaines de
            # milliers. Ces lignes sont donc MISES DE CÔTÉ par défaut
            # (comptées dans un KPI ; la case « Inclure les stocks négatifs »
            # a été retirée de l'écran le 2026-09-21) et
            # ne consomment pas le stock du dépôt dans la répartition.
            negatif = stock_brut < 0
            if negatif and not inclure_negatifs:
                nb_negatifs += 1
                continue

            # DEMANDE UTILISATEUR (2026-09-21) : pas d'alerte pour un article
            # que le magasin n'a JAMAIS reçu (sur tout l'historique). Ce ne
            # sont pas des besoins : ce sont des ventes passées sur la
            # mauvaise caisse ou prises dans le stock d'un autre magasin.
            # Cas vérifié : MAGASIN MARINA VETEMENTS AGADIR (vêtements
            # uniquement) a encaissé 49 chaussures du 28 au 31/07/2026 sans
            # en avoir jamais reçu une — le réassort proposait d'acheter des
            # babouches pour un magasin de vêtements. 92 alertes sur 852
            # étaient dans ce cas, toutes « Manquant au dépôt ».
            if not deja_recu:
                nb_jamais_recu += 1
                continue

            besoin = 0
            if vitesse > 0:
                # Arrondi au-dessus : on n'envoie pas 2,3 pièces, et arrondir
                # au-dessous laisserait le magasin juste sous sa cible.
                besoin = max(0, math.ceil(p['cible'] * vitesse - stock_mag - 1e-9))
            rows.append({
                'wh_id': wh_id,
                'magasin': wh_labels.get(wh_id) or '—',
                'shop_field': shop_par_wh.get(wh_id),
                'societe': wh_societes.get(wh_id) or '',
                'product_id': pid,
                'article_id': tmpl_id,
                'reference': ref or '—',
                'produit': produit or '—',
                'couleur': (couleur or '').strip() or '—',
                'taille': (taille or '').strip(),
                'recu': int(round(recu)),
                'vendu': int(round(vendu)),
                'stock': int(round(stock_mag)),
                'stock_negatif': int(round(stock_brut)) if negatif else 0,
                'reste_pct': round(reste_pct, 1) if reste_pct is not None else None,
                'vitesse_jour': round(vitesse, 3),
                'vitesse_semaine': round(vitesse * 7, 1),
                'jours_restants': round(jours, 1) if jours is not None else None,
                'depot': int(round(depot)),
                'besoin': besoin,
                'propose': 0,
                'alerte': alerte,
            })

        self._reassort_allocate(rows)

        for r in rows:
            if r['depot'] <= 0:
                r['statut'] = 'depot_vide'
            elif r['propose'] > 0:
                r['statut'] = 'partiel' if r['propose'] < r['besoin'] else 'servi'
            elif r['besoin'] == 0 and r['vitesse_jour'] == 0:
                r['statut'] = 'sans_vente'
            else:
                r['statut'] = 'couvert'

        # Ordre d'urgence : d'abord ce qui SE VEND (une alerte sur un article
        # sans vente sur la période n'appelle pas d'envoi, elle est à juger),
        # puis le moins de jours restants, puis la règle des 10 % avant
        # l'alerte vitesse, puis le plus rapide.
        def _urgence(r):
            j = r['jours_restants']
            return (0 if r['vitesse_jour'] > 0 else 1,
                    j if j is not None else 10 ** 6,
                    0 if r['alerte'] == 'pct' else 1,
                    -(r['vitesse_jour']))
        # Une alerte sans besoin ni envoi possible n'appelle aucune
        # action : le magasin a recu l'article, n'en a vendu aucun, et se
        # retrouve a zero. On ne l'affiche pas (707 lignes sur 755 dans ce
        # cas sur la base MaVie ; aucune sur Elite).
        rows = [r for r in rows if r['besoin'] > 0 or r['propose'] > 0]
        rows.sort(key=_urgence)

        servables = [r for r in rows if r['depot'] > 0]
        a_envoyer = [r for r in rows if r['propose'] > 0]
        kpis = {
            'nb_alertes': len(rows),
            'nb_alertes_pct': len([r for r in rows if r['alerte'] == 'pct']),
            'nb_alertes_vitesse': len([r for r in rows if r['alerte'] == 'vitesse']),
            'nb_servables': len(servables),
            'nb_depot_vide': len(rows) - len(servables),
            'pieces_proposees': sum(r['propose'] for r in rows),
            'pieces_besoin': sum(r['besoin'] for r in rows),
            'nb_magasins': len({r['wh_id'] for r in rows}),
            'nb_references': len({r['article_id'] for r in rows}),
            'nb_negatifs': nb_negatifs,
            'nb_jamais_recu': nb_jamais_recu,
            'inclure_negatifs': inclure_negatifs,
            'nb_ruptures': len([r for r in rows if r['stock'] == 0]),
            # Comptes calcules sur TOUTES les lignes, pas sur les seules
            # lignes chargees : sans cela, la carte « A envoyer » annoncait
            # 323 pieces quand le tableau n'en listait que 290, les 33
            # manquantes etant tombees avec le plafond d'affichage.
            'nb_envoyer': len(a_envoyer),
            'nb_surveiller': len(servables) - len(a_envoyer),
        }
        reste = [r for r in rows if r['propose'] <= 0]
        place = max(0, self.REASSORT_MAX_ROWS - len(a_envoyer))
        rows_affichees = a_envoyer + reste[:place]

        magasins = sorted({(r['wh_id'], r['magasin'], r['societe']) for r in rows}, key=lambda x: x[1])
        return {
            'params': p,
            'date_reference': ref_date.isoformat(),
            'date_debut': date_debut.isoformat(),
            'aujourdhui': date.today().isoformat(),
            'kpis': kpis,
            # Le plafond protege l'affichage, il ne doit pas faire
            # disparaitre du travail : les lignes qui ont quelque chose a
            # envoyer partent toutes (elles sont peu nombreuses et ce sont
            # les seules sur lesquelles on agit), le reste du plafond va aux
            # plus urgentes.
            'rows': rows_affichees,
            'tronque': len(rows) > len(rows_affichees),
            'nb_lignes_total': len(rows),
            'magasins': [{'id': w, 'name': n, 'societe': s} for w, n, s in magasins],
        }

    def _reassort_allocate(self, rows):
        """Répartit le stock du dépôt entre les magasins qui en ont besoin.

        Pour une même variante, si le dépôt couvre tous les besoins, chacun
        reçoit son besoin. Sinon, on distribue PIÈCE PAR PIÈCE : chaque
        pièce va au magasin dont le rapport vitesse ÷ (déjà attribué + 1)
        est le plus élevé, sans jamais dépasser son besoin (méthode de
        D'Hondt). Résultat : un partage proportionnel à la vitesse, en
        pièces entières, où le magasin qui vend le plus vite passe devant.

        CORRIGÉ avant livraison : une première version calculait une part
        au prorata puis la plafonnait au besoin, sans redistribuer le
        surplus. Dépôt = 10, A (besoin 2, vend vite), B (besoin 20) : A
        recevait 2, B recevait 2, et 6 pièces restaient au dépôt alors que B
        en manquait. Ici une pièce n'est jamais laissée au dépôt tant qu'un
        magasin en a besoin.

        Travaille en place sur `rows` (clés `besoin`, `depot`,
        `vitesse_jour`, `product_id`) et renseigne `propose` / `partage`.
        """
        par_variante = {}
        for r in rows:
            if r['besoin'] > 0:
                par_variante.setdefault(r['product_id'], []).append(r)
        for lignes in par_variante.values():
            dispo = int(lignes[0]['depot'])
            if dispo <= 0:
                continue
            if sum(r['besoin'] for r in lignes) <= dispo:
                for r in lignes:
                    r['propose'] = r['besoin']
                continue
            attrib = [0] * len(lignes)
            # (-priorité, index) : heapq est un tas min. L'index départage
            # les égalités de façon stable (ordre d'urgence de la liste).
            tas = [(-(r['vitesse_jour'] or 0.0), i) for i, r in enumerate(lignes)]
            heapq.heapify(tas)
            while dispo > 0 and tas:
                _prio, i = heapq.heappop(tas)
                attrib[i] += 1
                dispo -= 1
                if attrib[i] < lignes[i]['besoin']:
                    heapq.heappush(
                        tas, (-(lignes[i]['vitesse_jour'] or 0.0) / (attrib[i] + 1), i))
            for r, q in zip(lignes, attrib):
                r['propose'] = q
                r['partage'] = True

    # ─────────────────────────────────────────────────────────────
    # SOLDES DEPUIS LE DASHBOARD
    #
    # DEMANDE UTILISATEUR (2026-09-21) : un bouton « Solder » à côté de
    # « Transférer » dans la fiche référence, pour lancer une solde sans
    # quitter le dashboard. Règle : vérifier si le magasin a déjà sa liste
    # de soldes ; sinon en créer une nouvelle, avec un nom.
    #
    # Ce qu'est « la liste des soldes d'un magasin » dans cette base
    # (vérifié le 2026-09-21) : une LISTE DE PRIX Odoo (product.pricelist)
    # nommée « Solde … » et rattachée aux listes disponibles de la caisse
    # du magasin — ex. « Solde Sela Park » (735 règles, caisse MAGASIN
    # CARREFOUR AGADIR), « Solde Salam 2 » (556 règles, MAGASIN SALAM2
    # AGADIR). Chaque article soldé y est une règle « prix fixe » posée sur
    # l'ARTICLE (applied_on = 1_product : toutes couleurs et tailles), avec
    # une date de début. On reproduit exactement ce modèle, pour que les
    # soldes créées ici soient indiscernables de celles créées à la main.
    #
    # PIÈGE : ces prix sont enregistrés HORS TAXE, comme le prix de vente
    # de l'article (ex. 165,83 HT = 199 TTC). L'utilisateur saisit un prix
    # TTC (celui de l'étiquette) ; on le convertit avec les taxes de
    # l'article, sinon la solde partirait 20 % trop haut.
    # ─────────────────────────────────────────────────────────────

    def _solde_tax_ratio(self, product_tmpl, company):
        """Coefficient TTC/HT du prix de vente de l'article pour cette
        société (1.0 sans taxe, ou si la taxe est déjà incluse dans le prix
        catalogue — dans ce cas le prix catalogue et la règle sont tous deux
        TTC et il n'y a rien à convertir)."""
        taxes = product_tmpl.sudo().taxes_id.filtered(
            lambda t: not t.company_id or t.company_id == company)
        if not taxes:
            return 1.0
        res = taxes.compute_all(100.0, currency=company.currency_id)
        ratio = (res.get('total_included') or 100.0) / 100.0
        return ratio if 0.5 <= ratio <= 2.0 else 1.0

    def _solde_store_configs(self, mapping):
        """Caisses PHYSIQUES du magasin (les caisses « Online » du même
        entrepôt ont leurs propres promotions, on n'y touche pas)."""
        scope = self._get_shop_scope(mapping.shop_field)
        ids = (scope or {}).get('pos_config_ids') or []
        return request.env['pos.config'].sudo().browse(ids).exists()

    def _solde_is_default_list(self, pricelist):
        """Liste de prix NORMALE (« Liste de prix MAD par défaut ») : jamais
        une liste de soldes, on n'y pose aucune règle."""
        nom = (pricelist.name or '').lower()
        return 'par défaut' in nom or 'par defaut' in nom or 'default' in nom

    def _solde_find_list(self, configs):
        """La liste de soldes déjà rattachée à ces caisses, s'il y en a une.

        DEMANDE UTILISATEUR (2026-09-21) : « si la caisse a déjà une liste,
        il ne faut pas en créer une nouvelle ». Avant, seules les listes dont
        le nom contient « solde » étaient reconnues : « REMISE 20% »,
        rattachée à MAGASIN MORROCO MALL, était ignorée et le bouton créait
        « Solde Morocco Mall » à côté. Choix de l'utilisatrice : TOUJOURS
        réutiliser la liste existante, même si elle est partagée avec
        d'autres caisses (REMISE 20% sert les 14 caisses de la société : le
        prix soldé s'y appliquera partout ; le panneau l'annonce).

        Toute liste active disponible sur la caisse, sauf la liste normale
        par défaut. S'il y en a plusieurs : d'abord celles nommées « solde »,
        puis celle qui porte le plus d'articles.
        """
        lists = (configs.mapped('available_pricelist_ids') | configs.mapped('pricelist_id'))
        lists = lists.filtered(lambda p: p.active and not self._solde_is_default_list(p))

        # A04 (2026-09-24) : sur Elite, la liste « Solde » est la liste PAR
        # DÉFAUT des 7 caisses Boutique, des deux sociétés. Son nom ne
        # contient pas « défaut », elle passait donc pour une liste de
        # soldes ordinaire : solder un article pour UN magasin le soldait
        # dans les sept. On écarte ici toute liste qui sert de liste par
        # défaut à une caisse d'un autre magasin ; le bouton crée alors une
        # liste propre au magasin. Une liste partagée mais qui n'est la
        # liste par défaut de personne d'autre reste réutilisée, comme
        # demandé le 2026-09-21.
        Config = request.env['pos.config'].sudo()
        def _defaut_ailleurs(pl):
            autres = Config.search([('pricelist_id', '=', pl.id)]) - configs
            return bool(autres)
        # DEMANDE UTILISATRICE (2026-09-25) : « s'il y a déjà une liste de
        # soldes, le bouton doit écrire dedans ; sinon il en crée une ».
        # Une liste dont le NOM parle de solde est donc réutilisée telle
        # quelle, même si elle sert plusieurs caisses — le panneau annonce
        # alors les autres magasins concernés.
        soldes = lists.filtered(lambda p: 'solde' in (p.name or '').lower())
        if soldes:
            lists = soldes
        else:
            # Sinon on évite d'écrire dans la liste par défaut d'autres
            # magasins : le bouton créera une liste propre au magasin.
            propres = lists.filtered(lambda p: not _defaut_ailleurs(p))
            lists = propres or request.env['product.pricelist'].sudo().browse()
        if not lists:
            return request.env['product.pricelist'].sudo().browse()
        return lists.sorted(
            key=lambda p: ('solde' in (p.name or '').lower(), len(p.item_ids), p.id), reverse=True)[:1]

    def _solde_other_configs(self, pricelist, configs):
        """Caisses HORS de ce magasin qui utilisent aussi cette liste."""
        autres = request.env['pos.config'].sudo().search([
            '|', ('available_pricelist_ids', 'in', pricelist.ids), ('pricelist_id', 'in', pricelist.ids)])
        return autres - configs

    def _solde_variantes_couleur(self, product_tmpl, couleur):
        """Variantes (toutes tailles) d'UNE couleur de la référence.

        DEMANDE UTILISATRICE (2026-09-22) : depuis la page Action, pouvoir
        solder une seule couleur (bouton sur la ligne variante). Une règle de
        liste de prix ne vise qu'une variante : une couleur = une règle par
        taille (applied_on = 0_product_variant). Odoo fait passer ces règles
        avant la règle posée sur l'article entier."""
        request.env.cr.execute("""
            SELECT pp.id
              FROM product_product pp
              JOIN product_variant_combination pvc ON pvc.product_product_id = pp.id
              JOIN product_template_attribute_value ptav
                ON ptav.id = pvc.product_template_attribute_value_id
              JOIN product_attribute pa ON pa.id = ptav.attribute_id
              JOIN product_attribute_value pav ON pav.id = ptav.product_attribute_value_id
             WHERE pp.product_tmpl_id = %s
               AND UPPER(pa.name->>'en_US') LIKE 'COULEUR%%'
               AND TRIM(pav.name->>'en_US') = %s
        """, (product_tmpl.id, (couleur or '').strip()))
        return request.env['product.product'].sudo().browse([r[0] for r in request.env.cr.fetchall()])

    def _solde_rule_variante(self, pricelist, variant):
        return pricelist.item_ids.filtered(
            lambda i: i.applied_on == '0_product_variant' and i.product_id.id == variant.id
        )[:1]

    def _solde_rule(self, pricelist, product_tmpl):
        return pricelist.item_ids.filtered(
            lambda i: i.applied_on == '1_product' and i.product_tmpl_id.id == product_tmpl.id
        )[:1]

    def _solde_mappings(self):
        non_retail = self._get_non_retail_company_ids()
        return self._get_active_shop_mappings().filtered(
            lambda m: m.warehouse_id and m.company_id and m.company_id.id not in non_retail
        )

    # 742 references au perimetre actuel : le plafond doit les couvrir,
    # sinon la carte et le tableau se contredisent.
    SOLDES_MAX_LIGNES = 1000

    # Au-dela de cette couverture, une reference dort meme si elle vend un
    # peu : 400 pieces qui s'ecoulent en deux ans ne tournent pas. Seuil
    # PARTAGE par la carte « Stock dormant » et la page Propositions, pour
    # que les deux ecrans ne puissent pas se contredire.
    COUVERTURE_DORMANTE_JOURS = 120

    def _soldes_params(self, kw):
        """Réglages de la page Soldes, bornés pour rester raisonnables."""
        def _int(nom, defaut, mini, maxi):
            try:
                v = int(kw.get(nom) or defaut)
            except (TypeError, ValueError):
                v = defaut
            return max(mini, min(maxi, v))
        return {
            'fenetre': _int('fenetre', 90, 7, 365),
            'stock_min': _int('stock_min', 5, 1, 500),
            'remise1': _int('remise1', 30, 5, 90),
            'remise2': _int('remise2', 50, 5, 90),
            'couverture_min': _int('couverture_min',
                                   self.COUVERTURE_DORMANTE_JOURS, 7, 3650),
            # 'historique' : les paliers viennent de ce que la maison a
            # pratique et de ce que ca a vendu. 'fixe' : les deux valeurs
            # ci-dessus, telles quelles.
            'mode_remise': ('fixe' if (kw.get('mode_remise') or '') == 'fixe'
                            else 'historique'),
        }

    # Une bande de remise n'est retenue comme reference que si la maison
    # l'a pratiquee assez souvent pour que le chiffre veuille dire quelque
    # chose. En dessous, une seule vente heureuse ferait la loi.
    SOLDES_MIN_REGLES_BANDE = 5
    SOLDES_MIN_REGLES_PALIER = 3
    # En dessous de ce nombre de remises deja posees, une categorie n'a pas
    # d'habitude : une ou deux operations ne font pas une regle.
    SOLDES_MIN_REGLES_CATEGORIE = 3
    # 0 % n'est pas une remise, et 100 % est un cadeau ou une erreur de
    # saisie : ni l'un ni l'autre ne sert de reference.
    SOLDES_REMISE_MIN = 5
    SOLDES_REMISE_MAX = 95

    @staticmethod
    def _soldes_quantile(valeurs, part):
        """Le quantile d'une liste de niveaux déjà posés.

        On reste sur des valeurs OBSERVÉES : pas d'interpolation entre deux
        paliers, sinon on proposerait une remise que la maison n'a jamais
        pratiquée.
        """
        if not valeurs:
            return None
        tri = sorted(valeurs)
        i = int(round((len(tri) - 1) * part))
        return tri[max(0, min(len(tri) - 1, i))]

    def _soldes_habitude(self, niveaux):
        """L'habitude que dit une liste de remises déjà posées : ce qu'on
        fait d'ordinaire (médiane) et ce qu'on fait quand on va fort (3e
        quartile). Les deux sont des paliers réellement pratiqués."""
        normal = self._soldes_quantile(niveaux, 0.5)
        fort = self._soldes_quantile(niveaux, 0.75)
        if normal is None:
            return None, None
        # Si les deux se confondent, on prend le palier pratiqué juste
        # au-dessus : « fort » doit vouloir dire quelque chose de plus.
        if fort is None or fort <= normal:
            plus_haut = [n for n in niveaux if n > normal]
            fort = min(plus_haut) if plus_haut else normal
        return int(normal), int(fort)

    def _soldes_remises_pratiquees(self):
        """Ce que la maison a réellement pratiqué, et ce que ça a vendu.

        On lit les règles de prix de soldes en place, on les range par
        bande de 10 points, et on compte les pièces vendues depuis leur
        pose. La bande qui vend le plus par remise posée devient le palier
        conseillé — il n'y a pas de raison de proposer 30 % si 44 % est ce
        qui marche ici.
        """
        cle = self._cache_cle('soldes_remises_pratiquees', {})
        cache = self._cache_lire(cle)
        if cache is not None:
            return cache
        journal = self.api_soldes_journal()
        regles = journal.get('rows') or []
        # La categorie de chaque reference deja soldee : c'est par la que
        # l'on retrouve l'habitude de la maison sur une marchandise donnee.
        categories = {}
        ids = list({r['article_id'] for r in regles})
        if ids:
            categories = {t['id']: (t['categ_id'][1] if t.get('categ_id') else '')
                          for t in request.env['product.template'].sudo().search_read(
                              [('id', 'in', ids)], ['categ_id'])}
        exacts = {}
        bandes = {}
        par_cat = {}
        for r in regles:
            niveau = int(round(r['remise']))
            if not (self.SOLDES_REMISE_MIN <= niveau <= self.SOLDES_REMISE_MAX):
                continue
            nom_cat = categories.get(r['article_id']) or ''
            if nom_cat:
                par_cat.setdefault(nom_cat, []).append(niveau)
            e = exacts.setdefault(niveau, {'niveau': niveau, 'regles': 0, 'pieces': 0})
            e['regles'] += 1
            e['pieces'] += r['vendu_depuis']
            b = bandes.setdefault((niveau // 10) * 10,
                                  {'de': (niveau // 10) * 10, 'a': (niveau // 10) * 10 + 9,
                                   'regles': 0, 'pieces': 0, 'niveaux': {}})
            b['regles'] += 1
            b['pieces'] += r['vendu_depuis']
            b['niveaux'][niveau] = b['niveaux'].get(niveau, 0) + 1

        for b in bandes.values():
            b['par_regle'] = round(b['pieces'] / float(b['regles']), 1) if b['regles'] else 0.0
            # Le niveau representatif d'une bande : celui que la maison y a
            # le plus souvent pose. C'est son habitude, pas une moyenne.
            b['niveau'] = max(b['niveaux'].items(), key=lambda kv: (kv[1], kv[0]))[0] \
                if b['niveaux'] else b['de']

        retenues = [b for b in bandes.values()
                    if b['regles'] >= self.SOLDES_MIN_REGLES_BANDE]
        forte = normale = None
        if retenues:
            meilleure = max(retenues, key=lambda b: (b['par_regle'], b['de']))
            forte = meilleure
            dessous = [b for b in retenues if b['de'] < meilleure['de']]
            normale = (max(dessous, key=lambda b: (b['par_regle'], b['de']))
                       if dessous else None)
        # L'echelle proposee dans le tableau : les niveaux que la maison
        # pratique vraiment, pas une graduation inventee.
        echelle = sorted(n for n, e in exacts.items()
                         if e['regles'] >= self.SOLDES_MIN_REGLES_PALIER)

        # L'habitude de chaque categorie qui a assez de precedents. C'est
        # elle qui prime sur le releve global : on ne solde pas une valise
        # comme une mule.
        habitudes = {}
        for nom_cat, niveaux in par_cat.items():
            if len(niveaux) < self.SOLDES_MIN_REGLES_CATEGORIE:
                continue
            normal, fort = self._soldes_habitude(niveaux)
            if normal is None:
                continue
            habitudes[nom_cat] = {
                'categorie': nom_cat, 'regles': len(niveaux),
                'niveau_normal': normal, 'niveau_fort': fort,
                'mini': min(niveaux), 'maxi': max(niveaux),
            }
        # Meme lecture, tous produits confondus : le filet pour les
        # categories sans precedent.
        tous = [n for niveaux in par_cat.values() for n in niveaux] or \
               [n for n, e in exacts.items() for _ in range(e['regles'])]
        gen_normal, gen_fort = self._soldes_habitude(tous)

        def _ancrer(niveau):
            """Ramène un palier général sur l'échelle des niveaux assez
            souvent pratiqués : il gouverne les références sans précédent
            de catégorie, il ne peut pas tenir sur une ou deux règles."""
            if niveau is None or not echelle:
                return niveau
            return min(echelle, key=lambda n: (abs(n - niveau), -n))

        gen_normal, gen_fort = _ancrer(gen_normal), _ancrer(gen_fort)
        if gen_fort is not None and gen_normal is not None and gen_fort <= gen_normal:
            plus_haut = [n for n in echelle if n > gen_normal]
            gen_fort = min(plus_haut) if plus_haut else gen_normal

        res = {
            'echelle': echelle,
            'habitudes': sorted(habitudes.values(), key=lambda h: -h['regles']),
            'par_categorie': habitudes,
            'usage_normal': gen_normal,
            'usage_fort': gen_fort,
            'bandes': sorted(bandes.values(), key=lambda b: -b['de']),
            'niveau_normal': normale['niveau'] if normale else None,
            'niveau_fort': forte['niveau'] if forte else None,
            'bande_normale': [normale['de'], normale['a']] if normale else None,
            'bande_forte': [forte['de'], forte['a']] if forte else None,
            'nb_regles': len(regles),
        }
        self._cache_ecrire(cle, res)
        return res

    def _soldes_palier_suivant(self, actuelle, echelle):
        """Le prochain palier au-dessus d'une remise déjà en place.

        Proposer moins que ce qui est posé n'a aucun sens : la marchandise
        n'a pas bougé au prix actuel. On monte donc au palier pratiqué
        juste au-dessus.
        """
        plus_haut = [n for n in (echelle or []) if n > actuelle]
        if plus_haut:
            return min(plus_haut)
        return int(min(self.SOLDES_REMISE_MAX, actuelle + 15))

    def _soldes_paliers_anciennete(self):
        """Les tranches d'ancienneté de la dernière vente, et ce que
        chacune appelle comme remise. Partagées par la réponse et par son
        détail, pour qu'elles ne puissent pas se contredire."""
        return [
            ('jamais', "Jamais vendue",
             lambda r: r['jamais_vendu'], 'remise maximale'),
            ('plus6m', "Plus de 6 mois",
             lambda r: not r['jamais_vendu'] and r['jours_sans_vente'] > 180,
             'remise franche'),
            ('3a6m', "3 à 6 mois",
             lambda r: not r['jamais_vendu'] and 90 < r['jours_sans_vente'] <= 180,
             'remise conseillée'),
            ('1a3m', "1 à 3 mois",
             lambda r: not r['jamais_vendu'] and 30 < r['jours_sans_vente'] <= 90,
             'à surveiller'),
            ('moins1m', "Moins d'un mois",
             lambda r: not r['jamais_vendu'] and r['jours_sans_vente'] <= 30,
             'laisser tourner'),
        ]

    ASSISTANT_QUESTIONS = [
        {'id': 'solder_semaine', 'texte': 'Que dois-je solder cette semaine ?',
         'aide': "Les références les plus urgentes, prêtes à démarquer."},
        {'id': 'remise_reference', 'texte': 'Quelle remise pour une référence ?',
         'aide': "Choisissez une référence : la remise conseillée et pourquoi.",
         'besoin_reference': True},
        {'id': 'ou_solder', 'texte': 'Dans quels magasins solder ?',
         'aide': "Les magasins qui portent le plus de stock dormant."},
        {'id': 'categories_dorment', 'texte': 'Quelles catégories dorment le plus ?',
         'aide': "Où se concentre l'argent immobilisé, par famille d'articles."},
        {'id': 'depuis_quand', 'texte': 'Depuis combien de temps ça dort ?',
         'aide': "Le stock classé par ancienneté de la dernière vente."},
        {'id': 'deja_soldees', 'texte': 'Où en sont les soldes déjà posées ?',
         'aide': "Ce qui est déjà démarqué, et si ça s'écoule."},
        {'id': 'remise_qui_marche', 'texte': 'Quelle remise marche le mieux ici ?',
         'aide': "Les remises déjà pratiquées, et ce que chacune a vendu."},
        {'id': 'habitudes_categorie', 'texte': 'Quelles remises pratiquez-vous, par catégorie ?',
         'aide': "L'habitude de chaque famille d'articles — c'est elle qui "
                 "calibre les propositions."},
        {'id': 'moment_vente', 'texte': 'Quel moment vend le mieux, magasin par magasin ?',
         'aide': "Le jour et la tranche horaire où chaque magasin fait le plus de chiffre (heure de Paris)."},
    ]

    ASSISTANT_DETAIL_MAX = 300

    JOURNAL_SOLDES_MAX = 500

    def _soldes_listes_par_magasin(self):
        """{pricelist_id: magasin} pour les listes de prix des caisses.

        Une liste de soldes n'existe que rattachée à une caisse : c'est par
        là qu'on retrouve le magasin d'une règle de prix. La liste normale
        est écartée, aucune solde n'y est jamais posée.
        """
        listes = {}
        for m in self._solde_mappings():
            nom = m.shop_label or m.warehouse_id.name
            for cfg in self._solde_store_configs(m):
                for pl in cfg.available_pricelist_ids:
                    if self._solde_is_default_list(pl):
                        continue
                    d = listes.setdefault(pl.id, {
                        'liste': pl.display_name,
                        'magasins': [], 'societes': [], 'warehouse_ids': [],
                        'shop_fields': [],
                        # La societe de reference sert au calcul de la TVA ;
                        # la premiere suffit, les prix sont les memes.
                        'company_id': m.company_id.id,
                    })
                    if nom not in d['magasins']:
                        d['magasins'].append(nom)
                        d['warehouse_ids'].append(m.warehouse_id.id)
                        d['shop_fields'].append(m.shop_field)
                    if m.company_id.name not in d['societes']:
                        d['societes'].append(m.company_id.name)
        return listes

    def _soldes_portee_reelle(self):
        """Une remise posée sur une liste partagée s'applique à TOUS les
        magasins qui partagent cette liste.

        Le dashboard laisse choisir les magasins, mais la portée réelle est
        celle de la liste de prix : si une seule liste « Solde » est
        rattachée aux caisses de plusieurs magasins, décocher un magasin ne
        l'épargne pas. On renvoie de quoi le dire à l'écran.
        """
        par_liste = {}
        for m in self._solde_mappings():
            for cfg in self._solde_store_configs(m):
                for pl in cfg.available_pricelist_ids:
                    if self._solde_is_default_list(pl):
                        continue
                    d = par_liste.setdefault(pl.id, {'liste': pl.display_name,
                                                     'magasins': []})
                    nom = m.shop_label or m.warehouse_id.name
                    if nom not in d['magasins']:
                        d['magasins'].append(nom)
        partagees = [d for d in par_liste.values() if len(d['magasins']) > 1]
        for d in partagees:
            d['magasins'].sort()
        return {
            'partagees': partagees,
            # Vrai quand AUCUN magasin ne peut etre soldé séparément.
            'tout_lie': bool(partagees) and len(par_liste) == len(partagees),
        }

    TRANSFERTS_MAX_LIGNES = 400

    def _transferts_params(self, kw):
        """Réglages du moteur de transferts, bornés pour rester sensés."""
        def _int(nom, defaut, mini, maxi):
            try:
                v = int(kw.get(nom) or defaut)
            except (TypeError, ValueError):
                v = defaut
            return max(mini, min(maxi, v))
        return {
            'fenetre': _int('fenetre', 90, 7, 365),
            # Combien de jours de vente chaque magasin doit pouvoir tenir.
            # 60 et non 30 : a 30 jours le reseau ne sort que 10 propositions,
            # parce qu'il tourne trop lentement pour que quiconque soit court
            # a un mois. A 60 on obtient 77 propositions exploitables.
            'cible': _int('cible', 60, 7, 180),
            # En dessous, le camion coûte plus que la marchandise.
            'min_qte': _int('min_qte', 3, 1, 100),
            # Un donneur n'est retenu que s'il dort vraiment dessus.
            'couverture_donneur': _int('couverture_donneur', 120, 30, 3650),
            # Un demandeur n'est retenu que s'il est réellement court : avec
            # une cible de 60 jours, tenir 45 jours n'est pas une difficulté.
            'couverture_demandeur': _int('couverture_demandeur', 45, 1, 180),
        }

    def _transferts_en_attente(self):
        """Couples (variante, magasin source, magasin cible) deja couverts par un bon non livre."""
        Wh = request.env['stock.warehouse'].sudo()
        attente = set()
        bons = request.env['inter.internal.transfer'].sudo().search(
            [('state', 'in', ['draft', 'submitted', 'received', 'transmitted'])])
        for bon in bons:
            src = Wh.search([('view_location_id', 'parent_of', bon.location_source_id.id)], limit=1)
            dst = Wh.search([('view_location_id', 'parent_of', bon.location_target_id.id)], limit=1)
            if not src or not dst:
                continue
            for ligne in bon.line_ids:
                attente.add((ligne.product_id.id, src.id, dst.id))
        return attente

    def _transferts_flux_recents(self, jours):
        """Couples (variante, magasin source, magasin cible) de tous les bons crees depuis `jours`."""
        Wh = request.env['stock.warehouse'].sudo()
        depuis = fields.Datetime.now() - timedelta(days=jours)
        flux = set()
        bons = request.env['inter.internal.transfer'].sudo().search([
            ('create_date', '>=', depuis), ('state', '!=', 'cancelled')])
        for bon in bons:
            src = Wh.search([('view_location_id', 'parent_of', bon.location_source_id.id)], limit=1)
            dst = Wh.search([('view_location_id', 'parent_of', bon.location_target_id.id)], limit=1)
            if not src or not dst:
                continue
            for ligne in bon.line_ids:
                flux.add((ligne.product_id.id, src.id, dst.id))
        return flux

    def _transferts_recus_recemment(self, jours):
        """Couples (article, magasin) qui ont recu une livraison validee depuis `jours`."""
        Wh = request.env['stock.warehouse'].sudo()
        depuis = fields.Datetime.now() - timedelta(days=jours)
        recus = set()
        bons = request.env['inter.internal.transfer'].sudo().search(
            [('state', 'in', ['transmitted', 'done']), ('write_date', '>=', depuis)])
        for bon in bons:
            magasin = Wh.search([('view_location_id', 'parent_of', bon.location_target_id.id)],
                                limit=1)
            if not magasin:
                continue
            for ligne in bon.line_ids:
                recus.add((ligne.product_id.id, magasin.id))
        return recus

    @http.route('/mavie/api/transferts-proposition', type='json', auth='user',
                methods=['POST'], csrf=False)
    def api_transferts_proposition(self, **kw):
        """Ce qui gagnerait à changer de magasin, plutôt qu'à être soldé."""
        try:
            p = self._transferts_params(kw)
            warehouses, wh_labels = self._reassort_warehouses(kw)
            if not warehouses:
                return {'error': "Aucun magasin actif."}
            wh_ids = warehouses.ids
            if len(wh_ids) < 2:
                return {'error': "Il faut au moins deux magasins pour transférer."}
            ref_date = self._reassort_reference_date(wh_ids)
            debut = ref_date - timedelta(days=p['fenetre'])

            filtres = self._mfl_sans_test_sql('pt')
            params = {'wh': wh_ids, 'd1': str(debut) + ' 00:00:00',
                      'd2': str(ref_date) + ' 23:59:59'}
            sachets = self._get_sachet_variant_ids()
            if sachets:
                filtres += ' AND NOT (pp.id = ANY(%(sachets)s))'
                params['sachets'] = list(sachets)
            if self._filtre_produit_actif(kw):
                params['tmpls'] = request.env['product.template'].sudo().search(
                    self._build_product_domain(kw)).ids or [-1]
                filtres += ' AND pt.id = ANY(%(tmpls)s)'

            # Stock et ventes par MAGASIN : c'est le couple qui decide d'un
            # transfert, pas le total reseau.
            request.env.cr.execute("""
                WITH stock AS (
                    SELECT pp.id AS var, l.warehouse_id AS wh,
                           SUM(q.quantity) AS qte
                      FROM stock_quant q
                      JOIN stock_location l ON l.id = q.location_id
                      JOIN product_product pp ON pp.id = q.product_id
                      JOIN product_template pt ON pt.id = pp.product_tmpl_id
                     WHERE l.usage = 'internal' AND l.warehouse_id = ANY(%(wh)s)
                       AND q.quantity > 0 AND pt.active AND pp.active
                       {FILTRES}
                     GROUP BY 1, 2
                ), ventes AS (
                    SELECT pp.id AS var, spt.warehouse_id AS wh,
                           SUM(pol.qty) AS qte
                      FROM pos_order_line pol
                      JOIN pos_order po ON po.id = pol.order_id
                      JOIN pos_session ps ON ps.id = po.session_id
                      JOIN pos_config pc ON pc.id = ps.config_id
                      JOIN stock_picking_type spt ON spt.id = pc.picking_type_id
                      JOIN product_product pp ON pp.id = pol.product_id
                     WHERE po.state IN ('paid', 'done', 'invoiced')
                       AND spt.warehouse_id = ANY(%(wh)s)
                       AND po.date_order BETWEEN %(d1)s AND %(d2)s
                     GROUP BY 1, 2
                )
                SELECT COALESCE(s.var, v.var) AS var,
                       COALESCE(s.wh, v.wh) AS wh,
                       COALESCE(s.qte, 0) AS stock,
                       COALESCE(v.qte, 0) AS vendu
                  FROM stock s
                  FULL OUTER JOIN ventes v ON v.var = s.var AND v.wh = s.wh
            """.replace('{FILTRES}', filtres), params)
            brut = request.env.cr.fetchall()
            if not brut:
                return {'rows': [], 'paires': [], 'kpis': {}, 'params': p}

            par_var = {}
            for var_id, wh_id, stock, vendu in brut:
                if not var_id or not wh_id:
                    continue
                par_var.setdefault(var_id, {})[wh_id] = {
                    'stock': int(round(float(stock or 0))),
                    'vendu': int(round(float(vendu or 0))),
                }

            variant_recs = request.env['product.product'].sudo().browse(list(par_var))
            tmpl_de = {v.id: v.product_tmpl_id.id for v in variant_recs}
            variante_noms = {v.id: ', '.join(v.product_template_attribute_value_ids.mapped('name'))
                             for v in variant_recs}
            tmpl_ids = list(set(tmpl_de.values()))
            fiches = {t['id']: t for t in request.env['product.template'].sudo().search_read(
                [('id', 'in', tmpl_ids)],
                ['name', 'default_code', 'base_pivot_reference', 'list_price', 'categ_id'])}
            societe_ref = warehouses[0].company_id if warehouses else request.env.company
            ratios = {}
            for tmpl in request.env['product.template'].sudo().browse(tmpl_ids):
                ratios[tmpl.id] = self._solde_tax_ratio(tmpl, societe_ref)
            noms_wh = {w.id: (wh_labels.get(w.id) or w.name) for w in warehouses}
            champs_wh = {}
            societes_wh = {}
            villes_wh = {}
            for m in self._get_active_shop_mappings():
                if m.warehouse_id and m.warehouse_id.id in noms_wh:
                    champs_wh[m.warehouse_id.id] = m.shop_field
                    societes_wh[m.warehouse_id.id] = m.company_id.name or ''
                    villes_wh[m.warehouse_id.id] = (m.city or '').strip()

            recus = self._transferts_recus_recemment(p['fenetre'])
            attente = self._transferts_en_attente()
            flux = self._transferts_flux_recents(p['fenetre'])
            rows = []
            for var_id, magasins in par_var.items():
                tmpl_id = tmpl_de[var_id]
                fiche = fiches.get(tmpl_id) or {}
                prix = float(fiche.get('list_price') or 0.0) * ratios.get(tmpl_id, 1.0)
                donneurs, demandeurs = [], []
                for wh_id, d in magasins.items():
                    if wh_id not in noms_wh or wh_id not in champs_wh:
                        continue
                    vitesse = d['vendu'] / float(p['fenetre']) if d['vendu'] else 0.0
                    couverture = (d['stock'] / vitesse) if vitesse else None
                    cible_pieces = vitesse * p['cible']
                    if vitesse > 0 and (couverture or 0) < p['couverture_demandeur'] \
                            and (var_id, wh_id) not in recus:
                        # Il vend et va manquer : il demande.
                        besoin = int(round(cible_pieces - d['stock']))
                        if besoin > 0:
                            demandeurs.append({
                                'wh': wh_id, 'besoin': besoin, 'stock': d['stock'],
                                'vendu': d['vendu'],
                                'couverture': int(round(couverture)) if couverture else 0,
                            })
                    elif d['stock'] > 0 and (var_id, wh_id) not in recus and (couverture is None
                                             or couverture >= p['couverture_donneur']):
                        # Il dort dessus : il peut ceder, sans descendre
                        # sous ce qu'il lui faut pour tenir la cible.
                        cessible = int(round(d['stock'] - cible_pieces))
                        if cessible > 0:
                            donneurs.append({
                                'wh': wh_id, 'cessible': cessible, 'stock': d['stock'],
                                'vendu': d['vendu'],
                                'couverture': (int(round(couverture))
                                               if couverture is not None else None),
                            })
                if not donneurs or not demandeurs:
                    continue
                # Le besoin le plus criant d'abord, servi par le magasin qui
                # peut le plus s'en passer.
                demandeurs.sort(key=lambda x: (x['couverture'], -x['besoin']))
                donneurs.sort(key=lambda x: -x['cessible'])
                restes = {d['wh']: d['cessible'] for d in donneurs}
                for dem in demandeurs:
                    manque = dem['besoin']
                    for don in donneurs:
                        if manque <= 0:
                            break
                        dispo = restes.get(don['wh'], 0)
                        if dispo <= 0:
                            continue
                        qte = min(manque, dispo)
                        if qte < p['min_qte']:
                            continue
                        restes[don['wh']] = dispo - qte
                        manque -= qte
                        meme_ville = (villes_wh.get(don['wh'])
                                      and villes_wh.get(don['wh']) == villes_wh.get(dem['wh']))
                        rows.append({
                            'article_id': tmpl_id,
                            'variant_id': var_id,
                            'variante': variante_noms.get(var_id, ''),
                            'en_attente': (var_id, don['wh'], dem['wh']) in attente,
                            'aller_retour': (var_id, dem['wh'], don['wh']) in flux,
                            'reference': (fiche.get('base_pivot_reference')
                                          or fiche.get('default_code')
                                          or fiche.get('name') or '—'),
                            'produit': fiche.get('name') or '—',
                            'categorie': (fiche['categ_id'][1]
                                          if fiche.get('categ_id') else ''),
                            'source_wh': don['wh'],
                            'source': noms_wh.get(don['wh'], '?'),
                            'source_field': champs_wh.get(don['wh']),
                            'source_societe': societes_wh.get(don['wh'], ''),
                            'source_stock': don['stock'],
                            'source_vendu': don['vendu'],
                            'source_couverture': don['couverture'],
                            'dest_wh': dem['wh'],
                            'destination': noms_wh.get(dem['wh'], '?'),
                            'dest_field': champs_wh.get(dem['wh']),
                            'dest_societe': societes_wh.get(dem['wh'], ''),
                            'dest_stock': dem['stock'],
                            'dest_vendu': dem['vendu'],
                            'dest_couverture': dem['couverture'],
                            'quantite': qte,
                            'besoin': dem['besoin'],
                            'prix_ttc': round(prix, 2),
                            'valeur': round(qte * prix, 2),
                            'meme_societe': (societes_wh.get(don['wh'])
                                             == societes_wh.get(dem['wh'])),
                            'meme_ville': bool(meme_ville),
                            # Rouge : le demandeur est deja a sec ou presque.
                            'urgence': ('rupture' if dem['stock'] <= 0
                                        else ('critique' if dem['couverture'] <= 7
                                              else 'normale')),
                        })

            rang = {'rupture': 0, 'critique': 1, 'normale': 2}
            rows.sort(key=lambda r: (rang.get(r['urgence'], 3), -r['valeur'],
                                     r['reference']))

            # NOUVEAU : le regroupement par paire de magasins. Un bon de
            # transfert porte plusieurs references ; raisonner ligne par
            # ligne ferait autant de camions que de references.
            paires = {}
            for r in rows:
                cle = '%s>%s' % (r['source_field'], r['dest_field'])
                d = paires.setdefault(cle, {
                    'cle': cle,
                    'source': r['source'], 'source_field': r['source_field'],
                    'source_societe': r['source_societe'],
                    'destination': r['destination'], 'dest_field': r['dest_field'],
                    'dest_societe': r['dest_societe'],
                    'meme_societe': r['meme_societe'], 'meme_ville': r['meme_ville'],
                    'references': 0, 'pieces': 0, 'valeur': 0.0, 'urgentes': 0,
                })
                d['references'] += 1
                d['pieces'] += r['quantite']
                d['valeur'] += r['valeur']
                d['urgentes'] += 1 if r['urgence'] != 'normale' else 0
            paires = sorted(paires.values(), key=lambda d: -d['valeur'])
            for d in paires:
                d['valeur'] = round(d['valeur'], 2)

            # La matrice des flux : expediteur x destinataire. C'est la
            # forme naturelle d'un reseau de magasins — on y lit d'un coup
            # qui porte le stock de qui.
            ordre = sorted(noms_wh, key=lambda w: noms_wh[w])
            cases = {}
            for r in rows:
                cle = (r['source_wh'], r['dest_wh'])
                c = cases.setdefault(cle, {'pieces': 0, 'valeur': 0.0,
                                           'references': 0, 'urgentes': 0})
                c['pieces'] += r['quantite']
                c['valeur'] += r['valeur']
                c['references'] += 1
                c['urgentes'] += 1 if r['urgence'] != 'normale' else 0
            matrice = {
                'magasins': [{
                    'wh': w, 'nom': noms_wh[w], 'field': champs_wh.get(w),
                    'societe': societes_wh.get(w, ''), 'ville': villes_wh.get(w, ''),
                } for w in ordre if champs_wh.get(w)],
                'cases': [{
                    'source_wh': a, 'dest_wh': b,
                    'pieces': c['pieces'], 'valeur': round(c['valeur'], 2),
                    'references': c['references'], 'urgentes': c['urgentes'],
                } for (a, b), c in cases.items()],
            }

            # Le bilan de chaque magasin : ce qu'il donne, ce qu'il recoit.
            bilans = {}
            for w in ordre:
                if not champs_wh.get(w):
                    continue
                bilans[w] = {
                    'wh': w, 'nom': noms_wh[w], 'field': champs_wh.get(w),
                    'societe': societes_wh.get(w, ''), 'ville': villes_wh.get(w, ''),
                    'envoie': 0, 'envoie_refs': 0, 'envoie_valeur': 0.0,
                    'recoit': 0, 'recoit_refs': 0, 'recoit_valeur': 0.0,
                    'urgentes': 0,
                }
            for r in rows:
                b = bilans.get(r['source_wh'])
                if b:
                    b['envoie'] += r['quantite']
                    b['envoie_refs'] += 1
                    b['envoie_valeur'] += r['valeur']
                b = bilans.get(r['dest_wh'])
                if b:
                    b['recoit'] += r['quantite']
                    b['recoit_refs'] += 1
                    b['recoit_valeur'] += r['valeur']
                    b['urgentes'] += 1 if r['urgence'] != 'normale' else 0
            for b in bilans.values():
                b['solde'] = b['recoit'] - b['envoie']
                b['envoie_valeur'] = round(b['envoie_valeur'], 2)
                b['recoit_valeur'] = round(b['recoit_valeur'], 2)
                # Un magasin qui ne fait qu'envoyer porte le stock des
                # autres ; un magasin qui ne fait que recevoir etait affame.
                b['role'] = ('donneur' if b['envoie'] and not b['recoit']
                             else ('receveur' if b['recoit'] and not b['envoie']
                                   else ('equilibre' if b['envoie'] or b['recoit']
                                         else 'inactif')))
            bilan = sorted(bilans.values(),
                           key=lambda b: (-(b['envoie'] + b['recoit']), b['nom']))

            return {
                'params': p,
                'date_reference': ref_date.isoformat(),
                'date_debut': debut.isoformat(),
                'rows': rows[:self.TRANSFERTS_MAX_LIGNES],
                'paires': paires,
                'matrice': matrice,
                'bilan': bilan,
                'tronque': len(rows) > self.TRANSFERTS_MAX_LIGNES,
                'nb_lignes_total': len(rows),
                'kpis': {
                    'nb_propositions': len(rows),
                    'nb_references': len({r['article_id'] for r in rows}),
                    'nb_paires': len(paires),
                    'pieces': sum(r['quantite'] for r in rows),
                    'valeur': round(sum(r['valeur'] for r in rows), 2),
                    'nb_rupture': sum(1 for r in rows if r['urgence'] == 'rupture'),
                    'pieces_rupture': sum(r['quantite'] for r in rows
                                          if r['urgence'] == 'rupture'),
                },
            }
        except Exception as e:  # noqa: BLE001
            _logger.error("Erreur api_transferts_proposition: %s", e, exc_info=True)
            return {'error': str(e)}

    TRANSFERTS_QUESTIONS = [
        {'id': 'qui_manque', 'texte': 'Qui a le plus besoin ?',
         'aide': "Les magasins les plus courts, ceux à réapprovisionner en priorité."},
        {'id': 'qui_donne', 'texte': 'Qui peut donner le plus ?',
         'aide': "Les magasins qui dorment sur du stock et peuvent dépanner les autres."},
        {'id': 'categories_bouger', 'texte': 'Quelles catégories bouger en premier ?',
         'aide': "Où se concentre le volume à déplacer, par famille d'articles."},
        {'id': 'paires_prioritaires', 'texte': 'Quelles liaisons prioriser ?',
         'aide': "Les trajets magasin à magasin qui pèsent le plus lourd."},
        {'id': 'ruptures', 'texte': 'Qui est déjà en rupture ?',
         'aide': "Les magasins où une référence n'a plus une seule pièce."},
    ]

    def _bilan_flux(self, rows):
        """Pour chaque magasin : ce qu'il recoit et ce qu'il cede, sur les lignes donnees."""
        bilan = {}

        def case(champ, nom, societe):
            return bilan.setdefault(champ, {'field': champ, 'nom': nom, 'societe': societe,
                                            'recoit': 0, 'recoit_refs': 0, 'urgentes': 0,
                                            'envoie': 0, 'envoie_refs': 0})
        for r in rows:
            d = case(r['dest_field'], r['destination'], r['dest_societe'])
            d['recoit'] += r['quantite']
            d['recoit_refs'] += 1
            d['urgentes'] += 1 if r['urgence'] != 'normale' else 0
            s = case(r['source_field'], r['source'], r['source_societe'])
            s['envoie'] += r['quantite']
            s['envoie_refs'] += 1
        return list(bilan.values())

    def _paires_flux(self, rows):
        paires = {}
        for r in rows:
            cle = '%s>%s' % (r['source_field'], r['dest_field'])
            d = paires.setdefault(cle, {
                'cle': cle, 'source': r['source'], 'destination': r['destination'],
                'source_societe': r['source_societe'], 'dest_societe': r['dest_societe'],
                'meme_societe': r['meme_societe'], 'references': 0, 'pieces': 0,
                'valeur': 0.0, 'urgentes': 0})
            d['references'] += 1
            d['pieces'] += r['quantite']
            d['valeur'] += r['valeur']
            d['urgentes'] += 1 if r['urgence'] != 'normale' else 0
        return sorted(paires.values(), key=lambda d: -d['valeur'])

    @http.route('/mavie/api/transferts-assistant', type='json', auth='user',
                methods=['POST'], csrf=False)
    def api_transferts_assistant(self, **kw):
        """Répond à UNE question fermée sur les transferts."""
        try:
            question = (kw.get('question') or '').strip()
            if not question:
                return {'questions': self.TRANSFERTS_QUESTIONS}
            data = self.api_transferts_proposition(**kw)
            if data.get('error'):
                return {'error': data['error']}
            rows = data.get('rows') or []
            bilan = self._bilan_flux(rows)
            paires = self._paires_flux(rows)

            if question == 'qui_manque':
                demandeurs = [b for b in bilan if b['recoit'] > 0]
                demandeurs.sort(key=lambda b: -b['recoit'])
                return {
                    'titre': 'Qui a le plus besoin',
                    'resume': '',
                    'colonnes': ['Magasin', 'Société', 'À recevoir', 'Variantes',
                                 'Urgentes'],
                    'lignes': [[b['nom'], b['societe'], _fr_nombre(b['recoit']),
                                _fr_nombre(b['recoit_refs']), _fr_nombre(b['urgentes'])]
                               for b in demandeurs],
                    'cles': ['recoit:%s' % b['field'] for b in demandeurs],
                    'aide_clic': "Cliquez un magasin pour voir ce qu'il doit recevoir.",
                    'graphique': {
                        'type': 'barres', 'unite': 'pièces', 'libelle_refs': 'variante',
                        'items': [{
                            'nom': b['nom'], 'sous': b['societe'],
                            'valeur': b['recoit'], 'refs': b['recoit_refs'],
                            'alerte': b['urgentes'], 'cle': 'recoit:%s' % b['field'],
                            'detail': ('%s pièces à recevoir · %s variantes'
                                       % (_fr_nombre(b['recoit']),
                                          _fr_nombre(b['recoit_refs']))),
                        } for b in demandeurs],
                        'legende': ("longueur = pièces à recevoir · « réf. » = "
                                    "variantes concernées · la pastille rouge = "
                                    "dont des ruptures"),
                    },
                }

            if question == 'qui_donne':
                donneurs = [b for b in bilan if b['envoie'] > 0]
                donneurs.sort(key=lambda b: -b['envoie'])
                return {
                    'titre': 'Qui peut donner le plus',
                    'resume': '',
                    'colonnes': ['Magasin', 'Société', 'À donner', 'Variantes'],
                    'lignes': [[b['nom'], b['societe'], _fr_nombre(b['envoie']),
                                _fr_nombre(b['envoie_refs'])]
                               for b in donneurs],
                    'cles': ['envoie:%s' % b['field'] for b in donneurs],
                    'aide_clic': "Cliquez un magasin pour voir ce qu'il peut donner.",
                    'graphique': {
                        'type': 'barres', 'unite': 'pièces', 'libelle_refs': 'variante',
                        'items': [{
                            'nom': b['nom'], 'sous': b['societe'],
                            'valeur': b['envoie'], 'refs': b['envoie_refs'], 'alerte': 0,
                            'cle': 'envoie:%s' % b['field'],
                            'detail': ('%s pièces cessibles · %s variantes'
                                       % (_fr_nombre(b['envoie']),
                                          _fr_nombre(b['envoie_refs']))),
                        } for b in donneurs],
                        'legende': "longueur = pièces qu'il peut donner sans se mettre en rupture",
                    },
                }

            if question == 'categories_bouger':
                par_cat = {}
                for r in rows:
                    d = par_cat.setdefault(r['categorie'] or 'Sans catégorie',
                                           {'refs': 0, 'pieces': 0, 'valeur': 0.0})
                    d['refs'] += 1
                    d['pieces'] += r['quantite']
                    d['valeur'] += r['valeur']
                # Tri par pieces, pas par valeur : c'est ce que montre le
                # graphique (unite 'pieces'), les deux doivent coincider.
                classe = sorted(par_cat.items(), key=lambda kv: -kv[1]['pieces'])
                return {
                    'titre': 'Quelles catégories bouger en premier',
                    'resume': '',
                    'colonnes': ['Catégorie', 'Variantes', 'Pièces à déplacer'],
                    'lignes': [[nom, _fr_nombre(d['refs']), _fr_nombre(d['pieces'])]
                               for nom, d in classe],
                    'cles': ['cat:%s' % nom for nom, _d in classe],
                    'aide_clic': "Cliquez une catégorie pour voir ses variantes.",
                    'graphique': {
                        'type': 'carte', 'unite': 'pièces',
                        'items': [{
                            'nom': nom, 'valeur': d['pieces'], 'cle': 'cat:%s' % nom,
                            'detail': '%s variantes' % _fr_nombre(d['refs']),
                        } for nom, d in classe],
                    },
                }

            if question == 'paires_prioritaires':
                top = paires[:12]
                return {
                    'titre': 'Quelles liaisons prioriser',
                    'resume': '',
                    'colonnes': ['De', 'Vers', 'Variantes', 'Pièces'],
                    'lignes': [['%s (%s)' % (d['source'], d['source_societe']),
                                '%s (%s)' % (d['destination'], d['dest_societe']),
                                _fr_nombre(d['references']), _fr_nombre(d['pieces'])]
                               for d in top],
                    'cles': ['paire:%s' % d['cle'] for d in top],
                    'aide_clic': "Cliquez une liaison pour voir ce qu'elle transporte.",
                    'graphique': {
                        'type': 'barres', 'unite': 'pièces', 'libelle_refs': 'variante',
                        'items': [{
                            'nom': '%s \u2192 %s' % (d['source'], d['destination']),
                            'sous': ('interne' if d['meme_societe']
                                     else 'inter-sociétés'),
                            'valeur': d['pieces'], 'refs': d['references'],
                            'alerte': d['urgentes'], 'cle': 'paire:%s' % d['cle'],
                            'detail': ('%s variantes · %s pièces'
                                       % (_fr_nombre(d['references']),
                                          _fr_nombre(d['pieces']))),
                        } for d in top],
                        'legende': ("longueur = pièces à transporter sur cette liaison · "
                                    "la pastille rouge = dont des urgentes"),
                    },
                }

            if question == 'ruptures':
                rupt = [r for r in rows if r['urgence'] == 'rupture']
                par_mag = {}
                for r in rupt:
                    d = par_mag.setdefault(r['dest_field'],
                                           {'nom': r['destination'], 'champ': r['dest_field'],
                                            'refs': 0, 'pieces': 0})
                    d['refs'] += 1
                    d['pieces'] += r['quantite']
                classe = sorted(par_mag.values(), key=lambda d: -d['refs'])
                return {
                    'titre': 'Qui est déjà en rupture',
                    'resume': '',
                    'colonnes': ['Magasin', 'Variantes en rupture', 'Pièces à envoyer'],
                    'lignes': [[d['nom'], _fr_nombre(d['refs']), _fr_nombre(d['pieces'])]
                               for d in classe],
                    'cles': ['rupture:%s' % d['champ'] for d in classe],
                    'graphique': {
                        'type': 'colonnes', 'unite': 'variantes',
                        'items': [{
                            'nom': d['nom'], 'valeur': d['refs'],
                            'cle': 'rupture:%s' % d['champ'],
                            'fort': d is classe[0] if classe else False,
                            'detail': '%s pièces à envoyer' % _fr_nombre(d['pieces']),
                        } for d in classe],
                        'legende': "hauteur = nombre de variantes en rupture dans ce magasin",
                    },
                }

            return {'error': 'Question inconnue.'}
        except Exception as e:  # noqa: BLE001
            _logger.error("Erreur api_transferts_assistant: %s", e, exc_info=True)
            return {'error': str(e)}

    @http.route('/mavie/api/transferts-assistant-detail', type='json', auth='user',
                methods=['POST'], csrf=False)
    def api_transferts_assistant_detail(self, **kw):
        """Ce qu'il y a derrière une ligne de l'assistant des transferts."""
        try:
            cle = (kw.get('cle') or '').strip()
            if not cle:
                return {'error': "Aucun détail pour cette ligne."}
            if cle.startswith('bon:'):
                bon = request.env['inter.internal.transfer'].sudo().browse(int(cle[4:]))
                if not bon.exists() or not bon.created_from_dashboard:
                    return {'error': 'Bon introuvable.'}
                etats = dict(bon._fields['state'].selection)

                def nom_magasin(loc):
                    return loc.warehouse_id.name or loc.display_name
                etat = etats.get(bon.state, bon.state)
                lignes, cles = [], []
                magasin = (kw.get('magasin_bon') or '').strip()
                for ligne in bon.line_ids:
                    tmpl = ligne.product_id.product_tmpl_id
                    sens = ('Sortie' if nom_magasin(bon.location_source_id) == magasin
                            else ('Entrée' if nom_magasin(bon.location_target_id) == magasin else '—')) if magasin else ''
                    lignes.append([tmpl.base_pivot_reference or tmpl.default_code or tmpl.name,
                                   ', '.join(ligne.product_id.product_template_attribute_value_ids.mapped('name')) or '—',
                                   _fr_nombre(ligne.quantity), etat] + ([sens] if magasin else []))
                    cles.append('ref:%s' % tmpl.id)
                return {
                    'titre': 'Bon %s : %s → %s' % (bon.name, nom_magasin(bon.location_source_id),
                                                   nom_magasin(bon.location_target_id)),
                    'etat': etat,
                    'dates': [['Créé', fields.Datetime.to_string(bon.create_date)],
                              ['Fait', fields.Datetime.to_string(bon.date_validation)],
                              ['Reçu', fields.Datetime.to_string(bon.received_date)]],
                    'resume': '',
                    'colonnes': ['Article', 'Variante', 'Quantité', 'État du bon'] + (['Sens'] if magasin else []),
                    'lignes': lignes,
                    'cles': cles,
                }

            data = self.api_transferts_proposition(**kw)
            if data.get('error'):
                return {'error': data['error']}
            rows = data.get('rows') or []

            if cle.startswith('recoit:'):
                champ = cle[7:]
                choix = sorted([r for r in rows if r['dest_field'] == champ],
                               key=lambda r: -r['quantite'])
                titre = 'Ce que %s doit recevoir' % (choix[0]['destination'] if choix else champ)
            elif cle.startswith('envoie:'):
                champ = cle[7:]
                choix = sorted([r for r in rows if r['source_field'] == champ],
                               key=lambda r: -r['quantite'])
                titre = 'Ce que %s peut donner' % (choix[0]['source'] if choix else champ)
            elif cle.startswith('cat:'):
                nom = cle[4:]
                choix = sorted([r for r in rows
                                if (r['categorie'] or 'Sans catégorie') == nom],
                               key=lambda r: -r['quantite'])
                titre = 'Catégorie %s' % nom
            elif cle.startswith('paire:'):
                paire_cle = cle[6:]
                choix = sorted([r for r in rows
                                if '%s>%s' % (r['source_field'], r['dest_field']) == paire_cle],
                               key=lambda r: -r['quantite'])
                titre = ('%s \u2192 %s' % (choix[0]['source'], choix[0]['destination'])
                         if choix else 'Liaison')
            elif cle.startswith('rupture:'):
                champ = cle[8:]
                choix = sorted([r for r in rows
                                if r['dest_field'] == champ and r['urgence'] == 'rupture'],
                               key=lambda r: -r['quantite'])
                titre = ('En rupture chez %s' % choix[0]['destination'] if choix
                         else 'En rupture')
            else:
                return {'error': "Détail inconnu pour cette ligne."}

            if not choix:
                return {'titre': titre, 'resume': "Rien à afficher.",
                        'colonnes': [], 'lignes': []}
            return {
                'titre': titre,
                'resume': ("%s variantes · %s pièces"
                          % (_fr_nombre(len(choix)),
                             _fr_nombre(sum(r['quantite'] for r in choix)))),
                'colonnes': ['Article et variante', 'Catégorie', 'De', 'Vers', 'À déplacer'],
                'lignes': [[r['reference'] + (' · ' + r['variante'] if r['variante'] else ''),
                            r['categorie'] or '—', r['source'],
                            r['destination'], _fr_nombre(r['quantite'])]
                           for r in choix[:self.ASSISTANT_DETAIL_MAX]],
                'cles': ['ref:%s' % r['article_id']
                         for r in choix[:self.ASSISTANT_DETAIL_MAX]],
            }
        except Exception as e:  # noqa: BLE001
            _logger.error("Erreur api_transferts_assistant_detail: %s", e, exc_info=True)
            return {'error': str(e)}

    @http.route('/mavie/api/transferts-bons', type='json', auth='user',
                methods=['POST'], csrf=False)
    def api_transferts_bons(self, **kw):
        """Les bons crees depuis le tableau de bord, avec leur etat."""
        try:
            T = request.env['inter.internal.transfer'].sudo()
            etats = dict(T._fields['state'].selection)
            bons = T.search([('created_from_dashboard', '=', True)], order='id desc', limit=200)

            def quand(dt):
                # UTC brut : le navigateur l'affiche dans l'heure de la machine.
                return fields.Datetime.to_string(dt) if dt else ''
            Wh = request.env['stock.warehouse'].sudo()

            def vendu_depuis(b):
                dest = Wh.search([('view_location_id', 'parent_of', b.location_target_id.id)], limit=1)
                variantes = b.line_ids.mapped('product_id').ids
                if not dest or not variantes or not b.create_date:
                    return 0
                request.env.cr.execute("""
                    SELECT COALESCE(SUM(pol.qty), 0)
                      FROM pos_order_line pol
                      JOIN pos_order po ON po.id = pol.order_id
                      JOIN pos_session ps ON ps.id = po.session_id
                      JOIN pos_config pc ON pc.id = ps.config_id
                      JOIN stock_picking_type spt ON spt.id = pc.picking_type_id
                     WHERE po.state IN ('paid', 'done', 'invoiced')
                       AND spt.warehouse_id = %s
                       AND pol.product_id = ANY(%s)
                       AND po.date_order >= %s""", (dest.id, variantes, b.create_date))
                return int(round(float(request.env.cr.fetchone()[0] or 0)))

            return {'bons': [{
                'id': b.id,
                'name': b.name,
                'etat': etats.get(b.state, b.state),
                'source': b.location_source_id.warehouse_id.name or b.location_source_id.display_name,
                'dest': b.location_target_id.warehouse_id.name or b.location_target_id.display_name,
                'pieces': sum(b.line_ids.mapped('quantity')),
                'vendu': vendu_depuis(b),
                'valide': b.state == 'done',
                'lignes': [{'variante_id': l.product_id.id,
                            'ref': (l.product_id.product_tmpl_id.base_pivot_reference
                                    or l.product_id.product_tmpl_id.default_code
                                    or l.product_id.product_tmpl_id.name),
                            'variante': ', '.join(l.product_id.product_template_attribute_value_ids.mapped('name')) or '—',
                            'qte': l.quantity} for l in b.line_ids],
                'fait': quand(b.date_validation),
                'cree': quand(b.create_date),
                'recu': quand(b.received_date),
            } for b in bons]}
        except Exception as e:  # noqa: BLE001
            _logger.error("Erreur api_transferts_bons: %s", e, exc_info=True)
            return {'error': str(e)}

    @http.route('/mavie/transfer-bon-pdf/<int:transfer_id>', type='http', auth='user',
                methods=['GET'], csrf=False)
    def api_transfer_bon_pdf(self, transfer_id, **kw):
        """Le bon de transfert en PDF, pour les bons crees depuis le tableau de bord.
        Rendu en sudo, comme le PDF de Soldes : le tableau de bord donne deja acces
        aux transferts, la permission du menu ne doit pas bloquer l'impression."""
        bon = request.env['inter.internal.transfer'].sudo().browse(transfer_id)
        if not bon.exists() or not bon.created_from_dashboard:
            return request.not_found()
        try:
            pdf, _fmt = request.env['ir.actions.report'].sudo()._render_qweb_pdf(
                'mavie_dashboard.action_report_transfer', [bon.id])
        except Exception as e:  # noqa: BLE001
            _logger.error("Erreur PDF bon transfert %s: %s", bon.name, e, exc_info=True)
            return request.make_response(
                "Le bon n'a pas pu être généré : %s" % e,
                [('Content-Type', 'text/plain; charset=utf-8')])
        nom = re.sub(r'[^\w.-]+', '_', 'Bon_transfert_%s' % bon.name) + '.pdf'
        return request.make_response(pdf, headers=[
            ('Content-Type', 'application/pdf'),
            ('Content-Length', len(pdf)),
            ('Content-Disposition', 'inline; filename="%s"' % nom),
        ])

    @http.route('/mavie/transfer-bons-pdf', type='http', auth='user',
                methods=['GET'], csrf=False)
    def api_transfer_bons_pdf(self, ids='', **kw):
        """Plusieurs bons crees depuis le tableau de bord, en un seul PDF (rendu en sudo)."""
        ids_ok = [int(x) for x in (ids or '').split(',') if x.strip().isdigit()]
        bons = request.env['inter.internal.transfer'].sudo().search(
            [('id', 'in', ids_ok), ('created_from_dashboard', '=', True)], order='id asc')
        if not bons:
            return request.not_found()
        try:
            pdf, _fmt = request.env['ir.actions.report'].sudo()._render_qweb_pdf(
                'mavie_dashboard.action_report_transfer', bons.ids)
        except Exception as e:  # noqa: BLE001
            _logger.error("Erreur PDF bons groupes: %s", e, exc_info=True)
            return request.make_response(
                "Les bons n'ont pas pu être générés : %s" % e,
                [('Content-Type', 'text/plain; charset=utf-8')])
        return request.make_response(pdf, headers=[
            ('Content-Type', 'application/pdf'),
            ('Content-Length', len(pdf)),
            ('Content-Disposition', 'inline; filename="Bons_transfert.pdf"'),
        ])

    @http.route('/mavie/api/transferts-bilan', type='json', auth='user',
                methods=['POST'], csrf=False)
    def api_transferts_bilan(self, **kw):
        """Bilan des bons faits depuis le tableau de bord."""
        try:
            bons = request.env['inter.internal.transfer'].sudo().search(
                [('created_from_dashboard', '=', True), ('state', '=', 'done')])
            delais = [(b.date_validation - b.create_date).total_seconds() / 86400.0
                      for b in bons if b.date_validation and b.create_date]
            pieces = sum(sum(b.line_ids.mapped('quantity')) for b in bons)
            Wh = request.env['stock.warehouse'].sudo()
            vendu = 0
            for b in bons:
                dest = Wh.search([('view_location_id', 'parent_of', b.location_target_id.id)], limit=1)
                variantes = b.line_ids.mapped('product_id').ids
                if not dest or not variantes or not b.create_date:
                    continue
                request.env.cr.execute("""
                    SELECT COALESCE(SUM(pol.qty), 0)
                      FROM pos_order_line pol
                      JOIN pos_order po ON po.id = pol.order_id
                      JOIN pos_session ps ON ps.id = po.session_id
                      JOIN pos_config pc ON pc.id = ps.config_id
                      JOIN stock_picking_type spt ON spt.id = pc.picking_type_id
                     WHERE po.state IN ('paid', 'done', 'invoiced')
                       AND spt.warehouse_id = %s
                       AND pol.product_id = ANY(%s)
                       AND po.date_order >= %s""", (dest.id, variantes, b.create_date))
                vendu += int(round(float(request.env.cr.fetchone()[0] or 0)))
            return {'nb': len(bons), 'pieces': pieces, 'vendu': vendu,
                    'delai_jours': round(sum(delais) / len(delais), 1) if delais else None}
        except Exception as e:  # noqa: BLE001
            _logger.error("Erreur api_transferts_bilan: %s", e, exc_info=True)
            return {'error': str(e)}

    @http.route('/mavie/soldes-bon-pdf', type='http', auth='user', methods=['GET'], csrf=False)
    def api_soldes_bon_pdf(self, **kw):
        """Le bon des soldes de l'opération en cours : chaque remise posée, avec sa photo."""
        try:
            op = self._solde_operation()
            if not op['active']:
                return request.make_response("Aucune opération de soldes en cours.",
                                             [('Content-Type', 'text/plain; charset=utf-8')])
            lignes = []
            for r in self.api_soldes_journal().get('rows') or []:
                if r.get('debut') != op['debut']:
                    continue
                photo = None
                tmpl = request.env['product.template'].sudo().browse(r.get('article_id') or 0).exists()
                if tmpl and tmpl.image_128:
                    try:
                        photo = 'data:image/png;base64,' + tmpl.image_128.decode()
                    except Exception:  # noqa: BLE001
                        photo = None
                lignes.append({
                    'photo': photo,
                    'reference': r.get('reference') or '',
                    'produit': r.get('produit') or '',
                    'variante': r.get('variante') or '—',
                    'magasin': r.get('magasin') or '',
                    'societe': r.get('societe') or '',
                    'catalogue': r.get('prix_catalogue') or '',
                    'solde': r.get('prix_solde') or '',
                    'remise': r.get('remise') or '',
                    'debut': r.get('debut') or '',
                    'fin': r.get('fin') or '—',
                    'vendu': r.get('vendu_depuis') or 0,
                })
            html = request.env['ir.qweb']._render('mavie_dashboard.report_soldes_template', {
                'titre': 'Bon de soldes — %s' % op['nom'],
                'sous_titre': 'du %s au %s' % (op['debut'], op['fin'] or 'sans date de fin'),
                'lignes': lignes,
            })
            pdf = request.env['ir.actions.report'].sudo()._run_wkhtmltopdf([html], landscape=True)
            return request.make_response(pdf, headers=[
                ('Content-Type', 'application/pdf'),
                ('Content-Length', len(pdf)),
                ('Content-Disposition', 'inline; filename="Bon_de_soldes.pdf"'),
            ])
        except Exception as e:  # noqa: BLE001
            _logger.error("Erreur bon de soldes PDF: %s", e, exc_info=True)
            return request.make_response("Le bon n'a pas pu être généré : %s" % e,
                                         [('Content-Type', 'text/plain; charset=utf-8')])

    CLE_OPERATION = 'mavie_dashboard.solde_operation'

    def _solde_operation(self):
        """L'opération de soldes en cours, ou rien.

        Stockée dans trois paramètres de configuration : aucun modèle à
        créer, et la retirer ne laisse aucune trace.
        """
        param = request.env['ir.config_parameter'].sudo()
        nom = (param.get_param(self.CLE_OPERATION + '_nom') or '').strip()
        debut = (param.get_param(self.CLE_OPERATION + '_debut') or '').strip()
        fin = (param.get_param(self.CLE_OPERATION + '_fin') or '').strip()
        cats = [int(x) for x in (param.get_param(self.CLE_OPERATION + '_categories') or '').split(',')
                if x.strip().isdigit()]
        if not nom or not debut:
            return {'active': False, 'nom': '', 'debut': '', 'fin': '', 'categories': []}
        aujourdhui = fields.Date.context_today(request.env.user).isoformat()
        return {
            'active': True,
            'nom': nom,
            'debut': debut,
            'fin': fin,
            'categories': cats,
            'categories_noms': request.env['product.category'].sudo().browse(cats).mapped('complete_name'),
            # Une opération dont la date de fin est passée ne pose plus
            # rien : on le dit, plutôt que de la laisser croire en cours.
            'terminee': bool(fin and fin < aujourdhui),
            'a_venir': debut > aujourdhui,
        }

    def _solde_dates_operation(self, date_start, date_end):
        """Les dates à poser : celles demandées, sinon celles de l'opération.

        Une remise posée pendant une opération en porte les dates — c'est
        tout l'intérêt d'en déclarer une.
        """
        op = self._solde_operation()
        if not date_start and op['active'] and not op.get('terminee'):
            return op['debut'], op['fin']
        return (date_start or fields.Date.context_today(request.env.user).isoformat(),
                date_end)

    @http.route('/mavie/api/soldes-categories', type='json', auth='user',
                methods=['POST'], csrf=False)
    def api_soldes_categories(self, **kw):
        cats = request.env['product.category'].sudo().search([], order='complete_name')
        return {'categories': [{'id': c.id, 'nom': c.complete_name} for c in cats]}

    @http.route('/mavie/api/solde-operation', type='json', auth='user',
                methods=['POST'], csrf=False)
    def api_solde_operation(self, **kw):
        """Lire, définir ou effacer l'opération de soldes en cours."""
        try:
            param = request.env['ir.config_parameter'].sudo()
            action = (kw.get('action') or 'lire').strip()
            if action == 'effacer':
                for suffixe in ('_nom', '_debut', '_fin', '_categories'):
                    param.set_param(self.CLE_OPERATION + suffixe, '')
                _logger.info("Operation de soldes effacee par %s",
                             request.env.user.login)
            elif action == 'definir':
                nom = (kw.get('nom') or '').strip()
                debut = (kw.get('debut') or '').strip()
                fin = (kw.get('fin') or '').strip()
                if not nom:
                    return {'error': "Donnez un nom à l'opération."}
                if not debut:
                    return {'error': "Donnez une date de début."}
                if fin and fin < debut:
                    return {'error': "La fin ne peut pas précéder le début."}
                param.set_param(self.CLE_OPERATION + '_nom', nom[:80])
                param.set_param(self.CLE_OPERATION + '_debut', debut)
                param.set_param(self.CLE_OPERATION + '_fin', fin)
                categories = [int(c) for c in (kw.get('categories') or []) if str(c).isdigit()]
                param.set_param(self.CLE_OPERATION + '_categories', ','.join(str(c) for c in categories))
                _logger.info("Operation de soldes « %s » du %s au %s, par %s",
                             nom, debut, fin or 'sans fin', request.env.user.login)
            elif action != 'lire':
                return {'error': "Action inconnue."}

            op = self._solde_operation()
            op['remises_posees'] = self._solde_remises_operation(op)
            return op
        except Exception as e:  # noqa: BLE001
            _logger.error("Erreur api_solde_operation: %s", e, exc_info=True)
            return {'error': str(e)}

    def _solde_remises_operation(self, op):
        """Combien de remises portent déjà les dates de cette opération."""
        if not op.get('active'):
            return 0
        listes = self._soldes_listes_par_magasin()
        if not listes:
            return 0
        domaine = [('pricelist_id', 'in', list(listes)),
                   ('compute_price', '=', 'fixed'),
                   ('date_start', '>=', op['debut'] + ' 00:00:00'),
                   ('date_start', '<=', op['debut'] + ' 23:59:59')]
        return request.env['product.pricelist.item'].sudo().search_count(domaine)

    @http.route('/mavie/api/soldes-journal', type='json', auth='user',
                methods=['POST'], csrf=False)
    def api_soldes_journal(self, **kw):
        """Les remises posées, la plus récente d'abord.

        À ne pas confondre avec la section « Historique » du tableau de
        bord, qui liste les VENTES faites sous le prix catalogue : ici on
        liste ce qui a été POSÉ, même si rien n'a encore été vendu.
        """
        try:
            listes = self._soldes_listes_par_magasin()
            if not listes:
                return {'rows': [], 'kpis': {}}
            Item = request.env['product.pricelist.item'].sudo()
            domaine = [('pricelist_id', 'in', list(listes)),
                       ('compute_price', '=', 'fixed')]
            if kw.get('date_start'):
                domaine.append(('write_date', '>=', kw['date_start'] + ' 00:00:00'))
            if kw.get('date_end'):
                domaine.append(('write_date', '<=', kw['date_end'] + ' 23:59:59'))
            regles = Item.search(domaine, order='write_date desc',
                                 limit=self.JOURNAL_SOLDES_MAX)
            if not regles:
                return {'rows': [], 'kpis': {}}

            # Ce que chaque remise a vendu depuis sa pose : c'est la seule
            # facon de savoir si elle a pris. Les regles posees le meme jour
            # partagent une requete, sinon on en ferait une par ligne.
            aujourdhui = fields.Date.context_today(request.env.user)
            paquets = {}
            for it in regles:
                tmpl = it.product_tmpl_id or it.product_id.product_tmpl_id
                info = listes.get(it.pricelist_id.id) or {}
                if not tmpl or not info.get('warehouse_ids'):
                    continue
                depuis = it.date_start or it.write_date or it.create_date
                jour = fields.Datetime.to_string(depuis)[:10] if depuis else str(aujourdhui)
                # Une liste partagee vaut pour tous ses magasins : les
                # ventes depuis la pose se comptent sur tous, sinon on
                # sous-estime l'effet de la remise.
                for wh_id in info['warehouse_ids']:
                    paquets.setdefault(jour, set()).add((tmpl.id, wh_id))
            vendu = {}
            for jour, paires in paquets.items():
                request.env.cr.execute("""
                    SELECT pp.product_tmpl_id, spt.warehouse_id,
                           SUM(pol.qty), SUM(pol.price_subtotal_incl)
                      FROM pos_order_line pol
                      JOIN pos_order po ON po.id = pol.order_id
                      JOIN pos_session ps ON ps.id = po.session_id
                      JOIN pos_config pc ON pc.id = ps.config_id
                      JOIN stock_picking_type spt ON spt.id = pc.picking_type_id
                      JOIN product_product pp ON pp.id = pol.product_id
                     WHERE po.state IN ('paid', 'done', 'invoiced')
                       AND po.date_order >= %s
                       AND pp.product_tmpl_id = ANY(%s)
                       AND spt.warehouse_id = ANY(%s)
                     GROUP BY 1, 2
                """, (jour + ' 00:00:00',
                      list({t for t, _w in paires}),
                      list({w for _t, w in paires})))
                for tmpl_id, wh_id, qte, montant in request.env.cr.fetchall():
                    if (tmpl_id, wh_id) in paires:
                        cle = (tmpl_id, wh_id, jour)
                        vendu[cle] = (int(round(float(qte or 0))), float(montant or 0))
            def _vendu(tmpl_id, wh_ids, jour):
                q = m = 0.0
                for w in wh_ids:
                    a, b = vendu.get((tmpl_id, w, jour), (0, 0.0))
                    q += a
                    m += b
                return int(q), m

            rows = []
            pieces_vendues = recette = 0.0
            for it in regles:
                tmpl = it.product_tmpl_id or it.product_id.product_tmpl_id
                info = listes.get(it.pricelist_id.id) or {}
                if not tmpl or not info:
                    continue
                societe = request.env['res.company'].sudo().browse(info['company_id'])
                ratio = self._solde_tax_ratio(tmpl, societe)
                magasins = info.get('magasins') or []
                prix_ttc = round(float(it.fixed_price or 0) * ratio, 2)
                catalogue = round(float(tmpl.list_price or 0) * ratio, 2)
                remise = (round((1 - prix_ttc / catalogue) * 100.0, 1)
                          if catalogue > 0 and prix_ttc <= catalogue else 0.0)
                depuis = it.date_start or it.write_date or it.create_date
                jour = fields.Datetime.to_string(depuis)[:10] if depuis else str(aujourdhui)
                q, m = _vendu(tmpl.id, info.get('warehouse_ids') or [], jour)
                pieces_vendues += q
                recette += m
                fin = fields.Datetime.to_string(it.date_end)[:10] if it.date_end else ''
                rows.append({
                    'id': it.id,
                    'pose_le': fields.Datetime.to_string(it.write_date)[:16]
                               if it.write_date else '',
                    'par': (it.write_uid or it.create_uid).name or '',
                    'article_id': tmpl.id,
                    'reference': (tmpl.base_pivot_reference or tmpl.default_code
                                  or tmpl.name or '—'),
                    'produit': tmpl.name or '',
                    'variante': it.product_id.display_name if it.product_id else '',
                    # Une liste partagee : on nomme tous les magasins
                    # concernes, sinon le journal laisse croire que la
                    # remise n'a touche qu'une boutique.
                    'magasin': (magasins[0] if len(magasins) == 1
                                else '%s magasins' % len(magasins)),
                    'magasins': magasins,
                    'partagee': len(magasins) > 1,
                    'societe': ', '.join(info.get('societes') or []),
                    'liste': info['liste'],
                    'prix_catalogue': catalogue,
                    'prix_solde': prix_ttc,
                    'remise': remise,
                    'debut': jour,
                    'fin': fin,
                    # Une remise sans date de fin court indefiniment ; une
                    # remise expiree ne s'applique plus en caisse.
                    'expiree': bool(fin and fin < str(aujourdhui)),
                    'vendu_depuis': q,
                    'recette_depuis': round(m, 2),
                })
            actives = [r for r in rows if not r['expiree']]
            sans_effet = [r for r in actives if not r['vendu_depuis']]
            return {
                'rows': rows,
                'kpis': {
                    'nb_regles': len(rows),
                    'nb_actives': len(actives),
                    'nb_expirees': len(rows) - len(actives),
                    'nb_references': len({r['article_id'] for r in rows}),
                    'nb_magasins': len({m for r in rows for m in (r['magasins'] or [])}),
                    'nb_sans_effet': len(sans_effet),
                    'pieces_vendues': int(pieces_vendues),
                    'recette': round(recette, 2),
                },
                'tronque': len(regles) >= self.JOURNAL_SOLDES_MAX,
            }
        except Exception as e:  # noqa: BLE001
            _logger.error("Erreur api_soldes_journal: %s", e, exc_info=True)
            return {'error': str(e)}

    @http.route('/mavie/api/assistant-detail', type='json', auth='user',
                methods=['POST'], csrf=False)
    def api_assistant_detail(self, **kw):
        """Ce qu'il y a derrière UNE ligne de réponse de l'assistant.

        La clé dit de quoi il s'agit : `ref:12` une référence (on montre les
        magasins exacts), `mag:shop_04` un magasin, `cat:Sacs` une
        catégorie, sinon un palier d'ancienneté ou un total.
        """
        try:
            cle = (kw.get('cle') or '').strip()
            if not cle:
                return {'error': "Aucun détail pour cette ligne."}
            data = self.api_soldes_proposition(**kw)
            if data.get('error'):
                return {'error': data['error']}
            rows = data.get('rows') or []
            p = data.get('params') or {}
            a_traiter = [r for r in rows if not r['deja_solde']]

            # Une référence : les magasins exacts où la remise peut être
            # posée, et ceux qui en détiennent sans pouvoir la recevoir.
            if cle.startswith('ref:'):
                try:
                    aid = int(cle[4:])
                except ValueError:
                    return {'error': "Référence illisible."}
                ligne = ([r for r in rows if r['article_id'] == aid] or [None])[0]
                if not ligne:
                    return {'error': "Cette référence n'est plus dans la liste."}
                lignes = []
                for m in (ligne.get('magasins') or []):
                    lignes.append([
                        m['libelle'], m['societe'], _fr_nombre(m['stock']),
                        '%s MAD' % _fr_nombre(m['stock'] * ligne['prix_solde']),
                        'oui' if m['soldable'] else 'non — aucune caisse',
                    ])
                sans = sum(1 for m in (ligne.get('magasins') or []) if not m['soldable'])
                portee = self._soldes_portee_reelle()
                avert = ''
                if portee['partagees']:
                    avert = (" Attention : la liste de prix « %s » est partagée par %s "
                             "magasins (%s). Une remise posée s'y applique dans TOUS ces "
                             "magasins à la fois — décocher n'en épargne aucun."
                             % (portee['partagees'][0]['liste'],
                                len(portee['partagees'][0]['magasins']),
                                ', '.join(portee['partagees'][0]['magasins'])))
                return {
                    'portee': portee,
                    'avertissement': avert,
                    'titre': '%s — où solder' % ligne['reference'],
                    'resume': ("%s. Remise conseillée −%s %% : %s MAD au lieu de %s MAD. "
                               "La remise peut être posée dans %s magasin(s) sur %s.%s"
                               % (ligne['produit'], ligne['remise'],
                                  _fr_nombre(ligne['prix_solde']), _fr_nombre(ligne['prix_ttc']),
                                  ligne['nb_soldables'], ligne['nb_magasins'],
                                  ((" %s magasin(s) détiennent du stock sans caisse : "
                                    "il faut y déplacer la marchandise." % sans)
                                   if sans else '') + avert)),
                    'colonnes': ['Magasin', 'Société', 'Stock', 'Valeur soldée',
                                 'Remise possible'],
                    'lignes': lignes,
                    'article_id': aid,
                    'reference': ligne['reference'],
                    'remise': ligne['remise'],
                }

            # Une tranche de remise : ce qui y a deja ete solde. Cette
            # question-la regarde le PASSE, pas les propositions — son
            # detail sort donc du journal des remises posees.
            if cle.startswith('bande:'):
                try:
                    de, a = [int(x) for x in cle[6:].split('-')]
                except (TypeError, ValueError):
                    return {'error': "Tranche de remise illisible."}
                journal = self.api_soldes_journal()
                dedans = [r for r in (journal.get('rows') or [])
                          if de <= int(round(r['remise'])) <= a]
                # Ou la remise peut etre posee aujourd'hui : l'information
                # vient de la liste des propositions, pas du journal.
                ou = {x['article_id']: x['nb_soldables'] for x in rows}
                dedans.sort(key=lambda r: -r['vendu_depuis'])
                vendu = sum(r['vendu_depuis'] for r in dedans)
                muettes = [r for r in dedans if not r['vendu_depuis'] and not r['expiree']]
                return {
                    'titre': 'Soldé entre %s et %s %%' % (de, a),
                    'resume': ("%s remises posées · %s pièces vendues depuis · %s n'ont "
                               "encore rien vendu."
                               % (_fr_nombre(len(dedans)), _fr_nombre(vendu),
                                  _fr_nombre(len(muettes)))),
                    'colonnes': ['Référence', 'Où solder', 'Catalogue', 'Prix soldé',
                                 'Remise', 'Posée le', 'Vendu depuis', 'État'],
                    'lignes': [[r['reference'],
                                '%s magasin%s' % (ou.get(r['article_id'], 0),
                                                  's' if ou.get(r['article_id'], 0) > 1
                                                  else ''),
                                '%s MAD' % _fr_nombre(r['prix_catalogue']),
                                '%s MAD' % _fr_nombre(r['prix_solde']),
                                '−%s %%' % _fr_nombre(r['remise']),
                                r['debut'], _fr_nombre(r['vendu_depuis']),
                                ('terminée' if r['expiree']
                                 else ('vendu' if r['vendu_depuis']
                                       else 'rien vendu'))]
                               for r in dedans[:self.ASSISTANT_DETAIL_MAX]],
                    'cles': ['ref:%s' % r['article_id']
                             for r in dedans[:self.ASSISTANT_DETAIL_MAX]],
                    'aide_clic': "Cliquez une référence pour voir où elle est soldée.",
                    'tronque': len(dedans) > self.ASSISTANT_DETAIL_MAX,
                    'nb_total': len(dedans),
                }

            # Un magasin, une catégorie, un palier ou un total : dans tous
            # les cas une liste de références, presentée pareil.
            if cle.startswith('mag:'):
                champ = cle[4:]
                choix = [r for r in a_traiter
                         if any(m['shop_field'] == champ
                                for m in (r.get('magasins_soldables') or []))]
                nom = ''
                for r in choix:
                    for m in r['magasins_soldables']:
                        if m['shop_field'] == champ:
                            nom = m['libelle']
                            break
                    if nom:
                        break
                titre = 'Ce qui dort à %s' % (nom or champ)
                resume = ("%s références dorment dans ce magasin. La colonne « Stock ici » "
                          "est ce que CE magasin détient : c'est sur cette quantité que la "
                          "remise agira." % _fr_nombre(len(choix)))
                stock_local = {}
                for r in choix:
                    for m in r['magasins_soldables']:
                        if m['shop_field'] == champ:
                            stock_local[r['article_id']] = m['stock']
                choix.sort(key=lambda r: -stock_local.get(r['article_id'], 0))
                return {
                    'titre': titre, 'resume': resume,
                    'colonnes': ['Référence', 'Catégorie', 'Stock ici', 'Stock réseau',
                                 'Où solder', 'Dernière vente', 'Remise', 'Prix soldé'],
                    'lignes': [[r['reference'], r['categorie'] or '—',
                                _fr_nombre(stock_local.get(r['article_id'], 0)),
                                _fr_nombre(r['stock']),
                                '%s magasin%s' % (r['nb_soldables'],
                                                  's' if r['nb_soldables'] > 1 else ''),
                                'jamais' if r['jamais_vendu']
                                else '%s j' % _fr_nombre(r['jours_sans_vente']),
                                '−%s %%' % r['remise'],
                                '%s MAD' % _fr_nombre(r['prix_solde'])]
                               for r in choix[:self.ASSISTANT_DETAIL_MAX]],
                    'cles': ['ref:%s' % r['article_id']
                             for r in choix[:self.ASSISTANT_DETAIL_MAX]],
                }

            if cle.startswith('cat:'):
                nom = cle[4:]
                # Meme raison qu'au-dessus : une categorie peut n'avoir que
                # des references deja soldees, toujours bloquees.
                choix = [r for r in rows if (r['categorie'] or 'Sans catégorie') == nom]
                titre = 'Catégorie %s' % nom
                resume = ("%s références, %s pièces, %s MAD immobilisés."
                          % (_fr_nombre(len(choix)),
                             _fr_nombre(sum(r['stock'] for r in choix)),
                             _fr_nombre(sum(r['valeur_totale'] for r in choix))))
            else:
                palier = ([x for x in self._soldes_paliers_anciennete() if x[0] == cle]
                          or [None])[0]
                if not palier:
                    return {'error': "Détail inconnu pour cette ligne."}
                _code, nom, test, conseil = palier
                choix = [r for r in rows if test(r)]
                titre = 'Dernière vente : %s' % nom
                resume = ("%s références, %s pièces, %s MAD immobilisés. Ce que ça appelle : "
                          "%s." % (_fr_nombre(len(choix)),
                                   _fr_nombre(sum(r['stock'] for r in choix)),
                                   _fr_nombre(sum(r['valeur_totale'] for r in choix)),
                                   conseil))

            choix = sorted(choix, key=lambda r: -r['valeur_totale'])
            return {
                'titre': titre, 'resume': resume,
                'colonnes': ['Référence', 'Catégorie', 'Stock magasins', 'Dépôt',
                             'Où solder', 'Dernière vente', 'Remise', 'Prix soldé'],
                'lignes': [[r['reference'], r['categorie'] or '—', _fr_nombre(r['stock']),
                            _fr_nombre(r['depot']),
                            '%s / %s' % (r['nb_soldables'], r['nb_magasins']),
                            'jamais' if r['jamais_vendu']
                            else '%s j' % _fr_nombre(r['jours_sans_vente']),
                            '−%s %%' % r['remise'],
                            '%s MAD' % _fr_nombre(r['prix_solde'])]
                           for r in choix[:self.ASSISTANT_DETAIL_MAX]],
                'cles': ['ref:%s' % r['article_id'] for r in choix[:self.ASSISTANT_DETAIL_MAX]],
                'tronque': len(choix) > self.ASSISTANT_DETAIL_MAX,
                'nb_total': len(choix),
            }
        except Exception as e:  # noqa: BLE001
            _logger.error("Erreur api_assistant_detail: %s", e, exc_info=True)
            return {'error': str(e)}

    @http.route('/mavie/api/soldes-appliquer-lot', type='json', auth='user',
                methods=['POST'], csrf=False)
    def api_soldes_appliquer_lot(self, **kw):
        """Applique la remise conseillée à plusieurs références d'un coup.

        `lignes` = [{'article_id': 12, 'remise': 30, 'magasins': [...]}, ...].
        Par défaut, les magasins retenus sont ceux qui ont du stock ET une
        caisse : démarquer là où il n'y a rien n'a pas de sens. `magasins`
        (des `shop_field`) restreint encore cette liste, pour une opération
        qui ne concerne qu'une partie du réseau.
        """
        try:
            lignes = kw.get('lignes') or []
            if not lignes:
                return {'error': 'Aucune référence sélectionnée.'}
            if len(lignes) > 100:
                return {'error': "Trop de références d'un coup (100 au maximum)."}
            date_start, date_end = self._solde_dates_operation(
                (kw.get('date_start') or '').strip(),
                (kw.get('date_end') or '').strip())

            resultats = []
            faits = magasins_total = 0
            op_categories = self._solde_operation().get('categories') or []
            for ligne in lignes:
                try:
                    aid = int(ligne.get('article_id') or 0)
                    remise = float(ligne.get('remise') or 0)
                except (TypeError, ValueError):
                    continue
                if not aid or not (0 < remise < 100):
                    continue
                tmpl = request.env['product.template'].sudo().browse(aid).exists()
                nom = tmpl.name if tmpl else str(aid)
                if op_categories and (not tmpl or tmpl.categ_id.id not in op_categories):
                    resultats.append({'article_id': aid, 'reference': nom, 'ok': False,
                                      'message': "Hors des catégories de l'opération."})
                    continue
                ctx = self.api_solde_context(product_tmpl_id=aid)
                if ctx.get('error'):
                    resultats.append({'article_id': aid, 'reference': nom,
                                      'ok': False, 'message': ctx['error']})
                    continue
                catalogue = float(ctx.get('prix_catalogue_ttc') or 0)
                prix = _arrondi_prix_solde(round(catalogue * (100 - remise) / 100.0, 2))
                cibles = [m for m in (ctx.get('magasins') or [])
                          if (m.get('stock') or 0) > 0 and m.get('caisses')]
                # L'ecran peut restreindre la remise a certains magasins :
                # une operation ne se fait pas toujours partout.
                choisis = ligne.get('magasins')
                if choisis:
                    garde = set(choisis)
                    cibles = [m for m in cibles if m['shop_field'] in garde]
                if not cibles or prix <= 0:
                    resultats.append({
                        'article_id': aid, 'reference': ctx.get('reference') or nom,
                        'ok': False,
                        'message': "Aucun magasin avec du stock et une caisse."
                                   if not cibles else "Prix soldé invalide."})
                    continue
                res = self.api_solde_apply(
                    product_tmpl_id=aid, prix_ttc=prix, mode='pricelist',
                    date_start=date_start, date_end=date_end,
                    magasins=[{'shop_field': m['shop_field'],
                               'nom_liste': '' if m.get('liste') else m.get('nom_propose')}
                              for m in cibles])
                ok = bool(res.get('ok'))
                faits += 1 if ok else 0
                magasins_total += len(cibles) if ok else 0
                resultats.append({
                    'article_id': aid,
                    'reference': ctx.get('reference') or nom,
                    'ok': ok,
                    'remise': int(remise),
                    'prix': prix,
                    'magasins': len(cibles),
                    'message': '' if ok else (res.get('error') or 'Échec'),
                })
            _vider_cache_dashboard()
            _logger.info("Soldes en lot par %s : %s/%s references, %s magasins",
                         request.env.user.login, faits, len(resultats), magasins_total)
            return {'ok': faits > 0, 'faits': faits, 'total': len(resultats),
                    'magasins': magasins_total, 'resultats': resultats}
        except Exception as e:  # noqa: BLE001
            _logger.error("Erreur api_soldes_appliquer_lot: %s", e, exc_info=True)
            return {'error': str(e)}

    @http.route('/mavie/api/assistant', type='json', auth='user',
                methods=['POST'], csrf=False)
    def api_assistant(self, **kw):
        """Répond à UNE question fermée, avec les chiffres du moment."""
        try:
            question = (kw.get('question') or '').strip()
            if not question:
                return {'questions': self.ASSISTANT_QUESTIONS}
            data = self.api_soldes_proposition(**kw)
            if data.get('error'):
                return {'error': data['error']}
            rows = data.get('rows') or []
            k = data.get('kpis') or {}
            p = data.get('params') or {}
            a_traiter = [r for r in rows if not r['deja_solde']]

            if question == 'solder_semaine':
                choix = [r for r in a_traiter if r['urgence'] == 'urgent'][:10]
                if not choix:
                    choix = a_traiter[:10]
                pieces = sum(r['stock'] for r in choix)
                valeur = sum(r['valeur_totale'] for r in choix)
                return {
                    'titre': 'À solder cette semaine',
                    'resume': ("%s références · %s pièces en rayon · %s MAD bloqués."
                               % (len(choix), _fr_nombre(pieces), _fr_nombre(valeur))),
                    'colonnes': ['Référence', 'Stock magasins', 'Dépôt', 'Où solder',
                                 'Remise', 'Prix soldé'],
                    'lignes': [[r['reference'], _fr_nombre(r['stock']), _fr_nombre(r['depot']),
                                '%s magasin%s' % (r['nb_soldables'],
                                                  's' if r['nb_soldables'] > 1 else ''),
                                '−%s %%' % r['remise'], '%s MAD' % _fr_nombre(r['prix_solde'])]
                               for r in choix],
                    'cles': ['ref:%s' % r['article_id'] for r in choix],
                    'aide_clic': "Cliquez une référence pour voir les magasins exacts à solder.",
                    'articles': [r['article_id'] for r in choix],
                }

            if question == 'habitudes_categorie':
                cal = self._soldes_remises_pratiquees()
                habitudes = cal.get('habitudes') or []
                if not habitudes:
                    return {
                        'titre': 'Vos remises par catégorie',
                        'resume': ("Aucune catégorie ne compte encore %s remises posées : "
                                   "les propositions se calent sur le relevé global."
                                   % self.SOLDES_MIN_REGLES_CATEGORIE),
                        'colonnes': [], 'lignes': [],
                    }
                # Combien de references a solder chaque habitude gouverne.
                gouvernees = {}
                for r in a_traiter:
                    if r.get('remise_source') == 'categorie':
                        gouvernees[r['categorie']] = gouvernees.get(r['categorie'], 0) + 1
                return {
                    'titre': 'Vos remises par catégorie',
                    'resume': ("%s catégories · elles calibrent %s des %s références "
                               "à traiter."
                               % (len(habitudes), _fr_nombre(sum(gouvernees.values())),
                                  _fr_nombre(len(a_traiter)))),
                    'colonnes': ['Catégorie', 'La plus faible',
                                 "D'habitude", 'Quand vous allez fort', 'La plus forte',
                                 'Références concernées'],
                    'lignes': [[h['categorie'],
                                '−%s %%' % h['mini'],
                                '−%s %%' % h['niveau_normal'],
                                '−%s %%' % h['niveau_fort'],
                                '−%s %%' % h['maxi'],
                                _fr_nombre(gouvernees.get(h['categorie'], 0))]
                               for h in habitudes],
                    # Pas de cle quand la categorie n'a plus rien a traiter :
                    # son habitude vient du passe, pas du stock actuel.
                    'cles': ['cat:%s' % h['categorie'] if gouvernees.get(h['categorie'])
                             else '' for h in habitudes],
                    'aide_clic': "Cliquez une catégorie pour voir ses références à solder.",
                }

            if question == 'moment_vente':
                depuis = fields.Datetime.now() - timedelta(days=90)
                request.env.cr.execute("""
                    SELECT w.name, EXTRACT(ISODOW FROM x.d)::int, EXTRACT(HOUR FROM x.d)::int,
                           COUNT(DISTINCT x.oid), COALESCE(SUM(x.qte), 0)
                      FROM (SELECT po.id AS oid, pol.qty AS qte,
                                   (po.date_order AT TIME ZONE 'UTC') AT TIME ZONE 'Africa/Casablanca' AS d,
                                   spt.warehouse_id AS wid
                              FROM pos_order_line pol
                              JOIN pos_order po ON po.id = pol.order_id
                              JOIN pos_session ps ON ps.id = po.session_id
                              JOIN pos_config pc ON pc.id = ps.config_id
                              JOIN stock_picking_type spt ON spt.id = pc.picking_type_id
                             WHERE po.state IN ('paid', 'done', 'invoiced')
                               AND po.date_order >= %s) x
                      JOIN stock_warehouse w ON w.id = x.wid
                     GROUP BY 1, 2, 3
                """, (depuis,))
                jours = ['lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche']
                heures = list(range(8, 24))
                par = {}
                articles_jour = [0.0] * 7
                articles_heure = [0.0] * 24
                for nom, dow, h, tickets, qte in request.env.cr.fetchall():
                    qte = float(qte or 0)
                    d = par.setdefault(nom, {'jour': {}, 'heure': {}, 'tickets_heure': {}, 'tickets': 0})
                    d['jour'][dow] = d['jour'].get(dow, 0) + qte
                    d['heure'][h] = d['heure'].get(h, 0) + qte
                    d['tickets_heure'][h] = d['tickets_heure'].get(h, 0) + tickets
                    d['tickets'] += tickets
                    articles_jour[dow - 1] += qte
                    articles_heure[h] += qte
                lignes = []
                for nom in sorted(par):
                    d = par[nom]
                    bj = max(d['jour'], key=d['jour'].get)
                    bh = max(d['heure'], key=d['heure'].get)
                    lignes.append([nom, jours[bj - 1], '%02dh – %02dh' % (bh, (bh + 1) % 24),
                                   _fr_nombre(d['tickets_heure'][bh]), _fr_nombre(d['tickets'])])
                return {
                    'titre': 'Quel moment vend le mieux (90 derniers jours, heure du Maroc)',
                    'resume': '',
                    'colonnes': ['Magasin', 'Meilleur jour', 'Meilleure heure', 'Tickets dans cette heure', 'Tickets sur 90 jours'],
                    'lignes': lignes,
                    'cles': [],
                    'graphiques': [
                        {'titre': 'Articles vendus par jour de la semaine', 'type': 'colonnes', 'unite': 'articles',
                         'items': [{'nom': jours[j].capitalize(), 'valeur': round(articles_jour[j], 0),
                                    'fort': articles_jour[j] == max(articles_jour), 'detail': ''}
                                   for j in range(7)],
                         'legende': 'Nombre d’articles vendus, tous magasins confondus, 90 derniers jours.'},
                        {'titre': 'Articles vendus par heure', 'type': 'colonnes', 'unite': 'articles',
                         'items': [{'nom': '%02dh' % h, 'valeur': round(articles_heure[h], 0),
                                    'fort': articles_heure[h] == max(articles_heure[x] for x in heures),
                                    'detail': '%02dh – %02dh' % (h, (h + 1) % 24)} for h in heures],
                         'legende': 'Nombre d’articles vendus par heure, tous magasins confondus, 90 derniers jours. La barre pleine est l’heure la plus forte.'},
                    ],
                }

            if question == 'remise_qui_marche':
                cal = self._soldes_remises_pratiquees()
                bandes = [b for b in (cal.get('bandes') or [])
                          if b['regles'] >= self.SOLDES_MIN_REGLES_BANDE]
                if not bandes:
                    return {
                        'titre': 'Quelle remise marche le mieux',
                        'resume': ("Pas encore assez de remises posées pour en tirer une "
                                   "règle : il en faut au moins %s par tranche de 10 points. "
                                   "Les paliers conseillés restent ceux des réglages."
                                   % self.SOLDES_MIN_REGLES_BANDE),
                        'colonnes': [], 'lignes': [],
                    }
                meilleure = max(bandes, key=lambda b: b['par_regle'])
                return {
                    'titre': 'Quelle remise marche le mieux ici',
                    'resume': ("La tranche %s à %s %% est la plus efficace : %s pièces "
                               "vendues par remise posée."
                               % (meilleure['de'], meilleure['a'],
                                  _fr_nombre(meilleure['par_regle']))),
                    'colonnes': ['Tranche de remise', 'Pièces vendues',
                                 'Palier habituel'],
                    # Tri par pieces vendues : le critere d'efficacite n'etant
                    # plus affiche, classer dessus donnerait un ordre
                    # incomprehensible.
                    'lignes': [['%s à %s %%' % (b['de'], b['a']),
                                _fr_nombre(b['pieces']),
                                '−%s %%' % b['niveau']]
                               for b in sorted(bandes, key=lambda b: -b['pieces'])],
                    'cles': ['bande:%s-%s' % (b['de'], b['a'])
                             for b in sorted(bandes, key=lambda b: -b['pieces'])],
                    'aide_clic': "Cliquez une tranche pour voir ce qui y a été soldé.",
                    # En colonnes, rangees par REMISE croissante : c'est la
                    # forme qui parle — ca monte jusqu'au pic, puis ca
                    # retombe. Trie par volume, cette chute ne se verrait
                    # plus.
                    'graphique': {
                        'type': 'colonnes',
                        'unite': 'pièces',
                        'items': [{
                            'nom': '%s-%s' % (b['de'], b['a']),
                            'valeur': b['pieces'],
                            'fort': b['de'] == meilleure['de'],
                            'cle': 'bande:%s-%s' % (b['de'], b['a']),
                            'detail': ('%s remises posées · palier habituel −%s %% · '
                                       '%s pièces vendues depuis'
                                       % (_fr_nombre(b['regles']), b['niveau'],
                                          _fr_nombre(b['pieces']))),
                        } for b in sorted(bandes, key=lambda b: b['de'])],
                        'legende': ('hauteur = pièces vendues · la tranche %s à %s %% '
                                    'est la plus efficace'
                                    % (meilleure['de'], meilleure['a'])),
                    },
                }

            if question == 'ou_solder':
                # Une operation se decide magasin par magasin : celui qui
                # porte le plus de dormant est celui qui en souffre le plus.
                par_mag = {}
                for r in a_traiter:
                    for m in (r.get('magasins_soldables') or []):
                        d = par_mag.setdefault(m['shop_field'], {
                            'shop_field': m['shop_field'],
                            'libelle': m['libelle'], 'societe': m['societe'],
                            'refs': 0, 'pieces': 0, 'valeur': 0.0, 'urgentes': 0})
                        d['refs'] += 1
                        d['pieces'] += m['stock']
                        d['valeur'] += m['stock'] * r['prix_ttc']
                        d['urgentes'] += 1 if r['urgence'] == 'urgent' else 0
                classe = sorted(par_mag.values(), key=lambda d: -d['valeur'])
                sans_caisse = sum(1 for r in a_traiter
                                  if r.get('nb_magasins', 0) > r.get('nb_soldables', 0))
                return {
                    'titre': 'Où solder',
                    # Le graphique dit deja tout : magasins, references,
                    # urgentes. On ne garde que l'avertissement, s'il y a
                    # lieu — lui seul n'a pas d'equivalent visuel.
                    'resume': (("%s références ont du stock dans un magasin sans caisse."
                               % _fr_nombre(sans_caisse)) if sans_caisse else ''),
                    'colonnes': ['Magasin', 'Société', 'Références', 'Dont urgentes',
                                 'Pièces'],
                    'lignes': [[d['libelle'], d['societe'], _fr_nombre(d['refs']),
                                _fr_nombre(d['urgentes']), _fr_nombre(d['pieces'])]
                               for d in classe],
                    'cles': ['mag:%s' % d['shop_field'] for d in classe],
                    'aide_clic': "Cliquez un magasin pour voir ce qui y dort.",
                    # Des barres : l'argent bloque par magasin, et le nombre
                    # de references urgentes qu'il porte.
                    'graphique': {
                        'type': 'barres',
                        'unite': 'MAD',
                        'items': [{
                            'nom': d['libelle'],
                            'sous': d['societe'],
                            'valeur': round(d['valeur'], 2),
                            # Deux comptes distincts : tout ce qui dort ici,
                            # et la part dont le depot garde encore du stock.
                            'refs': d['refs'],
                            'alerte': d['urgentes'],
                            'cle': 'mag:%s' % d['shop_field'],
                            'detail': ('%s références dormantes · %s pièces · dont %s '
                                       'urgentes'
                                       % (_fr_nombre(d['refs']), _fr_nombre(d['pieces']),
                                          _fr_nombre(d['urgentes']))),
                        } for d in classe],
                        'legende': ("« réf. » = références qui dorment dans ce "
                                    "magasin · la pastille rouge = celles dont le dépôt "
                                    "garde encore du stock"),
                    },
                }

            if question == 'categories_dorment':
                # Diagnostic, pas une liste d'action : on compte TOUT ce qui
                # dort, meme une reference qui porte deja une regle de prix
                # sans avoir vendu. Sinon une categorie à 100 % deja soldee
                # disparait entierement, alors que son stock est toujours
                # bloque.
                par_cat = {}
                for r in rows:
                    d = par_cat.setdefault(r['categorie'] or 'Sans catégorie', {
                        'refs': 0, 'pieces': 0, 'valeur': 0.0, 'jamais': 0})
                    d['refs'] += 1
                    d['pieces'] += r['stock']
                    d['valeur'] += r['valeur_totale']
                    d['jamais'] += 1 if r['jamais_vendu'] else 0
                classe = sorted(par_cat.items(), key=lambda kv: -kv[1]['valeur'])
                total = sum(d['valeur'] for _n, d in classe) or 1.0
                return {
                    'titre': 'Où dort l\u2019argent, par catégorie',
                    'resume': ("%s catégories · la première concentre %s %% de l'argent "
                               "bloqué."
                               % (len(classe),
                                  _fr_nombre(round(classe[0][1]['valeur'] * 100.0 / total, 1))
                                  if classe else 0)),
                    'colonnes': ['Catégorie', 'Références', 'Jamais vendues', 'Pièces'],
                    'lignes': [[nom, _fr_nombre(d['refs']), _fr_nombre(d['jamais']),
                                _fr_nombre(d['pieces'])]
                               for nom, d in classe],
                    'cles': ['cat:%s' % nom for nom, _d in classe],
                    'aide_clic': "Cliquez une catégorie pour voir ses références.",
                    # La carte : un rectangle par categorie, sa taille est
                    # l'argent bloque. Les nombres sont bruts, le dessin se
                    # fait a l'ecran.
                    'graphique': {
                        'type': 'carte',
                        'unite': 'MAD',
                        'items': [{
                            'nom': nom,
                            'valeur': round(d['valeur'], 2),
                            'cle': 'cat:%s' % nom,
                            'detail': ('%s références · %s pièces'
                                       % (_fr_nombre(d['refs']), _fr_nombre(d['pieces']))),
                        } for nom, d in classe],
                    },
                }

            if question == 'depuis_quand':
                # Plus une reference dort, plus la remise doit etre franche :
                # a six mois, une petite remise ne la reveillera pas.
                lignes = []
                cles = []
                segments = []
                for code, nom, test, conseil in self._soldes_paliers_anciennete():
                    lot = [r for r in rows if test(r)]
                    if not lot:
                        continue
                    valeur_lot = sum(r['valeur_totale'] for r in lot)
                    lignes.append([nom, _fr_nombre(len(lot)),
                                   _fr_nombre(sum(r['stock'] for r in lot)),
                                   conseil])
                    cles.append(code)
                    segments.append({
                        'nom': nom, 'cle': code, 'conseil': conseil,
                        'valeur': round(valeur_lot, 2),
                        'refs': len(lot),
                        'pieces': sum(r['stock'] for r in lot),
                    })
                jamais = [r for r in rows if r['jamais_vendu']]
                return {
                    'titre': 'Depuis combien de temps ça dort',
                    # Le segment rouge de la barre dit deja le nombre de
                    # references et de pieces jamais vendues.
                    'resume': '',
                    'colonnes': ['Dernière vente', 'Références', 'Pièces',
                                 'Ce que ça appelle'],
                    'lignes': lignes,
                    'cles': cles,
                    'aide_clic': "Cliquez une tranche pour voir ses références.",
                    # Une seule barre, decoupee par anciennete : on voit la
                    # part de chaque tranche sans comparer des nombres.
                    'graphique': {'type': 'segments', 'unite': 'MAD',
                                  'items': segments},
                }

            if question == 'deja_soldees':
                faites = [r for r in rows if r['deja_solde']]
                if not faites:
                    return {'titre': 'Soldes déjà posées',
                            'resume': "Aucune remise n'est posée sur ces références.",
                            'colonnes': [], 'lignes': []}
                dort_encore = [r for r in faites if r['jamais_vendu']
                               or (r['jours_sans_vente'] or 0) > p.get('fenetre', 90)]
                return {
                    'titre': 'Soldes déjà posées',
                    'resume': ("%s références déjà soldées · %s n'ont rien vendu depuis "
                               "plus de %s jours."
                               % (_fr_nombre(len(faites)), _fr_nombre(len(dort_encore)),
                                  p.get('fenetre'))),
                    'colonnes': ['Référence', 'Remise en place', 'Stock magasins', 'Dépôt',
                                 'Où solder', 'Vendu', 'Dernière vente'],
                    'lignes': [[r['reference'], r['solde_en_place'] or '—',
                                _fr_nombre(r['stock']), _fr_nombre(r['depot']),
                                '%s magasin%s' % (r['nb_soldables'],
                                                  's' if r['nb_soldables'] > 1 else ''),
                                _fr_nombre(r['vendu']),
                                'jamais' if r['jamais_vendu']
                                else '%s j' % _fr_nombre(r['jours_sans_vente'])]
                               for r in sorted(dort_encore or faites,
                                               key=lambda x: -x['valeur_totale'])[:15]],
                    'cles': ['ref:%s' % r['article_id']
                             for r in sorted(dort_encore or faites,
                                             key=lambda x: -x['valeur_totale'])[:15]],
                    'aide_clic': "Cliquez une référence pour voir où la remise est posée.",
                    'articles': [r['article_id'] for r in (dort_encore or faites)[:15]],
                }

            if question == 'remise_reference':
                try:
                    aid = int(kw.get('article_id') or 0)
                except (TypeError, ValueError):
                    aid = 0
                ligne = ([r for r in rows if r['article_id'] == aid] or [None])[0]
                if not ligne:
                    return {'titre': 'Quelle remise ?',
                            'resume': "Choisissez une référence dans la liste ci-dessous.",
                            'colonnes': [], 'lignes': []}
                raisons = []
                if ligne['vendu'] == 0:
                    raisons.append("elle n'a rien vendu sur les %s derniers jours"
                                   % p.get('fenetre'))
                elif ligne['couverture_jours']:
                    raisons.append("son stock met %s jours à s'écouler au rythme actuel"
                                   % _fr_nombre(ligne['couverture_jours']))
                if ligne['depot']:
                    raisons.append("le dépôt en garde encore %s pièces"
                                   % _fr_nombre(ligne['depot']))
                raisons.append("elle occupe %s pièces dans %s magasin%s"
                               % (_fr_nombre(ligne['stock']), ligne['nb_magasins'],
                                  's' if ligne['nb_magasins'] > 1 else ''))
                return {
                    'titre': 'Remise conseillée : −%s %%' % ligne['remise'],
                    'resume': ("%s passe de %s MAD à %s MAD TTC. Pourquoi ce niveau : %s."
                               % (ligne['reference'], _fr_nombre(ligne['prix_ttc']),
                                  _fr_nombre(ligne['prix_solde']), ', '.join(raisons))),
                    'colonnes': ['Mesure', 'Valeur'],
                    'lignes': [
                        ['Acheté', _fr_nombre(ligne['achete'])],
                        ['Vendu sur la période', _fr_nombre(ligne['vendu'])],
                        ['Stock magasins', _fr_nombre(ligne['stock'])],
                        ['Encore au dépôt', _fr_nombre(ligne['depot'])],
                        ['Prix catalogue TTC', '%s MAD' % _fr_nombre(ligne['prix_ttc'])],
                        ['Prix soldé', '%s MAD' % _fr_nombre(ligne['prix_solde'])],
                    ],
                    'articles': [ligne['article_id']],
                }

            return {'error': 'Question inconnue.'}
        except Exception as e:  # noqa: BLE001
            _logger.error("Erreur api_assistant: %s", e, exc_info=True)
            return {'error': str(e)}

    @http.route('/mavie/api/soldes-proposition', type='json', auth='user',
                methods=['POST'], csrf=False)
    def api_soldes_proposition(self, **kw):
        """Les références à démarquer, de la plus coûteuse à garder à la
        moins coûteuse."""
        try:
            p = self._soldes_params(kw)
            warehouses, wh_labels = self._reassort_warehouses(kw)
            if not warehouses:
                return {'error': "Aucun magasin actif."}
            wh_ids = warehouses.ids
            ref_date = self._reassort_reference_date(wh_ids)
            debut = ref_date - timedelta(days=p['fenetre'])

            filtres = self._mfl_sans_test_sql('pt')
            params = {'wh': wh_ids, 'd1': str(debut) + ' 00:00:00',
                      'd2': str(ref_date) + ' 23:59:59',
                      'socs': warehouses.mapped('company_id').ids or [-1]}
            sachets = self._get_sachet_variant_ids()
            if sachets:
                filtres += ' AND NOT (pp.id = ANY(%(sachets)s))'
                params['sachets'] = list(sachets)
            if self._filtre_produit_actif(kw):
                params['tmpls'] = request.env['product.template'].sudo().search(
                    self._build_product_domain(kw)).ids or [-1]
                filtres += ' AND pt.id = ANY(%(tmpls)s)'

            request.env.cr.execute("""
                WITH stock AS (
                    SELECT pp.product_tmpl_id AS tmpl, l.warehouse_id AS wh,
                           SUM(q.quantity) AS qte
                      FROM stock_quant q
                      JOIN stock_location l ON l.id = q.location_id
                      JOIN product_product pp ON pp.id = q.product_id
                      JOIN product_template pt ON pt.id = pp.product_tmpl_id
                     WHERE l.usage = 'internal' AND l.warehouse_id = ANY(%(wh)s)
                       AND q.quantity > 0 AND pt.active AND pp.active
                       {FILTRES}
                     GROUP BY 1, 2
                ), ventes AS (
                    SELECT pp.product_tmpl_id AS tmpl,
                           SUM(pol.qty) AS qte,
                           MAX(po.date_order) AS derniere
                      FROM pos_order_line pol
                      JOIN pos_order po ON po.id = pol.order_id
                      JOIN pos_session ps ON ps.id = po.session_id
                      JOIN pos_config pc ON pc.id = ps.config_id
                      JOIN stock_picking_type spt ON spt.id = pc.picking_type_id
                      JOIN product_product pp ON pp.id = pol.product_id
                     WHERE po.state IN ('paid', 'done', 'invoiced')
                       AND spt.warehouse_id = ANY(%(wh)s)
                       AND po.date_order BETWEEN %(d1)s AND %(d2)s
                     GROUP BY 1
                ), achats AS (
                    SELECT pp.product_tmpl_id AS tmpl,
                           SUM(pol.qty_received) AS qte
                      FROM purchase_order_line pol
                      JOIN purchase_order po ON po.id = pol.order_id
                      JOIN product_product pp ON pp.id = pol.product_id
                     WHERE po.company_id = ANY(%(socs)s)
                       AND po.state IN ('purchase', 'done')
                     GROUP BY 1
                ), toutes_ventes AS (
                    SELECT pp.product_tmpl_id AS tmpl, MAX(po.date_order) AS derniere
                      FROM pos_order_line pol
                      JOIN pos_order po ON po.id = pol.order_id
                      JOIN pos_session ps ON ps.id = po.session_id
                      JOIN pos_config pc ON pc.id = ps.config_id
                      JOIN stock_picking_type spt ON spt.id = pc.picking_type_id
                      JOIN product_product pp ON pp.id = pol.product_id
                     WHERE po.state IN ('paid', 'done', 'invoiced')
                       AND spt.warehouse_id = ANY(%(wh)s)
                     GROUP BY 1
                )
                SELECT s.tmpl,
                       SUM(s.qte) AS stock_total,
                       COUNT(DISTINCT s.wh) AS nb_magasins,
                       ARRAY_AGG(s.wh ORDER BY s.wh) AS entrepots,
                       ARRAY_AGG(s.qte ORDER BY s.wh) AS quantites,
                       COALESCE(MAX(v.qte), 0) AS vendu,
                       MAX(tv.derniere) AS derniere_vente,
                       COALESCE(MAX(a.qte), 0) AS achete
                  FROM stock s
                  LEFT JOIN ventes v ON v.tmpl = s.tmpl
                  LEFT JOIN toutes_ventes tv ON tv.tmpl = s.tmpl
                  LEFT JOIN achats a ON a.tmpl = s.tmpl
                 GROUP BY s.tmpl
            """.replace('{FILTRES}', filtres), params)
            brut = request.env.cr.fetchall()
            if not brut:
                return {'rows': [], 'kpis': {}, 'params': p}

            tmpl_ids = [r[0] for r in brut]
            fiches = {t['id']: t for t in request.env['product.template'].sudo().search_read(
                [('id', 'in', tmpl_ids)],
                ['name', 'default_code', 'base_pivot_reference', 'list_price', 'categ_id'])}
            deja = self._soldes_deja_en_place(tmpl_ids)
            # La photo de chaque reference : on ne montre que celles dont le
            # fichier existe reellement, sinon la ligne afficherait un cadre
            # vide a la place d'un article.
            photos = self._image_availability(tmpl_ids)

            # Ce qu'il reste au DEPOT sur ces memes references : une
            # reference qui dort en magasin et dont le depot garde encore du
            # stock coute des deux cotes, et le depot ne peut pas la placer.
            depot = self._societe_depot()
            stock_depot = {}
            if depot:
                request.env.cr.execute("""
                    SELECT pp.product_tmpl_id, SUM(q.quantity)
                      FROM stock_quant q
                      JOIN stock_location l ON l.id = q.location_id
                      JOIN product_product pp ON pp.id = q.product_id
                     WHERE l.usage = 'internal' AND l.company_id = %s
                       AND pp.product_tmpl_id = ANY(%s) AND q.quantity > 0
                     GROUP BY 1
                """, (depot.id, tmpl_ids))
                stock_depot = {t: float(q or 0)
                               for t, q in request.env.cr.fetchall()}

            # Le prix affiche doit etre CELUI DU POP-UP, sinon les deux
            # ecrans se contredisent : list_price est HT, le pop-up le
            # convertit en TTC avec la taxe de l'article. Meme helper ici.
            societe_ref = warehouses[0].company_id if warehouses else request.env.company
            ratios = {}
            for tmpl in request.env['product.template'].sudo().browse(tmpl_ids):
                ratios[tmpl.id] = self._solde_tax_ratio(tmpl, societe_ref)

            # Ou une remise peut-elle etre posee ? Il faut une caisse
            # physique : sans caisse, aucune liste de prix a modifier, la
            # remise n'existerait nulle part. On le calcule une fois.
            soldables = {}
            for m in self._solde_mappings():
                if self._solde_store_configs(m):
                    soldables[m.warehouse_id.id] = {
                        'shop_field': m.shop_field,
                        'libelle': m.shop_label or m.warehouse_id.name,
                        'societe': m.company_id.name,
                    }

            # Le calibrage des paliers, une fois pour toute la page.
            pratiquees = ({} if p['mode_remise'] == 'fixe'
                          else self._soldes_remises_pratiquees())
            habitudes = pratiquees.get('par_categorie') or {}
            usage_normal = pratiquees.get('usage_normal')
            usage_fort = pratiquees.get('usage_fort')
            echelle = pratiquees.get('echelle') or []

            rows = []
            valeur_totale = pieces_totales = 0.0
            for (tmpl_id, stock_total, nb_mag, entrepots, quantites,
                 vendu, derniere, achete) in brut:
                stock_total = int(round(float(stock_total or 0)))
                vendu = int(round(float(vendu or 0)))
                if stock_total < p['stock_min']:
                    continue
                # Deux facons de dormir : ne rien vendre, ou vendre si
                # lentement que le stock met des mois a s'ecouler.
                vitesse = vendu / float(p['fenetre']) if vendu else 0.0
                # Arrondi avant comparaison, comme la carte « Stock dormant » :
                # le tableau affiche des jours entiers.
                couverture = round(stock_total / vitesse) if vitesse else None
                if vendu > 0 and (couverture or 0) <= p['couverture_min']:
                    continue
                fiche = fiches.get(tmpl_id) or {}
                prix = float(fiche.get('list_price') or 0.0) * ratios.get(tmpl_id, 1.0)
                valeur = stock_total * prix
                jours = None
                if derniere:
                    jours = (ref_date - derniere.date()).days
                # Deux paliers : remise forte si la reference n'a rien
                # vendu sur la periode, ou si son stock met plus de deux
                # fois le seuil a s'ecouler.
                fort = (vendu == 0) or ((couverture or 0) >= 2 * p['couverture_min'])
                categorie = (fiche['categ_id'][1] if fiche.get('categ_id') else '')
                # Du precedent le plus proche au plus general : l'habitude
                # de CETTE categorie d'abord, le releve global ensuite.
                habitude = habitudes.get(categorie)
                if habitude:
                    remise = (habitude['niveau_fort'] if fort
                              else habitude['niveau_normal'])
                    source = 'categorie'
                elif usage_fort:
                    remise = usage_fort if fort else usage_normal
                    source = 'general'
                else:
                    remise = p['remise2'] if fort else p['remise1']
                    source = 'reglage'
                pose = deja.get(tmpl_id) or {}
                remise_en_place = int(pose.get('remise') or 0)
                # Une remise deja posee n'a pas fait bouger la marchandise :
                # en proposer autant ou moins ne servirait a rien.
                if remise_en_place and remise <= remise_en_place:
                    remise = self._soldes_palier_suivant(remise_en_place, echelle)
                    source = 'accentuee'
                remise = int(max(self.SOLDES_REMISE_MIN,
                                 min(self.SOLDES_REMISE_MAX, remise)))
                # Une remise deja au plafond ne peut plus monter : proposer
                # MOINS que ce qui est pose serait un contresens. Ici la
                # remise n'est plus le levier — il faut deplacer la
                # marchandise ou la declasser.
                remise_au_plafond = bool(remise_en_place and remise <= remise_en_place)
                if remise_au_plafond:
                    remise = remise_en_place
                    source = 'plafond'
                au_depot = int(round(stock_depot.get(tmpl_id, 0.0)))
                valeur_depot = au_depot * prix
                # Urgent = elle dort ici ET le depot en garde : personne ne
                # peut l'ecouler, et la marchandise attend des deux cotes.
                if au_depot > 0:
                    urgence = 'urgent'
                elif fort:
                    urgence = 'forte'
                else:
                    urgence = 'normale'
                # Les magasins de CETTE reference, avec leur stock, et
                # ceux ou la remise peut reellement etre posee.
                par_magasin = []
                for wh_id, qte in zip(entrepots or [], quantites or []):
                    info = soldables.get(wh_id)
                    qte = int(round(float(qte or 0)))
                    if qte <= 0:
                        continue
                    par_magasin.append({
                        'warehouse_id': wh_id,
                        'shop_field': (info or {}).get('shop_field'),
                        'libelle': (info or {}).get('libelle') or '?',
                        'societe': (info or {}).get('societe') or '',
                        'stock': qte,
                        'soldable': bool(info),
                    })
                par_magasin.sort(key=lambda x: (not x['soldable'], -x['stock']))
                magasins_soldables = [x for x in par_magasin if x['soldable']]

                rows.append({
                    'article_id': tmpl_id,
                    'depot': au_depot,
                    'magasins': par_magasin,
                    'magasins_soldables': magasins_soldables,
                    'nb_soldables': len(magasins_soldables),
                    'stock_soldable': sum(x['stock'] for x in magasins_soldables),
                    'valeur_depot': round(valeur_depot, 2),
                    'valeur_totale': round(valeur + valeur_depot, 2),
                    'urgence': urgence,
                    'reference': (fiche.get('base_pivot_reference')
                                  or fiche.get('default_code')
                                  or fiche.get('name') or '—'),
                    'produit': fiche.get('name') or '—',
                    'photo': self._image_url(tmpl_id, photos.get(tmpl_id)) or '',
                    'categorie': fiche['categ_id'][1] if fiche.get('categ_id') else '',
                    'stock': stock_total,
                    'nb_magasins': int(nb_mag or 0),
                    'vendu': vendu,
                    'achete': int(round(float(achete or 0))),
                    'couverture_jours': (int(round(couverture))
                                         if couverture is not None else None),
                    'jours_sans_vente': jours,
                    'jamais_vendu': jours is None,
                    'prix_ttc': round(prix, 2),
                    'valeur': round(valeur, 2),
                    'remise': remise,
                    'prix_solde': _arrondi_prix_solde(round(prix * (100 - remise) / 100.0, 2)),
                    'gain_attendu': round(valeur * remise / 100.0, 2),
                    'deja_solde': bool(pose),
                    'solde_en_place': pose.get('liste') or None,
                    'remise_en_place': remise_en_place or None,
                    'remise_au_plafond': remise_au_plafond,
                    # D'ou vient la remise proposee : l'habitude de la
                    # categorie, le releve general, l'accentuation d'une
                    # remise en place, ou les reglages.
                    'remise_source': source,
                    'remise_source_regles': (habitude or {}).get('regles') or 0,
                })
                valeur_totale += valeur
                pieces_totales += stock_total

            rang = {'urgent': 0, 'forte': 1, 'normale': 2}
            rows.sort(key=lambda r: (rang.get(r['urgence'], 3),
                                     -r['valeur_totale'], r['reference']))
            a_traiter = [r for r in rows if not r['deja_solde']]
            urgentes = [r for r in a_traiter if r['urgence'] == 'urgent']
            return {
                'params': p,
                'operation': self._solde_operation(),
                'calibrage': pratiquees,
                'portee': self._soldes_portee_reelle(),
                'date_reference': ref_date.isoformat(),
                'date_debut': debut.isoformat(),
                'rows': rows[:self.SOLDES_MAX_LIGNES],
                'tronque': len(rows) > self.SOLDES_MAX_LIGNES,
                'nb_lignes_total': len(rows),
                'kpis': {
                    'nb_references': len(rows),
                    'nb_a_traiter': len(a_traiter),
                    'nb_deja_soldees': len(rows) - len(a_traiter),
                    'pieces': int(round(pieces_totales)),
                    'valeur': round(valeur_totale, 2),
                    'valeur_a_traiter': round(sum(r['valeur'] for r in a_traiter), 2),
                    # Le gisement « depot » : ce qui bloque des deux cotes.
                    'nb_urgent': len(urgentes),
                    'pieces_depot': int(round(sum(r['depot'] for r in urgentes))),
                    'valeur_depot': round(sum(r['valeur_depot'] for r in urgentes), 2),
                },
            }
        except Exception as e:  # noqa: BLE001
            _logger.error("Erreur api_soldes_proposition: %s", e, exc_info=True)
            return {'error': str(e)}

    def _soldes_deja_en_place(self, tmpl_ids):
        """{tmpl_id: {'liste', 'remise'}} pour les références qui portent
        déjà une règle de solde.

        La remise en place compte : on ne peut pas proposer moins que ce
        qui est déjà posé, puisque la marchandise n'a pas bougé à ce
        prix-là. On retient la remise la PLUS FORTE déjà en place.
        """
        if not tmpl_ids:
            return {}
        request.env.cr.execute("""
            SELECT COALESCE(pi.product_tmpl_id, pp.product_tmpl_id) AS tmpl,
                   MAX(COALESCE(pl.name->>'fr_FR', pl.name->>'en_US')) AS liste,
                   MAX(CASE WHEN pi.compute_price = 'fixed' AND pt.list_price > 0
                            THEN (1 - pi.fixed_price / pt.list_price) * 100.0
                            ELSE COALESCE(pi.percent_price, 0) END) AS remise
              FROM product_pricelist_item pi
              JOIN product_pricelist pl ON pl.id = pi.pricelist_id
              LEFT JOIN product_product pp ON pp.id = pi.product_id
              JOIN product_template pt
                ON pt.id = COALESCE(pi.product_tmpl_id, pp.product_tmpl_id)
             WHERE COALESCE(pi.product_tmpl_id, pp.product_tmpl_id) = ANY(%s)
             GROUP BY 1
        """, (list(tmpl_ids),))
        return {t: {'liste': nom, 'remise': int(round(float(rem or 0)))}
                for t, nom, rem in request.env.cr.fetchall() if t}

    @http.route('/mavie/api/solde-context', type='json', auth='user', methods=['POST'], csrf=False)
    def api_solde_context(self, **kw):
        """Tout ce que le panneau « Solder » affiche avant de valider :
        prix catalogue, et pour chaque magasin son stock de l'article, sa
        liste de soldes (existante ou à créer) et la solde déjà en place."""
        try:
            tmpl = request.env['product.template'].sudo().browse(int(kw.get('product_tmpl_id') or 0))
            if not tmpl.exists():
                return {'error': 'Référence introuvable.'}
            # Solde d'une seule couleur (bouton sur la ligne variante de la
            # page Action) : stock, solde en place et prix de CETTE couleur.
            couleur = (kw.get('couleur') or '').strip()
            variantes = None
            if couleur:
                variantes = self._solde_variantes_couleur(tmpl, couleur)
                if not variantes:
                    return {'error': 'Aucune variante « %s » pour cette référence.' % couleur}
            catalogue_ht = min(variantes.mapped('lst_price')) if variantes else tmpl.list_price
            mappings = self._solde_mappings()

            # Stock de l'article par magasin (négatifs ramenés à 0 : un
            # stock négatif n'est pas de la marchandise à solder).
            request.env.cr.execute("""
                SELECT w.id, COALESCE(SUM(sq.quantity), 0)
                  FROM stock_quant sq
                  JOIN product_product pp ON pp.id = sq.product_id
                  JOIN stock_location l ON l.id = sq.location_id
                  JOIN stock_warehouse w ON w.id = ANY(%(wh)s)
                  JOIN stock_location wl ON wl.id = w.lot_stock_id
                 WHERE pp.product_tmpl_id = %(tmpl)s{V}
                   AND l.parent_path LIKE wl.parent_path || '%%'
                 GROUP BY 1
            """.replace('{V}', ' AND pp.id = ANY(%(vids)s)' if variantes else ''),
                {'wh': mappings.mapped('warehouse_id').ids or [-1], 'tmpl': tmpl.id,
                 'vids': variantes.ids if variantes else []})
            stock_by_wh = {r[0]: max(0, int(round(r[1] or 0))) for r in request.env.cr.fetchall()}

            magasins = []
            ratio_ref = None
            for m in mappings:
                ratio = self._solde_tax_ratio(tmpl, m.company_id)
                ratio_ref = ratio_ref or ratio
                configs = self._solde_store_configs(m)
                lst = self._solde_find_list(configs)
                rule = None
                if lst and variantes:
                    # Solde déjà posée sur cette couleur, sinon celle de
                    # l'article entier (qui s'applique aussi à la couleur).
                    for vr in variantes:
                        rule = self._solde_rule_variante(lst, vr)
                        if rule:
                            break
                if lst and not rule:
                    rule = self._solde_rule(lst, tmpl)
                regle = None
                if rule:
                    prix = rule.fixed_price if rule.compute_price == 'fixed' else (
                        catalogue_ht * (1 - (rule.percent_price or 0) / 100.0))
                    regle = {
                        'prix_ttc': round(prix * ratio, 2),
                        'date_start': fields.Datetime.to_string(rule.date_start)[:10] if rule.date_start else '',
                        'date_end': fields.Datetime.to_string(rule.date_end)[:10] if rule.date_end else '',
                    }
                magasins.append({
                    'shop_field': m.shop_field,
                    'magasin': m.warehouse_id.name,
                    'libelle': m.shop_label or m.warehouse_id.name,
                    'societe': m.company_id.name,
                    'stock': stock_by_wh.get(m.warehouse_id.id, 0),
                    'caisses': configs.mapped('name'),
                    'liste': {'id': lst.id, 'name': lst.name, 'nb_articles': len(lst.item_ids),
                              'autres_caisses': self._solde_other_configs(lst, configs).mapped('name')} if lst else None,
                    'nom_propose': 'Solde %s' % (m.shop_label or m.warehouse_id.name),
                    'regle': regle,
                })
            magasins.sort(key=lambda x: (x['societe'], x['magasin']))
            ratio_ref = ratio_ref or 1.0
            return {
                'operation': self._solde_operation(),
                'product_tmpl_id': tmpl.id,
                'nom': tmpl.name,
                'reference': tmpl.base_pivot_reference or tmpl.default_code or tmpl.name,
                'prix_catalogue_ttc': round(catalogue_ht * ratio_ref, 2),
                'couleur': couleur,
                'nb_variantes': len(variantes) if variantes else 0,
                'magasins': magasins,
            }
        except Exception as e:
            _logger.error(f"Erreur api_solde_context: {str(e)}", exc_info=True)
            return {'error': str(e)}

    # ─────────────────────────────────────────────────────────────
    # SOLDER PAR UNE PROMOTION (Remise & Fidélité)
    #
    # ANALYSE EN BASE (2026-09-23) : les magasins soldent surtout par
    # loyalty.program, pas par les listes de prix — 636 promotions actives
    # contre 15 listes. Modèle copié sur « Solde été 2025 - 55A16 » :
    #   programme : program_type=promotion, applies_on=current, trigger=auto,
    #               pos_ok, société, dates, caisses (pos_config_ids) ;
    #   règle     : mode auto, minimum_qty 0, minimum_amount 1 TTC,
    #               product_ids = les variantes concernées ;
    #   récompense: discount / percent / applicability specific, mêmes
    #               variantes.
    # Les remises y sont TOUJOURS en pourcentage : le prix soldé saisi dans
    # le dashboard est converti (249 → 199 = 20,08 %), d'où les pourcentages
    # à décimales des promotions existantes.
    # ─────────────────────────────────────────────────────────────

    def _promo_pourcentage(self, catalogue_ttc, prix_ttc):
        if catalogue_ttc <= 0:
            return None
        return round(max(0.0, (1 - prix_ttc / catalogue_ttc)) * 100, 2)

    def _solde_creer_promotion(self, tmpl, variantes, prix_ttc, plan, date_start, date_end, couleur):
        """Une promotion par société concernée, limitée aux caisses des
        magasins cochés. Retourne la liste des résultats par magasin."""
        Program = request.env['loyalty.program'].sudo()
        resultats = []
        par_societe = {}
        for m, configs, _lst, _nom, ratio in plan:
            par_societe.setdefault(m.company_id, {'mappings': [], 'configs': request.env['pos.config'].sudo().browse(),
                                                  'ratio': ratio})
            par_societe[m.company_id]['mappings'].append(m)
            par_societe[m.company_id]['configs'] |= configs

        libelle = (tmpl.base_pivot_reference or tmpl.default_code or tmpl.name or '').strip()
        if couleur:
            libelle += ' ' + couleur

        for company, data in par_societe.items():
            ratio = data['ratio']
            catalogue_ht = min(variantes.mapped('lst_price')) if variantes else tmpl.list_price
            pct = self._promo_pourcentage(catalogue_ht * ratio, prix_ttc)
            if not pct:
                return None, ('Prix soldé trop proche du prix de vente : la remise calculée serait de 0 %.')
            programme = Program.create({
                'name': 'Solde %s — %s' % (libelle, (date_start or fields.Date.today().isoformat())),
                'program_type': 'promotion',
                'applies_on': 'current',
                'trigger': 'auto',
                'company_id': company.id,
                'currency_id': company.currency_id.id,
                # Caisses uniquement : le dashboard solde le magasin, pas les
                # bons de vente inter-sociétés.
                'pos_ok': True,
                'sale_ok': False,
                'date_from': date_start or False,
                'date_to': date_end or False,
                'pos_config_ids': [(6, 0, data['configs'].ids)],
                'rule_ids': [(0, 0, {
                    'mode': 'auto',
                    'minimum_qty': 0,
                    'minimum_amount': 1,
                    'minimum_amount_tax_mode': 'incl',
                    'reward_point_mode': 'order',
                    'reward_point_amount': 1,
                    'product_ids': [(6, 0, variantes.ids)],
                })],
                'reward_ids': [(0, 0, {
                    'reward_type': 'discount',
                    'discount_mode': 'percent',
                    'discount': pct,
                    'discount_applicability': 'specific',
                    'discount_product_ids': [(6, 0, variantes.ids)],
                    'required_points': 1,
                })],
            })
            for m in data['mappings']:
                resultats.append({
                    'magasin': m.warehouse_id.name,
                    'liste': programme.name,
                    'liste_creee': True,
                    'promotion': True,
                    'remise_pct': pct,
                    'ancien_prix_ttc': None,
                    'prix_ttc': round(prix_ttc, 2),
                    'prix_ht': round(prix_ttc / ratio, 2),
                })
        return resultats, None

    # ─────────────────────────────────────────────────────────────
    # CODE PROMO / CARTE CADEAU / FIDÉLITÉ, depuis le même pop-up
    #
    # DEMANDE UTILISATRICE (2026-09-23) : « je veux tout dans le pop-up de
    # solde, pas séparé ». Structures copiées sur les programmes existants
    # de la base :
    #   code promo   : « Quiz MA VIE Avril 40% » — trigger/mode with_code,
    #                  code saisi en caisse, remise en %.
    #   carte cadeau : « Cartes-cadeaux » — applies_on future, points en
    #                  monnaie (reward_point_mode money), récompense
    #                  per_point sur la commande ; les cartes elles-mêmes
    #                  sont des loyalty.card avec leur code et leur montant.
    #   fidélité     : « fidélité » — points gagnés par MAD dépensé,
    #                  échangés contre un % de remise.
    # ─────────────────────────────────────────────────────────────

    def _programme_societes(self, plan):
        """{société: caisses} des magasins cochés."""
        par_societe = {}
        for m, configs, _lst, _nom, _ratio in plan:
            entree = par_societe.setdefault(m.company_id, {
                'mappings': [], 'configs': request.env['pos.config'].sudo().browse()})
            entree['mappings'].append(m)
            entree['configs'] |= configs
        return par_societe

    def _creer_code_promo(self, tmpl, variantes, kw, plan, date_start, date_end, libelle):
        code = (kw.get('code') or '').strip()
        if not code:
            return None, 'Saisissez le code à taper en caisse.'
        if request.env['loyalty.rule'].sudo().search_count([('code', '=', code)]):
            return None, 'Ce code existe déjà : choisissez-en un autre.'
        pct = self._promo_pourcentage_demandee(tmpl, variantes, kw, plan)
        if not pct:
            return None, 'Indiquez un prix soldé ou une remise.'
        Program = request.env['loyalty.program'].sudo()
        resultats = []
        for company, data in self._programme_societes(plan).items():
            programme = Program.create({
                'name': 'Code promo %s — %s' % (code, libelle),
                'program_type': 'promo_code',
                'applies_on': 'current',
                'trigger': 'with_code',
                'company_id': company.id,
                'currency_id': company.currency_id.id,
                'pos_ok': True,
                'sale_ok': False,
                'date_from': date_start or False,
                'date_to': date_end or False,
                'pos_config_ids': [(6, 0, data['configs'].ids)],
                'rule_ids': [(0, 0, {
                    'mode': 'with_code',
                    'code': code,
                    'minimum_qty': 0,
                    'minimum_amount': 0,
                    'minimum_amount_tax_mode': 'incl',
                    'reward_point_mode': 'order',
                    'reward_point_amount': 1,
                })],
                'reward_ids': [(0, 0, {
                    'reward_type': 'discount',
                    'discount_mode': 'percent',
                    'discount': pct,
                    'discount_applicability': 'specific',
                    'discount_product_ids': [(6, 0, variantes.ids)],
                    'required_points': 1,
                })],
            })
            for m in data['mappings']:
                resultats.append({'magasin': m.warehouse_id.name, 'liste': programme.name,
                                  'promotion': True, 'remise_pct': pct, 'code': code,
                                  'liste_creee': True, 'ancien_prix_ttc': None,
                                  'prix_ttc': 0, 'prix_ht': 0})
        return resultats, None

    def _creer_carte_cadeau(self, kw, plan, date_end, libelle):
        try:
            montant = float(kw.get('montant') or 0)
            nb = int(kw.get('nb_cartes') or 1)
        except (TypeError, ValueError):
            return None, 'Montant ou nombre de cartes invalide.'
        if montant <= 0:
            return None, 'Indiquez le montant de la carte.'
        if nb < 1 or nb > 200:
            return None, 'Le nombre de cartes doit être entre 1 et 200.'
        Program = request.env['loyalty.program'].sudo()
        Card = request.env['loyalty.card'].sudo()
        resultats = []
        for company, data in self._programme_societes(plan).items():
            # Une seule « caisse » de cartes cadeaux par société : on
            # réutilise le programme existant s'il y en a un.
            programme = Program.search([('program_type', '=', 'gift_card'), ('active', '=', True),
                                        ('company_id', 'in', [company.id, False])], limit=1)
            creee = False
            if not programme:
                programme = Program.create({
                    'name': 'Cartes cadeaux %s' % company.name,
                    'program_type': 'gift_card',
                    'applies_on': 'future',
                    'trigger': 'auto',
                    'company_id': company.id,
                    'currency_id': company.currency_id.id,
                    'pos_ok': True,
                    'sale_ok': True,
                    'rule_ids': [(0, 0, {
                        'mode': 'auto', 'minimum_qty': 0, 'minimum_amount': 0,
                        'minimum_amount_tax_mode': 'incl',
                        'reward_point_mode': 'money', 'reward_point_amount': 1,
                        'reward_point_split': True,
                    })],
                    'reward_ids': [(0, 0, {
                        'reward_type': 'discount', 'discount_mode': 'per_point',
                        'discount': 1, 'discount_applicability': 'order',
                        'required_points': 0.001,
                    })],
                })
                creee = True
            cartes = Card.create([{
                'program_id': programme.id,
                'points': montant,
                'expiration_date': date_end or False,
            } for _ in range(nb)])
            for m in data['mappings']:
                resultats.append({'magasin': m.warehouse_id.name, 'liste': programme.name,
                                  'liste_creee': creee, 'cartes': cartes.mapped('code'),
                                  'montant': montant, 'ancien_prix_ttc': None,
                                  'prix_ttc': montant, 'prix_ht': montant})
        return resultats, None

    def _creer_fidelite(self, kw, plan, date_start, date_end):
        try:
            points = float(kw.get('points_par_mad') or 0)
            requis = float(kw.get('points_requis') or 0)
            remise = float(kw.get('remise_fidelite') or 0)
        except (TypeError, ValueError):
            return None, 'Valeurs de fidélité invalides.'
        if points <= 0 or requis <= 0 or not (0 < remise < 100):
            return None, 'Vérifiez les points gagnés, les points requis et la remise.'
        Program = request.env['loyalty.program'].sudo()
        resultats = []
        for company, data in self._programme_societes(plan).items():
            programme = Program.create({
                'name': 'Fidélité %s' % company.name,
                'program_type': 'loyalty',
                'applies_on': 'both',
                'trigger': 'auto',
                'company_id': company.id,
                'currency_id': company.currency_id.id,
                'pos_ok': True,
                'sale_ok': False,
                'date_from': date_start or False,
                'date_to': date_end or False,
                'pos_config_ids': [(6, 0, data['configs'].ids)],
                'rule_ids': [(0, 0, {
                    'mode': 'auto', 'minimum_qty': 0, 'minimum_amount': 0,
                    'minimum_amount_tax_mode': 'incl',
                    'reward_point_mode': 'money', 'reward_point_amount': points,
                })],
                'reward_ids': [(0, 0, {
                    'reward_type': 'discount', 'discount_mode': 'percent',
                    'discount': remise, 'discount_applicability': 'order',
                    'required_points': requis,
                })],
            })
            for m in data['mappings']:
                resultats.append({'magasin': m.warehouse_id.name, 'liste': programme.name,
                                  'liste_creee': True, 'fidelite': True,
                                  'points': points, 'points_requis': requis, 'remise_pct': remise,
                                  'ancien_prix_ttc': None, 'prix_ttc': 0, 'prix_ht': 0})
        return resultats, None

    def _promo_pourcentage_demandee(self, tmpl, variantes, kw, plan):
        """% de remise, à partir du prix soldé saisi (ou de la remise)."""
        try:
            remise = float(kw.get('remise_pct') or 0)
        except (TypeError, ValueError):
            remise = 0
        if remise > 0:
            return round(remise, 2)
        try:
            prix_ttc = float(kw.get('prix_ttc') or 0)
        except (TypeError, ValueError):
            return None
        if prix_ttc <= 0 or not plan:
            return None
        ratio = plan[0][4]
        catalogue_ht = min(variantes.mapped('lst_price')) if variantes else tmpl.list_price
        return self._promo_pourcentage(catalogue_ht * ratio, prix_ttc)

    @http.route('/mavie/api/solde-apply', type='json', auth='user', methods=['POST'], csrf=False)
    def api_solde_apply(self, **kw):
        """Applique la solde : pour chaque magasin choisi, ajoute (ou met à
        jour) la règle de l'article dans la liste de soldes du magasin, en
        créant cette liste si le magasin n'en a pas encore.

        Tout est validé AVANT la première écriture, et l'ensemble tient dans
        la transaction de la requête : si un seul magasin échoue, aucun
        magasin n'est modifié — jamais de solde appliquée à moitié.
        """
        try:
            tmpl = request.env['product.template'].sudo().browse(int(kw.get('product_tmpl_id') or 0))
            if not tmpl.exists():
                return {'error': 'Référence introuvable.'}
            try:
                prix_ttc = float(kw.get('prix_ttc') or 0)
            except (TypeError, ValueError):
                return {'error': 'Prix soldé invalide.'}
            date_start = (kw.get('date_start') or '').strip()
            date_end = (kw.get('date_end') or '').strip()
            demandes = kw.get('magasins') or []
            mode = kw.get('mode') or 'pricelist'
            # Carte cadeau et fidélité ne portent pas sur un prix d'article :
            # leurs propres champs sont vérifiés plus bas.
            besoin_prix = mode in ('pricelist', 'promotion')
            if not demandes:
                return {'error': 'Choisissez au moins un magasin.'}
            if besoin_prix and prix_ttc <= 0:
                return {'error': 'Le prix soldé doit être supérieur à 0.'}
            if date_end and date_start and date_end < date_start:
                return {'error': 'La date de fin est avant la date de début.'}

            # Solde d'une seule couleur (page Action) : une règle par taille.
            couleur = (kw.get('couleur') or '').strip()
            variantes = None
            if couleur:
                variantes = self._solde_variantes_couleur(tmpl, couleur)
                if not variantes:
                    return {'error': 'Aucune variante « %s » pour cette référence.' % couleur}
            catalogue_ht = min(variantes.mapped('lst_price')) if variantes else tmpl.list_price

            by_field = {m.shop_field: m for m in self._solde_mappings()}
            plan = []
            for d in demandes:
                m = by_field.get(d.get('shop_field'))
                if not m:
                    return {'error': 'Magasin inconnu ou inactif : %s' % d.get('shop_field')}
                configs = self._solde_store_configs(m)
                if not configs:
                    return {'error': '%s n\'a aucune caisse : impossible d\'y appliquer une solde.' % m.warehouse_id.name}
                ratio = self._solde_tax_ratio(tmpl, m.company_id)
                catalogue_ttc = catalogue_ht * ratio
                if besoin_prix and prix_ttc >= catalogue_ttc - 0.005:
                    return {'error': 'Le prix soldé (%.2f) doit être inférieur au prix de vente (%.2f TTC).' % (prix_ttc, catalogue_ttc)}
                lst = self._solde_find_list(configs)
                nom = (d.get('nom_liste') or '').strip() or 'Solde %s' % (m.shop_label or m.warehouse_id.name)
                plan.append((m, configs, lst, nom, ratio))

            # Mode « promotion » (Remise & Fidélité) : c'est l'outil que les
            # magasins utilisent vraiment pour solder. Choix fait dans le
            # panneau Solder (demande utilisatrice 2026-09-23).
            libelle = (tmpl.base_pivot_reference or tmpl.default_code or tmpl.name or '').strip()
            if couleur:
                libelle += ' ' + couleur
            cibles_prog = variantes if variantes else tmpl.product_variant_ids

            if mode in ('promo_code', 'gift_card', 'loyalty'):
                if mode == 'promo_code':
                    resultats, erreur = self._creer_code_promo(
                        tmpl, cibles_prog, kw, plan, date_start, date_end, libelle)
                elif mode == 'gift_card':
                    resultats, erreur = self._creer_carte_cadeau(kw, plan, date_end, libelle)
                else:
                    resultats, erreur = self._creer_fidelite(kw, plan, date_start, date_end)
                if erreur:
                    return {'error': erreur}
                _logger.info("Programme %s créé par %s pour %s",
                             mode, request.env.user.login, tmpl.display_name)
                _vider_cache_dashboard()
                return {'ok': True, 'resultats': resultats, 'mode': mode}

            if mode == 'promotion':
                cibles = cibles_prog
                resultats, erreur = self._solde_creer_promotion(
                    tmpl, cibles, prix_ttc, plan, date_start, date_end, couleur)
                if erreur:
                    return {'error': erreur}
                _logger.info("Promotion solde par %s : %s -> %s TTC (%s)",
                             request.env.user.login, tmpl.display_name, prix_ttc,
                             ', '.join(r['magasin'] for r in resultats))
                _vider_cache_dashboard()
                return {'ok': True, 'resultats': resultats, 'promotion': True}

            Pricelist = request.env['product.pricelist'].sudo()
            Item = request.env['product.pricelist.item'].sudo()
            resultats = []
            for m, configs, lst, nom, ratio in plan:
                creee = False
                if not lst:
                    lst = Pricelist.create({
                        'name': nom,
                        'company_id': m.company_id.id,
                        'currency_id': m.company_id.currency_id.id,
                    })
                    creee = True
                # Disponible sur la caisse, comme « Solde Sela Park » : la
                # liste par défaut de la caisse reste la liste normale.
                # pos.config.write exige la liste COMPLÈTE (commande 6) : il
                # compare l'ancienne et la nouvelle pour interdire tout
                # RETRAIT pendant une session ouverte. Un ajout reste permis,
                # session ouverte ou non — la caisse le verra au prochain
                # rechargement.
                for cfg in configs.filtered(lambda c: lst not in c.available_pricelist_ids):
                    vals_cfg = {'available_pricelist_ids': [(6, 0, (cfg.available_pricelist_ids | lst).ids)]}
                    if not cfg.use_pricelist:
                        vals_cfg['use_pricelist'] = True
                    cfg.write(vals_cfg)

                prix_ht = lst.currency_id.round(prix_ttc / ratio)
                base = {
                    'compute_price': 'fixed',
                    'fixed_price': prix_ht,
                    'min_quantity': 0,
                    'date_start': (date_start + ' 00:00:00') if date_start else False,
                    'date_end': (date_end + ' 23:59:59') if date_end else False,
                }
                ancien = None
                if variantes:
                    for vr in variantes:
                        vals = dict(base, applied_on='0_product_variant',
                                    product_id=vr.id, product_tmpl_id=tmpl.id)
                        rule = self._solde_rule_variante(lst, vr)
                        if rule:
                            if rule.compute_price == 'fixed' and ancien is None:
                                ancien = round(rule.fixed_price * ratio, 2)
                            rule.write(vals)
                        else:
                            vals['pricelist_id'] = lst.id
                            Item.create(vals)
                else:
                    vals = dict(base, applied_on='1_product', product_tmpl_id=tmpl.id)
                    rule = self._solde_rule(lst, tmpl)
                    if rule:
                        if rule.compute_price == 'fixed':
                            ancien = round(rule.fixed_price * ratio, 2)
                        rule.write(vals)
                    else:
                        vals['pricelist_id'] = lst.id
                        rule = Item.create(vals)
                resultats.append({
                    'magasin': m.warehouse_id.name,
                    'liste': lst.name,
                    'liste_creee': creee,
                    'ancien_prix_ttc': ancien,
                    'prix_ttc': round(prix_ht * ratio, 2),
                    'prix_ht': prix_ht,
                })
            _logger.info("Solde dashboard par %s : %s -> %s TTC dans %s",
                         request.env.user.login, tmpl.display_name + (' / ' + couleur if couleur else ''), prix_ttc,
                         ', '.join(r['magasin'] for r in resultats))
            _vider_cache_dashboard()
            return {'ok': True, 'resultats': resultats}
        except Exception as e:
            _logger.error(f"Erreur api_solde_apply: {str(e)}", exc_info=True)
            # La transaction de la requête est annulée par Odoo : aucune
            # écriture partielle ne reste en base.
            request.env.cr.rollback()
            return {'error': str(e)}

    def _solde_history(self, product_tmpl, variants):
        """Soldes PROGRAMMÉES de la référence, pour l'onglet « Soldes » de
        l'historique.

        DEMANDE UTILISATEUR (2026-09-21) : une solde lancée depuis le bouton
        « Solder » doit apparaître dans l'historique de la référence. Cet
        onglet ne listait que les VENTES en caisse à prix réduit : une solde
        qui vient d'être lancée, sans vente encore, n'y figurait pas.

        On lit donc les règles des listes de soldes (listes de prix dont le
        nom contient « solde », actives ou non) portant sur cet article ou
        sur une de ses variantes — celles créées depuis le dashboard comme
        celles saisies à la main dans Odoo. Prix affichés TTC, comme le reste
        du dashboard (les règles sont stockées HT).
        """
        # Depuis que le bouton réutilise la liste déjà rattachée à la caisse
        # (ex. « REMISE 20% »), une solde peut vivre dans une liste qui ne
        # s'appelle pas « solde » : on prend aussi toute liste rattachée à
        # une caisse, hors liste normale par défaut. Seules les règles posées
        # sur CET article comptent (la remise globale de 20 % n'en est pas).
        Item = request.env['product.pricelist.item'].sudo().with_context(active_test=False)
        cfg_lists = request.env['pos.config'].sudo().with_context(active_test=False).search([])
        cfg_lists = (cfg_lists.mapped('available_pricelist_ids') | cfg_lists.mapped('pricelist_id'))
        cfg_lists = cfg_lists.filtered(lambda p: not self._solde_is_default_list(p))
        items = Item.search([
            '|', ('pricelist_id.name', 'ilike', 'solde'), ('pricelist_id', 'in', cfg_lists.ids),
            '|',
            '&', ('applied_on', '=', '1_product'), ('product_tmpl_id', '=', product_tmpl.id),
            '&', ('applied_on', '=', '0_product_variant'), ('product_id', 'in', variants.ids),
        ], order='create_date desc, id desc')
        if not items:
            return []
        configs = request.env['pos.config'].sudo().with_context(active_test=False).search([
            '|', ('available_pricelist_ids', 'in', items.mapped('pricelist_id').ids),
            ('pricelist_id', 'in', items.mapped('pricelist_id').ids),
        ])
        now = fields.Datetime.now()
        out = []
        for it in items:
            pl = it.pricelist_id
            ratio = self._solde_tax_ratio(product_tmpl, pl.company_id or request.env.company)
            catalogue_ht = it.product_id.lst_price if it.applied_on == '0_product_variant' else product_tmpl.list_price
            if it.compute_price == 'fixed':
                prix_ht = it.fixed_price
            elif it.compute_price == 'percentage':
                prix_ht = catalogue_ht * (1 - (it.percent_price or 0.0) / 100.0)
            else:
                prix_ht = None
            if not pl.active:
                statut = 'inactive'
            elif it.date_end and it.date_end < now:
                statut = 'terminee'
            elif it.date_start and it.date_start > now:
                statut = 'a_venir'
            else:
                statut = 'en_cours'
            caisses = configs.filtered(lambda c: pl in c.available_pricelist_ids or c.pricelist_id == pl)
            catalogue_ttc = round(catalogue_ht * ratio, 2)
            prix_ttc = round(prix_ht * ratio, 2) if prix_ht is not None else None
            out.append({
                'date': fields.Datetime.to_string(it.create_date)[:16] if it.create_date else '—',
                'modifiee': (fields.Datetime.to_string(it.write_date)[:16]
                             if it.write_date and it.create_date and (it.write_date - it.create_date).total_seconds() > 60 else ''),
                'par': it.create_uid.name or '—',
                'liste': pl.name,
                'societe': pl.company_id.name or '',
                'magasins': caisses.mapped('name'),
                'variante': it.product_id.display_name if it.applied_on == '0_product_variant' else '',
                'prix_catalogue': catalogue_ttc,
                'prix_solde': prix_ttc,
                'remise_pct': (round((1 - prix_ttc / catalogue_ttc) * 100, 1)
                               if prix_ttc is not None and catalogue_ttc else None),
                'debut': fields.Datetime.to_string(it.date_start)[:10] if it.date_start else '',
                'fin': fields.Datetime.to_string(it.date_end)[:10] if it.date_end else '',
                'statut': statut,
            })
        return out
