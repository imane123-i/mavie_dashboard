/**
 * MaVie Dashboard – logique client principale
 * Module Odoo 17 : mavie_dashboard
 * Données depuis mv.article.base (Base Pivot)
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
    filters_loaded: false,
    shops: [],
    detail: {
        article_id: null,
        shop_field: null,
    },
};

var lastRupturesList = [];
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
}

function updateFiltersFromUI() {
    var colEl  = el('filter-collection');
    var shopEl = el('filter-magasin');
    var catEl  = el('filter-category');
    var batEl  = el('filter-batch');
    var startEl = el('filter-date-start');
    var endEl   = el('filter-date-end');
    var topLimitEl = el('top-limit');
    var flopLimitEl = el('flop-limit');

    state.collection_id = (colEl  && colEl.value)  ? colEl.value  : null;
    state.shop_field    = (shopEl && shopEl.value)  ? shopEl.value : null;
    state.categ_id      = (catEl  && catEl.value)   ? catEl.value  : null;
    state.batch_id      = (batEl  && batEl.value)   ? batEl.value  : null;
    state.date_start    = (startEl && startEl.value) ? startEl.value : null;
    state.date_end      = (endEl   && endEl.value)   ? endEl.value   : null;
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
        return;
    }

    var kpiMap = {
        'kpi-ca-total':      formatMAD(data.ca_total),
        'kpi-tickets':       formatNumber(data.tickets),
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

    lastRupturesList = data.ruptures_list || [];

    var stEl = el('kpi-sell-through');
    if (stEl) {
        var st = data.sell_through || 0;
        stEl.style.color = st >= 70 ? '#10B981' : (st >= 40 ? '#F59E0B' : '#EF4444');
    }

    _renderProductTable('top-products-tbody', data.top_products, false);
    _renderProductTable('flop-products-tbody', data.flop_products, true);

    if (currentPage === 'ventes') {
        _renderABC(data.abc_analysis);
        loadSalesDaily();
    }
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

async function loadSalesDaily() {
    var params = getFilterParams();
    var data = await rpc('/mavie/api/sales-daily', params);
    if (!data || data.error) return;

    var chartDiv = el('sales-daily-chart');
    if (!chartDiv) return;
    chartDiv.innerHTML = '';

    if (!data.daily || data.daily.length === 0) {
        chartDiv.innerHTML = '<p style="color:#999;text-align:center;padding:20px">Aucune donnée disponible</p>';
        return;
    }

    var maxCA = Math.max.apply(null, data.daily.map(function(d) { return d.ca || 0; }));
    if (maxCA === 0) maxCA = 1;

    data.daily.forEach(function(d) {
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
            var tip = el('chart-tooltip');
            if (tip) {
                tip.style.display = 'block';
                tip.style.left = (e.pageX + 10) + 'px';
                tip.style.top  = (e.pageY - 30) + 'px';
                tip.innerHTML = '<strong>' + d.date + '</strong><br>'
                    + formatMAD(d.ca) + '<br>'
                    + d.articles + ' art.';
            }
        });
        bar.addEventListener('mouseout', function() {
            var tip = el('chart-tooltip');
            if (tip) tip.style.display = 'none';
        });

        barInner.appendChild(bar);
        wrapper.appendChild(barInner);

        var label = document.createElement('div');
        label.className = 'chart-bar-label';
        label.textContent = d.label || d.date || '';
        wrapper.appendChild(label);

        chartDiv.appendChild(wrapper);
    });
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
    if (el('detail-ref'))        el('detail-ref').textContent  = 'Réf: ' + (data.ref || '—');
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
        'detail-margin':        formatPct(data.margin),
        'detail-sell-through':  formatPct(data.sell_through),
        'detail-pv-ttc':        formatMAD(data.pv_ttc),
        'detail-cost':          formatMAD(data.cost),
    };
    for (var id in kpiMap) {
        var e = el(id);
        if (e) e.textContent = kpiMap[id];
    }

    var stEl = el('detail-sell-through');
    if (stEl) {
        var st = data.sell_through || 0;
        stEl.style.color = st >= 70 ? '#10B981' : (st >= 40 ? '#F59E0B' : '#EF4444');
    }

    _renderStockByStore('detail-stock-pivot-tbody', data.stock_by_store, state.detail.shop_field);
    _renderRealStock('detail-stock-real-tbody', data.real_stock_by_store);
    _renderVariants('detail-variants-tbody', data.variants, state.detail.shop_field);

    var batchEl = el('detail-batch-info');
    if (batchEl && data.batch) {
        batchEl.innerHTML = '<strong>' + data.batch.name + '</strong>'
            + (data.batch.date ? ' — ' + data.batch.date : '')
            + (data.batch.collection && data.batch.collection !== '—'
               ? ' — Collection: <em>' + data.batch.collection + '</em>' : '');
        batchEl.style.display = 'block';
    } else if (batchEl) {
        batchEl.style.display = 'none';
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
        tdQty.textContent = formatNumber(s.qty);
        tr.appendChild(tdQty);

        tbody.appendChild(tr);
    });
}

function _renderRealStock(tbodyId, stores) {
    var tbody = el(tbodyId);
    if (!tbody) return;
    tbody.innerHTML = '';

    if (!stores || stores.length === 0) {
        var tr = document.createElement('tr');
        var td = document.createElement('td');
        td.colSpan = 2;
        td.textContent = 'Aucun stock dans Odoo';
        td.style.textAlign = 'center';
        td.style.color = '#999';
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }

    stores.forEach(function(s) {
        var tr = document.createElement('tr');

        var tdName = document.createElement('td');
        tdName.textContent = s.store_name || '—';
        tr.appendChild(tdName);

        var tdStock = document.createElement('td');
        tdStock.textContent = formatNumber(s.stock);
        tdStock.style.color = s.stock <= 0 ? '#EF4444' : '#10B981';
        tdStock.style.fontWeight = '600';
        tr.appendChild(tdStock);

        tbody.appendChild(tr);
    });
}

function _renderVariants(tbodyId, variants, activeShop) {
    var tbody = el(tbodyId);
    if (!tbody) return;
    tbody.innerHTML = '';

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

        var tdRank = document.createElement('td');
        tdRank.textContent = idx === 0 ? '🏆' : (idx + 1);
        tdRank.style.textAlign = 'center';
        tr.appendChild(tdRank);

        var tdName = document.createElement('td');
        tdName.textContent = v.name || '—';
        tdName.style.fontWeight = idx === 0 ? '700' : 'normal';
        tr.appendChild(tdName);

        var tdQty = document.createElement('td');
        if (activeShop && v.shops && v.shops[activeShop]) {
            tdQty.innerHTML = formatNumber(v.qty)
                + ' <small style="color:#7C3AED">(magasin: ' + formatNumber(v.shops[activeShop]) + ')</small>';
        } else {
            tdQty.textContent = formatNumber(v.qty);
        }
        tr.appendChild(tdQty);

        var tdTotal = document.createElement('td');
        tdTotal.textContent = formatNumber(v.total_pieces);
        tr.appendChild(tdTotal);

        var tdReste = document.createElement('td');
        tdReste.textContent = formatNumber(v.reste);
        tdReste.style.color = (v.reste > 0) ? '#F59E0B' : '#10B981';
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
            td.innerHTML = '<strong>Meilleure variante (' + best.name + ') — répartition par magasin :</strong> '
                + allShops.map(function(s) {
                    return '<span style="margin:0 4px;padding:2px 6px;background:#f3f4f6;border-radius:4px">'
                        + s + ': ' + formatNumber(best.shops[s]) + '</span>';
                }).join('');
            tr.appendChild(td);
            tbody.appendChild(tr);
        }
    }
}

function closeDetail() {
    var overlay = el('detail-overlay');
    if (overlay) overlay.classList.remove('active');
    state.detail.article_id = null;
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
function _renderRupturesList() {
    var tbody = el('ruptures-tbody');
    if (!tbody) return;
    tbody.innerHTML = '';

    if (!lastRupturesList || lastRupturesList.length === 0) {
        var tr = document.createElement('tr');
        var td = document.createElement('td');
        td.colSpan = 3;
        td.textContent = 'Aucun produit en rupture';
        td.style.textAlign = 'center';
        td.style.color = '#999';
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }

    lastRupturesList.forEach(function(p) {
        var tr = document.createElement('tr');
        tr.style.cursor = 'pointer';
        tr.onclick = function() {
            closeRuptures();
            openDetail(p.id, p.name);
        };

        var tdName = document.createElement('td');
        tdName.textContent = p.name;
        tr.appendChild(tdName);

        var tdRef = document.createElement('td');
        tdRef.textContent = p.ref || '—';
        tdRef.style.color = '#64748B';
        tr.appendChild(tdRef);

        var tdStock = document.createElement('td');
        tdStock.textContent = formatNumber(p.stock);
        tdStock.style.color = '#EF4444';
        tdStock.style.fontWeight = '600';
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
// EVENT LISTENERS
// ═══════════════════════════════════════════════════════════
document.addEventListener('DOMContentLoaded', function() {

    loadFilters().then(function() {
        loadKPIs();
    });

    ['filter-collection', 'filter-magasin', 'filter-category', 'filter-batch', 'filter-date-start', 'filter-date-end'].forEach(function(id) {
        var sel = el(id);
        if (sel) {
            sel.addEventListener('change', function() {
                updateFiltersFromUI();
                loadKPIs();
            });
        }
    });

    var _limitDebounce = null;
    ['top-limit', 'flop-limit'].forEach(function(id) {
        var sel = el(id);
        if (sel) {
            sel.addEventListener('click', function(e) { e.stopPropagation(); });
            sel.addEventListener('change', function() {
                updateFiltersFromUI();
                loadKPIs();
            });
            sel.addEventListener('input', function() {
                clearTimeout(_limitDebounce);
                _limitDebounce = setTimeout(function() {
                    updateFiltersFromUI();
                    if (state.top_limit >= 1 && state.flop_limit >= 1) {
                        loadKPIs();
                    }
                }, 600);
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

    var rupturesCard = el('card-ruptures');
    if (rupturesCard) rupturesCard.addEventListener('click', openRuptures);

    var closeRupturesBtn = el('close-ruptures-btn');
    if (closeRupturesBtn) closeRupturesBtn.addEventListener('click', closeRuptures);

    var rupturesOverlay = el('ruptures-overlay');
    if (rupturesOverlay) {
        rupturesOverlay.addEventListener('click', function(e) {
            if (e.target === rupturesOverlay) closeRuptures();
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