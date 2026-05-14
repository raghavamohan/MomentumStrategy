/* stock_chart.js — Live chart page (TradingView Lightweight Charts v4) */
(function () {
  'use strict';

  var TP_STORAGE_KEY = 'sc_chart_tooltip_prefs';
  var RSI_SHOW_KEY = 'sc_chart_show_rsi';
  var RSI_PERIOD = 14;
  var MAX_MAIN_INDICATORS = 3;
  var SAVE_DEBOUNCE_MS = 500;
  var WS_RECONNECT_MS = 4000;
  var STALE_TICK_MS = 90000;
  var RIGHT_OFFSET_BARS = 30;

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
    timeScale: { borderColor: 'rgba(148,163,184,.15)', fixLeftEdge: true, rightOffset: RIGHT_OFFSET_BARS }
  };

  // ── State ────────────────────────────────────────────────────────────────
  var currentIv = BOOT_INTERVAL in IV_CFG ? BOOT_INTERVAL : 'day';
  var allBars = [];
  var liveBar = null;
  var showVolume = true;
  var showRsi = true;
  try {
    var rsiStored = localStorage.getItem(RSI_SHOW_KEY);
    if (rsiStored === '0') showRsi = false;
  } catch (_) {}
  var ws = null;
  var wsReconnectTimer = null;
  var lastTick = null;
  var wsLiveState = 'idle';
  var staleCheckTimer = null;
  var chartResizeObs = null;
  var chartResizeDebounceTimer = null;

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
  var tlPreviewPx = null;
  var tlDraftMoveBound = null;
  var tlSelectMode = false;
  var selectedTlId = null;
  var TL_HIT_PX = 10;
  /** Pixel radius to grab start/end handles when reshaping with right-drag. */
  var TL_HANDLE_HIT_PX = 14;
  var tlRmbDrag = null;
  var tlSuppressSelectClick = false;
  var tlRedrawRaf = null;
  var tlAnchorPulse = null;
  var tlPulseTimer = null;

  var LW = window.LightweightCharts;
  var mainChart, rsiChart, candleSeries, volSeries, rsiLineSeries;
  var rsiObLine, rsiOsLine, rsiMidLine;
  var cachedRsiPoints = [];
  /** 0 idle, 1 syncing from main crosshair, 2 from RSI — prevents feedback loops */
  var crosshairSyncGuard = 0;

  /** Prevents ping-pong when mirroring visible *logical range* between main and RSI charts. */
  var paneRangeSyncing = false;

  function alignRsiTimeScaleToMain() {
    if (!mainChart || !rsiChart) return;
    var lr = mainChart.timeScale().getVisibleLogicalRange();
    if (!lr || lr.from == null || lr.to == null) return;
    if (typeof rsiChart.timeScale().setVisibleLogicalRange !== 'function') return;
    paneRangeSyncing = true;
    try {
      rsiChart.timeScale().setVisibleLogicalRange({ from: lr.from, to: lr.to });
    } catch (_) {}
    paneRangeSyncing = false;
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

  /**
   * RSI(period) with leading whitespace so the RSI series has one slot per
   * candle bar. Same logical bar count + same `rightOffset` lets the two
   * charts share a 1:1 visible-logical-range mirror with no clamping.
   */
  function calcRSI(bars, period) {
    var out = [];
    var prefixCount = Math.min(period, bars.length);
    for (var k = 0; k < prefixCount; k++) {
      out.push({ time: bars[k].time });
    }
    if (bars.length < period + 1) return out;
    var gains = 0, losses = 0;
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

  /** IST YYYY-MM-DD for a bar `time` (daily string, unix sec, or parseable datetime). */
  function istDayKeyForBar(b) {
    if (!b || b.time == null) return '';
    var t = b.time;
    if (typeof t === 'string' && t.length === 10 && /^\d{4}-\d{2}-\d{2}$/.test(t)) return t;
    if (typeof t === 'number' && isFinite(t)) return istDayString(t * 1000);
    if (typeof t === 'string') {
      var ms = new Date(t).getTime();
      if (isFinite(ms)) return istDayString(ms);
    }
    var utc = barToUtcMs(b);
    return utc ? istDayString(utc) : '';
  }

  function liveBarTimeToSec(t) {
    if (typeof t === 'number') return t;
    return Math.floor(new Date(t + 'T12:00:00+05:30').getTime() / 1000);
  }

  /** Unix seconds for any chart / trendline time (LW UTCTimestamp, daily string, etc.). */
  function chartTimeToUnixSec(t) {
    if (t == null) return NaN;
    if (typeof t === 'number' && isFinite(t)) return t;
    if (typeof t === 'string' && t.length === 10 && /^\d{4}-\d{2}-\d{2}$/.test(t)) {
      return Math.floor(new Date(t + 'T12:00:00+05:30').getTime() / 1000);
    }
    var ms = new Date(t).getTime();
    return isFinite(ms) ? Math.floor(ms / 1000) : NaN;
  }

  /** Match stored time shape to the reference bar (daily string vs unix seconds). */
  function unixSecToChartTime(sec, refBar) {
    var t = refBar && refBar.time;
    if (typeof t === 'string' && t.length === 10 && /^\d{4}-\d{2}-\d{2}$/.test(t)) {
      return istDayString(sec * 1000);
    }
    return Math.floor(sec);
  }

  function getTimeScaleExtrapolationBasis() {
    if (!mainChart || !allBars.length) return null;
    var ts = mainChart.timeScale();
    var bR = allBars[allBars.length - 1];
    var xR = ts.timeToCoordinate(bR.time);
    var secR = chartTimeToUnixSec(bR.time);
    if (xR == null || !isFinite(secR)) return null;
    var xL = null;
    var secL = null;
    for (var k = allBars.length - 2; k >= 0; k--) {
      var bx = ts.timeToCoordinate(allBars[k].time);
      if (bx != null && Math.abs(bx - xR) > 0.5) {
        xL = bx;
        secL = chartTimeToUnixSec(allBars[k].time);
        break;
      }
    }
    if (xL == null || secL == null || !isFinite(secL)) {
      var ivCfg = IV_CFG[currentIv] || IV_CFG.day;
      var step = barStepSec(ivCfg.kite);
      if (!step) step = 86400;
      secL = secR - step;
      xL = xR - 24;
    }
    if (Math.abs(xR - xL) < 1e-6 || Math.abs(secR - secL) < 1e-6) return null;
    return { xL: xL, secL: secL, xR: xR, secR: secR };
  }

  function coordinateToTimeExtrapolated(canvasX) {
    if (!mainChart) return null;
    var ts = mainChart.timeScale();
    var t = ts.coordinateToTime(canvasX);
    if (t != null) return t;
    var basis = getTimeScaleExtrapolationBasis();
    if (!basis) return null;
    var secEx = basis.secR + ((canvasX - basis.xR) / (basis.xR - basis.xL)) * (basis.secR - basis.secL);
    if (!isFinite(secEx)) return null;
    return unixSecToChartTime(secEx, allBars[allBars.length - 1]);
  }

  function timeToCoordinateExtrapolated(time) {
    if (!mainChart) return null;
    var ts = mainChart.timeScale();
    var x = ts.timeToCoordinate(time);
    if (x != null) return x;
    var sec = chartTimeToUnixSec(time);
    if (!isFinite(sec)) return null;
    var basis = getTimeScaleExtrapolationBasis();
    if (!basis) return null;
    var xEx = basis.xR + (sec - basis.secR) / (basis.secR - basis.secL) * (basis.xR - basis.xL);
    return xEx;
  }

  /** LW often returns null for Y outside the pane; linear map from visible top/bottom anchors. */
  function coordinateToPriceExtrapolated(y) {
    if (!candleSeries) return null;
    var p = candleSeries.coordinateToPrice(y);
    if (p != null && isFinite(Number(p))) return p;
    var wrap = document.getElementById('sc-main-wrap');
    var h = wrap ? Math.max(wrap.clientHeight, 120) : 400;
    var yTop = 0;
    var yBot = h;
    var pTop = candleSeries.coordinateToPrice(yTop);
    var pBot = candleSeries.coordinateToPrice(yBot);
    if (pTop == null || pBot == null || !isFinite(pTop) || !isFinite(pBot)) {
      if (allBars.length) {
        var b = allBars[allBars.length - 1];
        return (Number(b.high) + Number(b.low)) / 2;
      }
      return null;
    }
    if (Math.abs(yBot - yTop) < 1e-6) return pTop;
    return pTop + ((y - yTop) / (yBot - yTop)) * (pBot - pTop);
  }

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

  function getTooltipPanel() {
    return document.getElementById('sc-tooltip-panel');
  }

  function closeAllPanels(except) {
    var ind = document.getElementById('sc-ind-panel');
    var lvl = document.getElementById('sc-lvl-panel');
    var tp = getTooltipPanel();
    if (except !== 'ind' && ind) ind.hidden = true;
    if (except !== 'lvl' && lvl) lvl.hidden = true;
    if (except !== 'tooltip' && tp) tp.hidden = true;
  }

  function replaceChartUrlInterval() {
    try {
      var u = new URL(window.location.href);
      u.searchParams.set('interval', currentIv);
      history.replaceState(null, '', u.pathname + u.search);
    } catch (_) {}
  }

  function detachTlDraftPreviewListeners() {
    if (tlDraftMoveBound) {
      window.removeEventListener('mousemove', tlDraftMoveBound);
      tlDraftMoveBound = null;
    }
    tlPreviewPx = null;
  }

  function attachTlDraftPreviewListeners() {
    if (tlDraftMoveBound) return;
    tlDraftMoveBound = function (ev) {
      if (!tlMode || tlAnchors.length !== 1 || !mainChart) return;
      var p = canvasPixelFromClient(ev.clientX, ev.clientY);
      if (!p) return;
      tlPreviewPx = p;
      scheduleDrawTrendlines();
    };
    window.addEventListener('mousemove', tlDraftMoveBound);
  }

  function endTlRmbDrag() {
    var had = !!tlRmbDrag;
    var pid = tlRmbDrag && tlRmbDrag.pointerId;
    var canvasEl = getTlCanvas();
    if (pid != null && canvasEl && typeof canvasEl.releasePointerCapture === 'function') {
      try { canvasEl.releasePointerCapture(pid); } catch (_) {}
    }
    tlRmbDrag = null;
    if (had) scheduleSaveAnnotations();
  }

  function exitTrendlineMode() {
    if (!tlMode) return;
    tlMode = false;
    tlAnchors = [];
    detachTlDraftPreviewListeners();
    if (tlRedrawRaf != null) {
      cancelAnimationFrame(tlRedrawRaf);
      tlRedrawRaf = null;
    }
    if (tlPulseTimer) {
      clearTimeout(tlPulseTimer);
      tlPulseTimer = null;
    }
    tlAnchorPulse = null;
    var tlBtn = document.getElementById('sc-trendline-btn');
    var canvas = getTlCanvas();
    if (tlBtn) {
      tlBtn.classList.remove('active');
      tlBtn.setAttribute('aria-pressed', 'false');
    }
    if (canvas) canvas.classList.remove('draw-mode');
    drawTrendlines();
  }

  function updateDeleteSelectedTlButton() {
    var delBtn = document.getElementById('sc-delete-selected-tl');
    if (delBtn) delBtn.disabled = !selectedTlId;
  }

  function setSelectedTrendline(id) {
    selectedTlId = id || null;
    updateDeleteSelectedTlButton();
    drawTrendlines();
    renderChips();
  }

  function exitTlSelectMode() {
    if (!tlSelectMode) return;
    tlSelectMode = false;
    endTlRmbDrag();
    selectedTlId = null;
    updateDeleteSelectedTlButton();
    var selBtn = document.getElementById('sc-tl-select-btn');
    var canvas = getTlCanvas();
    if (selBtn) {
      selBtn.classList.remove('active');
      selBtn.setAttribute('aria-pressed', 'false');
    }
    if (canvas) canvas.classList.remove('select-mode');
    drawTrendlines();
    renderChips();
  }

  function setTlSelectMode(on) {
    var canvas = getTlCanvas();
    if (on) {
      exitTrendlineMode();
      tlSelectMode = true;
      var selBtn = document.getElementById('sc-tl-select-btn');
      if (selBtn) {
        selBtn.classList.add('active');
        selBtn.setAttribute('aria-pressed', 'true');
      }
      if (canvas) canvas.classList.add('select-mode');
    } else {
      exitTlSelectMode();
    }
  }

  function deleteSelectedTrendline() {
    if (!selectedTlId) return;
    endTlRmbDrag();
    var id = selectedTlId;
    trendlines = trendlines.filter(function (x) { return x.id !== id; });
    selectedTlId = null;
    updateDeleteSelectedTlButton();
    scheduleSaveAnnotations();
    drawTrendlines();
    renderChips();
  }

  function isTypingTarget(el) {
    if (!el || !el.tagName) return false;
    var t = el.tagName.toLowerCase();
    if (t === 'input' || t === 'textarea' || t === 'select') return true;
    if (el.isContentEditable) return true;
    return false;
  }

  function setLvlError(msg) {
    var el = document.getElementById('sc-lvl-err');
    if (!el) return;
    if (msg) {
      el.textContent = msg;
      el.classList.add('sc-visible');
    } else {
      el.textContent = '';
      el.classList.remove('sc-visible');
    }
  }

  function updateQuoteBarLayout() {
    var bar = document.getElementById('sc-quote-bar');
    var area = document.getElementById('sc-chart-area');
    if (!bar || !area || area.hidden) return;
    bar.hidden = false;
  }

  function setWsUiState(state) {
    wsLiveState = state;
    var dot = document.getElementById('sc-live-dot');
    var label = document.getElementById('sc-live-label');
    if (!dot || !label) return;
    dot.classList.remove('sc-live-on', 'sc-live-warn', 'sc-live-off');
    if (state === 'live') {
      dot.classList.add('sc-live-on');
      label.textContent = 'Live ticks';
    } else if (state === 'connecting') {
      dot.classList.add('sc-live-warn');
      label.textContent = 'Connecting\u2026';
    } else if (state === 'reconnecting') {
      dot.classList.add('sc-live-warn');
      label.textContent = 'Reconnecting\u2026';
    } else if (state === 'aggregated') {
      dot.classList.add('sc-live-off');
      label.textContent = 'No live stream (aggregated chart)';
    } else {
      dot.classList.add('sc-live-off');
      label.textContent = 'Ticks unavailable';
    }
  }

  function refreshQuoteBarFromBars() {
    var ltpEl = document.getElementById('sc-ltp');
    var chgEl = document.getElementById('sc-chg-bar');
    if (!ltpEl || !chgEl || !allBars.length) return;
    var b = liveBar || allBars[allBars.length - 1];
    if (!b) return;
    var c = Number(b.close);
    var o = Number(b.open);
    ltpEl.textContent = isFinite(c) ? fmtNum(c) : '\u2014';
    if (isFinite(c) && isFinite(o) && o) {
      var pct = ((c - o) / o) * 100;
      chgEl.textContent = (pct >= 0 ? '+' : '') + fmtNum(pct, 2) + '%';
      chgEl.classList.toggle('sc-chg-up', pct >= 0);
      chgEl.classList.toggle('sc-chg-down', pct < 0);
    } else {
      chgEl.textContent = '';
      chgEl.classList.remove('sc-chg-up', 'sc-chg-down');
    }
  }

  function updateStaleHint() {
    var hint = document.getElementById('sc-stale-hint');
    if (!hint) return;
    var ivCfg = IV_CFG[currentIv] || IV_CFG.day;
    if (ivCfg.agg || !lastTick || !lastTick.ts) {
      hint.hidden = true;
      return;
    }
    var age = Date.now() - Number(lastTick.ts);
    if (age > STALE_TICK_MS) {
      hint.hidden = false;
      hint.textContent = 'No ticks for ' + Math.round(age / 60000) + 'm (check session or market hours)';
    } else {
      hint.hidden = true;
    }
  }

  /**
   * Apply measured width/height to Lightweight Charts panes. The library does not
   * infer height from CSS alone; ResizeObserver previously updated width only,
   * which left the RSI pane at 0 height after the chart area became visible.
   * Do not call alignRsiTimeScaleToMain here — that fights user zoom/pan; range
   * sync is handled by main → RSI subscribeVisibleLogicalRangeChange.
   */
  function syncChartPaneSizes() {
    var area = document.getElementById('sc-chart-area');
    if (area && area.hidden) return;

    var mainEl = document.getElementById('sc-main-chart');
    var mainWrap = document.getElementById('sc-main-wrap');
    var rsiEl = document.getElementById('sc-rsi-chart');
    var rsiWrap = document.getElementById('sc-rsi-wrap');
    if (!mainChart || !mainEl || !mainWrap) return;

    var mw = Math.max(mainWrap.clientWidth, mainEl.offsetWidth, 1);
    var mh = Math.max(mainWrap.clientHeight, mainEl.offsetHeight, 1);
    void mainWrap.offsetHeight;
    mw = Math.max(mainWrap.clientWidth, mainEl.offsetWidth, mw);
    mh = Math.max(mainWrap.clientHeight, mainEl.offsetHeight, mh);
    if (mw < 48) mw = 320;
    if (mh < 48) mh = 280;

    mainChart.applyOptions({ width: mw, height: mh });

    if (!rsiChart || !rsiEl || !showRsi) {
      drawTrendlines();
      return;
    }

    var rw = Math.max(rsiWrap ? rsiWrap.clientWidth : rsiEl.offsetWidth, rsiEl.offsetWidth, 1);
    var rh = Math.max(rsiWrap ? rsiWrap.clientHeight : rsiEl.offsetHeight, rsiEl.offsetHeight, 1);
    if (rsiWrap) void rsiWrap.offsetHeight;
    rw = Math.max(rsiWrap ? rsiWrap.clientWidth : rsiEl.offsetWidth, rsiEl.offsetWidth, rw);
    rh = Math.max(rsiWrap ? rsiWrap.clientHeight : rsiEl.offsetHeight, rsiEl.offsetHeight, rh);
    if (rw < 48) rw = Math.max(mw, 200);
    if (rh < 40) rh = 120;

    rsiChart.applyOptions({ width: rw, height: rh });

    drawTrendlines();
  }

  function scheduleSyncChartPaneSizes() {
    if (chartResizeDebounceTimer) clearTimeout(chartResizeDebounceTimer);
    chartResizeDebounceTimer = setTimeout(function () {
      chartResizeDebounceTimer = null;
      syncChartPaneSizes();
    }, 100);
  }

  function applyRsiVisibility() {
    var area = document.getElementById('sc-chart-area');
    var rsiCb = document.getElementById('sc-show-rsi');
    if (rsiCb) rsiCb.checked = showRsi;
    if (area) area.classList.toggle('sc-rsi-hidden', !showRsi);
    try { localStorage.setItem(RSI_SHOW_KEY, showRsi ? '1' : '0'); } catch (_) {}
    requestAnimationFrame(function () {
      syncChartPaneSizes();
      requestAnimationFrame(syncChartPaneSizes);
    });
  }

  // ── Charts lifecycle ─────────────────────────────────────────────────────
  function destroyCharts() {
    clearStaleCheck();
    endTlRmbDrag();
    detachTlDraftPreviewListeners();
    if (tlPulseTimer) {
      clearTimeout(tlPulseTimer);
      tlPulseTimer = null;
    }
    tlAnchorPulse = null;
    if (tlRedrawRaf != null) {
      cancelAnimationFrame(tlRedrawRaf);
      tlRedrawRaf = null;
    }
    if (chartResizeDebounceTimer) {
      clearTimeout(chartResizeDebounceTimer);
      chartResizeDebounceTimer = null;
    }
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

    // Sync main → RSI by *visible logical range*, 1:1 (no offset).
    // RSI is padded with whitespace prefix in calcRSI, so both series have the
    // same logical bar count and identical times. Mirroring with no offset
    // means Lightweight Charts has nothing to clamp on the RSI side, which
    // previously destabilized the layout and let the live candle drift out of
    // view after enough ticks/resizes. RSI → main is intentionally omitted to
    // keep the main viewport authoritative.
    mainChart.timeScale().subscribeVisibleLogicalRangeChange(function (r) {
      if (paneRangeSyncing || !rsiChart || !r || r.from == null || r.to == null) return;
      paneRangeSyncing = true;
      try {
        rsiChart.timeScale().setVisibleLogicalRange({ from: r.from, to: r.to });
      } catch (_) {}
      paneRangeSyncing = false;
    });

    mainChart.subscribeCrosshairMove(function (param) {
      syncRsiCrosshairFromMain(param);
      onCrosshair(param);
    });
    rsiChart.subscribeCrosshairMove(function (param) {
      syncMainCrosshairFromRsi(param);
    });

    chartResizeObs = new ResizeObserver(function () {
      scheduleSyncChartPaneSizes();
    });
    var mainWrap = document.getElementById('sc-main-wrap');
    var rsiWrap = document.getElementById('sc-rsi-wrap');
    if (mainWrap) chartResizeObs.observe(mainWrap);
    if (rsiWrap) chartResizeObs.observe(rsiWrap);
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
          if (!tl.id) tl.id = uid();
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

  /**
   * Update last RSI / MA points during live ticks without removing series or
   * calling setData on the full RSI series (that resets time scale and fights zoom).
   */
  function updateDerivedSeriesLive() {
    if (!mainChart || !candleSeries || !rsiLineSeries) return;
    var bars = barsForIndicators();
    if (!bars.length) return;

    cachedRsiPoints = calcRSI(bars, RSI_PERIOD);
    var lastRsi = cachedRsiPoints[cachedRsiPoints.length - 1];
    if (lastRsi) {
      try { rsiLineSeries.update(lastRsi); } catch (_) {}
    }

    indicators.forEach(function (ind) {
      if (!ind.series) return;
      var pts = ind.type === 'EMA' ? calcEMA(bars, ind.period) : calcSMA(bars, ind.period);
      var lp = pts[pts.length - 1];
      if (lp) {
        try { ind.series.update(lp); } catch (_) {}
      }
    });
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

  function findTrendlineById(id) {
    for (var i = 0; i < trendlines.length; i++) {
      if (trendlines[i].id === id) return trendlines[i];
    }
    return null;
  }

  function scheduleDrawTrendlines() {
    if (tlRedrawRaf != null) return;
    tlRedrawRaf = requestAnimationFrame(function () {
      tlRedrawRaf = null;
      drawTrendlines();
    });
  }

  function canvasPixelFromClient(clientX, clientY) {
    var canvas = getTlCanvas();
    if (!canvas) return null;
    var rect = canvas.getBoundingClientRect();
    return { x: clientX - rect.left, y: clientY - rect.top };
  }

  function drawAnchorMarker(ctx, x, y) {
    if (x == null || y == null || isNaN(x) || isNaN(y)) return;
    var r = 5;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(15,23,42,.85)';
    ctx.fill();
    ctx.strokeStyle = 'rgba(250,250,250,.95)';
    ctx.lineWidth = 2;
    ctx.stroke();
  }

  /** Raw chart pixels for time1/price1 and time2/price2 (not segment-extended). */
  function getTrendlineAnchorPixels(tl) {
    if (!mainChart || !candleSeries) return null;
    var x1 = timeToCoordinateExtrapolated(tl.time1);
    var y1 = candleSeries.priceToCoordinate(tl.price1);
    var x2 = timeToCoordinateExtrapolated(tl.time2);
    var y2 = candleSeries.priceToCoordinate(tl.price2);
    if (x1 == null || y1 == null || x2 == null || y2 == null) return null;
    return { x1: x1, y1: y1, x2: x2, y2: y2 };
  }

  function getTrendlinePixelEndpoints(tl, w) {
    if (!mainChart || !candleSeries) return null;
    var anchors = getTrendlineAnchorPixels(tl);
    if (!anchors) return null;
    var x1 = anchors.x1, y1 = anchors.y1, x2 = anchors.x2, y2 = anchors.y2;
    if (tl.extended && x1 !== x2) {
      var dx = x2 - x1, dy = y2 - y1;
      var t0 = (-x1) / dx;
      var t1 = (w - x1) / dx;
      var ta = Math.min(t0, t1);
      var tb = Math.max(t0, t1);
      return {
        x1: x1 + ta * dx,
        y1: y1 + ta * dy,
        x2: x1 + tb * dx,
        y2: y1 + tb * dy
      };
    }
    return { x1: x1, y1: y1, x2: x2, y2: y2 };
  }

  /** 1 = first anchor, 2 = second; null if outside handle radius. */
  function hitTestTrendlineHandle(px, py, tl) {
    var ap = getTrendlineAnchorPixels(tl);
    if (!ap) return null;
    var d1 = Math.hypot(px - ap.x1, py - ap.y1);
    var d2 = Math.hypot(px - ap.x2, py - ap.y2);
    if (d1 <= TL_HANDLE_HIT_PX && d1 <= d2) return 1;
    if (d2 <= TL_HANDLE_HIT_PX) return 2;
    return null;
  }

  /**
   * Hit for parallel line move: on the drawn segment but not on an endpoint handle.
   * Handles always resize one anchor (left or right button); the line body moves both.
   */
  function hitTestTrendlineParallelBody(px, py, tl, w) {
    if (!tl) return false;
    var ep = getTrendlinePixelEndpoints(tl, w);
    if (!ep) return false;
    if (distPointToSegment(px, py, ep.x1, ep.y1, ep.x2, ep.y2) > TL_HIT_PX) return false;
    var h = hitTestTrendlineHandle(px, py, tl);
    if (h != null) return false;
    return true;
  }

  function distPointToSegment(px, py, x1, y1, x2, y2) {
    var dx = x2 - x1, dy = y2 - y1;
    var len2 = dx * dx + dy * dy;
    if (len2 < 1e-12) {
      var ux = px - x1, uy = py - y1;
      return Math.sqrt(ux * ux + uy * uy);
    }
    var t = ((px - x1) * dx + (py - y1) * dy) / len2;
    t = Math.max(0, Math.min(1, t));
    var qx = x1 + t * dx, qy = y1 + t * dy;
    var qdx = px - qx, qdy = py - qy;
    return Math.sqrt(qdx * qdx + qdy * qdy);
  }

  /** Closest point on segment (x1,y1)—(x2,y2) to (px,py); t in [0,1]. */
  function projectPointToSegment(px, py, x1, y1, x2, y2) {
    var dx = x2 - x1, dy = y2 - y1;
    var len2 = dx * dx + dy * dy;
    if (len2 < 1e-12) return { x: x1, y: y1, t: 0 };
    var t = ((px - x1) * dx + (py - y1) * dy) / len2;
    t = Math.max(0, Math.min(1, t));
    return { x: x1 + t * dx, y: y1 + t * dy, t: t };
  }

  function hitTestTrendlineIds(px, py, w) {
    var best = null;
    var bestD = TL_HIT_PX + 1;
    for (var i = 0; i < trendlines.length; i++) {
      var tl = trendlines[i];
      var ep = getTrendlinePixelEndpoints(tl, w);
      if (!ep) continue;
      var d = distPointToSegment(px, py, ep.x1, ep.y1, ep.x2, ep.y2);
      if (d <= TL_HIT_PX && d < bestD) {
        bestD = d;
        best = tl.id;
      }
    }
    return best;
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

    trendlines.forEach(function (tl) {
      var ep = getTrendlinePixelEndpoints(tl, w);
      if (!ep) return;
      var lw = Math.max(1, Number(tl.width) || 1);
      var sel = tl.id === selectedTlId;
      if (sel) {
        ctx.strokeStyle = 'rgba(255,255,255,.4)';
        ctx.lineWidth = lw + 8;
        ctx.setLineDash([]);
        ctx.beginPath();
        ctx.moveTo(ep.x1, ep.y1);
        ctx.lineTo(ep.x2, ep.y2);
        ctx.stroke();
      }
      ctx.strokeStyle = tl.color || '#f59e0b';
      ctx.lineWidth = sel ? lw + 2 : lw;
      ctx.setLineDash(tl.style === 'dashed' ? [6, 4] : []);
      ctx.beginPath();
      ctx.moveTo(ep.x1, ep.y1);
      ctx.lineTo(ep.x2, ep.y2);
      ctx.stroke();
      ctx.setLineDash([]);
      if (sel) {
        var ap = getTrendlineAnchorPixels(tl);
        if (ap) {
          drawAnchorMarker(ctx, ap.x1, ap.y1);
          drawAnchorMarker(ctx, ap.x2, ap.y2);
        }
      }
    });

    if (tlMode && tlAnchors.length === 1 && mainChart && candleSeries) {
      var a0 = tlAnchors[0];
      var ax = timeToCoordinateExtrapolated(a0.time);
      var ay = candleSeries.priceToCoordinate(a0.price);
      if (ax != null && ay != null) {
        drawAnchorMarker(ctx, ax, ay);
        if (tlPreviewPx) {
          ctx.strokeStyle = 'rgba(250,204,21,.75)';
          ctx.lineWidth = 2;
          ctx.setLineDash([4, 4]);
          ctx.beginPath();
          ctx.moveTo(ax, ay);
          ctx.lineTo(tlPreviewPx.x, tlPreviewPx.y);
          ctx.stroke();
          ctx.setLineDash([]);
        }
      }
    }

    if (tlAnchorPulse) {
      for (var pi = 0; pi < tlAnchorPulse.length; pi++) {
        drawAnchorMarker(ctx, tlAnchorPulse[pi].x, tlAnchorPulse[pi].y);
      }
    }
  }

  function canvasPointToChart(ev) {
    var canvas = getTlCanvas();
    if (!canvas || !mainChart || !candleSeries) return null;
    var rect = canvas.getBoundingClientRect();
    var x = ev.clientX - rect.left;
    var y = ev.clientY - rect.top;
    var time = coordinateToTimeExtrapolated(x);
    var price = coordinateToPriceExtrapolated(y);
    if (time == null || price == null) return null;
    return { time: time, price: price };
  }

  function onTlRmbDragMove(ev) {
    if (!tlRmbDrag || !mainChart || !candleSeries) return;
    var tlx = findTrendlineById(tlRmbDrag.tlId);
    if (!tlx) {
      endTlRmbDrag();
      return;
    }
    if (tlRmbDrag.mode === 'endpoint') {
      var p = canvasPixelFromClient(ev.clientX, ev.clientY);
      if (!p) return;
      var time = coordinateToTimeExtrapolated(p.x);
      var price = coordinateToPriceExtrapolated(p.y);
      if (time == null || price == null) return;
      var fo = tlRmbDrag.fixedOther;
      if (tlRmbDrag.whichEnd === 1) {
        tlx.time1 = time;
        tlx.price1 = price;
        if (fo) {
          tlx.time2 = fo.time;
          tlx.price2 = fo.price;
        }
      } else {
        tlx.time2 = time;
        tlx.price2 = price;
        if (fo) {
          tlx.time1 = fo.time;
          tlx.price1 = fo.price;
        }
      }
      scheduleDrawTrendlines();
      return;
    }
    if (tlRmbDrag.mode !== 'move') return;
    var p = canvasPixelFromClient(ev.clientX, ev.clientY);
    if (!p) return;
    var ddx = p.x - tlRmbDrag.gx;
    var ddy = p.y - tlRmbDrag.gy;
    var ep = tlRmbDrag.ep0;
    var t1 = coordinateToTimeExtrapolated(ep.x1 + ddx);
    var pr1 = coordinateToPriceExtrapolated(ep.y1 + ddy);
    var t2 = coordinateToTimeExtrapolated(ep.x2 + ddx);
    var pr2 = coordinateToPriceExtrapolated(ep.y2 + ddy);
    if (t1 == null || pr1 == null || t2 == null || pr2 == null) return;
    tlx.time1 = t1;
    tlx.price1 = pr1;
    tlx.time2 = t2;
    tlx.price2 = pr2;
    scheduleDrawTrendlines();
  }

  function bindTrendlineCanvas() {
    var canvas = getTlCanvas();
    if (!canvas || canvas.dataset.scTlBound === '1') return;
    canvas.dataset.scTlBound = '1';

    canvas.addEventListener('contextmenu', function (ev) {
      if (!tlSelectMode || !mainChart || !selectedTlId) return;
      var rect = canvas.getBoundingClientRect();
      var px = ev.clientX - rect.left;
      var py = ev.clientY - rect.top;
      var wrap = document.getElementById('sc-main-wrap');
      var wid = wrap ? wrap.clientWidth : 0;
      var selTl = findTrendlineById(selectedTlId);
      var onHandle = selTl && hitTestTrendlineHandle(px, py, selTl);
      var hit = hitTestTrendlineIds(px, py, wid);
      if (onHandle || hit === selectedTlId || !!tlRmbDrag) ev.preventDefault();
    });

    var onTlDragPointerMove = function (ev) {
      if (!tlRmbDrag || ev.pointerId !== tlRmbDrag.pointerId) return;
      var p = canvasPixelFromClient(ev.clientX, ev.clientY);
      if (p && (Math.abs(p.x - tlRmbDrag.downPx) > 1.5 || Math.abs(p.y - tlRmbDrag.downPy) > 1.5)) {
        tlRmbDrag.didMove = true;
      }
      onTlRmbDragMove(ev);
    };
    var onTlDragPointerEnd = function (ev) {
      if (!tlRmbDrag || ev.pointerId !== tlRmbDrag.pointerId) return;
      if (tlRmbDrag.didMove && tlRmbDrag.button === 0) tlSuppressSelectClick = true;
      endTlRmbDrag();
    };
    canvas.addEventListener('pointermove', onTlDragPointerMove);
    canvas.addEventListener('pointerup', onTlDragPointerEnd);
    canvas.addEventListener('pointercancel', onTlDragPointerEnd);

    canvas.addEventListener('pointerdown', function (ev) {
      if (!tlSelectMode || !mainChart || !candleSeries || !selectedTlId) return;
      if (ev.pointerType !== 'mouse') return;
      if (ev.button !== 0 && ev.button !== 2) return;
      if (tlRmbDrag) return;
      var rect = canvas.getBoundingClientRect();
      var px = ev.clientX - rect.left;
      var py = ev.clientY - rect.top;
      var wrap = document.getElementById('sc-main-wrap');
      var wid = wrap ? wrap.clientWidth : 0;
      var tl = findTrendlineById(selectedTlId);
      if (!tl) return;
      var whichEnd = hitTestTrendlineHandle(px, py, tl);
      if (whichEnd != null) {
        ev.preventDefault();
        var fixedOther = whichEnd === 1
          ? { time: tl.time2, price: tl.price2 }
          : { time: tl.time1, price: tl.price1 };
        tlRmbDrag = {
          mode: 'endpoint',
          whichEnd: whichEnd,
          fixedOther: fixedOther,
          tlId: selectedTlId,
          pointerId: ev.pointerId,
          button: ev.button,
          didMove: false,
          downPx: px,
          downPy: py
        };
        try {
          if (typeof canvas.setPointerCapture === 'function') {
            canvas.setPointerCapture(ev.pointerId);
          }
        } catch (_) {}
        onTlRmbDragMove(ev);
        return;
      }
      if (!hitTestTrendlineParallelBody(px, py, tl, wid)) return;
      var ep = getTrendlinePixelEndpoints(tl, wid);
      if (!ep) return;
      ev.preventDefault();
      var grab = projectPointToSegment(px, py, ep.x1, ep.y1, ep.x2, ep.y2);
      tlRmbDrag = {
        mode: 'move',
        tlId: selectedTlId,
        ep0: { x1: ep.x1, y1: ep.y1, x2: ep.x2, y2: ep.y2 },
        gx: grab.x,
        gy: grab.y,
        pointerId: ev.pointerId,
        button: ev.button,
        didMove: false,
        downPx: px,
        downPy: py
      };
      try {
        if (typeof canvas.setPointerCapture === 'function') {
          canvas.setPointerCapture(ev.pointerId);
        }
      } catch (_) {}
    });

    canvas.addEventListener('click', function (ev) {
      if (tlSelectMode && mainChart) {
        if (tlSuppressSelectClick) {
          tlSuppressSelectClick = false;
          ev.stopPropagation();
          return;
        }
        var rect = canvas.getBoundingClientRect();
        var px = ev.clientX - rect.left;
        var py = ev.clientY - rect.top;
        var wrap = document.getElementById('sc-main-wrap');
        var wid = wrap ? wrap.clientWidth : 0;
        var hit = hitTestTrendlineIds(px, py, wid);
        setSelectedTrendline(hit);
        ev.stopPropagation();
        return;
      }
      if (!tlMode || !mainChart) return;
      var pt = canvasPointToChart(ev);
      if (!pt) return;
      if (tlAnchors.length === 0) {
        tlAnchors.push(pt);
        var prim = canvasPixelFromClient(ev.clientX, ev.clientY);
        if (prim) tlPreviewPx = prim;
        attachTlDraftPreviewListeners();
        scheduleDrawTrendlines();
        return;
      }
      var a0 = tlAnchors[0];
      var ax = timeToCoordinateExtrapolated(a0.time);
      var ay = candleSeries.priceToCoordinate(a0.price);
      var bx = timeToCoordinateExtrapolated(pt.time);
      var by = candleSeries.priceToCoordinate(pt.price);
      if (tlPulseTimer) {
        clearTimeout(tlPulseTimer);
        tlPulseTimer = null;
      }
      tlAnchorPulse = (ax != null && ay != null && bx != null && by != null)
        ? [{ x: ax, y: ay }, { x: bx, y: by }]
        : null;
      tlAnchors = [];
      detachTlDraftPreviewListeners();
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
      if (tlAnchorPulse) {
        tlPulseTimer = setTimeout(function () {
          tlPulseTimer = null;
          tlAnchorPulse = null;
          drawTrendlines();
        }, 450);
      }
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

  function clearStaleCheck() {
    if (staleCheckTimer) {
      clearInterval(staleCheckTimer);
      staleCheckTimer = null;
    }
  }

  function connectWS() {
    closeWS();
    var ivCfg = IV_CFG[currentIv] || IV_CFG.day;
    if (ivCfg.agg) {
      setWsUiState('aggregated');
      return;
    }

    setWsUiState('connecting');
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var url = proto + '//' + location.host + '/ws/chart-ticks?instrument_token=' + TOKEN;
    try {
      ws = new WebSocket(url);
    } catch (_) {
      setWsUiState('reconnecting');
      return;
    }

    ws.onopen = function () {
      setWsUiState('live');
    };

    ws.onerror = function () {
      if (ws && ws.readyState !== WebSocket.OPEN) setWsUiState('reconnecting');
    };

    ws.onmessage = function (ev) {
      try {
        var msg = JSON.parse(ev.data);
        if (msg && msg.t === 'tick') onTick(msg);
      } catch (_) {}
    };

    ws.onclose = function () {
      ws = null;
      if (!TOKEN || (IV_CFG[currentIv] || {}).agg) return;
      setWsUiState('reconnecting');
      wsReconnectTimer = setTimeout(connectWS, WS_RECONNECT_MS);
    };
  }

  function onTick(tick) {
    if (!candleSeries || !allBars.length || !liveBar) return;

    var ltp = Number(tick.ltp);
    if (!isFinite(ltp) || ltp <= 0) return;

    lastTick = tick;

    var tsMs = Number(tick.ts) || Date.now();
    var tsSec = Math.floor(tsMs / 1000);
    var ivCfg = IV_CFG[currentIv] || IV_CFG.day;
    var kiteIv = ivCfg.kite;
    var step = barStepSec(kiteIv);

    var newBar = false;
    if (kiteIv === 'day') {
      var dStr = istDayString(tsMs);
      var lastDayKey = istDayKeyForBar(liveBar);
      if (lastDayKey) {
        if (dStr < lastDayKey) return;
        if (dStr > lastDayKey) {
          newBar = true;
          allBars.push({ time: dStr, open: ltp, high: ltp, low: ltp, close: ltp, volume: Number(tick.ltq) || 0 });
          liveBar = allBars[allBars.length - 1];
        }
      } else if (dStr !== liveBar.time) {
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
    updateDerivedSeriesLive();
    drawTrendlines();
    refreshQuoteBarFromBars();
    updateStaleHint();
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
    if (addInd) {
      addInd.disabled = full;
      addInd.title = full ? ('Maximum ' + MAX_MAIN_INDICATORS + ' indicators — remove one to add another') : '';
    }
    var hint = document.getElementById('sc-ind-limit-hint');
    if (hint) {
      if (full) {
        hint.textContent = 'Maximum ' + MAX_MAIN_INDICATORS + ' indicators on the chart. Remove one from the chips above to add another.';
        hint.classList.add('sc-visible');
      } else {
        hint.textContent = '';
        hint.classList.remove('sc-visible');
      }
    }
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
      if (tl.id === selectedTlId) chip.classList.add('sc-tl-selected');
      chip.innerHTML = 'TL ' + (idx + 1) +
        '<span class="sc-chip-remove" data-tlid="' + tl.id + '">\u00d7</span>';
      chip.addEventListener('click', function (e) {
        if (e.target.closest && e.target.closest('.sc-chip-remove')) return;
        setSelectedTrendline(tl.id === selectedTlId ? null : tl.id);
      });
      chip.querySelector('.sc-chip-remove').addEventListener('click', function (e) {
        e.stopPropagation();
        var id = tl.id;
        if (selectedTlId === id) {
          selectedTlId = null;
          updateDeleteSelectedTlButton();
        }
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
      var on = iv === currentIv;
      btn.classList.toggle('active', on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
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
    h.id = 'sc-tooltip-panel-title';
    h.textContent = 'Tooltip fields';
    panel.appendChild(h);
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-modal', 'true');
    panel.setAttribute('aria-labelledby', 'sc-tooltip-panel-title');

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

    btn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      if (panel.hidden) {
        closeAllPanels('tooltip');
        panel.hidden = false;
        var firstCb = panel.querySelector('input[type="checkbox"]');
        if (firstCb) setTimeout(function () { try { firstCb.focus(); } catch (_) {} }, 0);
      } else {
        panel.hidden = true;
      }
    });
  }

  function isPanelOpenTrigger(el) {
    if (!el || !el.closest) return false;
    return !!el.closest('#sc-add-ind') || !!el.closest('#sc-add-lvl') || !!el.closest('#sc-tooltip-btn');
  }

  function setupGlobalUiHandlers() {
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') {
        closeAllPanels(null);
        exitTrendlineMode();
        exitTlSelectMode();
        return;
      }
      if ((ev.key === 'Delete' || ev.key === 'Backspace') && selectedTlId) {
        if (isTypingTarget(ev.target)) return;
        ev.preventDefault();
        deleteSelectedTrendline();
      }
    });

    document.addEventListener('mousedown', function (ev) {
      var t = ev.target;
      if (t.closest && t.closest('.sc-panel')) return;
      if (isPanelOpenTrigger(t)) return;
      closeAllPanels(null);
    });
  }

  function setupUI() {
    syncIntervalButtons();
    buildTooltipSettingsPanel();
    bindTrendlineCanvas();
    setupGlobalUiHandlers();

    document.querySelectorAll('.sc-ivbtn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var iv = btn.getAttribute('data-iv');
        if (!iv || iv === currentIv) return;
        currentIv = iv;
        syncIntervalButtons();
        replaceChartUrlInterval();
        closeWS();
        destroyCharts();
        initCharts();
        loadHistory();
      });
    });

    var addInd = document.getElementById('sc-add-ind');
    var indPanel = document.getElementById('sc-ind-panel');
    if (addInd && indPanel) {
      addInd.addEventListener('click', function (ev) {
        ev.stopPropagation();
        closeAllPanels('ind');
        indPanel.hidden = false;
        var sel = document.getElementById('sc-ind-type');
        if (sel) setTimeout(function () { try { sel.focus(); } catch (_) {} }, 0);
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
      addLvl.addEventListener('click', function (ev) {
        ev.stopPropagation();
        closeAllPanels('lvl');
        setLvlError('');
        lvlPanel.hidden = false;
        var inp = document.getElementById('sc-lvl-price');
        if (inp) setTimeout(function () { try { inp.focus(); } catch (_) {} }, 0);
      });
    }
    document.getElementById('sc-lvl-cancel') && document.getElementById('sc-lvl-cancel').addEventListener('click', function () {
      if (lvlPanel) lvlPanel.hidden = true;
      setLvlError('');
    });
    document.getElementById('sc-lvl-add-btn') && document.getElementById('sc-lvl-add-btn').addEventListener('click', function () {
      var raw = (document.getElementById('sc-lvl-price') || {}).value;
      var price = parseFloat(String(raw).replace(/,/g, ''));
      if (!isFinite(price)) {
        setLvlError('Enter a valid price.');
        return;
      }
      setLvlError('');
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
      tlBtn.addEventListener('click', function (ev) {
        ev.stopPropagation();
        if (tlMode) {
          exitTrendlineMode();
          return;
        }
        exitTlSelectMode();
        if (selectedTlId) {
          selectedTlId = null;
          updateDeleteSelectedTlButton();
          drawTrendlines();
          renderChips();
        }
        tlMode = true;
        tlBtn.classList.add('active');
        tlBtn.setAttribute('aria-pressed', 'true');
        canvas.classList.add('draw-mode');
        tlAnchors = [];
        closeAllPanels(null);
      });
    }

    var selTlBtn = document.getElementById('sc-tl-select-btn');
    if (selTlBtn && canvas) {
      selTlBtn.addEventListener('click', function (ev) {
        ev.stopPropagation();
        setTlSelectMode(!tlSelectMode);
      });
    }

    var delSelBtn = document.getElementById('sc-delete-selected-tl');
    if (delSelBtn) {
      delSelBtn.addEventListener('click', function (ev) {
        ev.stopPropagation();
        deleteSelectedTrendline();
      });
    }

    document.getElementById('sc-clear-tl') && document.getElementById('sc-clear-tl').addEventListener('click', function () {
      trendlines = [];
      tlAnchors = [];
      selectedTlId = null;
      updateDeleteSelectedTlButton();
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

    var rsiCb = document.getElementById('sc-show-rsi');
    if (rsiCb) {
      rsiCb.checked = showRsi;
      rsiCb.addEventListener('change', function () {
        showRsi = rsiCb.checked;
        applyRsiVisibility();
      });
    }

    applyRsiVisibility();

    window.addEventListener('beforeunload', function () {
      clearStaleCheck();
      closeWS();
    });
  }

  // ── History load ──────────────────────────────────────────────────────────
  function parseHistoryFetchResponse(r) {
    return r.text().then(function (text) {
      var body = {};
      if (text) {
        try {
          body = JSON.parse(text);
        } catch (_) {
          body = { error: text.trim().slice(0, 240) || ('HTTP ' + r.status) };
        }
      }
      return { ok: r.ok, status: r.status, body: body };
    });
  }

  function loadHistory() {
    if (!TOKEN) { showStatus('Missing instrument token.'); return; }
    if (!candleSeries) return;

    showStatus('Loading\u2026');
    lastTick = null;
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
      .then(parseHistoryFetchResponse)
      .then(function (res) {
        if (!res.ok) {
          var msg = (res.body && res.body.error) ? String(res.body.error) : 'Could not load chart data.';
          if (res.status === 401) msg = 'Session expired. Open the dashboard and sign in again.';
          showStatus(msg);
          return;
        }
        var body = res.body;
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
        syncChartPaneSizes();
        // Suppress main → RSI range sync while we seed every series with
        // setData. Each setData triggers its chart's auto-fit (including the
        // rightOffset gutter). Letting the sync mirror those intermediate
        // states drives multiple competing time-scale fits and can leave the
        // live candle drifting out of view across subsequent ticks/resizes.
        // We re-sync once explicitly via alignRsiTimeScaleToMain() below.
        paneRangeSyncing = true;
        try {
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
        } finally {
          paneRangeSyncing = false;
        }
        renderChips();
        drawTrendlines();
        alignRsiTimeScaleToMain();
        updateQuoteBarLayout();
        refreshQuoteBarFromBars();
        applyRsiVisibility();
        clearStaleCheck();
        staleCheckTimer = setInterval(updateStaleHint, 15000);
        connectWS();
        replaceChartUrlInterval();
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
    updateDeleteSelectedTlButton();
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', window._scInit);
  } else {
    window._scInit();
  }
})();
