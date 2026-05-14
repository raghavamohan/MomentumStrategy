/* stock_chart.js — Live chart page (TradingView Lightweight Charts v4) */
(function () {
  'use strict';

  var TP_STORAGE_KEY = 'sc_chart_tooltip_prefs';
  var RSI_PERIOD = 14;
  var MAX_MAIN_INDICATORS = 3;
  var SAVE_DEBOUNCE_MS = 500;
  var WS_RECONNECT_MS = 4000;

  // ── Bootstrap ────────────────────────────────────────────────────────────
  var boot = {};
  try { boot = JSON.parse(document.getElementById('sc-bootstrap').value || '{}'); } catch (_) {}
  var TOKEN = boot.instrumentToken | 0;
  var EXCHANGE = boot.exchange || 'NSE';
  var BOOT_INTERVAL = boot.interval || 'day';

  (function () {
    var el = document.getElementById('sc-back');
    if (!el || !TOKEN) return;
    var href = '/dashboard?focus_token=' + TOKEN;
    var fc = boot.focusContext || '';
    if (fc === 'watchlist') { href += '&focus_context=watchlist'; el.textContent = '\u2190 Watchlist'; }
    else if (fc === 'equity_holding') { href += '&focus_context=equity_holding'; el.textContent = '\u2190 Holdings'; }
    el.href = href;
  })();

  // ── Interval config ─────────────────────────────────────────────────────
  var IV_CFG = {
    minute: { kite: 'minute', days: 60, label: '1m', timeVisible: true },
    '5minute': { kite: '5minute', days: 100, label: '5m', timeVisible: true },
    '15minute': { kite: '15minute', days: 200, label: '15m', timeVisible: true },
    '60minute': { kite: '60minute', days: 400, label: '1hr', timeVisible: true },
    day: { kite: 'day', days: 3650, label: '1D', timeVisible: false },
    week: { kite: 'day', days: 3650, label: '1W', timeVisible: false, agg: 'week' },
    month: { kite: 'day', days: 3650, label: '1M', timeVisible: false, agg: 'month' }
  };

  var CHART_OPTS = {
    layout: { background: { color: '#0f172a' }, textColor: '#94a3b8' },
    grid: { vertLines: { color: 'rgba(148,163,184,.08)' }, horzLines: { color: 'rgba(148,163,184,.08)' } },
    crosshair: { mode: 1 },
    rightPriceScale: { borderColor: 'rgba(148,163,184,.15)' },
    timeScale: { borderColor: 'rgba(148,163,184,.15)', fixLeftEdge: true }
  };

  // ── State ────────────────────────────────────────────────────────────────
  var currentIv = BOOT_INTERVAL in IV_CFG ? BOOT_INTERVAL : 'day';
  var allBars = [];
  var liveBar = null;
  var showVolume = true;
  var ws = null;
  var wsReconnectTimer = null;
  var lastTick = null;
  var chartResizeObs = null;

  var indicators = [
    { id: uid(), type: 'SMA', period: 21, color: '#f59e0b', series: null },
    { id: uid(), type: 'SMA', period: 14, color: '#60a5fa', series: null }
  ];

  var trendlines = [];
  var levels = [];
  var levelPriceLines = {};
  var saveTimer = null;
  var tlMode = false;
  var tlAnchors = [];

  var LW = window.LightweightCharts;
  var mainChart, rsiChart, candleSeries, volSeries, rsiLineSeries;
  var rsiObLine, rsiOsLine, rsiMidLine;
  var cachedRsiPoints = [];
  /** 0 idle, 1 syncing from main crosshair, 2 from RSI — prevents feedback loops */
  var crosshairSyncGuard = 0;

  /** Prevents ping-pong when mirroring visible *time* between main and RSI charts. */
  var tsTimeSyncing = false;

  function alignRsiTimeScaleToMain() {
    if (!mainChart || !rsiChart) return;
    var vr = mainChart.timeScale().getVisibleRange();
    if (!vr || vr.from == null || vr.to == null) return;
    if (typeof rsiChart.timeScale().setVisibleRange !== 'function') return;
    tsTimeSyncing = true;
    try {
      rsiChart.timeScale().setVisibleRange(vr);
    } catch (_) {}
    tsTimeSyncing = false;
  }

  function uid() { return Math.random().toString(36).slice(2); }

  // ── Tooltip prefs (localStorage) ─────────────────────────────────────────
  var defaultTooltipPrefs = {
    ohlc: true, vol: true, chg: true, ma: true, rsi: true, avg: false, ltt: false, bsq: false, depth: false
  };

  function loadTooltipPrefs() {
    try {
      var raw = localStorage.getItem(TP_STORAGE_KEY);
      if (!raw) return Object.assign({}, defaultTooltipPrefs);
      return Object.assign({}, defaultTooltipPrefs, JSON.parse(raw));
    } catch (_) {
      return Object.assign({}, defaultTooltipPrefs);
    }
  }

  function persistTooltipPrefs(p) {
    try { localStorage.setItem(TP_STORAGE_KEY, JSON.stringify(p)); } catch (_) {}
  }

  var tooltipPrefs = loadTooltipPrefs();

  // ── Math ───────────────────────────────────────────────────────────────────
  function calcSMA(bars, period) {
    var out = [];
    for (var i = 0; i < bars.length; i++) {
      if (i < period - 1) continue;
      var sum = 0;
      for (var j = i - period + 1; j <= i; j++) sum += bars[j].close;
      out.push({ time: bars[i].time, value: sum / period });
    }
    return out;
  }

  function calcEMA(bars, period) {
    var out = [], k = 2 / (period + 1), ema = 0, started = false;
    for (var i = 0; i < bars.length; i++) {
      if (i < period - 1) continue;
      if (!started) {
        var s = 0;
        for (var j = 0; j < period; j++) s += bars[j].close;
        ema = s / period;
        started = true;
      } else ema = bars[i].close * k + ema * (1 - k);
      out.push({ time: bars[i].time, value: ema });
    }
    return out;
  }

  function calcRSI(bars, period) {
    if (bars.length < period + 1) return [];
    var gains = 0, losses = 0, out = [];
    for (var i = 1; i <= period; i++) {
      var d = bars[i].close - bars[i - 1].close;
      if (d >= 0) gains += d; else losses -= d;
    }
    var ag = gains / period, al = losses / period;
    out.push({ time: bars[period].time, value: al === 0 ? 100 : 100 - 100 / (1 + ag / al) });
    for (var i = period + 1; i < bars.length; i++) {
      var d2 = bars[i].close - bars[i - 1].close;
      ag = (ag * (period - 1) + Math.max(d2, 0)) / period;
      al = (al * (period - 1) + Math.max(-d2, 0)) / period;
      out.push({ time: bars[i].time, value: al === 0 ? 100 : 100 - 100 / (1 + ag / al) });
    }
    return out;
  }

  /** UTC ms for a bar's session (daily string uses noon IST). */
  function barToUtcMs(b) {
    if (!b || b.time == null) return 0;
    if (typeof b.time === 'number') return b.time * 1000;
    if (typeof b.time === 'string') {
      if (b.time.length === 10) return new Date(b.time + 'T12:00:00+05:30').getTime();
      return new Date(b.time).getTime();
    }
    return 0;
  }

  /**
   * Week bucket id: Monday (IST) of the week containing the bar, as YYYY-MM-DD.
   * Uses Asia/Kolkata civil dates only (no browser-local getDay / toISOString).
   */
  function istMondayWeekKeyFromBar(b) {
    var utcMs = barToUtcMs(b);
    if (!utcMs) return String(b.time);
    var dayStr = istDayString(utcMs);
    var parts = dayStr.split('-');
    var y = +parts[0], mo = +parts[1], da = +parts[2];
    var noonMs = new Date(
      y + '-' + String(mo).padStart(2, '0') + '-' + String(da).padStart(2, '0') + 'T12:00:00+05:30'
    ).getTime();
    var wdStr = new Intl.DateTimeFormat('en-GB', {
      timeZone: 'Asia/Kolkata',
      weekday: 'short'
    }).format(new Date(noonMs));
    var wdMap = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };
    var wd = wdMap[wdStr];
    if (wd === undefined) return dayStr;
    var daysFromMon = (wd + 6) % 7;
    var monMs = noonMs - daysFromMon * 86400000;
    return istDayString(monMs);
  }

  function aggregateBars(bars, mode) {
    var buckets = {}, order = [];
    bars.forEach(function (b) {
      var t = typeof b.time === 'number' ? b.time : (new Date(b.time)).getTime() / 1000;
      var d = new Date(t * 1000);
      var key;
      if (mode === 'week') {
        key = istMondayWeekKeyFromBar(b);
      } else {
        key = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-01';
      }
      if (!buckets[key]) {
        buckets[key] = { time: key, open: b.open, high: b.high, low: b.low, close: b.close, volume: b.volume };
        order.push(key);
      } else {
        var bk = buckets[key];
        bk.high = Math.max(bk.high, b.high);
        bk.low = Math.min(bk.low, b.low);
        bk.close = b.close;
        bk.volume += b.volume;
      }
    });
    return order.map(function (k) { return buckets[k]; });
  }

  function toTime(dateStr) {
    if (!dateStr) return 0;
    if (dateStr.length === 10) return dateStr;
    return Math.floor(new Date(dateStr).getTime() / 1000);
  }

  function barStepSec(iv) {
    if (iv === 'minute') return 60;
    if (iv === '5minute') return 300;
    if (iv === '15minute') return 900;
    if (iv === '60minute') return 3600;
    return null;
  }

  function istDayString(tsMs) {
    return new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Kolkata', year: 'numeric', month: '2-digit', day: '2-digit'
    }).format(new Date(tsMs));
  }

  function liveBarTimeToSec(t) {
    if (typeof t === 'number') return t;
    return Math.floor(new Date(t + 'T12:00:00+05:30').getTime() / 1000);
  }

  // ── Status ───────────────────────────────────────────────────────────────
  function showStatus(msg) {
    var el = document.getElementById('sc-status');
    if (el) { el.hidden = false; el.textContent = msg; }
  }
  function hideStatus() {
    var el = document.getElementById('sc-status');
    if (el) el.hidden = true;
  }
  function showArea() {
    var el = document.getElementById('sc-chart-area');
    if (el) el.hidden = false;
  }

  // ── Charts lifecycle ─────────────────────────────────────────────────────
  function destroyCharts() {
    if (chartResizeObs) {
      try { chartResizeObs.disconnect(); } catch (_) {}
      chartResizeObs = null;
    }
    if (mainChart) {
      try { mainChart.remove(); } catch (_) {}
      mainChart = null;
    }
    if (rsiChart) {
      try { rsiChart.remove(); } catch (_) {}
      rsiChart = null;
    }
    candleSeries = null;
    volSeries = null;
    rsiLineSeries = null;
    rsiObLine = rsiOsLine = rsiMidLine = null;
    indicators.forEach(function (ind) { ind.series = null; });
    levelPriceLines = {};
  }

  function initCharts() {
    var mainEl = document.getElementById('sc-main-chart');
    var rsiEl = document.getElementById('sc-rsi-chart');
    if (!mainEl || !rsiEl || !LW) {
      showStatus('TradingView Lightweight Charts failed to load.');
      return;
    }

    var ivCfg = IV_CFG[currentIv] || IV_CFG.day;

    mainChart = LW.createChart(mainEl, Object.assign({}, CHART_OPTS, {
      width: mainEl.offsetWidth,
      height: mainEl.offsetHeight,
      timeScale: Object.assign({}, CHART_OPTS.timeScale, {
        timeVisible: ivCfg.timeVisible,
        secondsVisible: false
      })
    }));

    candleSeries = mainChart.addCandlestickSeries({
      upColor: '#22c55e',
      downColor: '#ef4444',
      borderVisible: false,
      wickUpColor: '#22c55e',
      wickDownColor: '#ef4444'
    });

    volSeries = mainChart.addHistogramSeries({
      color: 'rgba(148,163,184,.3)',
      priceFormat: { type: 'volume' },
      priceScaleId: 'vol',
      scaleMargins: { top: 0.82, bottom: 0 }
    });
    mainChart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });

    rsiChart = LW.createChart(rsiEl, Object.assign({}, CHART_OPTS, {
      width: rsiEl.offsetWidth,
      height: rsiEl.offsetHeight,
      timeScale: Object.assign({}, CHART_OPTS.timeScale, {
        timeVisible: ivCfg.timeVisible,
        secondsVisible: false
      }),
      rightPriceScale: { scaleMargins: { top: 0.1, bottom: 0.1 }, mode: 0 }
    }));

    rsiLineSeries = rsiChart.addLineSeries({
      color: '#a78bfa',
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: true
    });
    rsiObLine = rsiLineSeries.createPriceLine({
      price: 70, color: 'rgba(239,68,68,.5)', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'OB'
    });
    rsiOsLine = rsiLineSeries.createPriceLine({
      price: 30, color: 'rgba(34,197,94,.5)', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'OS'
    });
    rsiMidLine = rsiLineSeries.createPriceLine({
      price: 50, color: 'rgba(148,163,184,.3)', lineWidth: 1, lineStyle: 3, axisLabelVisible: false
    });

    var syncing = false;
    // Sync by *visible time* — RSI has fewer points than candles, so logical indices never match.
    if (typeof mainChart.timeScale().subscribeVisibleTimeRangeChange === 'function') {
      mainChart.timeScale().subscribeVisibleTimeRangeChange(function (range) {
        if (tsTimeSyncing || !rsiChart || !range || range.from == null || range.to == null) return;
        tsTimeSyncing = true;
        try {
          rsiChart.timeScale().setVisibleRange(range);
        } catch (_) {}
        tsTimeSyncing = false;
      });
      rsiChart.timeScale().subscribeVisibleTimeRangeChange(function (range) {
        if (tsTimeSyncing || !mainChart || !range || range.from == null || range.to == null) return;
        tsTimeSyncing = true;
        try {
          mainChart.timeScale().setVisibleRange(range);
        } catch (_) {}
        tsTimeSyncing = false;
      });
    } else {
      mainChart.timeScale().subscribeVisibleLogicalRangeChange(function (r) {
        if (syncing || !r) return;
        syncing = true;
        rsiChart.timeScale().setVisibleLogicalRange(r);
        syncing = false;
      });
      rsiChart.timeScale().subscribeVisibleLogicalRangeChange(function (r) {
        if (syncing || !r) return;
        syncing = true;
        mainChart.timeScale().setVisibleLogicalRange(r);
        syncing = false;
      });
    }

    mainChart.subscribeCrosshairMove(function (param) {
      syncRsiCrosshairFromMain(param);
      onCrosshair(param);
    });
    rsiChart.subscribeCrosshairMove(function (param) {
      syncMainCrosshairFromRsi(param);
    });

    chartResizeObs = new ResizeObserver(function () {
      if (!mainChart || !rsiChart) return;
      mainChart.applyOptions({ width: mainEl.offsetWidth });
      rsiChart.applyOptions({ width: rsiEl.offsetWidth });
      drawTrendlines();
      alignRsiTimeScaleToMain();
    });
    chartResizeObs.observe(mainEl);
    chartResizeObs.observe(rsiEl);
  }

  // ── Annotations API ───────────────────────────────────────────────────────
  function loadAnnotations(done) {
    if (!TOKEN) {
      if (done) done();
      return;
    }
    fetch('/dashboard/chart-annotations?instrument_token=' + TOKEN, {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' }
    })
      .then(function (r) { return r.json(); })
      .then(function (body) {
        trendlines = Array.isArray(body.trendlines) ? body.trendlines.slice() : [];
        levels = Array.isArray(body.levels) ? body.levels.slice() : [];
        trendlines.forEach(function (tl) {
          if (tl.extended === undefined) tl.extended = false;
        });
      })
      .catch(function () {
        trendlines = [];
        levels = [];
      })
      .finally(function () { if (done) done(); });
  }

  function scheduleSaveAnnotations() {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(function () {
      saveTimer = null;
      if (!TOKEN) return;
      fetch('/dashboard/chart-annotations', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({
          instrument_token: TOKEN,
          trendlines: trendlines,
          levels: levels
        })
      }).catch(function () {});
    }, SAVE_DEBOUNCE_MS);
  }

  function styleToLw(s) {
    if (s === 'dotted') return 1;
    if (s === 'dashed' || s === 'largeDashed') return 2;
    return 0;
  }

  function clearLevelPriceLines() {
    if (!candleSeries) return;
    Object.keys(levelPriceLines).forEach(function (id) {
      var pl = levelPriceLines[id];
      try { candleSeries.removePriceLine(pl); } catch (_) {}
    });
    levelPriceLines = {};
  }

  function restoreLevels() {
    clearLevelPriceLines();
    if (!candleSeries) return;
    levels.forEach(function (lv) {
      var id = lv.id || uid();
      if (!lv.id) lv.id = id;
      var pl = candleSeries.createPriceLine({
        price: Number(lv.price),
        color: lv.color || '#22c55e',
        lineWidth: Math.max(1, Number(lv.width) || 1),
        lineStyle: styleToLw(lv.style || 'dashed'),
        axisLabelVisible: true,
        title: (lv.label || '').slice(0, 24)
      });
      levelPriceLines[id] = pl;
    });
  }

  function barsForIndicators() {
    if (!allBars.length) return [];
    return allBars;
  }

  function refreshIndicators() {
    if (!mainChart || !candleSeries || !rsiLineSeries) return;

    indicators.forEach(function (ind) {
      if (ind.series) {
        try { mainChart.removeSeries(ind.series); } catch (_) {}
        ind.series = null;
      }
    });

    var bars = barsForIndicators();
    indicators.forEach(function (ind) {
      var pts = ind.type === 'EMA' ? calcEMA(bars, ind.period) : calcSMA(bars, ind.period);
      if (!pts.length) return;
      var ser = mainChart.addLineSeries({
        color: ind.color,
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: true
      });
      ser.setData(pts);
      ind.series = ser;
    });

    cachedRsiPoints = calcRSI(bars, RSI_PERIOD);
    rsiLineSeries.setData(cachedRsiPoints);
  }

  function rsiValueAtTime(t) {
    if (t == null || !cachedRsiPoints.length) return null;
    for (var i = 0; i < cachedRsiPoints.length; i++) {
      var p = cachedRsiPoints[i];
      if (p.time === t || String(p.time) === String(t)) return p.value;
    }
    return null;
  }

  function candleAtTime(t) {
    if (t == null || !allBars.length) return null;
    for (var i = allBars.length - 1; i >= 0; i--) {
      var b = allBars[i];
      if (b.time === t || String(b.time) === String(t)) return b;
    }
    return null;
  }

  function syncRsiCrosshairFromMain(param) {
    if (!rsiChart || !rsiLineSeries) return;
    if (typeof rsiChart.setCrosshairPosition !== 'function') return;
    if (crosshairSyncGuard === 2) return;
    if (!param || !param.point) {
      if (typeof rsiChart.clearCrosshairPosition === 'function') rsiChart.clearCrosshairPosition();
      return;
    }
    if (param.time === undefined || param.time === null) {
      if (typeof rsiChart.clearCrosshairPosition === 'function') rsiChart.clearCrosshairPosition();
      return;
    }
    crosshairSyncGuard = 1;
    var rv = rsiValueAtTime(param.time);
    if (rv != null) rsiChart.setCrosshairPosition(rv, param.time, rsiLineSeries);
    else rsiChart.clearCrosshairPosition();
    crosshairSyncGuard = 0;
  }

  function syncMainCrosshairFromRsi(param) {
    if (!mainChart || !candleSeries) return;
    if (typeof mainChart.setCrosshairPosition !== 'function') return;
    if (crosshairSyncGuard === 1) return;
    if (!param || !param.point) {
      if (typeof mainChart.clearCrosshairPosition === 'function') mainChart.clearCrosshairPosition();
      return;
    }
    if (param.time === undefined || param.time === null) {
      if (typeof mainChart.clearCrosshairPosition === 'function') mainChart.clearCrosshairPosition();
      return;
    }
    crosshairSyncGuard = 2;
    var b = candleAtTime(param.time);
    if (b) mainChart.setCrosshairPosition(b.close, param.time, candleSeries);
    else mainChart.clearCrosshairPosition();
    crosshairSyncGuard = 0;
  }

  // ── Trendlines (canvas) ───────────────────────────────────────────────────
  function getTlCanvas() {
    return document.getElementById('sc-tl-canvas');
  }

  function drawTrendlines() {
    var canvas = getTlCanvas();
    var wrap = document.getElementById('sc-main-wrap');
    if (!canvas || !wrap || !mainChart || !candleSeries) return;

    var w = wrap.clientWidth;
    var h = wrap.clientHeight;
    canvas.width = w;
    canvas.height = h;
    var ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.clearRect(0, 0, w, h);

    var ts = mainChart.timeScale();

    trendlines.forEach(function (tl) {
      var x1 = ts.timeToCoordinate(tl.time1);
      var y1 = candleSeries.priceToCoordinate(tl.price1);
      var x2 = ts.timeToCoordinate(tl.time2);
      var y2 = candleSeries.priceToCoordinate(tl.price2);
      if (x1 == null || y1 == null || x2 == null || y2 == null) return;

      ctx.strokeStyle = tl.color || '#f59e0b';
      ctx.lineWidth = Math.max(1, Number(tl.width) || 1);
      ctx.setLineDash(tl.style === 'dashed' ? [6, 4] : []);

      if (tl.extended && x1 !== x2) {
        var dx = x2 - x1, dy = y2 - y1;
        var t0 = (-x1) / dx;
        var t1 = (w - x1) / dx;
        var ta = Math.min(t0, t1);
        var tb = Math.max(t0, t1);
        var xa = x1 + ta * dx;
        var ya = y1 + ta * dy;
        var xb = x1 + tb * dx;
        var yb = y1 + tb * dy;
        ctx.beginPath();
        ctx.moveTo(xa, ya);
        ctx.lineTo(xb, yb);
        ctx.stroke();
      } else {
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();
      }
      ctx.setLineDash([]);
    });
  }

  function canvasPointToChart(ev) {
    var canvas = getTlCanvas();
    if (!canvas || !mainChart || !candleSeries) return null;
    var rect = canvas.getBoundingClientRect();
    var x = ev.clientX - rect.left;
    var y = ev.clientY - rect.top;
    var time = mainChart.timeScale().coordinateToTime(x);
    var price = candleSeries.coordinateToPrice(y);
    if (time == null || price == null) return null;
    return { time: time, price: price };
  }

  function bindTrendlineCanvas() {
    var canvas = getTlCanvas();
    if (!canvas || canvas.dataset.scTlBound === '1') return;
    canvas.dataset.scTlBound = '1';
    canvas.addEventListener('click', function (ev) {
      if (!tlMode || !mainChart) return;
      var pt = canvasPointToChart(ev);
      if (!pt) return;
      if (tlAnchors.length === 0) {
        tlAnchors.push(pt);
        return;
      }
      var a0 = tlAnchors[0];
      tlAnchors = [];
      trendlines.push({
        id: uid(),
        time1: a0.time,
        price1: a0.price,
        time2: pt.time,
        price2: pt.price,
        color: '#f59e0b',
        width: 1,
        label: '',
        extended: !!ev.shiftKey
      });
      scheduleSaveAnnotations();
      drawTrendlines();
      renderChips();
    });
  }

  // ── WebSocket ticks ───────────────────────────────────────────────────────
  function closeWS() {
    if (wsReconnectTimer) {
      clearTimeout(wsReconnectTimer);
      wsReconnectTimer = null;
    }
    if (ws) {
      try {
        ws.onclose = null;
        ws.close();
      } catch (_) {}
      ws = null;
    }
  }

  function connectWS() {
    closeWS();
    var ivCfg = IV_CFG[currentIv] || IV_CFG.day;
    if (ivCfg.agg) return;

    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var url = proto + '//' + location.host + '/ws/chart-ticks?instrument_token=' + TOKEN;
    try {
      ws = new WebSocket(url);
    } catch (_) {
      return;
    }

    ws.onmessage = function (ev) {
      try {
        var msg = JSON.parse(ev.data);
        if (msg && msg.t === 'tick') onTick(msg);
      } catch (_) {}
    };

    ws.onclose = function () {
      ws = null;
      if (!TOKEN || (IV_CFG[currentIv] || {}).agg) return;
      wsReconnectTimer = setTimeout(connectWS, WS_RECONNECT_MS);
    };
  }

  function onTick(tick) {
    if (!candleSeries || !allBars.length || !liveBar) return;
    lastTick = tick;

    var ltp = Number(tick.ltp) || 0;
    var tsMs = Number(tick.ts) || Date.now();
    var tsSec = Math.floor(tsMs / 1000);
    var ivCfg = IV_CFG[currentIv] || IV_CFG.day;
    var kiteIv = ivCfg.kite;
    var step = barStepSec(kiteIv);

    var newBar = false;
    if (kiteIv === 'day') {
      var dStr = istDayString(tsMs);
      if (dStr !== liveBar.time) {
        newBar = true;
        allBars.push({ time: dStr, open: ltp, high: ltp, low: ltp, close: ltp, volume: Number(tick.ltq) || 0 });
        liveBar = allBars[allBars.length - 1];
      }
    } else if (step) {
      var curSec = liveBarTimeToSec(liveBar.time);
      var curBucket = Math.floor(curSec / step);
      var tickBucket = Math.floor(tsSec / step);
      if (tickBucket !== curBucket) {
        newBar = true;
        var openT = tickBucket * step;
        allBars.push({
          time: openT,
          open: ltp,
          high: ltp,
          low: ltp,
          close: ltp,
          volume: Number(tick.ltq) || 0
        });
        liveBar = allBars[allBars.length - 1];
      }
    }

    if (!newBar) {
      liveBar.high = Math.max(liveBar.high, ltp);
      liveBar.low = Math.min(liveBar.low, ltp);
      liveBar.close = ltp;
      if (kiteIv === 'day') {
        var vtot = Number(tick.vol);
        if (vtot > 0) liveBar.volume = vtot;
        else liveBar.volume = (Number(liveBar.volume) || 0) + (Number(tick.ltq) || 0);
      } else {
        liveBar.volume = (Number(liveBar.volume) || 0) + (Number(tick.ltq) || 0);
      }
    }

    candleSeries.update(liveBar);
    volSeries.update({
      time: liveBar.time,
      value: liveBar.volume,
      color: liveBar.close >= liveBar.open ? 'rgba(34,197,94,.35)' : 'rgba(239,68,68,.35)'
    });
    refreshIndicators();
    drawTrendlines();
  }

  // ── Crosshair / tooltip ───────────────────────────────────────────────────
  function fmtNum(n, d) {
    if (n == null || isNaN(n)) return '—';
    var p = d != null ? d : 2;
    return Number(n).toFixed(p);
  }

  function row(lbl, val) {
    return '<div class="sc-tooltip-row"><span class="sc-tooltip-lbl">' + lbl + '</span><span class="sc-tooltip-val">' + val + '</span></div>';
  }

  function onCrosshair(param) {
    var tip = document.getElementById('sc-tooltip');
    var wrap = document.getElementById('sc-main-wrap');
    if (!tip || !wrap || !candleSeries) return;

    if (!param || !param.point || param.point.x === undefined) {
      tip.hidden = true;
      return;
    }

    var data = param.seriesData.get(candleSeries);
    if (!data) {
      tip.hidden = true;
      return;
    }

    var o = data.open, h = data.high, l = data.low, c = data.close;
    var v = data.volume != null ? data.volume : null;
    var parts = [];

    if (tooltipPrefs.ohlc) {
      parts.push(row('O', fmtNum(o)));
      parts.push(row('H', fmtNum(h)));
      parts.push(row('L', fmtNum(l)));
      parts.push(row('C', fmtNum(c)));
    }
    if (tooltipPrefs.vol && v != null) parts.push(row('Vol', String(Math.round(v))));

    if (tooltipPrefs.chg && o) {
      parts.push(row('Chg %', fmtNum(((c - o) / o) * 100, 2)));
    }

    if (tooltipPrefs.ma) {
      indicators.forEach(function (ind) {
        if (!ind.series) return;
        var sd = param.seriesData.get(ind.series);
        if (sd && sd.value != null) {
          parts.push(row(ind.type + ind.period, fmtNum(sd.value)));
        }
      });
    }

    if (tooltipPrefs.rsi) {
      var rv = rsiValueAtTime(param.time);
      if (rv != null) parts.push(row('RSI' + RSI_PERIOD, fmtNum(rv, 2)));
    }

    if (tooltipPrefs.avg && lastTick && lastTick.avg != null) {
      parts.push(row('Avg', fmtNum(lastTick.avg)));
    }
    if (tooltipPrefs.ltt && lastTick && lastTick.ts) {
      parts.push(row('LTT', new Date(lastTick.ts).toLocaleString()));
    }
    if (tooltipPrefs.bsq && lastTick) {
      parts.push(row('Buy Q', String(lastTick.buyQty != null ? lastTick.buyQty : '—')));
      parts.push(row('Sell Q', String(lastTick.sellQty != null ? lastTick.sellQty : '—')));
    }

    if (tooltipPrefs.depth && lastTick && lastTick.depth) {
      var db = (lastTick.depth.buy || []).slice(0, 5);
      var ds = (lastTick.depth.sell || []).slice(0, 5);
      var depthHtml = '<div class="sc-depth">';
      for (var i = 0; i < 5; i++) {
        var b = db[i], s = ds[i];
        depthHtml += '<div class="sc-depth-row">';
        depthHtml += '<span class="sc-bid">' + (b ? fmtNum(b.p, 2) + ' \u00d7 ' + b.q : '') + '</span>';
        depthHtml += '<span style="color:#94a3b8">' + (i + 1) + '</span>';
        depthHtml += '<span class="sc-ask">' + (s ? fmtNum(s.p, 2) + ' \u00d7 ' + s.q : '') + '</span>';
        depthHtml += '</div>';
      }
      depthHtml += '</div>';
      parts.push(depthHtml);
    }

    tip.innerHTML = parts.join('');
    tip.hidden = false;
    var x = param.point.x + 16;
    var y = param.point.y + 16;
    var maxX = wrap.clientWidth - tip.offsetWidth - 8;
    var maxY = wrap.clientHeight - tip.offsetHeight - 8;
    if (x > maxX) x = Math.max(8, maxX);
    if (y > maxY) y = Math.max(8, maxY);
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  }

  // ── UI chips ─────────────────────────────────────────────────────────────
  function updateIndAddButton() {
    var btn = document.getElementById('sc-ind-add-btn');
    var addInd = document.getElementById('sc-add-ind');
    var full = indicators.length >= MAX_MAIN_INDICATORS;
    if (btn) btn.disabled = full;
    if (addInd) addInd.disabled = full;
  }

  function renderChips() {
    var host = document.getElementById('sc-chips');
    if (!host) return;
    host.innerHTML = '';

    indicators.forEach(function (ind) {
      var chip = document.createElement('span');
      chip.className = 'sc-chip';
      chip.innerHTML = ind.type + ' ' + ind.period +
        '<span class="sc-chip-remove" data-ind="' + ind.id + '">\u00d7</span>';
      chip.querySelector('.sc-chip-remove').addEventListener('click', function (e) {
        e.stopPropagation();
        indicators = indicators.filter(function (x) { return x.id !== ind.id; });
        refreshIndicators();
        renderChips();
      });
      host.appendChild(chip);
    });

    trendlines.forEach(function (tl, idx) {
      var chip = document.createElement('span');
      chip.className = 'sc-chip';
      chip.style.opacity = '0.9';
      chip.innerHTML = 'TL ' + (idx + 1) +
        '<span class="sc-chip-remove" data-tlid="' + (tl.id || idx) + '">\u00d7</span>';
      chip.querySelector('.sc-chip-remove').addEventListener('click', function (e) {
        e.stopPropagation();
        var id = tl.id;
        trendlines = trendlines.filter(function (x) { return x.id !== id; });
        scheduleSaveAnnotations();
        drawTrendlines();
        renderChips();
      });
      host.appendChild(chip);
    });

    levels.forEach(function (lv) {
      var chip = document.createElement('span');
      chip.className = 'sc-chip';
      chip.innerHTML = 'Lv ' + fmtNum(lv.price, 2) +
        '<span class="sc-chip-remove" data-lvid="' + (lv.id || '') + '">\u00d7</span>';
      chip.querySelector('.sc-chip-remove').addEventListener('click', function (e) {
        e.stopPropagation();
        var id = lv.id;
        levels = levels.filter(function (x) { return x.id !== id; });
        if (levelPriceLines[id]) {
          try { candleSeries.removePriceLine(levelPriceLines[id]); } catch (_) {}
          delete levelPriceLines[id];
        }
        scheduleSaveAnnotations();
        renderChips();
      });
      host.appendChild(chip);
    });
    updateIndAddButton();
  }

  function syncIntervalButtons() {
    document.querySelectorAll('.sc-ivbtn').forEach(function (btn) {
      var iv = btn.getAttribute('data-iv');
      btn.classList.toggle('active', iv === currentIv);
    });
  }

  function buildTooltipSettingsPanel() {
    var wrap = document.querySelector('.sc-controls');
    if (!wrap || document.getElementById('sc-tooltip-panel')) return;

    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'sc-control-btn';
    btn.id = 'sc-tooltip-btn';
    btn.textContent = 'Tooltip';
    wrap.appendChild(btn);

    var panel = document.createElement('div');
    panel.id = 'sc-tooltip-panel';
    panel.className = 'sc-panel';
    panel.hidden = true;
    panel.style.top = '160px';
    panel.style.right = '20px';
    panel.style.maxWidth = '280px';

    var fields = [
      { key: 'ohlc', label: 'OHLC' },
      { key: 'vol', label: 'Volume' },
      { key: 'chg', label: 'Change %' },
      { key: 'ma', label: 'MA / overlays' },
      { key: 'rsi', label: 'RSI' },
      { key: 'avg', label: 'Avg traded price' },
      { key: 'ltt', label: 'Last trade time' },
      { key: 'bsq', label: 'Buy / sell qty' },
      { key: 'depth', label: 'Market depth (5)' }
    ];

    var h = document.createElement('h3');
    h.textContent = 'Tooltip fields';
    panel.appendChild(h);

    fields.forEach(function (f) {
      var rowEl = document.createElement('label');
      rowEl.className = 'sc-panel-row';
      rowEl.style.flexDirection = 'row';
      rowEl.style.alignItems = 'center';
      rowEl.style.gap = '8px';
      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.dataset.key = f.key;
      cb.checked = !!tooltipPrefs[f.key];
      cb.addEventListener('change', function () {
        tooltipPrefs[f.key] = cb.checked;
        persistTooltipPrefs(tooltipPrefs);
      });
      var span = document.createElement('span');
      span.textContent = f.label;
      rowEl.appendChild(cb);
      rowEl.appendChild(span);
      panel.appendChild(rowEl);
    });

    document.querySelector('.sc-page').appendChild(panel);

    btn.addEventListener('click', function () {
      panel.hidden = !panel.hidden;
    });
  }

  function setupUI() {
    syncIntervalButtons();
    buildTooltipSettingsPanel();
    bindTrendlineCanvas();

    document.querySelectorAll('.sc-ivbtn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var iv = btn.getAttribute('data-iv');
        if (!iv || iv === currentIv) return;
        currentIv = iv;
        syncIntervalButtons();
        closeWS();
        destroyCharts();
        initCharts();
        loadHistory();
      });
    });

    var addInd = document.getElementById('sc-add-ind');
    var indPanel = document.getElementById('sc-ind-panel');
    if (addInd && indPanel) {
      addInd.addEventListener('click', function () {
        indPanel.hidden = false;
      });
    }
    document.getElementById('sc-ind-cancel') && document.getElementById('sc-ind-cancel').addEventListener('click', function () {
      if (indPanel) indPanel.hidden = true;
    });
    document.getElementById('sc-ind-add-btn') && document.getElementById('sc-ind-add-btn').addEventListener('click', function () {
      if (indicators.length >= MAX_MAIN_INDICATORS) return;
      var typ = (document.getElementById('sc-ind-type') || {}).value || 'SMA';
      var period = Math.max(2, Math.min(200, parseInt(document.getElementById('sc-ind-period').value, 10) || 20));
      var color = (document.getElementById('sc-ind-color') || {}).value || '#f59e0b';
      indicators.push({ id: uid(), type: typ, period: period, color: color, series: null });
      if (indPanel) indPanel.hidden = true;
      refreshIndicators();
      renderChips();
      updateIndAddButton();
    });

    var addLvl = document.getElementById('sc-add-lvl');
    var lvlPanel = document.getElementById('sc-lvl-panel');
    if (addLvl && lvlPanel) {
      addLvl.addEventListener('click', function () { lvlPanel.hidden = false; });
    }
    document.getElementById('sc-lvl-cancel') && document.getElementById('sc-lvl-cancel').addEventListener('click', function () {
      if (lvlPanel) lvlPanel.hidden = true;
    });
    document.getElementById('sc-lvl-add-btn') && document.getElementById('sc-lvl-add-btn').addEventListener('click', function () {
      var price = parseFloat((document.getElementById('sc-lvl-price') || {}).value);
      if (!isFinite(price)) return;
      var label = (document.getElementById('sc-lvl-label') || {}).value || '';
      var color = (document.getElementById('sc-lvl-color') || {}).value || '#22c55e';
      var styleSel = document.getElementById('sc-lvl-style');
      var style = styleSel ? styleSel.value : 'dashed';
      levels.push({ id: uid(), price: price, color: color, style: style, width: 1, label: label });
      if (lvlPanel) lvlPanel.hidden = true;
      restoreLevels();
      scheduleSaveAnnotations();
      renderChips();
    });

    var tlBtn = document.getElementById('sc-trendline-btn');
    var canvas = getTlCanvas();
    if (tlBtn && canvas) {
      tlBtn.addEventListener('click', function () {
        tlMode = !tlMode;
        tlBtn.classList.toggle('active', tlMode);
        canvas.classList.toggle('draw-mode', tlMode);
        tlAnchors = [];
      });
    }

    document.getElementById('sc-clear-tl') && document.getElementById('sc-clear-tl').addEventListener('click', function () {
      trendlines = [];
      tlAnchors = [];
      scheduleSaveAnnotations();
      drawTrendlines();
      renderChips();
    });

    var volCb = document.getElementById('sc-show-vol');
    if (volCb) {
      volCb.checked = showVolume;
      volCb.addEventListener('change', function () {
        showVolume = volCb.checked;
        if (volSeries) volSeries.applyOptions({ visible: showVolume });
      });
    }

    window.addEventListener('beforeunload', closeWS);
  }

  // ── History load ──────────────────────────────────────────────────────────
  function loadHistory() {
    if (!TOKEN) { showStatus('Missing instrument token.'); return; }
    if (!candleSeries) return;

    showStatus('Loading\u2026');
    var ivCfg = IV_CFG[currentIv] || IV_CFG.day;
    var kiteIv = ivCfg.kite;
    var params = new URLSearchParams({
      instrument_token: TOKEN,
      exchange: EXCHANGE,
      interval: kiteIv,
      days: String(ivCfg.days)
    });

    fetch('/dashboard/stock-history?' + params, {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' }
    })
      .then(function (r) { return r.json(); })
      .then(function (body) {
        var raw = (body.candles || []).map(function (c) {
          return {
            time: toTime(c.date),
            open: c.open,
            high: c.high,
            low: c.low,
            close: c.close,
            volume: c.volume
          };
        });
        var bars = ivCfg.agg ? aggregateBars(raw, ivCfg.agg) : raw;
        allBars = bars;
        if (!bars.length) {
          showStatus('No data for this interval.');
          return;
        }
        hideStatus();
        showArea();
        candleSeries.setData(bars);
        volSeries.setData(bars.map(function (b) {
          return {
            time: b.time,
            value: b.volume,
            color: b.close >= b.open ? 'rgba(34,197,94,.35)' : 'rgba(239,68,68,.35)'
          };
        }));
        volSeries.applyOptions({ visible: showVolume });
        liveBar = Object.assign({}, bars[bars.length - 1]);
        allBars[allBars.length - 1] = liveBar;
        refreshIndicators();
        restoreLevels();
        renderChips();
        drawTrendlines();
        alignRsiTimeScaleToMain();
        connectWS();
      })
      .catch(function () { showStatus('Network error loading chart data.'); });
  }

  window._scInit = function () {
    if (!LW) { showStatus('Chart library not available.'); return; }
    initCharts();
    loadAnnotations(function () {
      loadHistory();
    });
    setupUI();
    renderChips();
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', window._scInit);
  } else {
    window._scInit();
  }
})();
