/**
 * Dashboard – logique client principale
 * Module Odoo 17 : mavie_dashboard
 * Données natives Odoo (Achats, Ventes/POS, Stock) — plus de dépendance à
 * mv.article.base (Base Pivot) pour les calculs affichés.
 */

function detectCurrentPage() {
    var urlP = new URLSearchParams(window.location.search);
    var p = urlP.get('page');
    if (p && ['ventes', 'stock', 'commandes', 'action', 'propositions'].indexOf(p) !== -1) return p;
    var hash = window.location.hash;
    var hashMatch = hash.match(/page=([^&]+)/);
    if (hashMatch && ['ventes', 'stock', 'commandes', 'action', 'propositions'].indexOf(hashMatch[1]) !== -1) return hashMatch[1];
    try {
        var ref = new URLSearchParams(window.parent.location.search);
        var rp = ref.get('page');
        if (rp && ['ventes', 'stock', 'commandes', 'action', 'propositions'].indexOf(rp) !== -1) return rp;
    } catch(e) {}
    return 'ventes';
}

var currentPage = detectCurrentPage();
console.log('[Dashboard] Page détectée:', currentPage, '| URL:', window.location.href);

var state = {
    period: 'all',
    date_start: null,
    date_end: null,
    shop_field: null,
    collection_id: null,
    categ_ids: [],
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
var lastSoldesList = [];
var lastSoldesCount = 0;
// Écarts d'inventaire (références en stock négatif) : le compteur vient des
// KPIs, la liste détaillée n'est chargée qu'à l'ouverture du popup — elle
// suppose de résoudre entrepôts et libellés, inutile tant qu'on ne clique pas.
var lastEcartsRefsCount = 0;
var lastEcartsList = [];
// Historique transferts / soldes affiché sous les cartes.
var lastHistory = { transfers: [], soldes: [] };
var historyTab = 'transferts';
// Historique d'UNE référence, ouvert depuis la fiche produit.
var lastProductHistory = { transfers: [], soldes: [] };
var productHistoryTab = 'transferts';
var _searchDebounce = null;

// ── Anti-réponse-périmée ──────────────────────────────────────────────
// BUG CORRIGÉ : le graphique "CA par Arrivage" affichait par intermittence
// TOUS les arrivages alors qu'une collection était filtrée. Cause : les
// appels sont lancés sans attendre, et le temps de réponse dépend du
// périmètre — une requête SANS filtre (lente, elle scanne toutes les ventes)
// peut donc revenir APRÈS la requête filtrée lancée ensuite, et écraser le
// résultat correct. D'où le "parfois ça marche, parfois non".
// On numérote chaque appel et on ignore toute réponse qui n'est plus la
// dernière demandée.
var _reqSeq = { kpis: 0, salesDaily: 0, history: 0, productHistory: 0 };

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
        state.online_shops = data.online_shops || [];
        var magasinSelect = el('filter-magasin');
        if (magasinSelect && data.shops && data.shops.length > 0) {
            while (magasinSelect.options.length > 1) magasinSelect.remove(1);
            _appendShopOptions(magasinSelect, data.shops, data.online_shops);
            magasinSelect.disabled = false;
        } else if (magasinSelect) {
            var opt = document.createElement('option');
            opt.value = '';
            opt.textContent = 'Aucun magasin configuré';
            magasinSelect.appendChild(opt);
        }

        // Catégories : menu à cocher, plusieurs choix possibles.
        _remplirCategories(data.categories || []);

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

// Remplit un <select> magasin : magasins physiques d'abord, puis les points
// de vente en ligne dans leur propre groupe. Chaque entrée donne un chiffre
// exact et distinct — un magasin physique ne compte plus les tickets de son
// jumeau « Online », qui a désormais sa propre ligne.
function _appendShopOptions(select, shops, onlineShops) {
    (shops || []).forEach(function(s) {
        var opt = document.createElement('option');
        opt.value = s.field;
        opt.textContent = s.name;
        select.appendChild(opt);
    });
    if (onlineShops && onlineShops.length) {
        var group = document.createElement('optgroup');
        group.label = '🌐 Magasins en ligne';
        onlineShops.forEach(function(s) {
            var opt = document.createElement('option');
            opt.value = s.field;
            opt.textContent = s.name + (s.company ? ' — ' + s.company : '');
            group.appendChild(opt);
        });
        select.appendChild(group);
    }
}

function _populateDetailFilters(data) {
    var detailMagasin = el('detail-filter-magasin');
    if (detailMagasin && data.shops) {
        while (detailMagasin.options.length > 1) detailMagasin.remove(1);
        _appendShopOptions(detailMagasin, data.shops, data.online_shops);
    }

    // Destination de transfert : uniquement les magasins physiques. Un point
    // de vente en ligne n'a pas d'entrepôt propre (il partage celui de son
    // magasin), il ne peut donc être ni source ni cible d'un transfert.
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
        var deb = (startEl && startEl.value) ? startEl.value : null;
        var fin = (endEl && endEl.value) ? endEl.value : null;
        // Tant que les deux bornes ne sont pas saisies, la période ne
        // filtre rien : sinon un rechargement déclenché par un autre
        // filtre partirait avec une seule date.
        if (!deb || !fin || deb > fin) return { start: null, end: null };
        return { start: deb, end: fin };
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

// Recalcul différé de la période : on laisse le temps de choisir le mois
// PUIS l'année sans lancer deux calculs. Toute nouvelle modification
// repousse l'échéance.
var _periodeMinuteur = null;
var PERIODE_PAUSE_MS = 1500;

function _periodeApresPause() {
    clearTimeout(_periodeMinuteur);
    var e = el('filter-periode-attente');
    if (e) e.textContent = 'Calcul dans un instant\u2026';
    _periodeMinuteur = setTimeout(function() {
        if (e) e.textContent = '';
        loadKPIs();
    }, PERIODE_PAUSE_MS);
}

// Dit ce qu'on attend encore sous les deux champs de dates.
function _majIndiceDates() {
    var e = el('filter-date-hint');
    if (!e) return;
    var d1 = el('filter-date-start');
    var d2 = el('filter-date-end');
    var deb = d1 ? d1.value : '';
    var fin = d2 ? d2.value : '';
    if (deb && fin && deb > fin) {
        e.textContent = 'La date de fin doit \u00eatre apr\u00e8s la date de d\u00e9but.';
        e.style.color = '#B91C1C';
    } else if (deb && !fin) {
        e.textContent = 'Choisissez la date de fin pour lancer le calcul.';
        e.style.color = '#B45309';
    } else if (!deb && fin) {
        e.textContent = 'Choisissez la date de d\u00e9but pour lancer le calcul.';
        e.style.color = '#B45309';
    } else {
        e.textContent = '';
    }
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
    // Le filtre catégorie accepte plusieurs valeurs : state.categ_ids
    // est tenu à jour par le menu à cocher (_remplirCategories).
    state.batch_id      = (batEl  && batEl.value)   ? batEl.value  : null;
    state.date_start    = period.start;
    state.date_end      = period.end;
    state.top_limit     = (topLimitEl && topLimitEl.value && !isNaN(parseInt(topLimitEl.value))) ? Math.max(1, parseInt(topLimitEl.value)) : 10;
    state.flop_limit    = (flopLimitEl && flopLimitEl.value && !isNaN(parseInt(flopLimitEl.value))) ? Math.max(1, parseInt(flopLimitEl.value)) : 10;
}

// ── Filtre Catégorie : menu à cocher ───────────────────────────────────
// Plusieurs catégories peuvent être sélectionnées (demande du 2026-09-25).
function _remplirCategories(categories) {
    var liste = el('filter-category-list');
    var bouton = el('filter-category-btn');
    var panneau = el('filter-category-panel');
    if (!liste || !bouton || !panneau) return;

    liste.innerHTML = '';
    categories.forEach(function(c) {
        var label = document.createElement('label');
        var cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.value = c.id;
        // On ne recharge PAS ici : l'utilisatrice coche plusieurs
        // categories a la suite, et chaque clic relançait un calcul
        // complet. Le rechargement se fait a la fermeture du menu.
        cb.addEventListener('change', _majCategories);
        var txt = document.createElement('span');
        txt.textContent = c.name;
        label.appendChild(cb);
        label.appendChild(txt);
        liste.appendChild(label);
    });

    if (!bouton.dataset.lie) {
        bouton.dataset.lie = '1';
        bouton.addEventListener('click', function(e) {
            e.stopPropagation();
            if (panneau.classList.contains('open')) {
                _fermerCategories();
            } else {
                _categSelectionOuverture = (state.categ_ids || []).join(',');
                panneau.classList.add('open');
            }
        });
        panneau.addEventListener('click', function(e) { e.stopPropagation(); });
        document.addEventListener('click', _fermerCategories);
        var tout = el('filter-category-all');
        var rien = el('filter-category-none');
        if (tout) tout.addEventListener('click', function() { _cocherCategories(true); });
        if (rien) rien.addEventListener('click', function() { _cocherCategories(false); });
        var appliquer = el('filter-category-apply');
        if (appliquer) {
            appliquer.addEventListener('click', function(e) {
                e.stopPropagation();
                _fermerCategories();
            });
        }
    }
    _majCategories();
}

// Selection au moment ou le menu s'est ouvert : sert a ne relancer le
// calcul que si quelque chose a change entre-temps.
var _categSelectionOuverture = '';

function _fermerCategories() {
    var panneau = el('filter-category-panel');
    if (!panneau || !panneau.classList.contains('open')) return;
    panneau.classList.remove('open');
    if ((state.categ_ids || []).join(',') !== _categSelectionOuverture) {
        _categSelectionOuverture = (state.categ_ids || []).join(',');
        loadKPIs();
    }
}

function _cocherCategories(valeur) {
    var liste = el('filter-category-list');
    if (!liste) return;
    Array.prototype.forEach.call(liste.querySelectorAll('input'), function(cb) {
        cb.checked = valeur;
    });
    // Comme pour une case : le calcul attend la fermeture du menu.
    _majCategories();
}

function _majCategories() {
    var liste = el('filter-category-list');
    var bouton = el('filter-category-btn');
    if (!liste) return;
    var choisies = [];
    var noms = [];
    Array.prototype.forEach.call(liste.querySelectorAll('input'), function(cb) {
        if (cb.checked) {
            choisies.push(cb.value);
            var sp = cb.parentNode.querySelector('span');
            noms.push(sp ? sp.textContent : cb.value);
        }
    });
    // Tout coché revient à ne rien filtrer : on garde la liste vide pour
    // que le serveur ne pose aucun filtre inutile.
    var total = liste.querySelectorAll('input').length;
    state.categ_ids = (choisies.length && choisies.length < total) ? choisies
        : (choisies.length === total ? [] : choisies);
    if (bouton) {
        if (!state.categ_ids.length) {
            bouton.textContent = 'Toutes les catégories';
            bouton.title = '';
        } else if (state.categ_ids.length === 1) {
            bouton.textContent = noms[0];
            bouton.title = noms[0];
        } else {
            bouton.textContent = state.categ_ids.length + ' catégories';
            bouton.title = noms.join(', ');
        }
    }
    var indice = el('filter-category-hint');
    if (indice) {
        var enAttente = (state.categ_ids || []).join(',') !== _categSelectionOuverture;
        indice.textContent = enAttente
            ? (choisies.length ? choisies.length + ' cochée' + (choisies.length > 1 ? 's' : '')
               : 'Aucune cochée') + ' — pas encore appliqué'
            : 'Cochez, puis Appliquer';
        indice.style.color = enAttente ? '#B45309' : '#64748B';
    }
}

function getFilterParams() {
    return {
        shop_field:    state.shop_field,
        collection_id: state.collection_id,
        categ_ids:     state.categ_ids || [],
        batch_id:      state.batch_id,
        date_start:    state.date_start,
        date_end:      state.date_end,
        top_limit:     state.top_limit,
        flop_limit:    state.flop_limit,
        page:          currentPage,
    };
}

// Seule page portant l'historique transferts/soldes : le "Tableau de bord"
// (menu Dashboard → Tableau de bord, page=ventes), qui est aussi la page par
// défaut et celle où s'affiche la vue MOD FOR LIFE.
function isHistoryPage() {
    return currentPage === 'ventes';
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

    // DÉCISION UTILISATEUR : l'historique transferts/soldes ne vit que sur
    // la page "Tableau de bord" — c'est de là que partent la quasi-totalité
    // des transferts, et c'est aussi la page qui affiche la vue MOD FOR LIFE.
    // Le dupliquer sur "Stock & ruptures" n'apportait rien et allongeait la
    // page pour rien.
    var historySection = el('history-section');
    if (historySection) historySection.style.display = isHistoryPage() ? '' : 'none';
    // La vue dépôt n'a pas d'historique transferts/soldes : le dépôt ne
    // vend pas en caisse et ne fait pas de transferts entre magasins
    // (demande du 2026-09-25). On le cache dès que cette vue s'affiche.

    // Page « Action » : tout le reste est masqué, seul son tableau s'affiche
    // (sous la barre de filtres, qui reste active).
    var actionPage = el('action-page');
    if (actionPage) actionPage.style.display = currentPage === 'action' ? '' : 'none';

    // Page « Propositions » : seuls ses blocs s'affichent, comme la page
    // Action.
    var propositionsPage = el('propositions-page');
    if (propositionsPage) propositionsPage.style.display = currentPage === 'propositions' ? '' : 'none';
    if (currentPage === 'propositions') {
        ['main-kpi-grid', 'stock-kpi-grid', 'section-top-flop', 'stock-middle-section',
         'stock-30j-section', 'stock-valorisation-section', 'section-abc', 'section-sales-chart',
         'modforlife-kpi-grid', 'history-section', 'action-page'].forEach(function(id) {
            var e = el(id); if (e) e.style.display = 'none';
        });
        var sbS = document.querySelector('.search-bar-wrapper');
        if (sbS) sbS.style.display = 'none';
        return;
    }
    if (currentPage === 'action') {
        ['main-kpi-grid', 'stock-kpi-grid', 'section-top-flop', 'stock-middle-section',
         'stock-30j-section', 'stock-valorisation-section', 'section-abc', 'section-sales-chart',
         'modforlife-kpi-grid', 'history-section'].forEach(function(id) {
            var e = el(id); if (e) e.style.display = 'none';
        });
        var sb = document.querySelector('.search-bar-wrapper');
        if (sb) sb.style.display = 'none';
        return;
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

    // Les colonnes des tableaux Top/Flop sont désormais FIXES (CA Achat HT,
    // Qté achetée, Qté vendue, Reste) — seuls les titres changent selon la
    // page, le critère de tri restant géré côté serveur.
    if (currentPage === 'ventes') {
        if (topTitleText) topTitleText.textContent = '🏆 Top Produits (par Ventes)';
        if (flopTitleText) flopTitleText.textContent = '📉 Flop Produits (par Ventes)';
    } else if (currentPage === 'stock') {
        if (topTitleText) topTitleText.textContent = '📦 Top Stocks (Quantités Elevées)';
        if (flopTitleText) flopTitleText.textContent = '⚠️ Alertes Stock / Ruptures';
    } else if (currentPage === 'commandes') {
        if (topTitleText) topTitleText.textContent = '📥 Top Commandes (Achats)';
        if (flopTitleText) flopTitleText.textContent = '📉 Flop Commandes (Achats)';
    }
}

async function loadKPIs() {
    updateFiltersFromUI();
    adjustUIForPage();
    // Page « Action » : pas de cartes KPI, seulement son grand tableau.
    // Les filtres du haut rappellent loadKPIs : il recharge donc le tableau.
    if (currentPage === 'action') {
        loadActions();
        return;
    }
    // Page « Propositions » : même principe, ses blocs remplacent les
    // cartes.
    if (currentPage === 'propositions') {
        loadPropositions();
        return;
    }
    showLoading(true);

    var seq = ++_reqSeq.kpis;
    var params = getFilterParams();
    var data = await rpc('/mavie/api/kpis', params);
    // Même garde que loadSalesDaily : une réponse correspondant à un filtre
    // déjà remplacé ne doit ni s'afficher, ni masquer le spinner d'un appel
    // encore en cours.
    if (seq !== _reqSeq.kpis) return;
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

    // L'historique n'existe que sur la page "Tableau de bord" — inutile de
    // payer l'appel serveur ailleurs. Il reste chargé avant l'aiguillage
    // MOD FOR LIFE, car il s'affiche aussi sous cette vue.
    if (isHistoryPage()) loadHistory();

    if (data.is_modforlife) {
        var histo = el('history-section');
        if (histo) histo.style.display = 'none';
        _renderModForLifeDashboard(data);
        return;
    }
    var mflGrid = el('modforlife-kpi-grid');
    if (mflGrid) mflGrid.style.display = 'none';
    _placeSearchBar(false);

    if (currentPage === 'stock') {
        _renderStockDashboard(data);
    } else {
        var kpiMap = {
            'kpi-ca-total':      formatMAD(data.ca_total),
            'kpi-ca-achat':      formatMAD(data.ca_achat),
            // CORRECTION #2b : On affiche references_count (nb SKUs actifs) et non tickets POS
            // La carte HTML indique "Références" donc on doit montrer le bon chiffre
            // « Références » = celles qui ont une activité (vendue, achetée
            // ou en stock). Le catalogue complet est rappelé en dessous.
            'kpi-tickets':       formatNumber(data.references_count || data.total_active_skus || data.tickets),
            'kpi-panier-moyen':  formatMAD(data.panier_moyen),
            'kpi-qty-sold':      formatNumber(data.qty_sold),
            'kpi-qty-purchased': formatNumber(data.qty_purchased),
            // Stock réellement présent en rayon (négatifs exclus) — voir le
            // commentaire de 'detail-stock-total'.
            'kpi-stock-total':   formatNumber(data.stock_present),
            'kpi-sell-through':  formatPct(data.sell_through),
            'kpi-ruptures':      formatNumber(data.ruptures_count),
        };

        for (var id in kpiMap) {
            var el_obj = el(id);
            if (el_obj) el_obj.textContent = kpiMap[id];
        }

        var qtySoldSoldeEl = el('kpi-qty-sold-solde');
        lastSoldesList = data.soldes_list || [];
        lastSoldesCount = data.soldes_count || 0;

        if (qtySoldSoldeEl) {
            // Cliquable : ouvre la liste des articles concernés (même
            // principe que les cartes Ruptures / Stock dormant).
            if (data.qty_sold_solde) {
                qtySoldSoldeEl.textContent = 'dont ' + formatNumber(data.qty_sold_solde)
                    + ' pièces soldées'
                    + (lastSoldesCount ? ' · ' + formatNumber(lastSoldesCount) + ' références — voir le détail' : '');
                qtySoldSoldeEl.style.cursor = lastSoldesCount ? 'pointer' : '';
                qtySoldSoldeEl.style.textDecoration = lastSoldesCount ? 'underline' : '';
                qtySoldSoldeEl.title = lastSoldesCount ? 'Cliquer pour voir les articles vendus en solde' : '';
            } else {
                qtySoldSoldeEl.textContent = '';
                qtySoldSoldeEl.style.cursor = '';
                qtySoldSoldeEl.style.textDecoration = '';
                qtySoldSoldeEl.title = '';
            }
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

        // Rappel des stocks négatifs sous la carte Stock : ils sont exclus du
        // chiffre affiché, il faut donc dire combien ils pèsent et où.
        var stockNegEl = el('kpi-stock-negatif');
        if (stockNegEl) {
            var nbMagNeg = data.nb_magasins_negatifs || 0;
            stockNegEl.textContent = nbMagNeg
                ? '⚠️ ' + formatNumber(Math.abs(data.stock_negatif || 0)) + ' pièces en négatif ('
                  + formatNumber(nbMagNeg) + ' magasins)'
                : '';
            stockNegEl.title = nbMagNeg
                ? 'Ces pièces ne sont pas comptées dans le stock affiché : un stock négatif n\'est pas de '
                  + 'la marchandise. Elles signalent des ventes sur des articles jamais entrés dans le '
                  + 'magasin — le plus souvent un transfert entre magasins non enregistré. '
                  + 'Stock comptable Odoo, négatifs inclus : ' + formatNumber(data.stock_total) + '.'
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

// ══════════════════════════════════════════════════════════════
// MOD FOR LIFE — entrepôt importateur
//
// DEMANDE UTILISATEUR (2026-09-17) : la Qté achetée est ce qui est ENTRÉ
// en stock (bons d'achat validés et réceptionnés, sans retour) et ne bouge
// plus ; il faut le compte exact « acheté = stock + dispatché » ; et le
// dispatch doit se déplier société → magasin → référence, avec la couleur,
// les tailles et les quantités exactes.
//
// Les données viennent des deux écrans Base Pivot du batch : « Bons
// d'achat » (purchase.order de MOD FOR LIFE chez le fournisseur) et « Bons
// de vente » (sale.order vers une société magasin, un par magasin). Le
// magasin destinataire est résolu côté serveur par le bon d'achat miroir
// de la société qui reçoit.
// ══════════════════════════════════════════════════════════════

var mflState = {
    tree: [],
    filter: '',
    open: {},        // clés dépliées : "SOC" et "SOC|||MAGASIN"
    bound: false,
    depot: '',       // nom de la société dépôt, pour nommer la colonne de stock
};

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
    _placeSearchBar(true);

    // Vue en PIÈCES (décision utilisateur du 2026-08-19) : une conversion en
    // cartons avait été ajoutée puis retirée, faute de donnée en base disant
    // combien de pièces tient un carton hors chaussures.
    var kpiMap = {
        'mfl-qty-achats':   formatNumber(data.qty_achats_fournisseurs),
        'mfl-stock':        formatNumber(data.stock_entrepot),
        'mfl-qty-dispatch': formatNumber(data.qty_ventes_societes),
        'mfl-ca-achats':    formatMAD(data.ca_achats_fournisseurs),
        'mfl-ca-ventes':    formatMAD(data.ca_ventes_societes),
        'mfl-nb-commandes': formatNumber(data.nb_commandes_fournisseurs),
    };
    for (var id in kpiMap) {
        var e = el(id);
        if (e) e.textContent = kpiMap[id];
    }

    var subMap = {
        'mfl-qty-achats-sub': 'pièces reçues · ' + formatNumber(data.nb_references_achetees) + ' références',
        // Le chiffre de la carte porte sur les références achetées par le
        // dépôt (c'est ce qui équilibre « acheté − dispatché = stock ») ;
        // on rappelle à côté le total réel de l'entrepôt.
        // Le total réel de l'entrepôt a été retiré d'ici le 2026-09-29 : il
        // s'affiche en tête de la fenêtre qu'ouvre la carte.
        'mfl-stock-sub': 'pièces restantes sur les références achetées',
        'mfl-qty-dispatch-sub': 'pièces livrées · ' + formatNumber(data.dispatch_nb_magasins) + ' magasins servis',
        // Le montant ne couvre que les bons ou un prix a ete saisi. Sur
        // cette base 271 des 272 bons d'achat du depot n'en ont aucun :
        // sans ce rappel, « 9 504 MAD pour 87 943 pièces » se lit comme
        // un calcul faux alors que c'est la saisie qui manque.
        'mfl-ca-achats-sub': formatNumber(data.qty_achats_fournisseurs) + ' pièces'
            + (data.achats_bons_sans_prix
                ? ' — ⚠️ prix absent sur ' + formatNumber(data.achats_bons_sans_prix)
                  + ' des ' + formatNumber(data.nb_commandes_fournisseurs) + ' bons'
                : ''),
        'mfl-qty-ventes-sub': formatNumber(data.qty_ventes_societes) + ' pièces'
            + (data.ventes_bons_sans_prix
                ? ' — ⚠️ prix absent sur ' + formatNumber(data.ventes_bons_sans_prix)
                  + ' des ' + formatNumber(data.dispatch_nb_bons_vente) + ' bons'
                : ''),
        // « 1 par magasin » etait faux : 110 bons pour 7 magasins.
        'mfl-nb-ventes-sub': formatNumber(data.dispatch_nb_bons_vente) + ' bons de vente vers '
            + formatNumber(data.dispatch_nb_magasins) + ' magasins',
    };
    for (var sid in subMap) {
        var se = el(sid);
        if (se) se.textContent = subMap[sid];
    }

    // BLOCS DÉSACTIVÉS le 2026-09-21 (demande utilisatrice) : « Le compte
    // exact » et « Dispatché sans bon d'achat, hors compte ». Leur HTML est
    // conservé en commentaire dans views/dashboard_templates.xml. Pour les
    // réactiver : décommenter ce HTML, puis la ligne ci-dessous (elle rend
    // les deux blocs ; les fonctions _mflRenderRecon et
    // _mflRenderHorsPerimetre sont toujours présentes plus bas).
    // _mflRenderRecon(data);

    mflState.depot = data.company_name || '';
    mflState.tree = data.dispatch_par_societe || [];
    mflState.filter = '';
    var searchInput = el('mfl-dispatch-search');
    if (searchInput) searchInput.value = '';
    // Une seule société : inutile de la faire cliquer pour voir ses magasins.
    if (mflState.tree.length === 1) mflState.open[mflState.tree[0].societe] = true;

    var noteEl = el('mfl-dispatch-note');
    if (noteEl) {
        var bits = [
            formatNumber(data.qty_ventes_societes) + ' pièces dispatchées sur '
                + formatNumber(data.dispatch_nb_bons_vente) + ' bons de vente',
        ];
        // Ce que le périmètre laisse dehors est annoncé ici, pas seulement
        // dans le bloc dédié : l'arbre ne doit jamais avoir l'air complet
        // alors qu'il ne l'est pas.
        if (data.hors_perimetre_qty) {
            // Le bloc « Dispatché sans bon d'achat » a été désactivé le
            // 2026-09-21 : la note y renvoyait encore, on cherchait une
            // section qui n'existe plus. Elle se suffit maintenant à
            // elle-même.
            bits.push('⚠️ ' + formatNumber(data.hors_perimetre_qty)
                + ' pièces exclues : livrées par le dépôt alors qu’aucun bon'
                + ' d’achat fournisseur ne les couvre'
                + (data.hors_perimetre_nb_refs
                    ? ' (' + formatNumber(data.hors_perimetre_nb_refs) + ' références, '
                      + formatNumber(data.hors_perimetre_nb_bons) + ' bons)' : ''));
        }
        if (data.direct_qty_total) {
            bits.push('en orange : ' + formatNumber(data.direct_qty_total)
                + ' pièces achetées par les magasins (' + formatNumber(data.direct_nb_bons)
                + ' bons), hors totaux');
        }
        // « Magasin non identifié » : phrase retirée de cette note le
        // 2026-09-21 à la demande de l'utilisatrice (ce n'est pas un écart,
        // juste 1 pièce sans magasin de réception). L'explication reste
        // dans la ligne « Magasin non identifié » du tableau (_mflBonsLine).
        noteEl.textContent = bits.join(' · ');
    }

    _mflBindOnce();
    _mflRenderTree();

    // Le réassort a sa propre route : il se charge après l'affichage des
    // cartes, sans les retarder.
    loadReassort();
}

function _mflRenderRecon(data) {
    var b = data.balance_mfl || {};

    // ── L'équation, UNIQUEMENT sur documents.
    //
    // RÈGLE UTILISATEUR (2026-09-17) : « ne fais pas ajouter au calcul ce
    // qui n'est pas noté dans les bons ; tout doit avoir des bons, validés
    // et aussi livrés ». Une version précédente ajoutait une ligne
    // « entré sans bon d'achat » de 1 728 pièces pour équilibrer : refusée,
    // c'était une quantité inventée. Désormais le périmètre est celui des
    // bons d'achat fournisseur réceptionnés, et ce qui n'en a pas sort du
    // compte (bloc « hors compte » plus bas). L'écart restant est un vrai
    // écart, affiché comme tel.
    var eqEl = el('mfl-equation');
    if (eqEl) {
        var ecart = b.ecart || 0;
        eqEl.innerHTML =
            _mflTerm('Qté achetée', b.qty_achetee) +
            '<span class="mfl-eq-op">−</span>' +
            _mflTerm('Dispatché', b.qty_dispatchee) +
            '<span class="mfl-eq-op">=</span>' +
            _mflTerm('Stock théorique', b.stock_theorique) +
            '<span class="mfl-eq-op">vs</span>' +
            _mflTerm('Stock réel entrepôt', b.stock_reel) +
            '<span class="mfl-eq-op">→</span>' +
            _mflTerm(ecart === 0 ? 'Écart' : 'Écart',
                     ecart === 0 ? '✓ 0' : formatNumber(ecart),
                     ecart === 0 ? 'mfl-eq-ok' : 'mfl-eq-ko');
    }

    var tb = el('mfl-balance-tbody');
    if (tb) {
        var h = '';
        h += '<tr><td>Qté achetée (bons d\'achat fournisseurs)</td>'
           + '<td class="num">' + formatNumber(b.qty_achetee || 0) + '</td>'
           + '<td>Bons d\'achat confirmés du dépôt chez un fournisseur externe '
           + '(Tom&amp;Eva, DIVERS, ABC…), quantité <strong>réceptionnée</strong> '
           + '<strong>convertie en pièces</strong> (une douzaine compte 12), articles stockables, '
           + 'nette des retours au fournisseur. Les bons où le dépôt est le '
           + 'fournisseur sont exclus : c\'est le sens inverse.</td></tr>';
        h += '<tr><td>− Dispatché vers les magasins (bons de vente)</td>'
           + '<td class="num mfl-neg">− ' + formatNumber(b.qty_dispatchee || 0) + '</td>'
           + '<td>Bons de vente inter-sociétés confirmés, quantité <strong>livrée</strong>, '
           + 'nette des retours</td></tr>';
        h += '<tr class="mfl-row-total"><td>= Stock théorique</td>'
           + '<td class="num">' + formatNumber(b.stock_theorique || 0) + '</td>'
           + '<td>Ce qui devrait rester en entrepôt d\'après les bons</td></tr>';
        h += '<tr class="mfl-row-total"><td>Stock réel entrepôt</td>'
           + '<td class="num">' + formatNumber(b.stock_reel || 0) + '</td>'
           + '<td>stock.quant, emplacements internes du dépôt</td></tr>';
        var eq = b.ecart || 0;
        h += '<tr class="' + (eq === 0 ? 'mfl-row-zero' : 'mfl-row-ecart') + '">'
           + '<td>Écart</td><td class="num">' + (eq === 0 ? '0 ✓' : formatNumber(eq)) + '</td>'
           + '<td>' + (eq === 0
               ? 'Le compte tombe juste : chaque pièce achetée sur bon est soit en stock, soit partie en magasin.'
               : _mflRefsPhrase(b.nb_refs_ecart, b.refs_ecart)
                 + ' — achetée sur bon d\'achat, puis ni retrouvée en stock ni dispatchée.')
           + '</td></tr>';
        tb.innerHTML = h;
    }

    var anom = el('mfl-anomalies');
    if (anom) {
        anom.innerHTML = _mflAnomalyTable(
            'Références en écart', b.refs_ecart, b.nb_refs_ecart)
            + _mflNonStockables(data.non_stockables);
    }

    var note = el('mfl-recon-note');
    if (note) {
        var txt = 'Le stock est une photo à l\'instant t : ce compte porte donc sur '
                + 'TOUT l\'historique de l\'entrepôt, même quand un filtre de période '
                + 'est actif sur les cartes du haut.';
        if (data.periode_filtree) {
            txt = '⚠️ Un filtre de période est actif : les cartes du haut ne montrent que '
                + 'cette période, alors que ce compte porte sur tout l\'historique — '
                + 'un stock ne se découpe pas en tranches de dates.';
        }
        note.textContent = txt;
    }

    _mflRenderHorsPerimetre(data);
}

function _mflRenderHorsPerimetre(data) {
    // Ce qui est parti en magasin sans qu'aucun fournisseur ne l'ait vendu à
    // MOD FOR LIFE. Exclu de tous les totaux (règle utilisateur), mais
    // jamais caché : c'est précisément ce qu'il faut régulariser dans Odoo.
    var box = el('mfl-hors-section');
    if (!box) return;
    var rows = data.hors_perimetre || [];
    if (!rows.length) {
        box.style.display = 'none';
        return;
    }
    box.style.display = '';
    var body = el('mfl-hors-body');
    if (!body) return;
    var h = '<div class="mfl-note" style="margin-bottom:10px;">'
          + '<strong>' + formatNumber(data.hors_perimetre_qty) + ' pièces</strong> sur '
          + formatNumber(data.hors_perimetre_nb_refs)
          + (data.hors_perimetre_nb_refs > 1 ? ' références' : ' référence')
          + ' sont parties vers les magasins par '
          + formatNumber(data.hors_perimetre_nb_bons) + ' bons de vente, alors qu\'<strong>aucun '
          + 'bon d\'achat fournisseur</strong> ne les a fait entrer au dépôt. '
          + 'Elles sont exclues de toutes les quantités affichées — pour les compter, '
          + 'il faut d\'abord saisir le bon d\'achat correspondant dans Odoo.</div>';
    h += '<table class="mfl-recon-table"><thead><tr>'
       + '<th>Référence</th><th>Produit</th><th>Société</th><th>Magasin</th><th class="num">Pièces</th>'
       + '</tr></thead><tbody>';
    rows.forEach(function(x) {
        h += '<tr><td><strong>' + _escapeHtml(x.reference) + '</strong></td>'
           + '<td>' + _escapeHtml(x.produit) + '</td>'
           + '<td>' + _escapeHtml(x.societe) + '</td>'
           + '<td>' + _escapeHtml(x.magasin) + '</td>'
           + '<td class="num">' + formatNumber(x.qty) + '</td></tr>';
    });
    h += '</tbody></table>';
    body.innerHTML = h;
}


function _mflNonStockables(items) {
    // Achats réceptionnés d'articles dont Odoo ne tient PAS le stock
    // (consommable, service). Ils ont un bon d'achat, mais ne peuvent pas
    // entrer dans « acheté = stock + dispatché » : sans stock suivi, le compte
    // ne tomberait jamais juste. Vérification du 2026-09-18 : c'est ce qui
    // expliquait l'écart d'1 pièce (MD-A50530, 1 douzaine sur P00001).
    items = items || [];
    if (!items.length) return '';
    var total = items.reduce(function(a, x) { return a + (x.qty || 0); }, 0);
    var h = '<div class="mfl-anomaly" style="border-color:#BFDBFE;background:#EFF6FF;">'
          + '<div class="mfl-anomaly-title" style="color:#1E40AF;">Hors du compte : articles non suivis en stock par Odoo'
          + ' <span class="mfl-chip">' + formatNumber(total) + ' pcs</span></div>'
          + '<div class="mfl-note" style="margin:0 0 8px;">Réceptionnés sur bon d\'achat, mais déclarés '
          + '« consommable » ou « service » : Odoo ne crée aucun stock pour eux, ils ne peuvent donc être '
          + 'comparés ni au stock ni au dispatché. Pour les compter, passer l\'article en '
          + '« Article stockable » dans Odoo.</div>'
          + '<table class="mfl-recon-table"><thead><tr>'
          + '<th>Référence</th><th>Produit</th><th>Type Odoo</th><th>Bon d\'achat</th><th class="num">Pièces</th>'
          + '</tr></thead><tbody>';
    items.forEach(function(x) {
        h += '<tr><td><strong>' + _escapeHtml(x.reference) + '</strong></td>'
           + '<td>' + _escapeHtml(x.produit) + '</td>'
           + '<td>' + _escapeHtml(x.type) + '</td>'
           + '<td>' + _escapeHtml(x.bons) + '</td>'
           + '<td class="num">' + formatNumber(x.qty) + '</td></tr>';
    });
    return h + '</tbody></table></div>';
}

function _mflRefsPhrase(nb, refs) {
    nb = nb || 0;
    var noms = (refs || []).slice(0, 3).map(function(x) { return x.reference; });
    var txt = nb + (nb > 1 ? ' références' : ' référence');
    if (noms.length) {
        txt += ' : ' + _escapeHtml(noms.join(', '));
        if (nb > noms.length) txt += '…';
    }
    return txt;
}

function _mflAnomalyTable(titre, refs, nb) {
    refs = refs || [];
    if (!refs.length) return '';
    var h = '<div class="mfl-anomaly">'
          + '<div class="mfl-anomaly-title">' + _escapeHtml(titre)
          + ' <span class="mfl-chip mfl-chip-warn">' + formatNumber(nb || refs.length) + '</span></div>'
          + '<table class="mfl-recon-table"><thead><tr>'
          + '<th>Référence</th><th>Produit</th><th class="num">Variantes</th><th class="num">Pièces</th>'
          + '</tr></thead><tbody>';
    refs.forEach(function(x) {
        h += '<tr><td><strong>' + _escapeHtml(x.reference) + '</strong></td>'
           + '<td>' + _escapeHtml(x.produit) + '</td>'
           + '<td class="num">' + formatNumber(x.nb_variantes) + '</td>'
           + '<td class="num">' + formatNumber(x.qty) + '</td></tr>';
    });
    h += '</tbody></table>';
    if ((nb || 0) > refs.length) {
        h += '<div class="mfl-note">Les ' + formatNumber(refs.length)
           + ' plus grosses sur ' + formatNumber(nb) + '.</div>';
    }
    h += '</div>';
    return h;
}

function _mflTerm(label, value, cls) {
    var txt = typeof value === 'string' ? value : formatNumber(value || 0);
    return '<div class="mfl-eq-term ' + (cls || '') + '">'
         + '<b>' + _escapeHtml(txt) + '</b>'
         + '<span>' + _escapeHtml(label) + '</span></div>';
}

function _mflMatches(soc, mag, ref) {
    var f = mflState.filter;
    if (!f) return true;
    var hay = [soc.societe, mag ? mag.magasin : '',
               ref ? ref.reference : '', ref ? ref.produit : '',
               ref ? ref.couleur : ''].join(' ').toLowerCase();
    return hay.indexOf(f) !== -1;
}

function _mflVisibleTree() {
    // Le filtre ne masque jamais un niveau parent dont un enfant matche :
    // taper « BROWN » doit laisser voir dans quelle société et quel magasin
    // ces pièces sont parties.
    if (!mflState.filter) return mflState.tree;
    var out = [];
    mflState.tree.forEach(function(soc) {
        var magasins = [];
        (soc.magasins || []).forEach(function(mag) {
            var refs = (mag.references || []).filter(function(r) {
                return _mflMatches(soc, mag, r);
            });
            var directs = (mag.references_directes || []).filter(function(r) {
                return _mflMatches(soc, mag, r);
            });
            // Lignes « achat magasin » pas encore chargées : on cherche dans
            // la liste de références / couleurs envoyée avec les totaux.
            var nonCharge = !!mag.directes_a_charger;
            var matchDirect = nonCharge && (mag.recherche_directe || '').indexOf(mflState.filter) !== -1;
            if (refs.length === 0 && directs.length === 0 && !matchDirect && !_mflMatches(soc, mag, null)) return;
            if (refs.length === 0 && directs.length === 0 && !matchDirect) {
                refs = mag.references || [];
                directs = mag.references_directes || [];
            }
            function somme(lst, k) { return lst.reduce(function(a, r) { return a + r[k]; }, 0); }
            magasins.push({
                magasin: mag.magasin, code: mag.code, warehouse_id: mag.warehouse_id,
                bons_ecartes: mag.bons_ecartes, bons_sans_livraison: mag.bons_sans_livraison,
                bons_sans_prix: mag.bons_sans_prix, qty_sans_prix: mag.qty_sans_prix,
                qty: somme(refs, 'qty'),
                ca: somme(refs, 'ca'),
                nb_references: refs.length,
                bons: mag.bons, nb_bons: mag.nb_bons,
                references: refs,
                references_directes: directs,
                qty_direct: nonCharge ? (mag.qty_direct || 0) : somme(directs, 'qty'),
                nb_references_directes: nonCharge ? (mag.nb_references_directes || 0) : directs.length,
                bons_directs: mag.bons_directs, nb_bons_directs: mag.nb_bons_directs,
                directes_a_charger: mag.directes_a_charger,
                company_id_direct: mag.company_id_direct,
                _source: mag,
            });
        });
        if (!magasins.length) return;
        out.push({
            societe: soc.societe,
            qty: magasins.reduce(function(a, m) { return a + m.qty; }, 0),
            ca: magasins.reduce(function(a, m) { return a + m.ca; }, 0),
            qty_direct: magasins.reduce(function(a, m) { return a + m.qty_direct; }, 0),
            nb_magasins: magasins.length,
            magasins: magasins,
        });
    });
    return out;
}

function _mflRenderTree() {
    var host = el('mfl-dispatch-tree');
    if (!host) return;
    var tree = _mflVisibleTree();
    if (!tree.length) {
        host.innerHTML = '<div class="mfl-empty">'
            + (mflState.filter
                ? 'Aucune référence, couleur ou magasin ne correspond à ce filtre.'
                : 'Aucun dispatch vers les sociétés magasins sur cette période.')
            + '</div>';
        return;
    }
    // Filtre actif : on déplie, sinon l'utilisateur ne voit que des totaux.
    var forceOpen = !!mflState.filter;
    var html = '';
    tree.forEach(function(soc) {
        var socKey = soc.societe;
        var socOpen = forceOpen || !!mflState.open[socKey];
        html += '<div class="mfl-soc">'
             + '<div class="mfl-soc-head' + (socOpen ? ' mfl-open' : '') + '" data-soc="'
             + _escapeHtml(socKey) + '">'
             + '<span class="mfl-caret">▶</span>'
             + '<span class="mfl-grow">' + _escapeHtml(soc.societe) + '</span>'
             + '<span class="mfl-chip">' + formatNumber(soc.nb_magasins) + ' magasins</span>'
             + _mflDirectChip(soc.qty_direct)
             + '<span class="mfl-qty" title="Pièces que le dépôt a envoyées aux magasins de cette société">' + formatNumber(soc.qty) + ' envoyées</span>'
             + '<span class="mfl-ca">' + formatMAD(soc.ca) + '</span>'
             + '</div>'
             + '<div class="mfl-soc-body" data-body-soc="' + _escapeHtml(socKey) + '"'
             + (socOpen ? '' : ' style="display:none;"') + '>';

        (soc.magasins || []).forEach(function(mag) {
            var magKey = socKey + '|||' + mag.magasin;
            var magOpen = forceOpen || !!mflState.open[magKey];
            var chipCls = mag.warehouse_id ? 'mfl-chip' : 'mfl-chip mfl-chip-warn';
            html += '<div class="mfl-mag-head' + (magOpen ? ' mfl-open' : '') + '" data-mag="'
                 + _escapeHtml(magKey) + '">'
                 + '<span class="mfl-caret">▶</span>'
                 // Le code Base Pivot à côté du nom : c'est le seul repère
                 // écrit par Odoo dans le « Document d'origine » des bons de
                 // vente, donc le pont entre cette ligne et Odoo.
                 + '<span class="mfl-grow">' + _escapeHtml(mag.magasin)
                   + (mag.code ? ' <span class="mfl-code" title="Code écrit dans le Document d’origine des bons de vente Odoo">'
                                 + _escapeHtml(mag.code) + '</span>' : '') + '</span>'
                 + '<span class="' + chipCls + '" title="Codes de référence différents reçus par ce magasin">' + formatNumber((mag.nb_references || 0) + (mag.nb_references_directes || 0)) + ' réf.</span>'
                 + _mflDirectChip(mag.qty_direct)
                 // Envoyées d'abord, vendues ensuite : c'est l'ordre du
                 // tableau en dessous, et celui de la lecture (ce qui est
                 // parti, puis ce qui s'est écoulé).
                 + '<span class="mfl-qty" title="Pièces que le dépôt a envoyées à ce magasin">' + formatNumber(mag.qty) + ' envoyées</span>'
                 + (mag.vendu ? '<span class="mfl-chip" title="Pièces vendues en caisse dans ce magasin, sur ces références">' + formatNumber(mag.vendu) + ' vendues</span>' : '')
                 + '<span class="mfl-ca">' + formatMAD(mag.ca) + '</span>'
                 + '</div>'
                 + '<div class="mfl-refs" data-body-mag="' + _escapeHtml(magKey) + '"'
                 + (magOpen ? '' : ' style="display:none;"') + '>';
            // Magasin ouvert seulement : un magasin porte jusqu'à plusieurs
            // milliers de lignes de chaussures, inutile de les construire
            // toutes au chargement.
            if (magOpen) html += _mflMagBody(mag, magKey);
            html += '</div>';
        });
        html += '</div></div>';
    });
    host.innerHTML = html;
}

// ── Achats directs des magasins chez MOD FOR LIFE (2026-09-21) ──
// Les chaussures n'ont jamais de bon de vente MFL : les magasins les
// achètent par leurs propres bons d'achat, fournisseur « MOD FOR LIFE ».
// Solution retenue par l'utilisatrice : les montrer dans l'arbre, en
// orange, HORS des pièces / montants MFL (qui restent ceux des bons de
// vente, pour que achats − dispatché = stock reste juste).
function _mflDirectChip(qty) {
    if (!qty) return '';
    return '<span class="mfl-chip" style="background:#FEF3C7;color:#92400E;" '
         + 'title="Acheté par le magasin, hors totaux">'
         + '+ ' + formatNumber(qty) + ' pcs achat magasin</span>';
}

function _mflFindMag(key) {
    var found = null;
    _mflVisibleTree().forEach(function(soc) {
        (soc.magasins || []).forEach(function(mag) {
            if (soc.societe + '|||' + mag.magasin === key) found = mag;
        });
    });
    return found;
}

function _mflRefsTable(refs, direct) {
    // Ordre demandé (2026-09-28) : envoyées, puis vendues, puis ce qui
    // reste au dépôt. Chaque colonne porte un titre qui dit de quelles
    // pièces il s'agit : « Qté » tout court ne le disait pas.
    var h = '<table><thead><tr' + (direct ? ' style="background:#FEF3C7;"' : '') + '>'
          + '<th>Référence</th><th>Produit</th><th>Couleur</th>'
          + '<th>Tailles</th>'
          + '<th class="num" title="Pièces que le dépôt a envoyées à ce magasin">Envoyées au magasin</th>'
          + '<th class="num" title="Pièces vendues en caisse dans ce magasin, sur cette référence">Vendues en caisse</th>'
          // Ce qu'il reste au dépôt : sans ça on voit ce qui est parti
          // sans savoir si le magasin peut encore être servi.
          + '<th class="num" title="Stock actuel de cette référence dans l’entrepôt de ' + (mflState.depot || 'la société dépôt')
            + ' — PAS le stock du magasin. C’est donc le même chiffre sur la ligne de chaque magasin.">Reste au dépôt</th>'
          + '<th class="num" title="Montant TTC du bon de vente pour cette ligne">Montant TTC</th>'
          + '</tr></thead><tbody>';
    refs.forEach(function(r) {
        h += '<tr>'
           + '<td><strong>' + _escapeHtml(r.reference) + '</strong></td>'
           + '<td>' + _escapeHtml(r.produit) + '</td>'
           + '<td><span class="mfl-color">' + _escapeHtml(r.couleur) + '</span></td>'
           + '<td class="mfl-tailles">' + _escapeHtml(r.tailles || '—') + '</td>'
           + '<td class="num">' + formatNumber(r.qty) + '</td>'
           + '<td class="num"' + (r.vendu ? '' : ' style="color:#94A3B8;"') + '>'
             + formatNumber(r.vendu || 0) + '</td>'
           + '<td class="num"' + ((r.depot || 0) > 0 ? '' : ' style="color:#94A3B8;"') + '>'
             + formatNumber(r.depot || 0) + '</td>'
           + (direct
               ? '<td class="num" style="color:#94A3B8;" title="Bon d\'achat saisi à 0 MAD : pas de montant">—</td>'
               : '<td class="num">' + formatMAD(r.ca) + '</td>')
           + '</tr>';
    });
    return h + '</tbody></table>';
}

// Charge les lignes « achat magasin » d'un magasin au premier dépliage,
// puis redessine son contenu.
async function _mflChargerDirectes(mag, key) {
    var src = mag._source || mag;
    if (src._chargement) return;
    src._chargement = true;
    var params = getFilterParams();
    params.company_id = src.company_id_direct;
    params.warehouse_id = src.warehouse_id || 0;
    var data = await rpc('/mavie/api/mfl-achats-directs', params);
    src._chargement = false;
    if (!data || data.error) {
        src._erreurDirectes = (data && data.error) || 'Erreur inconnue';
    } else {
        src.references_directes = data.references_directes || [];
        src.directes_a_charger = false;
    }
    var host = el('mfl-dispatch-tree');
    var body = host && host.querySelector('[data-body-mag="' + _mflAttr(key) + '"]');
    var frais = _mflFindMag(key);
    if (body && frais) body.innerHTML = _mflMagBody(frais, key);
}

function _mflMagBody(mag, key) {
    var h = '';
    var refs = mag.references || [];
    if (refs.length) h += _mflBonsLine(mag) + _mflRefsTable(refs, false);
    if (!mag.qty_direct) return h;
    var directs = mag.references_directes || [];
    var bons = mag.bons_directs || [];
    var bonsTxt = bons.join(', ') + ((mag.nb_bons_directs || 0) > bons.length
        ? '… (' + formatNumber(mag.nb_bons_directs) + ' bons)' : '');
    h += '<div class="mfl-note" style="margin:' + (refs.length ? '16px' : '2px') + ' 0 8px;padding:8px 10px;'
       + 'background:#FFFBEB;border-left:3px solid #F59E0B;border-radius:6px;">'
       + '<strong style="color:#92400E;">Acheté par le magasin</strong> · '
       + formatNumber(mag.qty_direct) + ' pcs reçues, hors totaux. Bons à 0 MAD, donc pas de montant.'
       + (bonsTxt ? '<br>Bons d\'achat : ' + _escapeHtml(bonsTxt) : '')
       + '</div>';
    var src = mag._source || mag;
    if (src._erreurDirectes) {
        h += '<div class="mfl-empty" style="color:#B91C1C;">' + _escapeHtml(src._erreurDirectes) + '</div>';
    } else if (mag.directes_a_charger) {
        h += '<div class="mfl-empty">Chargement des ' + formatNumber(mag.nb_references_directes || 0) + ' références…</div>';
        if (key) _mflChargerDirectes(mag, key);
    } else if (directs.length) {
        h += _mflRefsTable(directs, true);
    } else {
        h += '<div class="mfl-empty">Aucune ligne ne correspond au filtre.</div>';
    }
    return h;
}

function _mflBonsLine(mag) {
    // Les bons de vente derrière le magasin. Sur « Magasin non identifié »,
    // c'est la réponse à « comment tu sais que c'est dispatché s'il n'y a
    // pas de magasin ? » : le bon de vente est confirmé et sa livraison
    // validée, donc la marchandise est bien sortie de l'entrepôt ; ce qui
    // manque, c'est la réception côté magasin (aucun bon d'achat miroir),
    // donc personne ne l'a fait entrer quelque part.
    var bons = mag.bons || [];
    if (!bons.length) return '';
    var txt = (mag.nb_bons > bons.length)
        ? bons.join(', ') + '… (' + formatNumber(mag.nb_bons) + ' bons)'
        : 'Bon' + (bons.length > 1 ? 's' : '') + ' de vente : ' + bons.join(', ');
    // Odoo, lui, compte TOUS les bons rattachés au magasin. Quand
    // certains n'apportent rien au tableau, le lecteur trouve plus de
    // bons dans Odoo que sur cette ligne (constaté sur ELITE 06 : 18
    // contre 15). On dit pourquoi plutôt que de laisser chercher.
    var ec = mag.bons_ecartes || 0;
    if (ec > 0) {
        var sansLiv = mag.bons_sans_livraison || 0;
        var motifs = [];
        if (sansLiv) motifs.push(formatNumber(sansLiv) + ' sans livraison');
        if (ec - sansLiv > 0) motifs.push(formatNumber(ec - sansLiv)
            + ' sans bon d’achat fournisseur');
        txt += ' · ' + formatNumber(ec) + ' bon' + (ec > 1 ? 's' : '')
            + ' écarté' + (ec > 1 ? 's' : '') + ' (' + motifs.join(', ')
            + ') — Odoo en compte donc ' + formatNumber(mag.nb_bons + ec) + '.';
    }
    // Une colonne « Montant TTC » entièrement à 0,00 se lit comme une
    // panne du tableau. C'est la saisie qui manque : sur Elite Auderby,
    // 21 des 23 bons n'ont aucun prix et emportent 13 243 des 13 373
    // pièces. On le dit ici plutôt que de laisser croire à un bug.
    var sp = mag.bons_sans_prix || 0;
    if (sp > 0) {
        txt += ' · ⚠️ ' + formatNumber(sp) + ' de ces ' + formatNumber(mag.nb_bons)
            + ' bons ont été saisis sans prix dans Odoo'
            + (mag.qty_sans_prix ? ' : ' + formatNumber(mag.qty_sans_prix)
               + ' pièces ressortent donc à 0,00 MAD' : '') + '.';
    }
    var h = '<div class="mfl-note" style="margin:2px 0 8px;">' + _escapeHtml(txt);
    if (!mag.warehouse_id) {
        h += ' — sortie de l\'entrepôt validée (bon de livraison), mais aucun '
           + 'magasin ne l\'a réceptionnée : pas de bon d\'achat miroir dans la '
           + 'société, donc pas d\'entrepôt de destination. La pièce compte dans '
           + 'le dispatché, sans magasin connu.';
    }
    return h + '</div>';
}

function _mflBindOnce() {
    if (mflState.bound) return;
    mflState.bound = true;

    var host = el('mfl-dispatch-tree');
    if (host) {
        host.addEventListener('click', function(ev) {
            var socHead = ev.target.closest && ev.target.closest('.mfl-soc-head');
            if (socHead) {
                var k = socHead.getAttribute('data-soc');
                mflState.open[k] = !mflState.open[k];
                _mflToggle(socHead, host.querySelector('[data-body-soc="' + _mflAttr(k) + '"]'), mflState.open[k]);
                return;
            }
            var magHead = ev.target.closest && ev.target.closest('.mfl-mag-head');
            if (magHead) {
                var mk = magHead.getAttribute('data-mag');
                mflState.open[mk] = !mflState.open[mk];
                var magBody = host.querySelector('[data-body-mag="' + _mflAttr(mk) + '"]');
                if (mflState.open[mk] && magBody && !magBody.innerHTML) {
                    var magData = _mflFindMag(mk);
                    if (magData) magBody.innerHTML = _mflMagBody(magData, mk);
                }
                _mflToggle(magHead, magBody, mflState.open[mk]);
            }
        });
    }

    var search = el('mfl-dispatch-search');
    if (search) {
        var timer = null;
        search.addEventListener('input', function() {
            clearTimeout(timer);
            timer = setTimeout(function() {
                mflState.filter = (search.value || '').trim().toLowerCase();
                _mflRenderTree();
            }, 180);
        });
    }

    var btnExpand = el('btn-mfl-expand');
    if (btnExpand) btnExpand.addEventListener('click', function() { _mflSetAll(true); });
    var btnCollapse = el('btn-mfl-collapse');
    if (btnCollapse) btnCollapse.addEventListener('click', function() { _mflSetAll(false); });
    var btnExport = el('btn-mfl-export');
    if (btnExport) btnExport.addEventListener('click', _mflExportCsv);
}

function _mflAttr(value) {
    return String(value).replace(/"/g, '\\"');
}

function _mflToggle(head, body, open) {
    if (head) head.classList.toggle('mfl-open', open);
    if (body) body.style.display = open ? '' : 'none';
}

function _mflSetAll(open) {
    mflState.open = {};
    if (open) {
        mflState.tree.forEach(function(soc) {
            mflState.open[soc.societe] = true;
            (soc.magasins || []).forEach(function(mag) {
                mflState.open[soc.societe + '|||' + mag.magasin] = true;
            });
        });
    }
    _mflRenderTree();
}

async function _mflExportCsv() {
    // L'export doit contenir les lignes « achat magasin » de TOUS les
    // magasins, même ceux jamais dépliés : on les charge d'abord.
    var aCharger = [];
    mflState.tree.forEach(function(soc) {
        (soc.magasins || []).forEach(function(mag) {
            if (mag.directes_a_charger && mag.qty_direct) aCharger.push(mag);
        });
    });
    await Promise.all(aCharger.map(function(mag) {
        var params = getFilterParams();
        params.company_id = mag.company_id_direct;
        params.warehouse_id = mag.warehouse_id || 0;
        return rpc('/mavie/api/mfl-achats-directs', params).then(function(data) {
            if (data && !data.error) {
                mag.references_directes = data.references_directes || [];
                mag.directes_a_charger = false;
            }
        });
    }));
    var tree = _mflVisibleTree();
    var sep = ';';
    var lines = ['Societe' + sep + 'Magasin' + sep + 'Reference' + sep + 'Produit'
                 + sep + 'Couleur' + sep + 'Tailles' + sep + 'Envoyees au magasin'
                 + sep + 'Vendues en caisse' + sep + 'Reste au depot' + sep + 'Montant TTC'
                 + sep + 'Source'];
    function cell(v) {
        var s = (v === null || v === undefined) ? '' : String(v);
        return '"' + s.replace(/"/g, '""') + '"';
    }
    tree.forEach(function(soc) {
        (soc.magasins || []).forEach(function(mag) {
            function ligne(r, source, direct) {
                lines.push([cell(soc.societe), cell(mag.magasin), cell(r.reference),
                            cell(r.produit), cell(r.couleur), cell(r.tailles),
                            r.qty, r.vendu || 0, r.depot || 0,
                            direct ? '' : String(r.ca).replace('.', ','),
                            cell(source)].join(sep));
            }
            (mag.references || []).forEach(function(r) { ligne(r, 'Bon de vente MFL'); });
            (mag.references_directes || []).forEach(function(r) { ligne(r, 'Achat magasin sans bon de vente MFL (bon a 0 MAD)', true); });
        });
    });
    // BOM UTF-8 : sans lui Excel en locale FR casse les accents des libellés
    // magasins (« Aîn Sebaa ») et des couleurs.
    var blob = new Blob(['\ufeff' + lignes.join('\r\n')], { type: 'text/csv;charset=utf-8;' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'dispatch_mod_for_life.csv';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function() { URL.revokeObjectURL(url); }, 1000);
}

function _renderTopFlopFromCache() {
    _renderProductTable('top-products-tbody', state.top_products_all.slice(0, state.top_limit), false);
    _renderProductTable('flop-products-tbody', state.flop_products_all.slice(0, state.flop_limit), true);
}

// Cellule photo d'une ligne produit. `has_image` vient du serveur : sans
// lui on afficherait le visuel de remplacement gris d'Odoo pour la quasi
// totalité du catalogue (vérifié en base : 50 fiches produit sur ~5 000
// portent réellement une image), impossible à distinguer d'une vraie photo.
function _photoPlaceholder(titre) {
    var placeholder = document.createElement('span');
    placeholder.textContent = '—';
    placeholder.title = titre || 'Aucune photo enregistrée dans Odoo pour cette référence.';
    placeholder.style.cssText = 'display:inline-block;width:40px;height:40px;line-height:40px;text-align:center;border:1px dashed #CBD5E1;border-radius:6px;color:#CBD5E1;';
    return placeholder;
}

// La fiche annonce une photo mais le fichier est absent du serveur : on
// remet le même cadre que pour « pas de photo » au lieu de laisser l'icône
// d'image cassée du navigateur (constaté sur la base Elite, dont l'export
// est arrivé sans les fichiers images).
function _photoManquante(img) {
    if (!img || !img.parentNode) return;
    img.parentNode.replaceChild(
        _photoPlaceholder('La photo est enregistrée dans Odoo mais le fichier est introuvable sur le serveur.'),
        img);
}

function _photoCell(p) {
    var td = document.createElement('td');
    td.style.width = '52px';
    if (p.has_image && p.image_url) {
        var img = document.createElement('img');
        img.src = p.image_url;
        img.alt = p.name || '';
        img.loading = 'lazy';
        img.onerror = function() { _photoManquante(img); };
        img.style.cssText = 'width:40px;height:40px;object-fit:cover;border-radius:6px;border:1px solid #E2E8F0;display:block;';
        td.appendChild(img);
    } else {
        td.appendChild(_photoPlaceholder());
    }
    return td;
}

function _renderProductTable(tbodyId, products, isFlop) {
    var tbody = el(tbodyId);
    if (!tbody) return;
    tbody.innerHTML = '';

    if (!products || products.length === 0) {
        var tr = document.createElement('tr');
        var td = document.createElement('td');
        td.colSpan = 9;
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

        tr.appendChild(_photoCell(p));

        var tdName = document.createElement('td');
        tdName.textContent = p.name;
        tr.appendChild(tdName);

        var tdRef = document.createElement('td');
        tdRef.textContent = p.ref || '—';
        tdRef.style.color = '#64748B';
        tr.appendChild(tdRef);

        // Ordre demandé : CA Achat (HT), Qté achetée, Qté vendue, Reste.
        // Colonnes fixes quelle que soit la page — le tri, lui, continue de
        // dépendre de la page (voir sorted_top/sorted_flop côté serveur).
        var tdCaAchat = document.createElement('td');
        if (!p.ca_achat && p.qty_purchased) {
            // Acheté mais sans prix renseigné sur les commandes. On
            // n'affiche PAS « 0,00 MAD » : ce zéro se lit comme un achat
            // gratuit et fausse toute comparaison, alors que la donnée est
            // simplement absente. Vérifié en base : 97,6 % des références
            // achetées (2 450 sur 2 509) sont dans ce cas, aucun prix
            // n'ayant été saisi sur les commandes avant mai 2026.
            tdCaAchat.textContent = 'non renseigné';
            tdCaAchat.style.color = '#B45309';
            tdCaAchat.style.fontStyle = 'italic';
            tdCaAchat.title = 'Aucun prix d\'achat n\'est saisi sur les commandes fournisseur de cette '
                + 'référence. Ce n\'est pas un achat à 0 MAD : la donnée manque dans Odoo.';
        } else {
            tdCaAchat.textContent = formatMAD(p.ca_achat || 0);
        }
        tr.appendChild(tdCaAchat);

        var tdCaVendu = document.createElement('td');
        tdCaVendu.textContent = formatMAD(p.ca || 0);
        tr.appendChild(tdCaVendu);

        var tdAchat = document.createElement('td');
        tdAchat.textContent = formatNumber(p.qty_purchased || 0);
        tr.appendChild(tdAchat);

        var tdVendu = document.createElement('td');
        tdVendu.textContent = formatNumber(p.qty_sold || 0);
        tr.appendChild(tdVendu);

        var tdReste = document.createElement('td');
        // Reste = acheté − vendu (même définition que la fiche produit :
        // une estimation à partir des mouvements suivis, pas le stock
        // physique, d'où la couleur rouge quand elle passe négative).
        var reste = (p.qty_purchased || 0) - (p.qty_sold || 0);
        tdReste.textContent = formatNumber(reste);
        if (reste < 0) {
            tdReste.style.color = '#EF4444';
            tdReste.title = 'Plus vendu qu\'acheté sur les commandes suivies — stock initial ou ajustement non tracé par un achat.';
        } else if (reste === 0) {
            tdReste.style.color = '#10B981';
        }
        tr.appendChild(tdReste);

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
    var seq = ++_reqSeq.salesDaily;
    var params = getFilterParams();
    var data = await rpc('/mavie/api/sales-daily', params);
    // Réponse d'un filtre déjà remplacé : on la jette au lieu d'écraser le
    // graphique avec des données qui ne correspondent plus à l'écran.
    if (seq !== _reqSeq.salesDaily) return;
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

async function openDetail(articleId, productName, shopField) {
    var overlay = el('detail-overlay');
    if (!overlay) return;
    overlay.classList.add('active');

    var nameEl = el('detail-name');
    if (nameEl) nameEl.textContent = 'Chargement...';

    var detailShopEl = el('detail-filter-magasin');
    // Ouvert depuis une ligne qui parle d'UN magasin (alerte rupture) : la
    // fiche s'ouvre sur ce magasin, sinon elle affiche le réseau entier et
    // ses chiffres ne collent pas à la ligne cliquée.
    if (shopField && detailShopEl) detailShopEl.value = shopField;
    state.detail.article_id = articleId;
    state.detail.shop_field = shopField
        || (detailShopEl ? detailShopEl.value || null : state.shop_field);

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
    var imgMissingEl = el('detail-image-missing');
    var hasPhoto = !!(data.has_image && data.image_url);
    if (imgEl) {
        if (hasPhoto) {
            // Fichier image absent du serveur : on bascule sur le message
            // « pas de photo » plutôt que l'icône cassée du navigateur.
            imgEl.onerror = function() {
                imgEl.removeAttribute('src');
                imgEl.style.display = 'none';
                if (imgMissingEl) imgMissingEl.style.display = 'flex';
            };
            imgEl.src = data.image_url;
            imgEl.style.display = 'block';
        } else {
            imgEl.removeAttribute('src');
            imgEl.style.display = 'none';
        }
    }
    // Sans photo, on l'annonce explicitement : Odoo renvoie sinon un visuel
    // de remplacement gris qui laisse croire que la photo existe.
    if (imgMissingEl) imgMissingEl.style.display = hasPhoto ? 'none' : 'flex';

    var kpiMap = {
        'detail-prix-vente':    formatMAD(data.prix_vente_ttc),
        'detail-qty-sold':      formatNumber(data.qty_sold),
        'detail-qty-purchased': formatNumber(data.qty_purchased),
        // Stock RÉELLEMENT PRÉSENT en rayon. Les stocks négatifs ne sont pas
        // du stock : les soustraire donnait un total plus bas que ce que les
        // magasins ont vraiment (9 au lieu de 13 sur LQ-119 NOIR). Ils sont
        // signalés à part, sous la carte.
        'detail-stock-total':   formatNumber(data.stock_present),
        'detail-ca':            formatMAD(data.ca),
        // « 0,00 MAD » se lit comme un achat gratuit alors que la donnée est
        // simplement absente : on écrit « non renseigné ». Vérifié en base :
        // 2 450 références sur 2 509 n'ont aucun prix sur leurs commandes.
        'detail-ca-achat':      (data.ca_achat === 0 && data.qty_purchased > 0)
                                    ? 'non renseigné' : formatMAD(data.ca_achat),
        'detail-sell-through':  formatPct(data.sell_through),

    };
    for (var id in kpiMap) {
        var e = el(id);
        if (e) e.textContent = kpiMap[id];
    }

    // Les bons d'achat de cette base ne sont pas rattachés à un magasin :
    // avec un magasin choisi, la quantité affichée est celle de la société.
    var achatEl = el('detail-qty-purchased');
    if (achatEl) {
        achatEl.title = (data.achats_perimetre === 'societe')
            ? 'Achats de la société : sur cette base, les bons d’achat sont réceptionnés dans l’entrepôt de la société, pas dans celui du magasin.'
            : '';
    }

    var detailQtySoldSoldeEl = el('detail-qty-sold-solde');
    if (detailQtySoldSoldeEl) {
        // La phrase était coupée (« … soldées, sur »).
        detailQtySoldSoldeEl.textContent = data.qty_sold_solde
            ? 'dont ' + formatNumber(data.qty_sold_solde) + ' pièces soldées'
            : '';
    }

    // Tous les CA du dashboard sont désormais en TTC (décision utilisateur
    // 2026-08-18) : la ligne "soit X HT", qui servait à comparer avec un
    // CA Achat en HT, n'a plus lieu d'être.
    var detailCaHtEl = el('detail-ca-ht');
    if (detailCaHtEl) detailCaHtEl.textContent = '';

    // « Qté achetée 0 » alors qu'il y a du stock n'est pas une incohérence :
    // la marchandise est entrée par un comptage d'inventaire, pas par un bon
    // de commande. Vérifié en base : 63 références sont dans ce cas (145
    // pièces), toutes entrées lors du démarrage d'Odoo (février 2025) ou du
    // recomptage de septembre 2025. Sans ce rappel, le lecteur croit à un
    // calcul faux.
    var qtyPurchasedNoteEl = el('detail-qty-purchased-note');
    if (qtyPurchasedNoteEl) {
        var stockSansAchat = (!data.qty_purchased && (data.stock_present || 0) > 0);
        qtyPurchasedNoteEl.textContent = stockSansAchat
            ? 'ℹ️ stock entré par inventaire, sans bon de commande'
            : '';
        qtyPurchasedNoteEl.title = stockSansAchat
            ? 'Aucune commande fournisseur n\'existe pour cette référence. Le stock présent est '
              + 'entré par un ajustement d\'inventaire (stock repris au démarrage d\'Odoo ou '
              + 'constaté lors d\'un comptage). Le détail est dans « Voir le détail ».'
            : '';
    }

    // Dans Odoo, la liste des lignes de commande filtrée sur le produit
    // additionne les TROIS sociétés. La carte, elle, ne compte que ce que
    // les magasins ont reçu : additionner le dépôt reviendrait à compter
    // deux fois le même lot, une fois à son arrivée chez l'importateur et
    // une fois à sa revente au magasin. On écrit l'écart sous la carte
    // pour que le rapprochement soit immédiat (MRC-3313 : 1 120 magasins
    // + 1 201 dépôt = les 2 321 affichés par Odoo).
    if (qtyPurchasedNoteEl && (data.qty_purchased_depot || 0) > 0) {
        var recuDepot = data.qty_purchased_depot;
        var totalOdoo = (data.qty_purchased || 0) + recuDepot;
        qtyPurchasedNoteEl.textContent = '+ ' + formatNumber(recuDepot)
            + ' reçues au dépôt — ' + formatNumber(totalOdoo) + ' au total dans Odoo';
        qtyPurchasedNoteEl.title = 'Cette carte ne compte que ce que les magasins ont reçu. '
            + (data.depot_nom || 'Le dépôt') + ' a reçu ' + formatNumber(recuDepot)
            + ' pièces du fournisseur externe avant de les revendre aux magasins : '
            + 'les additionner reviendrait à compter deux fois la même marchandise. '
            + 'Odoo additionne les trois sociétés et affiche donc ' + formatNumber(totalOdoo)
            + ' sur la liste des lignes de commande filtrée par produit.';
    }

    var detailCaAchatEl = el('detail-ca-achat');
    var caAchatManquant = (data.ca_achat === 0 && data.qty_purchased > 0);
    if (detailCaAchatEl) {
        detailCaAchatEl.style.color = caAchatManquant ? '#B45309' : '';
        detailCaAchatEl.style.fontStyle = caAchatManquant ? 'italic' : '';
        detailCaAchatEl.style.fontSize = caAchatManquant ? '1.1rem' : '';
    }

    var detailCaAchatNoteEl = el('detail-ca-achat-note');
    if (detailCaAchatNoteEl) {
        detailCaAchatNoteEl.textContent = caAchatManquant
            ? '⚠️ aucun prix saisi sur les commandes fournisseur'
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

    // La réconciliation arrive avec la fiche : on la garde pour le pop-up,
    // qui n'a plus qu'à demander les documents justificatifs.
    state.detail.reconciliation = data.reconciliation || null;

    var ecartEl = el('detail-stock-ecart');
    var stockTotalEl = el('detail-stock-total');
    if (ecartEl) {
        var ecart = data.stock_ecart || 0;
        var nbNeg = data.nb_magasins_negatifs || 0;
        // Deux anomalies différentes, par ordre de gravité :
        //  1. des pièces qu'aucun mouvement n'explique (donnée écrite en base) ;
        //  2. des magasins en stock négatif (marchandise sortie sans être
        //     entrée — typiquement un transfert non enregistré).
        // La carte affiche le stock présent ; ces deux lignes disent ce qui
        // se cache derrière.
        ecartEl.style.display = 'block';
        ecartEl.style.color = '';
        if (Math.abs(ecart) >= 1) {
            ecartEl.textContent = '⚠️ ' + formatNumber(Math.abs(ecart)) + ' pièce(s) sans mouvement — voir le détail';
            ecartEl.title = 'Stock attendu d\'après les mouvements validés = ' + formatNumber(data.stock_theorique)
                + ', stock comptable Odoo = ' + formatNumber(data.stock_total)
                + '. La différence n\'est expliquée par aucun mouvement de stock : quantité écrite directement '
                + '(import, correction en base). Cliquer sur la carte pour le détail poste par poste.';
        } else if (nbNeg) {
            ecartEl.textContent = '⚠️ ' + formatNumber(Math.abs(data.stock_negatif || 0))
                + ' pièce(s) en négatif dans ' + formatNumber(nbNeg) + ' magasin(s)';
            // La chaîne complète depuis les deux cartes voisines : sans
            // elle, « acheté − vendu » ne tombe jamais sur le stock affiché
            // et on croit à une erreur de calcul. Les deux étapes qui
            // manquent sont toujours les mêmes : l'inventaire, puis les
            // stocks négatifs écartés.
            var achatsMoinsVentes = (data.qty_purchased || 0) - (data.qty_sold || 0);
            var recon = data.reconciliation || {};
            ecartEl.title =
                'Du panier au rayon :\n'
                + '  ' + formatNumber(data.qty_purchased) + ' achetée − ' + formatNumber(data.qty_sold)
                + ' vendue = ' + formatNumber(achatsMoinsVentes) + '\n'
                + '  + ' + formatNumber(recon.inventaire_gain || 0) + ' gains d\'inventaire − '
                + formatNumber(recon.inventaire_perte || 0) + ' pertes = '
                + formatNumber(data.stock_total) + ' (stock comptable Odoo)\n'
                + '  + ' + formatNumber(Math.abs(data.stock_negatif || 0))
                + ' pièces écartées (stocks négatifs) = ' + formatNumber(data.stock_present) + ' affiché\n\n'
                + 'Les stocks négatifs viennent de magasins ayant vendu de la marchandise qui n\'y est '
                + 'jamais entrée — transfert non enregistré, le plus souvent. Ce n\'est pas de la '
                + 'marchandise, donc ce n\'est pas déduit du rayon.';
        } else {
            ecartEl.textContent = '✔️ tout est expliqué';
            ecartEl.style.color = '#059669';
            ecartEl.title = 'Le stock réel correspond exactement aux mouvements enregistrés '
                + '(achats reçus, ventes livrées, inventaire, autres sorties). Cliquer pour le détail.';
        }
    }
    if (stockTotalEl) {
        // Le stock présent ne peut pas être négatif ; plus besoin de le
        // colorer en rouge. Les magasins en négatif sont signalés par la
        // ligne au-dessous.
        stockTotalEl.style.color = '';
    }

    state.detail.variants = data.variants || [];
    // Référence à laquelle appartiennent ces couleurs : survit à la
    // fermeture de la fiche (closeDetail vide article_id), pour que le
    // panneau Transférer ouvert depuis le détail couleur les retrouve.
    state.detail.variants_article_id = state.detail.article_id;

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
        // Le message ne doit pas affirmer une cause unique : vérifié en base
        // sur 24P-6011, l'écart venait en réalité des bons de vente livrés
        // par MOD FOR LIFE, pas de couleurs désactivées. On énonce les deux
        // origines possibles au lieu d'en inventer une.
        archivedGapNote.textContent = 'ℹ️ ' + formatNumber(archivedGap) + ' vente(s) comptée(s) dans les cartes du haut '
            + 'n\'apparaissent dans aucune ligne ci-dessous : ventes sur des couleurs désactivées/discontinuées, '
            + 'ou livraisons sur bon de vente (hors caisse). La somme du tableau ne peut donc pas toujours égaler "Qté vendue".';
        archivedGapNote.style.display = '';
    } else if (archivedGapNote) {
        archivedGapNote.style.display = 'none';
    }

    // Actions faites sur la référence (transfert / solde / réassort), pour
    // colorer le stock de chaque magasin et de chaque couleur.
    state.detail.actions = data.actions_detail || { magasins: {}, couleurs: {} };
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

// ── Couleur d'action dans la fiche produit (demande utilisatrice
// 2026-09-22) : le stock d'un magasin / d'une couleur s'affiche en bleu
// s'il a été transféré, en rouge s'il est en solde, en vert s'il a eu un
// réassort. Plusieurs actions : le nombre prend la plus importante (solde,
// puis réassort, puis transfert) et une pastille par action le suit.
var ACT_COULEURS = {
    solde:     ['#DC2626', 'en solde'],
    reassort:  ['#16A34A', 'réassort'],
    transfert: ['#2563EB', 'transféré'],
};
function _actColorer(td, actions) {
    if (!td || !actions || !actions.length) return;
    var vues = {};
    var liste = ['solde', 'reassort', 'transfert'].filter(function(k) {
        if (actions.indexOf(k) === -1 || vues[k]) return false;
        vues[k] = true;
        return true;
    });
    if (!liste.length) return;
    td.style.color = ACT_COULEURS[liste[0]][0];
    var titre = liste.map(function(k) { return ACT_COULEURS[k][1]; }).join(', ');
    td.title = (td.title ? td.title + '\n' : '') + 'Action : ' + titre;
    var dots = document.createElement('span');
    dots.className = 'act-dots';
    liste.forEach(function(k) {
        var d = document.createElement('span');
        d.className = 'act-dot';
        d.style.background = ACT_COULEURS[k][0];
        dots.appendChild(d);
    });
    td.appendChild(dots);
}

function _renderStockByStore(tbodyId, stores, activeShop) {
    var tbody = el(tbodyId);
    if (!tbody) return;
    tbody.innerHTML = '';

    if (!stores || stores.length === 0) {
        var tr = document.createElement('tr');
        var td = document.createElement('td');
        td.colSpan = 4;
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
        scopeTd.colSpan = 4;
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
        noteTd.colSpan = 4;
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

        // Société propriétaire du magasin : le dispatch d'une référence se
        // lit "quelle société, quel magasin", pas seulement "quel magasin"
        // (une même société possède jusqu'à 7 magasins).
        var tdCompany = document.createElement('td');
        tdCompany.textContent = s.company || '—';
        tdCompany.style.color = '#475569';
        tdCompany.style.fontSize = '0.85rem';
        tr.appendChild(tdCompany);

        var tdName = document.createElement('td');
        tdName.textContent = s.name || s.field || '—';
        if (s.city) tdName.title = 'Ville : ' + s.city;
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
        // Action faite sur ce magasin : la couleur de l'action remplace la
        // couleur du stock (bleu transfert, rouge solde, vert réassort).
        _actColorer(tdStock, ((state.detail.actions || {}).magasins || {})[s.field]);
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
        noteTd.colSpan = 6;
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
        td.colSpan = 6;
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

        // Prix de vente catalogue TTC de la couleur, avant la quantité vendue
        // (demande utilisateur 2026-09-21). Fourchette si les tailles d'une
        // même couleur n'ont pas toutes le même prix.
        var tdPrix = document.createElement('td');
        if (v.prix_min === null || v.prix_min === undefined) {
            tdPrix.textContent = '—';
            tdPrix.style.color = '#94A3B8';
        } else if (v.prix_max !== undefined && v.prix_max !== null && v.prix_max !== v.prix_min) {
            tdPrix.textContent = formatMAD(v.prix_min) + ' – ' + formatMAD(v.prix_max);
        } else {
            tdPrix.textContent = formatMAD(v.prix_min);
        }
        tdPrix.style.whiteSpace = 'nowrap';
        tr.appendChild(tdPrix);

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
        // Action faite sur cette couleur (« * » = solde posée sur l'article
        // entier, donc valable pour toutes ses couleurs).
        var actC = (state.detail.actions || {}).couleurs || {};
        _actColorer(tdReste, (actC[v.color] || []).concat(actC['*'] || []));
        tr.appendChild(tdReste);

        tbody.appendChild(tr);
    });

    if (activeShop && variants.length > 0 && variants[0].shops) {
        var best = variants[0];
        var allShops = Object.keys(best.shops);
        if (allShops.length > 1) {
            var tr = document.createElement('tr');
            var td = document.createElement('td');
            td.colSpan = 6;
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
    // L'historique s'ouvre par-dessus la fiche : le laisser ouvert alors que
    // sa fiche a disparu le rendrait orphelin (et il porte encore l'ancienne
    // référence).
    closeProductHistory();
    state.detail.article_id = null;
}

// ═══════════════════════════════════════════════════════════
// EXTRACTION / TRANSFERT INTER-MAGASINS
// ═══════════════════════════════════════════════════════════
function openTransferPanel(articleId, productName, presetColor, targetShopField, couleurs) {
    var overlay = el('transfer-overlay');
    if (!overlay || !articleId) return;

    state.transfer.article_id = articleId;
    state.transfer.article_name = productName || '';
    state.transfer.color = presetColor || null;
    state.transfer.reassort = false;
    state.transfer.group_ref = null;
    state.transfer.group_count = 0;

    var nameEl = el('transfer-product-name');
    if (nameEl) nameEl.textContent = productName || '—';

    var destSel = el('transfer-dest-shop');
    if (destSel) {
        // DEMANDE UTILISATRICE (2026-09-23) : le magasin cible n'est plus
        // choisi d'office — la liste s'ouvre vide et on sélectionne soi-même.
        // Exception : quand l'appelant impose déjà la cible (bouton
        // « Transférer » d'une ligne de réassort, par exemple).
        destSel.value = targetShopField || '';
    }

    var colorSel = el('transfer-color-filter');
    if (colorSel) {
        while (colorSel.options.length > 1) colorSel.remove(1);
        // BUG CORRIGÉ (2026-09-22) : les couleurs venaient TOUJOURS de la
        // fiche produit ouverte. Depuis la page Action (aucune fiche
        // ouverte) la liste restait vide et la couleur cliquée (ex. KAKI)
        // n'était pas sélectionnée ; si une autre fiche avait été ouverte
        // avant, on proposait même les couleurs d'une autre référence.
        // Ordre : couleurs fournies par l'appelant, sinon celles de la fiche
        // si c'est bien la même référence, et la couleur demandée toujours.
        var liste = [];
        if (couleurs && couleurs.length) {
            liste = couleurs.slice();
        } else if (state.detail.variants_article_id == articleId) {
            liste = (state.detail.variants || []).map(function(v) { return v.color; });
        }
        if (presetColor) liste.push(presetColor);
        var seenColors = {};
        liste.forEach(function(c) {
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
    _renderTransferAllStores(data.all_stores || [], destShopField);

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
        if (s.depot) {
            // Le dépôt MOD FOR LIFE : c'est lui qui alimente le réassort.
            tdName.style.fontWeight = '700';
            tr.style.background = 'rgba(124,58,237,0.06)';
            tdName.title = 'Entrepôt du dépôt — ce n\'est pas un magasin de vente';
        }
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

function _renderTransferAllStores(allStores, destShopField) {
    var tbody = el('transfer-all-stores-tbody');
    if (!tbody) return;
    tbody.innerHTML = '';

    if (!allStores || !allStores.length) {
        var tr = document.createElement('tr');
        var td = document.createElement('td');
        td.colSpan = 6;
        td.textContent = 'Aucune donnée de magasin disponible.';
        td.style.cssText = 'text-align:center;padding:16px;color:#94A3B8;';
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }

    allStores.forEach(function(s) {
        var tr = document.createElement('tr');
        var isTarget = s.shop_field === destShopField;
        if (isTarget) {
            tr.style.background = '#F8FAFC';
        }

        var tdName = document.createElement('td');
        tdName.style.fontWeight = '600';
        tdName.style.color = '#0F172A';
        tdName.textContent = s.shop_label;
        if (isTarget) {
            var badge = document.createElement('span');
            badge.style.cssText = 'margin-left:6px; background:#DBEAFE; color:#1E40AF; font-size:0.7rem; font-weight:700; padding:2px 6px; border-radius:4px;';
            badge.textContent = 'Cible';
            tdName.appendChild(badge);
        }
        tr.appendChild(tdName);

        var tdCity = document.createElement('td');
        tdCity.textContent = s.city || '—';
        tdCity.style.color = '#64748B';
        tr.appendChild(tdCity);

        var tdDisp = document.createElement('td');
        tdDisp.style.textAlign = 'center';
        tdDisp.style.fontWeight = '600';
        tdDisp.style.color = '#2563EB';
        tdDisp.textContent = formatNumber(s.dispatched || 0);
        tr.appendChild(tdDisp);

        var tdSold = document.createElement('td');
        tdSold.style.textAlign = 'center';
        tdSold.style.fontWeight = '600';
        tdSold.style.color = '#7C3AED';
        tdSold.textContent = formatNumber(s.sold || 0);
        tr.appendChild(tdSold);

        var tdStock = document.createElement('td');
        tdStock.style.textAlign = 'center';
        tdStock.style.fontWeight = '700';
        tdStock.style.color = s.stock > 0 ? '#059669' : '#DC2626';
        tdStock.textContent = formatNumber(s.stock || 0);
        tr.appendChild(tdStock);

        var tdAction = document.createElement('td');
        tdAction.style.textAlign = 'center';
        if (isTarget) {
            var targetLabel = document.createElement('span');
            targetLabel.style.fontSize = '0.8rem';
            targetLabel.style.color = '#94A3B8';
            targetLabel.textContent = 'Magasin cible';
            tdAction.appendChild(targetLabel);
        } else if (s.stock > 0) {
            var chooseBtn = document.createElement('button');
            chooseBtn.className = 'btn-transfer-create';
            chooseBtn.style.padding = '4px 10px';
            chooseBtn.style.fontSize = '0.78rem';
            chooseBtn.textContent = 'Choisir →';
            chooseBtn.onclick = function() {
                openTransferMatrix(s.shop_field, s.shop_label, destShopField);
            };
            tdAction.appendChild(chooseBtn);
        } else {
            var noStock = document.createElement('span');
            noStock.style.fontSize = '0.8rem';
            noStock.style.color = '#CBD5E1';
            noStock.textContent = 'Pas de stock';
            tdAction.appendChild(noStock);
        }
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
        // Ouvert depuis la fenêtre Réassort : compté comme « réassort ».
        reassort: !!state.transfer.reassort,
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
    // Le bon quitte le dashboard vers l'endroit où il sera collecté, qui
    // dépend des sociétés : Inventaire → Transferts → Interne pour deux
    // magasins d'une même société, module Transferts sinon.
    var html = '<strong>✅ Transfert ' + data.transfer_name + ' créé</strong> — ';
    if (data.picking_name) {
        html += 'envoyé dans <strong>Inventaire → Transferts → Interne</strong>, opération <strong>'
             + data.picking_name + '</strong> : le responsable du magasin source n\'a plus qu\'à collecter la marchandise et valider l\'opération.';
    } else {
        html += 'envoyé dans le <strong>module Transferts</strong> (transfert entre deux sociétés) : le responsable collecte la marchandise et valide le bon là-bas.';
    }
    if (state.transfer.group_count > 1) {
        html += ' (regroupé avec ' + (state.transfer.group_count - 1) + ' autre(s) bon(s) créé(s) pour cette même référence + destination — un seul PDF imprimera tout le groupe)';
    }
    html += ' <a href="' + pdfUrl + '" target="_blank">📄 Imprimer le bon (PDF)</a>';
    if (data.warning) html += '<br/><span style="color:#B45309;">' + data.warning + '</span>';
    // Noms de responsables et de magasins viennent de la base : échappés
    // avant d'entrer dans le HTML.
    var escHtml = function (s) {
        return String(s).replace(/[&<>"']/g, function (c) {
            return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
        });
    };
    var notified = data.notified || {};
    if (notified.source && notified.source.length) {
        html += '<br/><span style="color:#15803D;">📩 Responsable du magasin source notifié (bon PDF joint) : '
             + escHtml(notified.source.join(', ')) + '</span>';
    }
    if (notified.dest && notified.dest.length) {
        html += '<br/><span style="color:#15803D;">📩 Responsable du magasin cible prévenu pour la réception : '
             + escHtml(notified.dest.join(', ')) + '</span>';
    }
    if (data.notif_warning) html += '<br/><span style="color:#B45309;">' + escHtml(data.notif_warning) + '</span>';

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
    var qtySold = 0, ca = 0, totalPieces = 0, hasTotalPieces = false, stockTotal = 0, attendu = 0;
    matching.forEach(function(v) {
        qtySold += v.qty || 0;
        ca += v.ca || 0;
        stockTotal += v.stock || 0;
        attendu += v.attendu || 0;
        if (v.total_pieces !== null && v.total_pieces !== undefined) {
            totalPieces += v.total_pieces;
            hasTotalPieces = true;
        }
    });
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
    // La carte « Stock attendu » a été retirée du pop-up (demande
    // utilisateur) : elle affichait la même valeur que « Stock total » dès
    // que la traçabilité était complète. `attendu` reste calculé — il sert
    // au bandeau d'alerte ci-dessous, qui est le seul endroit où la
    // comparaison a de l'intérêt.
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
            // Le message compare désormais Stock attendu (mouvements) et
            // Stock total (stock.quant) : les deux mesurent la même chose,
            // donc un écart est une vraie anomalie. L'ancien message
            // comparait Total pièces (achats) au stock, deux grandeurs qui
            // n'avaient aucune raison d'être égales — il criait au loup sur
            // toutes les couleurs ayant eu le moindre ajustement.
            var ecartCouleur = attendu - stockTotal;
            discordanceNote.textContent = '⚠️ ' + formatNumber(Math.abs(ecartCouleur)) + ' pièce(s) d\'écart sur cette couleur : '
                + 'stock attendu d\'après les mouvements = ' + formatNumber(attendu) + ', stock réel Odoo = '
                + formatNumber(stockTotal) + '. '
                + (ecartCouleur < 0
                    ? 'Il y a plus de stock que ce que les mouvements justifient : une quantité a été écrite directement sur l\'emplacement.'
                    : 'Il y a moins de stock que ce que les mouvements justifient : une quantité a été retirée sans mouvement de sortie.');
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

    if (msgEl) msgEl.textContent = data.note || '';
    _renderColorDetailStores(data.stores || [], data);

    // La carte du haut compte le stock des MAGASINS (périmètre filtré), le
    // tableau en dessous liste en plus le dépôt : deux totaux différents
    // dans le même pop-up. On rappelle donc la part du dépôt à côté du
    // chiffre, au lieu de laisser croire à une erreur.
    // La carte compte les MAGASINS uniquement (demande du 2026-09-25) ;
    // le dépôt reste visible dans le tableau en dessous, sur sa propre
    // ligne, pour savoir d'où faire venir la marchandise.
    var depotRow = (data.stores || []).filter(function(st) { return st.depot; })[0];
    var stockTotalEl = el('color-detail-stock-total');
    if (stockTotalEl && depotRow && depotRow.stock_total) {
        stockTotalEl.title = 'Magasins uniquement. Le dépôt en détient '
            + formatNumber(depotRow.stock_total) + ', voir le tableau ci-dessous.';
    }
}

// Colonne « Ce qui s'est passé » du pop-up couleur : une étiquette par
// mouvement, avec le détail (bons, dates) en infobulle.
function _mouvementsCell(mv, stock) {
    var td = document.createElement('td');
    var puces = [];
    function puce(txt, fond, couleur, titre) {
        puces.push('<span style="display:inline-block;margin:0 4px 4px 0;padding:2px 8px;border-radius:10px;'
            + 'font-size:0.78rem;font-weight:700;background:' + fond + ';color:' + couleur + ';"'
            + (titre ? ' title="' + _escapeHtml(titre) + '"' : '') + '>' + txt + '</span>');
    }
    // Uniquement les ACTIONS faites : transfert, réassort, solde (choix de
    // l'utilisatrice le 2026-09-23 — achats, ventes et écart retirés).
    if (mv.entree) puce('+' + formatNumber(mv.entree) + ' reçu', '#DBEAFE', '#1D4ED8');
    if (mv.sortie) puce('−' + formatNumber(mv.sortie) + ' envoyé', '#E0E7FF', '#3730A3');
    if (mv.reassort) puce('+' + formatNumber(mv.reassort) + ' réassort', '#DCFCE7', '#166534');
    if (mv.solde) puce(formatNumber(mv.solde) + ' vendus en solde', '#FEE2E2', '#B91C1C');
    if (mv.attente) puce('⏳ ' + formatNumber(mv.attente) + ' en attente', '#FEF3C7', '#92400E');
    if (!puces.length) {
        td.textContent = '—';
        td.style.color = '#94A3B8';
    } else {
        // Clic = détail complet (date, reçu/envoyé, magasin, bon, état),
        // demandé le 2026-09-23.
        td.innerHTML = puces.join('')
            + ((mv.lignes && mv.lignes.length)
                ? '<button type="button" class="ac-btn" style="padding:2px 8px;font-size:0.72rem;">Détail ▾</button>' : '');
        if (mv.details && mv.details.length) td.title = mv.details.join('\n');
        td._lignes = mv.lignes || [];
    }
    return td;
}

// Tableau détaillé des mouvements d'un magasin pour cette couleur.
function _mouvementsDetailHtml(lignes) {
    var h = '<table class="ac-table" style="margin:6px 0;"><thead><tr>'
          + '<th>Date</th><th>Sens</th><th>Magasin</th><th class="num">Qté</th><th>Bon</th><th>État</th>'
          + '</tr></thead><tbody>';
    lignes.forEach(function(l) {
        var sens = l.sens === 'recu' ? '<span style="color:#1D4ED8;font-weight:700;">Reçu</span>'
            : (l.sens === 'envoye' ? '<span style="color:#3730A3;font-weight:700;">Envoyé</span>'
            : '<span style="color:#B91C1C;font-weight:700;">Vendu en solde</span>');
        h += '<tr class="ac-var"><td>' + _escapeHtml(l.date || '—') + '</td>'
           + '<td>' + sens + (l.quoi === 'Réassort' ? ' <span style="color:#166534;">(réassort)</span>' : '') + '</td>'
           + '<td>' + _escapeHtml(l.avec || '—') + '</td>'
           + '<td class="num">' + formatNumber(l.qty) + '</td>'
           + '<td>' + _escapeHtml(l.bon || '—') + '</td>'
           + '<td' + (l.fait ? '' : ' style="color:#B45309;"') + '>' + _escapeHtml(l.etat || '') + '</td></tr>';
    });
    return h + '</tbody></table>';
}

function _renderColorDetailStores(stores, totals) {
    var tbody = el('color-detail-stores-tbody');
    if (!tbody) return;
    tbody.innerHTML = '';

    if (!stores || stores.length === 0) {
        var tr = document.createElement('tr');
        var td = document.createElement('td');
        td.colSpan = 5;
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
        // Le nombre prend la couleur de ce qui s'est passé dans ce magasin
        // (demande utilisatrice 2026-09-23) : bleu transfert, vert réassort,
        // rouge vendu en solde — au lieu d'un gris qui ne dit rien.
        var mv = s.mouvements || {};
        var faits = [];
        if (mv.solde) faits.push('solde');
        if (mv.reassort) faits.push('reassort');
        if (mv.entree || mv.sortie) faits.push('transfert');
        _actColorer(tdStock, faits);
        if (mv.details && mv.details.length) tdStock.title = mv.details.join('\n');
        tdStock.style.fontWeight = faits.length ? '700' : '';
        tr.appendChild(tdStock);

        var tdMouv = _mouvementsCell(mv, s.stock_total);
        tr.appendChild(tdMouv);
        if (tdMouv._lignes && tdMouv._lignes.length) {
            tdMouv.style.cursor = 'pointer';
            tdMouv.addEventListener('click', function() {
                var ouvert = tr.nextSibling && tr.nextSibling._detailMouvements;
                if (ouvert) { tr.parentNode.removeChild(tr.nextSibling); return; }
                var trD = document.createElement('tr');
                trD._detailMouvements = true;
                var tdD = document.createElement('td');
                tdD.colSpan = 5;
                tdD.innerHTML = _mouvementsDetailHtml(tdMouv._lignes);
                trD.appendChild(tdD);
                tr.parentNode.insertBefore(trD, tr.nextSibling);
            });
        }

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

    // Lignes de total. Un stock négatif n'est PAS du stock : on ne peut pas
    // avoir −1 pièce en rayon. Le total « ce qu'il y a en magasin » ne
    // somme donc que les stocks positifs (demande utilisateur). Les négatifs
    // sont isolés sur leur propre ligne — ce sont des anomalies à corriger,
    // pas de la marchandise à déduire. Le total comptable Odoo n'apparaît
    // que s'il diffère, pour que la carte « Stock total » du haut reste
    // rapprochable sans se contredire avec ce tableau.
    if (!totals) return;

    function _totalRow(label, value, color, hint, strong) {
        var tr = document.createElement('tr');
        tr.style.borderTop = strong ? '2px solid #CBD5E1' : '1px solid #F1F5F9';
        if (strong) tr.style.fontWeight = '800';

        var tdLabel = document.createElement('td');
        tdLabel.textContent = label;
        tdLabel.colSpan = 2;
        tr.appendChild(tdLabel);

        var tdValue = document.createElement('td');
        tdValue.textContent = formatNumber(value);
        if (color) tdValue.style.color = color;
        tr.appendChild(tdValue);

        var tdHint = document.createElement('td');
        tdHint.innerHTML = hint
            ? '<span style="font-weight:400;color:#64748B;font-size:0.8rem;">' + hint + '</span>'
            : '';
        tr.appendChild(tdHint);

        tbody.appendChild(tr);
    }

    _totalRow('STOCK PRÉSENT EN MAGASIN', totals.stock_present, '',
        'Somme des magasins ayant réellement de la marchandise. Ceux à zéro ne sont pas listés.', true);

    if (totals.nb_magasins_negatifs) {
        _totalRow('dont anomalies (stocks négatifs)', totals.stock_negatif, '#DC2626',
            totals.nb_magasins_negatifs + ' magasin(s) affichent un stock négatif : de la marchandise en est '
            + 'sortie sans y être jamais entrée. À corriger par un inventaire, ce n\'est pas du stock manquant en rayon.');
        _totalRow('Total comptable Odoo', totals.stock_total, '#64748B',
            'Négatifs inclus. C\'est cette valeur qu\'affiche la carte « Stock total » et qui sert à la réconciliation.');
    }
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
            _setRetour('detail-overlay', openRuptures);
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
        td.colSpan = 6;
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
            _setRetour('detail-overlay', openDormant);
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
        tdMagasin.style.padding = '10px';
        tdMagasin.style.color = '#334155';
        tdMagasin.style.fontSize = '0.85rem';
        // Un seul magasin affiché : celui où la marchandise n'a plus bougé
        // depuis le plus longtemps (c'est là que le stock est vraiment
        // bloqué), avec la quantité qui s'y trouve. La répartition complète
        // reste disponible au survol.
        tdMagasin.textContent = (p.magasin || '—')
            + ((p.magasin_qty !== null && p.magasin_qty !== undefined)
                ? ' (' + formatNumber(p.magasin_qty) + ')' : '');
        var br = p.magasin_breakdown || [];
        var tip = '';
        if (p.magasin_days !== null && p.magasin_days !== undefined) {
            var sub = document.createElement('div');
            sub.textContent = 'sans mouvement depuis ' + formatNumber(p.magasin_days) + ' jours';
            sub.style.fontSize = '0.75rem';
            sub.style.color = '#B45309';
            tdMagasin.appendChild(sub);
            tip = 'Dernier mouvement dans ce magasin : ' + (p.magasin_last_move || '?') + '\n';
        }
        if (br.length > 1) {
            tip += 'Répartition sur ' + br.length + ' magasins :\n' + br.map(function(x) {
                return '• ' + x.magasin + ' : ' + formatNumber(x.qty);
            }).join('\n');
        }
        tdMagasin.title = tip;
        tr.appendChild(tdMagasin);

        var tdStock = document.createElement('td');
        tdStock.textContent = formatNumber(p.stock || 0);
        tdStock.style.padding = '10px';
        tdStock.style.textAlign = 'center';
        tdStock.style.color = '#B45309';
        tdStock.style.fontWeight = '700';
        tdStock.style.fontSize = '0.85rem';
        tr.appendChild(tdStock);

        // Pourquoi cette ligne est la : ce qu'elle a vendu en 90 jours, et
        // le temps qu'il faudrait pour ecouler le reste a ce rythme.
        var tdVendu = document.createElement('td');
        tdVendu.textContent = formatNumber(p.vendu_90j || 0);
        tdVendu.style.padding = '10px';
        tdVendu.style.textAlign = 'center';
        tdVendu.style.fontSize = '0.85rem';
        if (!p.vendu_90j) {
            tdVendu.style.color = '#B91C1C';
            tdVendu.style.fontWeight = '700';
            tdVendu.title = 'Aucune vente en caisse sur les 90 derniers jours.';
        } else {
            tdVendu.style.color = '#334155';
        }
        tr.appendChild(tdVendu);

        var tdCouv = document.createElement('td');
        tdCouv.style.padding = '10px';
        tdCouv.style.textAlign = 'center';
        tdCouv.style.fontSize = '0.85rem';
        if (p.couverture_jours === null || p.couverture_jours === undefined) {
            tdCouv.textContent = '—';
            tdCouv.style.color = '#94A3B8';
            tdCouv.title = 'Rien vendu : la couverture ne peut pas être calculée.';
        } else {
            tdCouv.textContent = formatNumber(p.couverture_jours) + ' j';
            tdCouv.style.color = '#B45309';
            tdCouv.style.fontWeight = '600';
            tdCouv.title = 'Au rythme des 90 derniers jours, il faudrait '
                + formatNumber(p.couverture_jours) + ' jours pour écouler ce stock.';
        }
        tr.appendChild(tdCouv);

        tbody.appendChild(tr);
    });
}

// CE QUI RESTE DANS L’ENTREPOT DU DEPOT
//
// La carte « Stock entrepôt » affiche deux totaux (les références que le
// dépôt a lui-même achetées, puis le stock réel de l’entrepôt) sans
// jamais dire de quoi ils sont faits. Cette fenêtre les détaille ligne
// par ligne (demande utilisatrice 2026-09-28).
var depotStockState = { rows: [], data: null, charge: false };

function _depotStockRender(filtre) {
    var tbody = el('depot-stock-tbody');
    if (!tbody) return;
    var rows = depotStockState.rows;
    var q = (filtre || '').toLowerCase().trim();
    if (q) {
        rows = rows.filter(function(r) {
            return (r.reference + ' ' + r.variante + ' ' + r.couleur + ' '
                    + r.emplacement).toLowerCase().indexOf(q) !== -1;
        });
    }
    var badge = el('depot-stock-badge');
    if (badge) badge.textContent = formatNumber(rows.length) + ' lignes';
    if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="5" style="padding:18px;text-align:center;color:#64748B;">'
            + (q ? 'Aucune ligne ne correspond.' : 'L’entrepôt est vide.') + '</td></tr>';
        return;
    }
    tbody.innerHTML = rows.map(function(r) {
        var neg = r.qty < 0;
        return '<tr style="border-bottom:1px solid #F1F5F9;">'
             + '<td style="padding:9px 10px;font-weight:600;color:#0F172A;">' + _escapeHtml(r.reference)
             + (r.archive ? ' <span class="mfl-code" title="Référence archivée dans Odoo">archivée</span>' : '')
             + '</td>'
             + '<td style="padding:9px 10px;color:#475569;">' + _escapeHtml(r.variante) + '</td>'
             + '<td style="padding:9px 10px;color:#64748B;font-size:0.84rem;">' + _escapeHtml(r.emplacement) + '</td>'
             + '<td style="padding:9px 10px;text-align:right;font-weight:700;'
             + (neg ? 'color:#B91C1C;' : 'color:#0F172A;') + '">' + formatNumber(r.qty) + '</td>'
             + '<td style="padding:9px 10px;font-size:0.82rem;'
             + (neg ? 'color:#B91C1C;' : (r.achetee ? 'color:#047857;' : 'color:#B45309;')) + '">'
             + (neg
                 ? (r.achetee ? 'Sorties supérieures aux entrées' : 'Pas d’achat au dépôt')
                 : (r.achetee ? 'achetée par le dépôt' : 'jamais achetée ici')) + '</td>'
             + '</tr>';
    }).join('');
}

async function openDepotStock() {
    var overlay = el('depot-stock-overlay');
    if (overlay) overlay.classList.add('active');
    var tbody = el('depot-stock-tbody');
    if (!depotStockState.charge) {
        if (tbody) tbody.innerHTML = '<tr><td colspan="5" style="padding:18px;text-align:center;color:#64748B;">Chargement…</td></tr>';
        var data = await rpc('/mavie/api/mfl-stock-entrepot', getFilterParams());
        if (!data || data.error) {
            if (tbody) tbody.innerHTML = '<tr><td colspan="5" style="padding:18px;text-align:center;color:#B91C1C;">'
                + _escapeHtml((data && data.error) || 'Erreur inconnue') + '</td></tr>';
            return;
        }
        depotStockState.rows = data.rows || [];
        depotStockState.data = data;
        depotStockState.charge = true;
        var sub = el('depot-stock-sub');
        if (sub) {
            var archivesNonAchetees = data.total_archives - data.total_archives_achetees;
            sub.textContent = 'Liste (articles actifs) : ' + formatNumber(data.total)
                + ' = ' + formatNumber(data.total_achetees) + ' achetées par le dépôt'
                + ' ' + formatNumber(data.total_non_achetees_actifs) + ' non achetées'
                + ' · Archivés (non listés) : ' + formatNumber(data.total_archives)
                + ' = ' + formatNumber(data.total_archives_achetees) + ' achetées'
                + ' ' + formatNumber(archivesNonAchetees) + ' non achetées'
                + ' · Total Odoo, tous articles : ' + formatNumber(data.total)
                + ' ' + formatNumber(-data.total_archives) + ' = ' + formatNumber(data.total + data.total_archives)
                + ' · Carte : ' + formatNumber(data.total_achetees) + ' + '
                + formatNumber(data.total_archives_achetees) + ' = '
                + formatNumber(data.total_achetees + data.total_archives_achetees)
                + ' (achetées, actifs et archivés)'
                + ' · ' + formatNumber(data.nb_references) + ' références actives'
                + (data.total_negatif ? ' · ⚠️ ' + formatNumber(data.total_negatif)
                   + ' en stock négatif (sorti sans jamais entrer)' : '');
        }
    }
    var recherche = el('search-depot-stock');
    _depotStockRender(recherche ? recherche.value : '');
}

function closeDepotStock() {
    var overlay = el('depot-stock-overlay');
    if (overlay) overlay.classList.remove('active');
}

function _depotStockExport() {
    var rows = depotStockState.rows || [];
    var sep = ';';
    var lignes = ['Reference' + sep + 'Article' + sep + 'Emplacement' + sep
                  + 'Quantite' + sep + 'Origine' + sep + 'Archivee'];
    function cell(v) {
        return '"' + String(v === null || v === undefined ? '' : v).replace(/"/g, '""') + '"';
    }
    rows.forEach(function(r) {
        lignes.push([cell(r.reference), cell(r.variante), cell(r.emplacement), r.qty,
                     cell(r.achetee ? 'achetee par le depot' : 'jamais achetee ici'),
                     cell(r.archive ? 'oui' : 'non')].join(sep));
    });
    var blob = new Blob(['﻿' + lignes.join('\r\n')], { type: 'text/csv;charset=utf-8;' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'stock-entrepot-depot.csv';
    a.click();
    URL.revokeObjectURL(url);
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
// ARTICLES VENDUS EN SOLDE
// ═══════════════════════════════════════════════════════════
function _renderSoldesList(searchFilter) {
    var tbody = el('soldes-tbody');
    if (!tbody) return;
    tbody.innerHTML = '';

    var badge = el('soldes-count-badge');
    if (badge) {
        badge.textContent = formatNumber(lastSoldesCount) + ' articles'
            + (lastSoldesCount > lastSoldesList.length ? ' (' + formatNumber(lastSoldesList.length) + ' affichés)' : '');
    }

    var list = lastSoldesList;
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
        td.colSpan = 7;
        td.textContent = searchFilter ? 'Aucun article ne correspond à votre recherche.' : 'Aucune vente en solde sur ce périmètre.';
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
            closeSoldes();
            openDetail(p.id, p.name);
            _setRetour('detail-overlay', openSoldes);
        };

        function cell(text, align, color, weight) {
            var td = document.createElement('td');
            td.textContent = text;
            td.style.padding = '10px';
            if (align) td.style.textAlign = align;
            if (color) td.style.color = color;
            if (weight) td.style.fontWeight = weight;
            return td;
        }

        tr.appendChild(cell(p.ref || '—', null, '#64748B', '600'));
        tr.appendChild(cell(p.name || '—'));
        tr.appendChild(cell(formatNumber(p.qty_solde), 'center', null, '700'));
        tr.appendChild(cell(formatMAD(p.prix_catalogue), 'right', '#64748B'));
        tr.appendChild(cell(formatMAD(p.prix_moyen_paye), 'right', '#0F172A', '600'));
        tr.appendChild(cell('-' + (p.remise_pct || 0).toFixed(1).replace('.', ',') + ' %', 'center', '#DC2626', '700'));
        tr.appendChild(cell(formatMAD(p.ca_solde), 'right'));

        tbody.appendChild(tr);
    });
}

function openSoldes() {
    if (!lastSoldesCount) return;
    _renderSoldesList();
    var overlay = el('soldes-overlay');
    if (overlay) overlay.classList.add('active');
}

function closeSoldes() {
    var overlay = el('soldes-overlay');
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
        // La carte affiche désormais le TAUX D'ÉCART (0 % = tout est sain)
        // et non plus une "précision" à ~99 % qui masquait le problème.
        'kpi-stock-precision':    formatPctTight(data.ecarts_inventaire_pct || 0),
    };
    for (var id in kpiMap) {
        var e = el(id);
        if (e) e.textContent = kpiMap[id];
    }

    var subEl = el('kpi-stock-skus-rupture-sub');
    if (subEl) {
        // A08 : une référence absente d'un seul magasin n'entrait pas dans
        // le compte « toutes boutiques ». On affiche les deux.
        // « actifs » = les références qui ont une activité (vendue,
        // achetée ou en stock) — pas le catalogue entier, sinon le taux de
        // rupture serait dilué par des fiches qui n'ont jamais tourné.
        subEl.textContent = '/ ' + formatNumber(data.total_active_skus) + ' références'
            + (data.ruptures_magasin_count
                ? ' · ' + formatNumber(data.ruptures_magasin_count) + ' manques par magasin'
                : '');
        subEl.title = data.ruptures_magasin_count
            ? formatNumber(data.ruptures_magasin_refs) + ' références sont à zéro dans au moins un magasin.'
            : '';
    }

    // A09 : les articles reçus depuis moins de 90 jours n'ont pas encore eu
    // le temps de se vendre, ils sont comptés à part.
    var dormantSub = el('kpi-stock-dormant-sub');
    if (dormantSub) {
        // Deux motifs possibles : rien vendu, ou trop lent a s'ecouler. On
        // dit lequel domine, et combien de recents sont mis de cote.
        var motifs = [];
        if (data.dormant_lents_count) {
            motifs.push(formatNumber(data.dormant_lents_count) + ' réf. trop lentes (> '
                + formatNumber(data.dormant_seuil_couverture || 120) + ' j)');
        }
        if (data.dormant_recents_count) {
            motifs.push(formatNumber(data.dormant_recents_count)
                + ' reçues récemment, hors compte');
        }
        dormantSub.textContent = motifs.length
            ? 'Seuil : 10% · ' + motifs.join(' · ')
            : 'Seuil : 10%';
        dormantSub.title = data.dormant_recents_count
            ? formatNumber(data.dormant_recents_stock) + ' pièces sans vente, mais réceptionnées depuis moins de 90 jours.'
            : '';
    }

    // Carte "Écarts d'inventaire" : verte à 0 %, rouge dès qu'un écart
    // existe, avec le nombre de références et de pièces manquantes.
    lastEcartsRefsCount = data.ecarts_refs_count || 0;
    var ecartsValueEl = el('kpi-stock-precision');
    var ecartsSubEl = el('kpi-stock-precision-sub');
    var ecartsCardEl = el('card-stock-precision');
    if (ecartsValueEl) {
        ecartsValueEl.style.color = lastEcartsRefsCount ? '#EF4444' : '#10B981';
    }
    if (ecartsSubEl) {
        ecartsSubEl.textContent = lastEcartsRefsCount
            ? formatNumber(lastEcartsRefsCount) + ' réf. · '
              + formatNumber(Math.abs(data.ecarts_qty_manquante || 0)) + ' pièces manquantes — cliquer'
            : 'Cible : 0% — aucun écart détecté';
    }
    if (ecartsCardEl) {
        ecartsCardEl.style.cursor = lastEcartsRefsCount ? 'pointer' : 'default';
    }

    lastRupturesList = data.ruptures_list || [];
    lastDormantList = data.dormant_list || [];
    lastRupturesCount = data.ruptures_count || 0;
    lastDormantCount = data.dormant_count || 0;

    _renderStockAlerts(data.alertes_stock);
    _renderRotationCollection(data.rotation_collection);
    _renderGmroiCategorie(data.gmroi_categorie, data.valeur_cost_couverture);
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
        tr.onclick = function() { openDetail(p.id, p.name, p.shop_field); };
        tr.title = 'Ouvrir la fiche sur ' + (p.magasin || 'ce magasin');
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
        // Colonne passée en ventes PAR SEMAINE (demande du 2026-09-25) :
        // à ce rythme de vente, un chiffre journalier était souvent
        // inférieur à 1 et ne disait rien.
        var parSemaine = (parseFloat(p.daily_rate) || 0) * 7;
        tdRate.textContent = parSemaine >= 10
            ? formatNumber(Math.round(parSemaine))
            : parSemaine.toFixed(1).replace('.', ',');
        tdRate.title = 'Soit ' + p.daily_rate + ' par jour.';
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
    if (costEl) {
        // A07 : sans coût saisi dans Odoo, ce montant repose sur une
        // poignée d'articles. On préfère le dire qu'afficher un faux total.
        // DEMANDE UTILISATRICE (2026-09-25) : montrer le montant, même
        // quand aucun article n'a de coût saisi dans Odoo — il est alors
        // reconstitué à partir du prix d'achat réellement payé. On le dit
        // dans l'info-bulle plutôt que de masquer le chiffre.
        var couvCost = data.valeur_cost_couverture;
        costEl.textContent = formatMAD(data.valeur_stock_cost);
        costEl.style.fontSize = '';
        costEl.title = (data.valeur_cost_disponible === false)
            ? 'Aucun article n’a de coût saisi dans Odoo : valeur estimée d’après le prix d’achat payé.'
            : ((typeof couvCost === 'number' && couvCost < 90)
                ? 'Coût renseigné sur ' + couvCost + ' % des pièces ; le reste est estimé d’après le prix d’achat payé.'
                : '');
        var costNote = el('kpi-valeur-cost-note');
        if (costNote) {
            costNote.textContent = (data.valeur_cost_disponible === false
                || (typeof couvCost === 'number' && couvCost < 90))
                ? 'estimée d’après le prix d’achat payé' : '';
        }
    }

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
        // Chaque ligne est une société : on peut l'ouvrir pour voir la
        // ventilation magasin par magasin (valeur, quantité).
        if (r.company_id) {
            tr.style.cursor = 'pointer';
            tr.title = 'Voir le détail par magasin de ' + (r.store_name || '');
            tr.onclick = function() { openValorisationDetail(r.company_id, r.store_name); };
            tdName.innerHTML = '<span class="td-link">' + (r.store_name || '—') + '</span> 🔎';
        } else {
            tdName.textContent = r.store_name || '—';
        }
        tr.appendChild(tdName);

        var tdQty = document.createElement('td');
        tdQty.textContent = formatNumber(r.qty);
        tr.appendChild(tdQty);

        var tdHt = document.createElement('td');
        tdHt.textContent = formatMAD(r.valeur_ht);
        tr.appendChild(tdHt);

        var tdCost = document.createElement('td');
        tdCost.textContent = formatMAD(r.valeur_cost);
        if (data.valeur_cost_disponible === false) {
            tdCost.title = 'Valeur estimée d’après le prix d’achat payé : aucun coût n’est saisi dans Odoo.';
        }
        tr.appendChild(tdCost);

        tbody.appendChild(tr);
    });
}

// ═══════════════════════════════════════════════════════════
// VALORISATION — DÉTAIL PAR MAGASIN D'UNE SOCIÉTÉ
// ═══════════════════════════════════════════════════════════
async function openValorisationDetail(companyId, companyName) {
    var overlay = el('valorisation-overlay');
    var nameEl = el('valorisation-company-name');
    var summaryEl = el('valorisation-summary');
    var tbody = el('valorisation-tbody');
    if (!overlay || !tbody) return;

    if (nameEl) nameEl.textContent = companyName || '';
    if (summaryEl) summaryEl.textContent = 'Chargement…';
    tbody.innerHTML = '';
    overlay.classList.add('active');

    var params = getFilterParams();
    params.company_id = companyId;
    var data = await rpc('/mavie/api/valorisation-detail', params);

    if (!data || data.error) {
        if (summaryEl) summaryEl.textContent = 'Erreur : ' + ((data && data.error) || 'inconnue');
        return;
    }

    // Aucun coût saisi dans Odoo : on ne montre pas un montant au coût que
    // rien ne fonde, ici non plus (même règle que la carte et le tableau).
    // On affiche le montant dans tous les cas ; l'info-bulle dit s'il
    // s'agit d'une estimation.
    var coutIndisponible = false;
    var coutEstime = data.cost_disponible === false || data.cost_estime;
    if (summaryEl) {
        summaryEl.textContent = formatNumber(data.total_qty) + ' pièces · '
            + formatMAD(data.total_ht) + ' HT · '
            + (coutIndisponible
                ? 'coût non disponible'
                : formatMAD(data.total_cost) + ' au coût'
                  + (data.cost_estime
                      ? ' (coût estimé depuis le prix d\'achat réellement payé pour les références sans champ « Coût » renseigné)'
                      : ''));
    }

    var rows = data.magasins || [];
    if (!rows.length) {
        var trEmpty = document.createElement('tr');
        var tdEmpty = document.createElement('td');
        tdEmpty.colSpan = 4;
        tdEmpty.textContent = 'Aucun stock valorisé pour cette société sur ce périmètre.';
        tdEmpty.style.cssText = 'text-align:center;padding:24px;color:#94A3B8;';
        trEmpty.appendChild(tdEmpty);
        tbody.appendChild(trEmpty);
        return;
    }

    rows.forEach(function(m) {
        var tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid #F1F5F9';

        function cell(text, align, weight, color) {
            var td = document.createElement('td');
            td.textContent = text;
            td.style.padding = '10px';
            if (align) td.style.textAlign = align;
            if (weight) td.style.fontWeight = weight;
            if (color) td.style.color = color;
            return td;
        }

        tr.appendChild(cell(m.name || '—', null, '600', '#0F172A'));
        tr.appendChild(cell(formatNumber(m.qty), 'center'));
        tr.appendChild(cell(formatMAD(m.valeur_ht), 'right', '600', '#10B981'));
        tr.appendChild(coutIndisponible
            ? cell('—', 'right', null, '#94A3B8')
            : cell(formatMAD(m.valeur_cost), 'right'));
        tbody.appendChild(tr);
    });
}

function closeValorisationDetail() {
    var overlay = el('valorisation-overlay');
    if (overlay) overlay.classList.remove('active');
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
            tBtn.onclick = function() { openTransferPanel(a.id, a.name || '', null, a.shop_field); };
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

function _renderGmroiCategorie(gmroi, couvertureCout) {
    var container = el('stock-gmroi-list');
    if (!container) return;
    container.innerHTML = '';

    if (!gmroi || gmroi.length === 0) {
        // A15 : le GMROI part du coût d'achat. Sans coût saisi, le bloc
        // restait vide sans qu'on sache pourquoi.
        container.innerHTML = (typeof couvertureCout === 'number' && couvertureCout < 5)
            ? '<div style="text-align:center;color:#94A3B8;padding:10px;">Indisponible : aucun article n’a de coût d’achat renseigné.</div>'
            : '<div style="text-align:center;color:#94A3B8;padding:10px;">Aucune donnée</div>';
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
// ÉCARTS D'INVENTAIRE — RÉFÉRENCES EN STOCK NÉGATIF
//
// Un stock négatif signifie qu'Odoo a enregistré plus de sorties que
// d'entrées pour cette référence dans ce magasin : c'est exactement le cas
// « le magasin a 3 pièces mais en a vendu 4 ». Le détail reconstitue le
// grand livre des mouvements pour montrer d'où viennent les pièces sorties.
// ═══════════════════════════════════════════════════════════
async function openEcarts() {
    if (!lastEcartsRefsCount) return;
    var overlay = el('ecarts-overlay');
    var tbody = el('ecarts-tbody');
    if (!overlay || !tbody) return;

    overlay.classList.add('active');
    tbody.innerHTML = '';
    var loadingRow = document.createElement('tr');
    var loadingCell = document.createElement('td');
    loadingCell.colSpan = 5;
    loadingCell.textContent = 'Analyse des mouvements de stock…';
    loadingCell.style.cssText = 'text-align:center;padding:24px;color:#94A3B8;';
    loadingRow.appendChild(loadingCell);
    tbody.appendChild(loadingRow);

    var data = await rpc('/mavie/api/inventory-anomalies', getFilterParams());
    if (!data || data.error) {
        loadingCell.textContent = 'Erreur : ' + ((data && data.error) || 'inconnue');
        return;
    }
    lastEcartsList = data.anomalies || [];

    var badge = el('ecarts-count-badge');
    if (badge) {
        badge.textContent = formatNumber(data.refs_count || 0) + ' réf. · '
            + formatNumber(Math.abs(data.qty_manquante || 0)) + ' pièces';
    }
    _renderEcartsList();
}

function closeEcarts() {
    var overlay = el('ecarts-overlay');
    if (overlay) overlay.classList.remove('active');
}

function _renderEcartsList(searchFilter) {
    var tbody = el('ecarts-tbody');
    if (!tbody) return;
    tbody.innerHTML = '';

    var list = lastEcartsList;
    if (searchFilter) {
        var q = searchFilter.toLowerCase().trim();
        list = list.filter(function(a) {
            return (a.ref && a.ref.toLowerCase().includes(q))
                || (a.name && a.name.toLowerCase().includes(q))
                || (a.magasin && a.magasin.toLowerCase().includes(q))
                || (a.company && a.company.toLowerCase().includes(q));
        });
    }

    if (!list.length) {
        var tr = document.createElement('tr');
        var td = document.createElement('td');
        td.colSpan = 5;
        td.textContent = searchFilter
            ? 'Aucun écart ne correspond à votre recherche.'
            : 'Aucun écart d\'inventaire détecté sur ce périmètre.';
        td.style.cssText = 'text-align:center;padding:24px;color:#94A3B8;';
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }

    list.forEach(function(a) {
        var tr = document.createElement('tr');
        tr.style.cursor = 'pointer';
        tr.style.borderBottom = '1px solid #F1F5F9';
        tr.title = 'Voir d\'où vient le manque';
        tr.onclick = function() { openEcartDetail(a); };

        function cell(text, color, weight, align) {
            var td = document.createElement('td');
            td.textContent = text;
            td.style.padding = '10px';
            td.style.fontSize = '0.85rem';
            if (color) td.style.color = color;
            if (weight) td.style.fontWeight = weight;
            if (align) td.style.textAlign = align;
            return td;
        }

        tr.appendChild(cell(a.ref || '—', '#64748B', '600'));
        var nomCell = cell(a.name || '—', '#0F172A', '600');
        if (a.archive) {
            nomCell.textContent = (a.name || '—') + ' ';
            var badge = document.createElement('span');
            badge.textContent = 'article archivé';
            badge.style.cssText = 'margin-left:6px;padding:1px 6px;border-radius:4px;background:#E2E8F0;color:#475569;font-size:0.72rem;font-weight:600;';
            nomCell.appendChild(badge);
        }
        tr.appendChild(nomCell);
        tr.appendChild(cell(a.company || '—', '#475569'));
        tr.appendChild(cell(a.magasin || '—', '#475569'));
        tr.appendChild(cell(a.achat_depot ? 'Oui' : 'Non', a.achat_depot ? '#047857' : '#B91C1C', '600'));
        tr.appendChild(cell(formatNumber(a.qty_negative), '#EF4444', '700', 'center'));

        tbody.appendChild(tr);
    });
}

async function openEcartDetail(anomaly) {
    var overlay = el('ecart-detail-overlay');
    var headerEl = el('ecart-detail-header');
    var bodyEl = el('ecart-detail-body');
    if (!overlay || !bodyEl) return;

    overlay.classList.add('active');
    if (headerEl) {
        headerEl.innerHTML = '<strong>' + (anomaly.ref || '') + '</strong> — '
            + (anomaly.name || '') + '<br/>'
            + (anomaly.company || '') + ' · ' + (anomaly.magasin || '');
    }
    bodyEl.innerHTML = '<p style="color:#94A3B8;">Reconstitution des mouvements…</p>';

    var data = await rpc('/mavie/api/inventory-anomaly-detail', {
        article_id: anomaly.id,
        warehouse_id: anomaly.warehouse_id,
    });
    if (!data || data.error) {
        bodyEl.innerHTML = '<p style="color:#EF4444;">Erreur : ' + ((data && data.error) || 'inconnue') + '</p>';
        return;
    }
    bodyEl.innerHTML = _negativesHtml(data.lignes_negatives || [], data.stock_positif || 0, data.stock_reel || 0)
        + _buildEcartDetailHtml(data);
}

// Ouvre le bon dans Odoo, dans un nouvel onglet.
function _lienBon(nom, id) {
    if (!id) return _escapeHtml(nom || '—');
    return '<a href="/web#id=' + id + '&model=stock.picking&view_type=form" target="_blank" rel="noopener" '
        + 'style="color:#1D4ED8;text-decoration:underline;">' + _escapeHtml(nom || '—') + '</a>';
}

// Chaque ligne négative : quand, quoi, où, qui, avec un lien vers chaque mouvement.
function _negativesHtml(lignes, positif, total) {
    if (!lignes.length) return '';
    var negatif = lignes.reduce(function(a, l) { return a + l.quantite; }, 0);
    var h = '<div style="margin-bottom:12px;padding:10px 12px;border-radius:8px;background:#FFF7ED;'
        + 'border:1px solid #FED7AA;color:#7C2D12;font-size:0.86rem;line-height:1.5;">'
        + '<b>Pourquoi un négatif apparaît :</b> une sortie a été enregistrée pour une taille sans stock '
        + 'à ce moment-là. Le total du magasin reste positif grâce aux autres tailles.'
        + '<br/><b>Calcul :</b> tailles en stock ' + formatNumber(positif) + ' − négatifs '
        + formatNumber(Math.abs(negatif)) + ' = ' + formatNumber(total) + ' pièces.</div>';
    lignes.forEach(function(l) {
        h += '<div style="font-weight:700;color:#0F172A;margin:10px 0 4px;">' + _escapeHtml(l.variante)
            + ' · ' + _escapeHtml(l.emplacement) + ' · lot ' + _escapeHtml(l.lot)
            + ' · <span style="color:#EF4444;">' + formatNumber(l.quantite) + '</span></div>';
        var diagnostic = 'Ajustement d’inventaire (aucune sortie retrouvée)';
        if (l.ventes && l.ventes.some(function(v) { return v.origine_saisie; })) {
            diagnostic = 'Sortie saisie à la main (origine saisie, commande de caisse introuvable)';
        } else if (l.ventes && l.ventes.length) {
            diagnostic = 'Vente ou sortie faite sans stock disponible à ce moment';
        }
        h += '<div style="font-size:0.8rem;color:#7C2D12;margin-bottom:4px;"><b>Diagnostic :</b> '
            + _escapeHtml(diagnostic) + '</div>';
        if (!l.ventes || !l.ventes.length) {
            h += '<div style="font-size:0.8rem;color:#94A3B8;margin-bottom:8px;">Aucun mouvement de sortie '
                + 'retrouvé : probablement un ajustement d’inventaire.</div>';
            return;
        }
        h += '<table style="width:100%;border-collapse:collapse;font-size:0.78rem;margin-bottom:8px;">'
            + '<thead><tr style="background:#F8FAFC;text-align:left;">'
            + '<th style="padding:4px;">Date et heure</th><th style="padding:4px;">Document</th>'
            + '<th style="padding:4px;">Magasin</th><th style="padding:4px;text-align:center;">Qté</th>'
            + '<th style="padding:4px;">Caisse</th><th style="padding:4px;">Vendeur</th>'
            + '<th style="padding:4px;">Enregistré par</th><th style="padding:4px;">Odoo</th></tr></thead><tbody>';
        l.ventes.forEach(function(v) {
            h += '<tr style="border-bottom:1px solid #F1F5F9;">'
                + '<td style="padding:4px;">' + _escapeHtml(_trQuand(v.date) || '—') + '</td>'
                + '<td style="padding:4px;">' + _lienBon(v.document, v.picking_id)
                + (v.origine_saisie ? '<br/><span style="color:#94A3B8;">Origine saisie : '
                    + _escapeHtml(v.origine_saisie) + '</span>' : '') + '</td>'
                + '<td style="padding:4px;">' + _escapeHtml(v.magasin) + '</td>'
                + '<td style="padding:4px;text-align:center;">' + formatNumber(v.qty) + '</td>'
                + '<td style="padding:4px;">' + _escapeHtml(v.caisse) + '</td>'
                + '<td style="padding:4px;">' + _escapeHtml(v.vendeur) + '</td>'
                + '<td style="padding:4px;">' + _escapeHtml(v.utilisateur || '—') + '</td>'
                + '<td style="padding:4px;"><a href="/web#id=' + v.move_line_id
                + '&model=stock.move.line&view_type=form" target="_blank" rel="noopener" '
                + 'style="color:#1D4ED8;">Mouvement</a></td></tr>';
        });
        h += '</tbody></table>';
    });
    return h;
}

function closeEcartDetail() {
    var overlay = el('ecart-detail-overlay');
    if (overlay) overlay.classList.remove('active');
}

function _escapeHtml(text) {
    var div = document.createElement('div');
    div.textContent = text === null || text === undefined ? '' : String(text);
    return div.innerHTML;
}

function _ledgerTable(title, buckets, color) {
    if (!buckets || !buckets.length) {
        return '<div style="margin-bottom:14px;"><div style="font-weight:700;color:#0F172A;margin-bottom:6px;">'
            + title + '</div><div style="color:#94A3B8;font-size:0.85rem;">Aucun mouvement enregistré.</div></div>';
    }
    var html = '<div style="margin-bottom:14px;"><div style="font-weight:700;color:#0F172A;margin-bottom:6px;">'
        + title + '</div>'
        + '<table style="width:100%;border-collapse:collapse;font-size:0.85rem;">'
        + '<thead><tr style="background:#F8FAFC;text-align:left;">'
        + '<th style="padding:8px;">Nature</th>'
        + '<th style="padding:8px;text-align:center;">Pièces</th>'
        + '<th style="padding:8px;text-align:center;">Mouvements</th>'
        + '<th style="padding:8px;">Derniers documents</th>'
        + '</tr></thead><tbody>';
    buckets.forEach(function(b) {
        var examples = (b.exemples || []).map(function(e) {
            return _escapeHtml(e.document) + ' (' + formatNumber(e.qty) + ' — ' + _escapeHtml(e.date) + ')';
        }).join('<br/>');
        html += '<tr style="border-bottom:1px solid #F1F5F9;">'
            + '<td style="padding:8px;font-weight:600;">' + _escapeHtml(b.label) + '</td>'
            + '<td style="padding:8px;text-align:center;font-weight:700;color:' + color + ';">'
                + formatNumber(b.qty) + '</td>'
            + '<td style="padding:8px;text-align:center;color:#64748B;">' + formatNumber(b.nb_mouvements) + '</td>'
            + '<td style="padding:8px;color:#64748B;font-size:0.78rem;">' + (examples || '—') + '</td>'
            + '</tr>';
    });
    return html + '</tbody></table></div>';
}

function _buildEcartDetailHtml(data) {
    var manque = data.stock_reel;
    var html = '';

    // Synthèse en une phrase : c'est la question posée — d'où vient le
    // produit vendu en trop.
    html += '<div style="background:#FEF2F2;border:1px solid #FECACA;border-radius:8px;padding:12px;margin-bottom:16px;font-size:0.88rem;color:#7F1D1D;line-height:1.6;">'
        + '<strong>Stock actuel dans ce magasin : ' + formatNumber(manque) + '</strong><br/>'
        + 'Entrées enregistrées : ' + formatNumber(data.total_entrees) + ' · '
        + 'Sorties enregistrées : ' + formatNumber(data.total_sorties) + ' · '
        + 'Stock théorique (entrées − sorties) : ' + formatNumber(data.stock_theorique) + '<br/>'
        + 'Vendu en caisse : <strong>' + formatNumber(data.qty_vendue_caisse) + '</strong> '
        + '(dont ' + formatNumber(data.qty_vendue_solde) + ' en solde et '
        + formatNumber(data.qty_vendue_normale) + ' au prix catalogue).'
        + '</div>';

    if (data.ecart_non_explique) {
        html += '<div style="background:#FFFBEB;border:1px solid #FEF3C7;border-radius:8px;padding:12px;margin-bottom:16px;font-size:0.85rem;color:#92400E;line-height:1.6;">'
            + '⚠️ ' + formatNumber(Math.abs(data.ecart_non_explique)) + ' pièces d\'écart ne sont expliquées par '
            + 'AUCUN mouvement de stock : le stock a été écrit directement (import de données, correction '
            + 'manuelle en base, reprise d\'historique) sans passer par une réception, un transfert ou un '
            + 'ajustement. C\'est la cause à corriger en priorité.'
            + '</div>';
    } else {
        html += '<div style="background:#F0FDF4;border:1px solid #BBF7D0;border-radius:8px;padding:12px;margin-bottom:16px;font-size:0.85rem;color:#166534;line-height:1.6;">'
            + '✔️ Tous les mouvements se recoupent : le stock négatif vient bien des sorties listées '
            + 'ci-dessous, pas d\'une donnée écrite hors mouvement. Le manque provient donc de sorties '
            + 'effectuées alors que la marchandise n\'avait jamais été réceptionnée dans ce magasin.'
            + '</div>';
    }

    html += _ledgerTable('📥 D\'où sont venues les pièces (entrées)', data.entrees, '#10B981');
    html += _ledgerTable('📤 Où sont parties les pièces (sorties)', data.sorties, '#EF4444');

    var transferts = data.transferts || [];
    if (transferts.length) {
        html += '<div style="margin-bottom:8px;"><div style="font-weight:700;color:#0F172A;margin-bottom:6px;">'
            + '🔄 Bons de transfert impliquant ce magasin pour cette référence</div>'
            + '<table style="width:100%;border-collapse:collapse;font-size:0.85rem;">'
            + '<thead><tr style="background:#F8FAFC;text-align:left;">'
            + '<th style="padding:8px;">Bon</th><th style="padding:8px;">Date</th>'
            + '<th style="padding:8px;">Sens</th><th style="padding:8px;">Source → Cible</th>'
            + '<th style="padding:8px;text-align:center;">Qté</th><th style="padding:8px;">État</th>'
            + '</tr></thead><tbody>';
        transferts.forEach(function(t) {
            html += '<tr style="border-bottom:1px solid #F1F5F9;">'
                + '<td style="padding:8px;font-weight:600;">' + _escapeHtml(t.name) + '</td>'
                + '<td style="padding:8px;color:#64748B;">' + _escapeHtml(t.date) + '</td>'
                + '<td style="padding:8px;">' + _escapeHtml(t.sens) + '</td>'
                + '<td style="padding:8px;color:#64748B;font-size:0.78rem;">'
                    + _escapeHtml(t.source) + ' → ' + _escapeHtml(t.cible) + '</td>'
                + '<td style="padding:8px;text-align:center;font-weight:700;">' + formatNumber(t.qty) + '</td>'
                + '<td style="padding:8px;color:#64748B;">' + _escapeHtml(t.state) + '</td>'
                + '</tr>';
        });
        html += '</tbody></table></div>';
    }

    return html;
}

// ═══════════════════════════════════════════════════════════
// RÉCONCILIATION DU STOCK — pop-up de la carte "Stock Réel Odoo"
//
// DEMANDE UTILISATEUR : « je ne dois pas avoir d'écarts ». Le pop-up montre
// comment on passe des achats reçus au stock réel, poste par poste, chaque
// ligne ayant un nom et des documents derrière. Ce qui reste en bas (l'écart)
// n'est plus un fourre-tout : c'est ce qu'AUCUN mouvement n'explique.
// ═══════════════════════════════════════════════════════════
async function openStockRecon() {
    var overlay = el('stock-recon-overlay');
    var headerEl = el('stock-recon-header');
    var bodyEl = el('stock-recon-body');
    if (!overlay || !bodyEl) return;
    if (!state.detail.article_id) return;

    overlay.classList.add('active');
    var nameEl = el('detail-name');
    if (headerEl) {
        headerEl.innerHTML = '<strong>' + _escapeHtml(nameEl ? nameEl.textContent : '') + '</strong>';
    }
    // Les totaux sont déjà connus (livrés avec la fiche) : on les affiche
    // immédiatement, les documents arrivent juste après.
    bodyEl.innerHTML = _buildReconTableHtml(state.detail.reconciliation)
        + '<p style="color:#94A3B8;">Chargement des documents…</p>';

    // EXACTEMENT les mêmes paramètres que /mavie/api/product-detail : le
    // périmètre du pop-up doit être celui de la carte cliquée, sinon les
    // documents listés ne totalisent pas les lignes affichées au-dessus.
    var data = await rpc('/mavie/api/product-stock-detail', {
        article_id: state.detail.article_id,
        shop_field: state.detail.shop_field,
        batch_id: state.batch_id,
        collection_id: state.collection_id,
    });

    if (!data || data.error) {
        bodyEl.innerHTML = _buildReconTableHtml(state.detail.reconciliation)
            + '<p style="color:#EF4444;">Documents indisponibles : '
            + _escapeHtml((data && data.error) || 'erreur inconnue') + '</p>';
        return;
    }
    if (headerEl) {
        headerEl.innerHTML = '<strong>' + _escapeHtml(data.ref || '') + '</strong> — '
            + _escapeHtml(data.name || '') + '<br/>'
            + '<span style="color:#64748B;">Périmètre : ' + _escapeHtml(data.perimetre || '—') + '</span>';
    }
    bodyEl.innerHTML = _buildReconTableHtml(state.detail.reconciliation)
        + _buildReconDocsHtml(data);
}

function closeStockRecon() {
    var overlay = el('stock-recon-overlay');
    if (overlay) overlay.classList.remove('active');
}

function _reconRow(label, value, hint, isTotal) {
    if (value === null || value === undefined) return '';
    var sign = value > 0 ? '+' : (value < 0 ? '−' : '');
    var color = value > 0 ? '#059669' : (value < 0 ? '#DC2626' : '#64748B');
    return '<tr' + (isTotal ? ' class="recon-total"' : '') + '>'
        + '<td>' + _escapeHtml(label)
        + (hint ? '<div class="recon-sub">' + _escapeHtml(hint) + '</div>' : '')
        + '</td>'
        + '<td class="num" style="color:' + color + ';">'
        + sign + formatNumber(Math.abs(value)) + '</td></tr>';
}

// Le tableau est volontairement coupé en DEUX blocs.
//
// Bloc 1 = le stock expliqué par les seuls MOUVEMENTS de stock. Chaque ligne
// y est une somme brute d'un type de mouvement, jamais un reste calculé :
// c'est pour ça que le total retombe toujours sur le stock réel dès que la
// traçabilité est complète, et que l'écart du bas ne peut plus être qu'une
// vraie anomalie.
//
// Bloc 2 = pourquoi les cartes du haut de fiche (« Qté achetée », « Qté
// vendue »), qui viennent des DOCUMENTS, n'affichent pas le même chiffre que
// les mouvements. Mélanger les deux dans un seul tableau donnait des lignes
// qui ressemblaient à des erreurs de calcul alors qu'elles comparaient deux
// sources différentes.
function _buildReconTableHtml(r) {
    if (!r) {
        return '<p style="color:#94A3B8;">Réconciliation indisponible pour cette fiche.</p>';
    }
    var html = '<div style="font-weight:800;color:#0F172A;margin:0 0 6px;">'
        + '1️⃣ Le stock expliqué par les mouvements de stock</div>'
        + '<div class="recon-sub" style="margin-bottom:8px;">'
        + 'Uniquement des mouvements validés dans Odoo. Aucun chiffre déduit.</div>'
        + '<table class="recon-table">';

    html += _reconRow('Réceptions fournisseur', r.recept_fournisseur,
        'Marchandise entrée dans ces magasins depuis un fournisseur.');
    if (r.retour_fournisseur) {
        html += _reconRow('Retours au fournisseur', -r.retour_fournisseur,
            'Marchandise renvoyée au fournisseur (le plus souvent le dépôt).');
    }
    if (r.retour_client) {
        html += _reconRow('Retours clients', r.retour_client,
            'Marchandise rendue par un client et remise en stock.');
    }
    html += _reconRow('Sorties vers les clients', -r.sortie_client,
        'Ventes en caisse et livraisons : tout ce qui est parti chez un client.');
    if (r.inventaire_gain) {
        html += _reconRow('Gains d\'inventaire', r.inventaire_gain,
            'Pièces retrouvées lors d\'un comptage.');
    }
    if (r.inventaire_perte) {
        html += _reconRow('Pertes d\'inventaire', -r.inventaire_perte,
            'Pièces manquantes constatées lors d\'un comptage (casse, vol, erreur de saisie).');
    }
    if (r.autres_in) {
        html += _reconRow('Autres entrées', r.autres_in,
            'Transit inter-sociétés, production, entrepôt hors périmètre affiché.');
    }
    if (r.autres_out) {
        html += _reconRow('Autres sorties', -r.autres_out,
            'Transferts vers un entrepôt qui n\'est pas dans le périmètre affiché.');
    }
    html += _reconRow('= Stock attendu', r.stock_attendu, null, true);
    html += '<tr><td>Stock comptable Odoo'
        + '<div class="recon-sub">Somme de tous les emplacements, stocks négatifs compris.</div></td>'
        + '<td class="num">' + formatNumber(r.stock_reel) + '</td></tr>';

    var ecart = r.ecart || 0;
    html += '<tr class="recon-total"><td>Écart inexpliqué</td>'
        + '<td class="num" style="color:' + (ecart ? '#DC2626' : '#059669') + ';">'
        + (ecart ? formatNumber(ecart) : '0 ✅') + '</td></tr>';

    html += '</table>';

    if (ecart) {
        // Message volontairement écrit sans jargon : le lecteur n'a pas à
        // savoir ce qu'est un quant ni un stock.move.line. On dit ce qui
        // cloche, avec les deux chiffres, puis quoi faire.
        html += '<div style="background:#FFFBEB;border:1px solid #FEF3C7;border-radius:8px;padding:12px;margin:14px 0;font-size:0.85rem;color:#92400E;line-height:1.7;">'
            + '<strong>⚠️ ' + formatNumber(Math.abs(ecart)) + ' pièce(s) ne s\'expliquent pas.</strong><br/>'
            + 'En additionnant toutes les entrées et toutes les sorties enregistrées, on devrait avoir '
            + '<strong>' + formatNumber(r.stock_attendu) + '</strong> en stock. Odoo en affiche '
            + '<strong>' + formatNumber(r.stock_reel) + '</strong>. '
            + (ecart < 0
                ? 'Il y a donc ' + formatNumber(Math.abs(ecart)) + ' pièce(s) de trop, arrivées sans réception, sans retour et sans inventaire.'
                : 'Il manque donc ' + formatNumber(ecart) + ' pièce(s), parties sans vente, sans transfert et sans inventaire.')
            + '<br/><em>Que faire :</em> lancer un inventaire dans Odoo sur les magasins concernés. '
            + 'Le comptage créera le mouvement d\'ajustement qui manque, et cette ligne repassera à 0. '
            + 'Toutes les autres lignes du tableau, elles, ont déjà un document derrière (bon de commande, '
            + 'ticket de caisse, bon de retour, ajustement).'
            + '</div>';
    } else if (r.stock_reel < 0) {
        // Écart nul mais stock négatif : tout est tracé, et pourtant le
        // résultat est physiquement impossible. Un « ✅ tout va bien » vert
        // serait trompeur ici — la cause type est un transfert entre
        // magasins fait dans la réalité mais jamais enregistré dans Odoo :
        // le magasin qui a reçu la marchandise vend un stock qu'il n'a
        // jamais reçu, et passe en négatif.
        html += '<div style="background:#FFFBEB;border:1px solid #FEF3C7;border-radius:8px;padding:12px;margin:14px 0;font-size:0.85rem;color:#92400E;line-height:1.7;">'
            + '<strong>⚠️ Stock négatif : ' + formatNumber(r.stock_reel) + '.</strong><br/>'
            + 'Tous les mouvements se recoupent (aucune pièce inexpliquée), mais le résultat est '
            + 'impossible : il est sorti plus de marchandise de ces magasins qu\'il n\'y en est jamais entré. '
            + 'Cause la plus fréquente : de la marchandise déplacée d\'un magasin à l\'autre sans que le '
            + 'transfert ait été enregistré dans Odoo — le magasin qui l\'a reçue la vend, et passe en négatif.'
            + '<br/><em>Que faire :</em> repérer les magasins en négatif dans le tableau « Stock par magasin », '
            + 'puis soit enregistrer le transfert manquant, soit lancer un inventaire pour remettre les compteurs à plat.'
            + '</div>';
    } else {
        html += '<div style="background:#F0FDF4;border:1px solid #BBF7D0;border-radius:8px;padding:12px;margin:14px 0;font-size:0.85rem;color:#166534;line-height:1.6;">'
            + '✅ Aucun écart : chaque pièce du stock réel est justifiée par un mouvement enregistré.'
            + '</div>';
    }

    // Passage du stock comptable au stock affiché sur la carte. C'était
    // auparavant deux lignes ajoutées SOUS le total du tableau : on les
    // lisait comme la suite de la soustraction, alors que ce sont les deux
    // morceaux du total. Une phrase avec le calcul explicite lève
    // l'ambiguïté sans rajouter de chiffres à additionner.
    if (r.nb_magasins_negatifs) {
        html += '<div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:8px;padding:12px;margin:14px 0;font-size:0.85rem;color:#334155;line-height:1.7;">'
            + '📦 Ces <strong>' + formatNumber(r.stock_reel) + '</strong> pièces comptables se répartissent en '
            + '<strong>' + formatNumber(r.stock_present) + '</strong> réellement en rayon et '
            + '<strong>' + formatNumber(r.stock_negatif) + '</strong> dans '
            + formatNumber(r.nb_magasins_negatifs) + ' magasin(s) passés en stock négatif '
            + '(' + formatNumber(r.stock_present) + ' − ' + formatNumber(Math.abs(r.stock_negatif))
            + ' = ' + formatNumber(r.stock_reel) + ').<br/>'
            + 'Un stock négatif n\'est pas de la marchandise : la carte « Stock en magasin » affiche donc '
            + '<strong>' + formatNumber(r.stock_present) + '</strong>.'
            + '</div>';
    }

    // ── Bloc 2 : documents vs mouvements ──
    html += '<div style="font-weight:800;color:#0F172A;margin:18px 0 6px;">'
        + '2️⃣ Pourquoi les cartes du haut affichent d\'autres chiffres</div>'
        + '<div class="recon-sub" style="margin-bottom:8px;">'
        + 'Les cartes comptent des DOCUMENTS (bons de commande, tickets de caisse). Le bloc 1 compte '
        + 'des MOUVEMENTS de marchandise. Voici l\'écart entre les deux — il ne change pas le stock.</div>'
        + '<table class="recon-table">';

    html += '<tr><td>Qté achetée affichée <span class="recon-sub">(bons de commande, quantité reçue)</span></td>'
        + '<td class="num">' + formatNumber(r.qty_purchased_doc) + '</td></tr>';
    html += '<tr><td>Réceptions réellement entrées ici <span class="recon-sub">(nettes des retours fournisseur)</span></td>'
        + '<td class="num">' + formatNumber(r.recu_mouvements) + '</td></tr>';
    html += '<tr><td><em>' + (r.reception_hors_bon < 0
            ? 'Différence : reçu ailleurs qu\'ici'
            : 'Différence : entré ici sans bon de commande') + '</em>'
        + '<div class="recon-sub">' + (r.reception_hors_bon < 0
            ? 'Bons de commande de ces sociétés réceptionnés dans un entrepôt non listé comme magasin actif (DIGITAL SHOP, entrepôt désactivé), ou sur une couleur aujourd\'hui désactivée.'
            : 'Marchandise entrée en stock sans ligne de bon de commande rattachée (reprise d\'historique, saisie manuelle).') + '</div></td>'
        + '<td class="num" style="color:#B45309;">' + formatNumber(Math.abs(r.reception_hors_bon)) + '</td></tr>';

    html += '<tr><td style="padding-top:16px;">Qté vendue affichée <span class="recon-sub">(caisse + bons de vente livrés)</span></td>'
        + '<td class="num" style="padding-top:16px;">' + formatNumber(r.qty_sold_doc) + '</td></tr>';
    html += '<tr><td>Sorties clients réellement constatées ici <span class="recon-sub">(nettes des retours clients)</span></td>'
        + '<td class="num">' + formatNumber(r.sorties_client_mvt) + '</td></tr>';
    html += '<tr><td><em>' + (r.sortie_hors_vente < 0
            ? 'Différence : vendu depuis un autre entrepôt'
            : 'Différence : sorti sans ligne de vente') + '</em>'
        + '<div class="recon-sub">' + (r.sortie_hors_vente < 0
            ? 'Ventes comptées dans la carte mais sorties d\'un entrepôt hors périmètre, ou sur une couleur aujourd\'hui désactivée.'
            : 'Marchandise partie chez un client sans ticket de caisse ni bon de vente en face : bon de livraison, transfert vers un autre magasin, ou quantité livrée différente de la quantité vendue.') + '</div></td>'
        + '<td class="num" style="color:#B45309;">' + formatNumber(Math.abs(r.sortie_hors_vente)) + '</td></tr>';
    html += '</table>';

    return html;
}

function _reconSection(title, columns, rows, cellFn, emptyMsg) {
    var html = '<div style="margin-bottom:16px;">'
        + '<div style="font-weight:700;color:#0F172A;margin-bottom:6px;">' + title + '</div>';
    if (!rows || !rows.length) {
        return html + '<div style="color:#94A3B8;font-size:0.85rem;">' + emptyMsg + '</div></div>';
    }
    html += '<table style="width:100%;border-collapse:collapse;font-size:0.85rem;">'
        + '<thead><tr style="background:#F8FAFC;text-align:left;">';
    columns.forEach(function(c) {
        html += '<th style="padding:8px;' + (c.num ? 'text-align:right;' : '') + '">' + c.label + '</th>';
    });
    html += '</tr></thead><tbody>';
    rows.forEach(function(r) {
        html += '<tr style="border-bottom:1px solid #F1F5F9;">' + cellFn(r) + '</tr>';
    });
    return html + '</tbody></table></div>';
}

function _td(value, opts) {
    opts = opts || {};
    return '<td style="padding:8px;'
        + (opts.num ? 'text-align:right;font-weight:700;white-space:nowrap;' : '')
        + (opts.color ? 'color:' + opts.color + ';' : '')
        + (opts.muted ? 'color:#64748B;' : '')
        + '">' + _escapeHtml(value === null || value === undefined ? '—' : value) + '</td>';
}

function _buildReconDocsHtml(data) {
    var html = '';

    html += _reconSection('📥 Bons de commande d\'achat',
        [{label: 'Bon'}, {label: 'Date'}, {label: 'Fournisseur'},
         {label: 'Commandé', num: true}, {label: 'Reçu', num: true}, {label: 'Non reçu', num: true}],
        data.achats,
        function(r) {
            return _td(r.bon) + _td(r.date, {muted: true}) + _td(r.fournisseur, {muted: true})
                + _td(formatNumber(r.commande), {num: true})
                + _td(formatNumber(r.recu), {num: true})
                + _td(r.ecart ? formatNumber(r.ecart) : '—', {num: true, color: r.ecart ? '#DC2626' : '#94A3B8'});
        },
        'Aucun bon de commande sur ce périmètre.');

    if (data.achats_total > (data.achats || []).length) {
        html += '<div class="recon-sub" style="margin:-10px 0 14px;">'
            + (data.achats_total - data.achats.length) + ' bon(s) supplémentaire(s) non listé(s) — export Excel pour la liste complète.</div>';
    }

    html += _reconSection('↩️ Retours au fournisseur',
        [{label: 'Bon de retour'}, {label: 'Réception d\'origine'}, {label: 'Date'}, {label: 'Pièces', num: true}],
        data.retours_fournisseur,
        function(r) {
            return _td(r.bon) + _td(r.origine, {muted: true}) + _td(r.date, {muted: true})
                + _td(formatNumber(r.qty), {num: true, color: '#DC2626'});
        },
        'Aucun retour fournisseur — les achats reçus valent donc les achats commandés.');

    html += _reconSection('📤 Sorties vers les clients, par type de document',
        [{label: 'Type de document'}, {label: 'Sorties', num: true},
         {label: 'Retours', num: true}, {label: 'Net', num: true}],
        data.ventes_documents,
        function(r) {
            return _td(r.type_document)
                + _td(formatNumber(r.sortie), {num: true})
                + _td(formatNumber(r.retour), {num: true, color: '#059669'})
                + _td(formatNumber(r.net), {num: true});
        },
        'Aucune sortie client enregistrée.');

    html += _reconSection('↩️ Retours sur bon de vente',
        [{label: 'Bon de retour'}, {label: 'Livraison'}, {label: 'Bon de vente'},
         {label: 'Date'}, {label: 'Pièces', num: true}],
        data.retours_vente,
        function(r) {
            return _td(r.bon) + _td(r.livraison, {muted: true}) + _td(r.bon_vente, {muted: true})
                + _td(r.date, {muted: true})
                + _td(formatNumber(r.qty), {num: true, color: '#059669'});
        },
        'Aucun retour sur bon de vente.');

    // DEMANDE UTILISATEUR (2026-09-07) : le tableau « Ajustements
    // d'inventaire, par mois » a été retiré du pop-up. Les pertes et les
    // gains restent affichés en haut, dans le bloc 1 — le détail mensuel
    // n'apportait rien de plus et alourdissait la lecture.

    return html;
}

// ═══════════════════════════════════════════════════════════
// HISTORIQUE — TRANSFERTS & SOLDES
// ═══════════════════════════════════════════════════════════
async function loadHistory() {
    var section = el('history-section');
    if (!section) return;
    var seq = ++_reqSeq.history;
    var data = await rpc('/mavie/api/history', getFilterParams());
    if (seq !== _reqSeq.history) return;
    if (!data || data.error) {
        lastHistory = { transfers: [], soldes: [] };
        var summaryErr = el('history-summary');
        if (summaryErr) summaryErr.textContent = 'Erreur : ' + ((data && data.error) || 'inconnue');
        return;
    }
    lastHistory = data;
    _renderHistory();
}

function setHistoryTab(tab) {
    historyTab = tab;
    var tabTransferts = el('history-tab-transferts');
    var tabSoldes = el('history-tab-soldes');
    if (tabTransferts) tabTransferts.classList.toggle('active', tab === 'transferts');
    if (tabSoldes) tabSoldes.classList.toggle('active', tab === 'soldes');
    _renderHistory();
}

function _renderHistory() {
    var theadRow = el('history-thead-row');
    var tbody = el('history-tbody');
    var summaryEl = el('history-summary');
    if (!theadRow || !tbody) return;

    theadRow.innerHTML = '';
    tbody.innerHTML = '';

    // Même ordre que le pop-up historique d'une référence : catalogue,
    // remise, prix payé. "CA encaissé" retiré de l'écran — le total reste
    // dans le résumé au-dessus et dans l'export CSV.
    var columns = historyTab === 'soldes'
        ? ['Date', 'Ticket', 'Magasin', 'Réf', 'Produit', 'Qté',
           'Prix catalogue (TTC)', 'Remise', 'Prix payé (TTC)']
        : ['Bon', 'Date', 'État', 'Opération', 'Société source', 'Magasin source',
           'Société cible', 'Magasin cible', 'Type', 'Réf.', 'Qté', 'Notification'];
    columns.forEach(function(label) {
        var th = document.createElement('th');
        th.textContent = label;
        th.style.cssText = 'padding:10px 8px;font-size:0.72rem;font-weight:700;color:#64748B;text-transform:uppercase;';
        theadRow.appendChild(th);
    });

    var rows = historyTab === 'soldes' ? (lastHistory.soldes || []) : (lastHistory.transfers || []);

    if (summaryEl) {
        summaryEl.textContent = historyTab === 'soldes'
            // A11 : le total porte sur toutes les ventes soldées ; le
            // tableau n'en montre que les plus récentes, on le dit.
            ? formatNumber(lastHistory.soldes_count || 0) + ' lignes vendues sous le prix catalogue · '
              + formatNumber(lastHistory.soldes_qty || 0) + ' pièces · '
              + formatMAD(lastHistory.soldes_ca || 0) + ' encaissés'
              + (lastHistory.soldes_tronque
                  ? ' — les ' + formatNumber(lastHistory.soldes_affiches || 0) + ' plus récentes sont affichées'
                  : '')
            : formatNumber(lastHistory.transfers_count || 0) + ' bons de transfert · '
              + formatNumber(lastHistory.transfers_qty || 0) + ' pièces déplacées';
    }

    if (!rows.length) {
        var tr = document.createElement('tr');
        var td = document.createElement('td');
        td.colSpan = columns.length;
        // L'historique ne liste QUE les transferts lancés depuis le
        // dashboard : il démarre donc vide, sans reprendre les bons créés
        // auparavant par le formulaire Odoo. On l'explique plutôt que de
        // laisser croire à un filtre trop restrictif ou à un bug.
        td.textContent = historyTab === 'soldes'
            ? 'Aucune vente en solde sur ce périmètre.'
            : 'Aucun transfert sur ce périmètre. '
              + 'Les bons créés ici apparaîtront dans cette liste ; '
              + 'les transferts antérieurs restent consultables dans Odoo (Transferts internes).';
        td.style.cssText = 'text-align:center;padding:24px;color:#94A3B8;';
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }

    function cell(text, color, weight, align) {
        var td = document.createElement('td');
        td.textContent = text;
        td.style.padding = '9px 8px';
        td.style.fontSize = '0.84rem';
        if (color) td.style.color = color;
        if (weight) td.style.fontWeight = weight;
        if (align) td.style.textAlign = align;
        return td;
    }

    rows.forEach(function(r) {
        var tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid #F1F5F9';

        if (historyTab === 'soldes') {
            tr.style.cursor = 'pointer';
            tr.onclick = function() { openDetail(r.id, r.name); };
            tr.appendChild(cell(r.date, '#64748B'));
            tr.appendChild(cell(r.ticket, '#64748B'));
            tr.appendChild(cell(r.magasin));
            tr.appendChild(cell(r.ref, '#64748B', '600'));
            tr.appendChild(cell(r.name, '#0F172A', '600'));
            tr.appendChild(cell(formatNumber(r.qty), null, '700', 'center'));
            tr.appendChild(cell(formatMAD(r.prix_catalogue), '#64748B', null, 'right'));
            tr.appendChild(cell('-' + (r.remise_pct || 0).toFixed(1).replace('.', ',') + ' %',
                                '#DC2626', '700', 'center'));
            tr.appendChild(cell(formatMAD(r.prix_paye), '#0F172A', '600', 'right'));
        } else {
            var stateColor = r.state === 'done' ? '#10B981'
                : (r.state === 'transmitted' ? '#2563EB'
                : (r.state === 'submitted' ? '#F59E0B' : '#94A3B8'));
            tr.appendChild(cell(r.name, '#0F172A', '700'));
            tr.appendChild(cell(r.date, '#64748B'));
            tr.appendChild(cell(r.state_label, stateColor, '600'));
            // Opération d'inventaire à collecter (transfert intra-société) :
            // c'est le document que le responsable ouvre pour valider, donc
            // on ouvre directement le bon dans Odoo. Vide pour un transfert
            // inter-sociétés, qui reste dans le module Transferts.
            var opCell = cell(r.picking_name || '—',
                              r.picking_name ? '#2563EB' : '#94A3B8',
                              r.picking_name ? '600' : null);
            if (r.picking_id) {
                opCell.textContent = '';
                var lien = document.createElement('a');
                lien.textContent = r.picking_name;
                lien.href = '/web#id=' + r.picking_id + '&model=stock.picking&view_type=form';
                lien.target = '_blank';
                lien.title = 'Ouvrir l\'opération dans Inventaire → Transferts';
                lien.style.cssText = 'color:#2563EB;font-weight:600;text-decoration:none;';
                opCell.appendChild(lien);
            }
            tr.appendChild(opCell);
            tr.appendChild(cell(r.source_societe, '#475569'));
            tr.appendChild(cell(r.source_magasin, '#0F172A', '600'));
            tr.appendChild(cell(r.dest_societe, '#475569'));
            tr.appendChild(cell(r.dest_magasin, '#0F172A', '600'));
            // Un transfert intra-société ne passe pas par le circuit
            // inter-sociétés (avoir + commande d'achat) : on le distingue.
            tr.appendChild(cell(r.intra_societe ? 'Intra-société' : 'Inter-sociétés',
                                r.intra_societe ? '#7C3AED' : '#64748B'));
            tr.appendChild(cell(formatNumber(r.nb_references), null, null, 'center'));
            tr.appendChild(cell(formatNumber(r.qty), null, '700', 'center'));
            tr.appendChild(_notifTestCell(r));
        }

        tbody.appendChild(tr);
    });
}

// Bouton « Test » de l'historique (demande utilisatrice 2026-09-23) :
// renvoie la notification de CE bon à soi-même, avec un bandeau rouge
// « ceci est un test », sans déranger les responsables de magasin.
function _notifTestCell(r) {
    var td = document.createElement('td');
    td.style.cssText = 'padding:9px 8px;';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn-transfer-open';
    btn.style.cssText = 'padding:3px 10px;font-size:0.75rem;margin:0;';
    btn.textContent = '✉️ Me l\'envoyer en test';
    btn.title = 'Vous envoie cette notification à vous seule, en test.';
    btn.addEventListener('click', async function(ev) {
        ev.stopPropagation();
        btn.disabled = true;
        btn.textContent = 'Envoi…';
        var res = await rpc('/mavie/api/transfer-notif-test', { transfer_id: r.id });
        btn.disabled = false;
        btn.textContent = '✉️ Me l\'envoyer en test';
        if (!res || res.error) {
            window.alert('Erreur : ' + ((res && res.error) || 'inconnue'));
            return;
        }
        window.alert('Envoyé à ' + res.destinataire + ' : ' + res.messages
            + ' message' + (res.messages > 1 ? 's' : '') + '.\n'
            + 'Regardez la cloche dans Odoo.\nLes magasins n\'ont rien reçu.');
    });
    td.appendChild(btn);
    return td;
}

function _buildExportParams(extra) {
    var filterParams = getFilterParams();
    var params = new URLSearchParams();
    for (var k in filterParams) {
        if (filterParams[k] !== null && filterParams[k] !== undefined && filterParams[k] !== '') {
            params.set(k, filterParams[k]);
        }
    }
    for (var key in (extra || {})) params.set(key, extra[key]);
    return params;
}
// ═══════════════════════════════════════════════════════════
// HISTORIQUE D'UNE RÉFÉRENCE — transferts & soldes
//
// Ouvert depuis la fiche produit. Montre TOUS les transferts de la
// référence, y compris ceux créés hors dashboard — contrairement à la
// section Historique du tableau de bord, volontairement limitée aux bons
// lancés depuis le dashboard.
// ═══════════════════════════════════════════════════════════
var productHistorySubTab = 'all'; // 'all', 'lances', 'remises'

async function openProductHistory() {
    if (!state.detail.article_id) return;
    var overlay = el('product-history-overlay');
    if (!overlay) return;

    productHistoryTab = 'transferts';
    productHistorySubTab = 'all';
    _syncProductHistoryTabs();
    overlay.classList.add('active');

    var refEl = el('product-history-ref');
    if (refEl) refEl.textContent = '…';
    var summaryEl = el('product-history-summary');
    if (summaryEl) summaryEl.textContent = 'Chargement de l\'historique…';
    var tbody = el('product-history-tbody');
    if (tbody) tbody.innerHTML = '';

    var seq = ++_reqSeq.productHistory;
    var data = await rpc('/mavie/api/product-history', { article_id: state.detail.article_id });
    if (seq !== _reqSeq.productHistory) return;

    if (!data || data.error) {
        if (summaryEl) summaryEl.textContent = 'Erreur : ' + ((data && data.error) || 'inconnue');
        return;
    }
    lastProductHistory = data;
    if (refEl) refEl.textContent = data.ref || '';
    _renderProductHistory();
}

function closeProductHistory() {
    var overlay = el('product-history-overlay');
    if (overlay) overlay.classList.remove('active');
}

function _syncProductHistoryTabs() {
    var tabT = el('product-history-tab-transferts');
    var tabS = el('product-history-tab-soldes');
    if (tabT) tabT.classList.toggle('active', productHistoryTab === 'transferts');
    if (tabS) tabS.classList.toggle('active', productHistoryTab === 'soldes');

    var subtabsContainer = el('product-history-soldes-subtabs');
    if (subtabsContainer) {
        subtabsContainer.style.display = (productHistoryTab === 'soldes') ? 'flex' : 'none';
    }

    var subAll = el('product-history-subtab-all');
    var subLances = el('product-history-subtab-lances');
    var subRemises = el('product-history-subtab-remises');
    if (subAll) subAll.classList.toggle('active', productHistorySubTab === 'all');
    if (subLances) subLances.classList.toggle('active', productHistorySubTab === 'lances');
    if (subRemises) subRemises.classList.toggle('active', productHistorySubTab === 'remises');
}

function setProductHistoryTab(tab) {
    productHistoryTab = tab;
    _syncProductHistoryTabs();
    _renderProductHistory();
}

function setProductHistorySubTab(subTab) {
    productHistorySubTab = subTab;
    _syncProductHistoryTabs();
    _renderProductHistory();
}

function _renderProductHistory() {
    var theadRow = el('product-history-thead-row');
    var tbody = el('product-history-tbody');
    var summaryEl = el('product-history-summary');
    if (!theadRow || !tbody) return;

    theadRow.innerHTML = '';
    tbody.innerHTML = '';

    var isSoldes = productHistoryTab === 'soldes';
    var columns = isSoldes
        ? ['Date', 'Ticket', 'Magasin', 'Couleur', 'Taille', 'Qté',
           'Prix catalogue (TTC)', 'Remise', 'Prix payé (TTC)']
        : ['Bon', 'Date', 'État', 'Origine', 'Magasin source', 'Magasin cible',
           'Couleur', 'Taille', 'Qté'];
    columns.forEach(function(label) {
        var th = document.createElement('th');
        th.textContent = label;
        th.style.cssText = 'padding:10px 8px;font-size:0.72rem;font-weight:700;'
            + 'color:#475569;text-transform:uppercase;';
        theadRow.appendChild(th);
    });

    var data = lastProductHistory || {};
    var rawRows = isSoldes ? (data.soldes || []) : (data.transfers || []);
    var rows = rawRows;

    if (isSoldes) {
        if (productHistorySubTab === 'lances') {
            rows = rawRows.filter(function(r) { return r.solde_kind === 'solde_lance' || r.solde_kind === 'retour'; });
        } else if (productHistorySubTab === 'remises') {
            rows = rawRows.filter(function(r) { return r.solde_kind === 'remise_magasin' || r.solde_kind === 'retour'; });
        }
    }

    // Soldes PROGRAMMÉES (listes de prix « Solde … ») : bloc à part au-dessus
    // des ventes, visible dans « Tous » et « Soldes lancés ». Une solde qui
    // vient d'être lancée n'a encore aucune vente : sans ce bloc, elle
    // n'apparaissait nulle part dans l'historique.
    _renderSoldesProgrammees(isSoldes && productHistorySubTab !== 'remises' ? (data.soldes_programmees || []) : null);

    if (summaryEl) {
        if (isSoldes) {
            var totalQty = rows.reduce(function(acc, r) { return acc + (r.qty || 0); }, 0);
            var totalCa = rows.reduce(function(acc, r) { return acc + (r.ca || 0); }, 0);
            var retoursCount = rows.filter(function(r) { return r.solde_kind === 'retour'; }).length;
            var labelType = productHistorySubTab === 'lances' ? ' (soldes lancés)'
                          : (productHistorySubTab === 'remises' ? ' (remises magasin)' : '');
            summaryEl.textContent = 'Ventes en caisse' + labelType + ' : ' + formatNumber(rows.length) + ' ligne(s) · '
                + formatNumber(totalQty) + ' pièces · '
                + formatMAD(totalCa) + ' encaissés'
                + (retoursCount ? ' · dont ' + formatNumber(retoursCount) + ' retour(s) client' : '');
        } else {
            summaryEl.textContent = formatNumber(data.transfers_bons || 0) + ' bon(s) de transfert · '
                + formatNumber(data.transfers_count || 0) + ' ligne(s) · '
                + formatNumber(data.transfers_qty || 0) + ' pièces déplacées';
        }
    }

    if (!rows.length) {
        var tr = document.createElement('tr');
        var td = document.createElement('td');
        td.colSpan = columns.length;
        td.textContent = isSoldes
            ? 'Cette référence n\'a jamais été vendue en solde.'
            : 'Cette référence n\'a jamais fait l\'objet d\'un transfert.';
        td.style.cssText = 'text-align:center;padding:24px;color:#94A3B8;';
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }

    function cell(text, color, weight, align) {
        var td = document.createElement('td');
        td.textContent = text;
        td.style.padding = '9px 8px';
        td.style.fontSize = '0.84rem';
        if (color) td.style.color = color;
        if (weight) td.style.fontWeight = weight;
        if (align) td.style.textAlign = align;
        return td;
    }

    rows.forEach(function(r) {
        var tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid #F1F5F9';

        if (isSoldes) {
            var estRetour = r.type === 'retour';
            if (estRetour) tr.style.background = '#FFF7ED';
            tr.appendChild(cell(r.date, '#64748B'));
            tr.appendChild(cell(r.ticket, '#64748B'));
            tr.appendChild(cell(r.magasin, '#0F172A', '600'));
            tr.appendChild(cell(r.couleur));
            tr.appendChild(cell(r.taille, '#64748B'));
            tr.appendChild(cell(formatNumber(r.qty), estRetour ? '#EA580C' : null, '700', 'center'));
            tr.appendChild(cell(formatMAD(r.prix_catalogue), '#64748B', null, 'right'));
            // Un retour n'a pas de "remise" : afficher un pourcentage ici
            // donnait des valeurs absurdes (-233 % relevé en base).
            var remiseCell = estRetour
                ? cell('Retour', '#EA580C', '700', 'center')
                : cell('-' + (r.remise_pct || 0).toFixed(1).replace('.', ',') + ' %',
                       '#DC2626', '700', 'center');
            if (estRetour) {
                remiseCell.title = 'Avoir / retour client : quantité ou prix négatif en caisse. '
                    + 'Ce n\'est pas une vente en solde.';
            }
            tr.appendChild(remiseCell);
            tr.appendChild(cell(formatMAD(r.prix_paye), '#0F172A', '600', 'right'));
        } else {
            var stateColor = r.state === 'done' ? '#10B981'
                : (r.state === 'transmitted' ? '#2563EB'
                : (r.state === 'submitted' ? '#F59E0B' : '#94A3B8'));
            tr.appendChild(cell(r.name, '#0F172A', '700'));
            tr.appendChild(cell(r.date, '#64748B'));
            // Pas de colonne dédiée ici (le pop-up en a déjà neuf) : quand
            // le bon est parti à l'Inventaire, l'opération à collecter est
            // donnée en infobulle sur l'état, qui dit déjà « transmis ».
            var etat = cell(r.state_label, stateColor, '600');
            if (r.picking_name) {
                etat.title = 'Opération d\'inventaire à collecter : ' + r.picking_name;
            }
            tr.appendChild(etat);
            // D'où vient le bon : le dashboard, ou le formulaire Odoo.
            var origine = cell(r.depuis_dashboard ? 'Dashboard' : 'Odoo',
                               r.depuis_dashboard ? '#7C3AED' : '#94A3B8');
            origine.title = r.intra_societe
                ? 'Transfert au sein d\'une même société'
                : 'Transfert entre deux sociétés (' + r.source_societe + ' → ' + r.dest_societe + ')';
            tr.appendChild(origine);
            var src = cell(r.source_magasin, '#0F172A', '600');
            src.title = r.source_societe;
            tr.appendChild(src);
            var dst = cell(r.dest_magasin, '#0F172A', '600');
            dst.title = r.dest_societe;
            tr.appendChild(dst);
            tr.appendChild(cell(r.couleur));
            tr.appendChild(cell(r.taille, '#64748B'));
            tr.appendChild(cell(formatNumber(r.qty), null, '700', 'center'));
        }

        tbody.appendChild(tr);
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

    ['filter-collection', 'filter-magasin', 'filter-batch'].forEach(function(id) {
        var sel = el(id);
        if (sel) {
            sel.addEventListener('change', function() {
                updateFiltersFromUI();
                loadKPIs();
            });
        }
    });

    // Période : mois et année sont deux listes séparées. Recalculer à
    // chaque changement lançait deux calculs complets quand on les change
    // l'une après l'autre, et affichait entre les deux une période que
    // personne n'a demandée. On attend une courte pause après la dernière
    // modification.
    ['filter-period-month', 'filter-period-month-year',
     'filter-period-week', 'filter-period-year'].forEach(function(id) {
        var sel = el(id);
        if (!sel) return;
        sel.addEventListener('change', function() {
            updateFiltersFromUI();
            _periodeApresPause();
        });
    });

    // Période personnalisée : on attend les DEUX bornes avant de
    // recalculer. Saisir la date de début rechargeait tout le tableau de
    // bord alors que la date de fin n'était pas encore choisie.
    ['filter-date-start', 'filter-date-end'].forEach(function(id) {
        var inp = el(id);
        if (!inp) return;
        inp.addEventListener('change', function() {
            updateFiltersFromUI();
            _majIndiceDates();
            var d1 = el('filter-date-start');
            var d2 = el('filter-date-end');
            var deb = d1 ? d1.value : '';
            var fin = d2 ? d2.value : '';
            if (!deb || !fin || deb > fin) return;
            loadKPIs();
        });
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

    var openSoldeBtn = el('btn-open-solde');
    if (openSoldeBtn) {
        openSoldeBtn.addEventListener('click', function() {
            if (state.detail.article_id) openSoldePanel(state.detail.article_id);
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

    // ── Historique d'une référence (bouton de la fiche produit) ──
    var productHistoryBtn = el('btn-product-history');
    if (productHistoryBtn) productHistoryBtn.addEventListener('click', openProductHistory);

    var closeProductHistoryBtn = el('close-product-history-btn');
    if (closeProductHistoryBtn) closeProductHistoryBtn.addEventListener('click', closeProductHistory);

    var productHistoryOverlay = el('product-history-overlay');
    if (productHistoryOverlay) {
        productHistoryOverlay.addEventListener('click', function(e) {
            if (e.target === productHistoryOverlay) closeProductHistory();
        });
    }

    var prodHistTabT = el('product-history-tab-transferts');
    if (prodHistTabT) {
        prodHistTabT.addEventListener('click', function() { setProductHistoryTab('transferts'); });
    }
    var prodHistTabS = el('product-history-tab-soldes');
    if (prodHistTabS) {
        prodHistTabS.addEventListener('click', function() { setProductHistoryTab('soldes'); });
    }
    var prodHistSubAll = el('product-history-subtab-all');
    if (prodHistSubAll) {
        prodHistSubAll.addEventListener('click', function() { setProductHistorySubTab('all'); });
    }
    var prodHistSubLances = el('product-history-subtab-lances');
    if (prodHistSubLances) {
        prodHistSubLances.addEventListener('click', function() { setProductHistorySubTab('lances'); });
    }
    var prodHistSubRemises = el('product-history-subtab-remises');
    if (prodHistSubRemises) {
        prodHistSubRemises.addEventListener('click', function() { setProductHistorySubTab('remises'); });
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
            _setRetour('transfer-overlay', function() { openColorDetail(articleId, productName, color); });
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

    var carteDepotStock = el('card-mfl-stock');
    if (carteDepotStock) carteDepotStock.addEventListener('click', openDepotStock);
    var fermerDepotStock = el('close-depot-stock-btn');
    if (fermerDepotStock) fermerDepotStock.addEventListener('click', closeDepotStock);
    var overlayDepotStock = el('depot-stock-overlay');
    if (overlayDepotStock) {
        overlayDepotStock.addEventListener('click', function(e) {
            if (e.target === overlayDepotStock) closeDepotStock();
        });
    }
    var rechercheDepotStock = el('search-depot-stock');
    if (rechercheDepotStock) {
        rechercheDepotStock.addEventListener('input', function(e) {
            _depotStockRender(e.target.value);
        });
    }
    var exportDepotStock = el('btn-export-depot-stock');
    if (exportDepotStock) {
        exportDepotStock.addEventListener('click', function(e) {
            e.stopPropagation();
            _depotStockExport();
        });
    }

    var exportDormantBtn = el('btn-export-dormant');
    if (exportDormantBtn) {
        exportDormantBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            window.open('/mavie/api/dormant/export?' + _buildExportParams().toString(), '_blank');
        });
    }

    // ── Écarts d'inventaire (carte cliquable + popup explicatif) ──
    var cardStockPrecision = el('card-stock-precision');
    if (cardStockPrecision) cardStockPrecision.addEventListener('click', openEcarts);

    var searchEcartsInput = el('search-ecarts-input');
    if (searchEcartsInput) {
        searchEcartsInput.addEventListener('input', function(e) {
            _renderEcartsList(e.target.value);
        });
    }

    var closeEcartsBtn = el('close-ecarts-btn');
    if (closeEcartsBtn) closeEcartsBtn.addEventListener('click', closeEcarts);

    var ecartsOverlay = el('ecarts-overlay');
    if (ecartsOverlay) {
        ecartsOverlay.addEventListener('click', function(e) {
            if (e.target === ecartsOverlay) closeEcarts();
        });
    }

    // ── Réconciliation du stock : ouverte par la carte "Stock Réel Odoo" ──
    var stockCard = el('detail-stock-card');
    if (stockCard) stockCard.addEventListener('click', openStockRecon);

    var closeStockReconBtn = el('close-stock-recon-btn');
    if (closeStockReconBtn) closeStockReconBtn.addEventListener('click', closeStockRecon);

    var stockReconOverlay = el('stock-recon-overlay');
    if (stockReconOverlay) {
        stockReconOverlay.addEventListener('click', function(e) {
            if (e.target === stockReconOverlay) closeStockRecon();
        });
    }

    var closeEcartDetailBtn = el('close-ecart-detail-btn');
    if (closeEcartDetailBtn) closeEcartDetailBtn.addEventListener('click', closeEcartDetail);

    var ecartDetailOverlay = el('ecart-detail-overlay');
    if (ecartDetailOverlay) {
        ecartDetailOverlay.addEventListener('click', function(e) {
            if (e.target === ecartDetailOverlay) closeEcartDetail();
        });
    }

    // ── Valorisation : détail par magasin d'une société ──
    var closeValorisationBtn = el('close-valorisation-btn');
    if (closeValorisationBtn) closeValorisationBtn.addEventListener('click', closeValorisationDetail);

    var valorisationOverlay = el('valorisation-overlay');
    if (valorisationOverlay) {
        valorisationOverlay.addEventListener('click', function(e) {
            if (e.target === valorisationOverlay) closeValorisationDetail();
        });
    }

    // ── Historique transferts / soldes ──
    var historyTabTransferts = el('history-tab-transferts');
    if (historyTabTransferts) {
        historyTabTransferts.addEventListener('click', function() { setHistoryTab('transferts'); });
    }
    var historyTabSoldes = el('history-tab-soldes');
    if (historyTabSoldes) {
        historyTabSoldes.addEventListener('click', function() { setHistoryTab('soldes'); });
    }
    // Rafraîchir l'historique sans recharger toute la page (demande
    // utilisatrice 2026-09-22) : un transfert validé ailleurs change d'état.
    var refreshHistoryBtn = el('btn-refresh-history');
    if (refreshHistoryBtn) {
        refreshHistoryBtn.addEventListener('click', async function() {
            refreshHistoryBtn.disabled = true;
            refreshHistoryBtn.textContent = '↻ …';
            await loadHistory();
            refreshHistoryBtn.disabled = false;
            refreshHistoryBtn.textContent = '↻ Rafraîchir';
        });
    }

    var exportHistoryBtn = el('btn-export-history');
    if (exportHistoryBtn) {
        exportHistoryBtn.addEventListener('click', function() {
            var params = _buildExportParams({ kind: historyTab });
            window.open('/mavie/api/history/export?' + params.toString(), '_blank');
        });
    }

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

    // ── Articles vendus en solde (sous-texte cliquable de la carte Qté vendue) ──
    var soldeSubtext = el('kpi-qty-sold-solde');
    if (soldeSubtext) {
        soldeSubtext.addEventListener('click', function(e) {
            e.stopPropagation();
            openSoldes();
        });
    }

    var searchSoldesInput = el('search-soldes-input');
    if (searchSoldesInput) {
        searchSoldesInput.addEventListener('input', function(e) {
            _renderSoldesList(e.target.value);
        });
    }

    var closeSoldesBtn = el('close-soldes-btn');
    if (closeSoldesBtn) closeSoldesBtn.addEventListener('click', closeSoldes);

    var soldesOverlay = el('soldes-overlay');
    if (soldesOverlay) {
        soldesOverlay.addEventListener('click', function(e) {
            if (e.target === soldesOverlay) closeSoldes();
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
// ══════════════════════════════════════════════════════════════
// RÉASSORT — que doit envoyer le dépôt MOD FOR LIFE, et à quel magasin
//
// DEMANDE UTILISATEUR (2026-09-18), placée dans la vue MOD FOR LIFE à sa
// demande (c'est le dépôt qui envoie). Source = dépôt uniquement ; les
// transferts entre magasins ont leur propre écran. Règle d'alerte
// principale = celle de l'utilisateur (reste ≤ 10 % du reçu), complétée
// par la vitesse de vente pour trier, détecter ce qui part trop vite, et
// calculer la quantité à envoyer. Tout le calcul est côté serveur
// (/mavie/api/reassort) ; ici on affiche et on filtre sans recharger.
//
// REFONTE 2 (2026-09-21) — la première refonte (onglets + trois vues +
// filtres segmentés) a été jugée pas assez claire. Choix de
// l'utilisatrice : « 3 blocs simples empilés », vocabulaire simple :
//   ① À envoyer maintenant   (le dépôt peut livrer : quantité proposée > 0)
//   ② Manquant au dépôt      (le dépôt n'a plus l'article : à acheter)
//   ③ À surveiller           (le dépôt l'a, mais rien à envoyer) — replié
// Deux filtres seulement (magasin, recherche), communs aux trois blocs.
// ══════════════════════════════════════════════════════════════

var raState = {
    data: null,
    rows: [],
    bound: false,
    seq: 0,
};

// Libellés simples (l'utilisatrice trouvait « Règle 10 % » / « Livrables »
// compliqués).
var RA_BLOCS = {
    envoyer:    { test: function(r) { return r.propose > 0; }, label: 'À envoyer' },
    vide:       { test: function(r) { return !(r.depot > 0); }, label: 'Manquant au dépôt' },
    surveiller: { test: function(r) { return r.depot > 0 && !(r.propose > 0); }, label: 'À surveiller' },
};

function _raParams() {
    function v(id, def) {
        var e = el(id);
        var n = e ? parseInt(e.value, 10) : NaN;
        return isNaN(n) ? def : n;
    }
    return {
        collection_id: state.collection_id,
        categ_ids: state.categ_ids || [],
        batch_id: state.batch_id,
        fenetre: v('ra-fenetre', 90),
        seuil_pct: v('ra-seuil', 10),
        delai: v('ra-delai', 21),
        cible: v('ra-cible', 30),
        // Stocks négatifs toujours écartés : l'option « Inclure les stocks
        // négatifs » a été retirée des Réglages le 2026-09-21 (demande
        // utilisatrice). Le serveur les écarte par défaut.
        inclure_negatifs: false,
        // Bloc ④ « À placer » : plafond de couverture par magasin.
        plafond: v('ra-plafond', 90),
    };
}

async function loadReassort() {
    _raBindOnce();
    ['envoyer', 'vide', 'placer'].forEach(function(b) {
        var body = el('ra-body-' + b);
        if (body) body.innerHTML = '<div class="rb-empty">Calcul en cours…</div>';
    });
    // Un calcul plus ancien qui répond après un plus récent ne doit pas
    // écraser l'écran (même garde que loadKPIs).
    var seq = ++raState.seq;
    var data = await rpc('/mavie/api/reassort', _raParams());
    if (seq !== raState.seq) return;
    if (!data || data.error) {
        var body = el('ra-body-envoyer');
        if (body) body.innerHTML = '<div class="rb-empty" style="color:#B91C1C;"><b>Le calcul a échoué.</b> '
            + _escapeHtml(data && data.error || 'Erreur inconnue') + '</div>';
        return;
    }
    raState.data = data;
    raState.rows = data.rows || [];
    _raRenderHeader(data);
    _raFillMagasins(data.magasins || []);
    _raRender();
    // Le mode « sortir le stock du dépôt » tourne à part : il ne part pas
    // des ruptures mais du stock, et son calcul est court (0,2 s). On ne
    // bloque donc pas l'affichage principal avec.
    _raChargerAPlacer(seq);
}

async function _raChargerAPlacer(seq) {
    var corps = el('ra-body-placer');
    if (corps) corps.innerHTML = '<div class="rb-empty">Calcul en cours…</div>';
    var data = await rpc('/mavie/api/reassort-a-placer', _raParams());
    if (seq !== raState.seq) return;
    if (!data || data.error) {
        if (corps) corps.innerHTML = '<div class="rb-empty" style="color:#B91C1C;">'
            + _escapeHtml((data && data.error) || 'Erreur inconnue') + '</div>';
        return;
    }
    raState.placer = data;
    _raRenderPlacer();
}

function _raRenderPlacer() {
    var data = raState.placer;
    if (!data) return;
    var k = data.kpis || {};
    var carte = el('ra-kpi-placer');
    if (carte) carte.textContent = formatNumber(k.pieces || 0);

    // Mêmes filtres que les autres blocs : magasin et recherche.
    var magEl = el('ra-f-magasin');
    var mag = magEl ? magEl.value : '';
    var sEl = el('ra-f-search');
    var search = sEl ? (sEl.value || '').trim().toLowerCase() : '';
    var rows = (data.rows || []).filter(function(r) {
        if (mag && String(r.wh_id) !== mag) return false;
        if (search) {
            var hay = (r.reference + ' ' + r.produit + ' ' + r.couleur + ' '
                       + r.taille + ' ' + r.magasin).toLowerCase();
            if (hay.indexOf(search) === -1) return false;
        }
        return true;
    });

    var pcs = rows.reduce(function(a, r) { return a + (r.envoi || 0); }, 0);
    var mags = {};
    rows.forEach(function(r) { mags[r.wh_id] = 1; });
    _raSetCount('placer', rows.length
        ? formatNumber(pcs) + (pcs > 1 ? ' pièces' : ' pièce') + ' · '
          + Object.keys(mags).length + ' magasin' + (Object.keys(mags).length > 1 ? 's' : '')
        : '0 pièce');

    if (!rows.length) {
        _raSetBody('placer', '<div class="rb-empty">Rien à placer : tout ce que le dépôt garde '
            + 'est soit déjà couvert en magasin, soit invendu partout.</div>');
        return;
    }
    _raSetBody('placer', _raTable([
        { label: 'Magasin' }, { label: 'Référence' }, { label: 'Couleur' }, { label: 'Taille' },
        { label: 'Au dépôt', num: 1 }, { label: 'En rayon', num: 1 },
        { label: 'Vendu (' + (data.params && data.params.fenetre || 90) + ' j)', num: 1 },
        { label: 'Couverture', num: 1 }, { label: 'À envoyer', num: 1 },
    ], rows, function(r) {
        var jours = (r.jours_couverts === null || r.jours_couverts === undefined)
            ? '—' : formatNumber(Math.round(r.jours_couverts)) + ' j';
        return '<tr>'
             + '<td><span class="rb-mag">' + _escapeHtml(r.magasin) + '</span></td>'
             + '<td>' + _raRefCell(r) + '</td>'
             + '<td><span class="mfl-color">' + _escapeHtml(r.couleur) + '</span></td>'
             + '<td>' + _escapeHtml(r.taille || '—') + '</td>'
             + '<td class="num">' + formatNumber(r.depot) + '</td>'
             + '<td class="num"' + (r.stock ? '' : ' style="color:#94A3B8;"') + '>'
               + formatNumber(r.stock) + '</td>'
             + '<td class="num">' + formatNumber(r.vendu) + '</td>'
             + '<td class="num" title="Jours de vente que couvre le stock actuel du magasin">'
               + jours + '</td>'
             + '<td class="num" style="font-weight:700;color:#6D28D9;">'
               + formatNumber(r.envoi) + '</td>'
             + '</tr>';
    }));
}

function _raFmtDate(iso) {
    if (!iso) return '—';
    var p = iso.split('-');
    return p.length === 3 ? p[2] + '/' + p[1] + '/' + p[0] : iso;
}

function _raRenderHeader(data) {
    var k = data.kpis || {};
    var p = data.params || {};
    function set(id, txt) { var e = el(id); if (e) e.textContent = txt; }

    // Le sous-titre de paramètres a été retiré le 2026-09-28 (demande
    // utilisatrice) : les réglages sont déjà expliqués un par un dans le
    // panneau « Réglages ».

    // Les trois cartes comptent sur TOUTES les lignes calculées, pas sur
    // les seules lignes chargées : sinon « À envoyer 323 » s'affichait
    // au-dessus d'un tableau qui n'en listait que 290, et « Manquant au
    // dépôt » annonçait 2 696 pour 3 650 lignes réelles.
    set('ra-kpi-pieces', formatNumber(k.pieces_proposees || 0));
    // La carte compte les deux situations désormais réunies : dépôt vide
    // et dépôt épuisé (pièces parties ailleurs).
    set('ra-kpi-vide', formatNumber((k.nb_depot_vide || 0) + (k.nb_surveiller || 0)));


    // Bandeau d'avertissements retiré le 2026-09-28 (demande
    // utilisatrice) : alertes écartées et troncature.
}

function _raFillMagasins(magasins) {
    var sel = el('ra-f-magasin');
    if (!sel) return;
    var courant = sel.value;
    sel.innerHTML = '<option value="">Tous les magasins</option>';
    magasins.forEach(function(m) {
        var o = document.createElement('option');
        o.value = String(m.id);
        o.textContent = m.name;
        sel.appendChild(o);
    });
    if (courant && magasins.some(function(m) { return String(m.id) === courant; })) {
        sel.value = courant;
    }
}

// Filtres communs aux trois blocs : magasin et recherche.
function _raFiltered() {
    var magEl = el('ra-f-magasin');
    var mag = magEl ? magEl.value : '';
    var sEl = el('ra-f-search');
    var search = sEl ? (sEl.value || '').trim().toLowerCase() : '';
    return raState.rows.filter(function(r) {
        if (mag && String(r.wh_id) !== mag) return false;
        if (search) {
            var hay = (r.reference + ' ' + r.produit + ' ' + r.couleur + ' '
                       + r.taille + ' ' + r.magasin).toLowerCase();
            if (hay.indexOf(search) === -1) return false;
        }
        return true;
    });
}

function _raCtx() {
    var p = (raState.data && raState.data.params) || {};
    return { seuil: p.seuil_pct || 10, delai: p.delai || 21, fenetre: p.fenetre || 90 };
}

// ── Cellules ───────────────────────────────────────────────────

function _raRefCell(r) {
    var ref = r.reference || '—';
    var prod = r.produit || '';
    // Le nom produit n'apporte rien quand il répète la référence — cas
    // fréquent en base : « SAC 24P-5938 » / « 24P-5938 ».
    function norm(s) { return String(s).toUpperCase().replace(/[^A-Z0-9]/g, ''); }
    var dup = !prod || norm(ref).indexOf(norm(prod)) !== -1 || norm(prod).indexOf(norm(ref)) !== -1;
    // Bouton de réassort : il manquait, on ne pouvait lancer un envoi que
    // depuis la page Action. Il ouvre la même fenêtre (quantités par
    // magasin, puis génération du transfert).
    var bouton = '<button type="button" class="ra-btn-reassort" data-article="' + r.article_id
               + '" data-couleur="' + _escapeHtml(r.couleur || '')
               + '" data-taille="' + _escapeHtml(r.taille || '')
               // Les lignes du bloc ④ portent « envoi » et visent le
               // plafond de couverture ; les autres portent « propose »
               // et visent la couverture cible.
               + '" data-base="' + (r.envoi !== undefined ? 'plafond' : 'cible')
               + '" data-wh="' + (r.wh_id || '')
               + '" title="Proposition de r\u00e9assort pour cette r\u00e9f\u00e9rence, d\u00e9j\u00e0 chiffr\u00e9e">\u21c4 R\u00e9assort</button>';
    return '<span class="rx-ref" data-article="' + r.article_id + '" data-name="'
         + _escapeHtml(prod || ref) + '">' + _escapeHtml(ref) + '</span>'
         + (dup ? '' : '<span class="rx-prod">' + _escapeHtml(prod) + '</span>')
         + bouton;
}

function _raMagCell(r) {
    return '<td><span class="rb-mag">' + _escapeHtml(r.magasin)
         + (r.societe ? '<small>' + _escapeHtml(r.societe) + '</small>' : '') + '</span></td>';
}

function _raResteCell(r, seuil) {
    // La règle de l'utilisatrice, en un coup d'œil : la barre = part du reçu
    // encore en rayon ; rouge sous le seuil. Le texte garde les pièces.
    var neg = r.stock_negatif
        ? ' <span class="rx-muted" title="Stock Odoo négatif (' + r.stock_negatif + '), compté comme 0.">(' + r.stock_negatif + ')</span>'
        : '';
    if (!r.recu) {
        return '<td class="num">' + formatNumber(r.stock) + neg
             + ' <span class="rx-muted" title="Rien reçu sur la période.">/ —</span></td>';
    }
    var pct = Math.max(0, Math.min(100, r.reste_pct || 0));
    var low = (r.reste_pct || 0) <= seuil;
    return '<td class="num"><span class="rx-reste" title="' + formatNumber(r.reste_pct) + ' % du reçu restant">'
         + '<span class="rx-bar' + (low ? ' low' : '') + '"><i style="width:' + pct + '%"></i></span>'
         + '<span class="rx-reste-txt">' + formatNumber(r.stock) + neg + ' / ' + formatNumber(r.recu) + '</span>'
         + '</span></td>';
}

function _raJoursCell(r, delai) {
    var j = r.jours_restants;
    if (j === null || j === undefined) {
        return '<td class="num"><span class="rx-days none" title="Aucune vente sur la période.">—</span></td>';
    }
    var cls = j === 0 ? 'out' : (j < 7 ? 'urgent' : (j < delai ? 'soon' : ''));
    var txt = j === 0 ? 'Rupture' : (j >= 999 ? '999+ j' : formatNumber(j) + ' j');
    return '<td class="num"><span class="rx-days ' + cls + '">' + txt + '</span></td>';
}

function _raVenduCell(r) {
    // DEMANDE UTILISATEUR (2026-09-21) : « Vendu (90 j) » au lieu de
    // « Vente / sem. ». Une moyenne à virgule (0,6 par semaine) était peu
    // parlante, et trompeuse pour un magasin qui ne vend que depuis
    // quelques jours : on affiche le nombre de pièces vendues sur la
    // fenêtre. La vitesse reste utilisée en coulisse pour les jours
    // restants et la quantité à envoyer.
    return '<td class="num">' + (r.vendu ? formatNumber(r.vendu) : '<span class="rx-muted">0</span>') + '</td>';
}

function _raEnvoiCell(r) {
    if (r.propose > 0) {
        return '<td class="num"><span class="rx-send">' + formatNumber(r.propose) + '</span>'
             + (r.propose < r.besoin ? ' <span class="rx-send-part" title="Le dépôt n\'a pas assez pour tous les magasins.">sur ' + formatNumber(r.besoin) + '</span>' : '')
             + '</td>';
    }
    return '<td class="num"><span class="rx-muted">—</span></td>';
}

function _raAlerteCell(r, seuil, delai) {
    return r.alerte === 'pct'
        ? '<td><span class="rx-tag pv" title="Il reste ' + seuil + ' % ou moins de ce que le magasin a reçu.">Presque vide</span></td>'
        : '<td><span class="rx-tag sv" title="Au rythme actuel, le stock ne tiendra pas ' + delai + ' jours.">Se vend vite</span></td>';
}

// ── Rendu des trois blocs ──────────────────────────────────────

var RA_MAX_LIGNES = 200;

function _raTable(heads, rows, lineFn) {
    var max = Math.min(rows.length, RA_MAX_LIGNES);
    var h = '<div class="rx-scroll"><table class="rx-table"><thead><tr>'
          + heads.map(function(x) { return '<th' + (x.num ? ' class="num"' : '') + '>' + x.label + '</th>'; }).join('')
          + '</tr></thead><tbody>';
    for (var i = 0; i < max; i++) h += lineFn(rows[i]);
    h += '</tbody></table></div>';
    if (rows.length > max) {
        h += '<div class="rb-more">… ' + formatNumber(rows.length - max)
           + ' lignes de plus — affinez avec le magasin ou la recherche, ou utilisez Exporter.</div>';
    }
    return h;
}

function _raRender() {
    // Le bloc ④ suit les mêmes filtres magasin et recherche.
    if (raState.placer) _raRenderPlacer();
    var c = _raCtx();
    var rows = _raFiltered();
    var envoyer = rows.filter(RA_BLOCS.envoyer.test);
    // Les deux cas où le dépôt ne peut pas servir ne font plus qu'une
    // liste (demande utilisatrice 2026-09-28) : « dépôt épuisé » d'abord,
    // parce que ce sont les plus proches d'un envoi possible.
    var vide = rows.filter(RA_BLOCS.vide.test);
    var surveiller = rows.filter(RA_BLOCS.surveiller.test);
    // Bloc ① : regroupé à l'œil par magasin (tri magasin puis urgence),
    // pour se lire comme une liste de préparation.
    envoyer.sort(function(a, b) {
        return String(a.magasin).localeCompare(b.magasin) || ((a.jours_restants || 0) - (b.jours_restants || 0));
    });
    var filtre = !!((el('ra-f-magasin') || {}).value || ((el('ra-f-search') || {}).value || '').trim());

    // ① À envoyer maintenant
    var pcs = envoyer.reduce(function(a, r) { return a + (r.propose || 0); }, 0);
    var mags = {};
    envoyer.forEach(function(r) { mags[r.wh_id] = 1; });
    _raSetCount('envoyer', envoyer.length
        ? formatNumber(pcs) + (pcs > 1 ? ' pièces' : ' pièce') + ' · ' + Object.keys(mags).length + ' magasin' + (Object.keys(mags).length > 1 ? 's' : '')
        : '0 pièce');
    _raSetBody('envoyer', envoyer.length ? _raTable([
            { label: 'Magasin' }, { label: 'Référence' }, { label: 'Couleur' }, { label: 'Taille' },
            { label: 'Stock / reçu', num: 1 }, { label: 'Vendu (' + c.fenetre + ' j)', num: 1 }, { label: 'Jours restants', num: 1 },
            { label: 'Au dépôt', num: 1 }, { label: 'À envoyer', num: 1 }, { label: 'Pourquoi' },
        ], envoyer, function(r) {
            return '<tr>' + _raMagCell(r) + '<td>' + _raRefCell(r) + '</td>'
                 + '<td><span class="mfl-color">' + _escapeHtml(r.couleur) + '</span></td>'
                 + '<td>' + _escapeHtml(r.taille || '—') + '</td>'
                 + _raResteCell(r, c.seuil) + _raVenduCell(r) + _raJoursCell(r, c.delai)
                 + '<td class="num">' + formatNumber(r.depot) + '</td>'
                 + _raEnvoiCell(r) + _raAlerteCell(r, c.seuil, c.delai) + '</tr>';
        })
        : '<div class="rb-empty">' + (filtre ? 'Rien à envoyer pour ce filtre.'
            : 'Rien à envoyer pour l\'instant : aucun article en alerte qui se vend n\'est disponible au dépôt.') + '</div>');

    // ② Manquant au dépôt — « dépôt épuisé » en tête, puis le plus
    // urgent d'abord (ce qui se vend).
    function parUrgence(a, b) {
        return ((b.vitesse_jour || 0) - (a.vitesse_jour || 0))
            || String(a.magasin).localeCompare(b.magasin);
    }
    vide.sort(parUrgence);
    surveiller.sort(parUrgence);
    var aCommander = surveiller.concat(vide);
    var videTotal = ((raState.data && raState.data.kpis && raState.data.kpis.nb_depot_vide) || 0)
        + ((raState.data && raState.data.kpis && raState.data.kpis.nb_surveiller) || 0);
    var piecesManquantes = aCommander.reduce(function(a, r) { return a + (r.besoin || 0); }, 0);
    _raSetCount('vide', formatNumber(aCommander.length) + ' ligne'
        + (aCommander.length > 1 ? 's' : '')
        + ((!filtre && videTotal > aCommander.length)
            ? ' affichées sur ' + formatNumber(videTotal) : '')
        + (piecesManquantes ? ' · ' + formatNumber(piecesManquantes) + ' pièces' : ''));
    _raSetBody('vide', aCommander.length ? _raTable([
            { label: 'Magasin' }, { label: 'Référence' }, { label: 'Couleur' }, { label: 'Taille' },
            { label: 'Stock / reçu', num: 1 }, { label: 'Vendu (' + c.fenetre + ' j)', num: 1 }, { label: 'Jours restants', num: 1 },
            { label: 'Besoin', num: 1 }, { label: 'Dépôt' }, { label: 'Pourquoi' },
        ], aCommander, function(r) {
            // Deux situations dans la même liste : le dépôt n'a rien, ou il
            // avait des pièces mais elles sont déjà attribuées ailleurs.
            var epuise = (r.depot || 0) > 0;
            return '<tr' + (epuise ? ' style="background:#FFFBEB;"' : '') + '>'
                 + _raMagCell(r) + '<td>' + _raRefCell(r) + '</td>'
                 + '<td><span class="mfl-color">' + _escapeHtml(r.couleur) + '</span></td>'
                 + '<td>' + _escapeHtml(r.taille || '—') + '</td>'
                 + _raResteCell(r, c.seuil) + _raVenduCell(r) + _raJoursCell(r, c.delai)
                 + '<td class="num">' + (r.besoin ? '<b>' + formatNumber(r.besoin) + '</b>' : '<span class="rx-muted">—</span>') + '</td>'
                 + (epuise
                     ? '<td><span class="mfl-code" title="Le d\u00e9p\u00f4t avait ' + formatNumber(r.depot)
                       + ' pi\u00e8ce(s), d\u00e9j\u00e0 attribu\u00e9e(s) \u00e0 un magasin qui vend plus vite">\u00e9puis\u00e9</span></td>'
                     : '<td><span class="rx-muted">vide</span></td>')
                 + _raAlerteCell(r, c.seuil, c.delai) + '</tr>';
        })
        : '<div class="rb-empty">Le dépôt a tous les articles en alerte.</div>');

    var foot = el('ra-foot');
    if (foot) foot.textContent = 'Cliquer sur une référence ouvre sa fiche (stock par magasin, historique).';
}

function _raSetCount(bloc, txt) { var e = el('ra-cnt-' + bloc); if (e) e.textContent = txt; }
function _raSetBody(bloc, html) { var e = el('ra-body-' + bloc); if (e) e.innerHTML = html; }

// ── Événements ─────────────────────────────────────────────────

function _raBindOnce() {
    if (raState.bound) return;
    raState.bound = true;

    var settingsBtn = el('btn-ra-settings');
    if (settingsBtn) settingsBtn.addEventListener('click', function() {
        var box = el('ra-settings');
        if (!box) return;
        var open = box.style.display === 'none';
        box.style.display = open ? '' : 'none';
        settingsBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    var recalc = el('btn-ra-recalc');
    if (recalc) recalc.addEventListener('click', loadReassort);
    ['ra-fenetre', 'ra-seuil', 'ra-delai', 'ra-cible'].forEach(function(id) {
        var e = el(id);
        if (e) e.addEventListener('keydown', function(ev) {
            if (ev.key === 'Enter') loadReassort();
        });
    });

    var mag = el('ra-f-magasin');
    if (mag) mag.addEventListener('change', _raRender);
    var search = el('ra-f-search');
    if (search) {
        var t = null;
        search.addEventListener('input', function() {
            clearTimeout(t);
            t = setTimeout(_raRender, 180);
        });
    }
    var exp = el('btn-ra-export');
    if (exp) exp.addEventListener('click', function() { _raExportCsv(_raFiltered()); });

    // Cliquer une carte de synthèse ouvre le bloc correspondant et y
    // amène la page : les blocs ②③④ sont repliés par défaut, donc
    // cliquer la carte ne faisait rien de visible.
    [['ra-kpi-pieces', 'envoyer'], ['ra-kpi-vide', 'vide'],
     ['ra-kpi-placer', 'placer']].forEach(function(paire) {
        var valeur = el(paire[0]);
        var carte = valeur && valeur.closest ? valeur.closest('.rx-stat') : null;
        if (!carte) return;
        carte.style.cursor = 'pointer';
        carte.title = 'Cliquer pour voir le d\u00e9tail';
        carte.addEventListener('click', function() {
            var bloc = el('ra-bloc-' + paire[1]);
            if (!bloc) return;
            // Le tableau de bord tourne dans une iframe : scrollIntoView n'y
            // déplace rien de visible. On replie les autres blocs pour que
            // celui qu'on demande remonte juste sous les cartes.
            ['envoyer', 'vide', 'placer'].forEach(function(autre) {
                var e = el('ra-bloc-' + autre);
                if (e) e.setAttribute('data-open', autre === paire[1] ? '1' : '0');
            });
            if (bloc.scrollIntoView) {
                try { bloc.scrollIntoView({ behavior: 'smooth', block: 'start' }); } catch (e) {}
            }
        });
    });

    ['envoyer', 'vide', 'placer'].forEach(function(b) {
        var bloc = el('ra-bloc-' + b);
        if (!bloc) return;
        // Clic sur l'en-tête d'un bloc : le replier ou le déplier.
        var head = bloc.querySelector('.rb-head');
        if (head) head.addEventListener('click', function() {
            bloc.setAttribute('data-open', bloc.getAttribute('data-open') === '1' ? '0' : '1');
        });
        // Clic sur une référence -> fiche produit existante : stock par
        // magasin, historique. C'est la suite logique d'une alerte.
        var body = el('ra-body-' + b);
        if (body) body.addEventListener('click', function(ev) {
            var cible = ev.target;
            var btn = cible.closest && cible.closest('.ra-btn-reassort');
            if (btn) {
                ev.stopPropagation();
                var aid = parseInt(btn.getAttribute('data-article'), 10);
                if (aid) {
                    var spanRef = btn.parentNode && btn.parentNode.querySelector
                        ? btn.parentNode.querySelector('.rx-ref') : null;
                    // On passe la taille et les réglages du bloc : sinon la
                    // fenêtre additionne toutes les pointures et annonce un
                    // besoin nul là où la ligne dit « envoyer 3 ».
                    openActionReassort(
                        { id: aid, ref: spanRef ? spanRef.textContent : '' },
                        btn.getAttribute('data-couleur') || '',
                        btn.getAttribute('data-taille') || '',
                        btn.getAttribute('data-base') || 'cible',
                        parseInt(btn.getAttribute('data-wh'), 10) || 0);
                }
                return;
            }
            var ref = cible.closest && cible.closest('.rx-ref');
            if (!ref) return;
            var id = parseInt(ref.getAttribute('data-article'), 10);
            if (id) openDetail(id, ref.getAttribute('data-name') || ref.textContent);
        });
    });
}

function _raExportCsv(rows) {
    var p = (raState.data && raState.data.params) || {};
    var sep = ';';
    function cell(v) {
        var s = (v === null || v === undefined) ? '' : String(v);
        return '"' + s.replace(/"/g, '""') + '"';
    }
    function num(v) {
        return (v === null || v === undefined) ? '' : String(v).replace('.', ',');
    }
    function bloc(r) {
        return RA_BLOCS.envoyer.test(r) ? RA_BLOCS.envoyer.label
             : (RA_BLOCS.vide.test(r) ? RA_BLOCS.vide.label : RA_BLOCS.surveiller.label);
    }
    var lines = [[
        'Decision', 'Magasin', 'Societe', 'Reference', 'Produit', 'Couleur', 'Taille', 'Recu', 'Vendu',
        'Stock', 'Stock Odoo negatif', 'Reste %', 'Vente par semaine',
        'Jours restants', 'Au depot', 'Besoin', 'A envoyer', 'Pourquoi'
    ].join(sep)];
    rows.forEach(function(r) {
        lines.push([
            cell(bloc(r)), cell(r.magasin), cell(r.societe), cell(r.reference), cell(r.produit), cell(r.couleur),
            cell(r.taille), r.recu, r.vendu, r.stock, r.stock_negatif || '',
            num(r.reste_pct), num(r.vitesse_semaine), num(r.jours_restants),
            r.depot, r.besoin, r.propose,
            cell(r.alerte === 'pct' ? 'Presque vide (reste <= ' + (p.seuil_pct || 10) + '% du recu)' : 'Se vend vite'),
        ].join(sep));
    });
    var blob = new Blob(['﻿' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8;' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'reassort_depot_' + ((raState.data && raState.data.date_reference) || 'export') + '.csv';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function() { URL.revokeObjectURL(url); }, 1000);
}

// ══════════════════════════════════════════════════════════════
// SOLDER UNE RÉFÉRENCE DEPUIS LE DASHBOARD
//
// DEMANDE UTILISATEUR (2026-09-21) : bouton « Solder » à côté de
// « Transférer » dans la fiche référence. Pour chaque magasin coché, la
// solde est écrite dans SA liste de soldes (liste de prix « Solde … »
// rattachée à sa caisse) ; si le magasin n'en a pas, une nouvelle liste est
// créée avec le nom saisi. Le serveur (/mavie/api/solde-apply) valide tout
// avant d'écrire et n'applique rien si un seul magasin échoue.
// ══════════════════════════════════════════════════════════════

var sdState = { data: null, checked: {}, noms: {}, bound: false, busy: false };

async function openSoldePanel(articleId, couleur, prerempli) {
    var overlay = el('solde-overlay');
    if (!overlay || !articleId) return;
    _sdBindOnce();
    sdState.checked = {};
    sdState.noms = {};
    var result = el('sd-result');
    if (result) { result.style.display = 'none'; result.innerHTML = ''; }
    ['sd-prix', 'sd-remise', 'sd-fin', 'sd-code', 'sd-montant'].forEach(function(id) { var e = el(id); if (e) e.value = ''; });
    var nbC = el('sd-nb-cartes'); if (nbC) nbC.value = '1';
    _sdMajChamps();
    var debut = el('sd-debut');
    if (debut) debut.value = new Date().toISOString().slice(0, 10);
    var tbody = el('sd-tbody');
    if (tbody) tbody.innerHTML = '<tr><td colspan="5" class="rx-muted" style="text-align:center;padding:18px;">Chargement des magasins…</td></tr>';
    overlay.classList.add('active');
    sdState.couleur = couleur || '';
    await _sdLoad(articleId);
    // Appelé depuis la page Propositions : on pose la remise conseillée
    // et on coche les magasins qui ont du stock, pour qu'il ne reste que
    // le bouton à cliquer.
    if (prerempli && prerempli.remise > 0) _sdPreremplir(prerempli.remise);
}

function _sdPreremplir(remise) {
    var data = sdState.data;
    if (!data) return;
    var cat = data.prix_catalogue_ttc || 0;
    var champ = el('sd-remise');
    if (champ) champ.value = remise;
    // « sd-prix » fait foi pour la validation : on le calcule ici, sinon le
    // bouton reste desactive malgre la remise affichee.
    var champPrix = el('sd-prix');
    if (champPrix && cat > 0 && remise > 0 && remise < 100) {
        champPrix.value = (Math.round(cat * (1 - remise / 100) * 100) / 100).toFixed(2);
    }
    (data.magasins || []).forEach(function(m) {
        // Un magasin sans caisse ne peut pas recevoir de solde, et un
        // magasin sans stock n'a rien à démarquer.
        if ((m.stock || 0) > 0 && m.caisses && m.caisses.length) {
            sdState.checked[m.shop_field] = true;
        }
    });
    _sdRenderStores();
    _sdRefresh();
}

function closeSoldePanel() {
    var overlay = el('solde-overlay');
    if (overlay) overlay.classList.remove('active');
}

async function _sdLoad(articleId) {
    var data = await rpc('/mavie/api/solde-context', { product_tmpl_id: articleId, couleur: sdState.couleur || '' });
    var tbody = el('sd-tbody');
    if (!data || data.error) {
        if (tbody) tbody.innerHTML = '<tr><td colspan="5" style="color:#B91C1C;text-align:center;padding:18px;">'
            + _escapeHtml(data && data.error || 'Erreur inconnue') + '</td></tr>';
        return;
    }
    sdState.data = data;
    function set(id, txt) { var e = el(id); if (e) e.textContent = txt; }
    // Solde d'une seule couleur (page Action) : on l'écrit dans le titre.
    set('sd-ref', data.reference + (data.couleur ? ' — ' + data.couleur + ' (' + data.nb_variantes + ' taille' + (data.nb_variantes > 1 ? 's' : '') + ')' : ''));
    set('sd-name', data.nom);
    set('sd-catalogue', formatMAD(data.prix_catalogue_ttc) + ' TTC');
    _sdRenderStores();
    _sdRefresh();
}

function _sdRenderStores() {
    var tbody = el('sd-tbody');
    var data = sdState.data;
    if (!tbody || !data) return;
    var h = '';
    (data.magasins || []).forEach(function(m) {
        var on = !!sdState.checked[m.shop_field];
        var liste;
        if (m.liste) {
            liste = '<span class="sd-list-name">' + _escapeHtml(m.liste.name) + '</span>'
                  + '<span class="sd-badge exist" title="' + formatNumber(m.liste.nb_articles) + ' articles déjà dans cette liste">existante · '
                  + formatNumber(m.liste.nb_articles) + ' art.</span>';
            // Liste partagée (ex. « REMISE 20% ») : le prix soldé s'appliquera
            // aussi dans ces autres caisses — on le dit avant de valider.
            var autres = m.liste.autres_caisses || [];
            if (autres.length) {
                liste += '<div style="font-size:0.72rem;color:#B45309;margin-top:3px;" title="' + _escapeHtml(autres.join(', ')) + '">'
                       + '⚠ partagée avec ' + autres.length + ' autre' + (autres.length > 1 ? 's' : '') + ' caisse' + (autres.length > 1 ? 's' : '')
                       + ' : même prix là-bas</div>';
            }
        } else {
            var nom = sdState.noms[m.shop_field] !== undefined ? sdState.noms[m.shop_field] : m.nom_propose;
            liste = '<input type="text" class="sd-list-input" data-nom="' + _escapeHtml(m.shop_field) + '" value="'
                  + _escapeHtml(nom) + '" title="Nom de la nouvelle liste de soldes de ce magasin"/>'
                  + '<span class="sd-badge new">sera créée</span>';
        }
        var actuelle = m.regle
            ? '<b>' + formatMAD(m.regle.prix_ttc) + '</b>' + (m.regle.date_start ? '<span class="rx-prod">depuis le ' + _raFmtDate(m.regle.date_start) + '</span>' : '')
            : '<span class="rx-muted">—</span>';
        h += '<tr class="' + (on ? 'sd-on' : '') + '">'
           + '<td><input type="checkbox" data-shop="' + _escapeHtml(m.shop_field) + '"' + (on ? ' checked="checked"' : '')
           + (m.caisses && m.caisses.length ? '' : ' disabled="disabled" title="Aucune caisse : impossible d\'y appliquer une solde"') + '/></td>'
           + '<td><span class="rx-group-name" style="font-size:0.83rem;">' + _escapeHtml(m.magasin) + '</span>'
           + '<span class="rx-prod">' + _escapeHtml(m.societe) + '</span></td>'
           + '<td class="num">' + (m.stock ? formatNumber(m.stock) : '<span class="rx-muted">0</span>') + '</td>'
           + '<td>' + liste + '</td>'
           + '<td class="num">' + actuelle + '</td>'
           + '</tr>';
    });
    tbody.innerHTML = h || '<tr><td colspan="5" class="rx-muted" style="text-align:center;">Aucun magasin actif.</td></tr>';
}

function _sdPrix() {
    var v = parseFloat((el('sd-prix') || {}).value);
    return isNaN(v) ? null : Math.round(v * 100) / 100;
}

// Contrôle et résumé à chaque saisie : le bouton ne s'active que si la
// solde est applicable — le serveur revérifie de toute façon.
// Le pop-up porte les 5 types (demande utilisatrice 2026-09-23) : promotion,
// liste de prix, code promo, carte cadeau, fidélité. On n'affiche que les
// champs du type choisi.
var SD_AIDE = {
    promotion: 'Crée une promotion Remise & Fidélité sur cette référence, dans les caisses choisies.',
    pricelist: 'Écrit le prix soldé dans la liste de prix de chaque magasin.',
    promo_code: 'Remise donnée seulement si le client dit le code en caisse.',
    gift_card: 'Crée des cartes cadeaux : un code et un montant, utilisables comme paiement.',
    loyalty: 'Le client cumule des points sur ses achats et les échange contre une remise.',
};
function _sdMode() {
    return (el('sd-mode') || {}).value || 'promotion';
}
function _sdMajChamps() {
    var mode = _sdMode();
    (document.querySelectorAll('.sd-form .rx-field[data-modes]') || []).forEach(function(f) {
        var ok = f.getAttribute('data-modes').split(' ').indexOf(mode) !== -1;
        f.style.display = ok ? '' : 'none';
    });
    var aide = el('sd-aide');
    if (aide) aide.textContent = SD_AIDE[mode] || '';
    var btn = el('btn-sd-apply');
    if (btn) btn.textContent = mode === 'gift_card' ? 'Créer les cartes'
        : (mode === 'loyalty' ? 'Créer le programme'
        : (mode === 'promo_code' ? 'Créer le code' : 'Appliquer la solde'));
    var titre = el('sd-titre');
    if (titre) titre.textContent = mode === 'gift_card' ? 'Cartes cadeaux'
        : (mode === 'loyalty' ? 'Programme de fidélité'
        : (mode === 'promo_code' ? 'Code promo' : 'Solder une référence'));
}
function _sdNum(id) {
    var e = el(id);
    var n = e ? parseFloat(e.value) : NaN;
    return isNaN(n) ? null : n;
}

function _sdRefresh() {
    var data = sdState.data || {};
    var cat = data.prix_catalogue_ttc || 0;
    var mode = _sdMode();
    var prix = _sdPrix();
    var prev = el('sd-preview');
    var erreur = '';
    if (mode === 'gift_card' || mode === 'loyalty' || mode === 'promo_code') {
        return _sdRefreshProgramme(mode);
    }
    if (prix !== null) {
        if (prix <= 0) erreur = 'Le prix soldé doit être supérieur à 0.';
        else if (prix >= cat) erreur = 'Le prix soldé doit être inférieur au prix de vente (' + formatMAD(cat) + ').';
    }
    var d1 = (el('sd-debut') || {}).value, d2 = (el('sd-fin') || {}).value;
    if (!erreur && d1 && d2 && d2 < d1) erreur = 'La date de fin est avant la date de début.';
    if (prev) {
        if (erreur) prev.innerHTML = '<span class="sd-err">' + _escapeHtml(erreur) + '</span>';
        else if (prix !== null) prev.innerHTML = '<span class="sd-old">' + formatMAD(cat) + '</span> → <span class="sd-new">'
            + formatMAD(prix) + ' TTC</span> · remise de ' + Math.round((1 - prix / cat) * 100) + ' %'
            + (d1 ? ' · à partir du ' + _raFmtDate(d1) : '') + (d2 ? ' jusqu\'au ' + _raFmtDate(d2) : ' · sans date de fin');
        else prev.innerHTML = '<span class="rx-muted">Saisissez le prix soldé TTC, ou un pourcentage de remise.</span>';
    }
    var choisis = (data.magasins || []).filter(function(m) { return sdState.checked[m.shop_field]; });
    var nouvelles = choisis.filter(function(m) { return !m.liste; });
    var sum = el('sd-summary');
    if (sum) {
        sum.textContent = choisis.length
            ? choisis.length + ' magasin' + (choisis.length > 1 ? 's' : '') + ' sélectionné' + (choisis.length > 1 ? 's' : '')
              + (nouvelles.length ? ' · ' + nouvelles.length + ' nouvelle' + (nouvelles.length > 1 ? 's' : '') + ' liste' + (nouvelles.length > 1 ? 's' : '') + ' de soldes à créer' : '')
            : 'Aucun magasin sélectionné.';
    }
    var nomVide = nouvelles.some(function(m) { return !((sdState.noms[m.shop_field] !== undefined ? sdState.noms[m.shop_field] : m.nom_propose) || '').trim(); });
    var btn = el('btn-sd-apply');
    if (btn) btn.disabled = sdState.busy || !!erreur || prix === null || !choisis.length || nomVide;
}

async function _sdApply() {
    var data = sdState.data;
    if (!data || sdState.busy) return;
    var prix = _sdPrix();
    var mode = _sdMode();
    var choisis = (data.magasins || []).filter(function(m) { return sdState.checked[m.shop_field]; });
    var magasinsTxt = choisis.map(function(m) { return '• ' + m.magasin; }).join('\n');
    // Confirmation explicite : l'action change ce qui se passe en caisse.
    var msg;
    if (mode === 'gift_card') {
        msg = 'Créer ' + (_sdNum('sd-nb-cartes') || 1) + ' carte(s) de ' + formatMAD(_sdNum('sd-montant')) + ' ?';
    } else if (mode === 'loyalty') {
        msg = 'Créer le programme de fidélité dans :\n' + magasinsTxt;
    } else if (mode === 'promo_code') {
        msg = 'Créer le code ' + ((el('sd-code') || {}).value || '').trim() + ' dans :\n' + magasinsTxt;
    } else {
        var quoi = mode === 'promotion' ? 'Promotion' : 'Liste de prix';
        msg = quoi + ' · ' + data.reference + (data.couleur ? ' ' + data.couleur : '')
            + ' à ' + formatMAD(prix) + ' TTC dans :\n' + magasinsTxt;
    }
    if (!window.confirm(msg)) return;

    sdState.busy = true;
    _sdRefresh();
    var res = await rpc('/mavie/api/solde-apply', {
        product_tmpl_id: data.product_tmpl_id,
        couleur: data.couleur || '',
        mode: mode,
        prix_ttc: prix,
        remise_pct: _sdNum('sd-remise'),
        code: ((el('sd-code') || {}).value || '').trim(),
        montant: _sdNum('sd-montant'),
        nb_cartes: _sdNum('sd-nb-cartes'),
        points_par_mad: _sdNum('sd-points'),
        points_requis: _sdNum('sd-points-requis'),
        remise_fidelite: _sdNum('sd-remise-fid'),
        date_start: (el('sd-debut') || {}).value || '',
        date_end: (el('sd-fin') || {}).value || '',
        magasins: choisis.map(function(m) {
            return { shop_field: m.shop_field, nom_liste: m.liste ? '' : (sdState.noms[m.shop_field] !== undefined ? sdState.noms[m.shop_field] : m.nom_propose) };
        }),
    });
    sdState.busy = false;
    var box = el('sd-result');
    if (!res || res.error || !res.ok) {
        if (box) {
            box.className = 'sd-result ko';
            box.innerHTML = '<b>La solde n\'a pas été appliquée.</b> Aucun magasin n\'a été modifié.<br/>'
                + _escapeHtml(res && res.error || 'Erreur inconnue');
            box.style.display = '';
        }
        _sdRefresh();
        return;
    }
    if (box) {
        box.className = 'sd-result ok';
        var cartes = [];
        (res.resultats || []).forEach(function(r) { (r.cartes || []).forEach(function(c) { if (cartes.indexOf(c) === -1) cartes.push(c); }); });
        var titres = { gift_card: 'Cartes créées.', loyalty: 'Programme de fidélité créé.', promo_code: 'Code promo créé.' };
        box.innerHTML = '<b>' + (titres[mode] || 'Solde appliquée.') + '</b><br/>' + res.resultats.map(function(r) {
            if (r.cartes) return '✓ ' + _escapeHtml(r.magasin) + ' — ' + _escapeHtml(r.liste);
            if (r.fidelite) return '✓ ' + _escapeHtml(r.magasin) + ' — ' + formatNumber(r.points_requis) + ' points = ' + r.remise_pct + ' %';
            if (r.code) return '✓ ' + _escapeHtml(r.magasin) + ' — code ' + _escapeHtml(r.code) + ' · ' + formatNumber(r.remise_pct) + ' %';
            return '✓ ' + _escapeHtml(r.magasin) + ' — ' + formatMAD(r.prix_ttc) + ' TTC dans « ' + _escapeHtml(r.liste) + ' »'
                + (r.promotion ? ' (promotion créée, remise ' + formatNumber(r.remise_pct) + ' %)'
                               : (r.liste_creee ? ' (liste créée et ajoutée à la caisse)' : ''))
                + (r.ancien_prix_ttc ? ' · remplace ' + formatMAD(r.ancien_prix_ttc) : '');
        }).join('<br/>')
            + (cartes.length ? '<br/><b>Codes :</b> ' + _escapeHtml(cartes.join(', ')) : '')
            + '<br/><span style="color:#475569;">Une caisse déjà ouverte le verra après rechargement.</span>';
        box.style.display = '';
    }
    // Recharge l'état réel des magasins (listes créées, solde en place).
    sdState.checked = {};
    sdState.noms = {};
    await _sdLoad(data.product_tmpl_id);
}

// Aperçu et contrôle des trois autres types.
function _sdRefreshProgramme(mode) {
    var data = sdState.data || {};
    var choisis = (data.magasins || []).filter(function(m) { return sdState.checked[m.shop_field]; });
    var prev = el('sd-preview');
    var erreur = '';
    var texte = '';
    if (mode === 'promo_code') {
        var code = ((el('sd-code') || {}).value || '').trim();
        var prix = _sdPrix();
        var cat = data.prix_catalogue_ttc || 0;
        if (!code) erreur = 'Saisissez le code.';
        else if (prix === null) erreur = 'Saisissez le prix soldé ou la remise.';
        else if (prix >= cat) erreur = 'Le prix doit être inférieur à ' + formatMAD(cat) + '.';
        else texte = 'Code <b>' + _escapeHtml(code) + '</b> → ' + formatMAD(prix) + ' TTC ('
            + Math.round((1 - prix / cat) * 100) + ' % de remise) sur cette référence.';
    } else if (mode === 'gift_card') {
        var montant = _sdNum('sd-montant'), nb = _sdNum('sd-nb-cartes') || 1;
        if (!montant || montant <= 0) erreur = 'Saisissez le montant de la carte.';
        else if (nb < 1 || nb > 200) erreur = 'Entre 1 et 200 cartes.';
        else texte = formatNumber(nb) + ' carte' + (nb > 1 ? 's' : '') + ' de ' + formatMAD(montant) + '.';
    } else {
        var pts = _sdNum('sd-points'), requis = _sdNum('sd-points-requis'), rem = _sdNum('sd-remise-fid');
        if (!pts || !requis || !rem) erreur = 'Remplissez les trois champs.';
        else texte = pts + ' point(s) par MAD · ' + formatNumber(requis) + ' points = ' + rem + ' % de remise.';
    }
    var d1 = (el('sd-debut') || {}).value, d2 = (el('sd-fin') || {}).value;
    if (!erreur && d1 && d2 && d2 < d1) erreur = 'La date de fin est avant la date de début.';
    if (prev) prev.innerHTML = erreur ? '<span class="sd-err">' + _escapeHtml(erreur) + '</span>' : texte;
    var sum = el('sd-summary');
    if (sum) sum.textContent = choisis.length
        ? choisis.length + ' magasin' + (choisis.length > 1 ? 's' : '') + ' sélectionné' + (choisis.length > 1 ? 's' : '')
        : 'Aucun magasin sélectionné.';
    var btn = el('btn-sd-apply');
    if (btn) btn.disabled = sdState.busy || !!erreur || !choisis.length;
}

function _sdBindOnce() {
    if (sdState.bound) return;
    sdState.bound = true;

    var modeSel = el('sd-mode');
    if (modeSel) modeSel.addEventListener('change', function() { _sdMajChamps(); _sdRefresh(); });
    ['sd-code', 'sd-montant', 'sd-nb-cartes', 'sd-points', 'sd-points-requis', 'sd-remise-fid'].forEach(function(id) {
        var e = el(id);
        if (e) e.addEventListener('input', _sdRefresh);
    });
    var close = el('close-solde-btn');
    if (close) close.addEventListener('click', closeSoldePanel);
    var overlay = el('solde-overlay');
    if (overlay) overlay.addEventListener('click', function(ev) { if (ev.target === overlay) closeSoldePanel(); });

    var prix = el('sd-prix'), remise = el('sd-remise');
    // Prix et remise sont liés : on saisit l'un, l'autre se calcule.
    if (prix) prix.addEventListener('input', function() {
        var cat = (sdState.data || {}).prix_catalogue_ttc || 0;
        var p = _sdPrix();
        if (remise) remise.value = (p !== null && cat > 0 && p > 0 && p < cat) ? Math.round((1 - p / cat) * 100) : '';
        _sdRefresh();
    });
    if (remise) remise.addEventListener('input', function() {
        var cat = (sdState.data || {}).prix_catalogue_ttc || 0;
        var r = parseFloat(remise.value);
        if (prix) prix.value = (!isNaN(r) && r > 0 && r < 100 && cat > 0) ? (Math.round(cat * (1 - r / 100) * 100) / 100).toFixed(2) : '';
        _sdRefresh();
    });
    ['sd-debut', 'sd-fin'].forEach(function(id) { var e = el(id); if (e) e.addEventListener('change', _sdRefresh); });

    var tbody = el('sd-tbody');
    if (tbody) {
        tbody.addEventListener('change', function(ev) {
            var cb = ev.target.closest && ev.target.closest('input[type="checkbox"][data-shop]');
            if (!cb) return;
            sdState.checked[cb.getAttribute('data-shop')] = cb.checked;
            var tr = cb.closest('tr');
            if (tr) tr.classList.toggle('sd-on', cb.checked);
            _sdRefresh();
        });
        tbody.addEventListener('input', function(ev) {
            var inp = ev.target.closest && ev.target.closest('input[data-nom]');
            if (!inp) return;
            sdState.noms[inp.getAttribute('data-nom')] = inp.value;
            _sdRefresh();
        });
    }
    var withStock = el('btn-sd-with-stock');
    if (withStock) withStock.addEventListener('click', function() {
        ((sdState.data || {}).magasins || []).forEach(function(m) {
            if (m.stock > 0 && m.caisses && m.caisses.length) sdState.checked[m.shop_field] = true;
        });
        _sdRenderStores();
        _sdRefresh();
    });
    var none = el('btn-sd-none');
    if (none) none.addEventListener('click', function() { sdState.checked = {}; _sdRenderStores(); _sdRefresh(); });
    var apply = el('btn-sd-apply');
    if (apply) apply.addEventListener('click', _sdApply);
}

// Bloc « Soldes programmées » de l'historique d'une référence.
// rows = null -> bloc masqué (onglet Transferts, ou sous-onglet Remises
// magasin, qui ne concerne que les remises faites en caisse).
var SD_STATUTS = {
    en_cours: ['En cours', '#15803D', '#DCFCE7'],
    a_venir:  ['À venir', '#1D4ED8', '#DBEAFE'],
    terminee: ['Terminée', '#475569', '#F1F5F9'],
    inactive: ['Liste désactivée', '#475569', '#F1F5F9'],
};

function _renderSoldesProgrammees(rows) {
    var box = el('product-history-programmees');
    if (!box) return;
    if (rows === null) {
        box.style.display = 'none';
        box.innerHTML = '';
        return;
    }
    box.style.display = '';
    var titre = '<div style="font-size:0.85rem;font-weight:700;color:#0F172A;margin-bottom:8px;">'
              + '🏷️ Soldes programmées — listes de prix des magasins (' + rows.length + ')</div>';
    if (!rows.length) {
        box.innerHTML = titre + '<div style="font-size:0.82rem;color:#94A3B8;padding:10px 12px;border:1px dashed #E2E8F0;border-radius:8px;">'
            + 'Aucune solde programmée pour cette référence. Utilisez le bouton « Solder » de la fiche pour en lancer une.</div>';
        return;
    }
    var th = function(t, right) {
        return '<th style="padding:8px;font-size:0.7rem;font-weight:700;color:#475569;text-transform:uppercase;'
             + 'background:#F8FAFC;border-bottom:1px solid #E2E8F0;white-space:nowrap;' + (right ? 'text-align:right;' : 'text-align:left;') + '">' + t + '</th>';
    };
    var h = titre + '<div style="border:1px solid #E2E8F0;border-radius:8px;overflow:auto;"><table style="width:100%;border-collapse:collapse;"><thead><tr>'
          + th('Lancée le') + th('Magasin') + th('Liste de soldes') + th('Prix normal (TTC)', 1)
          + th('Prix soldé (TTC)', 1) + th('Remise', 1) + th('Période') + th('Statut') + th('Par')
          + '</tr></thead><tbody>';
    rows.forEach(function(r) {
        var st = SD_STATUTS[r.statut] || [r.statut, '#475569', '#F1F5F9'];
        var td = function(v, style) {
            return '<td style="padding:8px;font-size:0.82rem;border-bottom:1px solid #F1F5F9;' + (style || '') + '">' + v + '</td>';
        };
        var magasins = (r.magasins || []).length ? r.magasins.map(_escapeHtml).join('<br/>')
            : '<span style="color:#94A3B8;" title="Cette liste n\'est rattachée à aucune caisse">aucune caisse</span>';
        var periode = (r.debut ? 'du ' + _raFmtDate(r.debut) : 'dès maintenant')
                    + (r.fin ? ' au ' + _raFmtDate(r.fin) : ' · sans fin');
        var lancee = _escapeHtml(r.date) + (r.modifiee ? '<div style="font-size:0.72rem;color:#94A3B8;">modifiée le ' + _escapeHtml(r.modifiee) + '</div>' : '');
        var remise = (r.remise_pct === null || r.remise_pct === undefined) ? '—'
            : '-' + String(r.remise_pct).replace('.', ',') + ' %';
        h += '<tr>'
           + td(lancee, 'color:#64748B;white-space:nowrap;')
           + td(magasins, 'color:#0F172A;font-weight:600;')
           + td(_escapeHtml(r.liste) + (r.variante ? '<div style="font-size:0.72rem;color:#94A3B8;">' + _escapeHtml(r.variante) + '</div>' : '')
                + (r.societe ? '<div style="font-size:0.72rem;color:#94A3B8;">' + _escapeHtml(r.societe) + '</div>' : ''))
           + td(formatMAD(r.prix_catalogue), 'text-align:right;color:#64748B;white-space:nowrap;')
           + td(r.prix_solde === null || r.prix_solde === undefined ? '—' : formatMAD(r.prix_solde), 'text-align:right;font-weight:700;color:#B91C1C;white-space:nowrap;')
           + td(remise, 'text-align:right;font-weight:700;color:#DC2626;white-space:nowrap;')
           + td(periode, 'white-space:nowrap;')
           + td('<span style="display:inline-block;padding:2px 9px;border-radius:999px;font-size:0.72rem;font-weight:700;color:' + st[1] + ';background:' + st[2] + ';">' + st[0] + '</span>')
           + td(_escapeHtml(r.par), 'color:#64748B;white-space:nowrap;')
           + '</tr>';
    });
    box.innerHTML = h + '</tbody></table></div>';
}

// ══════════════════════════════════════════════════════════════
// Position de la barre de recherche produit
//
// DEMANDE UTILISATEUR (2026-09-21) : dans la vue MOD FOR LIFE, la barre de
// recherche doit se trouver juste avant « Dispatch — société → magasin →
// référence » (elle tombait tout en bas, sous le réassort). La barre est
// commune à toutes les pages : on la DÉPLACE dans la vue MOD FOR LIFE et on
// la remet à sa place d'origine ailleurs, plutôt que de la sortir du HTML
// commun. Déplacer le nœud garde ses écouteurs (recherche, résultats).
// ══════════════════════════════════════════════════════════════
var _searchBarHome = null;

function _placeSearchBar(dansModForLife) {
    var bar = document.querySelector('.search-bar-wrapper');
    if (!bar || !bar.parentNode) return;
    if (!_searchBarHome) {
        // Repère invisible à l'emplacement d'origine, posé une seule fois.
        _searchBarHome = document.createComment('emplacement de la barre de recherche');
        bar.parentNode.insertBefore(_searchBarHome, bar);
    }
    if (dansModForLife) {
        // Page Dépôt : plus de barre de recherche (2026-09-29). Le tableau
        // Dispatch a son propre filtre, et le bloc Réassort le sien : une
        // troisième zone de saisie au-dessus n'ajoutait rien.
        bar.style.display = 'none';
        if (_searchBarHome.parentNode && _searchBarHome.nextSibling !== bar) {
            _searchBarHome.parentNode.insertBefore(bar, _searchBarHome.nextSibling);
        }
    } else {
        bar.style.display = '';
        if (_searchBarHome.parentNode && _searchBarHome.nextSibling !== bar) {
            _searchBarHome.parentNode.insertBefore(bar, _searchBarHome.nextSibling);
        }
    }
}

// ══════════════════════════════════════════════════════════════
// BOUTON « ← RETOUR » DANS TOUTES LES FENÊTRES POP-UP
//
// DEMANDE UTILISATEUR (2026-09-21) : une flèche de retour dans tous les
// pop-up pour revenir en arrière. Le bouton est ajouté à gauche du ✕ dans
// les 12 fenêtres (ajout par JS, pour ne pas dupliquer le balisage).
//
//   ←  revient à la fenêtre précédente : il ferme la fenêtre du dessus ;
//      la plupart des fenêtres s'ouvrent PAR-DESSUS une autre (Fiche →
//      Historique, Transférer, Solder, Couleur ; Écarts → détail), qui
//      réapparaît alors d'elle-même. Quatre enchaînements FERMENT au
//      contraire la fenêtre d'origine (Ruptures, Stock dormant, Liste des
//      soldes → Fiche ; Couleur → Transférer) : ils mémorisent comment la
//      rouvrir (_setRetour) et « ← » la rouvre.
//   ✕  ferme tout et revient au tableau de bord (le retour mémorisé est
//      oublié, comme pour un clic sur le fond).
// ══════════════════════════════════════════════════════════════
var _retourVers = {};   // id de la fenêtre -> fonction qui rouvre celle d'où l'on vient

function _setRetour(overlayId, rouvrir) {
    _retourVers[overlayId] = rouvrir;
}

function _popupFermetures() {
    return {
        'detail-overlay':          closeDetail,
        'solde-overlay':           closeSoldePanel,
        'transfer-overlay':        closeTransferPanel,
        'color-detail-overlay':    closeColorDetail,
        'ruptures-overlay':        closeRuptures,
        'soldes-overlay':          closeSoldes,
        'dormant-overlay':         closeDormant,
        'depot-stock-overlay':     closeDepotStock,
        'valorisation-overlay':    closeValorisationDetail,
        'ecarts-overlay':          closeEcarts,
        'ecart-detail-overlay':    closeEcartDetail,
        'stock-recon-overlay':     closeStockRecon,
        'product-history-overlay': closeProductHistory,
        'ac-reassort-overlay':     closeActionReassort,
        'ac-transferts-overlay':   closeHistoriqueTransferts,
    };
}

function _retourPopup(overlayId) {
    var rouvrir = _retourVers[overlayId];
    delete _retourVers[overlayId];
    var fermer = _popupFermetures()[overlayId];
    if (fermer) {
        fermer();
    } else {
        var o = el(overlayId);
        if (o) o.classList.remove('active');
    }
    if (rouvrir) rouvrir();
}

function _initBoutonsRetour() {
    var fermetures = _popupFermetures();
    Object.keys(fermetures).forEach(function(id) {
        var overlay = el(id);
        if (!overlay) return;
        var croix = overlay.querySelector('.close-detail-btn');
        if (!croix || overlay.querySelector('.back-popup-btn')) return;
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'back-popup-btn';
        b.title = 'Retour';
        b.setAttribute('aria-label', 'Retour à la fenêtre précédente');
        b.textContent = '←';
        b.addEventListener('click', function(ev) {
            ev.stopPropagation();
            _retourPopup(id);
        });
        croix.parentNode.insertBefore(b, croix);
        // ✕ ou clic sur le fond : on quitte, plus de retour mémorisé.
        croix.addEventListener('click', function() { delete _retourVers[id]; });
        overlay.addEventListener('click', function(ev) {
            if (ev.target === overlay) delete _retourVers[id];
        });
    });
}

document.addEventListener('DOMContentLoaded', _initBoutonsRetour);

// ══════════════════════════════════════════════════════════════
// PAGE « ACTION » (menu Dashboard → Action, page=action)
//
// DEMANDE UTILISATRICE (2026-09-22) : le tableau du Top/Flop Produits en
// grand, du top au flop ; une ligne par référence et ses couleurs dessous ;
// Catégorie et Prix après la référence, sans la colonne Produit ; Qté en
// dépôt, puis trois boutons : Transférer, Prix (panneau Solder), Réassort.
// En haut : nombre de lignes, choix des colonnes, et une recherche avec
// Filtres / Regrouper par / Favoris comme dans Odoo.
// Données : /mavie/api/actions (mêmes calculs que le Top/Flop).
// ══════════════════════════════════════════════════════════════

var AC_COLONNES = [
    // cle, libellé, visible par défaut, numérique
    ['rang',          '#',                 true,  false],
    ['photo',         'Photo',             true,  false],
    ['ref',           'Référence',         true,  false],
    ['transferts',    'Transferts',        true,  true],
    ['couleur',       'Couleur',           true,  false],
    ['categorie',     'Catégorie',         true,  false],
    ['prix',          'Prix (TTC)',        true,  true],
    ['produit',       'Produit',           false, false],
    ['collection',    'Collection',        false, false],
    ['ca_achat',      'CA Achat (TTC)',    true,  true],
    ['ca',            'CA Vendu (TTC)',    true,  true],
    ['qty_purchased', 'Qté achetée',       true,  true],
    ['qty_sold',      'Qté vendue',        true,  true],
    ['reste',         'Reste',             true,  true],
    ['stock',         'Stock magasins',    false, true],
    ['depot',         'Qté en dépôt',      true,  true],
    ['action',        'Action',            true,  false],
];
var AC_FILTRES = {
    vendus: 'Vendus', non_vendus: 'Non vendus', en_stock: 'En stock',
    rupture: 'En rupture', depot: 'Disponible au dépôt',
    depot_vide: 'Manquant au dépôt', reste_negatif: 'Reste négatif',
};
var AC_GROUPES = { categorie: 'Catégorie', collection: 'Collection' };

var acState = {
    rows: [], total: 0, q: '', filtres: [], groupe: null,
    ouvertes: {}, colonnes: null, bound: false, seq: 0, societe_id: '', ordre: 'top',
    regions: [], villes: [], magasins: [], lieux: null,
};

// Préférences d'affichage (colonnes, favoris) : propres à ce navigateur,
// donc localStorage, toujours protégé (navigation privée, stockage bloqué).
function _acStore(cle, valeur) {
    try {
        if (valeur === undefined) return JSON.parse(localStorage.getItem('mavie_ac_' + cle) || 'null');
        localStorage.setItem('mavie_ac_' + cle, JSON.stringify(valeur));
    } catch (e) { return null; }
}

function _acColonnesVisibles() {
    if (!acState.colonnes) {
        var saved = _acStore('colonnes');
        acState.colonnes = {};
        AC_COLONNES.forEach(function(c) {
            acState.colonnes[c[0]] = saved && saved[c[0]] !== undefined ? !!saved[c[0]] : c[2];
        });
    }
    return AC_COLONNES.filter(function(c) { return acState.colonnes[c[0]]; });
}

async function loadActions() {
    // Filet : si quoi que ce soit échoue avant l'affichage, on l'écrit dans
    // le tableau au lieu de laisser la page vide (constat 2026-09-24).
    try {
        await _loadActions();
    } catch (e) {
        console.error('Erreur page Actions:', e);
        var tb = el('ac-tbody');
        if (tb) tb.innerHTML = '<tr><td class="ac-empty" style="color:#B91C1C;" colspan="'
            + AC_COLONNES.length + '">Erreur : ' + _escapeHtml(e && e.message || String(e)) + '</td></tr>';
        showLoading(false);
    }
}

async function _loadActions() {
    _acBindOnce();
    var limitEl = el('ac-limit');
    var limit = limitEl ? parseInt(limitEl.value, 10) : 20;
    if (isNaN(limit) || limit < 1) limit = 20;
    var params = getFilterParams();
    params.limit = limit;
    params.q = acState.q;
    params.filtres = acState.filtres;
    params.societe_id = acState.societe_id || '';
    params.ordre = acState.ordre || 'top';
    params.regions = acState.regions;
    params.villes = acState.villes;
    params.magasins = acState.magasins;
    var seq = ++acState.seq;
    var tbody = el('ac-tbody');
    if (tbody && !acState.rows.length) {
        tbody.innerHTML = '<tr><td class="ac-empty" colspan="' + _acColonnesVisibles().length + '">Chargement…</td></tr>';
    }
    showLoading(true);
    var data = await rpc('/mavie/api/actions', params);
    // CORRIGÉ (2026-09-24, « rien ne s'affiche tant que je ne clique pas
    // sur Top → Flop ») : une réponse dépassée était ignorée, et si c'était
    // la seule, le tableau restait vide. On ne l'ignore que si quelque
    // chose est déjà affiché.
    if (seq !== acState.seq && acState.rows.length) return;
    showLoading(false);
    if (!data || data.error) {
        acState.rows = [];
        if (tbody) tbody.innerHTML = '<tr><td class="ac-empty" style="color:#B91C1C;" colspan="'
            + _acColonnesVisibles().length + '">Erreur : ' + _escapeHtml(data && data.error || 'inconnue') + '</td></tr>';
        return;
    }
    acState.rows = data.rows || [];
    acState.total = data.total || 0;
    acState.nbRefs = data.nb_references || 0;
    acState.perimetre = data.perimetre || '';
    acState.depotNom = data.depot_societe || '';
    try {
        _acFillSocietes(data.societes || []);
        if (data.lieux) acState.lieux = data.lieux;
        _acMajOrdre();
        _acRender();
    } catch (e) {
        // Une erreur d'affichage laissait un écran blanc : on la montre.
        console.error('Erreur affichage Actions:', e);
        if (tbody) tbody.innerHTML = '<tr><td class="ac-empty" style="color:#B91C1C;" colspan="'
            + AC_COLONNES.length + '">Erreur d\'affichage : ' + _escapeHtml(e && e.message || String(e)) + '</td></tr>';
    }
}

var AC_NIVEAUX = {
    top:   ['▲', '#16A34A', 'Top : 80 % des ventes'],
    moyen: ['►', '#D97706', 'Moyen'],
    flop:  ['▼', '#DC2626', 'Flop : peu ou pas de ventes'],
};

// Pastilles « ce que cette référence a eu comme action » (demande
// utilisatrice 2026-09-22) : bleu = transfert, rouge = solde active,
// vert = réassort (transfert lancé depuis la fenêtre Réassort). Un clic
// ouvre la fiche produit, où le stock de chaque magasin / couleur porte la
// même couleur.
var AC_ACTIONS = {
    transfert: ['🔄', '#2563EB', '#DBEAFE', function(n) { return n + ' transfert' + (n > 1 ? 's' : ''); }],
    solde:     ['🏷️', '#DC2626', '#FEE2E2', function(n) { return 'en solde dans ' + n + ' magasin' + (n > 1 ? 's' : ''); }],
    reassort:  ['📦', '#16A34A', '#DCFCE7', function(n) { return n + ' réassort' + (n > 1 ? 's' : ''); }],
};
// Bouton « Top → Flop / Flop → Top » : même classement, lu dans l'un ou
// l'autre sens (le rang # reste celui du top).
function _acMajOrdre() {
    var b = el('ac-ordre');
    if (!b) return;
    var flop = acState.ordre === 'flop';
    b.innerHTML = flop ? '<span style="color:#DC2626;">▼</span> Flop → Top' : '<span style="color:#16A34A;">▲</span> Top → Flop';
    b.title = 'Cliquer pour inverser';
}

function _acBadges(actions) {
    if (!actions) return '';
    var h = '';
    ['transfert', 'solde', 'reassort'].forEach(function(k) {
        var n = actions[k] || 0;
        if (!n) return;
        var a = AC_ACTIONS[k];
        // Le badge « solde » compte des MAGASINS, pas des pièces vendues :
        // on l'écrit, sinon un « 7 » se lit comme 7 ventes (constaté sur
        // GJ-5, qui affiche 7 magasins pour une vente nette de −1).
        var suffixe = (k === 'solde') ? ' mag.' : '';
        h += '<button type="button" class="ac-badge" data-ac="fiche" style="color:' + a[1] + ';background:' + a[2] + ';" title="'
           + _escapeHtml(a[3](n)) + ' — voir la fiche">' + a[0] + ' ' + n + suffixe + '</button>';
    });
    return h ? ' <span class="ac-badges">' + h + '</span>' : '';
}

// Menu « Société » de la page : sociétés cochées dans Odoo. Une société
// choisie qui n'est plus cochée retombe sur « Toutes ».
function _acFillSocietes(societes) {
    var sel = el('ac-societe');
    if (!sel) return;
    var ids = societes.map(function(s) { return String(s.id); });
    if (acState.societe_id && ids.indexOf(String(acState.societe_id)) === -1) acState.societe_id = '';
    sel.innerHTML = '<option value="">Toutes les sociétés cochées' + (societes.length > 1 ? ' (' + societes.length + ')' : '') + '</option>'
        + societes.map(function(s) {
            return '<option value="' + s.id + '">' + _escapeHtml(s.name) + '</option>';
        }).join('');
    sel.value = acState.societe_id ? String(acState.societe_id) : '';
}

function _acCell(cle, r, estRef) {
    var v = function(n) { return formatNumber(n || 0); };
    switch (cle) {
        case 'rang':
            if (!estRef) return '';
            // Flèche de niveau (demande utilisatrice : top rouge, moyen
            // jaune, flop vert). Niveau calculé côté serveur en ABC du CA.
            var niv = AC_NIVEAUX[r.niveau] || AC_NIVEAUX.flop;
            return '<span class="ac-rank">' + r.rang + '</span> <span class="ac-niv" style="color:' + niv[1] + ';" title="' + niv[2] + '">' + niv[0] + '</span>';
        case 'photo':
            if (!estRef) return '';
            return r.has_image && r.image_url
                ? '<img class="ac-photo" loading="lazy" src="' + _escapeHtml(r.image_url) + '" alt=""/>'
                : '<div class="ac-nophoto">pas de photo</div>';
        case 'ref':
            return estRef ? '<span class="ac-caret">▾</span> ' + _escapeHtml(r.ref) + _acBadges(r.actions) : '';
        // Transferts. CORRIGÉ le 2026-09-24 : sans magasin choisi, « reçu »
        // et « envoyé » donnaient le même nombre — normal, chaque bon part
        // d'un magasin et arrive dans un autre. On montre donc les pièces
        // réellement déplacées, et le reçu / envoyé seulement quand un
        // magasin, une ville ou une région est filtré.
        case 'transferts':
            if (!estRef) return '';
            var tr2 = r.transferts || {};
            if (!tr2.pieces) return '<span class="ac-muted">—</span>';
            // Une seule colonne (demande utilisatrice 2026-09-24) : les
            // pièces déplacées, ou le reçu et l'envoyé quand un lieu est
            // filtré — là seulement les deux sens sont différents.
            if (!tr2.scope) {
                return '<button type="button" class="ac-btn" data-ac="histo" style="padding:2px 8px;font-size:0.74rem;" '
                     + 'title="Reçues dans ' + formatNumber(tr2.nb_magasins_recu) + ' magasins, envoyées depuis '
                     + formatNumber(tr2.nb_magasins_envoye) + ' — voir le détail">'
                     + formatNumber(tr2.pieces) + ' pcs déplacées</button>';
            }
            var bouts = [];
            if (tr2.recu) bouts.push('<button type="button" class="ac-btn" data-ac="histo-recu" '
                + 'style="padding:2px 8px;font-size:0.74rem;color:#1D4ED8;" title="Reçu par ces magasins — voir les bons">+ '
                + formatNumber(tr2.recu) + '</button>');
            if (tr2.envoye) bouts.push('<button type="button" class="ac-btn" data-ac="histo-envoye" '
                + 'style="padding:2px 8px;font-size:0.74rem;color:#3730A3;" title="Envoyé par ces magasins — voir les bons">− '
                + formatNumber(tr2.envoye) + '</button>');
            return bouts.length ? '<div class="ac-actions" style="justify-content:flex-end;">' + bouts.join('') + '</div>'
                                : '<span class="ac-muted">—</span>';
        case 'couleur':
            return estRef ? '' : '<span class="mfl-color">' + _escapeHtml(r.couleur) + '</span>';
        case 'categorie': return estRef ? '<span class="ac-cat">' + _escapeHtml(r.categorie) + '</span>' : '';
        case 'prix': return estRef ? formatMAD(r.prix) : '';
        case 'produit': return estRef ? _escapeHtml(r.name) : '';
        case 'collection': return estRef ? _escapeHtml(r.collection) : '';
        case 'ca_achat':
            // Acheté sans prix saisi sur les commandes : « non renseigné »,
            // comme dans le Top/Flop (pas un achat à 0 MAD).
            if (!r.ca_achat && r.qty_purchased) return '<span class="ac-muted" title="Aucun prix d\'achat saisi sur les commandes fournisseur">non renseigné</span>';
            return formatMAD(r.ca_achat || 0);
        case 'ca': return formatMAD(r.ca || 0);
        case 'qty_purchased': return v(r.qty_purchased);
        case 'qty_sold': return v(r.qty_sold);
        case 'reste':
            var reste = (r.qty_purchased || 0) - (r.qty_sold || 0);
            return '<span class="' + (reste < 0 ? 'ac-neg' : '') + '">' + formatNumber(reste) + '</span>';
        case 'stock': return '<span class="' + (r.stock < 0 ? 'ac-neg' : '') + '">' + v(r.stock) + '</span>';
        case 'depot': return r.depot ? v(r.depot) : '<span class="ac-muted">0</span>';
        case 'action':
            // Mêmes boutons sur la référence (toutes couleurs) et sur chaque
            // couleur (demande utilisatrice : « si je veux choisir la
            // variante ? ») : la couleur est alors présélectionnée.
            var quoi = estRef ? 'toutes couleurs' : r.couleur;
            return '<div class="ac-actions">'
                + '<button type="button" class="ac-btn" data-ac="transfer" title="Transférer · ' + _escapeHtml(quoi) + '">🔄 Transférer</button>'
                + '<button type="button" class="ac-btn" data-ac="solde" title="Solder · ' + _escapeHtml(quoi) + '">🏷️ Solder</button>'
                + '<button type="button" class="ac-btn" data-ac="reassort" title="Réassort · ' + _escapeHtml(quoi) + '">📦 Réassort</button>'
                + '</div>';
    }
    return '';
}

function _acLignesRef(r, cols) {
    // Repliée par défaut (demande utilisatrice 2026-09-22 : « le tableau
    // reste comme ça, et si je clique sur une référence j'ai ses variantes »).
    var ferme = !acState.ouvertes[r.id];
    var h = '<tr class="ac-ref' + (ferme ? ' closed' : '') + '" data-id="' + r.id + '">';
    cols.forEach(function(c) {
        h += '<td class="' + (c[3] ? 'num' : '') + '">' + _acCell(c[0], r, true) + '</td>';
    });
    h += '</tr>';
    if (!ferme) {
        (r.variantes || []).forEach(function(vr) {
            h += '<tr class="ac-var" data-parent="' + r.id + '" data-couleur="' + _escapeHtml(vr.couleur) + '">';
            cols.forEach(function(c) {
                var cls = (c[3] ? 'num' : '') + (c[0] === 'couleur' ? ' ac-color-cell' : '');
                h += '<td class="' + cls + '">' + _acCell(c[0], vr, false) + '</td>';
            });
            h += '</tr>';
        });
    }
    return h;
}

function _acRender() {
    var cols = _acColonnesVisibles();
    var thead = el('ac-thead');
    if (thead) {
        thead.innerHTML = cols.map(function(c) {
            return '<th class="' + (c[3] ? 'num' : '') + '">' + _escapeHtml(c[1]) + '</th>';
        }).join('');
    }
    var count = el('ac-count');
    if (count) {
        count.textContent = formatNumber(Math.min(acState.rows.length, acState.total)) + ' / '
            + formatNumber(acState.total) + ' références' + (acState.perimetre ? ' · ' + acState.perimetre : '');
        count.title = 'Classement calculé pour : ' + (acState.perimetre || '—');
    }
    var tbody = el('ac-tbody');
    if (!tbody) return;
    if (!acState.rows.length) {
        tbody.innerHTML = '<tr><td class="ac-empty" colspan="' + cols.length + '">'
            + (acState.q || acState.filtres.length ? 'Aucune référence ne correspond à cette recherche.' : 'Aucune référence sur cette période.')
            + '</td></tr>';
    } else if (acState.groupe) {
        // Regroupement sur les lignes affichées, dans l'ordre top → flop
        // de la première référence de chaque groupe.
        var groupes = [], index = {};
        acState.rows.forEach(function(r) {
            var k = r[acState.groupe] || '—';
            if (!(k in index)) { index[k] = groupes.length; groupes.push({ nom: k, rows: [] }); }
            groupes[index[k]].rows.push(r);
        });
        var h = '';
        groupes.forEach(function(g) {
            var ca = g.rows.reduce(function(a, r) { return a + (r.ca || 0); }, 0);
            var qte = g.rows.reduce(function(a, r) { return a + (r.qty_sold || 0); }, 0);
            h += '<tr class="ac-grp"><td colspan="' + cols.length + '">' + _escapeHtml(g.nom)
               + '<span>' + formatNumber(g.rows.length) + ' réf. · ' + formatNumber(qte) + ' vendues · ' + formatMAD(ca) + '</span></td></tr>';
            g.rows.forEach(function(r) { h += _acLignesRef(r, cols); });
        });
        tbody.innerHTML = h;
    } else {
        tbody.innerHTML = acState.rows.map(function(r) { return _acLignesRef(r, cols); }).join('');
    }
    var foot = el('ac-foot');
    if (foot) {
        // Phrases courtes (demande utilisatrice 2026-09-23).
        foot.innerHTML = 'Classé par ventes, ' + (acState.ordre === 'flop' ? 'du flop au top' : 'du top au flop') + '.'
            + '<br>▲ top · ► moyen · ▼ flop.'
            + '<br>🔄 transferts · 🏷️ en solde · 📦 réassorts.'
            + '<br>Dépôt = stock ' + _escapeHtml(acState.depotNom || 'de la société entrepôt') + '.'
            + '<br>Cliquez une référence pour voir ses couleurs.';
    }
    // Photo annoncée mais fichier absent du serveur : même cadre que « pas
    // de photo », au lieu de l'icône cassée du navigateur.
    Array.prototype.forEach.call(tbody.querySelectorAll('img.ac-photo'), function(img) {
        img.onerror = function() {
            var vide = document.createElement('div');
            vide.className = 'ac-nophoto';
            vide.textContent = 'pas de photo';
            vide.title = 'La photo est enregistrée dans Odoo mais le fichier est introuvable sur le serveur.';
            if (img.parentNode) img.parentNode.replaceChild(vide, img);
        };
    });
    _acRenderFacets();
    _acRenderPanels();
}

function _acRenderFacets() {
    var box = el('ac-facets');
    if (!box) return;
    var h = '';
    if (acState.filtres.length) {
        h += '<span class="ac-facet"><b>⏷</b> ' + acState.filtres.map(function(f) { return _escapeHtml(AC_FILTRES[f]); }).join(' ou ')
           + '<button type="button" data-clear="filtres" title="Retirer">×</button></span>';
    }
    var lieuxActifs = (acState.regions || []).concat(acState.villes || [])
        .concat((acState.magasins || []).map(function(sf) {
            var m = ((acState.lieux || {}).magasins || []).filter(function(x) { return x.shop_field === sf; })[0];
            return m ? m.nom : sf;
        }));
    if (lieuxActifs.length) {
        h += '<span class="ac-facet"><b>📍</b> ' + _escapeHtml(lieuxActifs.join(', '))
           + '<button type="button" data-clear="lieux" title="Retirer">×</button></span>';
    }
    if (acState.groupe) {
        h += '<span class="ac-facet grp"><b>☰</b> ' + _escapeHtml(AC_GROUPES[acState.groupe])
           + '<button type="button" data-clear="groupe" title="Retirer">×</button></span>';
    }
    box.innerHTML = h;
}

// Filtres Lieux : régions, villes, magasins (demande 2026-09-23).
function _acRenderLieux() {
    var lieux = acState.lieux || {};
    function liste(hote, valeurs, cle, libelle) {
        var box = el(hote);
        if (!box) return;
        box.innerHTML = (valeurs || []).map(function(v) {
            var val = libelle ? v[cle] : v;
            var txt = libelle ? libelle(v) : v;
            var on = (acState[hote === 'ac-regions' ? 'regions' : (hote === 'ac-villes' ? 'villes' : 'magasins')] || [])
                .indexOf(val) !== -1;
            return '<div class="ac-item' + (on ? ' on' : '') + '" data-lieu="' + hote + '" data-val="'
                 + _escapeHtml(val) + '">' + _escapeHtml(txt) + '</div>';
        }).join('') || '<div class="ac-fav-empty">—</div>';
    }
    liste('ac-regions', lieux.regions);
    liste('ac-villes', lieux.villes);
    // Aucune ville n'est renseignée sur les magasins (Base Pivot →
    // Configuration → Mapping Magasins) : les blocs Région et Ville ne
    // peuvent rien proposer. On les masque plutôt que d'afficher « — »,
    // et une ligne d'aide dit où les remplir (demande du 2026-09-25).
    var aucuneVille = !((lieux.villes || []).length);
    ['ac-regions', 'ac-villes'].forEach(function(id) {
        var box = el(id);
        if (!box) return;
        box.style.display = aucuneVille ? 'none' : '';
        // Le titre « Région » / « Ville » est le frère juste avant.
        var titre = box.previousElementSibling;
        if (titre && titre.style) titre.style.display = aucuneVille ? 'none' : '';
    });
    var aide = el('ac-lieux-aide');
    if (aide) {
        aide.style.display = aucuneVille ? '' : 'none';
        aide.textContent = aucuneVille
            ? 'Région et ville : renseignez la ville des magasins dans Base Pivot → Configuration → Mapping Magasins pour filtrer par zone.'
            : '';
    }
    liste('ac-magasins', lieux.magasins, 'shop_field', function(m) {
        return m.nom + (m.ville ? ' · ' + m.ville : '');
    });
}

function _acRenderPanels() {
    _acRenderLieux();
    var panel = el('ac-search-panel');
    if (panel) {
        panel.querySelectorAll('[data-filtre]').forEach(function(it) {
            it.classList.toggle('on', acState.filtres.indexOf(it.getAttribute('data-filtre')) !== -1);
        });
        panel.querySelectorAll('[data-group]').forEach(function(it) {
            it.classList.toggle('on', acState.groupe === it.getAttribute('data-group'));
        });
    }
    var favs = el('ac-favs');
    if (favs) {
        var list = _acStore('favoris') || [];
        favs.innerHTML = list.length ? list.map(function(f, i) {
            return '<div class="ac-item ac-fav" data-fav="' + i + '"><span>' + _escapeHtml(f.nom) + '</span>'
                 + '<button type="button" data-fav-del="' + i + '" title="Supprimer ce favori">🗑</button></div>';
        }).join('') : '<div class="ac-fav-empty">Aucune recherche enregistrée.</div>';
    }
    var cp = el('ac-cols-panel');
    if (cp) {
        _acColonnesVisibles();
        cp.innerHTML = AC_COLONNES.map(function(c) {
            return '<label><input type="checkbox" data-col="' + c[0] + '"' + (acState.colonnes[c[0]] ? ' checked' : '') + '/> '
                 + _escapeHtml(c[1] === '#' ? 'Rang (#)' : c[1]) + '</label>';
        }).join('');
    }
}

function _acTogglePanel(panelId, btnId, forcer) {
    var p = el(panelId), b = el(btnId);
    if (!p) return;
    var open = forcer !== undefined ? forcer : !p.classList.contains('open');
    p.classList.toggle('open', open);
    if (b) b.classList.toggle('open', open);
}

function _acBindOnce() {
    if (acState.bound) return;
    acState.bound = true;

    var limitEl = el('ac-limit');
    if (limitEl) {
        var saved = _acStore('limit');
        if (saved) limitEl.value = saved;
        var t1 = null;
        limitEl.addEventListener('input', function() {
            clearTimeout(t1);
            t1 = setTimeout(function() { _acStore('limit', parseInt(limitEl.value, 10) || 20); loadActions(); }, 450);
        });
    }

    var ordreBtn = el('ac-ordre');
    if (ordreBtn) ordreBtn.addEventListener('click', function() {
        acState.ordre = acState.ordre === 'flop' ? 'top' : 'flop';
        _acMajOrdre();
        loadActions();
    });

    var socSel = el('ac-societe');
    if (socSel) socSel.addEventListener('change', function() {
        acState.societe_id = socSel.value;
        loadActions();
    });

    var q = el('ac-q');
    if (q) {
        var t2 = null;
        q.addEventListener('input', function() {
            clearTimeout(t2);
            t2 = setTimeout(function() { acState.q = q.value.trim(); loadActions(); }, 350);
        });
        q.addEventListener('keydown', function(e) {
            // Retour arrière dans une recherche vide : retire la dernière
            // étiquette, comme la barre de recherche d'Odoo.
            if (e.key === 'Backspace' && !q.value) {
                if (acState.groupe) { acState.groupe = null; _acRender(); }
                else if (acState.filtres.length) { acState.filtres.pop(); loadActions(); }
            }
        });
    }

    var toggle = el('ac-search-toggle');
    if (toggle) toggle.addEventListener('click', function(e) {
        e.stopPropagation();
        _acTogglePanel('ac-cols-panel', 'ac-cols-toggle', false);
        _acTogglePanel('ac-search-panel', 'ac-search-toggle');
    });
    var colsBtn = el('ac-cols-toggle');
    if (colsBtn) colsBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        _acTogglePanel('ac-search-panel', 'ac-search-toggle', false);
        _acTogglePanel('ac-cols-panel', 'ac-cols-toggle');
    });
    document.addEventListener('click', function(e) {
        if (!e.target.closest) return;
        if (!e.target.closest('#ac-search-panel') && !e.target.closest('#ac-search-toggle')) _acTogglePanel('ac-search-panel', 'ac-search-toggle', false);
        if (!e.target.closest('#ac-cols-panel') && !e.target.closest('#ac-cols-toggle')) _acTogglePanel('ac-cols-panel', 'ac-cols-toggle', false);
    });

    var panel = el('ac-search-panel');
    if (panel) panel.addEventListener('click', function(e) {
        var t = e.target;
        var del = t.closest('[data-fav-del]');
        if (del) {
            e.stopPropagation();
            var list = _acStore('favoris') || [];
            list.splice(parseInt(del.getAttribute('data-fav-del'), 10), 1);
            _acStore('favoris', list);
            _acRenderPanels();
            return;
        }
        var fav = t.closest('[data-fav]');
        if (fav) {
            var f = (_acStore('favoris') || [])[parseInt(fav.getAttribute('data-fav'), 10)];
            if (f) {
                acState.q = f.q || ''; acState.filtres = (f.filtres || []).slice(); acState.groupe = f.groupe || null;
                if (q) q.value = acState.q;
                _acTogglePanel('ac-search-panel', 'ac-search-toggle', false);
                loadActions();
            }
            return;
        }
        var fi = t.closest('[data-filtre]');
        if (fi) {
            var k = fi.getAttribute('data-filtre');
            var i = acState.filtres.indexOf(k);
            if (i === -1) acState.filtres.push(k); else acState.filtres.splice(i, 1);
            loadActions();
            return;
        }
        var lieu = t.closest('[data-lieu]');
        if (lieu) {
            var hote = lieu.getAttribute('data-lieu');
            var cleL = hote === 'ac-regions' ? 'regions' : (hote === 'ac-villes' ? 'villes' : 'magasins');
            var valL = lieu.getAttribute('data-val');
            var iL = acState[cleL].indexOf(valL);
            if (iL === -1) acState[cleL].push(valL); else acState[cleL].splice(iL, 1);
            loadActions();
            return;
        }
        var gr = t.closest('[data-group]');
        if (gr) {
            var g = gr.getAttribute('data-group');
            acState.groupe = acState.groupe === g ? null : g;
            _acRender();
        }
    });

    var favBtn = el('ac-fav-save-btn');
    if (favBtn) favBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        var nomEl = el('ac-fav-name');
        var nom = nomEl ? nomEl.value.trim() : '';
        if (!nom) { if (nomEl) nomEl.focus(); return; }
        var list = _acStore('favoris') || [];
        list.push({ nom: nom, q: acState.q, filtres: acState.filtres.slice(), groupe: acState.groupe });
        _acStore('favoris', list);
        if (nomEl) nomEl.value = '';
        _acRenderPanels();
    });

    var facets = el('ac-facets');
    if (facets) facets.addEventListener('click', function(e) {
        var b = e.target.closest('[data-clear]');
        if (!b) return;
        var quoi = b.getAttribute('data-clear');
        if (quoi === 'groupe') { acState.groupe = null; _acRender(); }
        else if (quoi === 'lieux') { acState.regions = []; acState.villes = []; acState.magasins = []; loadActions(); }
        else { acState.filtres = []; loadActions(); }
    });

    var cp = el('ac-cols-panel');
    if (cp) cp.addEventListener('change', function(e) {
        var c = e.target.getAttribute && e.target.getAttribute('data-col');
        if (!c) return;
        acState.colonnes[c] = e.target.checked;
        _acStore('colonnes', acState.colonnes);
        _acRender();
    });

    var tbody = el('ac-tbody');
    if (tbody) tbody.addEventListener('click', function(e) {
        var tr = e.target.closest('tr.ac-ref, tr.ac-var');
        if (!tr) return;
        var estRef = tr.classList.contains('ac-ref');
        var id = parseInt(tr.getAttribute(estRef ? 'data-id' : 'data-parent'), 10);
        var r = acState.rows.filter(function(x) { return x.id === id; })[0];
        if (!r) return;
        // Ligne couleur : la couleur choisie ; « — » = variante sans couleur.
        var couleur = estRef ? null : tr.getAttribute('data-couleur');
        if (couleur === '—') couleur = null;
        var btn = e.target.closest('[data-ac]');
        if (btn) {
            e.stopPropagation();
            var a = btn.getAttribute('data-ac');
            if (a === 'transfer') openTransferPanel(r.id, r.name, couleur, null, (r.variantes || []).map(function(x) { return x.couleur; }));
            else if (a === 'solde') openSoldePanel(r.id, couleur);
            else if (a === 'reassort') openActionReassort(r, couleur);
            else if (a === 'fiche') openDetail(r.id, r.name);
            else if (a === 'histo') openHistoriqueTransferts(r);
            else if (a === 'histo-recu') openHistoriqueTransferts(r, 'recu');
            else if (a === 'histo-envoye') openHistoriqueTransferts(r, 'envoye');
            return;
        }
        if (!estRef) return;
        acState.ouvertes[id] = !acState.ouvertes[id];
        _acRender();
    });

    var closeTr = el('close-ac-transferts-btn');
    if (closeTr) closeTr.addEventListener('click', closeHistoriqueTransferts);
    var trOv = el('ac-transferts-overlay');
    if (trOv) trOv.addEventListener('click', function(e) { if (e.target === trOv) closeHistoriqueTransferts(); });

    var closeRa = el('close-ac-reassort-btn');
    if (closeRa) closeRa.addEventListener('click', closeActionReassort);
    var raOv = el('ac-reassort-overlay');
    if (raOv) raOv.addEventListener('click', function(e) { if (e.target === raOv) closeActionReassort(); });
}

// ── Réassort d'une référence, depuis la page Action ──
// REFAIT le 2026-09-24 : la fenêtre ne montrait que les magasins en alerte,
// donc souvent rien. Elle montre maintenant TOUS les magasins, avec ce que
// le dépôt peut envoyer, et prépare les documents Odoo en brouillon :
// bon d'achat fournisseur si le dépôt manque, bons de vente inter-sociétés
// sinon.
var raArticle = { data: null, qtes: {} };

async function openActionReassort(r, couleur, taille, base, whId) {
    var ov = el('ac-reassort-overlay');
    if (!ov) return;
    var refEl = el('ac-ra-ref');
    if (refEl) refEl.textContent = r.ref + (couleur ? ' — ' + couleur : '')
        + (taille ? ' — ' + taille : '');
    var body = el('ac-ra-body');
    if (body) body.innerHTML = '<div class="ac-empty">Calcul du réassort…</div>';
    ov.classList.add('active');
    // Ouverte depuis le bloc Réassort, la fenêtre doit appliquer les
    // réglages de ce bloc (fenêtre de vente, couverture cible) et porter
    // sur la seule pointure de la ligne : c'est une PROPOSITION, elle doit
    // arriver déjà chiffrée.
    var params = {
        article_id: r.id, couleur: couleur || '', taille: taille || '',
        shop_field: state.shop_field, societe_id: acState.societe_id || '',
    };
    if (taille) {
        var rp = _raParams();
        params.fenetre = rp.fenetre;
        params.cible = rp.cible;
        params.delai = rp.delai;
        params.plafond = rp.plafond;
        params.base = base || 'cible';
    }
    var data = await rpc('/mavie/api/reassort-article', params);
    if (!body) return;
    if (!data || data.error) {
        body.innerHTML = '<div class="ac-empty" style="color:#B91C1C;">Erreur : '
            + _escapeHtml((data && data.error) || 'inconnue') + '</div>';
        return;
    }
    data._article = r;
    data._couleur = couleur || '';
    raArticle.data = data;
    raArticle.qtes = {};
    // Le magasin de la ligne cliquée : on le surligne et on dit ce qui lui
    // arrive, sinon on ne comprend pas pourquoi la proposition sert un
    // autre magasin que celui qu'on regardait.
    raArticle.wh = whId || 0;
    (data.magasins || []).forEach(function(m) { raArticle.qtes[m.wh_id] = m.propose; });
    _raArticleRender();
}

function _raArticleRender() {
    var data = raArticle.data || {};
    var body = el('ac-ra-body');
    var sub = el('ac-ra-sub');
    if (sub) {
        var propose = Object.keys(raArticle.qtes).reduce(
            function(a, k) { return a + (raArticle.qtes[k] || 0); }, 0);
        sub.textContent = (data.nom || '')
            + (data.taille ? ' · pointure ' + data.taille : '')
            + ' · ' + formatNumber(data.depot) + ' pièces au dépôt'
            + ' · besoin ' + formatNumber(data.besoin_total) + ' pcs pour tenir '
            + formatNumber(data.jours_couverture || data.fenetre) + ' jours'
            + ' · proposition déjà remplie : ' + formatNumber(propose) + ' pcs'
            + (data.fournisseur ? ' · fournisseur ' + data.fournisseur : '');
    }
    if (!body) return;
    var aEnvoyer = Object.keys(raArticle.qtes).reduce(function(a, k) { return a + (raArticle.qtes[k] || 0); }, 0);
    var h = '<div class="ac-ra-kpis">'
          + '<div class="ac-ra-kpi"><b>' + formatNumber(data.depot) + '</b><span>pièces au dépôt</span></div>'
          + '<div class="ac-ra-kpi"><b>' + formatNumber(data.besoin_total) + '</b><span>besoin des magasins</span></div>'
          + '<div class="ac-ra-kpi"><b>' + formatNumber(data.manque_depot) + '</b><span>manquant au dépôt</span></div>'
          + '</div>';
    h += '<div class="ac-table-wrap" style="max-height:44vh;"><table class="ac-table"><thead><tr>'
       + '<th>Magasin</th><th>Société</th><th class="num" title="Stock de ce magasin, pas celui du dépôt">Stock magasin</th>'
       + '<th class="num">Vendu (' + data.fenetre + ' j)</th>'
       + '<th class="num">Besoin</th><th class="num">À envoyer</th></tr></thead><tbody>';
    (data.magasins || []).forEach(function(m) {
        var origine = raArticle.wh && m.wh_id === raArticle.wh;
        h += '<tr class="ac-var"' + (origine ? ' style="background:#F5F3FF;"' : '') + '>'
           + '<td><strong>' + _escapeHtml(m.magasin) + '</strong>'
           + (origine ? ' <span class="mfl-code" title="Le magasin de la ligne que vous avez cliqu\u00e9e">votre ligne</span>' : '')
           + '</td>'
           + '<td class="ac-muted">' + _escapeHtml(m.societe) + '</td>'
           + '<td class="num"' + (m.stock < 0 ? ' style="color:#DC2626;"' : '') + '>' + formatNumber(m.stock) + '</td>'
           + '<td class="num">' + formatNumber(m.vendu) + '</td>'
           + '<td class="num">' + (m.besoin ? formatNumber(m.besoin) : '<span class="ac-muted">—</span>') + '</td>'
           + '<td class="num"><input type="number" min="0" step="1" data-ra-wh="' + m.wh_id + '" value="'
           + (raArticle.qtes[m.wh_id] || 0) + '" style="width:72px;text-align:right;border:1px solid var(--border);'
           + 'border-radius:6px;padding:3px 6px;font-family:inherit;"/></td></tr>';
    });
    h += '</tbody></table></div>';
    // Le bouton dépend de l'endroit où est le besoin, pas d'un choix à
    // faire : on ne propose l'achat fournisseur que si le dépôt ne peut
    // pas suivre, et la vente inter-sociétés que s'il a de quoi envoyer.
    h += '<div class="sd-foot" style="margin-top:12px;">'
       + '<div class="sd-summary" id="ac-ra-resume"></div>'
       + '<div class="ac-actions">'
       + '<button type="button" class="ac-btn" id="btn-ra-achat" title="Base Pivot génère le bon d\'achat fournisseur dans la société dépôt">'
       + '🛒 Commander au fournisseur</button>'
       + '<button type="button" class="ac-btn" id="btn-ra-bon" title="Voir le bon de réassort en PDF, sans rien générer">📄 Voir le bon</button>'
       + '<button type="button" class="ac-btn ac-btn-primary" id="btn-ra-vente" title="Affiche le bon en PDF, puis Base Pivot génère les ventes inter-sociétés du dépôt vers les magasins">'
       + '🚚 Envoyer depuis le dépôt</button>'
       + '</div></div>';
    h += '<div class="ac-foot">Les documents sont générés par Base Pivot, comme depuis son écran.</div>';
    h += '<div id="ac-ra-result" style="margin-top:10px;"></div>';
    body.innerHTML = h;

    _raMajActions();
    body.querySelectorAll('[data-ra-wh]').forEach(function(inp) {
        inp.addEventListener('input', function() {
            raArticle.qtes[inp.getAttribute('data-ra-wh')] = Math.max(0, parseInt(inp.value, 10) || 0);
            _raMajActions();
        });
    });
    var btnA = el('btn-ra-achat');
    if (btnA) btnA.addEventListener('click', function() { _raGenerer('achat'); });
    var btnV = el('btn-ra-vente');
    // Le bon s'ouvre dans _raGenerer, APRÈS la confirmation : ouvrir un
    // onglet avant le window.confirm mettait la page en arrière-plan, où
    // Chrome renvoie false sans rien demander — la génération était
    // annulée en silence.
    if (btnV) btnV.addEventListener('click', function() { _raGenerer('vente'); });
    var btnBon = el('btn-ra-bon');
    if (btnBon) btnBon.addEventListener('click', function() { _raOuvrirBon('vente'); });
}

// Le bon de réassort en PDF : il décrit ce qui VA partir, à partir des
// quantités affichées. Rien n'est créé par cette ouverture.
function _raOuvrirBon(mode, quantite) {
    var data = raArticle.data || {};
    var lignes = Object.keys(raArticle.qtes)
        .filter(function(k) { return raArticle.qtes[k] > 0; })
        .map(function(k) { return k + ':' + raArticle.qtes[k]; })
        .join(',');
    // Un achat peut n'avoir aucune répartition par magasin : la quantité
    // saisie suffit, le bon porte alors une seule ligne.
    if (!lignes && !(mode === 'achat' && quantite > 0)) return;
    var p = new URLSearchParams({
        article_id: data.article_id || '',
        couleur: data._couleur || '',
        taille: data.taille || '',
        lignes: lignes,
        mode: mode || 'vente',
        quantite: quantite || 0,
        fenetre: (data.fenetre || 90),
        cible: (data.jours_couverture || 30),
        base: 'cible',
    });
    window.open('/mavie/reassort/bon?' + p.toString(), '_blank');
}

// Quel bouton proposer, et quoi écrire au-dessus. Refait à chaque
// saisie : demander plus que le stock du dépôt fait apparaître l'achat.
function _raMajActions() {
    var data = raArticle.data || {};
    var depot = data.depot || 0;
    var somme = Object.keys(raArticle.qtes).reduce(
        function(a, k) { return a + (raArticle.qtes[k] || 0); }, 0);
    var manque = Math.max(data.manque_depot || 0, somme - depot);
    var envoyable = Math.min(somme, depot);

    var btnA = el('btn-ra-achat');
    var btnV = el('btn-ra-vente');
    if (btnA) btnA.style.display = manque > 0 ? '' : 'none';
    if (btnV) btnV.style.display = envoyable > 0 ? '' : 'none';
    var btnBon = el('btn-ra-bon');
    if (btnBon) btnBon.style.display = somme > 0 ? '' : 'none';

    var res = el('ac-ra-resume');
    if (!res) return;

    // Cas du bloc « À surveiller » : le dépôt a bien quelque chose, mais
    // pas assez, et ses pièces partent au magasin qui vend le plus vite.
    var note = '';
    if (raArticle.wh) {
        var mien = (data.magasins || []).filter(function(m) { return m.wh_id === raArticle.wh; })[0];
        if (mien && !(raArticle.qtes[raArticle.wh] > 0)) {
            var servis = (data.magasins || []).filter(function(m) {
                return (raArticle.qtes[m.wh_id] || 0) > 0;
            }).map(function(m) { return m.magasin; });
            note = servis.length
                ? '<div style="margin-top:4px;color:#B45309;">' + _escapeHtml(mien.magasin)
                  + ' n\u2019est pas servi : le d\u00e9p\u00f4t n\u2019a que '
                  + formatNumber(depot) + ' pi\u00e8ce' + (depot > 1 ? 's' : '')
                  + ', elles vont \u00e0 ' + _escapeHtml(servis.join(', '))
                  + ', qui vend' + (servis.length > 1 ? 'ent' : '')
                  + ' plus vite. Vous pouvez forcer une quantit\u00e9 sur sa ligne.</div>'
                : '';
        }
    }
    if (envoyable > 0 && manque > 0) {
        res.innerHTML = '<b>' + formatNumber(envoyable) + '</b> pièces partent du dépôt, '
            + '<b>' + formatNumber(manque) + '</b> manquent — à commander au fournisseur.' + note;
    } else if (envoyable > 0) {
        res.innerHTML = 'Le dépôt a tout ce qu’il faut : <b>' + formatNumber(envoyable)
            + '</b> pièces à envoyer aux magasins.' + note;
    } else if (manque > 0) {
        res.innerHTML = 'Le dépôt n’a rien à envoyer : <b>' + formatNumber(manque)
            + '</b> pièces à commander au fournisseur.' + note;
    } else {
        res.textContent = 'Rien à envoyer : aucun magasin n’est en besoin sur cette ligne.';
    }
}

async function _raGenerer(mode) {
    var data = raArticle.data || {};
    var res = el('ac-ra-result');
    var params = { article_id: data.article_id, couleur: data._couleur || '',
                   taille: data.taille || '', mode: mode };
    var msg;
    // Les quantités saisies dans la colonne « À envoyer » suffisent : plus
    // de seconde question (demande utilisatrice 2026-09-24).
    params.lignes = Object.keys(raArticle.qtes)
        .filter(function(k) { return raArticle.qtes[k] > 0; })
        .map(function(k) { return { wh_id: parseInt(k, 10), qty: raArticle.qtes[k] }; });
    var total = params.lignes.reduce(function(a, l) { return a + l.qty; }, 0);
    if (mode === 'achat') {
        if (!total) {
            // Le dépôt ne peut rien envoyer, donc la colonne « À envoyer »
            // est à zéro partout : ce qu'on commande, c'est le BESOIN de
            // chaque magasin. On ne le demande pas, on le sait — l'ancienne
            // boîte window.prompt était bloquée dans l'iframe et le bouton
            // semblait ne rien faire.
            params.lignes = (data.magasins || [])
                .filter(function(m) { return (m.besoin || 0) > 0; })
                .map(function(m) { return { wh_id: m.wh_id, qty: m.besoin }; });
            total = params.lignes.reduce(function(a, l) { return a + l.qty; }, 0);
        }
        if (!total) {
            if (res) res.innerHTML = '<div class="sd-result ko" style="display:block;">'
                + 'Aucun magasin n\u2019a de besoin sur cette ligne : rien \u00e0 commander.</div>';
            return;
        }
        msg = 'Créer le bon d\'achat fournisseur '
            + (data.fournisseur ? '(' + data.fournisseur + ') ' : '')
            + 'pour ' + total + ' pièces ? Le document est créé directement dans Odoo.';
    } else {
        if (!params.lignes.length) {
            if (res) res.innerHTML = '<div class="sd-result ko" style="display:block;">'
                + 'Indiquez au moins une quantit\u00e9 \u00e0 envoyer.</div>';
            return;
        }
        msg = 'Créer les ventes inter-sociétés vers ' + params.lignes.length
            + ' magasin(s) pour ' + total + ' pièces ? Bons de vente confirmés et '
            + 'livraisons créés directement dans Odoo.';
    }
    if (!window.confirm(msg)) return;
    // Le bon part maintenant que c'est confirmé — pour l'envoi comme pour
    // la commande fournisseur : on le voit pendant que Base Pivot crée les
    // documents.
    _raOuvrirBon(mode, params.quantite || 0);
    if (res) res.innerHTML = '<div class="ac-empty">Création…</div>';
    var out = await rpc('/mavie/api/reassort-generer', params);
    if (!res) return;
    if (!out || out.error) {
        res.innerHTML = '<div class="sd-result ko" style="display:block;">' + _escapeHtml((out && out.error) || 'Erreur inconnue') + '</div>';
        return;
    }
    var lignes = (out.documents || []).map(function(d) {
        return '✓ <b>' + _escapeHtml(d.document) + '</b> — ' + _escapeHtml(d.partenaire || '')
             + ' · ' + formatNumber(d.quantite) + ' pcs · ' + _escapeHtml(d.etat || '');
    });
    res.innerHTML = '<div class="sd-result ok" style="display:block;"><b>'
        + lignes.length + ' document' + (lignes.length > 1 ? 's' : '') + ' créé'
        + (lignes.length > 1 ? 's' : '') + ' dans Odoo.</b><br/>'
        + lignes.join('<br/>')
        + '<br/><span style="color:#475569;">'
        + (mode === 'achat' ? 'Achats → Commandes' : 'Ventes → Commandes')
        + ' · batch Base Pivot : ' + _escapeHtml(out.batch || '')
        + '</span></div>';
}

// ══════════════════════════════════════════════════════════════
// PAGE PROPOSITIONS — bloc « Soldes » : ce qu'il faut démarquer, et de
// combien. Le bloc « Transferts » viendra à côté.
//
// Le module savait déjà appliquer une solde sur UNE référence ; cette
// page répond à la question d'avant : laquelle, et dans quel ordre. Le
// classement se fait par valeur immobilisée, pour traiter d'abord ce qui
// coûte le plus cher à garder en rayon.
// ══════════════════════════════════════════════════════════════
var spState = { data: null, rows: [], seq: 0, bound: false, sel: {}, remises: {},
                mags: {} };

// ── Assistant a questions fermees ──────────────────────────────────
// Pas de modele de langage : chaque question interroge les memes moteurs
// que les tableaux. La reponse est chiffree et reproductible.
var asState = { questions: [], active: null };

async function _asCharger() {
    if (asState.questions.length) return;
    var d = await rpc('/mavie/api/assistant', {});
    asState.questions = (d && d.questions) || [];
    var hote = el('as-questions');
    if (!hote) return;
    hote.innerHTML = asState.questions.map(function(q) {
        return '<button type="button" class="as-q" data-q="' + q.id + '" title="'
             + _escapeHtml(q.aide || '') + '">' + _escapeHtml(q.texte) + '</button>';
    }).join('');
}

// ── Les graphiques des reponses ────────────────────────────────────
// Dessines a la main, sans bibliotheque : le module n'a aucune dependance
// a ajouter pour ca. Chaque element porte sa cle de detail, donc un clic
// sur un rectangle ouvre la meme fenetre qu'un clic sur une ligne.

// Carte des categories : des rectangles dont la surface est la valeur.
// On repartit sur trois rangees par parts cumulees — les grosses en haut,
// la traine en bas — ce qui se lit mieux qu'un vrai pavage.
function _asCarte(g) {
    var tous = (g.items || []).slice();
    if (!tous.length) return '';
    // La carte sert aussi hors Soldes (ex. transferts, en pieces) : on ne
    // formate en MAD que si le graphique le demande vraiment.
    var uniteMAD = !g.unite || g.unite === 'MAD';
    function fmtCarte(v) {
        return uniteMAD ? formatMAD(v) : (formatNumber(v) + ' ' + g.unite);
    }
    var grand = tous.reduce(function(a, x) { return a + x.valeur; }, 0) || 1;
    // Toutes les categories restent dans le graphique. On les repartit en
    // rangees de six au plus : au-dela, les dernieres seraient trop
    // etroites pour porter leur nom. La hauteur d'une rangee suit son
    // poids, sa largeur se partage entre ses categories — avec un plancher
    // de 9 %, sinon les plus petites redeviendraient illisibles. Ce
    // plancher est le seul ecart a la proportion exacte, et il ne touche
    // que la traine.
    var PAR_RANGEE = 6;
    var PLANCHER = 9;
    var rangees = [];
    var courante = [];
    var cumul = 0;
    var bornes = [0.5, 0.8, 0.95];
    tous.forEach(function(x, i) {
        courante.push(x);
        cumul += x.valeur / grand;
        var seuil = bornes[rangees.length];
        var pleine = courante.length >= PAR_RANGEE;
        var atteint = seuil !== undefined && cumul >= seuil;
        if ((pleine || atteint) && i < tous.length - 1) {
            rangees.push(courante);
            courante = [];
        }
    });
    if (courante.length) rangees.push(courante);

    var h = '<div class="as-carte">';
    rangees.forEach(function(lot) {
        var somme = lot.reduce(function(a, x) { return a + x.valeur; }, 0);
        var hauteur = Math.max(44, Math.round(somme / grand * 230));
        // Largeurs : proportionnelles dans la rangee, jamais sous le
        // plancher. Ce qui est donne aux petites est repris aux grandes.
        var brutes = lot.map(function(x) {
            return somme > 0 ? x.valeur / somme * 100 : 100 / lot.length;
        });
        var dette = 0;
        brutes = brutes.map(function(w) {
            if (w >= PLANCHER) return w;
            dette += PLANCHER - w;
            return PLANCHER;
        });
        var grosses = brutes.reduce(function(a, w) { return a + (w > PLANCHER ? w : 0); }, 0);
        if (dette > 0 && grosses > 0) {
            brutes = brutes.map(function(w) {
                return w > PLANCHER ? w - dette * (w / grosses) : w;
            });
        }
        h += '<div class="as-carte-rangee" style="height:' + hauteur + 'px;">';
        lot.forEach(function(x, j) {
            var force = Math.max(0.18, Math.min(1, x.valeur / (tous[0].valeur || 1)));
            h += '<div class="as-carte-case' + (x.cle ? ' as-clic-el' : '') + '"'
               + (x.cle ? ' data-cle="' + _escapeHtml(x.cle) + '"' : '')
               + ' style="width:' + brutes[j].toFixed(2) + '%;opacity:'
               + (0.2 + force * 0.8).toFixed(2) + ';" title="' + _escapeHtml(x.nom)
               + ' \u2014 ' + fmtCarte(x.valeur)
               + (x.detail ? ' \u00b7 ' + _escapeHtml(x.detail) : '') + '">'
               + '<span class="as-carte-nom">' + _escapeHtml(x.nom) + '</span>'
               + '<span class="as-carte-val">' + fmtCarte(x.valeur) + '</span>'
               + '</div>';
        });
        h += '</div>';
    });
    return h + '</div>';
}

// Barre decoupee : la part de chaque tranche, dans une seule barre.
function _asSegments(g) {
    var items = (g.items || []).filter(function(x) { return x.valeur > 0; });
    if (!items.length) return '';
    var total = items.reduce(function(a, x) { return a + x.valeur; }, 0);
    var teintes = ['#B91C1C', '#D97706', '#CA8A04', '#0F766E', '#047857'];
    var h = '<div class="as-barre">';
    items.forEach(function(x, i) {
        var part = x.valeur / total * 100;
        h += '<div class="as-barre-seg' + (x.cle ? ' as-clic-el' : '') + '"'
           + (x.cle ? ' data-cle="' + _escapeHtml(x.cle) + '"' : '')
           + ' style="width:' + part.toFixed(2) + '%;background:'
           + teintes[Math.min(i, teintes.length - 1)] + ';" title="'
           + _escapeHtml(x.nom) + ' \u2014 ' + formatMAD(x.valeur) + ' \u00b7 '
           + formatNumber(x.refs) + ' r\u00e9f\u00e9rences \u00b7 '
           + _escapeHtml(x.conseil || '') + '">'
           + '<span>' + _escapeHtml(x.nom) + '</span>'
           + '</div>';
    });
    h += '</div><div class="as-legende">';
    items.forEach(function(x, i) {
        h += '<span class="as-leg' + (x.cle ? ' as-clic-el' : '') + '"'
           + (x.cle ? ' data-cle="' + _escapeHtml(x.cle) + '"' : '') + '>'
           + '<span class="as-leg-point" style="background:'
           + teintes[Math.min(i, teintes.length - 1)] + ';"></span>'
           + _escapeHtml(x.nom) + ' \u00b7 ' + formatNumber(x.refs) + ' r\u00e9f. \u00b7 '
           + formatMAD(x.valeur) + '</span>';
    });
    return h + '</div>';
}

// Barres horizontales : un magasin par ligne, longueur = argent bloque.
function _asBarres(g) {
    var items = (g.items || []).filter(function(x) { return x.valeur > 0; });
    if (!items.length) return '';
    var maxi = items[0].valeur;
    items.forEach(function(x) { if (x.valeur > maxi) maxi = x.valeur; });
    var h = '<div class="as-barres">';
    items.forEach(function(x) {
        h += '<div class="as-barres-ligne' + (x.cle ? ' as-clic-el' : '') + '"'
           + (x.cle ? ' data-cle="' + _escapeHtml(x.cle) + '"' : '')
           + ' title="' + _escapeHtml(x.nom)
           + (x.detail ? ' \u2014 ' + _escapeHtml(x.detail) : '') + '">'
           + '<div class="as-barres-haut">'
             + '<span class="as-barres-nom">' + _escapeHtml(x.nom)
               + (x.sous ? '<span class="as-barres-soc">' + _escapeHtml(x.sous) + '</span>' : '')
               + '</span>'
             + '<span class="as-barres-piste"><span class="as-barres-trait" style="width:'
               + (x.valeur / maxi * 100).toFixed(1) + '%;"></span></span>'
           + '</div>'
           + '<div class="as-barres-bas">'
             + '<span class="as-barres-refs" title="r\u00e9f\u00e9rences qui dorment dans ce '
               + 'magasin \u2014 c\u2019est ce que le clic ouvre">'
               + formatNumber(x.refs || 0) + ' ' + (g.libelle_refs || 'r\u00e9f\u00e9rence')
               + (x.refs > 1 ? 's' : '') + '</span>'
             + (x.alerte ? '<span class="as-barres-alerte" '
                 + 'title="dont le d\u00e9p\u00f4t garde encore du stock">'
                 + formatNumber(x.alerte) + ' urgente' + (x.alerte > 1 ? 's' : '')
                 + '</span>' : '')
           + '</div></div>';
    });
    h += '</div>';
    if (g.legende) {
        h += '<div class="as-col-legende">' + _escapeHtml(g.legende) + '</div>';
    }
    return h;
}

// Colonnes : une par tranche de remise, rangees par remise croissante. La
// plus efficace est pleine, les autres pales — on voit la montee puis la
// chute sans lire un seul nombre.
function _asColonnes(g) {
    var items = g.items || [];
    if (!items.length) return '';
    var maxi = 1;
    items.forEach(function(x) { if (x.valeur > maxi) maxi = x.valeur; });
    var h = '<div class="as-col">';
    items.forEach(function(x) {
        var haut = Math.max(3, Math.round(x.valeur / maxi * 100));
        h += '<div class="as-col-item' + (x.cle ? ' as-clic-el' : '') + '"'
           + (x.cle ? ' data-cle="' + _escapeHtml(x.cle) + '"' : '')
           + ' title="' + _escapeHtml(x.nom) + ' % — '
           + _escapeHtml(x.detail || '') + '">'
           + '<span class="as-col-val' + (x.fort ? ' as-col-val-fort' : '') + '">'
             + formatNumber(x.valeur) + '</span>'
           + '<span class="as-col-piste"><span class="as-col-barre'
             + (x.fort ? ' as-col-fort' : '') + '" style="height:' + haut + '%;"></span></span>'
           + '<span class="as-col-nom' + (x.fort ? ' as-col-nom-fort' : '') + '">'
             + _escapeHtml(x.nom) + '</span></div>';
    });
    h += '</div>';
    if (g.legende) {
        h += '<div class="as-col-legende">' + _escapeHtml(g.legende) + '</div>';
    }
    return h;
}

function _asGraphique(g) {
    if (!g || !g.type) return '';
    if (g.type === 'carte') return _asCarte(g);
    if (g.type === 'segments') return _asSegments(g);
    if (g.type === 'barres') return _asBarres(g);
    if (g.type === 'colonnes') return _asColonnes(g);
    return '';
}

// Le contenu d'une cellule. Sur une ligne de reference, la colonne « Ou
// solder » garde son propre clic : la ligne ouvre la fiche de l'article,
// cette cellule-la ouvre la liste des magasins ou poser la remise.
function _asCellule(valeur, colonne, colonnes, cle) {
    var titre = (colonnes || [])[colonne] || '';
    if (titre.indexOf('O\u00f9 solder') === 0 && cle && cle.indexOf('ref:') === 0) {
        return '<span class="as-ou" data-cle="' + _escapeHtml(cle)
             + '" title="Voir les magasins exacts o\u00f9 poser la remise">'
             + _escapeHtml(valeur) + '</span>';
    }
    return _escapeHtml(valeur);
}

// Refermer : la reponse disparait et plus aucune question n'est active.
function _asFermer() {
    asState.active = null;
    var zone = el('as-reponse');
    if (zone) zone.innerHTML = '';
    Array.prototype.forEach.call(document.querySelectorAll('.as-q'), function(b) {
        b.classList.remove('as-on');
    });
}

async function _asRepondre(qid, articleId) {
    var zone = el('as-reponse');
    if (!zone) return;
    asState.active = qid;
    (document.querySelectorAll('.as-q') || []).forEach(function(b) {
        b.classList.toggle('as-on', b.getAttribute('data-q') === qid);
    });
    zone.innerHTML = '<div class="as-rep-texte">Calcul\u2026</div>';
    var params = _spParams();
    params.question = qid;
    if (articleId) params.article_id = articleId;
    var r = await rpc('/mavie/api/assistant', params);
    if (!r || r.error) {
        zone.innerHTML = '<div class="as-rep-texte" style="color:#B91C1C;">'
            + _escapeHtml((r && r.error) || 'Erreur inconnue') + '</div>';
        return;
    }
    var h = '<div class="as-rep-titre">' + _escapeHtml(r.titre || '') + '</div>'
          + '<div class="as-rep-texte">' + _escapeHtml(r.resume || '') + '</div>';
    // Quand la reponse a un graphique, il remplace le tableau : c'est la
    // meme information, lue sans compter.
    var dessin = (r.graphiques || [r.graphique]).map(function(g) {
        if (!g) return '';
        return (g.titre ? '<div class="as-rep-titre" style="font-size:0.9rem;margin:12px 0 4px;">'
            + _escapeHtml(g.titre) + '</div>' : '') + _asGraphique(g);
    }).join('');
    if (dessin) {
        h += dessin;
        if (r.aide_clic) {
            h += '<div class="as-rep-aide">' + _escapeHtml(r.aide_clic) + '</div>';
        }
    }
    if (!dessin && (r.colonnes || []).length) {
        var cles = r.cles || [];
        h += '<table class="as-rep-table"><thead><tr>'
           + r.colonnes.map(function(c) { return '<th>' + _escapeHtml(c) + '</th>'; }).join('')
           + '</tr></thead><tbody>'
           + (r.lignes || []).map(function(l, i) {
                 var cle = cles[i] || '';
                 var ref = cle.indexOf('ref:') === 0;
                 return '<tr' + (cle ? ' class="as-clic" data-cle="' + _escapeHtml(cle)
                     + (ref ? '" data-nom="' + _escapeHtml(l[0]) : '')
                     + '" title="' + (ref ? 'Ouvrir la fiche de l\u2019article'
                                          : 'Voir le d\u00e9tail') + '"' : '') + '>'
                     + l.map(function(c, j) {
                         return '<td>' + _asCellule(c, j, r.colonnes, cle) + '</td>';
                     }).join('') + '</tr>';
             }).join('')
           + '</tbody></table>';
        if (r.aide_clic && cles.filter(function(c) { return !!c; }).length) {
            h += '<div class="as-rep-aide">' + _escapeHtml(r.aide_clic) + '</div>';
        }
    }
    // La question « quelle remise » porte sur UNE reference : on offre la
    // liste, triee comme le tableau, pour en changer sans quitter la page.
    if (qid === 'remise_reference') {
        var options = (spState.rows || []).slice(0, 200).map(function(x) {
            return '<option value="' + x.article_id + '"'
                 + (String(x.article_id) === String(articleId) ? ' selected="selected"' : '')
                 + '>' + _escapeHtml(x.reference) + '</option>';
        }).join('');
        h += '<div class="as-choix"><span>Référence :</span>'
           + '<select id="as-article" class="rx-select">' + options + '</select></div>';
    }
    zone.innerHTML = h;
    var sel = el('as-article');
    if (sel) sel.addEventListener('change', function() {
        _asRepondre('remise_reference', parseInt(sel.value, 10));
    });
    // Chaque ligne qui porte une cle s'ouvre sur son detail.
    var tableau = zone.querySelector('.as-rep-table');
    if (tableau) tableau.addEventListener('click', function(ev) {
        // La colonne « Ou solder » prime sur la ligne : on veut les magasins.
        var ou = ev.target.closest && ev.target.closest('.as-ou');
        if (ou) {
            ev.stopPropagation();
            _asDetOuvrir(ou.getAttribute('data-cle'), AS_CTX_SOLDES);
            return;
        }
        var tr = ev.target.closest && ev.target.closest('tr.as-clic');
        if (!tr) return;
        var c = tr.getAttribute('data-cle') || '';
        if (c.indexOf('ref:') === 0) {
            var id = parseInt(c.slice(4), 10);
            if (id) openDetail(id, tr.getAttribute('data-nom') || '');
            return;
        }
        _asDetOuvrir(c, AS_CTX_SOLDES);
    });
    // Un rectangle, un segment ou une barre ouvrent le meme detail.
    if (!asState.zoneBound) {
        asState.zoneBound = true;
        zone.addEventListener('click', function(ev) {
            var e = ev.target.closest && ev.target.closest('.as-clic-el');
            if (e) _asDetOuvrir(e.getAttribute('data-cle'), AS_CTX_SOLDES);
        });
    }
}

function _spParams() {
    function v(id, def) {
        var e = el(id);
        var n = e ? parseInt(e.value, 10) : NaN;
        return isNaN(n) ? def : n;
    }
    return {
        collection_id: state.collection_id,
        categ_ids: state.categ_ids || [],
        batch_id: state.batch_id,
        shop_field: state.shop_field,
        fenetre: v('sp-fenetre', 90),
        stock_min: v('sp-stock-min', 5),
        remise1: v('sp-remise1', 30),
        remise2: v('sp-remise2', 50),
        couverture_min: v('sp-couverture', 120),
    };
}

// L'onglet ouvert. Les transferts ne se calculent qu'a la premiere
// ouverture de leur onglet : inutile d'interroger les caisses si on vient
// pour les soldes.
var prOnglet = 'soldes';
var prTransfertsCharges = false;

function _prOnglet(nom) {
    prOnglet = nom;
    ['soldes', 'transferts'].forEach(function(o) {
        var zone = el('onglet-' + o);
        if (zone) zone.style.display = o === nom ? '' : 'none';
        var bouton = el('onglet-btn-' + o);
        if (bouton) bouton.className = 'pr-onglet' + (o === nom ? ' pr-on' : '');
    });
    // Les boutons Reglages et Exporter du bandeau ne valent que pour les
    // soldes : l'onglet Transferts a les siens.
    var actions = el('sp-head-actions');
    if (actions) actions.style.display = nom === 'soldes' ? '' : 'none';
    if (nom === 'transferts' && !prTransfertsCharges) {
        prTransfertsCharges = true;
        loadTransferts();
    }
}

function _prOngletsBindOnce() {
    ['soldes', 'transferts'].forEach(function(o) {
        var bouton = el('onglet-btn-' + o);
        if (bouton) bouton.addEventListener('click', function() { _prOnglet(o); });
    });
}

async function loadPropositions() {
    _spBindOnce();
    _prOngletsBindOnce();
    _spOperationBindOnce();
    _spChargerOperation();
    _asCharger();
    _traCharger();
    // Un recalcul demande depuis l'onglet Transferts doit le rafraichir.
    if (prOnglet === 'transferts' && prTransfertsCharges) loadTransferts();
    var corps = el('sp-body');
    if (corps) corps.innerHTML = '<div class="rb-empty">Calcul en cours\u2026</div>';
    var seq = ++spState.seq;
    var data = await rpc('/mavie/api/soldes-proposition', _spParams());
    if (seq !== spState.seq) return;
    if (!data || data.error) {
        if (corps) corps.innerHTML = '<div class="rb-empty" style="color:#B91C1C;"><b>Le calcul a \u00e9chou\u00e9.</b> '
            + _escapeHtml((data && data.error) || 'Erreur inconnue') + '</div>';
        return;
    }
    spState.data = data;
    spState.rows = data.rows || [];
    _spRender();
}

// Ce que chaque choix du filtre affiche. La ligne suit le choix en cours,
// pour qu'on sache toujours ce qu'on a sous les yeux.
var SP_AIDE_ETAT = {
    toutes: 'tout le tableau, remises déjà posées comprises',
    urgent: 'elles dorment en magasin et il en reste au dépôt',
    deja: 'une remise est déjà posée — la resolder l’écrase',
    a_traiter: 'aucune remise posée pour le moment',
};

function _spAideEtat() {
    var zone = el('sp-f-aide');
    var etatEl = el('sp-f-etat');
    if (!zone) return;
    var etat = etatEl ? etatEl.value : 'urgent';
    zone.textContent = SP_AIDE_ETAT[etat] ? '— ' + SP_AIDE_ETAT[etat] : '';
}

function _spFiltrees() {
    var etatEl = el('sp-f-etat');
    var etat = etatEl ? etatEl.value : 'urgent';
    var sEl = el('sp-f-search');
    var q = sEl ? (sEl.value || '').trim().toLowerCase() : '';
    return spState.rows.filter(function(r) {
        if (etat === 'urgent' && (r.deja_solde || r.urgence !== 'urgent')) return false;
        if (etat === 'a_traiter' && r.deja_solde) return false;
        if (etat === 'deja' && !r.deja_solde) return false;
        if (q) {
            var hay = (r.reference + ' ' + r.produit + ' ' + r.categorie).toLowerCase();
            if (hay.indexOf(q) === -1) return false;
        }
        return true;
    });
}

// Une remise ne s'applique pas magasin par magasin quand plusieurs
// magasins partagent la meme liste de prix : elle s'applique a tous. On
// le dit avant que l'utilisatrice ne coche des magasins pour rien.
// Les deux bandeaux ont ete retires de la page : trop de texte au-dessus
// du tableau (demande utilisatrice). Ce qu'ils disaient vit ailleurs — les
// habitudes dans la question « Quelles remises pratiquez-vous, par
// categorie ? », la provenance d'une remise dans l'infobulle de son menu,
// et la portee reelle dans la confirmation avant d'appliquer.
function _spCalibrage() { _spMasquer('sp-calibrage'); }
function _spPortee() { _spMasquer('sp-portee'); }

function _spMasquer(id) {
    var zone = el(id);
    if (!zone) return;
    zone.style.display = 'none';
    zone.innerHTML = '';
}

// Le bandeau de l'operation en cours. Tant qu'elle dure, tout ce qu'on
// solde en prend les dates — et s'arrete donc avec elle.
var spOperation = null;

async function _spChargerOperation(force) {
    if (spOperation && !force) return spOperation;
    var o = await rpc('/mavie/api/solde-operation', { action: 'lire' });
    spOperation = (o && !o.error) ? o : null;
    _spOperation();
    return spOperation;
}

function _spOperation() {
    var zone = el('sp-operation');
    if (!zone) return;
    var o = spOperation;
    if (!o || !o.active) {
        zone.className = 'sp-operation sp-op-muette';
        zone.innerHTML = '<span class="sp-op-nom">Aucune op\u00e9ration en cours</span>'
            + '<span class="sp-op-dates">Les remises pos\u00e9es n\u2019auront pas de date '
            + 'de fin : elles resteront jusqu\u2019\u00e0 ce qu\u2019on les retire.</span>'
            + '<span class="sp-op-droite"><button type="button" class="rx-btn rx-btn-sm" '
            + 'id="btn-sp-op-modifier">D\u00e9clarer une op\u00e9ration</button></span>';
    } else {
        var etat = o.terminee ? ' sp-op-finie' : (o.a_venir ? ' sp-op-muette' : '');
        zone.className = 'sp-operation' + etat;
        zone.innerHTML = '<span class="sp-op-nom">' + _escapeHtml(o.nom) + '</span>'
            + '<span class="sp-op-dates">du ' + _raFmtDate(o.debut)
            + (o.fin ? ' au ' + _raFmtDate(o.fin) : ', sans date de fin')
            + (o.terminee ? ' \u2014 termin\u00e9e, elle ne pose plus rien'
               : (o.a_venir ? ' \u2014 pas encore commenc\u00e9e' : ''))
            + '</span>'
            + (o.remises_posees ? '<span class="sp-op-compte">'
                + formatNumber(o.remises_posees) + ' remise'
                + (o.remises_posees > 1 ? 's' : '') + ' pos\u00e9e'
                + (o.remises_posees > 1 ? 's' : '') + '</span>' : '')
            + '<span class="sp-op-droite"><button type="button" class="rx-btn rx-btn-sm" '
            + 'id="btn-sp-op-modifier">Modifier</button> <a class="rx-btn rx-btn-sm" href="/mavie/soldes-bon-pdf" target="_blank" rel="noopener">Bon de soldes (PDF)</a></span>';
    }
    var modifier = el('btn-sp-op-modifier');
    if (modifier) modifier.addEventListener('click', function() {
        var f = el('sp-op-form');
        if (!f) return;
        var nom = el('sp-op-nom');
        var debut = el('sp-op-debut');
        var fin = el('sp-op-fin');
        if (nom) nom.value = (o && o.nom) || '';
        if (debut) debut.value = (o && o.debut) || '';
        if (fin) fin.value = (o && o.fin) || '';
        var cs = el('sp-op-categ');
        if (cs) Array.prototype.forEach.call(cs.querySelectorAll('input[type=checkbox]'), function(cb) {
            cb.checked = ((o && o.categories) || []).indexOf(parseInt(cb.value, 10)) >= 0;
        });
        f.style.display = f.style.display === 'none' ? '' : 'none';
    });
}

async function _spChargerCategories() {
    var cs = el('sp-op-categ');
    if (!cs) return;
    var d = await rpc('/mavie/api/soldes-categories', {});
    cs.innerHTML = ((d && d.categories) || []).map(function(c) {
        return '<label class="sp-op-cat"><input type="checkbox" value="' + c.id + '"/> '
            + _escapeHtml(c.nom) + '</label>';
    }).join('');
}

function _spOperationBindOnce() {
    _spChargerCategories();
    var ok = el('btn-sp-op-ok');
    if (ok) ok.addEventListener('click', async function() {
        var r = await rpc('/mavie/api/solde-operation', {
            action: 'definir',
            nom: (el('sp-op-nom') || {}).value || '',
            debut: (el('sp-op-debut') || {}).value || '',
            fin: (el('sp-op-fin') || {}).value || '',
            categories: (function() {
                var cs = el('sp-op-categ');
                return cs ? Array.prototype.filter.call(cs.querySelectorAll('input[type=checkbox]'),
                    function(cb) { return cb.checked; }).map(function(cb) { return parseInt(cb.value, 10); }) : [];
            })() });
        if (r && r.error) { window.alert(r.error); return; }
        spOperation = r;
        var f = el('sp-op-form');
        if (f) f.style.display = 'none';
        _spOperation();
    });
    var annuler = el('btn-sp-op-annuler');
    if (annuler) annuler.addEventListener('click', function() {
        var f = el('sp-op-form');
        if (f) f.style.display = 'none';
    });
    var effacer = el('btn-sp-op-effacer');
    if (effacer) effacer.addEventListener('click', async function() {
        if (!window.confirm('Arr\u00eater l\u2019op\u00e9ration en cours ?\n\n'
            + 'Les remises d\u00e9j\u00e0 pos\u00e9es gardent leurs dates. Seules les '
            + 'prochaines n\u2019auront plus de date de fin.')) return;
        var r = await rpc('/mavie/api/solde-operation', { action: 'effacer' });
        spOperation = (r && !r.error) ? r : null;
        var f = el('sp-op-form');
        if (f) f.style.display = 'none';
        _spOperation();
    });
}

function _spRender() {
    var data = spState.data;
    if (!data) return;
    if (data.operation) { spOperation = data.operation; _spOperation(); }
    _spAideEtat();
    _spCalibrage();
    _spPortee();
    var k = data.kpis || {};
    function set(id, txt) { var e = el(id); if (e) e.textContent = txt; }
    set('sp-kpi-refs', formatNumber(k.nb_a_traiter || 0));
    set('sp-kpi-refs-sub', 'r\u00e9f\u00e9rences \u00b7 ' + formatNumber(k.pieces || 0) + ' pi\u00e8ces'
        + ' \u00b7 ' + formatMAD(k.valeur_a_traiter || 0) + ' \u00e0 solder');
    set('sp-kpi-deja', formatNumber(k.nb_deja_soldees || 0));
    set('sp-kpi-urgent', formatNumber(k.nb_urgent || 0));
    set('sp-kpi-urgent-sub', formatNumber(k.pieces_depot || 0)
        + ' pi\u00e8ces au d\u00e9p\u00f4t \u00b7 ' + formatMAD(k.valeur_depot || 0));

    var rows = _spFiltrees();
    var valeur = rows.reduce(function(a, r) { return a + (r.valeur || 0); }, 0);
    var pieces = rows.reduce(function(a, r) { return a + (r.stock || 0); }, 0);
    // Quand la liste est tronquee, dire combien le filtre compte VRAIMENT :
    // sinon le compteur du bloc contredit la carte du haut.
    var etatEl = el('sp-f-etat');
    var etat = etatEl ? etatEl.value : 'urgent';
    var totaux = { toutes: k.nb_references, urgent: k.nb_urgent,
                   deja: k.nb_deja_soldees, a_traiter: k.nb_a_traiter };
    var vrai = totaux[etat];
    var manquantes = (data.tronque && vrai && vrai > rows.length) ? vrai - rows.length : 0;
    _spSetCount(rows.length
        ? formatNumber(rows.length) + ' r\u00e9f\u00e9rence' + (rows.length > 1 ? 's' : '')
          + (manquantes ? ' affich\u00e9es sur ' + formatNumber(vrai) : '')
          + ' \u00b7 ' + formatNumber(pieces) + ' pi\u00e8ces \u00b7 ' + formatMAD(valeur)
        : 'aucune');

    var corps = el('sp-body');
    if (!corps) return;
    if (!rows.length) {
        corps.innerHTML = '<div class="rb-empty">Aucune r\u00e9f\u00e9rence ne correspond.</div>';
        return;
    }
    var max = Math.min(rows.length, RA_MAX_LIGNES);
    var h = '<div class="rx-scroll"><table class="rx-table"><thead><tr>'
          + '<th class="sp-c"><input type="checkbox" id="sp-all" title="Tout cocher / tout d\u00e9cocher"/></th>'
          + '<th class="sp-photo-c"></th>'
          + '<th>R\u00e9f\u00e9rence</th><th>Cat\u00e9gorie</th>'
          + '<th class="num" title="Nombre de boutiques qui d\u00e9tiennent ce stock dormant">Nb magasins</th>'
          + '<th class="num" title="Pi\u00e8ces re\u00e7ues par les magasins sur les bons d\u2019achat confirm\u00e9s, nettes des retours">Qt\u00e9 achet\u00e9e</th>'
          + '<th class="num" title="Pi\u00e8ces vendues en caisse sur la p\u00e9riode">Vendu</th>'
          + '<th class="num" title="Pi\u00e8ces pr\u00e9sentes dans les MAGASINS \u2014 pas au d\u00e9p\u00f4t">Stock magasins</th>'
          + '<th class="num" title="Pi\u00e8ces de cette r\u00e9f\u00e9rence encore au d\u00e9p\u00f4t : le solder lib\u00e8re aussi ce stock">D\u00e9p\u00f4t</th>'
          + '<th class="num" title="Jours \u00e9coul\u00e9s depuis la derni\u00e8re vente en caisse">Derni\u00e8re vente</th>'
          + '<th class="num" title="Jours n\u00e9cessaires pour \u00e9couler le stock au rythme actuel">Couverture</th>'
          + '<th class="num" title="Prix de vente catalogue TTC, celui de l\u2019\u00e9tiquette">Prix TTC</th>'
          + '<th class="num">Remise</th>'
          + '<th class="num" title="Prix apr\u00e8s remise">Prix sold\u00e9</th><th>Action</th>'
          + '</tr></thead><tbody>';
    for (var i = 0; i < max; i++) {
        var r = rows[i];
        var rem = _spRemise(r);
        h += '<tr data-row="' + r.article_id + '"'
           + (r.deja_solde ? ' style="background:#F8FAFC;"' : '') + '>'
           + '<td class="sp-c"><input type="checkbox" class="sp-chk" data-article="'
             + r.article_id + '"' + (spState.sel[r.article_id] ? ' checked="checked"' : '')
             + ' title="' + (r.deja_solde
                 ? 'Une remise est d\u00e9j\u00e0 pos\u00e9e : la resolder l\u2019\u00e9crase'
                 : 'Inclure cette r\u00e9f\u00e9rence dans la s\u00e9lection') + '"/></td>'
           + '<td class="sp-photo-c">' + (r.photo
             ? '<img class="sp-photo" src="' + _escapeHtml(r.photo) + '" alt="" loading="lazy"/>'
             : '<span class="sp-photo sp-photo-vide" title="Aucune photo sur cette r\u00e9f\u00e9rence">\u2014</span>')
             + '</td>'
           + '<td><button type="button" class="sp-expand" data-article="' + r.article_id
             + '" title="Voir le stock magasin par magasin">\u25b8</button> '
           + '<span class="rx-ref" data-article="' + r.article_id + '" data-name="'
             + _escapeHtml(r.produit) + '">' + _escapeHtml(r.reference) + '</span>'
           + (r.deja_solde ? ' <span class="mfl-code" title="Une r\u00e8gle de prix existe d\u00e9j\u00e0 dans « '
              + _escapeHtml(r.solde_en_place || '') + ' »'
              + (r.remise_en_place ? ' \u2014 la proposition l\u2019accentue' : '') + '">'
              + (r.remise_en_place ? 'd\u00e9j\u00e0 \u2212' + r.remise_en_place + ' %'
                 : 'd\u00e9j\u00e0 sold\u00e9e') + '</span>' : '')
           + (r.remise_au_plafond ? ' <span class="sp-urg sp-urg-1" title="La remise est d\u00e9j\u00e0 au maximum : elle ne peut plus monter. Il faut d\u00e9placer la marchandise ou la d\u00e9classer.">au plafond</span>' : '')
           + (r.urgence === 'urgent' ? ' <span class="sp-urg sp-urg-1" title="Dort en magasin ET il en reste au d\u00e9p\u00f4t">urgent</span>' : (r.urgence === 'forte' ? ' <span class="sp-urg sp-urg-2" title="Aucune vente, ou stock qui met tr\u00e8s longtemps \u00e0 s\u2019\u00e9couler">forte</span>' : ''))
           + '</td>'
           + '<td class="rx-muted">' + _escapeHtml(r.categorie || '\u2014') + '</td>'
           + '<td class="num">' + formatNumber(r.nb_magasins) + '</td>'
           + '<td class="num">' + formatNumber(r.achete || 0) + '</td>'
           + '<td class="num"' + (r.vendu ? '' : ' style="color:#94A3B8;"') + '>'
             + formatNumber(r.vendu || 0) + '</td>'
           + '<td class="num">' + formatNumber(r.stock) + '</td>'
           + '<td class="num"' + (r.depot ? ' style="font-weight:700;color:#B91C1C;"' : ' style="color:#94A3B8;"') + '>' + formatNumber(r.depot || 0) + '</td>'
           + '<td class="num">' + (r.jamais_vendu
               ? '<span style="color:#B91C1C;font-weight:600;">jamais</span>'
               : formatNumber(r.jours_sans_vente) + ' j') + '</td>'
           + '<td class="num">' + (r.couverture_jours === null || r.couverture_jours === undefined
               ? '<span class="rx-muted">\u2014</span>'
               : formatNumber(r.couverture_jours) + ' j') + '</td>'
           + '<td class="num">' + formatMAD(r.prix_ttc) + '</td>'
           + '<td class="num"><select class="sp-remise-sel" data-article="' + r.article_id
             + '" title="' + _escapeHtml(_spSourceRemise(r))
             + ' Ajustez-la avant de solder si vous le jugez utile.">'
             + _spOptionsRemise(rem) + '</select></td>'
           + '<td class="num" data-row-prix="' + r.article_id + '">'
             + formatMAD(_spPrixSolde(r)) + '</td>'
           + '<td><button type="button" class="sp-btn-solder" data-article="' + r.article_id
             + '" data-remise="' + rem
             + '" title="Ouvre le panneau d\u00e9j\u00e0 rempli : remise pos\u00e9e et magasins concern\u00e9s coch\u00e9s">\ud83c\udff7\ufe0f Solder</button></td>'
           + '</tr>'
           + '<tr class="sp-det-row" data-det="' + r.article_id + '" style="display:none;">'
             + '<td colspan="15" class="sp-det-cell" id="sp-det-' + r.article_id + '"></td>'
           + '</tr>';
    }
    h += '</tbody></table></div>';
    if (rows.length > max) {
        h += '<div class="rb-more">\u2026 ' + formatNumber(rows.length - max)
           + ' r\u00e9f\u00e9rences de plus \u2014 affinez la recherche ou utilisez Exporter.</div>';
    }
    corps.innerHTML = h;

    var foot = el('sp-foot');
    if (foot) {
        foot.textContent = 'Calcul\u00e9 sur les ventes du ' + _raFmtDate(data.date_debut)
            + ' au ' + _raFmtDate(data.date_reference)
            + (data.tronque ? ' \u00b7 ' + formatNumber(data.nb_lignes_total)
               + ' r\u00e9f\u00e9rences au total' : '');
    }
    _spRecap();
}

// ── Popup de detail de l'assistant ─────────────────────────────────
// Le meme popup sert a tout. On empile les cles traversees pour offrir un
// retour : depuis un magasin on ouvre une reference, et on revient.
var asDet = { pile: [], bound: false, ctx: null };

// Chaque assistant (Soldes, Transferts) ouvre ce meme popup de detail,
// mais sur SA propre route et avec SES propres parametres. Le contexte
// est fixe au premier clic depuis le tableau de reponse, puis reste en
// place pour les clics suivants (retour, drill-down imbrique).
var AS_CTX_SOLDES = {
    endpoint: '/mavie/api/assistant-detail',
    params: _spParams,
    question: function() { return asState.active; },
};

function _asDetFermer() {
    var o = el('as-detail-overlay');
    if (o) o.classList.remove('active');
    asDet.pile = [];
    asDet.ctx = null;
}

function _asDetBindOnce() {
    if (asDet.bound) return;
    asDet.bound = true;
    var fermer = el('close-as-detail-btn');
    if (fermer) fermer.addEventListener('click', _asDetFermer);
    var fond = el('as-detail-overlay');
    if (fond) fond.addEventListener('click', function(ev) {
        if (ev.target === fond) _asDetFermer();
    });
    var retour = el('as-det-retour');
    if (retour) retour.addEventListener('click', function() {
        asDet.pile.pop();
        var precedente = asDet.pile.pop();
        if (precedente) _asDetOuvrir(precedente);
        else _asDetFermer();
    });
    var corps = el('as-det-corps');
    if (corps) corps.addEventListener('click', function(ev) {
        // Le nom de l'article prime sur la ligne : on veut sa fiche.
        var fiche = ev.target.closest && ev.target.closest('.as-fiche');
        if (fiche) {
            ev.stopPropagation();
            var aid = parseInt(fiche.getAttribute('data-article'), 10);
            if (aid) {
                _asDetFermer();
                openDetail(aid, fiche.getAttribute('data-name') || fiche.textContent);
            }
            return;
        }
        var ou = ev.target.closest && ev.target.closest('.as-ou');
        if (ou) {
            ev.stopPropagation();
            _asDetOuvrir(ou.getAttribute('data-cle'));
            return;
        }
        var tr = ev.target.closest && ev.target.closest('tr.as-clic');
        if (!tr) return;
        var c = tr.getAttribute('data-cle') || '';
        // Une ligne de reference ouvre sa fiche, ou qu'on clique dessus.
        if (c.indexOf('ref:') === 0) {
            var id = parseInt(c.slice(4), 10);
            if (id) {
                _asDetFermer();
                openDetail(id, tr.getAttribute('data-nom') || '');
            }
            return;
        }
        _asDetOuvrir(c);
    });
}

async function _asDetOuvrir(cle, ctx) {
    if (!cle) return;
    if (ctx) asDet.ctx = ctx;
    if (!asDet.ctx) asDet.ctx = AS_CTX_SOLDES;
    _asDetBindOnce();
    var o = el('as-detail-overlay');
    if (!o) return;
    asDet.pile.push(cle);
    o.classList.add('active');
    var retour = el('as-det-retour');
    if (retour) retour.style.display = asDet.pile.length > 1 ? '' : 'none';
    function set(id, txt) { var e = el(id); if (e) e.textContent = txt; }
    set('as-det-titre', 'D\u00e9tail');
    set('as-det-resume', 'Calcul\u2026');
    var corps = el('as-det-corps');
    if (corps) corps.innerHTML = '';
    var pied = el('as-det-pied');
    if (pied) pied.innerHTML = '';
    var params = asDet.ctx.params();
    params.cle = cle;
    params.question = asDet.ctx.question() || '';
    var seq = asDet.seq = (asDet.seq || 0) + 1;
    var d = await rpc(asDet.ctx.endpoint, params);
    if (seq !== asDet.seq) return;
    if (!d || d.error) {
        set('as-det-titre', 'D\u00e9tail indisponible');
        set('as-det-resume', (d && d.error) || 'Erreur inconnue');
        return;
    }
    set('as-det-titre', d.titre || 'D\u00e9tail');
    set('as-det-resume', d.dates ? ('État : ' + d.etat + ' · ' + d.dates.map(function(x) {
        return x[0] + ' le ' + (_trQuand(x[1]) || '—');
    }).join(' · ')) : (d.resume || ''));
    if (corps) {
        if (!(d.lignes || []).length) {
            corps.innerHTML = '<div class="rx-muted">Aucune ligne.</div>';
        } else {
            var cles = d.cles || [];
            corps.innerHTML = '<table class="as-det-table"><thead><tr>'
                + (d.colonnes || []).map(function(c) {
                      return '<th>' + _escapeHtml(c) + '</th>'; }).join('')
                + '</tr></thead><tbody>'
                + d.lignes.map(function(l, i) {
                      var c = cles[i] || '';
                      // Quand la ligne EST une reference, son nom ouvre la
                      // fiche article, comme dans le tableau des soldes ;
                      // le reste de la ligne ouvre son detail.
                      var art = c.indexOf('ref:') === 0 ? c.slice(4) : '';
                      return '<tr' + (c ? ' class="as-clic" data-cle="' + _escapeHtml(c)
                          + (art ? '" data-nom="' + _escapeHtml(l[0]) : '')
                          + '" title="' + (art ? 'Ouvrir la fiche de l\u2019article'
                                               : 'Voir le d\u00e9tail') + '"' : '') + '>'
                          + l.map(function(v, j) {
                                if (j === 0 && art) {
                                    return '<td><span class="rx-ref as-fiche" data-article="'
                                         + art + '" data-name="' + _escapeHtml(v)
                                         + '" title="Ouvrir la fiche de l\u2019article">'
                                         + _escapeHtml(v) + '</span></td>';
                                }
                                return '<td>' + _asCellule(v, j, d.colonnes, c) + '</td>';
                            }).join('') + '</tr>';
                  }).join('')
                + '</tbody></table>';
        }
    }
    if (pied) {
        var bouts = [];
        if (d.tronque) {
            bouts.push('<span>' + formatNumber(d.nb_total)
                + ' lignes au total, les ' + formatNumber((d.lignes || []).length)
                + ' plus co\u00fbteuses sont affich\u00e9es.</span>');
        }
        // Sur une reference, on propose le geste directement : ouvrir le
        // panneau Solder deja rempli plutot que la chercher dans le tableau.
        if (d.article_id) {
            bouts.push('<button type="button" class="sp-btn-solder" id="as-det-solder"'
                + ' data-article="' + d.article_id + '" data-remise="' + (d.remise || 0)
                + '">\ud83c\udff7\ufe0f Solder ' + _escapeHtml(d.reference || '')
                + '</button>');
        }
        pied.innerHTML = bouts.join(' ');
        var bs = el('as-det-solder');
        if (bs) bs.addEventListener('click', function() {
            _asDetFermer();
            openSoldePanel(d.article_id, '', { remise: d.remise || 0 });
        });
    }
}

// ── Transferts entre magasins ──────────────────────────────────────
// Une reference qui se vend ici et dort la-bas n'a pas besoin d'une
// remise, elle a besoin d'un camion.
//
// La page ne montre pas une liste : chaque proposition est un BON DE
// TRANSFERT deja redige, presente une carte a la fois. On le cree ou on
// l'ecarte, et on passe au suivant. Un geste par bon.
var trState = {
    data: null, rows: [], bound: false,
    index: 0,          // le bon affiche
    traites: {},       // cle du bon -> 'cree' | 'ecarte'
    exclus: {},        // references retirees d'un bon
};

function _trParams() {
    function v(id, def) {
        var e = el(id);
        var n = e ? parseInt(e.value, 10) : NaN;
        return isNaN(n) ? def : n;
    }
    return {
        collection_id: state.collection_id,
        categ_ids: state.categ_ids || [],
        batch_id: state.batch_id,
        shop_field: state.shop_field,
        fenetre: v('tr-fenetre', 90),
        cible: v('tr-cible', 60),
        min_qte: v('tr-min-qte', 3),
        couverture_donneur: v('tr-couv-donneur', 120),
        couverture_demandeur: v('tr-couv-demandeur', 45),
    };
}

// ── Assistant des transferts ────────────────────────────────────────
// Meme principe que Soldes (une question fermee = un graphique, pas de
// chatbot) mais une AUTRE mise en scene, demandee explicitement : la liste
// des questions reste affichee a cote de la reponse, comme un sommaire,
// plutot que des pastilles qui disparaissent une fois cliquees.
var traState = { questions: [], active: null };

var AS_CTX_TRANSFERTS = {
    endpoint: '/mavie/api/transferts-assistant-detail',
    params: _trParams,
    question: function() { return traState.active; },
};

async function _traCharger() {
    if (traState.questions.length) return;
    var d = await rpc('/mavie/api/transferts-assistant', {});
    traState.questions = (d && d.questions) || [];
    var hote = el('tra-questions');
    if (!hote) return;
    hote.innerHTML = traState.questions.map(function(q) {
        return '<button type="button" class="tra-q" data-q="' + q.id + '" title="'
             + _escapeHtml(q.aide || '') + '">' + _escapeHtml(q.texte) + '</button>';
    }).join('');
    hote.addEventListener('click', function(ev) {
        var b = ev.target.closest && ev.target.closest('.tra-q');
        if (!b) return;
        var qid = b.getAttribute('data-q');
        // Un second clic sur la question ouverte la referme.
        if (traState.active === qid) {
            _traFermer();
            return;
        }
        _traRepondre(qid);
    });
}

function _traFermer() {
    traState.active = null;
    var zone = el('tra-reponse');
    if (zone) zone.innerHTML = '<div class="tra-vide">Choisissez une question à gauche.</div>';
    (document.querySelectorAll('.tra-q') || []).forEach(function(b) {
        b.classList.remove('tra-on');
    });
}

function _traExporter() {
    var r = traState.reponse;
    if (!r || !(r.colonnes || []).length) return;
    function cell(v) {
        return '"' + String(v === null || v === undefined ? '' : v).replace(/"/g, '""') + '"';
    }
    var lignes = [r.colonnes.map(cell).join(';')];
    (r.lignes || []).forEach(function(l) { lignes.push(l.map(cell).join(';')); });
    var blob = new Blob(['\ufeff' + lignes.join('\r\n')], { type: 'text/csv;charset=utf-8;' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'transferts-' + (traState.active || 'reponse') + '.csv';
    a.click();
    URL.revokeObjectURL(url);
}

async function _traRepondre(qid) {
    var zone = el('tra-reponse');
    if (!zone) return;
    traState.active = qid;
    (document.querySelectorAll('.tra-q') || []).forEach(function(b) {
        b.classList.toggle('tra-on', b.getAttribute('data-q') === qid);
    });
    zone.innerHTML = '<div class="as-rep-texte">Calcul\u2026</div>';
    var params = _trParams();
    params.question = qid;
    var r = await rpc('/mavie/api/transferts-assistant', params);
    if (!r || r.error) {
        zone.innerHTML = '<div class="as-rep-texte" style="color:#B91C1C;">'
            + _escapeHtml((r && r.error) || 'Erreur inconnue') + '</div>';
        return;
    }
    var h = '<div class="as-rep-titre">' + _escapeHtml(r.titre || '') + '</div>'
          + (r.resume ? '<div class="as-rep-texte">' + _escapeHtml(r.resume) + '</div>' : '');
    var dessin = _asGraphique(r.graphique);
    if (dessin) {
        h += dessin;
        if (r.aide_clic) {
            h += '<div class="as-rep-aide">' + _escapeHtml(r.aide_clic) + '</div>';
        }
    }
    if (!dessin && (r.colonnes || []).length) {
        var cles = r.cles || [];
        h += '<table class="as-rep-table"><thead><tr>'
           + r.colonnes.map(function(c) { return '<th>' + _escapeHtml(c) + '</th>'; }).join('')
           + '</tr></thead><tbody>'
           + (r.lignes || []).map(function(l, i) {
                 var cle = cles[i] || '';
                 return '<tr' + (cle ? ' class="as-clic" data-cle="' + _escapeHtml(cle)
                     + '" title="Voir le d\u00e9tail"' : '') + '>'
                     + l.map(function(c, j) {
                         return '<td>' + _asCellule(c, j, r.colonnes, cle) + '</td>';
                     }).join('') + '</tr>';
             }).join('')
           + '</tbody></table>';
        if (r.aide_clic && cles.filter(function(c) { return !!c; }).length) {
            h += '<div class="as-rep-aide">' + _escapeHtml(r.aide_clic) + '</div>';
        }
    }
    zone.innerHTML = h;
    traState.reponse = r;
    zone.insertAdjacentHTML('afterbegin', '<div class="tra-actions">'
        + '<button type="button" class="rx-btn rx-btn-sm" id="tra-export">\u2B07 Exporter cette réponse</button></div>');
    var bExp = zone.querySelector('#tra-export');
    if (bExp) bExp.addEventListener('click', _traExporter);
    var tableau = zone.querySelector('.as-rep-table');
    if (tableau) tableau.addEventListener('click', function(ev) {
        var tr = ev.target.closest && ev.target.closest('tr.as-clic');
        if (!tr) return;
        _asDetOuvrir(tr.getAttribute('data-cle'), AS_CTX_TRANSFERTS);
    });
    if (!traState.zoneBound) {
        traState.zoneBound = true;
        zone.addEventListener('click', function(ev) {
            var e = ev.target.closest && ev.target.closest('.as-clic-el');
            if (e) _asDetOuvrir(e.getAttribute('data-cle'), AS_CTX_TRANSFERTS);
        });
    }
}

var trBonsMagasin = '';

async function _trChargerBons() {
    var d = await rpc('/mavie/api/transferts-bons', {});
    if (!d || d.error) return;
    trState.bonsCrees = d.bons || [];
    _trRenderBons();
    _trChargerBilan();
}

function _trArticlesMagasin(bons, magasin) {
    var par = {};
    bons.forEach(function(b) {
        var sortie = b.source === magasin;
        var entree = b.dest === magasin;
        if (!sortie && !entree) return;
        (b.lignes || []).forEach(function(l) {
            var c = par[l.variante_id] = par[l.variante_id] || {
                ref: l.ref, variante: l.variante, envoye: 0, recu: 0, envoyeEnCours: 0, recuEnCours: 0 };
            var qte = Number(l.qte) || 0;
            if (b.valide) {
                if (sortie) c.envoye += qte;
                if (entree) c.recu += qte;
            } else {
                if (sortie) c.envoyeEnCours += qte;
                if (entree) c.recuEnCours += qte;
            }
        });
    });
    var lignes = Object.keys(par).map(function(k) { return par[k]; });
    if (!lignes.length) return '<div class="rx-muted">Aucun article transféré pour ce magasin.</div>';
    return '<table class="as-rep-table"><thead><tr><th>Article</th><th>Variante</th>'
        + '<th>Envoyé (fait)</th><th>Reçu (fait)</th><th>Envoyé (en cours)</th><th>Reçu (en cours)</th><th></th></tr></thead><tbody>'
        + lignes.map(function(c) {
              var aller = c.envoye > 0 && c.recu > 0;
              return '<tr' + (aller ? ' style="background:#FEF2F2;"' : '') + '><td>' + _escapeHtml(c.ref)
                   + '</td><td>' + _escapeHtml(c.variante) + '</td><td>' + formatNumber(c.envoye)
                   + '</td><td>' + formatNumber(c.recu) + '</td><td>' + formatNumber(c.envoyeEnCours)
                   + '</td><td>' + formatNumber(c.recuEnCours) + '</td><td>'
                   + (aller ? '<span style="color:#B91C1C;font-weight:600;">aller-retour</span>' : '')
                   + '</td></tr>';
          }).join('')
        + '</tbody></table>';
}

function _trRenderBons() {
    var zone = el('tr-foot');
    if (!zone) return;
    var bons = trState.bonsCrees || [];
    if (!bons.length) {
        zone.innerHTML = '<div class="rx-muted">Aucun bon créé depuis le tableau de bord.</div>';
        return;
    }
    var noms = {};
    bons.forEach(function(b) { noms[b.source] = true; noms[b.dest] = true; });
    var choix = Object.keys(noms).sort();
    var affiches = trBonsMagasin
        ? bons.filter(function(b) { return b.source === trBonsMagasin || b.dest === trBonsMagasin; })
        : bons;
    var sens = !!trBonsMagasin;
    zone.innerHTML = '<div class="tr-bons-titre">Bons créés</div>'
        + '<div style="margin:6px 0;"><select id="tr-bons-magasin" class="rx-select">'
        + '<option value="">Tous les magasins</option>'
        + choix.map(function(m) {
              return '<option value="' + _escapeHtml(m) + '"'
                   + (m === trBonsMagasin ? ' selected="selected"' : '') + '>' + _escapeHtml(m) + '</option>';
          }).join('')
        + '</select></div>'
        + '<div class="rx-muted" style="margin-bottom:6px;"><a href="/mavie/transfer-bons-pdf?ids='
        + affiches.map(function(b) { return b.id; }).join(',')
        + '" target="_blank" rel="noopener">Tout imprimer (PDF)</a> · '
        + '<a href="#" id="tr-bons-csv">Exporter (CSV)</a></div>'
        + '<table class="as-rep-table"><thead><tr><th>Bon</th><th>Trajet</th>'
        + (sens ? '<th>Sens</th>' : '')
        + '<th>État</th><th>Créé le</th><th>Fait le</th><th>Reçu le</th>'
        + '<th>Pièces</th><th>Vendu depuis l’envoi</th><th></th></tr></thead><tbody>'
        + affiches.map(function(b) {
              var sensCell = '';
              if (sens) {
                  sensCell = '<td>' + (b.source === trBonsMagasin ? 'Envoyé' : 'Reçu') + '</td>';
              }
              return '<tr class="tr-bon-ligne" data-bon="' + b.id + '"><td>' + _escapeHtml(b.name)
                   + '</td><td>' + _escapeHtml(b.source) + ' → ' + _escapeHtml(b.dest) + '</td>'
                   + sensCell
                   + '<td>' + _escapeHtml(b.etat)
                   + '</td><td>' + _escapeHtml(_trQuand(b.cree) || '—')
                   + '</td><td>' + _escapeHtml(_trQuand(b.fait) || '—')
                   + '</td><td>' + _escapeHtml(_trQuand(b.recu) || '—')
                   + '</td><td>' + formatNumber(b.pieces) + '</td><td>' + formatNumber(b.vendu || 0)
                   + '</td><td><a href="/mavie/transfer-bon-pdf/' + b.id
                   + '" target="_blank" rel="noopener">PDF</a></td></tr>';
          }).join('')
        + '</tbody></table>';
    if (trBonsMagasin) {
        zone.insertAdjacentHTML('beforeend', '<div class="tr-bilan" style="margin:12px 0 6px;"><b>Articles de '
            + _escapeHtml(trBonsMagasin) + '</b></div>'
            + _trArticlesMagasin(bons, trBonsMagasin));
    }
    var sel = el('tr-bons-magasin');
    if (sel) sel.addEventListener('change', function() {
        trBonsMagasin = sel.value;
        _trRenderBons();
    });
    var csv = el('tr-bons-csv');
    if (csv) csv.addEventListener('click', function(ev) {
        ev.preventDefault();
        _trExporterBons();
    });
    if (!zone.dataset.bonsLies) {
        zone.dataset.bonsLies = '1';
        zone.addEventListener('click', function(ev) {
            if (ev.target.closest && ev.target.closest('a')) return;
            var ligne = ev.target.closest && ev.target.closest('tr.tr-bon-ligne');
            if (!ligne) return;
            _asDetOuvrir('bon:' + ligne.getAttribute('data-bon'), {
                endpoint: '/mavie/api/transferts-assistant-detail',
                params: function() { var p = _trParams(); p.magasin_bon = trBonsMagasin; return p; },
                question: function() { return ''; }
            });
        });
    }
}

function _trExporterBons() {
    var bons = trState.bonsCrees || [];
    function cell(v) {
        return '"' + String(v === null || v === undefined ? '' : v).replace(/"/g, '""') + '"';
    }
    var lignes = ['Bon;Trajet;Etat;Cree le;Recu le;Pieces;Vendu depuis envoi'];
    bons.forEach(function(b) {
        lignes.push([cell(b.name), cell(b.source + ' -> ' + b.dest), cell(b.etat),
                     cell(b.cree), cell(b.recu), b.pieces, b.vendu || 0].join(';'));
    });
    var blob = new Blob([String.fromCharCode(0xFEFF) + lignes.join('\r\n')],
                        { type: 'text/csv;charset=utf-8;' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'bons-transferts.csv';
    a.click();
    URL.revokeObjectURL(url);
}

async function loadTransferts() {
    _trBindOnce();
    var zone = el('tr-pile');
    if (zone) zone.innerHTML = '<div class="rb-empty">Calcul en cours\u2026</div>';
    var data = await rpc('/mavie/api/transferts-proposition', _trParams());
    if (!data || data.error) {
        if (zone) zone.innerHTML = '<div class="rb-empty" style="color:#B91C1C;">'
            + '<b>Le calcul a \u00e9chou\u00e9.</b> '
            + _escapeHtml((data && data.error) || 'Erreur inconnue') + '</div>';
        return;
    }
    trState.data = data;
    trState.rows = data.rows || [];
    trState.index = 0;
    trState.traites = {};
    trState.exclus = {};
    _trRender();
    _trChargerBons();
}

function _trFiltrees() {
    var urg = el('tr-f-urgence');
    var seulesUrgentes = urg && urg.value === 'urgentes';
    var circuit = el('tr-f-circuit');
    var choixCircuit = circuit ? circuit.value : 'toutes';
    var sEl = el('tr-f-search');
    var q = sEl ? (sEl.value || '').trim().toLowerCase() : '';
    return trState.rows.filter(function(r) {
        if (seulesUrgentes && r.urgence === 'normale') return false;
        if (choixCircuit === 'interne' && !r.meme_societe) return false;
        if (choixCircuit === 'inter' && r.meme_societe) return false;
        if (q) {
            var hay = (r.reference + ' ' + r.produit + ' ' + r.source + ' '
                     + r.destination + ' ' + r.categorie).toLowerCase();
            if (hay.indexOf(q) === -1) return false;
        }
        return true;
    });
}

function _trQuand(utc) {
    if (!utc) return '';
    var d = new Date(utc.replace(' ', 'T') + 'Z');
    return d.toLocaleString('fr-FR', { day: '2-digit', month: '2-digit', year: 'numeric',
                                       hour: '2-digit', minute: '2-digit' });
}

function _trCle(r) { return r.variant_id + '|' + r.source_field + '|' + r.dest_field; }

// La pile : un bon par couple de magasins, le plus lourd d'abord.
function _trBons() {
    var par = {};
    _trFiltrees().forEach(function(r) {
        var cle = r.source_field + '>' + r.dest_field;
        var d = par[cle];
        if (!d) {
            d = par[cle] = {
                cle: cle, source: r.source, source_field: r.source_field,
                source_societe: r.source_societe, destination: r.destination,
                dest_field: r.dest_field, dest_societe: r.dest_societe,
                meme_societe: r.meme_societe, meme_ville: r.meme_ville,
                lignes: [],
            };
        }
        d.lignes.push(r);
    });
    var liste = [];
    for (var k in par) {
        if (!par.hasOwnProperty(k)) continue;
        var d = par[k];
        d.lignes.sort(function(a, b) { return b.quantite - a.quantite; });
        d.pieces = d.lignes.reduce(function(a, r) { return a + r.quantite; }, 0);
        d.valeur = d.lignes.reduce(function(a, r) { return a + r.valeur; }, 0);
        d.urgentes = d.lignes.filter(function(r) { return r.urgence !== 'normale'; }).length;
        liste.push(d);
    }
    liste.sort(function(a, b) { return b.valeur - a.valeur; });
    return liste;
}

function _trRestants() {
    return _trBons().filter(function(d) { return !trState.traites[d.cle]; });
}

function _trRetenues(bon) {
    return bon.lignes.filter(function(r) { return !trState.exclus[_trCle(r)]; });
}

function _trBadge(r) {
    if (r.aller_retour) {
        return ' <span class="tr-urg" style="background:#FEF2F2;color:#B91C1C;" title="Un bon existe déjà pour cette variante dans le sens inverse, dans la fenêtre de ventes">aller-retour</span>';
    }
    if (r.en_attente) {
        return ' <span class="tr-urg" style="background:#E2E8F0;color:#334155;" title="Un bon non livr\u00e9 existe d\u00e9j\u00e0 pour cette variante sur cette liaison">d\u00e9j\u00e0 en cours</span>';
    }
    if (r.urgence === 'rupture') {
        return ' <span class="tr-urg tr-urg-1" title="Le magasin destinataire n\u2019a plus '
             + 'aucune pi\u00e8ce de cette r\u00e9f\u00e9rence">rupture</span>';
    }
    if (r.urgence === 'critique') {
        return ' <span class="tr-urg tr-urg-2" title="Le magasin destinataire tient moins '
             + 'd\u2019une semaine">critique</span>';
    }
    return '';
}

function _trRender() {
    var data = trState.data;
    if (!data) return;
    var k = data.kpis || {};
    function set(id, txt) { var e = el(id); if (e) e.textContent = txt; }
    set('tr-kpi-nb', formatNumber(k.nb_propositions || 0));
    set('tr-kpi-nb-sub', formatNumber(k.nb_references || 0) + ' r\u00e9f\u00e9rences \u00b7 '
        + formatNumber(k.pieces || 0) + ' pi\u00e8ces');
    set('tr-kpi-paires', formatNumber(k.nb_paires || 0));
    set('tr-kpi-paires-sub', formatMAD(k.valeur || 0) + ' d\u00e9plac\u00e9s');
    set('tr-kpi-rupture', formatNumber(k.nb_rupture || 0));
    set('tr-kpi-rupture-sub', formatNumber(k.pieces_rupture || 0)
        + ' pi\u00e8ces \u00e0 servir en premier');

    var tous = _trBons();
    var restants = _trRestants();
    var cnt = el('tr-cnt');
    if (cnt) {
        cnt.textContent = tous.length
            ? formatNumber(restants.length) + ' bon' + (restants.length > 1 ? 's' : '')
              + ' \u00e0 passer sur ' + formatNumber(tous.length)
            : 'aucun';
    }
    _trCarte();

    var foot = el('tr-foot');
    if (foot) {
        foot.textContent = 'Vitesses mesur\u00e9es sur les ventes du '
            + _raFmtDate(data.date_debut) + ' au ' + _raFmtDate(data.date_reference)
            + ' \u00b7 couverture vis\u00e9e ' + (data.params || {}).cible + ' jours';
    }
}

// La carte du bon courant.
function _trCarte() {
    var zone = el('tr-pile');
    if (!zone) return;
    var tous = _trBons();
    if (!tous.length) {
        zone.innerHTML = '<div class="rb-empty">Aucun transfert \u00e0 proposer. '
            + 'Chaque magasin tient sa couverture, ou personne n\u2019a de stock \u00e0 '
            + 'c\u00e9der.</div>';
        return;
    }
    var restants = _trRestants();
    if (!restants.length) {
        var crees = 0, ecartes = 0;
        for (var c in trState.traites) {
            if (!trState.traites.hasOwnProperty(c)) continue;
            if (trState.traites[c] === 'cree') crees++; else ecartes++;
        }
        zone.innerHTML = '<div class="tr-fini"><div class="tr-fini-titre">'
            + '\u2713 Pile termin\u00e9e</div><div class="tr-fini-sous">'
            + formatNumber(crees) + ' bon(s) cr\u00e9\u00e9(s), '
            + formatNumber(ecartes) + ' \u00e9cart\u00e9(s). '
            + 'Les bons cr\u00e9\u00e9s sont en brouillon dans l\u2019historique des '
            + 'transferts.</div><div style="margin-top:14px;">'
            + '<button type="button" class="rx-btn rx-btn-sm" id="btn-tr-rejouer">'
            + '\u21bb Reprendre la pile</button></div></div>';
        var rejouer = el('btn-tr-rejouer');
        if (rejouer) rejouer.addEventListener('click', function() {
            trState.traites = {};
            trState.index = 0;
            _trRender();
        });
        return;
    }
    if (trState.index >= restants.length) trState.index = restants.length - 1;
    if (trState.index < 0) trState.index = 0;
    var bon = restants[trState.index];
    var retenues = _trRetenues(bon);
    var pieces = retenues.reduce(function(a, r) { return a + r.quantite; }, 0);
    var valeur = retenues.reduce(function(a, r) { return a + r.valeur; }, 0);
    var faits = tous.length - restants.length;

    var h = '<div class="tr-pile">'
          + '<div class="tr-progres"><span>' + formatNumber(faits) + ' sur '
            + formatNumber(tous.length) + ' trait\u00e9s</span>'
          + '<span class="tr-jauge"><span class="tr-jauge-faite" style="width:'
            + Math.round(faits / tous.length * 100) + '%"></span></span>'
          + '<span>' + formatNumber(restants.length) + ' restant'
            + (restants.length > 1 ? 's' : '') + '</span></div>';

    h += '<div class="tr-carte"><div class="tr-carte-tete">'
       + '<div class="tr-carte-num">Bon ' + formatNumber(trState.index + 1) + ' sur '
         + formatNumber(restants.length) + '</div>'
       + '<div class="tr-carte-trajet">'
         + '<span>' + _escapeHtml(bon.source)
           + '<span class="tr-carte-soc"> ' + _escapeHtml(bon.source_societe) + '</span></span>'
         + '<span class="tr-fleche">\u2192</span>'
         + '<span>' + _escapeHtml(bon.destination)
           + '<span class="tr-carte-soc"> ' + _escapeHtml(bon.dest_societe) + '</span></span>'
         + (bon.meme_societe
             ? ' <span class="tr-lien" title="M\u00eame soci\u00e9t\u00e9 : simple transfert de stock interne">Même société</span>'
             : ' <span class="tr-lien" title="Soci\u00e9t\u00e9s diff\u00e9rentes : le bon passe par le circuit inter-soci\u00e9t\u00e9s">Entre sociétés</span>')
         + (bon.meme_ville ? ' <span class="tr-lien">m\u00eame ville</span>' : '')
       + '</div>'
       + '<div class="tr-carte-chiffres">'
         + '<div class="tr-carte-c"><span>' + formatNumber(retenues.length) + '</span>r\u00e9f\u00e9rences</div>'
         + '<div class="tr-carte-c"><span>' + formatNumber(pieces) + '</span>pi\u00e8ces</div>'
         + '<div class="tr-carte-c"><span>' + formatMAD(valeur) + '</span>valeur</div>'
         + (bon.urgentes ? '<div class="tr-carte-c"><span>' + formatNumber(bon.urgentes)
             + '</span>urgentes</div>' : '')
       + '</div></div>';

    h += '<div class="tr-carte-corps">';
    bon.lignes.forEach(function(r) {
        var cle = _trCle(r);
        var dedans = !trState.exclus[cle];
        h += '<div class="tr-art' + (dedans ? '' : ' tr-art-hors') + '">'
           + '<div><input type="checkbox" class="tr-chk" data-ligne="' + _escapeHtml(cle)
             + '"' + (dedans ? ' checked="checked"' : '')
             + ' title="D\u00e9cochez pour laisser cette r\u00e9f\u00e9rence hors du bon"/></div>'
           + '<div><span class="rx-ref tr-art-ref" data-article="' + r.article_id
             + '" data-name="' + _escapeHtml(r.produit) + '">' + _escapeHtml(r.reference)
             + (r.variante ? ' <span class="tr-art-cat">' + _escapeHtml(r.variante) + '</span>' : '')
             + '</span>' + _trBadge(r)
             + '<span class="tr-art-cat">' + _escapeHtml(r.categorie || '') + '</span></div>'
           + '<div class="tr-art-info" title="Stock de l\u2019exp\u00e9diteur, et le temps '
             + 'qu\u2019il mettrait \u00e0 l\u2019\u00e9couler seul">'
             + formatNumber(r.source_stock) + ' en stock<br/>'
             + (r.source_couverture === null ? 'jamais vendu'
                : formatNumber(r.source_couverture) + ' j de couverture') + '</div>'
           + '<div class="tr-art-info" title="Stock du destinataire, et le temps qu\u2019il '
             + 'tient encore">' + formatNumber(r.dest_stock) + ' sur place<br/>'
             + formatNumber(r.dest_couverture) + ' j</div>'
           + '<div class="tr-art-qte">' + formatNumber(r.quantite) + ' pi\u00e8ces</div>'
           + '</div>';
    });
    h += '</div>';

    h += '<div class="tr-carte-pied">'
       + '<button type="button" class="tr-btn-creer" id="btn-tr-creer"'
         + (retenues.length ? '' : ' disabled="disabled"')
         + ' title="Le bon part en brouillon : rien ne quitte le magasin avant validation">'
         + '\u2713 Cr\u00e9er ce bon</button>'
       + '<button type="button" class="tr-btn-ecarter" id="btn-tr-ecarter"'
         + ' title="Passer ce bon sans le cr\u00e9er">\u2715 \u00c9carter</button>'
       + '<span class="tr-nav">'
         + '<button type="button" id="btn-tr-prec"'
           + (trState.index <= 0 ? ' disabled="disabled"' : '') + '>\u2039 Pr\u00e9c\u00e9dent</button>'
         + '<button type="button" id="btn-tr-suiv"'
           + (trState.index >= restants.length - 1 ? ' disabled="disabled"' : '')
           + '>Suivant \u203a</button>'
       + '</span></div></div></div>';
    zone.innerHTML = h;

    var creer = el('btn-tr-creer');
    if (creer) creer.addEventListener('click', function() { _trCreer(_trRetenues(bon), bon); });
    var ecarter = el('btn-tr-ecarter');
    if (ecarter) ecarter.addEventListener('click', function() {
        trState.traites[bon.cle] = 'ecarte';
        _trRender();
    });
    var prec = el('btn-tr-prec');
    if (prec) prec.addEventListener('click', function() { trState.index -= 1; _trCarte(); });
    var suiv = el('btn-tr-suiv');
    if (suiv) suiv.addEventListener('click', function() { trState.index += 1; _trCarte(); });
}

// Creer les bons : un par couple de magasins, chacun portant toutes ses
// references retenues. On passe par la meme route que la page Action, donc
// memes controles de stock, meme circuit inter-societes, meme historique.
async function _trCreer(lignes, bon) {
    if (!lignes || !lignes.length) return;
    var bons = {};
    lignes.forEach(function(r) {
        var cle = r.source_field + '>' + r.dest_field;
        (bons[cle] = bons[cle] || []).push(r);
    });
    var cles = Object.keys(bons);
    var pieces = lignes.reduce(function(a, r) { return a + r.quantite; }, 0);
    var apercu = cles.slice(0, 8).map(function(c) {
        var lot = bons[c];
        return '\u2022 ' + lot[0].source + ' \u2192 ' + lot[0].destination + '  ('
             + lot.length + ' r\u00e9f., '
             + lot.reduce(function(a, r) { return a + r.quantite; }, 0) + ' pi\u00e8ces)';
    }).join('\n');
    var texte = 'Cr\u00e9er ' + cles.length + ' bon' + (cles.length > 1 ? 's' : '')
              + ' de transfert ?\n\n' + apercu
              + (cles.length > 8 ? '\n\u2026 et ' + (cles.length - 8) + ' autre(s)' : '')
              + '\n\n' + formatNumber(lignes.length) + ' r\u00e9f\u00e9rences, '
              + formatNumber(pieces) + ' pi\u00e8ces au total. Les bons partent en '
              + 'brouillon : rien ne quitte un magasin avant validation.';
    if (!window.confirm(texte)) return;
    var bouton = el('btn-tr-creer');
    if (bouton) { bouton.disabled = true; bouton.textContent = 'Cr\u00e9ation en cours\u2026'; }

    var faits = 0;
    var echecs = [];
    for (var i = 0; i < cles.length; i++) {
        var lot = bons[cles[i]];
        var okCouple = 0;
        for (var j = 0; j < lot.length; j++) {
            var r = lot[j];
            // Le stock reel par variante : une reference se decline en
            // couleurs et tailles, et c'est la variante qui se transfere.
            var res = await rpc('/mavie/api/transfer-create', {
                product_tmpl_id: r.article_id,
                source_shop_field: r.source_field,
                dest_shop_field: r.dest_field,
                lines: [{ product_id: r.variant_id, qty: r.quantite }] });
            if (res && res.error) echecs.push(r.reference + ' : ' + res.error);
            else okCouple += 1;
        }
        if (okCouple) faits += 1;
    }
    var msg = faits + ' bon(s) de transfert cr\u00e9\u00e9(s) sur ' + cles.length + '.';
    if (echecs.length) {
        msg += '\n\nNon trait\u00e9es :\n' + echecs.slice(0, 10).join('\n');
        if (echecs.length > 10) msg += '\n\u2026 et ' + (echecs.length - 10) + ' autre(s)';
    }
    msg += '\n\nRetrouvez-les dans l\u2019historique des transferts.';
    window.alert(msg);
    // On ne recharge pas : la pile repartirait de zero et ferait repasser
    // les bons deja traites. On marque celui-ci et on passe au suivant.
    if (bon) trState.traites[bon.cle] = faits ? 'cree' : 'ecarte';
    _trRender();
    _trChargerBons();
}

function _trExportCsv(choisies) {
    var rows = (choisies && choisies.length) ? choisies : _trFiltrees();
    var sep = ';';
    function cell(v) {
        return '"' + String(v === null || v === undefined ? '' : v).replace(/"/g, '""') + '"';
    }
    var lignes = ['Reference' + sep + 'Produit' + sep + 'Categorie' + sep + 'Expediteur'
                  + sep + 'Societe expediteur' + sep + 'Destinataire'
                  + sep + 'Societe destinataire' + sep + 'A deplacer'
                  + sep + 'Stock expediteur' + sep + 'Couverture expediteur (j)'
                  + sep + 'Stock destinataire' + sep + 'Couverture destinataire (j)'
                  + sep + 'Valeur' + sep + 'Urgence' + sep + 'Circuit'];
    rows.forEach(function(r) {
        lignes.push([cell(r.reference), cell(r.produit), cell(r.categorie),
                     cell(r.source), cell(r.source_societe),
                     cell(r.destination), cell(r.dest_societe),
                     r.quantite, r.source_stock,
                     r.source_couverture === null ? '' : r.source_couverture,
                     r.dest_stock, r.dest_couverture,
                     String(r.valeur).replace('.', ','), cell(r.urgence),
                     cell(r.meme_societe ? 'interne' : 'inter-societes')].join(sep));
    });
    var blob = new Blob(['\ufeff' + lignes.join('\r\n')], { type: 'text/csv;charset=utf-8;' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'propositions-transferts.csv';
    a.click();
    URL.revokeObjectURL(url);
}

function _trBindOnce() {
    if (trState.bound) return;
    trState.bound = true;
    var reglages = el('btn-tr-settings');
    if (reglages) reglages.addEventListener('click', function() {
        var box = el('tr-settings');
        if (!box) return;
        var ouvert = box.style.display === 'none';
        box.style.display = ouvert ? '' : 'none';
        reglages.setAttribute('aria-expanded', ouvert ? 'true' : 'false');
    });
    var recalc = el('btn-tr-recalc');
    if (recalc) recalc.addEventListener('click', loadTransferts);
    var exp = el('btn-tr-export');
    if (exp) exp.addEventListener('click', function() { _trExportCsv(); });
    // Changer un filtre refait la pile : on repart du premier bon, sinon on
    // se retrouverait au milieu d'une pile qui n'est plus la meme.
    var circuit = el('tr-f-circuit');
    if (circuit) circuit.addEventListener('change', function() {
        trState.index = 0;
        _trRender();
    });
    var urg = el('tr-f-urgence');
    if (urg) urg.addEventListener('change', function() {
        trState.index = 0;
        _trRender();
    });
    var rech = el('tr-f-search');
    if (rech) {
        var t = null;
        rech.addEventListener('input', function() {
            clearTimeout(t);
            t = setTimeout(function() { trState.index = 0; _trRender(); }, 180);
        });
    }
    // La carte : retirer une reference du bon, ou ouvrir la fiche article.
    var pile = el('tr-pile');
    if (pile) pile.addEventListener('click', function(ev) {
        var chk = ev.target.closest && ev.target.closest('.tr-chk');
        if (chk) {
            var cle = chk.getAttribute('data-ligne');
            if (chk.checked) delete trState.exclus[cle];
            else trState.exclus[cle] = true;
            _trCarte();
            return;
        }
        var ligne = ev.target.closest && ev.target.closest('.tr-art');
        var ref = ligne && ligne.querySelector('.rx-ref');
        if (!ref) return;
        var id = parseInt(ref.getAttribute('data-article'), 10);
        if (id) openDetail(id, ref.getAttribute('data-name') || ref.textContent);
    });
}

function _spSetCount(txt) { var e = el('sp-cnt'); if (e) e.textContent = txt; }

// ── Journal des remises posees ─────────────────────────────────────
// La section Historique du tableau de bord montre les VENTES faites sous
// le prix catalogue ; ici on montre ce qui a ete POSE, meme si rien n'a
// encore ete vendu. C'est ce que l'utilisatrice cherchait.
var sjState = { rows: [], bound: false, charge: false };

async function _sjCharger(force) {
    if (sjState.charge && !force) return;
    var corps = el('sj-body');
    if (corps) corps.innerHTML = '<div class="rb-empty">Lecture du journal\u2026</div>';
    var d = await rpc('/mavie/api/soldes-journal', {
        date_start: state.date_start, date_end: state.date_end });
    if (!d || d.error) {
        if (corps) corps.innerHTML = '<div class="rb-empty" style="color:#B91C1C;">'
            + _escapeHtml((d && d.error) || 'Erreur inconnue') + '</div>';
        return;
    }
    sjState.charge = true;
    sjState.rows = d.rows || [];
    _sjRender(d);
}

function _sjRender(d) {
    var k = (d && d.kpis) || {};
    var rows = sjState.rows;
    var cnt = el('sj-cnt');
    if (cnt) {
        cnt.textContent = rows.length
            ? formatNumber(k.nb_actives || 0) + ' en cours \u00b7 '
              + formatNumber(k.nb_references || 0) + ' r\u00e9f\u00e9rences \u00b7 '
              + formatNumber(k.pieces_vendues || 0) + ' pi\u00e8ces vendues depuis'
            : 'aucune';
    }
    var corps = el('sj-body');
    if (!corps) return;
    if (!rows.length) {
        corps.innerHTML = '<div class="rb-empty">Aucune remise pos\u00e9e pour le moment. '
            + 'Les remises appliqu\u00e9es depuis cette page appara\u00eetront ici.</div>';
        return;
    }
    var h = '<div class="rx-scroll"><table class="rx-table"><thead><tr>'
          + '<th>R\u00e9f\u00e9rence</th><th>Magasin</th><th>Soci\u00e9t\u00e9</th>'
          + '<th class="num" title="Prix de l\u2019\u00e9tiquette TTC">Catalogue</th>'
          + '<th class="num">Prix sold\u00e9</th><th class="num">Remise</th>'
          + '<th class="num" title="Date de d\u00e9but de la r\u00e8gle de prix">Depuis</th>'
          + '<th class="num" title="Date de fin, si la remise en a une">Jusqu\u2019au</th>'
          + '<th class="num" title="Pi\u00e8ces vendues dans CE magasin depuis la pose de la remise">Vendu depuis</th>'
          + '<th class="num" title="Recette encaiss\u00e9e sur ces ventes">Recette</th>'
          + '<th>\u00c9tat</th><th class="num" title="Qui a pos\u00e9 la remise, et quand">Pos\u00e9e par</th>'
          + '</tr></thead><tbody>';
    rows.forEach(function(r) {
        var etat = r.expiree
            ? '<span class="sj-etat sj-fini" title="La date de fin est pass\u00e9e : la remise ne s\u2019applique plus en caisse">termin\u00e9e</span>'
            : (r.vendu_depuis
                ? '<span class="sj-etat sj-vend" title="Cette remise a fait vendre">elle vend</span>'
                : '<span class="sj-etat sj-muet" title="Aucune vente depuis la pose : la remise n\u2019a pas pris">rien vendu</span>');
        h += '<tr' + (r.expiree ? ' style="background:#F8FAFC;"' : '') + '>'
           + '<td><span class="rx-ref" data-article="' + r.article_id + '" data-name="'
             + _escapeHtml(r.produit) + '">' + _escapeHtml(r.reference) + '</span>'
           + (r.variante ? ' <span class="mfl-code">' + _escapeHtml(r.variante) + '</span>' : '')
           + '</td>'
           + '<td' + (r.partagee ? ' title=\"Liste de prix partagée : ' + _escapeHtml((r.magasins || []).join(', ')) + '\"' : '') + '>'
             + _escapeHtml(r.magasin)
             + (r.partagee ? ' <span class=\"mfl-code\">partagée</span>' : '') + '</td>'
           + '<td class="rx-muted">' + _escapeHtml(r.societe) + '</td>'
           + '<td class="num">' + formatMAD(r.prix_catalogue) + '</td>'
           + '<td class="num" style="font-weight:700;">' + formatMAD(r.prix_solde) + '</td>'
           + '<td class="num" style="color:#B45309;font-weight:700;">\u2212'
             + formatNumber(r.remise) + ' %</td>'
           + '<td class="num">' + _raFmtDate(r.debut) + '</td>'
           + '<td class="num">' + (r.fin ? _raFmtDate(r.fin)
               : '<span class="rx-muted" title="Aucune date de fin : la remise court ind\u00e9finiment">\u2014</span>') + '</td>'
           + '<td class="num"' + (r.vendu_depuis ? ' style="font-weight:700;color:#166534;"'
               : ' style="color:#B91C1C;"') + '>' + formatNumber(r.vendu_depuis) + '</td>'
           + '<td class="num">' + (r.recette_depuis ? formatMAD(r.recette_depuis)
               : '<span class="rx-muted">\u2014</span>') + '</td>'
           + '<td>' + etat + '</td>'
           + '<td class="num rx-muted" title="' + _escapeHtml(r.pose_le) + '">'
             + _escapeHtml(r.par) + '</td>'
           + '</tr>';
    });
    h += '</tbody></table></div>';
    corps.innerHTML = h;
    var foot = el('sj-foot');
    if (foot) {
        foot.textContent = formatNumber(k.nb_regles || 0) + ' remise(s) pos\u00e9e(s) sur '
            + formatNumber(k.nb_references || 0) + ' r\u00e9f\u00e9rences et '
            + formatNumber(k.nb_magasins || 0) + ' magasins \u00b7 '
            + formatNumber(k.nb_sans_effet || 0) + ' en cours sans aucune vente \u00b7 '
            + formatMAD(k.recette || 0) + ' encaiss\u00e9s depuis'
            + ((d && d.tronque) ? ' \u00b7 les ' + formatNumber(k.nb_regles)
               + ' plus r\u00e9centes sont affich\u00e9es' : '');
    }
}

// Remises proposees dans la ligne : on ajuste sans ouvrir le panneau, et
// le recap de la selection suit immediatement. Les paliers offerts sont
// ceux que la maison pratique vraiment (releve du journal des remises) ;
// la graduation ronde ne sert que si l'historique est encore vide.
var SP_REMISES_DEFAUT = [10, 20, 30, 40, 50, 60, 70];

function _spEchelle() {
    var cal = (spState.data && spState.data.calibrage) || {};
    var e = cal.echelle || [];
    return e.length ? e : SP_REMISES_DEFAUT;
}

function _spRemise(r) {
    var o = spState.remises[r.article_id];
    return (o === undefined || o === null) ? r.remise : o;
}

function _spPrixSolde(r) {
    return Math.round(r.prix_ttc * (100 - _spRemise(r))) / 100;
}

// D'ou vient le palier propose. On le dit dans l'infobulle : un conseil
// dont on ne connait pas la source ne peut pas etre juge.
function _spSourceRemise(r) {
    var n = r.remise_source_regles || 0;
    if (r.remise_source === 'plafond') {
        return 'La remise est d\u00e9j\u00e0 au maximum : elle ne peut plus monter.';
    }
    if (r.remise_source === 'accentuee') {
        return 'Cette r\u00e9f\u00e9rence porte d\u00e9j\u00e0 \u2212'
             + r.remise_en_place + ' % et n\u2019a pas boug\u00e9 : la proposition monte au '
             + 'palier que vous pratiquez juste au-dessus.';
    }
    if (r.remise_source === 'categorie') {
        return 'Habitude de la cat\u00e9gorie ' + (r.categorie || '')
             + ' : relev\u00e9 sur ' + formatNumber(n) + ' remises que vous y avez '
             + 'd\u00e9j\u00e0 pos\u00e9es.';
    }
    if (r.remise_source === 'general') {
        return 'Aucun pr\u00e9c\u00e9dent suffisant dans cette cat\u00e9gorie : palier '
             + 'issu du relev\u00e9 de toutes vos remises pos\u00e9es.';
    }
    return 'Palier des r\u00e9glages de la page.';
}

function _spOptionsRemise(rem) {
    var vals = _spEchelle().slice();
    if (vals.indexOf(rem) === -1) {
        vals.push(rem);
        vals.sort(function(a, b) { return a - b; });
    }
    return vals.map(function(v) {
        return '<option value="' + v + '"' + (v === rem ? ' selected="selected"' : '')
             + '>\u2212' + v + ' %</option>';
    }).join('');
}

// Toute ligne cochee compte, filtre compris : reposer une remise plus
// franche sur une reference deja soldee est un geste normal.
function _spSelectionnees() {
    return spState.rows.filter(function(r) { return !!spState.sel[r.article_id]; });
}

// Les magasins ou la remise peut reellement etre posee : il leur faut du
// stock ET une caisse. Sans caisse, aucune liste de prix a modifier.
function _spMagsSoldables(r) { return r.magasins_soldables || []; }

function _spMagsChoisis(r) {
    var dispo = _spMagsSoldables(r);
    var retenus = spState.mags[r.article_id];
    if (!retenus) return dispo;
    return dispo.filter(function(m) { return retenus.indexOf(m.shop_field) !== -1; });
}

// Les pieces reellement touchees : celles des magasins retenus, pas tout
// le stock de la reference.
function _spPieces(r) {
    return _spMagsChoisis(r).reduce(function(a, m) { return a + (m.stock || 0); }, 0);
}

// Le recap : ce que la selection immobilise, ce qu'elle rapporterait aux
// remises retenues, et ce que la demarque coute. Les trois chiffres qui
// decident d'une operation.
function _spRecap() {
    var bar = el('sp-bar');
    if (!bar) return;
    var sel = _spSelectionnees();
    if (!sel.length) {
        bar.style.display = 'none';
        bar.innerHTML = '';
        return;
    }
    var pieces = 0, depot = 0, catalogue = 0, recette = 0, magasins = 0, sansOu = 0;
    sel.forEach(function(r) {
        var pr = _spPieces(r);
        var mg = _spMagsChoisis(r).length;
        if (!mg) sansOu++;
        magasins += mg;
        pieces += pr;
        depot += r.depot || 0;
        catalogue += (pr + (r.depot || 0)) * r.prix_ttc;
        recette += (pr + (r.depot || 0)) * _spPrixSolde(r);
    });
    var pluriel = sel.length > 1 ? 's' : '';
    bar.style.display = '';
    bar.innerHTML =
        '<div class="sp-bar-chiffres">'
      + '<div class="sp-bar-c"><span>' + formatNumber(sel.length) + '</span>r\u00e9f\u00e9rence'
        + pluriel + ' coch\u00e9e' + pluriel + '</div>'
      + '<div class="sp-bar-c"><span>' + formatNumber(pieces) + '</span>pi\u00e8ces en magasin</div>'
      + '<div class="sp-bar-c"><span>' + formatNumber(magasins) + '</span>remises \u00e0 poser</div>'
      + '<div class="sp-bar-c"><span>' + formatNumber(depot) + '</span>pi\u00e8ces au d\u00e9p\u00f4t</div>'
      + '<div class="sp-bar-c"><span>' + formatMAD(catalogue) + '</span>valeur au catalogue</div>'
      + '<div class="sp-bar-c sp-bar-ok"><span>' + formatMAD(recette)
        + '</span>recette si tout part sold\u00e9</div>'
      + '</div>'
      + (sansOu ? '<div class="sp-bar-avert">' + formatNumber(sansOu)
          + ' r\u00e9f\u00e9rence(s) coch\u00e9e(s) sans magasin o\u00f9 poser la remise : '
          + 'elles seront ignor\u00e9es.</div>' : '')
      + '<div class="sp-bar-actions">'
      + '<button type="button" class="rx-btn rx-btn-sm" id="btn-sp-vider">Tout d\u00e9cocher</button>'
      + '<button type="button" class="rx-btn rx-btn-sm" id="btn-sp-export-sel">Exporter la s\u00e9lection</button>'
      + '<button type="button" class="sp-btn-lot" id="btn-sp-lot">\ud83c\udff7\ufe0f Solder la s\u00e9lection ('
        + formatNumber(sel.length) + ')</button>'
      + '</div>';
    var vider = el('btn-sp-vider');
    if (vider) vider.addEventListener('click', function() { spState.sel = {}; _spRender(); });
    var expo = el('btn-sp-export-sel');
    if (expo) expo.addEventListener('click', function() { _spExportCsv(_spSelectionnees()); });
    var lot = el('btn-sp-lot');
    if (lot) lot.addEventListener('click', _spSolderLot);
}

// Detail deplie sous la ligne : ou est le stock, quelle caisse le vend, et
// quelle remise y est deja posee. Sans ca, impossible de savoir si la
// demarque touchera vraiment de la marchandise.
async function _spDetail(aid, bouton) {
    var cell = el('sp-det-' + aid);
    var ligne = document.querySelector('.sp-det-row[data-det="' + aid + '"]');
    if (!cell || !ligne) return;
    var ouvert = ligne.style.display !== 'none';
    ligne.style.display = ouvert ? 'none' : '';
    if (bouton) bouton.textContent = ouvert ? '\u25b8' : '\u25be';
    if (ouvert || cell.getAttribute('data-charge') === '1') return;
    cell.innerHTML = '<div class="rx-muted">Lecture des magasins\u2026</div>';
    var d = await rpc('/mavie/api/solde-context', { product_tmpl_id: aid });
    if (!d || d.error) {
        cell.innerHTML = '<div style="color:#B91C1C;">'
            + _escapeHtml((d && d.error) || 'Erreur inconnue') + '</div>';
        return;
    }
    cell.setAttribute('data-charge', '1');
    var r = (spState.rows.filter(function(x) { return x.article_id === aid; }) || [])[0];
    var prix = r ? _spPrixSolde(r) : 0;
    // Les magasins viennent de la ligne (stock reel, caisse ou non) ; le
    // contexte y ajoute le nom des caisses et la remise deja en place.
    var parChamp = {};
    (d.magasins || []).forEach(function(m) { parChamp[m.shop_field] = m; });
    var liste = (r && r.magasins) ? r.magasins : [];
    var retenus = r ? _spMagsChoisis(r).map(function(m) { return m.shop_field; }) : [];
    var h = '<div class="sp-det-titre">' + _escapeHtml(d.nom || '')
          + ' \u2014 catalogue ' + formatMAD(d.prix_catalogue_ttc || 0)
          + (prix ? ', sold\u00e9 \u00e0 ' + formatMAD(prix) : '') + '</div>';
    if (!liste.length) {
        h += '<div class="rx-muted">Aucun magasin n\u2019a de stock de cette '
           + 'r\u00e9f\u00e9rence : une remise n\u2019y changerait rien.</div>';
    } else {
        h += '<div class="sp-det-aide">D\u00e9cochez un magasin pour l\u2019exclure de '
           + 'l\u2019op\u00e9ration : une remise ne se fait pas toujours partout.</div>'
           + '<table class="sp-det-table"><thead><tr><th class="sp-c"></th><th>Magasin</th>'
           + '<th>Soci\u00e9t\u00e9</th><th>Stock</th><th>Valeur sold\u00e9e</th>'
           + '<th>Caisse</th><th>Remise d\u00e9j\u00e0 en place</th></tr></thead><tbody>'
           + liste.map(function(m) {
                 var ctx = parChamp[m.shop_field] || {};
                 var coche = m.soldable && retenus.indexOf(m.shop_field) !== -1;
                 return '<tr' + (m.soldable ? '' : ' style="opacity:.6;"') + '>'
                      + '<td class="sp-c">' + (m.soldable
                          ? '<input type="checkbox" class="sp-mag" data-article="' + aid
                            + '" data-shop="' + _escapeHtml(m.shop_field) + '"'
                            + (coche ? ' checked="checked"' : '') + '/>'
                          : '') + '</td>'
                      + '<td>' + _escapeHtml(m.libelle || '') + '</td>'
                      + '<td>' + _escapeHtml(m.societe || '\u2014') + '</td>'
                      + '<td>' + formatNumber(m.stock) + '</td>'
                      + '<td>' + (m.soldable ? formatMAD((m.stock || 0) * prix)
                                             : '<span class="rx-muted">\u2014</span>') + '</td>'
                      + '<td>' + ((ctx.caisses || []).length
                          ? _escapeHtml(ctx.caisses.join(', '))
                          : '<span style="color:#B91C1C;">aucune caisse</span>') + '</td>'
                      + '<td>' + (ctx.regle
                          ? formatMAD(ctx.regle.prix_ttc)
                            + (ctx.regle.date_end
                               ? ' jusqu\u2019au ' + _raFmtDate(ctx.regle.date_end) : '')
                          : '\u2014') + '</td></tr>';
             }).join('')
           + '</tbody></table>';
        var sansCaisse = liste.filter(function(m) { return !m.soldable; }).length;
        if (sansCaisse) {
            h += '<div class="rx-muted" style="margin-top:6px;">' + formatNumber(sansCaisse)
               + ' magasin(s) d\u00e9tiennent du stock mais n\u2019ont pas de caisse : '
               + 'aucune remise ne peut y \u00eatre pos\u00e9e, il faut d\u00e9placer la '
               + 'marchandise.</div>';
        }
    }
    cell.innerHTML = h;
}

// Solder la selection : une seule demande au serveur, qui repasse par le
// meme chemin que le panneau, reference par reference. Ce qui echoue est
// nomme, le reste est applique.
async function _spSolderLot() {
    // Une reference sans magasin retenu n'a rien a poser : on l'ecarte ici
    // plutot que de laisser le serveur la refuser une par une.
    var sel = _spSelectionnees().filter(function(r) { return _spMagsChoisis(r).length; });
    var ecartees = _spSelectionnees().length - sel.length;
    if (!sel.length) {
        window.alert('Aucune des r\u00e9f\u00e9rences coch\u00e9es n\u2019a de magasin '
            + 'o\u00f9 poser la remise : il leur faut du stock et une caisse.');
        return;
    }
    var pieces = sel.reduce(function(a, r) { return a + _spPieces(r); }, 0);
    var remises = sel.reduce(function(a, r) { return a + _spMagsChoisis(r).length; }, 0);
    var apercu = sel.slice(0, 12).map(function(r) {
        var mg = _spMagsChoisis(r);
        return '\u2022 ' + r.reference + '   \u2212' + _spRemise(r) + ' %  \u2192  '
             + formatMAD(_spPrixSolde(r)) + '   (' + mg.length + ' magasin'
             + (mg.length > 1 ? 's' : '') + (r.deja_solde ? ', remise existante \u00e9cras\u00e9e' : '')
             + ')';
    }).join('\n');
    var texte = 'Appliquer la remise \u00e0 ' + sel.length + ' r\u00e9f\u00e9rence'
              + (sel.length > 1 ? 's' : '') + ' ?\n\n' + apercu
              + (sel.length > 12 ? '\n\u2026 et ' + (sel.length - 12) + ' autre(s)' : '')
              + (ecartees ? '\n\n' + ecartees + ' r\u00e9f\u00e9rence(s) coch\u00e9e(s) '
                 + 'sont \u00e9cart\u00e9es : aucun magasin o\u00f9 poser la remise.' : '')
              + '\n\n' + formatNumber(remises) + ' remise(s) \u00e0 poser, '
              + formatNumber(pieces) + ' pi\u00e8ces en magasin concern\u00e9es.'
              + (((spState.data && spState.data.portee
                   && spState.data.portee.partagees) || []).length
                 ? '\n\nATTENTION : la liste de prix de soldes est partag\u00e9e par '
                   + spState.data.portee.partagees[0].magasins.length
                   + ' magasins. La remise s\u2019appliquera dans tous, '
                   + 'pas seulement dans ceux coch\u00e9s.'
                 : '')
              + (spOperation && spOperation.active && !spOperation.terminee
                 ? '\n\nOp\u00e9ration « ' + spOperation.nom + ' » : '
                   + (spOperation.fin
                      ? 'les remises s\u2019arr\u00eateront le '
                        + _raFmtDate(spOperation.fin) + '.'
                      : 'aucune date de fin d\u00e9clar\u00e9e.')
                 : '\n\nAucune op\u00e9ration en cours : ces remises n\u2019auront pas '
                   + 'de date de fin.');
    if (!window.confirm(texte)) return;
    var bouton = el('btn-sp-lot');
    if (bouton) {
        bouton.disabled = true;
        bouton.textContent = 'Application en cours\u2026';
    }
    var params = _spParams();
    params.lignes = sel.map(function(r) {
        return { article_id: r.article_id, remise: _spRemise(r),
                 magasins: _spMagsChoisis(r).map(function(m) { return m.shop_field; }) };
    });
    // Les dates de l'operation, s'il y en a une en cours : c'est elle qui
    // donne une fin aux remises.
    if (spOperation && spOperation.active && !spOperation.terminee) {
        params.date_start = spOperation.debut;
        params.date_end = spOperation.fin || '';
    }
    var res = await rpc('/mavie/api/soldes-appliquer-lot', params);
    if (!res || res.error) {
        window.alert('La d\u00e9marque n\u2019a pas \u00e9t\u00e9 appliqu\u00e9e : '
            + ((res && res.error) || 'erreur inconnue'));
        if (bouton) bouton.disabled = false;
        _spRecap();
        return;
    }
    var echecs = (res.resultats || []).filter(function(x) { return !x.ok; });
    var msg = formatNumber(res.faits) + ' r\u00e9f\u00e9rence(s) sold\u00e9e(s) sur '
            + formatNumber(res.total) + ', dans ' + formatNumber(res.magasins) + ' magasin(s).';
    if (echecs.length) {
        msg += '\n\nNon trait\u00e9es :\n' + echecs.slice(0, 10).map(function(x) {
            return '\u2022 ' + x.reference + ' : ' + x.message;
        }).join('\n');
        if (echecs.length > 10) msg += '\n\u2026 et ' + (echecs.length - 10) + ' autre(s)';
    }
    window.alert(msg);
    spState.sel = {};
    spState.remises = {};
    spState.mags = {};
    await _spChargerOperation(true);
    await loadPropositions();
    // Ce qu'on vient de poser doit se voir : on ouvre le journal dessus.
    var journal = el('sj-bloc');
    if (journal) journal.setAttribute('data-open', '1');
    await _sjCharger(true);
}

function _spExportCsv(choisies) {
    // Appele soit par le bouton du bloc (un evenement, sans .length), soit
    // avec la selection cochee.
    var rows = (choisies && choisies.length) ? choisies : _spFiltrees();
    var sep = ';';
    var lignes = ['Reference' + sep + 'Produit' + sep + 'Categorie'
                  + sep + 'Urgence' + sep + 'Nb magasins' + sep + 'Qte achetee' + sep + 'Vendu' + sep + 'Stock magasins' + sep + 'Depot' + sep + 'Jours depuis derniere vente' + sep + 'Couverture (j)' + sep + 'Valeur'
                  + sep + 'Prix' + sep + 'Remise' + sep + 'Prix solde' + sep + 'Deja soldee'];
    function cell(v) {
        return '"' + String(v === null || v === undefined ? '' : v).replace(/"/g, '""') + '"';
    }
    rows.forEach(function(r) {
        lignes.push([cell(r.reference), cell(r.produit), cell(r.categorie),
                     cell(r.urgence), r.nb_magasins, r.achete || 0, r.vendu || 0, r.stock, r.depot || 0, r.jamais_vendu ? 'jamais' : r.jours_sans_vente, (r.couverture_jours === null || r.couverture_jours === undefined) ? '' : r.couverture_jours,
                     String(r.valeur).replace('.', ','), String(r.prix_ttc).replace('.', ','),
                     _spRemise(r), String(_spPrixSolde(r)).replace('.', ','),
                     cell(r.deja_solde ? 'oui' : 'non')].join(sep));
    });
    var blob = new Blob(['\ufeff' + lignes.join('\r\n')], { type: 'text/csv;charset=utf-8;' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'propositions-soldes.csv';
    a.click();
    URL.revokeObjectURL(url);
}

function _spBindOnce() {
    if (spState.bound) return;
    spState.bound = true;
    var reglages = el('btn-sp-settings');
    if (reglages) reglages.addEventListener('click', function() {
        var box = el('sp-settings');
        if (!box) return;
        var ouvert = box.style.display === 'none';
        box.style.display = ouvert ? '' : 'none';
        reglages.setAttribute('aria-expanded', ouvert ? 'true' : 'false');
    });
    var recalc = el('btn-sp-recalc');
    if (recalc) recalc.addEventListener('click', loadPropositions);
    var exp = el('btn-sp-export');
    if (exp) exp.addEventListener('click', _spExportCsv);
    var etat = el('sp-f-etat');
    if (etat) etat.addEventListener('change', _spRender);
    var rech = el('sp-f-search');
    if (rech) {
        var t = null;
        rech.addEventListener('input', function() {
            clearTimeout(t);
            t = setTimeout(_spRender, 180);
        });
    }
    var quest = el('as-questions');
    if (quest) quest.addEventListener('click', function(ev) {
        var b = ev.target.closest && ev.target.closest('.as-q');
        if (!b) return;
        var qid = b.getAttribute('data-q');
        // Un second clic sur la question ouverte la referme.
        if (asState.active === qid) {
            _asFermer();
            return;
        }
        // « Quelle remise » a besoin d'une reference : on prend la premiere
        // de la liste, l'utilisatrice en change ensuite.
        var premier = (spState.rows || [])[0];
        _asRepondre(qid, qid === 'remise_reference' && premier ? premier.article_id : 0);
    });

    var bloc = el('sp-bloc');
    if (bloc) {
        var tete = bloc.querySelector('.rb-head');
        if (tete) tete.addEventListener('click', function() {
            bloc.setAttribute('data-open', bloc.getAttribute('data-open') === '1' ? '0' : '1');
        });
    }
    var journal = el('sj-bloc');
    if (journal) {
        var teteJ = journal.querySelector('.rb-head');
        if (teteJ) teteJ.addEventListener('click', function() {
            var ouvert = journal.getAttribute('data-open') === '1';
            journal.setAttribute('data-open', ouvert ? '0' : '1');
            if (!ouvert) _sjCharger();
        });
    }
    var corpsJ = el('sj-body');
    if (corpsJ) corpsJ.addEventListener('click', function(ev) {
        var ref = ev.target.closest && ev.target.closest('.rx-ref');
        if (!ref) return;
        var id = parseInt(ref.getAttribute('data-article'), 10);
        if (id) openDetail(id, ref.getAttribute('data-name') || ref.textContent);
    });
    var corps = el('sp-body');
    if (corps) corps.addEventListener('change', function(ev) {
        var s = ev.target.closest && ev.target.closest('.sp-remise-sel');
        if (!s) return;
        var a = parseInt(s.getAttribute('data-article'), 10);
        var v = parseInt(s.value, 10);
        spState.remises[a] = v;
        var r = (spState.rows.filter(function(x) { return x.article_id === a; }) || [])[0];
        var cellule = document.querySelector('[data-row-prix="' + a + '"]');
        if (r && cellule) cellule.textContent = formatMAD(_spPrixSolde(r));
        var bs = document.querySelector('.sp-btn-solder[data-article="' + a + '"]');
        if (bs) bs.setAttribute('data-remise', v);
        // Une remise qu'on ajuste, c'est une reference qu'on veut traiter :
        // on la coche pour eviter un second geste. Y compris une reference
        // deja soldee : reposer une remise plus franche est un geste normal.
        if (r) {
            spState.sel[a] = true;
            var c = document.querySelector('.sp-chk[data-article="' + a + '"]');
            if (c) c.checked = true;
        }
        var det = el('sp-det-' + a);
        if (det) det.removeAttribute('data-charge');
        _spRecap();
    });
    if (corps) corps.addEventListener('change', function(ev) {
        var cm = ev.target.closest && ev.target.closest('.sp-mag');
        if (!cm) return;
        var am = parseInt(cm.getAttribute('data-article'), 10);
        var shop = cm.getAttribute('data-shop');
        var rm = (spState.rows.filter(function(x) { return x.article_id === am; }) || [])[0];
        if (!rm) return;
        // Tant qu'on n'a rien decoche, tous les magasins soldables sont
        // retenus : on part de cette liste pour la premiere modification.
        var retenus = spState.mags[am]
            || _spMagsSoldables(rm).map(function(m) { return m.shop_field; });
        if (cm.checked) {
            if (retenus.indexOf(shop) === -1) retenus = retenus.concat([shop]);
        } else {
            retenus = retenus.filter(function(x) { return x !== shop; });
        }
        spState.mags[am] = retenus;
        // Choisir des magasins, c'est vouloir traiter la reference.
        spState.sel[am] = true;
        var c = document.querySelector('.sp-chk[data-article="' + am + '"]');
        if (c) c.checked = true;
        _spRecap();
    });
    if (corps) corps.addEventListener('click', function(ev) {
        var tout = ev.target.closest && ev.target.closest('#sp-all');
        if (tout) {
            ev.stopPropagation();
            // Tout ce qui est affiche, sans exception : le filtre a deja
            // decide de ce qui est a l'ecran.
            var visibles = _spFiltrees().slice(0, RA_MAX_LIGNES);
            visibles.forEach(function(r) {
                if (tout.checked) spState.sel[r.article_id] = true;
                else delete spState.sel[r.article_id];
            });
            Array.prototype.forEach.call(document.querySelectorAll('.sp-chk'), function(c) {
                c.checked = !!spState.sel[parseInt(c.getAttribute('data-article'), 10)];
            });
            _spRecap();
            return;
        }
        var chk = ev.target.closest && ev.target.closest('.sp-chk');
        if (chk) {
            ev.stopPropagation();
            var ac = parseInt(chk.getAttribute('data-article'), 10);
            if (chk.checked) spState.sel[ac] = true;
            else delete spState.sel[ac];
            _spRecap();
            return;
        }
        var lo = ev.target.closest && ev.target.closest('.sp-expand');
        if (lo) {
            ev.stopPropagation();
            _spDetail(parseInt(lo.getAttribute('data-article'), 10), lo);
            return;
        }
        var btn = ev.target.closest && ev.target.closest('.sp-btn-solder');
        if (btn) {
            ev.stopPropagation();
            var aid = parseInt(btn.getAttribute('data-article'), 10);
            var rem = parseInt(btn.getAttribute('data-remise'), 10);
            // Le panneau arrive déjà rempli : remise posée et magasins
            // qui ont du stock cochés. Il ne reste qu'à valider.
            if (aid) openSoldePanel(aid, '', { remise: rem });
            return;
        }
        var ref = ev.target.closest && ev.target.closest('.rx-ref');
        if (!ref) return;
        var id = parseInt(ref.getAttribute('data-article'), 10);
        if (id) openDetail(id, ref.getAttribute('data-name') || ref.textContent);
    });
}

// Mêmes regroupements de villes que le serveur (ACTION_REGIONS).
var AC_REGIONS_VILLES = {
    'Grand Casablanca': ['Casablanca', 'Mohammadia'],
    'Rabat-Salé': ['Rabat', 'Témara'],
    'Souss (Agadir)': ['Agadir'],
    'Nord (Tanger)': ['Tanger'],
};

// Noms des magasins retenus par les filtres Lieux de la page Action.
function _acMagasinsFiltresNoms() {
    var lieux = (acState.lieux || {}).magasins || [];
    var noms = [];
    lieux.forEach(function(m) {
        var pris = (acState.magasins || []).indexOf(m.shop_field) !== -1
            || (acState.villes || []).indexOf(m.ville) !== -1
            || (acState.regions || []).some(function(r) {
                return (AC_REGIONS_VILLES[r] || []).indexOf(m.ville) !== -1;
            });
        if (pris && noms.indexOf(m.nom) === -1) noms.push(m.nom);
    });
    return noms;
}

// Historique des transferts d'une référence (colonne Transferts de la page
// Action) : d'abord ce qui s'est passé par magasin, puis tous les bons.
async function openHistoriqueTransferts(r, sens) {
    var ov = el('ac-transferts-overlay');
    if (!ov) return;
    var refEl = el('ac-tr-ref');
    if (refEl) refEl.textContent = r.ref + (sens === 'recu' ? ' · reçu' : (sens === 'envoye' ? ' · envoyé' : ''));
    var sub = el('ac-tr-sub'); if (sub) sub.textContent = r.name || '';
    var body = el('ac-tr-body');
    if (body) body.innerHTML = '<div class="ac-empty">Chargement…</div>';
    ov.classList.add('active');
    var data = await rpc('/mavie/api/transferts-reference', { article_id: r.id });
    if (!body) return;
    if (!data || data.error) {
        body.innerHTML = '<div class="ac-empty" style="color:#B91C1C;">Erreur : '
            + _escapeHtml((data && data.error) || 'inconnue') + '</div>';
        return;
    }
    var lignes = data.lignes || [];
    var magsFiltres = _acMagasinsFiltresNoms();
    if (sens && magsFiltres.length) {
        lignes = lignes.filter(function(l) {
            return magsFiltres.indexOf(sens === 'recu' ? l.dest : l.source) !== -1;
        });
    }
    var titreSens = sens === 'recu' ? 'Reçu' : (sens === 'envoye' ? 'Envoyé' : '');
    if (sub) sub.textContent = (data.nom || '')
        + (titreSens ? ' · ' + titreSens : '')
        + (magsFiltres.length ? ' · ' + magsFiltres.join(', ') : '')
        // Un brouillon n'a rien déplacé : il est compté à part, sinon le
        // pop-up annonçait 42 pièces là où le tableau en affiche 6.
        + ' · ' + formatNumber(lignes.filter(function(l) { return !l.brouillon; }).length)
        + ' bons · '
        + formatNumber(lignes.reduce(function(a, l) { return a + (l.brouillon ? 0 : l.qty); }, 0))
        + ' pièces déplacées'
        + (lignes.some(function(l) { return l.brouillon; })
            ? ' · ' + formatNumber(lignes.filter(function(l) { return l.brouillon; }).length)
              + ' brouillon(s) non validé(s), '
              + formatNumber(lignes.reduce(function(a, l) { return a + (l.brouillon ? l.qty : 0); }, 0))
              + ' pièces en attente'
            : '');
    if (!lignes.length) {
        body.innerHTML = '<div class="ac-empty">Aucun transfert pour cette référence.</div>';
        return;
    }
    var h = '';
    var mags = data.magasins || [];
    if (mags.length && !sens) {
        h += '<div class="ac-table-wrap" style="max-height:32vh;margin-bottom:12px;"><table class="ac-table"><thead><tr>'
           + '<th>Magasin</th><th class="num">Reçu</th><th class="num">Envoyé</th><th class="num">Net</th>'
           + '</tr></thead><tbody>';
        mags.forEach(function(m) {
            h += '<tr class="ac-var"><td><strong>' + _escapeHtml(m.magasin) + '</strong></td>'
               + '<td class="num"' + (m.recu ? ' style="color:#1D4ED8;font-weight:700;"' : '') + '>'
               + (m.recu ? '+ ' + formatNumber(m.recu) : '—') + '</td>'
               + '<td class="num"' + (m.envoye ? ' style="color:#3730A3;font-weight:700;"' : '') + '>'
               + (m.envoye ? '− ' + formatNumber(m.envoye) : '—') + '</td>'
               + '<td class="num"' + (m.net < 0 ? ' style="color:#DC2626;"' : '') + '>' + formatNumber(m.net) + '</td></tr>';
        });
        h += '</tbody></table></div><div class="ac-foot" style="margin:0 0 6px;">Tous les bons :</div>';
    }
    h += '<div class="ac-table-wrap" style="max-height:44vh;"><table class="ac-table"><thead><tr>'
       + '<th>Date</th><th>Bon</th><th>Départ</th><th>Arrivée</th><th>Sociétés</th>'
       + '<th class="num">Qté</th><th>État</th></tr></thead><tbody>';
    lignes.forEach(function(l) {
        h += '<tr class="ac-var">'
           + '<td>' + _escapeHtml(l.date) + '</td>'
           + '<td><strong>' + _escapeHtml(l.bon) + '</strong>'
           + (l.reassort ? ' <span style="color:#166534;">réassort</span>' : '') + '</td>'
           + '<td>' + _escapeHtml(l.source) + '</td>'
           + '<td>' + _escapeHtml(l.dest) + '</td>'
           + '<td class="ac-muted">' + _escapeHtml(l.societes) + '</td>'
           + '<td class="num">' + formatNumber(l.qty) + '</td>'
           + '<td' + (l.brouillon ? ' style="color:#94A3B8;"'
                        : (l.fait ? '' : ' style="color:#B45309;"')) + '>'
           + _escapeHtml(l.etat) + (l.brouillon ? ' — rien déplacé' : '') + '</td></tr>';
    });
    body.innerHTML = h + '</tbody></table></div>';
}

function closeHistoriqueTransferts() {
    var ov = el('ac-transferts-overlay');
    if (ov) ov.classList.remove('active');
}

function closeActionReassort() {
    var ov = el('ac-reassort-overlay');
    if (ov) ov.classList.remove('active');
}
