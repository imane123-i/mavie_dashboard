/**
 * MaVie Dashboard – logique client principale
 * Module Odoo 17 : mavie_dashboard
 * Données natives Odoo (Achats, Ventes/POS, Stock) — plus de dépendance à
 * mv.article.base (Base Pivot) pour les calculs affichés.
 */

function detectCurrentPage() {
    var urlP = new URLSearchParams(window.location.search);
    var p = urlP.get('page');
    if (p && ['ventes', 'stock', 'commandes'].indexOf(p) !== -1) return p;
    var hash = window.location.hash;
    var hashMatch = hash.match(/page=([^&]+)/);
    if (hashMatch && ['ventes', 'stock', 'commandes'].indexOf(hashMatch[1]) !== -1) return hashMatch[1];
    try {
        var ref = new URLSearchParams(window.parent.location.search);
        var rp = ref.get('page');
        if (rp && ['ventes', 'stock', 'commandes'].indexOf(rp) !== -1) return rp;
    } catch(e) {}
    return 'ventes';
}

var currentPage = detectCurrentPage();
console.log('[MaVie Dashboard] Page détectée:', currentPage, '| URL:', window.location.href);

var state = {
    period: 'all',
    date_start: null,
    date_end: null,
    shop_field: null,
    collection_id: null,
    categ_id: null,
    batch_id: null,
    top_limit: 10,
    flop_limit: 10,
    top_products_all: [],
    flop_products_all: [],
    filters_loaded: false,
    shops: [],
    proches_rupture_30j_cache: [],
    detail: {
        article_id: null,
        shop_field: null,
        // Dernière liste de variantes (couleurs) chargée pour la fiche
        // produit ouverte — réutilisée par le popup de transfert (liste des
        // couleurs) et le popup détail couleur, sans nouvel appel serveur.
        variants: [],
    },
    transfer: {
        article_id: null,
        article_name: '',
        source_shop_field: null,
        dest_shop_field: null,
        color: null,
        // Repère commun posé sur tous les bons créés dans la même session de
        // transfert pour la même référence + destination (permet de les
        // regrouper à l'affichage quand plusieurs magasins source sont
        // nécessaires — voir _createTransferFromMatrix).
        group_ref: null,
        group_count: 0,
    },
    colorDetail: {
        article_id: null,
        product_name: '',
        color: null,
    },
};

var lastRupturesList = [];
var lastDormantList = [];
// Compteurs RÉELS (non plafonnés) — les listes ci-dessus sont limitées à
// 500 côté serveur pour l'affichage, mais le badge doit montrer le vrai
// total, pas la longueur de la liste tronquée.
var lastRupturesCount = 0;
var lastDormantCount = 0;
var _searchDebounce = null;

function el(id) { return document.getElementById(id); }

function formatNumber(n) {
    if (!n && n !== 0) return '—';
    return Math.round(n).toLocaleString('fr-FR');
}

function formatMAD(n) {
    if (!n && n !== 0) return '— MAD';
    n = Math.round(n * 100) / 100;
    return n.toLocaleString('fr-FR', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' MAD';
}

function formatPct(n) {
    if (!n && n !== 0) return '— %';
    return (Math.round(n * 10) / 10) + ' %';
}

function formatPctTight(n) {
    if (!n && n !== 0) return '—%';
    return (Math.round(n * 10) / 10) + '%';
}

function rpc(route, params) {
    return fetch(route, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ jsonrpc: '2.0', method: 'call', params: params || {} }),
    })
    .then(function(r) { return r.json(); })
    .then(function(json) {
        if (json.error) {
            console.error('RPC Error:', json.error);
            return { error: json.error.data && json.error.data.message || JSON.stringify(json.error) };
        }
        return json.result;
    })
    .catch(function(err) {
        console.error('RPC Fetch Error:', err);
        return { error: err.message };
    });
}

function showLoading(show) {
    var loader = el('loading-overlay');
    if (loader) loader.style.display = show ? 'flex' : 'none';
}

async function loadFilters() {
    try {
        var data = await rpc('/mavie/api/filters', {});
        if (!data || data.error) {
            console.error('Erreur chargement filtres:', data && data.error);
            return;
        }

        var collectionSelect = el('filter-collection');
        if (collectionSelect && data.collections) {
            data.collections.forEach(function(c) {
                var opt = document.createElement('option');
                opt.value = c.id;
                opt.textContent = c.name;
                collectionSelect.appendChild(opt);
            });
        }

        state.shops = data.shops || [];
        var magasinSelect = el('filter-magasin');
        if (magasinSelect && data.shops && data.shops.length > 0) {
            while (magasinSelect.options.length > 1) magasinSelect.remove(1);
            data.shops.forEach(function(s) {
                var opt = document.createElement('option');
                opt.value = s.field;
                opt.textContent = s.name;
                magasinSelect.appendChild(opt);
            });
            magasinSelect.disabled = false;
        } else if (magasinSelect) {
            var opt = document.createElement('option');
            opt.value = '';
            opt.textContent = 'Aucun magasin configuré';
            magasinSelect.appendChild(opt);
        }

        var categorySelect = el('filter-category');
        if (categorySelect && data.categories) {
            data.categories.forEach(function(c) {
                var opt = document.createElement('option');
                opt.value = c.id;
                opt.textContent = c.name;
                categorySelect.appendChild(opt);
            });
        }

        var batchSelect = el('filter-batch');
        if (batchSelect && data.batches && data.batches.length > 0) {
            data.batches.forEach(function(b) {
                var opt = document.createElement('option');
                opt.value = b.id;
                opt.textContent = b.name + (b.collection && b.collection !== '—' ? ' (' + b.collection + ')' : '');
                batchSelect.appendChild(opt);
            });
        }

        state.filters_loaded = true;
        _populateDetailFilters(data);

    } catch (err) {
        console.error('Erreur loadFilters:', err);
    }
}

function _populateDetailFilters(data) {
    var detailMagasin = el('detail-filter-magasin');
    if (detailMagasin && data.shops) {
        while (detailMagasin.options.length > 1) detailMagasin.remove(1);
        data.shops.forEach(function(s) {
            var opt = document.createElement('option');
            opt.value = s.field;
            opt.textContent = s.name;
            detailMagasin.appendChild(opt);
        });
    }

    var transferDest = el('transfer-dest-shop');
    if (transferDest && data.shops) {
        while (transferDest.options.length > 1) transferDest.remove(1);
        data.shops.forEach(function(s) {
            var opt = document.createElement('option');
            opt.value = s.field;
            opt.textContent = s.name;
            transferDest.appendChild(opt);
        });
    }
}

function _pad2(n) { return n < 10 ? '0' + n : '' + n; }
function _fmtDate(y, m, d) { return y + '-' + _pad2(m) + '-' + _pad2(d); }

// Convertit une valeur <input type="week"> (ex: "2026-W31") en {start, end}
// (lundi -> dimanche de cette semaine ISO).
function _isoWeekToRange(weekValue) {
    var parts = weekValue.split('-W');
    var year = parseInt(parts[0], 10);
    var week = parseInt(parts[1], 10);
    // 4 janvier est toujours dans la semaine ISO 1
    var jan4 = new Date(year, 0, 4);
    var jan4Day = jan4.getDay() || 7; // dimanche = 0 -> 7
    var monday1 = new Date(jan4);
    monday1.setDate(jan4.getDate() - (jan4Day - 1));
    var monday = new Date(monday1);
    monday.setDate(monday1.getDate() + (week - 1) * 7);
    var sunday = new Date(monday);
    sunday.setDate(monday.getDate() + 6);
    return {
        start: _fmtDate(monday.getFullYear(), monday.getMonth() + 1, monday.getDate()),
        end: _fmtDate(sunday.getFullYear(), sunday.getMonth() + 1, sunday.getDate()),
    };
}

function _computePeriodDates() {
    var typeEl = el('filter-period-type');
    var type = typeEl ? typeEl.value : 'all';

    if (type === 'all') {
        return { start: null, end: null };
    }

    if (type === 'week') {
        var weekEl = el('filter-period-week');
        if (weekEl && weekEl.value) return _isoWeekToRange(weekEl.value);
        return { start: null, end: null };
    }

    if (type === 'year') {
        var yearEl = el('filter-period-year');
        var y = yearEl && yearEl.value ? parseInt(yearEl.value, 10) : null;
        if (!y) return { start: null, end: null };
        return { start: _fmtDate(y, 1, 1), end: _fmtDate(y, 12, 31) };
    }

    if (type === 'custom') {
        var startEl = el('filter-date-start');
        var endEl = el('filter-date-end');
        return {
            start: (startEl && startEl.value) ? startEl.value : null,
            end: (endEl && endEl.value) ? endEl.value : null,
        };
    }

    // type === 'month' (par défaut)
    var monthEl = el('filter-period-month');
    var monthYearEl = el('filter-period-month-year');
    var m = monthEl && monthEl.value ? parseInt(monthEl.value, 10) : null;
    var my = monthYearEl && monthYearEl.value ? parseInt(monthYearEl.value, 10) : null;
    if (!m || !my) return { start: null, end: null };
    var lastDay = new Date(my, m, 0).getDate();
    return { start: _fmtDate(my, m, 1), end: _fmtDate(my, m, lastDay) };
}

function _updatePeriodVisibility() {
    var typeEl = el('filter-period-type');
    var type = typeEl ? typeEl.value : 'all';

    var monthEl = el('filter-period-month');
    var monthYearEl = el('filter-period-month-year');
    var weekEl = el('filter-period-week');
    var yearEl = el('filter-period-year');
    var customGroup = el('filter-period-custom-group');

    if (monthEl) monthEl.style.display = (type === 'month') ? '' : 'none';
    if (monthYearEl) monthYearEl.style.display = (type === 'month') ? '' : 'none';
    if (weekEl) weekEl.style.display = (type === 'week') ? '' : 'none';
    if (yearEl) yearEl.style.display = (type === 'year') ? '' : 'none';
    if (customGroup) customGroup.style.display = (type === 'custom') ? '' : 'none';
}

function _initPeriodFilters() {
    var monthEl = el('filter-period-month');
    var monthYearEl = el('filter-period-month-year');
    var yearEl = el('filter-period-year');
    var weekEl = el('filter-period-week');
    if (!monthEl || !monthYearEl || !yearEl) return;

    var monthNames = ['Janvier', 'Février', 'Mars', 'Avril', 'Mai', 'Juin', 'Juillet', 'Août', 'Septembre', 'Octobre', 'Novembre', 'Décembre'];
    var today = new Date();
    var currentYear = today.getFullYear();
    var currentMonth = today.getMonth() + 1;

    monthNames.forEach(function(name, idx) {
        var opt = document.createElement('option');
        opt.value = idx + 1;
        opt.textContent = name;
        monthEl.appendChild(opt);
    });
    monthEl.value = currentMonth;

    for (var y = currentYear + 1; y >= currentYear - 5; y--) {
        var optY1 = document.createElement('option');
        optY1.value = y;
        optY1.textContent = y;
        monthYearEl.appendChild(optY1);

        var optY2 = document.createElement('option');
        optY2.value = y;
        optY2.textContent = y;
        yearEl.appendChild(optY2);
    }
    monthYearEl.value = currentYear;
    yearEl.value = currentYear;

    if (weekEl) {
        var jan4 = new Date(currentYear, 0, 4);
        var jan4Day = jan4.getDay() || 7;
        var monday1 = new Date(jan4);
        monday1.setDate(jan4.getDate() - (jan4Day - 1));
        var weekNum = Math.round(((today - monday1) / 86400000 - 3 + ((monday1.getDay() + 6) % 7)) / 7) + 1;
        weekEl.value = currentYear + '-W' + _pad2(Math.max(1, weekNum));
    }

    _updatePeriodVisibility();
}

function updateFiltersFromUI() {
    var colEl  = el('filter-collection');
    var shopEl = el('filter-magasin');
    var catEl  = el('filter-category');
    var batEl  = el('filter-batch');
    var topLimitEl = el('top-limit');
    var flopLimitEl = el('flop-limit');

    var period = _computePeriodDates();

    state.collection_id = (colEl  && colEl.value)  ? colEl.value  : null;
    state.shop_field    = (shopEl && shopEl.value)  ? shopEl.value : null;
    state.categ_id      = (catEl  && catEl.value)   ? catEl.value  : null;
    state.batch_id      = (batEl  && batEl.value)   ? batEl.value  : null;
    state.date_start    = period.start;
    state.date_end      = period.end;
    state.top_limit     = (topLimitEl && topLimitEl.value && !isNaN(parseInt(topLimitEl.value))) ? Math.max(1, parseInt(topLimitEl.value)) : 10;
    state.flop_limit    = (flopLimitEl && flopLimitEl.value && !isNaN(parseInt(flopLimitEl.value))) ? Math.max(1, parseInt(flopLimitEl.value)) : 10;
}

function getFilterParams() {
    return {
        shop_field:    state.shop_field,
        collection_id: state.collection_id,
        categ_id:      state.categ_id,
        batch_id:      state.batch_id,
        date_start:    state.date_start,
        date_end:      state.date_end,
        top_limit:     state.top_limit,
        flop_limit:    state.flop_limit,
        page:          currentPage,
    };
}

function adjustUIForPage() {
    // ── Bascule entre l'ancien tableau de bord et le nouveau "Stock & Rupture" ──
    var stockOnlyIds  = ['stock-kpi-grid', 'stock-middle-section', 'stock-30j-section', 'stock-valorisation-section'];
    var legacyOnlyIds = ['main-kpi-grid', 'section-top-flop'];

    if (currentPage === 'stock') {
        stockOnlyIds.forEach(function(id)  { var e = el(id); if (e) e.style.display = ''; });
        legacyOnlyIds.forEach(function(id) { var e = el(id); if (e) e.style.display = 'none'; });
    } else {
        stockOnlyIds.forEach(function(id)  { var e = el(id); if (e) e.style.display = 'none'; });
        legacyOnlyIds.forEach(function(id) { var e = el(id); if (e) e.style.display = ''; });
    }

    var displayMap = {
        ventes: {
            'card-ca-total': 'block',
            'card-tickets': 'block',
            'card-panier-moyen': 'block',
            'card-qty-sold': 'block',
            'card-qty-purchased': 'none',
            'card-stock-total': 'none',
            'card-sell-through': 'block',
            'card-ruptures': 'none',
            'section-abc': 'block',
            'section-sales-chart': 'block'
        },
        stock: {
            'card-ca-total': 'none',
            'card-tickets': 'block',
            'card-panier-moyen': 'none',
            'card-qty-sold': 'none',
            'card-qty-purchased': 'none',
            'card-stock-total': 'block',
            'card-sell-through': 'block',
            'card-ruptures': 'block',
            'section-abc': 'none',
            'section-sales-chart': 'none'
        },
        commandes: {
            'card-ca-total': 'none',
            'card-tickets': 'block',
            'card-panier-moyen': 'none',
            'card-qty-sold': 'none',
            'card-qty-purchased': 'block',
            'card-stock-total': 'block',
            'card-sell-through': 'none',
            'card-ruptures': 'none',
            'section-abc': 'none',
            'section-sales-chart': 'none'
        }
    };

    var currentDisplay = displayMap[currentPage] || displayMap.ventes;
    for (var id in currentDisplay) {
        var element = el(id);
        if (element) element.style.display = currentDisplay[id];
    }

    var topTitleText = el('top-title-text');
    var flopTitleText = el('flop-title-text');

    var topCol4 = document.querySelector('.top-col-4');
    var topCol5 = document.querySelector('.top-col-5');
    var flopCol4 = document.querySelector('.flop-col-4');
    var flopCol5 = document.querySelector('.flop-col-5');

    if (currentPage === 'ventes') {
        if (topTitleText) topTitleText.textContent = '🏆 Top Produits (par Ventes)';
        if (flopTitleText) flopTitleText.textContent = '📉 Flop Produits (par Ventes)';
        if (topCol4) topCol4.textContent = 'Qté vendue';
        if (topCol5) topCol5.textContent = 'CA Potentiel';
        if (flopCol4) flopCol4.textContent = 'Qté vendue';
        if (flopCol5) flopCol5.textContent = 'CA Potentiel';
    } else if (currentPage === 'stock') {
        if (topTitleText) topTitleText.textContent = '📦 Top Stocks (Quantités Elevées)';
        if (flopTitleText) flopTitleText.textContent = '⚠️ Alertes Stock / Ruptures';
        if (topCol4) topCol4.textContent = 'Stock';
        if (topCol5) topCol5.textContent = 'Qté vendue';
        if (flopCol4) flopCol4.textContent = 'Stock';
        if (flopCol5) flopCol5.textContent = 'Qté vendue';
    } else if (currentPage === 'commandes') {
        if (topTitleText) topTitleText.textContent = '📥 Top Commandes (Achats)';
        if (flopTitleText) flopTitleText.textContent = '📉 Flop Commandes (Achats)';
        if (topCol4) topCol4.textContent = 'Qté Achetée';
        if (topCol5) topCol5.textContent = 'Stock';
        if (flopCol4) flopCol4.textContent = 'Qté Achetée';
        if (flopCol5) flopCol5.textContent = 'Stock';
    }
}

async function loadKPIs() {
    updateFiltersFromUI();
    adjustUIForPage();
    showLoading(true);

    var params = getFilterParams();
    var data = await rpc('/mavie/api/kpis', params);
    showLoading(false);

    if (!data || data.error) {
    console.error('Erreur KPIs:', data && data.error);
    showLoading(false);
    var errBox = el('main-kpi-grid') || el('stock-kpi-grid');
    if (errBox) {
        errBox.innerHTML = '<div style="grid-column:1/-1;color:#EF4444;padding:20px;text-align:center;">Erreur : ' + (data && data.error || 'inconnue') + '</div>';
    }
    return;
}

    if (data.is_modforlife) {
        _renderModForLifeDashboard(data);
        return;
    }
    var mflGrid = el('modforlife-kpi-grid');
    if (mflGrid) mflGrid.style.display = 'none';

    if (currentPage === 'stock') {
        _renderStockDashboard(data);
    } else {
        var kpiMap = {
            'kpi-ca-total':      formatMAD(data.ca_total),
            'kpi-ca-achat':      formatMAD(data.ca_achat),
            // CORRECTION #2b : On affiche references_count (nb SKUs actifs) et non tickets POS
            // La carte HTML indique "Références" donc on doit montrer le bon chiffre
            'kpi-tickets':       formatNumber(data.references_count || data.total_active_skus || data.tickets),
            'kpi-panier-moyen':  formatMAD(data.panier_moyen),
            'kpi-qty-sold':      formatNumber(data.qty_sold),
            'kpi-qty-purchased': formatNumber(data.qty_purchased),
            'kpi-stock-total':   formatNumber(data.stock_total),
            'kpi-sell-through':  formatPct(data.sell_through),
            'kpi-ruptures':      formatNumber(data.ruptures_count),
        };

        for (var id in kpiMap) {
            var el_obj = el(id);
            if (el_obj) el_obj.textContent = kpiMap[id];
        }

        var qtySoldSoldeEl = el('kpi-qty-sold-solde');
        if (qtySoldSoldeEl) {
            qtySoldSoldeEl.textContent = data.qty_sold_solde ? ('dont ' + formatNumber(data.qty_sold_solde) + ' en solde') : '';
        }

        lastRupturesList = data.ruptures_list || [];
        lastRupturesCount = data.ruptures_count || 0;

        var stEl = el('kpi-sell-through');
        if (stEl) {
            var st = data.sell_through || 0;
            stEl.style.color = st >= 70 ? '#10B981' : (st >= 40 ? '#F59E0B' : '#EF4444');
            // Peut légitimement dépasser 100% : Qté vendue vient du POS,
            // Qté achetée des commandes fournisseur — une partie du stock
            // vendu peut provenir d'un stock initial/ajustement jamais
            // passé par une commande fournisseur tracée (vendu > acheté
            // dans les seules données suivies, sans que ce soit une erreur).
            stEl.title = st > 100
                ? 'Peut dépasser 100% : une partie du stock vendu provient d\'un stock initial ou d\'un ajustement jamais enregistré comme commande fournisseur suivie.'
                : '';
        }

        var stockTotalKpiEl = el('kpi-stock-total');
        if (stockTotalKpiEl) {
            stockTotalKpiEl.style.color = (data.stock_total || 0) < 0 ? '#EF4444' : '';
        }

        var caAchatNoteEl = el('kpi-ca-achat-note');
        if (caAchatNoteEl) {
            // Beaucoup de bons de commande fournisseur sont saisis sans prix
            // unitaire dans cette base : CA Achat/Marge peuvent donc être
            // sous-estimés (voire à 0) même quand des quantités ont bien été
            // achetées. Ce n'est pas un bug du dashboard, mais un rappel que
            // le prix d'achat doit être renseigné sur les commandes.
            caAchatNoteEl.textContent = (data.ca_achat === 0 && data.qty_purchased > 0)
                ? '⚠️ prix d\'achat non renseigné sur les commandes'
                : '';
        }
        // Le backend renvoie toujours jusqu'à 100 lignes (voir dashboard.py) ;
        // on garde la liste complète en cache pour pouvoir changer le nombre
        // affiché (10/20/50...) sans refaire tout l'appel KPI (coûteux).
        state.top_products_all = data.top_products || [];
        state.flop_products_all = data.flop_products || [];
        _renderTopFlopFromCache();

        if (currentPage === 'ventes') {
            _renderABC(data.abc_analysis);
            loadSalesDaily();
        }
    }
}

function _renderModForLifeDashboard(data) {
    // MOD FOR LIFE n'est pas un magasin (pas de vente en caisse, pas
    // d'alertes rupture retail) : on masque tout l'affichage normal
    // (ventes/stock/commandes) et on montre sa propre grille dédiée.
    var idsToHide = [
        'main-kpi-grid', 'stock-kpi-grid', 'section-top-flop',
        'stock-middle-section', 'stock-30j-section', 'stock-valorisation-section',
        'section-abc', 'section-sales-chart',
    ];
    idsToHide.forEach(function(id) { var e = el(id); if (e) e.style.display = 'none'; });

    var grid = el('modforlife-kpi-grid');
    if (grid) grid.style.display = '';

    var kpiMap = {
        'mfl-ca-achats':    formatMAD(data.ca_achats_fournisseurs),
        'mfl-nb-commandes': formatNumber(data.nb_commandes_fournisseurs),
        'mfl-ca-ventes':    formatMAD(data.ca_ventes_societes),
        'mfl-stock':        formatNumber(data.stock_entrepot),
    };
    for (var id in kpiMap) {
        var e = el(id);
        if (e) e.textContent = kpiMap[id];
    }

    var qtyAchatsSubEl = el('mfl-qty-achats-sub');
    if (qtyAchatsSubEl) qtyAchatsSubEl.textContent = formatNumber(data.qty_achats_fournisseurs) + ' pièces';
    var qtyVentesSubEl = el('mfl-qty-ventes-sub');
    if (qtyVentesSubEl) qtyVentesSubEl.textContent = formatNumber(data.qty_ventes_societes) + ' pièces';

    var tbody = el('modforlife-ventes-tbody');
    if (!tbody) return;
    tbody.innerHTML = '';
    var rows = data.ventes_par_societe || [];
    if (rows.length === 0) {
        var tr = document.createElement('tr');
        var td = document.createElement('td');
        td.colSpan = 3;
        td.textContent = 'Aucune vente inter-société sur cette période.';
        td.style.textAlign = 'center';
        td.style.color = '#999';
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }
    rows.forEach(function(r) {
        var tr = document.createElement('tr');
        var tdName = document.createElement('td');
        tdName.textContent = r.societe;
        tr.appendChild(tdName);
        var tdQty = document.createElement('td');
        tdQty.textContent = formatNumber(r.qty);
        tr.appendChild(tdQty);
        var tdCa = document.createElement('td');
        tdCa.textContent = formatMAD(r.ca);
        tr.appendChild(tdCa);
        tbody.appendChild(tr);
    });
}

function _renderTopFlopFromCache() {
    _renderProductTable('top-products-tbody', state.top_products_all.slice(0, state.top_limit), false);
    _renderProductTable('flop-products-tbody', state.flop_products_all.slice(0, state.flop_limit), true);
}

function _renderProductTable(tbodyId, products, isFlop) {
    var tbody = el(tbodyId);
    if (!tbody) return;
    tbody.innerHTML = '';

    if (!products || products.length === 0) {
        var tr = document.createElement('tr');
        var td = document.createElement('td');
        td.colSpan = 5;
        td.textContent = 'Aucun produit';
        td.style.textAlign = 'center';
        td.style.color = '#999';
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }

    products.forEach(function(p, idx) {
        var tr = document.createElement('tr');
        tr.style.cursor = 'pointer';
        tr.onclick = function() { openDetail(p.id, p.name); };

        var tdRank = document.createElement('td');
        tdRank.textContent = (idx + 1);
        tdRank.style.fontWeight = '700';
        tdRank.style.color = isFlop ? '#EF4444' : '#7C3AED';
        tr.appendChild(tdRank);

        var tdName = document.createElement('td');
        tdName.textContent = p.name;
        tr.appendChild(tdName);

        var tdRef = document.createElement('td');
        tdRef.textContent = p.ref || '—';
        tdRef.style.color = '#64748B';
        tr.appendChild(tdRef);

        var tdCol4 = document.createElement('td');
        var tdCol5 = document.createElement('td');

        if (currentPage === 'ventes') {
            tdCol4.textContent = formatNumber(p.qty_sold);
            tdCol5.textContent = formatMAD(p.ca);
        } else if (currentPage === 'stock') {
            tdCol4.textContent = formatNumber(p.stock);
            tdCol5.textContent = formatNumber(p.qty_sold);
        } else if (currentPage === 'commandes') {
            tdCol4.textContent = formatNumber(p.qty_purchased);
            tdCol5.textContent = formatNumber(p.stock);
        }

        tr.appendChild(tdCol4);
        tr.appendChild(tdCol5);
        tbody.appendChild(tr);
    });
}

function _renderABC(abc) {
    var container = el('abc-analysis-content');
    if (!container) return;
    container.innerHTML = '';

    var cats = [
        { key: 'A', label: '🥇 Produits A — 80% du CA', cls: 'abc-a' },
        { key: 'B', label: '🥈 Produits B — 15% du CA', cls: 'abc-b' },
        { key: 'C', label: '🥉 Produits C — 5% du CA',  cls: 'abc-c' },
    ];

    cats.forEach(function(cat) {
        var section = document.createElement('div');
        section.className = 'abc-section ' + cat.cls;

        var h4 = document.createElement('h4');
        h4.textContent = cat.label;
        section.appendChild(h4);

        var list = abc && abc[cat.key] && abc[cat.key].length > 0 ? abc[cat.key] : [];
        if (list.length > 0) {
            var ul = document.createElement('ul');
            list.forEach(function(product) {
                var li = document.createElement('li');
                li.style.cursor = 'pointer';
                li.innerHTML = '<span class="abc-name">' + (product.name || '—') + '</span>'
                    + '<span class="abc-ca">' + formatMAD(product.ca) + '</span>';
                li.onclick = function() { openDetail(product.id, product.name); };
                ul.appendChild(li);
            });
            section.appendChild(ul);
        } else {
            var p = document.createElement('p');
            p.textContent = 'Aucun produit';
            p.style.opacity = '0.7';
            section.appendChild(p);
        }

        container.appendChild(section);
    });
}

var SHOP_CHART_COLORS = [
    '#7C3AED', '#2563EB', '#059669', '#D97706', '#DC2626',
    '#DB2777', '#0891B2', '#65A30D', '#9333EA', '#EA580C',
];

var lastSalesDailyData = null;
var salesChartMode = 'arrivage';

function _showChartTooltip(e, html) {
    var tip = el('chart-tooltip');
    if (tip) {
        tip.style.display = 'block';
        tip.style.left = (e.pageX + 10) + 'px';
        tip.style.top  = (e.pageY - 30) + 'px';
        tip.innerHTML = html;
    }
}
function _hideChartTooltip() {
    var tip = el('chart-tooltip');
    if (tip) tip.style.display = 'none';
}

async function loadSalesDaily() {
    var params = getFilterParams();
    var data = await rpc('/mavie/api/sales-daily', params);
    if (!data || data.error) return;
    lastSalesDailyData = data;
    _renderSalesChart();
}

function setSalesChartMode(mode) {
    salesChartMode = mode;
    var arrBtn = el('chart-mode-arrivage');
    var shopBtn = el('chart-mode-shop');
    if (arrBtn) arrBtn.classList.toggle('active', mode === 'arrivage');
    if (shopBtn) shopBtn.classList.toggle('active', mode === 'shop');
    _renderSalesChart();
}

function _renderSalesChart() {
    var chartDiv = el('sales-daily-chart');
    var legendDiv = el('sales-daily-legend');
    if (!chartDiv) return;
    chartDiv.innerHTML = '';
    if (legendDiv) { legendDiv.innerHTML = ''; legendDiv.style.display = 'none'; }

    var data = lastSalesDailyData;
    if (!data) return;

    if (salesChartMode === 'shop') {
        _renderSalesChartByShop(chartDiv, legendDiv, data.by_shop || []);
    } else {
        _renderSalesChartByArrivage(chartDiv, data.daily || []);
    }
}

function _renderSalesChartByArrivage(chartDiv, daily) {
    if (!daily || daily.length === 0) {
        chartDiv.innerHTML = '<p style="color:#999;text-align:center;padding:20px">Aucune donnée disponible</p>';
        return;
    }

    var maxCA = Math.max.apply(null, daily.map(function(d) { return d.ca || 0; }));
    if (maxCA === 0) maxCA = 1;

    daily.forEach(function(d) {
        var wrapper = document.createElement('div');
        wrapper.className = 'chart-bar-wrapper';

        var barInner = document.createElement('div');
        barInner.className = 'chart-bar-inner';

        var barHeight = Math.max(((d.ca / maxCA) * 180), 4);
        var bar = document.createElement('div');
        bar.className = 'chart-bar';
        bar.style.height = barHeight + 'px';
        bar.title = formatMAD(d.ca) + '\n' + d.articles + ' article(s)';

        bar.addEventListener('mouseover', function(e) {
            _showChartTooltip(e, '<strong>' + d.date + '</strong><br>' + formatMAD(d.ca) + '<br>' + d.articles + ' art.');
        });
        bar.addEventListener('mouseout', _hideChartTooltip);

        barInner.appendChild(bar);
        wrapper.appendChild(barInner);

        var label = document.createElement('div');
        label.className = 'chart-bar-label';
        label.textContent = d.label || d.date || '';
        wrapper.appendChild(label);

        chartDiv.appendChild(wrapper);
    });
}

function _renderSalesChartByShop(chartDiv, legendDiv, byShop) {
    if (!byShop || byShop.length === 0) {
        chartDiv.innerHTML = '<p style="color:#999;text-align:center;padding:20px">Aucune donnée disponible</p>';
        return;
    }

    // Palette stable par magasin : même couleur pour un magasin donné sur
    // toutes les barres, dans l'ordre où il apparaît (le magasin avec le
    // plus gros CA total passe en premier grâce au tri déjà fait côté
    // backend sur chaque arrivage).
    var shopColorByName = {};
    var colorIdx = 0;
    byShop.forEach(function(a) {
        a.shops.forEach(function(s) {
            if (!(s.shop in shopColorByName)) {
                shopColorByName[s.shop] = SHOP_CHART_COLORS[colorIdx % SHOP_CHART_COLORS.length];
                colorIdx++;
            }
        });
    });

    var totals = byShop.map(function(a) { return a.shops.reduce(function(sum, s) { return sum + (s.ca || 0); }, 0); });
    var maxCA = Math.max.apply(null, totals.concat([0]));
    if (maxCA === 0) maxCA = 1;

    byShop.forEach(function(a) {
        var totalCA = a.shops.reduce(function(sum, s) { return sum + (s.ca || 0); }, 0);

        var wrapper = document.createElement('div');
        wrapper.className = 'chart-bar-wrapper';

        var barInner = document.createElement('div');
        barInner.className = 'chart-bar-inner';

        var stack = document.createElement('div');
        stack.className = 'chart-bar-stack';
        var stackHeight = Math.max(((totalCA / maxCA) * 180), 4);
        stack.style.height = stackHeight + 'px';

        a.shops.forEach(function(s) {
            var segHeight = totalCA > 0 ? (s.ca / totalCA) * stackHeight : 0;
            if (segHeight <= 0) return;
            var seg = document.createElement('div');
            seg.className = 'chart-bar-segment';
            seg.style.height = segHeight + 'px';
            seg.style.background = shopColorByName[s.shop];
            seg.addEventListener('mouseover', function(e) {
                _showChartTooltip(e, '<strong>' + a.arrivage + '</strong><br>' + s.shop + ': ' + formatMAD(s.ca) + '<br>' + s.qty + ' art.');
            });
            seg.addEventListener('mouseout', _hideChartTooltip);
            stack.appendChild(seg);
        });

        barInner.appendChild(stack);
        wrapper.appendChild(barInner);

        var label = document.createElement('div');
        label.className = 'chart-bar-label';
        label.textContent = a.arrivage || '';
        wrapper.appendChild(label);

        chartDiv.appendChild(wrapper);
    });

    if (legendDiv) {
        legendDiv.style.display = 'flex';
        Object.keys(shopColorByName).forEach(function(shopName) {
            var item = document.createElement('div');
            item.className = 'chart-legend-item';
            var swatch = document.createElement('span');
            swatch.className = 'chart-legend-swatch';
            swatch.style.background = shopColorByName[shopName];
            item.appendChild(swatch);
            var text = document.createElement('span');
            text.textContent = shopName;
            item.appendChild(text);
            legendDiv.appendChild(item);
        });
    }
}

async function openDetail(articleId, productName) {
    var overlay = el('detail-overlay');
    if (!overlay) return;
    overlay.classList.add('active');

    var nameEl = el('detail-name');
    if (nameEl) nameEl.textContent = 'Chargement...';

    var detailShopEl = el('detail-filter-magasin');
    state.detail.article_id = articleId;
    state.detail.shop_field = detailShopEl ? detailShopEl.value || null : state.shop_field;

    await _fetchAndRenderDetail();
}

async function refreshDetail() {
    var detailShopEl = el('detail-filter-magasin');
    state.detail.shop_field = detailShopEl ? detailShopEl.value || null : null;
    await _fetchAndRenderDetail();
}

async function _fetchAndRenderDetail() {
    var params = {
        article_id: state.detail.article_id,
        shop_field: state.detail.shop_field,
        batch_id: state.batch_id,
        collection_id: state.collection_id,
    };

    var data = await rpc('/mavie/api/product-detail', params);

    if (!data || data.error) {
        var nameEl = el('detail-name');
        if (nameEl) nameEl.textContent = 'Erreur : ' + (data && data.error || 'Inconnu');
        return;
    }

    if (el('detail-name'))       el('detail-name').textContent = data.name || '—';
    var refEl = el('detail-ref');
    if (refEl) {
        if (data.ref && data.ref !== '—') {
            refEl.textContent = 'Réf: ' + data.ref;
            refEl.style.display = '';
        } else {
            refEl.style.display = 'none';
        }
    }
    if (el('detail-collection')) el('detail-collection').textContent = data.collection_name || '—';
    if (el('detail-family'))     el('detail-family').textContent     = data.family || '—';

    var imgEl = el('detail-image');
    if (imgEl) {
        if (data.image_url) {
            imgEl.src = data.image_url;
            imgEl.style.display = 'block';
        } else {
            imgEl.style.display = 'none';
        }
    }

    var kpiMap = {
        'detail-qty-sold':      formatNumber(data.qty_sold),
        'detail-qty-purchased': formatNumber(data.qty_purchased),
        'detail-stock-total':   formatNumber(data.stock_total),
        'detail-ca':            formatMAD(data.ca),
        'detail-ca-achat':      formatMAD(data.ca_achat),
        'detail-sell-through':  formatPct(data.sell_through),

    };
    for (var id in kpiMap) {
        var e = el(id);
        if (e) e.textContent = kpiMap[id];
    }

    var detailQtySoldSoldeEl = el('detail-qty-sold-solde');
    if (detailQtySoldSoldeEl) {
        detailQtySoldSoldeEl.textContent = data.qty_sold_solde ? ('dont ' + formatNumber(data.qty_sold_solde) + ' en solde') : '';
    }

    // CA Vendu est affiché TTC (ce que le client a payé) alors que CA Achat
    // est HT (ce qui est facturé par le fournisseur) — deux bases fiscales
    // différentes côte à côte. On affiche l'équivalent HT du CA Vendu pour
    // permettre une comparaison HT contre HT sans calcul mental.
    var detailCaHtEl = el('detail-ca-ht');
    if (detailCaHtEl) {
        detailCaHtEl.textContent = (data.ca_ht && data.ca_ht !== data.ca)
            ? ('soit ' + formatMAD(data.ca_ht) + ' HT')
            : '';
    }

    var detailCaAchatNoteEl = el('detail-ca-achat-note');
    if (detailCaAchatNoteEl) {
        detailCaAchatNoteEl.textContent = (data.ca_achat === 0 && data.qty_purchased > 0)
            ? '⚠️ prix d\'achat non renseigné'
            : '';
    }

    var stEl = el('detail-sell-through');
    if (stEl) {
        var st = data.sell_through || 0;
        stEl.style.color = st >= 70 ? '#10B981' : (st >= 40 ? '#F59E0B' : '#EF4444');
        stEl.title = st > 100
            ? 'Peut dépasser 100% : une partie du stock vendu provient d\'un stock initial ou d\'un ajustement jamais enregistré comme commande fournisseur suivie.'
            : '';
    }

    var ecartEl = el('detail-stock-ecart');
    var stockTotalEl = el('detail-stock-total');
    if (ecartEl) {
        var ecart = data.stock_ecart || 0;
        var stockTotal = data.stock_total || 0;
        // Achetée - Vendue = stock théorique. Un écart notable avec le stock
        // réel Odoo indique des mouvements de stock non tracés (pertes,
        // ventes hors POS, stock négatif dans un magasin...) — pas un bug
        // du dashboard, mais un signal à vérifier dans Odoo.
        if (stockTotal < 0) {
            // Stock réel négatif = plus vendu en caisse que jamais réceptionné
            // dans Odoo pour ce(s) magasin(s) : message en clair plutôt que le
            // seul libellé technique "écart", qui prêtait à confusion.
            ecartEl.style.display = 'block';
            ecartEl.textContent = '⚠️ Stock négatif — ventes non couvertes par des réceptions trackées dans Odoo';
            ecartEl.title = 'Stock réel Odoo = ' + formatNumber(stockTotal) + ' (négatif). '
                + 'Stock théorique (achetée − vendue) = ' + formatNumber(data.stock_theorique)
                + '. Cela signifie que plus d\'unités ont été vendues en caisse que de réceptions enregistrées dans Odoo pour ce(s) magasin(s) — à vérifier : réceptions manquantes, transferts non tracés, ou ventes hors POS.';
        } else if (Math.abs(ecart) >= 1) {
            ecartEl.style.display = 'block';
            ecartEl.textContent = '⚠️ écart ' + (ecart > 0 ? '+' : '') + formatNumber(ecart) + ' vs théorique';
            ecartEl.title = 'Stock théorique (achetée − vendue) = ' + formatNumber(data.stock_theorique)
                + ', stock réel Odoo = ' + formatNumber(data.stock_total)
                + '. Écart probable : sorties de stock non tracées.';
        } else {
            ecartEl.style.display = 'none';
        }
    }
    if (stockTotalEl) {
        stockTotalEl.style.color = (data.stock_total || 0) < 0 ? '#EF4444' : '';
    }

    state.detail.variants = data.variants || [];

    // Le tableau Variantes Couleurs ne liste que les couleurs ACTIVES —
    // si des couleurs ont été discontinuées, leur historique achats/ventes
    // reste compté dans les cartes KPI (Qté vendue/achetée, qui filtrent
    // par produit entier, pas par variante) mais n'apparaît dans AUCUNE
    // ligne du tableau ci-dessous. Sans ce rappel, la somme des lignes ne
    // colle jamais aux cartes et ça ressemble à une erreur de calcul.
    var visibleQtySold = (data.variants || []).reduce(function(s, v) { return s + (v.qty || 0); }, 0);
    var archivedGapNote = document.querySelector('#detail-variants-archived-note');
    var variantsSection = document.querySelector('#detail-variants-tbody') && document.querySelector('#detail-variants-tbody').closest('.detail-section');
    var archivedGap = (data.qty_sold || 0) - visibleQtySold;
    if (archivedGap > 0 && variantsSection) {
        if (!archivedGapNote) {
            archivedGapNote = document.createElement('div');
            archivedGapNote.id = 'detail-variants-archived-note';
            archivedGapNote.style.margin = '4px 0 10px';
            archivedGapNote.style.padding = '6px 10px';
            archivedGapNote.style.fontSize = '0.8rem';
            archivedGapNote.style.color = '#3730A3';
            archivedGapNote.style.background = '#EEF2FF';
            archivedGapNote.style.borderRadius = '6px';
            var h3 = variantsSection.querySelector('h3');
            if (h3) h3.insertAdjacentElement('afterend', archivedGapNote);
        }
        archivedGapNote.textContent = 'ℹ️ ' + formatNumber(archivedGap) + ' vente(s) supplémentaire(s) sur des couleurs aujourd\'hui '
            + 'désactivées/discontinuées (non listées ci-dessous) sont comptées dans les cartes en haut de fiche — '
            + 'la somme du tableau ne peut donc pas toujours égaler "Qté vendue".';
        archivedGapNote.style.display = '';
    } else if (archivedGapNote) {
        archivedGapNote.style.display = 'none';
    }

    _renderStockByStore('detail-stock-pivot-tbody', data.stock_by_store, state.detail.shop_field);
    _renderVariants('detail-variants-tbody', data.variants, state.detail.shop_field, data.has_base_pivot_data);
    _renderVerification(data.verification);

    var batchEl = el('detail-batch-info');
    if (batchEl) {
        // Arrivage/collection natifs (product.arrivage / product.collection),
        // pas Base Pivot — voir data.batch_name / data.collection_name.
        var hasBatch = data.batch_name && data.batch_name !== '—';
        var hasCollection = data.collection_name && data.collection_name !== '—';
        if (hasBatch || hasCollection) {
            var parts = [];
            if (hasBatch) parts.push('<strong>' + data.batch_name + '</strong>');
            if (hasCollection) parts.push('Collection: <em>' + data.collection_name + '</em>');
            batchEl.innerHTML = parts.join(' — ');
            batchEl.style.display = 'block';
        } else {
            batchEl.style.display = 'none';
        }
    }
}

function _renderStockByStore(tbodyId, stores, activeShop) {
    var tbody = el(tbodyId);
    if (!tbody) return;
    tbody.innerHTML = '';

    if (!stores || stores.length === 0) {
        var tr = document.createElement('tr');
        var td = document.createElement('td');
        td.colSpan = 3;
        td.textContent = 'Aucun dispatch enregistré';
        td.style.textAlign = 'center';
        td.style.color = '#999';
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }

    // Ce tableau montre TOUJOURS tout le réseau, même quand "Filtrer par
    // magasin" ci-dessus cible un seul magasin (utile pour voir où
    // transférer depuis) — contrairement aux cartes KPI en haut, qui elles
    // respectent ce filtre. Sans ce rappel, "0" en haut et un stock non-nul
    // plus bas pour un AUTRE magasin semble contradictoire alors que les
    // deux chiffres sont corrects, juste sur des périmètres différents.
    if (activeShop) {
        var scopeRow = document.createElement('tr');
        var scopeTd = document.createElement('td');
        scopeTd.colSpan = 3;
        scopeTd.innerHTML = '<span style="font-size:0.78rem;color:#3730A3;background:#EEF2FF;border:1px solid #E0E7FF;border-radius:6px;padding:4px 10px;display:block;margin-bottom:4px;">'
            + 'ℹ️ Ce tableau affiche tout le réseau, indépendamment du magasin filtré ci-dessus (⭐ = magasin filtré) — les cartes en haut de fiche, elles, ne montrent que ce magasin.'
            + '</span>';
        scopeRow.appendChild(scopeTd);
        tbody.appendChild(scopeRow);
    }

    var hasNegative = stores.some(function(s) { return (s.stock || 0) < 0; });
    if (hasNegative) {
        var noteRow = document.createElement('tr');
        var noteTd = document.createElement('td');
        noteTd.colSpan = 3;
        noteTd.innerHTML = '<span style="font-size:0.78rem;color:#92400E;background:#FFFBEB;border:1px solid #FEF3C7;border-radius:6px;padding:4px 10px;display:block;margin-bottom:4px;">'
            + '⚠️ Stock négatif = plus de sorties enregistrées que d\'entrées dans Odoo (ajustements, retours ou imports manquants)'
            + '</span>';
        noteRow.appendChild(noteTd);
        tbody.appendChild(noteRow);
    }

    stores.forEach(function(s) {
        var tr = document.createElement('tr');
        if (activeShop && s.field === activeShop) {
            tr.style.background = 'rgba(124,58,237,0.08)';
            tr.style.fontWeight = '600';
        }

        var tdName = document.createElement('td');
        tdName.textContent = s.name || s.field || '—';
        if (activeShop && s.field === activeShop) tdName.innerHTML += ' ⭐';
        tr.appendChild(tdName);

        var tdQty = document.createElement('td');
        if (s.qty === null || s.qty === undefined) {
            tdQty.textContent = '—';
            tdQty.style.color = '#94A3B8';
            tdQty.title = 'Aucune commande fournisseur confirmée pour ce magasin';
        } else {
            var whSourceLabel = s.dispatch_source === 'achats' ? ' achats' : '';
            tdQty.innerHTML = formatNumber(s.qty)
                + (whSourceLabel ? ' <small style="color:#94A3B8">(' + whSourceLabel.trim() + ')</small>' : '');
        }
        tr.appendChild(tdQty);

        var tdStock = document.createElement('td');
        tdStock.textContent = formatNumber(s.stock);
        if ((s.stock || 0) < 0) {
            tdStock.style.color = '#EF4444';
            tdStock.title = 'Stock négatif dans Odoo : sorties > entrées. Vérifier les mouvements de stock.';
        } else if (!s.stock) {
            tdStock.style.color = '#F59E0B';
        } else {
            tdStock.style.color = '#10B981';
        }
        tdStock.style.fontWeight = '600';
        tr.appendChild(tdStock);

        tbody.appendChild(tr);
    });
}

function _renderVariants(tbodyId, variants, activeShop, hasBasePivotData) {
    var tbody = el(tbodyId);
    if (!tbody) return;
    tbody.innerHTML = '';

    if (hasBasePivotData === false && variants && variants.length > 0) {
        var noteRow = document.createElement('tr');
        var noteTd = document.createElement('td');
        noteTd.colSpan = 5;
        noteTd.style.fontSize = '0.8em';
        noteTd.style.color = '#B45309';
        noteTd.style.background = '#FFFBEB';
        noteTd.style.padding = '6px 8px';
        noteTd.textContent = 'ℹ️ Ce produit n\'a aucune commande fournisseur confirmée enregistrée — "Total pièces" et "Reste" ne sont pas disponibles pour lui.';
        noteRow.appendChild(noteTd);
        tbody.appendChild(noteRow);
    }

    if (!variants || variants.length === 0) {
        var tr = document.createElement('tr');
        var td = document.createElement('td');
        td.colSpan = 5;
        td.textContent = 'Aucune variante';
        td.style.textAlign = 'center';
        td.style.color = '#999';
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }

    variants.forEach(function(v, idx) {
        var tr = document.createElement('tr');
        if (idx === 0) {
            tr.style.background = 'rgba(124,58,237,0.08)';
        }
        if (v.color && v.color !== '—') {
            tr.style.cursor = 'pointer';
            tr.title = 'Cliquer pour voir le stock de cette couleur dans chaque magasin';
            tr.onclick = function() {
                var nameEl = el('detail-name');
                openColorDetail(state.detail.article_id, nameEl ? nameEl.textContent : '', v.color);
            };
        }

        var tdRank = document.createElement('td');
        tdRank.textContent = idx === 0 ? '🏆' : (idx + 1);
        tdRank.style.textAlign = 'center';
        tr.appendChild(tdRank);

        var tdName = document.createElement('td');
        tdName.textContent = v.name || '—';
        tdName.style.fontWeight = idx === 0 ? '700' : 'normal';
        tr.appendChild(tdName);

        var tdTotal = document.createElement('td');
        if (v.total_pieces === null || v.total_pieces === undefined) {
            tdTotal.textContent = '—';
            tdTotal.title = 'Aucune commande fournisseur confirmée pour cette couleur';
            tdTotal.style.color = '#94A3B8';
        } else {
            var sourceLabel = v.dispatch_source === 'achats' ? ' achats' : '';
            tdTotal.innerHTML = formatNumber(v.total_pieces)
                + (sourceLabel ? ' <small style="color:#94A3B8">(' + sourceLabel.trim() + ')</small>' : '');
            if (v.total_pieces === 0) {
                tdTotal.title = 'Aucun dispatch/achat enregistré, aucune vente et aucun stock';
                tdTotal.style.color = '#94A3B8';
            }
            if (v.dispatch_missing) {
                tdTotal.innerHTML += ' <span title="Aucune commande fournisseur confirmée pour cette couleur, alors qu\'il y a du stock et/ou des ventes" style="color:#F59E0B;cursor:help">⚠️</span>';
            }
        }
        tr.appendChild(tdTotal);

        var tdQty = document.createElement('td');
        // "Qté (magasin)" = stock ACTUEL de cette variante dans le magasin
        // filtré (v.stock_shop), pas les ventes — v.shops reste dédié au
        // bloc "répartition des ventes par magasin" plus bas.
        if (activeShop && v.stock_shop !== null && v.stock_shop !== undefined) {
            tdQty.innerHTML = formatNumber(v.qty)
                + ' <small style="color:#7C3AED">(stock magasin: ' + formatNumber(v.stock_shop) + ')</small>';
        } else {
            tdQty.textContent = formatNumber(v.qty);
        }
        tr.appendChild(tdQty);

        var tdReste = document.createElement('td');
        // Reste = total pièces (dispatché, jamais recalculé) - vendu.
        var resteVal = v.reste;
        if (resteVal === null || resteVal === undefined) {
            tdReste.textContent = '—';
            tdReste.style.color = '#94A3B8';
        } else {
            tdReste.textContent = formatNumber(resteVal);
            if (resteVal < 0) {
                tdReste.style.color = '#EF4444'; // Rouge si survendu par rapport au dispatch
                tdReste.title = 'Plus vendu que dispatché — vérifier les commandes fournisseur';
            } else if (resteVal === 0) {
                tdReste.style.color = '#10B981';
            } else {
                tdReste.style.color = '#F59E0B';
            }
        }
        if (v.discordance) {
            tdReste.innerHTML += ' <span title="' + (v.discordance_detail || 'Écart entre dispatché et stock+vendu') + '" style="color:#EF4444;cursor:help">⚠️</span>';
        }
        tr.appendChild(tdReste);

        tbody.appendChild(tr);
    });

    if (activeShop && variants.length > 0 && variants[0].shops) {
        var best = variants[0];
        var allShops = Object.keys(best.shops);
        if (allShops.length > 1) {
            var tr = document.createElement('tr');
            var td = document.createElement('td');
            td.colSpan = 5;
            td.style.paddingTop = '8px';
            td.style.fontSize = '0.85em';
            td.style.color = '#666';
            td.innerHTML = '<strong>Meilleure variante (' + best.name + ') — répartition des ventes par magasin :</strong> '
                + allShops.map(function(s) {
                    return '<span style="margin:0 4px;padding:2px 6px;background:#f3f4f6;border-radius:4px">'
                        + s + ': ' + formatNumber(best.shops[s]) + '</span>';
                }).join('');
            tr.appendChild(td);
            tbody.appendChild(tr);
        }
    }
}

function _renderVerification(verification) {
    var colorTbody = el('detail-verif-color-tbody');
    var magasinTbody = el('detail-verif-magasin-tbody');
    if (!colorTbody || !magasinTbody) return;

    function fillRows(tbody, rows, firstColKey) {
        tbody.innerHTML = '';
        if (!rows || rows.length === 0) {
            var tr = document.createElement('tr');
            var td = document.createElement('td');
            td.colSpan = 3;
            td.textContent = 'Aucune donnée achats pour ce produit.';
            td.style.textAlign = 'center';
            td.style.color = '#94A3B8';
            tr.appendChild(td);
            tbody.appendChild(tr);
            return;
        }
        rows.forEach(function(r) {
            var tr = document.createElement('tr');

            var tdName = document.createElement('td');
            tdName.textContent = r[firstColKey] || '—';
            tr.appendChild(tdName);

            var tdAchats = document.createElement('td');
            tdAchats.textContent = formatNumber(r.achats);
            tr.appendChild(tdAchats);

            var tdDash = document.createElement('td');
            tdDash.textContent = (r.dashboard === null || r.dashboard === undefined) ? '—' : formatNumber(r.dashboard);
            tdDash.style.fontWeight = '700';
            tr.appendChild(tdDash);

            tbody.appendChild(tr);
        });
    }

    fillRows(colorTbody, verification && verification.by_color, 'color');
    fillRows(magasinTbody, verification && verification.by_magasin, 'magasin');
}

function closeDetail() {
    var overlay = el('detail-overlay');
    if (overlay) overlay.classList.remove('active');
    state.detail.article_id = null;
}

// ═══════════════════════════════════════════════════════════
// EXTRACTION / TRANSFERT INTER-MAGASINS
// ═══════════════════════════════════════════════════════════
function openTransferPanel(articleId, productName, presetColor) {
    var overlay = el('transfer-overlay');
    if (!overlay || !articleId) return;

    state.transfer.article_id = articleId;
    state.transfer.article_name = productName || '';
    state.transfer.color = presetColor || null;
    state.transfer.group_ref = null;
    state.transfer.group_count = 0;

    var nameEl = el('transfer-product-name');
    if (nameEl) nameEl.textContent = productName || '—';

    var destSel = el('transfer-dest-shop');
    if (destSel) destSel.value = state.shop_field || '';

    // Liste des couleurs de ce produit — dérivée de state.detail.variants,
    // déjà chargé pour le tableau "Variantes Couleurs" (pas de nouvel appel
    // serveur nécessaire pour peupler ce filtre).
    var colorSel = el('transfer-color-filter');
    if (colorSel) {
        while (colorSel.options.length > 1) colorSel.remove(1);
        var seenColors = {};
        (state.detail.variants || []).forEach(function(v) {
            var c = v.color;
            if (c && c !== '—' && !seenColors[c]) {
                seenColors[c] = true;
                var opt = document.createElement('option');
                opt.value = c;
                opt.textContent = c;
                colorSel.appendChild(opt);
            }
        });
        colorSel.value = presetColor || '';
    }

    var msgEl = el('transfer-suggestions-msg');
    if (msgEl) msgEl.textContent = '';

    var resultEl = el('transfer-result');
    if (resultEl) { resultEl.style.display = 'none'; resultEl.innerHTML = ''; }

    var tbody = el('transfer-suggestions-tbody');
    if (tbody) tbody.innerHTML = '';

    _showTransferSuggestionsView();

    overlay.classList.add('active');

    if (destSel && destSel.value) {
        _loadTransferSuggestions();
    }
}

function closeTransferPanel() {
    var overlay = el('transfer-overlay');
    if (overlay) overlay.classList.remove('active');
    state.transfer.article_id = null;
    state.transfer.group_ref = null;
    state.transfer.group_count = 0;
}

function _showTransferSuggestionsView() {
    var suggSection = el('transfer-suggestions-section');
    var matrixSection = el('transfer-matrix-section');
    if (suggSection) suggSection.style.display = '';
    if (matrixSection) matrixSection.style.display = 'none';
}

function _showTransferMatrixView() {
    var suggSection = el('transfer-suggestions-section');
    var matrixSection = el('transfer-matrix-section');
    if (suggSection) suggSection.style.display = 'none';
    if (matrixSection) matrixSection.style.display = '';
}

async function _loadTransferSuggestions() {
    var destSel = el('transfer-dest-shop');
    var destShopField = destSel ? destSel.value : '';
    var tbody = el('transfer-suggestions-tbody');
    var msgEl = el('transfer-suggestions-msg');

    if (!destShopField) {
        if (tbody) tbody.innerHTML = '';
        if (msgEl) msgEl.textContent = 'Choisissez un magasin cible pour voir les suggestions.';
        return;
    }
    if (!state.transfer.article_id) return;

    if (msgEl) msgEl.textContent = 'Recherche des magasins source…';
    if (tbody) tbody.innerHTML = '';

    var colorSel = el('transfer-color-filter');
    var colorFilter = colorSel ? colorSel.value : '';
    state.transfer.color = colorFilter || null;

    var data = await rpc('/mavie/api/transfer-suggestions', {
        product_tmpl_id: state.transfer.article_id,
        dest_shop_field: destShopField,
        color: colorFilter || null,
    });

    if (!data || data.error) {
        if (msgEl) msgEl.textContent = 'Erreur : ' + (data && data.error || 'inconnue');
        return;
    }

    _renderTransferSuggestions(data.suggestions || [], destShopField);

    if (msgEl) {
        var suggestions = data.suggestions || [];
        var forWhat = colorFilter ? ('pour la couleur ' + colorFilter) : 'pour ce produit';
        if (suggestions.length === 0) {
            msgEl.textContent = 'Aucun stock disponible ' + forWhat + ' dans les autres magasins.';
        } else if (data.dest_city && !suggestions.some(function(s) { return s.tier === 'same_city'; })) {
            msgEl.textContent = 'Aucun magasin de ' + data.dest_city + ' (même ville) n\'a de stock disponible ' + forWhat + ' — voici les autres magasins qui en ont.';
        } else {
            msgEl.textContent = '';
        }
    }
}

function _renderTransferSuggestions(suggestions, destShopField) {
    var tbody = el('transfer-suggestions-tbody');
    if (!tbody) return;
    tbody.innerHTML = '';

    var tierLabels = { same_city: 'Même ville', nearby: 'Environs', other: 'Autre' };

    suggestions.forEach(function(s) {
        var tr = document.createElement('tr');
        tr.style.cursor = 'pointer';
        tr.onclick = function() { openTransferMatrix(s.shop_field, s.shop_label, destShopField); };

        var tdName = document.createElement('td');
        tdName.textContent = s.shop_label;
        tr.appendChild(tdName);

        var tdCity = document.createElement('td');
        tdCity.textContent = s.city || '—';
        tr.appendChild(tdCity);

        var tdTier = document.createElement('td');
        var badge = document.createElement('span');
        badge.className = 'transfer-tier-badge transfer-tier-' + s.tier;
        badge.textContent = tierLabels[s.tier] || s.tier;
        tdTier.appendChild(badge);
        tr.appendChild(tdTier);

        var tdStock = document.createElement('td');
        tdStock.textContent = formatNumber(s.available_qty);
        tr.appendChild(tdStock);

        var tdAction = document.createElement('td');
        var chooseBtn = document.createElement('button');
        chooseBtn.className = 'btn-transfer-create';
        chooseBtn.textContent = 'Choisir →';
        chooseBtn.onclick = function(e) {
            e.stopPropagation();
            openTransferMatrix(s.shop_field, s.shop_label, destShopField);
        };
        tdAction.appendChild(chooseBtn);
        tr.appendChild(tdAction);

        tbody.appendChild(tr);
    });
}

async function openTransferMatrix(sourceShopField, sourceLabel, destShopField) {
    state.transfer.source_shop_field = sourceShopField;
    state.transfer.dest_shop_field = destShopField;

    var nameEl = el('transfer-matrix-source-name');
    if (nameEl) nameEl.textContent = sourceLabel || sourceShopField;

    var emetteurRecepteurEl = el('transfer-emetteur-recepteur');
    if (emetteurRecepteurEl) {
        var destSelEl = el('transfer-dest-shop');
        var destLabel = (destSelEl && destSelEl.selectedOptions && destSelEl.selectedOptions[0])
            ? destSelEl.selectedOptions[0].textContent
            : destShopField;
        emetteurRecepteurEl.innerHTML = '📤 <strong>Émetteur :</strong> ' + (sourceLabel || sourceShopField)
            + '&nbsp;&nbsp;→&nbsp;&nbsp;📥 <strong>Récepteur :</strong> ' + destLabel;
    }

    var msgEl = el('transfer-matrix-msg');
    if (msgEl) msgEl.textContent = 'Chargement du stock par couleur/taille…';

    var tbody = el('transfer-matrix-tbody');
    if (tbody) tbody.innerHTML = '';

    var resultEl = el('transfer-result');
    if (resultEl) { resultEl.style.display = 'none'; resultEl.innerHTML = ''; }

    _showTransferMatrixView();

    var data = await rpc('/mavie/api/transfer-variant-stock', {
        product_tmpl_id: state.transfer.article_id,
        source_shop_field: sourceShopField,
        color: state.transfer.color || null,
    });

    if (!data || data.error) {
        if (msgEl) msgEl.textContent = 'Erreur : ' + (data && data.error || 'inconnue');
        return;
    }

    if (msgEl) msgEl.textContent = '';
    _renderTransferMatrix(data.variants || []);
}

function _renderTransferMatrix(variants) {
    var tbody = el('transfer-matrix-tbody');
    if (!tbody) return;
    tbody.innerHTML = '';

    if (variants.length === 0) {
        var tr = document.createElement('tr');
        var td = document.createElement('td');
        td.colSpan = 4;
        td.textContent = 'Aucun stock disponible dans ce magasin pour ce produit.';
        td.style.textAlign = 'center';
        td.style.color = '#999';
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }

    variants.forEach(function(v) {
        var tr = document.createElement('tr');

        var tdColor = document.createElement('td');
        tdColor.textContent = v.color;
        tr.appendChild(tdColor);

        var tdSize = document.createElement('td');
        tdSize.textContent = v.size;
        tr.appendChild(tdSize);

        var tdStock = document.createElement('td');
        tdStock.textContent = formatNumber(v.available_qty);
        tr.appendChild(tdStock);

        var tdQty = document.createElement('td');
        var qtyInput = document.createElement('input');
        qtyInput.type = 'number';
        qtyInput.className = 'transfer-qty-input';
        qtyInput.min = '0';
        qtyInput.max = String(v.available_qty);
        qtyInput.value = '0';
        qtyInput.dataset.productId = v.product_id;
        tdQty.appendChild(qtyInput);
        tr.appendChild(tdQty);

        tbody.appendChild(tr);
    });
}

async function _createTransferFromMatrix(btnEl) {
    var tbody = el('transfer-matrix-tbody');
    if (!tbody) return;

    var lines = [];
    tbody.querySelectorAll('input.transfer-qty-input').forEach(function(input) {
        var qty = parseFloat(input.value);
        if (qty > 0) {
            lines.push({ product_id: parseInt(input.dataset.productId, 10), qty: qty });
        }
    });

    if (lines.length === 0) {
        var msgEl = el('transfer-matrix-msg');
        if (msgEl) msgEl.textContent = 'Indique au moins une quantité à transférer.';
        return;
    }

    if (btnEl) { btnEl.disabled = true; btnEl.textContent = 'Création…'; }

    // Un seul bon par référence + destination : si un autre bon a déjà été
    // créé dans cette même session (magasin source différent), on réutilise
    // son group_ref pour qu'ils soient regroupés à l'affichage (liste + PDF)
    // au lieu d'apparaître comme des transferts sans rapport.
    if (!state.transfer.group_ref) {
        state.transfer.group_ref = state.transfer.article_id + '-' + state.transfer.dest_shop_field + '-' + Date.now();
    }

    var data = await rpc('/mavie/api/transfer-create', {
        product_tmpl_id: state.transfer.article_id,
        source_shop_field: state.transfer.source_shop_field,
        dest_shop_field: state.transfer.dest_shop_field,
        lines: lines,
        group_ref: state.transfer.group_ref,
    });

    if (btnEl) { btnEl.disabled = false; btnEl.textContent = 'Créer le transfert'; }

    var resultEl = el('transfer-result');
    if (!resultEl) return;

    if (!data || data.error) {
        resultEl.style.display = 'block';
        resultEl.style.background = '#FEF2F2';
        resultEl.style.borderColor = '#FCA5A5';
        resultEl.innerHTML = '<strong>Erreur :</strong> ' + (data && data.error || 'inconnue');
        return;
    }

    state.transfer.group_count = (state.transfer.group_count || 0) + 1;

    var pdfUrl = '/report/pdf/mavie_dashboard.report_transfer_template/' + data.transfer_id;
    var html = '<strong>✅ Transfert ' + data.transfer_name + ' créé</strong> — envoyé dans le module Transferts, en attente de validation par le Responsable Approvisionnement.';
    if (state.transfer.group_count > 1) {
        html += ' (regroupé avec ' + (state.transfer.group_count - 1) + ' autre(s) bon(s) créé(s) pour cette même référence + destination — un seul PDF imprimera tout le groupe)';
    }
    html += ' <a href="' + pdfUrl + '" target="_blank">📄 Imprimer le bon (PDF)</a>';
    if (data.warning) html += '<br/><span style="color:#B45309;">' + data.warning + '</span>';
    if (data.notif_warning) html += '<br/><span style="color:#B45309;">' + data.notif_warning + '</span>';
    if (!data.notif_warning) html += '<br/><span style="color:#15803D;">📩 Le responsable du magasin source a été notifié.</span>';

    resultEl.style.display = 'block';
    resultEl.style.background = '#F0FDF4';
    resultEl.style.borderColor = '#86EFAC';
    resultEl.innerHTML = html;

    // Retour à la liste des suggestions (le stock source a changé)
    _showTransferSuggestionsView();
    _loadTransferSuggestions();
}

// ═══════════════════════════════════════════════════════════
// DÉTAIL D'UNE COULEUR — stock par magasin (popup depuis Variantes Couleurs)
// ═══════════════════════════════════════════════════════════
function openColorDetail(articleId, productName, color) {
    var overlay = el('color-detail-overlay');
    if (!overlay || !articleId || !color) return;

    state.colorDetail.article_id = articleId;
    state.colorDetail.product_name = productName || '';
    state.colorDetail.color = color;

    var colorNameEl = el('color-detail-color-name');
    if (colorNameEl) colorNameEl.textContent = color;
    var productNameEl = el('color-detail-product-name');
    if (productNameEl) productNameEl.textContent = productName || '—';

    // KPIs calculés côté client à partir de state.detail.variants, déjà
    // chargé pour le tableau "Variantes Couleurs" — pas de second calcul
    // serveur pour ces totaux, seule la répartition par magasin (ci-dessous)
    // vient d'un nouvel appel.
    var matching = (state.detail.variants || []).filter(function(v) { return v.color === color; });
    var qtySold = 0, ca = 0, totalPieces = 0, hasTotalPieces = false, stockTotal = 0;
    matching.forEach(function(v) {
        qtySold += v.qty || 0;
        ca += v.ca || 0;
        stockTotal += v.stock || 0;
        if (v.total_pieces !== null && v.total_pieces !== undefined) {
            totalPieces += v.total_pieces;
            hasTotalPieces = true;
        }
    });
    var reste = hasTotalPieces ? (totalPieces - qtySold) : null;

    var qtySoldEl = el('color-detail-qty-sold');
    if (qtySoldEl) qtySoldEl.textContent = formatNumber(qtySold);
    var caEl = el('color-detail-ca');
    if (caEl) caEl.textContent = formatMAD(ca);
    var totalPiecesEl = el('color-detail-total-pieces');
    if (totalPiecesEl) totalPiecesEl.textContent = hasTotalPieces ? formatNumber(totalPieces) : '—';
    // Reste (achats - vendu, "papier") et Stock total (compte physique réel
    // stock.quant) mesurent deux choses différentes et ne sont PAS censés
    // être égaux — un écart signale un mouvement de stock hors achats/ventes
    // suivis (stock initial, transfert, ajustement), pas une erreur de calcul.
    var resteEl = el('color-detail-reste');
    if (resteEl) {
        resteEl.textContent = (reste === null) ? '—' : formatNumber(reste);
        resteEl.title = (reste !== null)
            ? 'Reste = Total pièces (commandes fournisseur) − Qté vendue (POS). Estimation "papier", à comparer au Stock total physique ci-contre — un écart entre les deux est normal s\'il y a eu un mouvement hors achats/ventes suivis.'
            : '';
    }
    var stockTotalEl = el('color-detail-stock-total');
    if (stockTotalEl) {
        stockTotalEl.textContent = formatNumber(stockTotal);
        stockTotalEl.style.color = stockTotal < 0 ? '#EF4444' : '';
        stockTotalEl.title = stockTotal < 0
            ? 'Stock négatif : selon Odoo, plus de pièces sont sorties (ventes/transferts) de cet emplacement qu\'il n\'en a jamais été reçu — écart d\'inventaire réel à vérifier physiquement en magasin.'
            : 'Compte physique réel (stock.quant), tous magasins mappés confondus.';
    }

    // Signale le même écart Total pièces/Stock que le tableau Variantes
    // Couleurs (v.discordance, déjà calculé côté serveur) — ici agrégé sur
    // toutes les tailles de cette couleur. Cause la plus fréquente : du
    // stock entré par ajustement d'inventaire manuel plutôt que par une
    // commande fournisseur, donc invisible pour "Total pièces" par
    // construction (ce champ ne compte QUE les achats).
    var kpiGrid = document.querySelector('#color-detail-overlay .detail-kpi-grid');
    var discordanceNote = el('color-detail-discordance-note');
    var anyDiscordance = matching.some(function(v) { return v.discordance; });
    if (anyDiscordance) {
        if (!discordanceNote && kpiGrid) {
            discordanceNote = document.createElement('div');
            discordanceNote.id = 'color-detail-discordance-note';
            discordanceNote.style.margin = '10px 0';
            discordanceNote.style.padding = '8px 12px';
            discordanceNote.style.fontSize = '0.85rem';
            discordanceNote.style.color = '#B45309';
            discordanceNote.style.background = '#FFFBEB';
            discordanceNote.style.borderRadius = '6px';
            kpiGrid.insertAdjacentElement('afterend', discordanceNote);
        }
        if (discordanceNote) {
            discordanceNote.textContent = '⚠️ Écart entre Total pièces (' + formatNumber(totalPieces) + ') et Stock total (' +
                formatNumber(stockTotal) + ') : une partie du stock de cette couleur n\'est pas passée par une commande ' +
                'fournisseur suivie (ajustement d\'inventaire, stock initial…) — Total pièces ne peut pas la voir par construction.';
            discordanceNote.style.display = '';
        }
    } else if (discordanceNote) {
        discordanceNote.style.display = 'none';
    }

    var msgEl = el('color-detail-msg');
    if (msgEl) msgEl.textContent = 'Chargement du stock par magasin…';
    var tbody = el('color-detail-stores-tbody');
    if (tbody) tbody.innerHTML = '';

    overlay.classList.add('active');

    _loadColorDetailStores(articleId, color);
}

function closeColorDetail() {
    var overlay = el('color-detail-overlay');
    if (overlay) overlay.classList.remove('active');
}

async function _loadColorDetailStores(articleId, color) {
    var msgEl = el('color-detail-msg');

    var data = await rpc('/mavie/api/color-stock-by-store', {
        product_tmpl_id: articleId,
        color: color,
    });

    if (!data || data.error) {
        if (msgEl) msgEl.textContent = 'Erreur : ' + (data && data.error || 'inconnue');
        return;
    }

    if (msgEl) msgEl.textContent = '';
    _renderColorDetailStores(data.stores || []);
}

function _renderColorDetailStores(stores) {
    var tbody = el('color-detail-stores-tbody');
    if (!tbody) return;
    tbody.innerHTML = '';

    if (!stores || stores.length === 0) {
        var tr = document.createElement('tr');
        var td = document.createElement('td');
        td.colSpan = 4;
        td.textContent = 'Aucun stock trouvé pour cette couleur dans les magasins actifs.';
        td.style.textAlign = 'center';
        td.style.color = '#999';
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }

    stores.forEach(function(s) {
        var tr = document.createElement('tr');

        var tdName = document.createElement('td');
        tdName.textContent = s.shop_label;
        tr.appendChild(tdName);

        var tdCity = document.createElement('td');
        tdCity.textContent = s.city || '—';
        tr.appendChild(tdCity);

        var tdStock = document.createElement('td');
        tdStock.textContent = formatNumber(s.stock_total);
        if (s.stock_total <= 0) tdStock.style.color = '#94A3B8';
        tr.appendChild(tdStock);

        var tdSizes = document.createElement('td');
        var sizeKeys = Object.keys(s.by_size || {}).sort();
        if (sizeKeys.length === 0) {
            tdSizes.textContent = '—';
        } else {
            tdSizes.innerHTML = sizeKeys.map(function(sz) {
                return '<span style="margin:0 4px 4px 0;padding:2px 6px;background:#f3f4f6;border-radius:4px;display:inline-block;font-size:0.85em">'
                    + sz + ': ' + formatNumber(s.by_size[sz]) + '</span>';
            }).join('');
        }
        tr.appendChild(tdSizes);

        tbody.appendChild(tr);
    });
}

// ═══════════════════════════════════════════════════════════
// RECHERCHE PRODUIT
// ═══════════════════════════════════════════════════════════
function _renderSearchResults(results) {
    var container = el('product-search-results');
    if (!container) return;
    container.innerHTML = '';

    if (!results || results.length === 0) {
        var empty = document.createElement('div');
        empty.className = 'search-result-empty';
        empty.textContent = 'Aucun produit trouvé';
        container.appendChild(empty);
        container.classList.add('active');
        return;
    }

    results.forEach(function(p) {
        var item = document.createElement('div');
        item.className = 'search-result-item';
        item.innerHTML = '<span>' + (p.name || '—') + '</span><span class="sr-ref">' + (p.ref || '—') + '</span>';
        item.onclick = function() {
            container.classList.remove('active');
            var input = el('product-search-input');
            if (input) input.value = '';
            openDetail(p.id, p.name);
        };
        container.appendChild(item);
    });

    container.classList.add('active');
}

async function _doProductSearch(query) {
    var data = await rpc('/mavie/api/search-products', { query: query });
    if (!data || data.error) return;
    _renderSearchResults(data.results);
}

// ═══════════════════════════════════════════════════════════
// RUPTURES DE STOCK
// ═══════════════════════════════════════════════════════════
function _renderRupturesList(searchFilter) {
    var tbody = el('ruptures-tbody');
    if (!tbody) return;
    tbody.innerHTML = '';

    var badge = el('ruptures-count-badge');
    if (badge) {
        // lastRupturesList est plafonnée à 500 côté serveur (perf) —
        // lastRupturesCount est le vrai total, jamais tronqué.
        badge.textContent = formatNumber(lastRupturesCount) + ' articles'
            + (lastRupturesCount > lastRupturesList.length ? ' (' + formatNumber(lastRupturesList.length) + ' affichés)' : '');
    }

    var list = lastRupturesList;
    if (searchFilter) {
        var q = searchFilter.toLowerCase().trim();
        list = list.filter(function(p) {
            return (p.name && p.name.toLowerCase().includes(q)) ||
                   (p.ref && p.ref.toLowerCase().includes(q));
        });
    }

    if (!list || list.length === 0) {
        var tr = document.createElement('tr');
        var td = document.createElement('td');
        td.colSpan = 5;
        td.textContent = searchFilter ? 'Aucun produit ne correspond à votre recherche.' : 'Aucun produit en rupture';
        td.style.textAlign = 'center';
        td.style.padding = '24px';
        td.style.color = '#94A3B8';
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }

    list.forEach(function(p) {
        var tr = document.createElement('tr');
        tr.style.cursor = 'pointer';
        tr.style.borderBottom = '1px solid #F1F5F9';
        tr.onclick = function() {
            closeRuptures();
            openDetail(p.id, p.name);
        };

        var tdRef = document.createElement('td');
        tdRef.textContent = p.ref || '—';
        tdRef.style.padding = '10px';
        tdRef.style.color = '#64748B';
        tdRef.style.fontWeight = '600';
        tdRef.style.fontSize = '0.85rem';
        tr.appendChild(tdRef);

        var tdName = document.createElement('td');
        tdName.textContent = p.name || '—';
        tdName.style.padding = '10px';
        tdName.style.fontWeight = '600';
        tdName.style.color = '#0F172A';
        tdName.style.fontSize = '0.85rem';
        tr.appendChild(tdName);

        var tdQty = document.createElement('td');
        tdQty.textContent = formatNumber(p.qty_sold || 0);
        tdQty.style.padding = '10px';
        tdQty.style.textAlign = 'center';
        tdQty.style.color = '#334155';
        tdQty.style.fontWeight = '500';
        tdQty.style.fontSize = '0.85rem';
        tr.appendChild(tdQty);

        var tdCa = document.createElement('td');
        tdCa.textContent = formatMAD(p.ca || 0);
        tdCa.style.padding = '10px';
        tdCa.style.textAlign = 'right';
        tdCa.style.color = '#059669';
        tdCa.style.fontWeight = '600';
        tdCa.style.fontSize = '0.85rem';
        tr.appendChild(tdCa);

        var tdStock = document.createElement('td');
        tdStock.textContent = formatNumber(p.stock || 0);
        tdStock.style.padding = '10px';
        tdStock.style.textAlign = 'center';
        tdStock.style.color = '#DC2626';
        tdStock.style.fontWeight = '700';
        tdStock.style.fontSize = '0.85rem';
        tr.appendChild(tdStock);

        tbody.appendChild(tr);
    });
}

function openRuptures() {
    _renderRupturesList();
    var overlay = el('ruptures-overlay');
    if (overlay) overlay.classList.add('active');
}

function closeRuptures() {
    var overlay = el('ruptures-overlay');
    if (overlay) overlay.classList.remove('active');
}

// ═══════════════════════════════════════════════════════════
// STOCK DORMANT
// ═══════════════════════════════════════════════════════════
function _renderDormantList(searchFilter) {
    var tbody = el('dormant-tbody');
    if (!tbody) return;
    tbody.innerHTML = '';

    var badge = el('dormant-count-badge');
    if (badge) {
        badge.textContent = formatNumber(lastDormantCount) + ' articles'
            + (lastDormantCount > lastDormantList.length ? ' (' + formatNumber(lastDormantList.length) + ' affichés)' : '');
    }

    var list = lastDormantList;
    if (searchFilter) {
        var q = searchFilter.toLowerCase().trim();
        list = list.filter(function(p) {
            return (p.name && p.name.toLowerCase().includes(q)) ||
                   (p.ref && p.ref.toLowerCase().includes(q));
        });
    }

    if (!list || list.length === 0) {
        var tr = document.createElement('tr');
        var td = document.createElement('td');
        td.colSpan = 4;
        td.textContent = searchFilter ? 'Aucun produit ne correspond à votre recherche.' : 'Aucun stock dormant';
        td.style.textAlign = 'center';
        td.style.padding = '24px';
        td.style.color = '#94A3B8';
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }

    list.forEach(function(p) {
        var tr = document.createElement('tr');
        tr.style.cursor = 'pointer';
        tr.style.borderBottom = '1px solid #F1F5F9';
        tr.onclick = function() {
            closeDormant();
            openDetail(p.id, p.name);
        };

        var tdRef = document.createElement('td');
        tdRef.textContent = p.ref || '—';
        tdRef.style.padding = '10px';
        tdRef.style.color = '#64748B';
        tdRef.style.fontWeight = '600';
        tdRef.style.fontSize = '0.85rem';
        tr.appendChild(tdRef);

        var tdName = document.createElement('td');
        tdName.textContent = p.name || '—';
        tdName.style.padding = '10px';
        tdName.style.fontWeight = '600';
        tdName.style.color = '#0F172A';
        tdName.style.fontSize = '0.85rem';
        tr.appendChild(tdName);

        var tdMagasin = document.createElement('td');
        tdMagasin.textContent = p.magasin || '—';
        tdMagasin.style.padding = '10px';
        tdMagasin.style.color = '#334155';
        tdMagasin.style.fontSize = '0.85rem';
        tr.appendChild(tdMagasin);

        var tdStock = document.createElement('td');
        tdStock.textContent = formatNumber(p.stock || 0);
        tdStock.style.padding = '10px';
        tdStock.style.textAlign = 'center';
        tdStock.style.color = '#B45309';
        tdStock.style.fontWeight = '700';
        tdStock.style.fontSize = '0.85rem';
        tr.appendChild(tdStock);

        tbody.appendChild(tr);
    });
}

function openDormant() {
    _renderDormantList();
    var overlay = el('dormant-overlay');
    if (overlay) overlay.classList.add('active');
}

function closeDormant() {
    var overlay = el('dormant-overlay');
    if (overlay) overlay.classList.remove('active');
}

// ═══════════════════════════════════════════════════════════
// DASHBOARD STOCK & RUPTURE (nouvelle vue)
// ═══════════════════════════════════════════════════════════

function _renderStockDashboard(data) {
    var kpiMap = {
        'kpi-stock-taux-rupture': formatPctTight(data.taux_rupture),
        'kpi-stock-skus-rupture': formatNumber(data.ruptures_count),
        'kpi-stock-couverture':   formatNumber(data.couverture_moy) + ' j',
        'kpi-stock-dormant':      formatPctTight(data.stock_dormant_pct),
        'kpi-stock-precision':    formatPctTight(data.precision_inventaire),
    };
    for (var id in kpiMap) {
        var e = el(id);
        if (e) e.textContent = kpiMap[id];
    }

    var subEl = el('kpi-stock-skus-rupture-sub');
    if (subEl) subEl.textContent = '/ ' + formatNumber(data.total_active_skus) + ' actifs';

    lastRupturesList = data.ruptures_list || [];
    lastDormantList = data.dormant_list || [];
    lastRupturesCount = data.ruptures_count || 0;
    lastDormantCount = data.dormant_count || 0;

    _renderStockAlerts(data.alertes_stock);
    _renderRotationCollection(data.rotation_collection);
    _renderGmroiCategorie(data.gmroi_categorie);
    state.proches_rupture_30j_cache = data.proches_rupture_30j || [];
    _renderStock30j(state.proches_rupture_30j_cache);
    _renderStockValorisation(data);
}

function _renderStock30j(fullList) {
    var tbody = el('stock-30j-tbody');
    if (!tbody) return;
    tbody.innerHTML = '';

    var limitEl = el('stock-30j-limit');
    var limit = (limitEl && parseInt(limitEl.value, 10) > 0) ? parseInt(limitEl.value, 10) : 10;
    var list = (fullList || []).slice(0, limit);

    if (list.length === 0) {
        var tr = document.createElement('tr');
        var td = document.createElement('td');
        td.colSpan = 6;
        td.textContent = 'Aucune rupture prévue sous 30 jours';
        td.style.textAlign = 'center';
        td.style.color = '#999';
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }

    list.forEach(function(p) {
        var tr = document.createElement('tr');
        tr.style.cursor = 'pointer';
        tr.onclick = function() { openDetail(p.id, p.name); };
        var color = p.days_left <= 7 ? '#EF4444' : (p.days_left <= 15 ? '#F59E0B' : '#EAB308');

        var tdRef = document.createElement('td');
        var link = document.createElement('a');
        link.href = 'javascript:void(0)';
        link.className = 'td-link';
        link.textContent = p.ref || p.name || '—';
        tdRef.appendChild(link);
        tr.appendChild(tdRef);

        var tdMagasin = document.createElement('td');
        tdMagasin.textContent = p.magasin || '—';
        tr.appendChild(tdMagasin);

        var tdStock = document.createElement('td');
        tdStock.textContent = formatNumber(p.stock);
        tr.appendChild(tdStock);

        var tdRate = document.createElement('td');
        tdRate.textContent = p.daily_rate;
        tr.appendChild(tdRate);

        var tdDays = document.createElement('td');
        tdDays.textContent = p.days_left + ' j';
        tdDays.style.color = color;
        tdDays.style.fontWeight = '700';
        tr.appendChild(tdDays);

        var tdAction = document.createElement('td');
        var btn = document.createElement('button');
        btn.className = 'btn-transfer-row-icon';
        btn.title = 'Proposer un transfert';
        btn.textContent = '🔄';
        btn.onclick = function(e) {
            e.stopPropagation();
            openTransferPanel(p.id, p.name);
        };
        tdAction.appendChild(btn);
        tr.appendChild(tdAction);

        tbody.appendChild(tr);
    });
}

function _renderStockValorisation(data) {
    var htEl = el('kpi-valeur-ht');
    if (htEl) htEl.textContent = formatMAD(data.valeur_stock_ht);
    var costEl = el('kpi-valeur-cost');
    if (costEl) costEl.textContent = formatMAD(data.valeur_stock_cost);

    var tbody = el('stock-valorisation-tbody');
    if (!tbody) return;
    tbody.innerHTML = '';

    var rows = data.stock_val_by_store || [];
    if (rows.length === 0) {
        var tr = document.createElement('tr');
        var td = document.createElement('td');
        td.colSpan = 4;
        td.textContent = 'Aucune donnée';
        td.style.textAlign = 'center';
        td.style.color = '#999';
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }

    rows.sort(function(a, b) { return b.valeur_ht - a.valeur_ht; });

    rows.forEach(function(r) {
        var tr = document.createElement('tr');

        var tdName = document.createElement('td');
        tdName.textContent = r.store_name || '—';
        tr.appendChild(tdName);

        var tdQty = document.createElement('td');
        tdQty.textContent = formatNumber(r.qty);
        tr.appendChild(tdQty);

        var tdHt = document.createElement('td');
        tdHt.textContent = formatMAD(r.valeur_ht);
        tr.appendChild(tdHt);

        var tdCost = document.createElement('td');
        tdCost.textContent = formatMAD(r.valeur_cost);
        tr.appendChild(tdCost);

        tbody.appendChild(tr);
    });
}

function _renderStockAlerts(alertes) {
    var container = el('stock-alerts-list');
    if (!container) return;
    container.innerHTML = '';

    if (!alertes || alertes.length === 0) {
        container.innerHTML = '<div style="text-align:center;color:#94A3B8;padding:20px;">Aucune alerte</div>';
        return;
    }

    var dotColors = { danger: '#EF4444', warning: '#F59E0B', info: '#3B82F6', success: '#10B981' };

    alertes.forEach(function(a) {
        var div = document.createElement('div');
        div.className = 'alert-card-item alert-item-' + a.type;

        var msgBlock = document.createElement('div');
        msgBlock.className = 'alert-msg-block';

        var dot = document.createElement('span');
        dot.className = 'alert-dot';
        dot.style.color = dotColors[a.type] || '#94A3B8';
        dot.textContent = '●';
        msgBlock.appendChild(dot);

        var msgSpan = document.createElement('span');
        msgSpan.textContent = a.message;
        msgBlock.appendChild(msgSpan);

        div.appendChild(msgBlock);

        var locSpan = document.createElement('span');
        locSpan.className = 'alert-location';
        locSpan.textContent = a.magasin || '';
        div.appendChild(locSpan);

        if (a.id) {
            var tBtn = document.createElement('button');
            tBtn.className = 'btn-transfer-row-icon';
            tBtn.title = 'Proposer un transfert';
            tBtn.textContent = '🔄';
            tBtn.onclick = function() { openTransferPanel(a.id, a.name || ''); };
            div.appendChild(tBtn);
        }

        container.appendChild(div);
    });
}

function _renderRotationCollection(rotation) {
    var container = el('stock-rotation-list');
    if (!container) return;
    container.innerHTML = '';

    if (!rotation || rotation.length === 0) {
        container.innerHTML = '<div style="text-align:center;color:#94A3B8;padding:10px;">Aucune donnée</div>';
        return;
    }

    var warnings = [];

    rotation.forEach(function(r) {
        var color = r.turnover >= 4 ? '#78350F' : (r.turnover >= 2.5 ? '#B45309' : '#EF4444');

        var row = document.createElement('div');
        row.className = 'progress-item-row';

        var meta = document.createElement('div');
        meta.className = 'progress-item-meta';
        meta.innerHTML = '<span>' + r.name + '</span><span>' + r.turnover.toFixed(1) + 'x</span>';
        row.appendChild(meta);

        var barBg = document.createElement('div');
        barBg.className = 'progress-item-bar-bg';
        var barFill = document.createElement('div');
        barFill.className = 'progress-item-bar-fill';
        barFill.style.width = (r.pct || 0) + '%';
        barFill.style.background = color;
        barBg.appendChild(barFill);
        row.appendChild(barBg);

        container.appendChild(row);

        if (r.warning) warnings.push(r.warning);
    });

    var footer = document.createElement('div');
    footer.style.fontSize = '0.78rem';
    footer.style.color = '#94A3B8';
    footer.style.marginTop = '4px';
    footer.style.paddingTop = '8px';
    footer.style.borderTop = '1px solid #F1F5F9';
    footer.innerHTML = 'Cible : 4 à 6 rotations/an'
        + (warnings.length ? ' · <span style="color:#EF4444">' + warnings.join(', ') + '</span>' : '');
    container.appendChild(footer);
}

function _renderGmroiCategorie(gmroi) {
    var container = el('stock-gmroi-list');
    if (!container) return;
    container.innerHTML = '';

    if (!gmroi || gmroi.length === 0) {
        container.innerHTML = '<div style="text-align:center;color:#94A3B8;padding:10px;">Aucune donnée</div>';
        return;
    }

    gmroi.forEach(function(g) {
        var color = g.gmroi >= 3 ? '#10B981' : (g.gmroi >= 2 ? '#B45309' : '#EF4444');

        var row = document.createElement('div');
        row.className = 'progress-item-row';

        var meta = document.createElement('div');
        meta.className = 'progress-item-meta';
        meta.innerHTML = '<span>' + g.name + '</span><span style="color:' + color + '">' + g.gmroi.toFixed(1) + '</span>';
        row.appendChild(meta);

        var barBg = document.createElement('div');
        barBg.className = 'progress-item-bar-bg';
        var barFill = document.createElement('div');
        barFill.className = 'progress-item-bar-fill';
        barFill.style.width = (g.pct || 0) + '%';
        barFill.style.background = color;
        barBg.appendChild(barFill);
        row.appendChild(barBg);

        container.appendChild(row);
    });
}

// ═══════════════════════════════════════════════════════════
// EVENT LISTENERS
// ═══════════════════════════════════════════════════════════
document.addEventListener('DOMContentLoaded', function() {

    _initPeriodFilters();

    // CORRECTION #1 : Chargement en parallèle pour accélérer l'affichage
    // On lance les filtres et les KPIs en même temps
    var filtersPromise = loadFilters();
    filtersPromise.then(function() {
        // Rien à faire ici, loadKPIs est déjà lancé ci-dessous
    });
    // KPIs lancés immédiatement sans attendre les filtres
    loadKPIs();

    var chartModeArrivageBtn = el('chart-mode-arrivage');
    if (chartModeArrivageBtn) {
        chartModeArrivageBtn.addEventListener('click', function() { setSalesChartMode('arrivage'); });
    }
    var chartModeShopBtn = el('chart-mode-shop');
    if (chartModeShopBtn) {
        chartModeShopBtn.addEventListener('click', function() { setSalesChartMode('shop'); });
    }

    ['filter-collection', 'filter-magasin', 'filter-category', 'filter-batch', 'filter-date-start', 'filter-date-end',
     'filter-period-month', 'filter-period-month-year', 'filter-period-week', 'filter-period-year'].forEach(function(id) {
        var sel = el(id);
        if (sel) {
            sel.addEventListener('change', function() {
                updateFiltersFromUI();
                loadKPIs();
            });
        }
    });

    var periodTypeEl = el('filter-period-type');
    if (periodTypeEl) {
        periodTypeEl.addEventListener('change', function() {
            _updatePeriodVisibility();
            updateFiltersFromUI();
            loadKPIs();
        });
    }

    ['top-limit', 'flop-limit'].forEach(function(id) {
        var sel = el(id);
        if (sel) {
            sel.addEventListener('click', function(e) { e.stopPropagation(); });
            // Changer le nombre affiché ne relance plus l'appel KPI complet :
            // le backend renvoie déjà jusqu'à 100 lignes (cf. loadKPIs), donc
            // on tranche simplement le cache local — c'est instantané, sans
            // aller re-scanner ventes/achats/stock côté serveur pour ça.
            // Si le cache est vide (page pas encore chargée), on retombe sur
            // un chargement complet.
            sel.addEventListener('change', function() {
                updateFiltersFromUI();
                if (state.top_products_all.length || state.flop_products_all.length) {
                    _renderTopFlopFromCache();
                } else {
                    loadKPIs();
                }
            });
            sel.addEventListener('input', function() {
                updateFiltersFromUI();
                if (state.top_products_all.length || state.flop_products_all.length) {
                    _renderTopFlopFromCache();
                }
            });
        }
    });

    ['detail-filter-magasin'].forEach(function(id) {
        var sel = el(id);
        if (sel) {
            sel.addEventListener('change', function() {
                if (state.detail.article_id) {
                    refreshDetail();
                }
            });
        }
    });

    var closeBtn = el('close-detail-btn');
    if (closeBtn) closeBtn.addEventListener('click', closeDetail);

    var overlay = el('detail-overlay');
    if (overlay) {
        overlay.addEventListener('click', function(e) {
            if (e.target === overlay) closeDetail();
        });
    }

    var openTransferBtn = el('btn-open-transfer');
    if (openTransferBtn) {
        openTransferBtn.addEventListener('click', function() {
            if (!state.detail.article_id) return;
            var nameEl = el('detail-name');
            openTransferPanel(state.detail.article_id, nameEl ? nameEl.textContent : '');
        });
    }

    var exportDetailBtn = el('btn-export-detail');
    if (exportDetailBtn) {
        exportDetailBtn.addEventListener('click', function() {
            if (!state.detail.article_id) return;
            var params = new URLSearchParams();
            params.set('article_id', state.detail.article_id);
            if (state.detail.shop_field) params.set('shop_field', state.detail.shop_field);
            if (state.batch_id) params.set('batch_id', state.batch_id);
            if (state.collection_id) params.set('collection_id', state.collection_id);
            window.open('/mavie/api/product-detail/export?' + params.toString(), '_blank');
        });
    }

    function _exportTopFlop(kind) {
        var filterParams = getFilterParams();
        var params = new URLSearchParams();
        params.set('kind', kind);
        for (var k in filterParams) {
            if (filterParams[k] !== null && filterParams[k] !== undefined && filterParams[k] !== '') {
                params.set(k, filterParams[k]);
            }
        }
        window.open('/mavie/api/top-flop/export?' + params.toString(), '_blank');
    }

    var exportTopBtn = el('btn-export-top');
    if (exportTopBtn) {
        exportTopBtn.addEventListener('click', function() { _exportTopFlop('top'); });
    }

    var exportFlopBtn = el('btn-export-flop');
    if (exportFlopBtn) {
        exportFlopBtn.addEventListener('click', function() { _exportTopFlop('flop'); });
    }

    var closeTransferBtn = el('close-transfer-btn');
    if (closeTransferBtn) closeTransferBtn.addEventListener('click', closeTransferPanel);

    var transferOverlay = el('transfer-overlay');
    if (transferOverlay) {
        transferOverlay.addEventListener('click', function(e) {
            if (e.target === transferOverlay) closeTransferPanel();
        });
    }

    var closeColorDetailBtn = el('close-color-detail-btn');
    if (closeColorDetailBtn) closeColorDetailBtn.addEventListener('click', closeColorDetail);

    var colorDetailOverlay = el('color-detail-overlay');
    if (colorDetailOverlay) {
        colorDetailOverlay.addEventListener('click', function(e) {
            if (e.target === colorDetailOverlay) closeColorDetail();
        });
    }

    var btnColorDetailTransfer = el('btn-color-detail-transfer');
    if (btnColorDetailTransfer) {
        btnColorDetailTransfer.addEventListener('click', function() {
            var articleId = state.colorDetail.article_id;
            var productName = state.colorDetail.product_name;
            var color = state.colorDetail.color;
            closeColorDetail();
            openTransferPanel(articleId, productName, color);
        });
    }

    var transferDestSel = el('transfer-dest-shop');
    if (transferDestSel) {
        transferDestSel.addEventListener('change', function() {
            // Changer de destination = un nouveau groupe de bons (les bons
            // déjà créés visaient une autre destination).
            state.transfer.group_ref = null;
            state.transfer.group_count = 0;
            _showTransferSuggestionsView();
            _loadTransferSuggestions();
        });
    }

    var transferColorSel = el('transfer-color-filter');
    if (transferColorSel) {
        transferColorSel.addEventListener('change', function() {
            // Changer de couleur = un nouveau groupe de bons, même logique
            // que changer de destination ci-dessus.
            state.transfer.color = transferColorSel.value || null;
            state.transfer.group_ref = null;
            state.transfer.group_count = 0;
            _showTransferSuggestionsView();
            _loadTransferSuggestions();
        });
    }

    var transferMatrixBackBtn = el('transfer-matrix-back');
    if (transferMatrixBackBtn) {
        transferMatrixBackBtn.addEventListener('click', function() {
            _showTransferSuggestionsView();
        });
    }

    var transferMatrixCreateBtn = el('btn-transfer-matrix-create');
    if (transferMatrixCreateBtn) {
        transferMatrixCreateBtn.addEventListener('click', function() {
            _createTransferFromMatrix(transferMatrixCreateBtn);
        });
    }

    var searchInput = el('product-search-input');
    if (searchInput) {
        searchInput.addEventListener('input', function() {
            var query = searchInput.value.trim();
            clearTimeout(_searchDebounce);
            if (query.length < 2) {
                var container = el('product-search-results');
                if (container) container.classList.remove('active');
                return;
            }
            _searchDebounce = setTimeout(function() {
                _doProductSearch(query);
            }, 300);
        });
        searchInput.addEventListener('keydown', function(e) {
            e.stopPropagation();
        });
        searchInput.addEventListener('focus', function() {
            window.focus();
        });
        document.addEventListener('click', function(e) {
            var container = el('product-search-results');
            if (container && !searchInput.contains(e.target) && !container.contains(e.target)) {
                container.classList.remove('active');
            }
        });
    }

    ['card-ruptures', 'card-stock-skus-rupture', 'card-stock-taux-rupture'].forEach(function(cardId) {
        var card = el(cardId);
        if (card) {
            card.style.cursor = 'pointer';
            card.addEventListener('click', openRuptures);
        }
    });

    var searchRupturesInput = el('search-ruptures-input');
    if (searchRupturesInput) {
        searchRupturesInput.addEventListener('input', function(e) {
            _renderRupturesList(e.target.value);
        });
    }

    var closeRupturesBtn = el('close-ruptures-btn');
    if (closeRupturesBtn) closeRupturesBtn.addEventListener('click', closeRuptures);

    var rupturesOverlay = el('ruptures-overlay');
    if (rupturesOverlay) {
        rupturesOverlay.addEventListener('click', function(e) {
            if (e.target === rupturesOverlay) closeRuptures();
        });
    }

    var cardStockDormant = el('card-stock-dormant');
    if (cardStockDormant) cardStockDormant.addEventListener('click', openDormant);

    var searchDormantInput = el('search-dormant-input');
    if (searchDormantInput) {
        searchDormantInput.addEventListener('input', function(e) {
            _renderDormantList(e.target.value);
        });
    }

    var closeDormantBtn = el('close-dormant-btn');
    if (closeDormantBtn) closeDormantBtn.addEventListener('click', closeDormant);

    var dormantOverlay = el('dormant-overlay');
    if (dormantOverlay) {
        dormantOverlay.addEventListener('click', function(e) {
            if (e.target === dormantOverlay) closeDormant();
        });
    }

    // ── Section "Alerte rupture sous 30 jours" (vue Stock) ──
    var stock30jOkBtn = el('btn-stock-30j-ok');
    if (stock30jOkBtn) {
        stock30jOkBtn.addEventListener('click', function() {
            _renderStock30j(state.proches_rupture_30j_cache || []);
        });
    }

    var stock30jLimitEl = el('stock-30j-limit');
    if (stock30jLimitEl) {
        stock30jLimitEl.addEventListener('click', function(e) { e.stopPropagation(); });
        stock30jLimitEl.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                _renderStock30j(state.proches_rupture_30j_cache || []);
            }
        });
    }

    document.addEventListener('mousemove', function(e) {
        var tip = el('chart-tooltip');
        if (tip && tip.style.display === 'block') {
            tip.style.left = (e.pageX + 14) + 'px';
            tip.style.top  = (e.pageY - 36) + 'px';
        }
    });
});