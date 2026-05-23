/**
 * Stock chart page (TradingView Lightweight Charts v4).
 */
import {
  TP_STORAGE_KEY,
  RSI_SHOW_KEY,
  RSI_PERIOD,
  MAX_MAIN_INDICATORS,
  DEFAULT_MAIN_INDICATORS,
  SAVE_DEBOUNCE_MS,
  WS_RECONNECT_MS,
  STALE_TICK_MS,
  IV_CFG,
  CHART_OPTS,
  MODE_NONE,
  MODE_CROSSHAIR,
  MODE_OBJECTS,
  FIB_LEVELS
} from './constants.js';
import { indicatorRegistry } from './indicatorRegistry.js';
import { drawingRegistry } from './drawingRegistry.js';
import './indicators/sma.js';
import './indicators/ema.js';
import './indicators/rsi.js';
import './indicators/fib_bands.js';
import './indicators/supertrend.js';
import './drawings/trendline.js';
import './drawings/fib_retracement.js';
import './drawings/parallel_channel.js';

import {
  aggregateBars,
  toTime,
  barStepSec,
  istDayString,
  istDayKeyForBar,
  liveBarTimeToSec,
  chartTimeToUnixSec,
  unixSecToChartTime
} from './barMath.js';

export function mountStockChartPage() {
  'use strict';

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
  var chartInteractionMode = MODE_NONE;
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

  function uid() { return Math.random().toString(36).slice(2); }

  var INDICATOR_RUNTIME_KEYS = {
    series: true,
    glowSeries: true,
    cachedPts: true,
    seriesArr: true,
    cachedPtsArr: true,
    fibGlowSeriesArr: true,
    seriesObj: true
  };

  function normalizeIndicatorType(type) {
    var raw = typeof type === 'string' ? type.trim() : '';
    if (!raw) return 'SMA';
    var upper = raw.toUpperCase();
    if (indicatorRegistry.get(upper)) return upper;
    if (indicatorRegistry.get(raw)) return raw;
    return indicatorRegistry.get('SMA') ? 'SMA' : upper;
  }

  function getIndicatorPluginDefaults(type) {
    var plugin = indicatorRegistry.get(normalizeIndicatorType(type));
    if (plugin && plugin.defaultOptions && typeof plugin.defaultOptions === 'object') {
      return plugin.defaultOptions;
    }
    return {};
  }

  function createIndicatorState(base) {
    var row = Object.assign({}, base || {});
    row.id = typeof row.id === 'string' && row.id ? row.id : uid();
    row.type = normalizeIndicatorType(row.type);
    row.period = Math.max(2, Math.min(200, parseInt(row.period, 10) || 20));
    row.color = typeof row.color === 'string' && row.color ? row.color : '#f59e0b';
    row.lineWidth = Math.max(1, Math.min(8, Number(row.lineWidth) || 2));
    row.style = row.style || 'solid';
    row.visible = row.visible !== false;
    row.series = null;
    row.glowSeries = null;
    row.cachedPts = [];
    row.seriesArr = [];
    row.cachedPtsArr = null;
    row.fibGlowSeriesArr = [];
    row.seriesObj = null;
    return row;
  }

  function getAddableIndicatorPlugins() {
    return indicatorRegistry.getAll().filter(function (plugin) {
      if (!plugin || !plugin.id) return false;
      if (plugin.userAddable === false) return false;
      if (plugin.isOscillator) return false;
      return true;
    });
  }

  function indicatorAddLabel(plugin) {
    if (!plugin) return 'Indicator';
    return plugin.addLabel || plugin.name || plugin.id;
  }

  function normalizeDrawingType(type) {
    var raw = typeof type === 'string' ? type.trim() : '';
    if (!raw) return 'TRENDLINE';
    var lower = raw.toLowerCase();
    if (lower === 'fib' || lower === 'fib_retracement') return 'FIB';
    if (lower === 'trendline' || lower === 'tl') return 'TRENDLINE';
    var upper = raw.toUpperCase();
    if (drawingRegistry.get(upper)) return upper;
    if (drawingRegistry.get(raw)) return raw;
    return drawingRegistry.get('TRENDLINE') ? 'TRENDLINE' : upper;
  }

  function getActiveDrawType() {
    return normalizeDrawingType(activeDrawType || 'TRENDLINE');
  }

  function setActiveDrawType(type) {
    activeDrawType = normalizeDrawingType(type || 'TRENDLINE');
  }

  function isAltDrawingModeActive() {
    return getActiveDrawType() !== 'TRENDLINE';
  }

  function getAddableDrawingPlugins() {
    return drawingRegistry.getAll().filter(function (plugin) {
      return !!(plugin && plugin.id && plugin.userAddable !== false);
    });
  }

  function drawingAddLabel(plugin) {
    if (!plugin) return 'Drawing';
    return plugin.addLabel || plugin.name || plugin.id;
  }

  function beginLevelDrawing() {
    drawingLevel = true;
    setActiveDrawType('TRENDLINE');
    tlAnchors = [];
    if (chartInteractionMode !== MODE_OBJECTS) {
      setChartInteractionMode(MODE_OBJECTS);
    } else {
      applyChartInteractionMode();
      updateExplicitButtonsState();
    }
    setSelectedLevel(null);
    attachTlDraftPreviewListeners();
    scheduleDrawTrendlines();
  }

  function beginObjectDrawing(drawType) {
    drawingLevel = false;
    setActiveDrawType(drawType);
    tlAnchors = [];
    if (chartInteractionMode !== MODE_OBJECTS) {
      setChartInteractionMode(MODE_OBJECTS);
    } else {
      applyChartInteractionMode();
      updateExplicitButtonsState();
    }
    setSelectedTrendline(null);
    attachTlDraftPreviewListeners();
    scheduleDrawTrendlines();
  }

  function createDefaultIndicators() {
    return DEFAULT_MAIN_INDICATORS.map(function (cfg) {
      var defaults = getIndicatorPluginDefaults(cfg.type);
      return createIndicatorState(Object.assign({}, defaults, cfg));
    });
  }

  function serializeIndicatorsForSave() {
    return indicators.map(function (ind) {
      var type = normalizeIndicatorType(ind.type);
      var defaults = getIndicatorPluginDefaults(type);
      var out = {};
      Object.keys(ind || {}).forEach(function (key) {
        if (INDICATOR_RUNTIME_KEYS[key]) return;
        var value = ind[key];
        if (value === undefined || typeof value === 'function') return;
        out[key] = value;
      });
      out.id = typeof out.id === 'string' && out.id ? out.id : uid();
      out.type = type;
      out.period = Math.max(2, Math.min(200, parseInt(out.period, 10) || parseInt(defaults.period, 10) || 20));
      out.color = typeof out.color === 'string' && out.color ? out.color : (defaults.color || '#f59e0b');
      out.lineWidth = Math.max(1, Math.min(8, Number(out.lineWidth) || Number(defaults.lineWidth) || 2));
      out.style = out.style || defaults.style || 'solid';
      out.visible = out.visible !== false;
      return out;
    });
  }

  function hydrateIndicatorsFromSave(rows) {
    if (!Array.isArray(rows) || !rows.length) return [];
    return rows.slice(0, MAX_MAIN_INDICATORS).map(function (row) {
      var type = normalizeIndicatorType(row && row.type);
      var defaults = getIndicatorPluginDefaults(type);
      var merged = Object.assign({}, defaults, row || {});
      merged.type = type;
      return createIndicatorState(merged);
    });
  }

  function annotationsPayloadForSave() {
    return {
      instrument_token: TOKEN,
      trendlines: trendlines,
      levels: levels,
      indicators: serializeIndicatorsForSave()
    };
  }

  var indicators = createDefaultIndicators();

  var trendlines = [];
  var levels = [];
  var levelPriceLines = {};
  var levelGlowPriceLines = {};
  var saveTimer = null;
  var tlAnchors = [];
  var drawingLevel = false;
  var activeDrawType = 'TRENDLINE';
  var tlPreviewPx = null;
  var tlDraftMoveBound = null;
  var selectedTlId = null;
  var selectedLevelId = null;
  var selectedIndId = null;
  /** Active vertical drag for a horizontal price level (pointer capture on main wrap). */
  var levelDrag = null;
  var LEVEL_HIT_PX = 8;
  var IND_HIT_PX = 10;
  /** When two indicators are equally close, prefer the one drawn on top. */
  var IND_HIT_TIE_PX = 0.75;
  var mainWrapOverlayBound = false;
  var mainWrapPdCapture = null;
  var mainWrapClickCaptureHandler = null;
  var mainWrapCtxMenu = null;
  var lastMainCtxPriceHint = null;
  var lastMainCtxClientX = 0;
  var lastMainCtxClientY = 0;
  var levelDragMoveDoc = null;
  var levelDragEndDoc = null;
  var TL_HIT_PX = 10;
  /** Pixel radius to grab start/end handles when reshaping with right-drag. */
  var TL_HANDLE_HIT_PX = 14;
  var tlRmbDrag = null;
  var tlDragDocMove = null;
  var tlDragDocEnd = null;
  var tlDragDocBound = false;
  var tlSuppressSelectClick = false;
  /** After first anchor on pointerdown, ignore the following click (mouseup) so the line is not finished in one press. */
  var tlSuppressNextDrawClick = false;
  var tlRedrawRaf = null;
  var tlAnchorPulse = null;
  var tlPulseTimer = null;
  /** Main chart DOM listeners (wheel / pointer) to redraw TLs when price scale changes; v4.2 has no price-scale subscription. */
  var tlMainDomSyncEl = null;
  var tlMainWrapSyncEl = null;
  var tlDomWheelHandler = null;
  var tlDomPointerDownHandler = null;
  /** LW may call setPointerCapture on the price axis; document capture + legacy mouse sees drags reliably. */
  var tlDocPointerSyncArmed = false;
  var tlDocPointerMoveSync = null;
  var tlDocMouseMoveSync = null;
  var tlDocDragEndSync = null;

  /** Main / RSI pane double-click → reset zoom (see bindPaneDoubleClickZoomReset). */
  var paneDblClickMainHandler = null;
  var paneDblClickRsiHandler = null;

  /** Last applied barSpacing|minBar|rightOffset from main → RSI (avoid redundant applyOptions). */
  var rsiTimeScaleSyncKey = null;

  var LW = window.LightweightCharts;
  var mainChart, rsiChart, candleSeries, volSeries, rsiLineSeries;
  var rsiObLine, rsiOsLine, rsiMidLine;
  var cachedRsiPoints = [];
  /** 0 idle, 1 syncing from main crosshair, 2 from RSI — prevents feedback loops */
  var crosshairSyncGuard = 0;
  /** Re-entrancy guard when programmatically snapping main crosshair Y to OHLC / MAs. */
  var crosshairOhlcSnapGuard = 0;
  /**
   * Last OHLC/MA snap we applied via setCrosshairPosition for the current bar.
   * param.point.y stays tied to the raw mouse, so we must not compare it to the snapped Y
   * (that would re-apply every move and skip RSI sync / tooltip / click snap below).
   */
  var crosshairOhlcSnapApplied = null;
  /** Last snapped price under crosshair (for click-to-place horizontal level). */
  var crosshairLastSnap = null;
  var crosshairSnapListenersBound = false;
  var crosshairSnapClick = null;

  /** Prevents ping-pong when mirroring visible *logical range* between main and RSI charts. */
  var paneRangeSyncing = false;

  function isDblClickOnChromeUi(tgt) {
    if (!tgt || !tgt.closest) return false;
    return !!(tgt.closest('button') || tgt.closest('a') || tgt.closest('input') ||
      tgt.closest('select') || tgt.closest('textarea') || tgt.closest('.sc-panel') ||
      tgt.closest('label'));
  }

  function shouldIgnoreMainPaneDoubleClick(ev) {
    if (isDblClickOnChromeUi(ev.target)) return true;
    var wrap = document.getElementById('sc-main-wrap');
    if (!wrap) return true;
    if (chartInteractionMode === MODE_OBJECTS) {
      var canvas = getTlCanvas();
      if (canvas && canvas.classList.contains('object-interact-mode') &&
          (ev.target === canvas || canvas.contains(ev.target))) {
        if (tlAnchors.length || tlRmbDrag) return true;
        var rect = canvas.getBoundingClientRect();
        var px = ev.clientX - rect.left;
        var py = ev.clientY - rect.top;
        var wid = wrap.clientWidth;
        if (hitTestTrendlineIds(px, py, wid) != null) return true;
      }
    }
    if (chartAllowsObjectSelection()) {
      var rectW = wrap.getBoundingClientRect();
      var pwx = ev.clientX - rectW.left;
      var pwy = ev.clientY - rectW.top;
      var wwid = wrap.clientWidth;
      if (hitTestTrendlineIds(pwx, pwy, wwid) != null) return true;
    }
    return false;
  }

  function resetSharedTimeScaleToDefault() {
    if (!mainChart) return;
    try {
      var ts = mainChart.timeScale();
      if (typeof ts.resetTimeScale === 'function') {
        ts.resetTimeScale();
      } else if (typeof ts.fitContent === 'function') {
        ts.fitContent();
      }
    } catch (_) {}
    rsiTimeScaleSyncKey = null;
    try {
      ensureRsiTimeScaleVisualMatchesMain();
    } catch (_) {}
    if (rsiChart && showRsi) {
      try {
        alignRsiTimeScaleToMain();
      } catch (_) {}
    }
  }

  function resetMainPanePriceScales() {
    if (!mainChart) return;
    try {
      mainChart.priceScale('right').applyOptions({ autoScale: true });
    } catch (_) {}
    try {
      mainChart.priceScale('vol').applyOptions({ autoScale: true });
    } catch (_) {}
  }

  function resetRsiPanePriceScale() {
    if (!rsiChart) return;
    try {
      rsiChart.priceScale('right').applyOptions({ autoScale: true });
    } catch (_) {}
  }

  function onMainChartPaneDoubleClick(ev) {
    if (!mainChart) return;
    if (typeof ev.button === 'number' && ev.button !== 0) return;
    if (shouldIgnoreMainPaneDoubleClick(ev)) return;
    ev.preventDefault();
    resetSharedTimeScaleToDefault();
    resetMainPanePriceScales();
    scheduleDrawTrendlines();
  }

  function onRsiPaneDoubleClick(ev) {
    if (!rsiChart || !showRsi) return;
    if (typeof ev.button === 'number' && ev.button !== 0) return;
    if (isDblClickOnChromeUi(ev.target)) return;
    ev.preventDefault();
    resetSharedTimeScaleToDefault();
    resetRsiPanePriceScale();
    scheduleDrawTrendlines();
  }

  function bindPaneDoubleClickZoomReset() {
    unbindPaneDoubleClickZoomReset();
    var mainWrap = document.getElementById('sc-main-wrap');
    var rsiWrap = document.getElementById('sc-rsi-wrap');
    if (mainWrap) {
      paneDblClickMainHandler = function (e) {
        onMainChartPaneDoubleClick(e);
      };
      mainWrap.addEventListener('dblclick', paneDblClickMainHandler, true);
    }
    if (rsiWrap) {
      paneDblClickRsiHandler = function (e) {
        onRsiPaneDoubleClick(e);
      };
      rsiWrap.addEventListener('dblclick', paneDblClickRsiHandler, true);
    }
  }

  function unbindPaneDoubleClickZoomReset() {
    var mainWrap = document.getElementById('sc-main-wrap');
    var rsiWrap = document.getElementById('sc-rsi-wrap');
    if (mainWrap && paneDblClickMainHandler) {
      mainWrap.removeEventListener('dblclick', paneDblClickMainHandler, true);
    }
    paneDblClickMainHandler = null;
    if (rsiWrap && paneDblClickRsiHandler) {
      rsiWrap.removeEventListener('dblclick', paneDblClickRsiHandler, true);
    }
    paneDblClickRsiHandler = null;
  }

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
    var tlP = document.getElementById('sc-tl-props-panel');
    var lvlE = document.getElementById('sc-lvl-edit-panel');
    var indE = document.getElementById('sc-ind-edit-panel');
    var addM = document.getElementById('sc-add-ctx-menu');
    if (except !== 'ind' && ind) ind.hidden = true;
    if (except !== 'lvl' && lvl) lvl.hidden = true;
    if (except !== 'tooltip' && tp) tp.hidden = true;
    if (except !== 'tlprops' && tlP) tlP.hidden = true;
    if (except !== 'lvlprops' && lvlE) lvlE.hidden = true;
    if (except !== 'indprops' && indE) indE.hidden = true;
    if (except !== 'addctx' && addM) addM.hidden = true;
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
      if (chartInteractionMode !== MODE_OBJECTS || !mainChart) return;
      var p = canvasPixelFromClient(ev.clientX, ev.clientY);
      if (!p) return;
      tlPreviewPx = p;
      scheduleDrawTrendlines();
    };
    window.addEventListener('mousemove', tlDraftMoveBound);
  }

  function updateExplicitButtonsState() {
    document.querySelectorAll('.sc-draw-btn').forEach(function (btn) {
      var drawType = (btn.getAttribute('data-draw-type') || '').trim();
      var isOn = false;
      if (drawType === 'LEVEL') {
        isOn = chartInteractionMode === MODE_OBJECTS && drawingLevel;
      } else if (drawType) {
        isOn = chartInteractionMode === MODE_OBJECTS && !drawingLevel &&
          getActiveDrawType() === normalizeDrawingType(drawType);
      }
      btn.classList.toggle('active', isOn);
      btn.setAttribute('aria-pressed', isOn ? 'true' : 'false');
    });
  }

  function applyChartInteractionMode() {
    var hiddenMode = LW && LW.CrosshairMode ? LW.CrosshairMode.Hidden : 2;
    var magnetMode = LW && LW.CrosshairMode ? LW.CrosshairMode.Magnet : 1;
    /** Main: Normal (0) — we snap Y to OHLC + MAs ourselves; LW magnet on candles favors close only. */
    var normalMode = LW && LW.CrosshairMode ? LW.CrosshairMode.Normal : 0;
    var mainCxMode = chartInteractionMode === MODE_CROSSHAIR ? normalMode : hiddenMode;
    var rsiCxMode = chartInteractionMode === MODE_CROSSHAIR ? magnetMode : hiddenMode;
    if (mainChart) {
      try { mainChart.applyOptions({ crosshair: { mode: mainCxMode } }); } catch (_) {}
    }
    if (rsiChart) {
      try { rsiChart.applyOptions({ crosshair: { mode: rsiCxMode } }); } catch (_) {}
    }
    bindCrosshairOhlcSnapDom();
    var canvas = getTlCanvas();
    if (canvas) {
      canvas.classList.remove('object-interact-mode', 'drawing-tl');
      if (chartInteractionMode === MODE_OBJECTS) {
        canvas.classList.add('object-interact-mode');
        if (tlAnchors.length >= 1 || drawingLevel || isAltDrawingModeActive()) canvas.classList.add('drawing-tl');
      }
    }
    var cxBtn = document.getElementById('sc-mode-crosshair');
    if (cxBtn) {
      var cxOn = chartInteractionMode === MODE_CROSSHAIR;
      cxBtn.classList.toggle('active', cxOn);
      cxBtn.setAttribute('aria-pressed', cxOn ? 'true' : 'false');
    }
    updateExplicitButtonsState();
  }

  function clearCrosshairBothCharts() {
    try { if (mainChart && typeof mainChart.clearCrosshairPosition === 'function') mainChart.clearCrosshairPosition(); } catch (_) {}
    try { if (rsiChart && typeof rsiChart.clearCrosshairPosition === 'function') rsiChart.clearCrosshairPosition(); } catch (_) {}
  }

  function unbindTlDragDocumentListeners() {
    if (!tlDragDocBound) return;
    tlDragDocBound = false;
    if (tlDragDocMove) {
      document.removeEventListener('pointermove', tlDragDocMove, true);
      tlDragDocMove = null;
    }
    if (tlDragDocEnd) {
      document.removeEventListener('pointerup', tlDragDocEnd, true);
      document.removeEventListener('pointercancel', tlDragDocEnd, true);
      tlDragDocEnd = null;
    }
  }

  function bindTlDragDocumentListeners() {
    if (tlDragDocBound) return;
    tlDragDocBound = true;
    tlDragDocMove = function (e) { onTlDragPointerMoveGlob(e); };
    tlDragDocEnd = function (e) { onTlDragPointerEndGlob(e); };
    document.addEventListener('pointermove', tlDragDocMove, true);
    document.addEventListener('pointerup', tlDragDocEnd, true);
    document.addEventListener('pointercancel', tlDragDocEnd, true);
  }

  function endTlRmbDrag() {
    var had = !!tlRmbDrag;
    var pid = tlRmbDrag && tlRmbDrag.pointerId;
    var capEl = (tlRmbDrag && tlRmbDrag.captureEl) || getTlCanvas();
    if (pid != null && capEl && typeof capEl.releasePointerCapture === 'function') {
      try { capEl.releasePointerCapture(pid); } catch (_) {}
    }
    unbindTlDragDocumentListeners();
    tlRmbDrag = null;
    if (had) scheduleSaveAnnotations();
  }

  function leaveObjectsExclusiveState() {
    endTlRmbDrag();
    if (levelDrag && levelDrag.wrapEl) {
      try {
        if (typeof levelDrag.wrapEl.releasePointerCapture === 'function') {
          levelDrag.wrapEl.releasePointerCapture(levelDrag.pointerId);
        }
      } catch (_) {}
    }
    levelDrag = null;
    unbindLevelDragDoc();
    tlAnchors = [];
    drawingLevel = false;
    setActiveDrawType('TRENDLINE');
    tlSuppressNextDrawClick = false;
    tlSuppressSelectClick = false;
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
    tlPreviewPx = null;
  }

  function clearObjectSelections() {
    selectedTlId = null;
    selectedLevelId = null;
    selectedIndId = null;
    syncIndicatorSelectionVisuals();
    syncLevelSelectionVisuals();
    syncTrendlinePropsPanel();
    syncLevelEditPanel();
    syncIndEditPanel();
    drawTrendlines();
    renderChips();
    closeAllPanels(null);
  }

  function setChartInteractionMode(desired) {
    var next = desired === chartInteractionMode ? MODE_NONE : desired;
    if (next === chartInteractionMode) return;
    var prev = chartInteractionMode;
    if (prev === MODE_OBJECTS && next !== MODE_OBJECTS) {
      leaveObjectsExclusiveState();
    }
    if (next === MODE_NONE) {
      clearObjectSelections();
    }
    if (next !== MODE_CROSSHAIR) {
      clearCrosshairBothCharts();
      crosshairOhlcSnapApplied = null;
    }
    chartInteractionMode = next;
    applyChartInteractionMode();
    drawTrendlines();
    renderChips();
  }

  function setSelectedTrendline(id) {
    if (id) {
      selectedTlId = id;
      selectedLevelId = null;
      selectedIndId = null;
    } else {
      selectedTlId = null;
    }
    syncIndicatorSelectionVisuals();
    syncLevelSelectionVisuals();
    drawTrendlines();
    renderChips();
  }

  function setSelectedLevel(id) {
    if (id) {
      selectedLevelId = id;
      selectedTlId = null;
      selectedIndId = null;
    } else {
      selectedLevelId = null;
    }
    syncIndicatorSelectionVisuals();
    syncLevelSelectionVisuals();
    drawTrendlines();
    renderChips();
  }

  function setSelectedInd(id) {
    if (id) {
      selectedIndId = id;
      selectedTlId = null;
      selectedLevelId = null;
    } else {
      selectedIndId = null;
    }
    syncIndicatorSelectionVisuals();
    syncLevelSelectionVisuals();
    drawTrendlines();
    renderChips();
  }

  function chartAllowsObjectSelection() {
    return chartInteractionMode === MODE_NONE ||
      chartInteractionMode === MODE_CROSSHAIR ||
      chartInteractionMode === MODE_OBJECTS;
  }

  function deleteSelectedTrendline() {
    if (!selectedTlId) return;
    endTlRmbDrag();
    var id = selectedTlId;
    trendlines = trendlines.filter(function (x) { return x.id !== id; });
    selectedTlId = null;
    scheduleSaveAnnotations();
    drawTrendlines();
    renderChips();
  }

  function findLevelById(id) {
    for (var i = 0; i < levels.length; i++) {
      if (levels[i].id === id) return levels[i];
    }
    return null;
  }

  function deleteSelectedLevel() {
    if (!selectedLevelId) return;
    var id = selectedLevelId;
    levels = levels.filter(function (x) { return x.id !== id; });
    if (levelPriceLines[id]) {
      try { candleSeries.removePriceLine(levelPriceLines[id]); } catch (_) {}
      delete levelPriceLines[id];
    }
    if (levelGlowPriceLines[id]) {
      try { candleSeries.removePriceLine(levelGlowPriceLines[id]); } catch (_) {}
      delete levelGlowPriceLines[id];
    }
    selectedLevelId = null;
    scheduleSaveAnnotations();
    renderChips();
  }

  function deleteSelectedInd() {
    if (!selectedIndId) return;
    var sid = selectedIndId;
    indicators.forEach(function (ind) {
      if (ind.id === sid) detachIndicatorSeries(ind);
    });
    indicators = indicators.filter(function (x) { return x.id !== sid; });
    selectedIndId = null;
    syncIndicatorSelectionVisuals();
    refreshIndicators();
    renderChips();
    updateIndAddButton();
    scheduleSaveAnnotations();
  }

  function unbindLevelDragDoc() {
    if (levelDragMoveDoc) {
      document.removeEventListener('pointermove', levelDragMoveDoc, true);
      levelDragMoveDoc = null;
    }
    if (levelDragEndDoc) {
      document.removeEventListener('pointerup', levelDragEndDoc, true);
      document.removeEventListener('pointercancel', levelDragEndDoc, true);
      levelDragEndDoc = null;
    }
  }

  function wrapPixelFromClient(clientX, clientY) {
    var wrap = document.getElementById('sc-main-wrap');
    if (!wrap) return null;
    var rect = wrap.getBoundingClientRect();
    return { px: clientX - rect.left, py: clientY - rect.top, wid: wrap.clientWidth, wrap: wrap };
  }

  function hitTestLevelId(px, py) {
    if (!candleSeries || !levels.length) return null;
    var best = null;
    var bestD = LEVEL_HIT_PX + 1;
    for (var i = 0; i < levels.length; i++) {
      var lv = levels[i];
      var y = candleSeries.priceToCoordinate(Number(lv.price));
      if (y == null || !isFinite(y)) continue;
      var d = Math.abs(py - y);
      if (d <= LEVEL_HIT_PX && d < bestD) {
        bestD = d;
        best = lv.id;
      }
    }
    return best;
  }

  function seriesValueAtTime(pts, t) {
    if (!pts || !pts.length || t == null) return null;
    var st = chartTimeToUnixSec(t);
    if (!isFinite(st)) return null;
    var i;
    for (i = 0; i < pts.length; i++) {
      var sti = chartTimeToUnixSec(pts[i].time);
      if (isFinite(sti) && sti >= st) break;
    }
    if (i >= pts.length) {
      var last = pts[pts.length - 1];
      return last && last.value != null ? Number(last.value) : null;
    }
    if (i === 0) {
      return pts[0].value != null ? Number(pts[0].value) : null;
    }
    var p0 = pts[i - 1];
    var p1 = pts[i];
    var s0 = chartTimeToUnixSec(p0.time);
    var s1 = chartTimeToUnixSec(p1.time);
    if (!isFinite(s0) || !isFinite(s1) || Math.abs(s1 - s0) < 1e-9) {
      return p1.value != null ? Number(p1.value) : null;
    }
    var alpha = (st - s0) / (s1 - s0);
    alpha = Math.max(0, Math.min(1, alpha));
    var v0 = Number(p0.value);
    var v1 = Number(p1.value);
    if (!isFinite(v0) || !isFinite(v1)) return isFinite(v1) ? v1 : v0;
    return v0 + (v1 - v0) * alpha;
  }

  function indicatorDrawZIndex(i) {
    var ind = indicators[i];
    var z = i;
    if (selectedIndId === ind.id && ind.visible !== false &&
        (ind.glowSeries || (ind.fibGlowSeriesArr && ind.fibGlowSeriesArr.length))) {
      z += indicators.length;
    }
    return z;
  }

  function indicatorPluginFor(ind) {
    if (!ind) return null;
    return indicatorRegistry.get(ind.type);
  }

  function indicatorBandKeys(ind) {
    var plugin = indicatorPluginFor(ind);
    if (plugin && Array.isArray(plugin.bandKeys) && plugin.bandKeys.length) return plugin.bandKeys;
    return null;
  }

  function isBandIndicator(ind) {
    var keys = indicatorBandKeys(ind);
    return !!(keys && keys.length);
  }

  function indicatorPrimaryPoints(plugin, data) {
    if (Array.isArray(data)) return data;
    if (plugin && typeof plugin.getPrimarySeriesData === 'function') {
      var pts = plugin.getPrimarySeriesData(data);
      if (Array.isArray(pts)) return pts;
    }
    if (data && Array.isArray(data.l1000)) return data.l1000;
    if (data && Array.isArray(data.upData)) return data.upData;
    return [];
  }

  function fibBandColorMap(ind) {
    return {
      l1000: ind.color || '#f59e0b',
      l786: '#94a3b8',
      l618: '#22c55e',
      l500: '#ef4444',
      l382: '#3b82f6',
      l236: '#a855f7',
      l0: ind.color || '#f59e0b'
    };
  }

  function indicatorHitPx(ind, selected) {
    var lw = Math.max(1, Math.min(8, Number(ind.lineWidth) || 2));
    var hitPx = IND_HIT_PX + lw / 2;
    if (selected) {
      if (isBandIndicator(ind)) hitPx += lw / 2 + 2;
      else if (ind.glowSeries) hitPx += Math.min(16, lw + 5) / 2;
    }
    return hitPx;
  }

  function hitTestBandIndicatorId(ind, px, py, t, i) {
    if (!ind.seriesArr || !ind.seriesArr.length || !ind.cachedPtsArr) return null;
    var keys = indicatorBandKeys(ind);
    if (!keys || !keys.length) return null;
    var bestD = IND_HIT_PX + 1;
    var hit = false;
    var selected = selectedIndId === ind.id;
    var hitPx = indicatorHitPx(ind, selected);
    for (var fi = 0; fi < keys.length; fi++) {
      var lvl = keys[fi];
      var pts = ind.cachedPtsArr[lvl];
      if (!pts || !pts.length) continue;
      var v = seriesValueAtTime(pts, t);
      if (v == null || !isFinite(v)) continue;
      var ser = ind.seriesArr[fi];
      if (!ser) continue;
      var y = ser.priceToCoordinate(v);
      if (y == null || !isFinite(y)) continue;
      var d = Math.abs(py - y);
      if (d <= hitPx && d < bestD) {
        bestD = d;
        hit = true;
      }
    }
    return hit ? { id: ind.id, d: bestD, z: indicatorDrawZIndex(i) } : null;
  }

  function hitTestIndicatorId(px, py) {
    if (!mainChart || !candleSeries) return null;
    var t = coordinateToTimeExtrapolated(px);
    if (t == null) return null;
    var candidates = [];
    for (var i = 0; i < indicators.length; i++) {
      var ind = indicators[i];
      if (ind.visible === false) continue;
      if (isBandIndicator(ind)) {
        var bandHit = hitTestBandIndicatorId(ind, px, py, t, i);
        if (bandHit) candidates.push(bandHit);
        continue;
      }
      if (!ind.series || !ind.cachedPts || !ind.cachedPts.length) continue;
      var v = seriesValueAtTime(ind.cachedPts, t);
      if (v == null || !isFinite(v)) continue;
      var y = ind.series.priceToCoordinate(v);
      if (y == null || !isFinite(y)) continue;
      var d = Math.abs(py - y);
      var hitPx = indicatorHitPx(ind, selectedIndId === ind.id);
      if (d <= hitPx) {
        candidates.push({ id: ind.id, d: d, z: indicatorDrawZIndex(i) });
      }
    }
    if (!candidates.length) return null;
    candidates.sort(function (a, b) {
      if (Math.abs(a.d - b.d) > IND_HIT_TIE_PX) return a.d - b.d;
      return b.z - a.z;
    });
    return candidates[0].id;
  }

  function levelPriceFreeExcept(price, exceptId) {
    var k = levelPriceKey(price);
    for (var i = 0; i < levels.length; i++) {
      if (levels[i].id === exceptId) continue;
      if (levelPriceKey(levels[i].price) === k) return false;
    }
    return true;
  }

  function applyLevelPriceLineVisual(lv) {
    if (!candleSeries || !lv) return;
    var id = lv.id;
    var pl = levelPriceLines[id];
    if (!pl) return;
    var baseLw = Math.max(1, Number(lv.width) || 1);
    var selected = id === selectedLevelId && chartAllowsObjectSelection();
    var opts = {
      price: Number(lv.price),
      color: lv.color || '#22c55e',
      lineWidth: selected ? baseLw + 2 : baseLw,
      lineStyle: styleToLw(lv.style || 'dashed'),
      axisLabelVisible: true,
      title: (lv.label || '').slice(0, 24)
    };
    try {
      pl.applyOptions(opts);
    } catch (_) {
      try { candleSeries.removePriceLine(pl); } catch (__) {}
      delete levelPriceLines[id];
      try {
        levelPriceLines[id] = candleSeries.createPriceLine(opts);
      } catch (___) {}
    }
  }

  function tryBeginTrendlineDragFromPointerDown(ev, px, py, wid, captureEl) {
    if (!chartAllowsObjectSelection() || !mainChart || !candleSeries || tlAnchors.length || drawingLevel) return false;
    if (tlRmbDrag) return false;
    if (ev.button !== 0 && ev.button !== 2) return false;
    var tlHit = hitTestTrendlineIds(px, py, wid);
    if (!tlHit) return false;
    setSelectedTrendline(tlHit);
    var tlDrag = findTrendlineById(tlHit);
    if (!tlDrag) return false;
    var whichEnd = hitTestTrendlineHandle(px, py, tlDrag);
    if (whichEnd != null) {
      ev.preventDefault();
      ev.stopPropagation();
      var fixedOther = null;
      if (whichEnd === 1) {
        fixedOther = { time: tlDrag.time2, price: tlDrag.price2 };
      } else if (whichEnd === 2) {
        fixedOther = { time: tlDrag.time1, price: tlDrag.price1 };
      }
      tlRmbDrag = {
        mode: 'endpoint',
        whichEnd: whichEnd,
        fixedOther: fixedOther,
        tlId: tlHit,
        pointerId: ev.pointerId,
        button: ev.button,
        didMove: false,
        downPx: px,
        downPy: py,
        captureEl: captureEl || null
      };
      try {
        if (captureEl && typeof captureEl.setPointerCapture === 'function') {
          captureEl.setPointerCapture(ev.pointerId);
        }
      } catch (_) {}
      bindTlDragDocumentListeners();
      onTlRmbDragMove(ev);
      return true;
    }
    if (hitTestTrendlineParallelBody(px, py, tlDrag, wid)) {
      var ep = getTrendlinePixelEndpoints(tlDrag, wid);
      if (!ep) return false;
      ev.preventDefault();
      ev.stopPropagation();
      
      var ep0 = { x1: ep.x1, y1: ep.y1, x2: ep.x2, y2: ep.y2 };
      var grab = projectPointToSegment(px, py, ep.x1, ep.y1, ep.x2, ep.y2);
      if (ep.x3 != null && ep.y3 != null) {
        ep0.x3 = ep.x3;
        ep0.y3 = ep.y3;
        var dx = ep.x2 - ep.x1;
        var dy = ep.y2 - ep.y1;
        var x4 = ep.x3 + dx;
        var y4 = ep.y3 + dy;
        var dLower = distPointToSegment(px, py, ep.x1, ep.y1, ep.x2, ep.y2);
        var dUpper = distPointToSegment(px, py, ep.x3, ep.y3, x4, y4);
        if (dUpper < dLower) {
          grab = projectPointToSegment(px, py, ep.x3, ep.y3, x4, y4);
        }
      }
      
      tlRmbDrag = {
        mode: 'move',
        tlId: tlHit,
        ep0: ep0,
        gx: grab.x,
        gy: grab.y,
        pointerId: ev.pointerId,
        button: ev.button,
        didMove: false,
        downPx: px,
        downPy: py,
        captureEl: captureEl || null
      };
      try {
        if (captureEl && typeof captureEl.setPointerCapture === 'function') {
          captureEl.setPointerCapture(ev.pointerId);
        }
      } catch (_) {}
      bindTlDragDocumentListeners();
      return true;
    }
    ev.preventDefault();
    ev.stopPropagation();
    return true;
  }

  function onLevelDragPointerMove(ev) {
    if (!levelDrag || ev.pointerId !== levelDrag.pointerId || !candleSeries) return;
    var wp = wrapPixelFromClient(ev.clientX, ev.clientY);
    if (!wp) return;
    var price = coordinateToPriceExtrapolated(wp.py);
    if (price == null || !isFinite(price)) return;
    if (!levelPriceFreeExcept(price, levelDrag.id)) return;
    var lv = findLevelById(levelDrag.id);
    if (!lv) return;
    lv.price = price;
    applyLevelPriceLineVisual(lv);
    syncLevelSelectionVisuals();
    scheduleDrawTrendlines();
  }

  function onLevelDragPointerEnd(ev) {
    if (!levelDrag || ev.pointerId !== levelDrag.pointerId) return;
    try {
      if (levelDrag.wrapEl && typeof levelDrag.wrapEl.releasePointerCapture === 'function') {
        levelDrag.wrapEl.releasePointerCapture(ev.pointerId);
      }
    } catch (_) {}
    levelDrag = null;
    unbindLevelDragDoc();
    scheduleSaveAnnotations();
    scheduleDrawTrendlines();
  }

  function bindLevelDragDocumentListeners() {
    if (levelDragMoveDoc) return;
    levelDragMoveDoc = function (e) { onLevelDragPointerMove(e); };
    levelDragEndDoc = function (e) { onLevelDragPointerEnd(e); };
    document.addEventListener('pointermove', levelDragMoveDoc, true);
    document.addEventListener('pointerup', levelDragEndDoc, true);
    document.addEventListener('pointercancel', levelDragEndDoc, true);
  }

  function mainWrapPointerDownCapture(ev) {
    if (!mainChart || !candleSeries) return;
    var wrap = document.getElementById('sc-main-wrap');
    if (!wrap || !wrap.contains(ev.target)) return;
    if (ev.target.closest && ev.target.closest('.sc-panel')) return;
    var wp = wrapPixelFromClient(ev.clientX, ev.clientY);
    if (!wp) return;
    var px = wp.px;
    var py = wp.py;
    var wid = wp.wid;

    if (chartAllowsObjectSelection() && ev.button === 0 && !tlRmbDrag && tlAnchors.length === 0 && !drawingLevel && !isAltDrawingModeActive()) {
      var lid = hitTestLevelId(px, py);
      if (lid) {
        var hitTl = hitTestTrendlineIds(px, py, wid);
        if (!hitTl) {
          setSelectedLevel(lid);
          ev.preventDefault();
          ev.stopPropagation();
          levelDrag = { id: lid, pointerId: ev.pointerId, wrapEl: wrap };
          try {
            if (typeof wrap.setPointerCapture === 'function') wrap.setPointerCapture(ev.pointerId);
          } catch (_) {}
          bindLevelDragDocumentListeners();
          return;
        }
      }
    }

    if (chartAllowsObjectSelection()) {
      if (tryBeginTrendlineDragFromPointerDown(ev, px, py, wid, wrap)) return;
    }

    if (chartAllowsObjectSelection() && ev.button === 0 && !tlRmbDrag && tlAnchors.length === 0 && !drawingLevel && !isAltDrawingModeActive()) {
      var iid = hitTestIndicatorId(px, py);
      if (iid) {
        setSelectedInd(iid === selectedIndId ? null : iid);
        ev.preventDefault();
        ev.stopPropagation();
        return;
      }
    }
  }

  function handleMainWrapClickCapture(ev) {
    if (!mainChart || !candleSeries || !chartAllowsObjectSelection()) return;
    if (ev.detail !== 1) return;
    var wrap = document.getElementById('sc-main-wrap');
    if (!wrap || !wrap.contains(ev.target)) return;
    if (ev.target.closest && ev.target.closest('.sc-panel')) return;
    if (tlAnchors.length === 1 && chartInteractionMode === MODE_OBJECTS) return;
    if (drawingLevel && chartInteractionMode === MODE_OBJECTS) return;
    if (isAltDrawingModeActive() && chartInteractionMode === MODE_OBJECTS) return;
    if (tlSuppressSelectClick) {
      tlSuppressSelectClick = false;
      return;
    }
    var wp = wrapPixelFromClient(ev.clientX, ev.clientY);
    if (!wp) return;
    var px = wp.px;
    var py = wp.py;
    var wid = wp.wid;
    if (hitTestTrendlineIds(px, py, wid)) return;
    if (hitTestLevelId(px, py)) return;
    if (hitTestIndicatorId(px, py)) return;
    clearObjectSelections();
  }

  function positionPanelAtClient(panel, clientX, clientY) {
    if (!panel) return;
    var pad = 8;
    panel.style.left = Math.min(window.innerWidth - panel.offsetWidth - pad, Math.max(pad, clientX)) + 'px';
    panel.style.top = Math.min(window.innerHeight - panel.offsetHeight - pad, Math.max(pad, clientY)) + 'px';
  }

  function syncTrendlinePropsPanel() {
    var p = document.getElementById('sc-tl-props-panel');
    if (!p) return;
    var tl = selectedTlId ? findTrendlineById(selectedTlId) : null;
    var color = document.getElementById('sc-tl-prop-color');
    var width = document.getElementById('sc-tl-prop-width');
    var style = document.getElementById('sc-tl-prop-style');
    var ext = document.getElementById('sc-tl-prop-ext');
    var lab = document.getElementById('sc-tl-prop-label');
    if (!tl) {
      p.hidden = true;
      return;
    }
    if (color) color.value = tl.color || '#f59e0b';
    if (width) width.value = String(Math.max(1, Number(tl.width) || 1));
    if (style) style.value = tl.style === 'dashed' ? 'dashed' : (tl.style === 'dotted' ? 'dotted' : 'solid');
    if (ext) ext.checked = !!tl.extended;
    if (lab) lab.value = tl.label || '';
  }

  function syncLevelEditPanel() {
    var p = document.getElementById('sc-lvl-edit-panel');
    if (!p) return;
    var lv = selectedLevelId ? findLevelById(selectedLevelId) : null;
    var price = document.getElementById('sc-lvl-edit-price');
    var lab = document.getElementById('sc-lvl-edit-label');
    var color = document.getElementById('sc-lvl-edit-color');
    var style = document.getElementById('sc-lvl-edit-style');
    var width = document.getElementById('sc-lvl-edit-width');
    if (!lv) {
      p.hidden = true;
      return;
    }
    if (price) price.value = String(lv.price);
    if (lab) lab.value = lv.label || '';
    if (color) color.value = lv.color || '#22c55e';
    if (style) style.value = lv.style || 'dashed';
    if (width) width.value = String(Math.max(1, Number(lv.width) || 1));
  }

  function syncIndEditPanel() {
    var p = document.getElementById('sc-ind-edit-panel');
    if (!p) return;
    var ind = null;
    if (selectedIndId) {
      for (var i = 0; i < indicators.length; i++) {
        if (indicators[i].id === selectedIndId) { ind = indicators[i]; break; }
      }
    }
    var typ = document.getElementById('sc-ind-edit-type');
    var per = document.getElementById('sc-ind-edit-period');
    var color = document.getElementById('sc-ind-edit-color');
    var width = document.getElementById('sc-ind-edit-width');
    var style = document.getElementById('sc-ind-edit-style');
    if (!ind) {
      p.hidden = true;
      return;
    }
    if (typ) typ.textContent = ind.type + ' ' + ind.period;
    if (per) {
      per.value = String(ind.period);
      var perRow = per.closest('.sc-panel-row');
      var perLbl = perRow ? perRow.querySelector('label') : null;
      if (perLbl) {
        var plugin = indicatorRegistry.get(ind.type);
        perLbl.textContent = (plugin && plugin.periodLabel) ? plugin.periodLabel : 'Period';
      }
    }
    if (color) color.value = ind.color || '#f59e0b';
    if (width) width.value = String(Math.max(1, Number(ind.lineWidth) || 2));
    if (style) style.value = ind.style || 'solid';
  }

  function buildObjectPropertyPanels() {
    var page = document.querySelector('.sc-page');
    if (!page || document.getElementById('sc-tl-props-panel')) return;

    function mkPanel(id, title) {
      var el = document.createElement('div');
      el.id = id;
      el.className = 'sc-panel';
      el.hidden = true;
      el.setAttribute('role', 'dialog');
      el.setAttribute('aria-modal', 'true');
      var h = document.createElement('h3');
      h.textContent = title;
      el.appendChild(h);
      page.appendChild(el);
      return el;
    }

    var tlP = mkPanel('sc-tl-props-panel', 'Trendline');
    tlP.innerHTML = '<h3 id="sc-tl-props-title">Trendline</h3>' +
      '<div class="sc-panel-row"><label>Color</label><input type="color" id="sc-tl-prop-color"></div>' +
      '<div class="sc-panel-row"><label>Width</label><input type="number" id="sc-tl-prop-width" min="1" max="6" step="1"></div>' +
      '<div class="sc-panel-row"><label>Style</label><select id="sc-tl-prop-style"><option value="solid">Solid</option><option value="dashed">Dashed</option><option value="dotted">Dotted</option></select></div>' +
      '<div class="sc-panel-row"><label><input type="checkbox" id="sc-tl-prop-ext"> Extended</label></div>' +
      '<div class="sc-panel-row"><label>Label</label><input type="text" id="sc-tl-prop-label" maxlength="48"></div>' +
      '<div class="sc-panel-btns"><button type="button" class="sc-btn-primary" id="sc-tl-prop-apply">Apply</button>' +
      '<button type="button" class="sc-btn-secondary" id="sc-tl-prop-close">Close</button></div>';
    tlP.querySelector('#sc-tl-prop-apply').addEventListener('click', function () {
      var tl = selectedTlId ? findTrendlineById(selectedTlId) : null;
      if (!tl) return;
      tl.color = (document.getElementById('sc-tl-prop-color') || {}).value || tl.color;
      tl.width = Math.max(1, parseInt(document.getElementById('sc-tl-prop-width').value, 10) || 1);
      tl.style = (document.getElementById('sc-tl-prop-style') || {}).value || 'solid';
      tl.extended = !!(document.getElementById('sc-tl-prop-ext') || {}).checked;
      tl.label = (document.getElementById('sc-tl-prop-label') || {}).value || '';
      scheduleSaveAnnotations();
      drawTrendlines();
      renderChips();
      tlP.hidden = true;
    });
    tlP.querySelector('#sc-tl-prop-close').addEventListener('click', function () {
      tlP.hidden = true;
    });

    var lvP = mkPanel('sc-lvl-edit-panel', 'Edit level');
    lvP.innerHTML = '<h3 id="sc-lvl-edit-title">Horizontal level</h3>' +
      '<div class="sc-panel-row"><label>Price</label><input type="number" id="sc-lvl-edit-price" step="any"></div>' +
      '<div class="sc-panel-row"><label>Label</label><input type="text" id="sc-lvl-edit-label" maxlength="48"></div>' +
      '<div class="sc-panel-row"><label>Color</label><input type="color" id="sc-lvl-edit-color"></div>' +
      '<div class="sc-panel-row"><label>Style</label><select id="sc-lvl-edit-style"><option value="solid">Solid</option><option value="dashed">Dashed</option><option value="dotted">Dotted</option></select></div>' +
      '<div class="sc-panel-row"><label>Width</label><input type="number" id="sc-lvl-edit-width" min="1" max="6" step="1"></div>' +
      '<div class="sc-panel-btns"><button type="button" class="sc-btn-primary" id="sc-lvl-edit-apply">Apply</button>' +
      '<button type="button" class="sc-btn-secondary" id="sc-lvl-edit-close">Close</button></div>';
    lvP.querySelector('#sc-lvl-edit-apply').addEventListener('click', function () {
      var lv = selectedLevelId ? findLevelById(selectedLevelId) : null;
      if (!lv) return;
      var raw = (document.getElementById('sc-lvl-edit-price') || {}).value;
      var price = parseFloat(String(raw).replace(/,/g, ''));
      if (!isFinite(price)) return;
      if (!levelPriceFreeExcept(price, lv.id)) return;
      lv.price = price;
      lv.label = (document.getElementById('sc-lvl-edit-label') || {}).value || '';
      lv.color = (document.getElementById('sc-lvl-edit-color') || {}).value || lv.color;
      lv.style = (document.getElementById('sc-lvl-edit-style') || {}).value || 'dashed';
      lv.width = Math.max(1, parseInt(document.getElementById('sc-lvl-edit-width').value, 10) || 1);
      applyLevelPriceLineVisual(lv);
      syncLevelSelectionVisuals();
      scheduleSaveAnnotations();
      renderChips();
      lvP.hidden = true;
    });
    lvP.querySelector('#sc-lvl-edit-close').addEventListener('click', function () {
      lvP.hidden = true;
    });

    var inP = mkPanel('sc-ind-edit-panel', 'Edit indicator');
    inP.innerHTML = '<h3 id="sc-ind-edit-title">Indicator</h3>' +
      '<div class="sc-panel-row"><label>Type</label><span id="sc-ind-edit-type"></span></div>' +
      '<div class="sc-panel-row"><label>Period</label><input type="number" id="sc-ind-edit-period" min="2" max="200"></div>' +
      '<div class="sc-panel-row"><label>Color</label><input type="color" id="sc-ind-edit-color"></div>' +
      '<div class="sc-panel-row"><label>Width</label><input type="number" id="sc-ind-edit-width" min="1" max="6" step="1"></div>' +
      '<div class="sc-panel-row"><label>Style</label><select id="sc-ind-edit-style"><option value="solid">Solid</option><option value="dashed">Dashed</option><option value="dotted">Dotted</option></select></div>' +
      '<div class="sc-panel-btns"><button type="button" class="sc-btn-primary" id="sc-ind-edit-apply">Apply</button>' +
      '<button type="button" class="sc-btn-secondary" id="sc-ind-edit-close">Close</button></div>';
    inP.querySelector('#sc-ind-edit-apply').addEventListener('click', function () {
      var ind = null;
      if (selectedIndId) {
        for (var j = 0; j < indicators.length; j++) {
          if (indicators[j].id === selectedIndId) { ind = indicators[j]; break; }
        }
      }
      if (!ind) return;
      var per = Math.max(2, Math.min(200, parseInt(document.getElementById('sc-ind-edit-period').value, 10) || ind.period));
      ind.period = per;
      ind.color = (document.getElementById('sc-ind-edit-color') || {}).value || ind.color;
      ind.lineWidth = Math.max(1, parseInt(document.getElementById('sc-ind-edit-width').value, 10) || 2);
      ind.style = (document.getElementById('sc-ind-edit-style') || {}).value || 'solid';
      if (ind.series) {
        try {
          ind.series.applyOptions({
            color: ind.color,
            lineWidth: ind.lineWidth,
            lineStyle: styleToLw(ind.style)
          });
        } catch (_) {}
      }
      refreshIndicators();
      renderChips();
      scheduleSaveAnnotations();
      inP.hidden = true;
    });
    inP.querySelector('#sc-ind-edit-close').addEventListener('click', function () {
      inP.hidden = true;
    });
  }

  function addIndicatorFromContext(type, overrides) {
    if (indicators.length >= MAX_MAIN_INDICATORS) return;
    var t = normalizeIndicatorType(type);
    var plugin = indicatorRegistry.get(t);
    if (!plugin || plugin.userAddable === false || plugin.isOscillator) return;
    var IND_COLORS = [
      '#f59e0b', '#60a5fa', '#34d399', '#f472b6', '#c084fc',
      '#fb923c', '#2dd4bf', '#fbbf24', '#a78bfa', '#38bdf8'
    ];
    var defaults = getIndicatorPluginDefaults(t);
    var color = defaults.color || IND_COLORS[indicators.length % IND_COLORS.length];
    indicators.push(createIndicatorState(Object.assign({}, defaults, {
      type: t,
      color: color
    }, overrides || {})));
    refreshIndicators();
    renderChips();
    updateIndAddButton();
    scheduleSaveAnnotations();
  }

  function openLevelAddPanelAt(clientX, clientY, priceHint) {
    var lvlPanel = document.getElementById('sc-lvl-panel');
    if (!lvlPanel) return;
    closeAllPanels('lvl');
    setLvlError('');
    lvlPanel.hidden = false;
    positionPanelAtClient(lvlPanel, clientX, clientY);
    var inp = document.getElementById('sc-lvl-price');
    if (inp) {
      if (priceHint != null && isFinite(priceHint)) inp.value = String(Number(priceHint).toFixed(2));
      setTimeout(function () { try { inp.focus(); } catch (_) {} }, 0);
    }
  }

  function showMainContextMenu(clientX, clientY, priceHint, includeAddOptions) {
    var menu = document.getElementById('sc-add-ctx-menu');
    if (!menu) return;
    lastMainCtxClientX = clientX;
    lastMainCtxClientY = clientY;
    lastMainCtxPriceHint = isFinite(priceHint) ? Number(priceHint) : null;
    var addSec = document.getElementById('sc-add-ctx-menu-add-sec');
    var addSep = document.getElementById('sc-add-ctx-menu-add-sep');
    var showAdd = !!includeAddOptions;
    if (addSec) addSec.hidden = !showAdd;
    if (addSep) addSep.hidden = !showAdd;
    closeAllPanels('addctx');
    menu.hidden = false;
    positionPanelAtClient(menu, clientX, clientY);
  }

  function getMainChartScreenshotCanvas() {
    if (mainChart && typeof mainChart.takeScreenshot === 'function') {
      try { return mainChart.takeScreenshot(); } catch (_) {}
    }
    var host = document.getElementById('sc-main-chart');
    if (!host) return null;
    var c = host.querySelector('canvas');
    return c || null;
  }

  function saveChartImageFromContext() {
    var c = getMainChartScreenshotCanvas();
    if (!c) return;
    var filename = 'stock-chart-' + (TOKEN || 'snapshot') + '.png';
    try {
      c.toBlob(function (blob) {
        if (!blob) return;
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(function () { URL.revokeObjectURL(url); }, 0);
      }, 'image/png');
      return;
    } catch (_) {}
    try {
      var a2 = document.createElement('a');
      a2.href = c.toDataURL('image/png');
      a2.download = filename;
      document.body.appendChild(a2);
      a2.click();
      a2.remove();
    } catch (_) {}
  }

  function copyChartImageFromContext() {
    var c = getMainChartScreenshotCanvas();
    if (!c) return;
    if (!navigator.clipboard || typeof navigator.clipboard.write !== 'function' || typeof ClipboardItem === 'undefined') {
      try { navigator.clipboard && navigator.clipboard.writeText && navigator.clipboard.writeText(location.href); } catch (_) {}
      return;
    }
    try {
      c.toBlob(function (blob) {
        if (!blob) return;
        try {
          navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
        } catch (_) {}
      }, 'image/png');
    } catch (_) {}
  }

  function inspectChartFromContext() {
    showStatus('Inspect: press F12 or Ctrl+Shift+I.');
    setTimeout(function () { hideStatus(); }, 1600);
  }

  function buildDefaultAddContextMenu() {
    var page = document.querySelector('.sc-page');
    if (!page || document.getElementById('sc-add-ctx-menu')) return;
    var menu = document.createElement('div');
    menu.id = 'sc-add-ctx-menu';
    menu.className = 'sc-panel';
    menu.hidden = true;
    menu.style.minWidth = '180px';
    menu.setAttribute('role', 'menu');
    menu.setAttribute('aria-label', 'Add chart object');

    function addMenuBtn(parent, label, handler) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'sc-btn-secondary';
      b.style.width = '100%';
      b.style.textAlign = 'left';
      b.style.marginBottom = '6px';
      b.textContent = label;
      b.addEventListener('click', function (ev) {
        ev.stopPropagation();
        menu.hidden = true;
        handler();
      });
      parent.appendChild(b);
    }

    function addMenuSep() {
      var sep = document.createElement('div');
      sep.id = 'sc-add-ctx-menu-add-sep';
      sep.style.height = '1px';
      sep.style.margin = '4px 0 8px';
      sep.style.background = 'var(--border)';
      menu.appendChild(sep);
    }

    var addSec = document.createElement('div');
    addSec.id = 'sc-add-ctx-menu-add-sec';
    menu.appendChild(addSec);

    addMenuBtn(addSec, 'Add Level', function () {
      openLevelAddPanelAt(lastMainCtxClientX, lastMainCtxClientY, lastMainCtxPriceHint);
    });

    getAddableDrawingPlugins().forEach(function (plugin) {
      addMenuBtn(addSec, 'Add ' + drawingAddLabel(plugin), function () {
        closeAllPanels(null);
        beginObjectDrawing(plugin.id);
      });
    });

    getAddableIndicatorPlugins().forEach(function (plugin) {
      addMenuBtn(addSec, 'Add ' + indicatorAddLabel(plugin), function () {
        addIndicatorFromContext(plugin.id);
      });
    });
    addMenuSep();
    addMenuBtn(menu, 'Save Image', function () {
      saveChartImageFromContext();
    });
    addMenuBtn(menu, 'Copy', function () {
      copyChartImageFromContext();
    });
    addMenuBtn(menu, 'Inspect (F12)', function () {
      inspectChartFromContext();
    });

    page.appendChild(menu);
  }

  function openObjectContextPanels(ev) {
    if (!chartAllowsObjectSelection() || !mainChart || !candleSeries) return;
    var wrap = document.getElementById('sc-main-wrap');
    if (!wrap || !wrap.contains(ev.target)) return;
    if (ev.target.closest && ev.target.closest('.sc-panel')) return;
    var wp = wrapPixelFromClient(ev.clientX, ev.clientY);
    if (!wp) return;
    var px = wp.px;
    var py = wp.py;
    var wid = wp.wid;
    var tlHit = hitTestTrendlineIds(px, py, wid);
    if (tlHit) {
      setSelectedTrendline(tlHit);
      syncTrendlinePropsPanel();
      var p = document.getElementById('sc-tl-props-panel');
      if (p) {
        closeAllPanels('tlprops');
        p.hidden = false;
        positionPanelAtClient(p, ev.clientX, ev.clientY);
      }
      ev.preventDefault();
      return;
    }
    var lvlHit = hitTestLevelId(px, py);
    if (lvlHit) {
      setSelectedLevel(lvlHit);
      syncLevelEditPanel();
      var pl = document.getElementById('sc-lvl-edit-panel');
      if (pl) {
        closeAllPanels('lvlprops');
        pl.hidden = false;
        positionPanelAtClient(pl, ev.clientX, ev.clientY);
      }
      ev.preventDefault();
      return;
    }
    var indHit = hitTestIndicatorId(px, py);
    if (indHit) {
      setSelectedInd(indHit);
      syncIndEditPanel();
      var pi = document.getElementById('sc-ind-edit-panel');
      if (pi) {
        closeAllPanels('indprops');
        pi.hidden = false;
        positionPanelAtClient(pi, ev.clientX, ev.clientY);
      }
      ev.preventDefault();
      return;
    }

    if (chartInteractionMode === MODE_NONE || chartInteractionMode === MODE_CROSSHAIR) {
      var priceHint = coordinateToPriceExtrapolated(py);
      showMainContextMenu(ev.clientX, ev.clientY, priceHint, chartInteractionMode === MODE_NONE);
      ev.preventDefault();
    }
  }

  function bindMainWrapOverlayInteraction() {
    var wrap = document.getElementById('sc-main-wrap');
    if (!wrap || mainWrapOverlayBound) return;
    mainWrapOverlayBound = true;
    mainWrapPdCapture = function (e) { mainWrapPointerDownCapture(e); };
    mainWrapClickCaptureHandler = function (e) { handleMainWrapClickCapture(e); };
    mainWrapCtxMenu = function (e) { openObjectContextPanels(e); };
    wrap.addEventListener('pointerdown', mainWrapPdCapture, true);
    wrap.addEventListener('click', mainWrapClickCaptureHandler, true);
    wrap.addEventListener('contextmenu', mainWrapCtxMenu, true);
  }

  function unbindMainWrapOverlayInteraction() {
    var wrap = document.getElementById('sc-main-wrap');
    if (!wrap || !mainWrapOverlayBound) return;
    mainWrapOverlayBound = false;
    if (mainWrapPdCapture) wrap.removeEventListener('pointerdown', mainWrapPdCapture, true);
    if (mainWrapClickCaptureHandler) wrap.removeEventListener('click', mainWrapClickCaptureHandler, true);
    if (mainWrapCtxMenu) wrap.removeEventListener('contextmenu', mainWrapCtxMenu, true);
    mainWrapPdCapture = null;
    mainWrapClickCaptureHandler = null;
    mainWrapCtxMenu = null;
    unbindLevelDragDoc();
    levelDrag = null;
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
   * Keep RSI pane in lockstep with the main chart:
   * 1) Match the candle `right` price-scale gutter width to the RSI `right` gutter
   *    (same minimumWidth on both so OHLC vs RSI labels align vertically).
   * 2) Nudge RSI chart pixel width until timeScale().width() matches main (capped),
   *    since main has an extra volume price strip and the time-plot widths differ otherwise.
   */
  function alignRsiPaneLayout() {
    if (!mainChart || !rsiChart || !showRsi) return;
    var mts = mainChart.timeScale();
    var rts = rsiChart.timeScale();
    if (typeof mts.width !== 'function' || typeof rts.width !== 'function') return;
    var rsiWrap = document.getElementById('sc-rsi-wrap');
    var rsiEl = document.getElementById('sc-rsi-chart');
    var baseW = Math.max(rsiWrap ? rsiWrap.clientWidth : rsiEl.offsetWidth, rsiEl.offsetWidth, 1);
    var maxExtra = 100;
    var capW = baseW + maxExtra;

    for (var i = 0; i < 8; i++) {
      var mps = mainChart.priceScale('right');
      var rps = rsiChart.priceScale('right');
      if (typeof mps.width === 'function' && typeof rps.width === 'function') {
        var wm = mps.width();
        var wr = rps.width();
        if (wm != null && wr != null && isFinite(wm) && isFinite(wr) && wm > 0 && wr > 0) {
          var gTarget = Math.ceil(Math.max(wm, wr));
          try {
            mps.applyOptions({ minimumWidth: gTarget });
            rps.applyOptions({ minimumWidth: gTarget });
          } catch (_) {}
        }
      }

      var twM = mts.width();
      var twR = rts.width();
      if (twM == null || twR == null || !isFinite(twM) || !isFinite(twR) || twM <= 0 || twR <= 0) return;

      mps = mainChart.priceScale('right');
      rps = rsiChart.priceScale('right');
      var wmA = typeof mps.width === 'function' ? mps.width() : null;
      var wrA = typeof rps.width === 'function' ? rps.width() : null;
      var timeOk = Math.abs(twM - twR) < 0.75;
      var gutterOk = wmA != null && wrA != null && isFinite(wmA) && isFinite(wrA) && Math.abs(wmA - wrA) < 1;
      if (timeOk && gutterOk) return;

      var d = twM - twR;
      var ow;
      try {
        ow = rsiChart.options().width;
      } catch (_) {
        ow = baseW;
      }
      if (ow == null || !isFinite(ow)) ow = baseW;
      var raw = Math.round(ow + d);
      var nw = Math.min(capW, Math.max(baseW, raw));
      if (nw !== ow) {
        rsiChart.applyOptions({ width: nw });
      } else if (timeOk) {
        return;
      }
    }
  }

  /**
   * Apply measured width/height to Lightweight Charts panes. The library does not
   * infer height from CSS alone; ResizeObserver previously updated width only,
   * which left the RSI pane at 0 height after the chart area became visible.
   * Do not call alignRsiTimeScaleToMain here — that fights user zoom/pan; range
   * sync is handled by main ↔ RSI subscribeVisibleLogicalRangeChange.
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
      try {
        mainChart.priceScale('right').applyOptions({ minimumWidth: 0 });
      } catch (_) {}
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

    alignRsiPaneLayout();
    requestAnimationFrame(function () {
      alignRsiPaneLayout();
      requestAnimationFrame(alignRsiPaneLayout);
    });

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
    unbindMainWrapOverlayInteraction();
    unbindMainChartTrendlineDomSync();
    unbindPaneDoubleClickZoomReset();
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
    unbindCrosshairOhlcSnapDom();
    crosshairOhlcSnapApplied = null;
    rsiTimeScaleSyncKey = null;
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
    indicators.forEach(function (ind) { ind.series = null; ind.glowSeries = null; });
    levelPriceLines = {};
    levelGlowPriceLines = {};
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
      wickDownColor: '#ef4444',
      priceLineVisible: false,
      lastValueVisible: false,
      /** Extra top margin so price lines at the bar high sit inside the pane (not clipped). */
      scaleMargins: { top: 0.14, bottom: 0.28 }
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

    // Sync main ↔ RSI by *visible logical range*, 1:1 (no offset).
    // RSI is padded with whitespace prefix in calcRSI, so both series have the
    // same logical bar count and identical times. Mirroring with no offset
    // means Lightweight Charts has nothing to clamp on the RSI side, which
    // previously destabilized the layout and let the live candle drift out of
    // view after enough ticks/resizes. `paneRangeSyncing` prevents ping-pong
    // when programmatically applying the same range on the peer chart.
    mainChart.timeScale().subscribeVisibleLogicalRangeChange(function (r) {
      scheduleDrawTrendlines();
      if (paneRangeSyncing || !rsiChart || !r || r.from == null || r.to == null) return;
      paneRangeSyncing = true;
      try {
        rsiChart.timeScale().setVisibleLogicalRange({ from: r.from, to: r.to });
      } catch (_) {}
      paneRangeSyncing = false;
    });
    rsiChart.timeScale().subscribeVisibleLogicalRangeChange(function (r) {
      scheduleDrawTrendlines();
      if (paneRangeSyncing || !mainChart || !r || r.from == null || r.to == null) return;
      if (typeof mainChart.timeScale().setVisibleLogicalRange !== 'function') return;
      paneRangeSyncing = true;
      try {
        mainChart.timeScale().setVisibleLogicalRange({ from: r.from, to: r.to });
      } catch (_) {}
      paneRangeSyncing = false;
    });

    mainChart.subscribeCrosshairMove(function (param) {
      if (chartInteractionMode === MODE_CROSSHAIR) {
        if (!param || !param.point || param.time === undefined || param.time === null) {
          crosshairOhlcSnapApplied = null;
        }
        tryApplyMainCrosshairOhlcSnap(param);
      } else {
        crosshairOhlcSnapApplied = null;
      }
      syncRsiCrosshairFromMain(param);
      onCrosshair(param);
      updateCrosshairLastSnap(param);
    });
    rsiChart.subscribeCrosshairMove(function (param) {
      syncMainCrosshairFromRsi(param);
    });

    alignRsiPaneLayout();

    chartResizeObs = new ResizeObserver(function () {
      scheduleSyncChartPaneSizes();
    });
    var mainWrap = document.getElementById('sc-main-wrap');
    var rsiWrap = document.getElementById('sc-rsi-wrap');
    if (mainWrap) chartResizeObs.observe(mainWrap);
    if (rsiWrap) chartResizeObs.observe(rsiWrap);

    applyChartInteractionMode();
    bindMainChartTrendlineDomSync();
    bindPaneDoubleClickZoomReset();
    bindMainWrapOverlayInteraction();
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
        if (Object.prototype.hasOwnProperty.call(body, 'indicators') && Array.isArray(body.indicators)) {
          indicators = hydrateIndicatorsFromSave(body.indicators);
        }
        trendlines.forEach(function (tl) {
          if (tl.extended === undefined) tl.extended = false;
          if (!tl.style) tl.style = 'solid';
          if (!tl.id) tl.id = uid();
          tl.type = normalizeDrawingType(tl.type || 'TRENDLINE');
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
      flushSaveAnnotations();
    }, SAVE_DEBOUNCE_MS);
  }

  function flushSaveAnnotations() {
    if (!TOKEN) return;
    fetch('/dashboard/chart-annotations', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(annotationsPayloadForSave()),
      keepalive: true
    }).catch(function () {});
  }

  function styleToLw(s) {
    if (s === 'dotted') return 1;
    if (s === 'dashed' || s === 'largeDashed') return 2;
    return 0;
  }

  function normalizeHexColor(color, fallback) {
    fallback = fallback || '#94a3b8';
    if (typeof color !== 'string') return fallback;
    var h = color.trim();
    if (h.charAt(0) !== '#') return fallback;
    var hex = h.slice(1);
    if (hex.length === 3) {
      hex = hex.charAt(0) + hex.charAt(0) + hex.charAt(1) + hex.charAt(1) + hex.charAt(2) + hex.charAt(2);
    }
    if (!/^[0-9a-fA-F]{6}$/.test(hex)) return fallback;
    return '#' + hex.toLowerCase();
  }

  function rgbaFromHex(hex, alpha) {
    var norm = normalizeHexColor(hex);
    var h = norm.slice(1);
    var r = parseInt(h.slice(0, 2), 16);
    var g = parseInt(h.slice(2, 4), 16);
    var b = parseInt(h.slice(4, 6), 16);
    return 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')';
  }

  function applyChipObjectColor(chip, color, fallback) {
    var hex = normalizeHexColor(color, fallback);
    chip.style.borderColor = hex;
    chip.style.background = rgbaFromHex(hex, 0.14);
    chip.style.setProperty('--sc-chip-color', hex);
    chip.style.setProperty('--sc-chip-glow', rgbaFromHex(hex, 0.38));
  }

  function indicatorGlowColor(color) {
    if (typeof color !== 'string') return 'rgba(255,255,255,.28)';
    var h = color.trim();
    if (h.startsWith('hsl')) {
      return h.replace('hsl', 'hsla').replace(')', ', 0.33)');
    }
    return rgbaFromHex(color, 0.33);
  }

  /** Remove all Lightweight Charts series owned by one indicator row. */
  function detachIndicatorSeries(ind) {
    if (!ind || !mainChart) return;

    // Call custom plugin cleanup if defined
    var plugin = indicatorRegistry.get(ind.type);
    if (plugin && typeof plugin.destroySeries === 'function') {
      try { plugin.destroySeries(mainChart, ind.seriesObj, ind); } catch (_) {}
    } else {
      // Fallback generic cleanup: remove any series found in seriesObj recursively
      if (ind.seriesObj && typeof ind.seriesObj === 'object') {
        function removeRecursive(obj) {
          if (!obj) return;
          if (typeof obj.removeSeries === 'function') return;
          if (Array.isArray(obj)) {
            obj.forEach(removeRecursive);
          } else if (typeof obj === 'object') {
            Object.keys(obj).forEach(function (k) {
              var val = obj[k];
              if (val && typeof val.applyOptions === 'function') {
                try { mainChart.removeSeries(val); } catch (_) {}
              } else if (val && typeof val === 'object') {
                removeRecursive(val);
              }
            });
          }
        }
        removeRecursive(ind.seriesObj);
      }
    }

    if (ind.series) {
      try { mainChart.removeSeries(ind.series); } catch (_) {}
      ind.series = null;
    }
    if (ind.glowSeries) {
      try { mainChart.removeSeries(ind.glowSeries); } catch (_) {}
      ind.glowSeries = null;
    }
    if (ind.seriesArr && ind.seriesArr.length) {
      ind.seriesArr.forEach(function (s) {
        try { mainChart.removeSeries(s); } catch (_) {}
      });
      ind.seriesArr = [];
    }
    if (ind.fibGlowSeriesArr && ind.fibGlowSeriesArr.length) {
      ind.fibGlowSeriesArr.forEach(function (s) {
        try { mainChart.removeSeries(s); } catch (_) {}
      });
      ind.fibGlowSeriesArr = [];
    }
    ind.seriesObj = null;
  }

  function syncIndicatorSelectionVisuals() {
    if (!mainChart) return;
    indicators.forEach(function (ind) {
      var lw = Math.max(1, Math.min(8, Number(ind.lineWidth) || 2));
      var ls = styleToLw(ind.style || 'solid');
      var selected = selectedIndId != null && selectedIndId === ind.id && chartAllowsObjectSelection();
      var plugin = indicatorRegistry.get(ind.type);
      var bandKeys = indicatorBandKeys(ind);
      if (plugin && typeof plugin.syncSelection === 'function') {
        try { plugin.syncSelection(ind.seriesObj, selected, ind); } catch (_) {}
      } else if (bandKeys && ind.seriesArr && ind.seriesArr.length) {
        var fibLw = Math.max(1, Math.min(8, Number(ind.lineWidth) || 1));
        var fibLs = styleToLw(ind.style || 'solid');
        var fibColors = fibBandColorMap(ind);
        ind.seriesArr.forEach(function (s, idx) {
          var lvl = bandKeys[idx];
          try {
            s.applyOptions({
              color: fibColors[lvl] || ind.color || '#f59e0b',
              lineWidth: fibLw,
              lineStyle: fibLs,
              priceLineVisible: false,
              lastValueVisible: true,
              visible: ind.visible !== false
            });
          } catch (_) {}
        });
        if (!ind.fibGlowSeriesArr) ind.fibGlowSeriesArr = [];
        if (!selected || ind.visible === false) {
          if (ind.fibGlowSeriesArr.length) {
            ind.fibGlowSeriesArr.forEach(function (g) {
              try { mainChart.removeSeries(g); } catch (_) {}
            });
            ind.fibGlowSeriesArr = [];
          }
          return;
        }
        if (!ind.fibGlowSeriesArr.length && ind.cachedPtsArr) {
          bandKeys.forEach(function (lvl) {
            var pts = ind.cachedPtsArr[lvl];
            if (!pts || !pts.length) return;
            try {
              var glow = mainChart.addLineSeries({
                color: indicatorGlowColor(fibColors[lvl] || ind.color),
                lineWidth: Math.min(16, fibLw + 5),
                lineStyle: 0,
                priceLineVisible: false,
                lastValueVisible: false,
                crosshairMarkerVisible: false
              });
              glow.setData(pts);
              ind.fibGlowSeriesArr.push(glow);
            } catch (_) {}
          });
        }
        return;
      }
      if (ind.series) {
        try {
          ind.series.applyOptions({
            color: ind.color || '#f59e0b',
            lineWidth: lw,
            lineStyle: ls,
            priceLineVisible: false,
            lastValueVisible: true,
            visible: ind.visible !== false
          });
        } catch (_) {}
      }
      if (selected && ind.visible !== false) {
        if (!ind.glowSeries) {
          try {
            ind.glowSeries = mainChart.addLineSeries({
              color: indicatorGlowColor(ind.color),
              lineWidth: Math.min(16, lw + 5),
              lineStyle: 0,
              priceLineVisible: false,
              lastValueVisible: false,
              crosshairMarkerVisible: false
            });
            if (ind.cachedPts && ind.cachedPts.length) ind.glowSeries.setData(ind.cachedPts);
          } catch (_) {
            ind.glowSeries = null;
          }
        } else {
          try {
            ind.glowSeries.applyOptions({
              color: indicatorGlowColor(ind.color),
              lineWidth: Math.min(16, lw + 5),
              lineStyle: 0,
              priceLineVisible: false,
              lastValueVisible: false,
              crosshairMarkerVisible: false
            });
          } catch (_) {}
        }
      } else if (ind.glowSeries) {
        try { mainChart.removeSeries(ind.glowSeries); } catch (_) {}
        ind.glowSeries = null;
      }
    });
  }

  /** Match OHLC / snap dedupe granularity so "same price" is consistent. */
  function levelPriceKey(p) {
    return Number(p).toFixed(8);
  }

  function hasLevelAtPrice(price) {
    var key = levelPriceKey(price);
    for (var i = 0; i < levels.length; i++) {
      if (levelPriceKey(levels[i].price) === key) return true;
    }
    return false;
  }

  function clearLevelGlowPriceLines() {
    if (!candleSeries) {
      levelGlowPriceLines = {};
      return;
    }
    Object.keys(levelGlowPriceLines).forEach(function (id) {
      var pl = levelGlowPriceLines[id];
      try { candleSeries.removePriceLine(pl); } catch (_) {}
    });
    levelGlowPriceLines = {};
  }

  function syncLevelSelectionVisuals() {
    if (!candleSeries) return;
    var allow = chartAllowsObjectSelection();
    Object.keys(levelGlowPriceLines).forEach(function (id) {
      if (!allow || id !== selectedLevelId) {
        try { candleSeries.removePriceLine(levelGlowPriceLines[id]); } catch (_) {}
        delete levelGlowPriceLines[id];
      }
    });
    levels.forEach(function (lv) {
      if (levelPriceLines[lv.id]) applyLevelPriceLineVisual(lv);
    });
    if (!allow || !selectedLevelId) return;
    var lv = findLevelById(selectedLevelId);
    if (!lv) return;
    var id = lv.id;
    var baseLw = Math.max(1, Number(lv.width) || 1);
    var glowOpts = {
      price: Number(lv.price),
      color: indicatorGlowColor(lv.color || '#22c55e'),
      lineWidth: Math.min(16, baseLw + 5),
      lineStyle: 0,
      axisLabelVisible: false,
      title: ''
    };
    var glow = levelGlowPriceLines[id];
    if (!glow) {
      try {
        levelGlowPriceLines[id] = candleSeries.createPriceLine(glowOpts);
      } catch (_) {}
    } else {
      try {
        glow.applyOptions(glowOpts);
      } catch (_) {
        try { candleSeries.removePriceLine(glow); } catch (__) {}
        delete levelGlowPriceLines[id];
        try {
          levelGlowPriceLines[id] = candleSeries.createPriceLine(glowOpts);
        } catch (___) {}
      }
    }
  }

  function clearLevelPriceLines() {
    clearLevelGlowPriceLines();
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
    syncLevelSelectionVisuals();
  }

  function barsForIndicators() {
    if (!allBars.length) return [];
    return allBars;
  }

  function refreshIndicators() {
    if (!mainChart || !candleSeries || !rsiLineSeries) return;

    indicators.forEach(function (ind) {
      detachIndicatorSeries(ind);
    });

    var bars = barsForIndicators();
    indicators.forEach(function (ind) {
      var plugin = indicatorRegistry.get(ind.type);
      if (!plugin) return;
      
      var data = plugin.calculate(bars, ind);
      ind.cachedPtsArr = data;
      ind.cachedPts = indicatorPrimaryPoints(plugin, data);
      
      var seriesObj = plugin.createSeries(mainChart, ind);
      ind.seriesObj = seriesObj;
      
      if (seriesObj.mainSeries) ind.series = seriesObj.mainSeries;
      if (seriesObj.glowSeries) ind.glowSeries = seriesObj.glowSeries;
      if (isBandIndicator(ind)) {
         ind.seriesArr = Object.values(seriesObj.bands || {});
         ind.fibGlowSeriesArr = Object.values(seriesObj.glowBands || {});
      }
      
      plugin.updateSeries(seriesObj, data);
    });

    syncIndicatorSelectionVisuals();

    var rsiPlugin = indicatorRegistry.get('RSI');
    if (rsiPlugin) {
      cachedRsiPoints = rsiPlugin.calculate(bars, {period: RSI_PERIOD});
      rsiLineSeries.setData(cachedRsiPoints);
    }
    
    requestAnimationFrame(function () {
      alignRsiPaneLayout();
    });
  }

  /**
   * Update last RSI / MA points during live ticks without removing series or
   * calling setData on the full RSI series (that resets time scale and fights zoom).
   */
  function updateDerivedSeriesLive() {
    if (!mainChart || !candleSeries || !rsiLineSeries) return;
    var bars = barsForIndicators();
    if (!bars.length) return;

    var rsiPlugin = indicatorRegistry.get('RSI');
    if (rsiPlugin) {
      cachedRsiPoints = rsiPlugin.calculate(bars, {period: RSI_PERIOD});
      var lastRsi = cachedRsiPoints[cachedRsiPoints.length - 1];
      if (lastRsi) {
        try { rsiLineSeries.update(lastRsi); } catch (_) {}
      }
    }

    indicators.forEach(function (ind) {
      var plugin = indicatorRegistry.get(ind.type);
      if (!plugin || !ind.seriesObj) return;
      var data = plugin.calculate(bars, ind);
      ind.cachedPtsArr = data;
      ind.cachedPts = indicatorPrimaryPoints(plugin, data);
      try {
        plugin.updateSeries(ind.seriesObj, data);
      } catch(_) {}
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

  /** Time under the main crosshair's vertical line (matches pointer X, not always param.time). */
  function mainCrosshairDataTime(param) {
    if (!param) return null;
    if (mainChart && param.point && param.point.x != null) {
      var ts = mainChart.timeScale();
      if (typeof ts.coordinateToTime === 'function') {
        var ct = ts.coordinateToTime(param.point.x);
        if (ct != null) return ct;
      }
    }
    return param.time;
  }

  /** Time under the RSI crosshair's vertical line. */
  function rsiCrosshairDataTime(param) {
    if (!param) return null;
    if (rsiChart && param.point && param.point.x != null) {
      var ts = rsiChart.timeScale();
      if (typeof ts.coordinateToTime === 'function') {
        var ct = ts.coordinateToTime(param.point.x);
        if (ct != null) return ct;
      }
    }
    return param.time;
  }

  function candleForCrosshairTime(t) {
    if (t == null) return null;
    var b = candleAtTime(t);
    if (b) return b;
    if (!allBars.length) return null;
    var secT = chartTimeToUnixSec(t);
    if (!isFinite(secT)) return null;
    var best = null;
    var bestD = Infinity;
    for (var i = 0; i < allBars.length; i++) {
      var bar = allBars[i];
      var d = Math.abs(chartTimeToUnixSec(bar.time) - secT);
      if (d < bestD) {
        bestD = d;
        best = bar;
      }
    }
    return best;
  }

  function rsiValueForCrosshairTime(t) {
    if (t == null || !cachedRsiPoints.length) return null;
    var v = rsiValueAtTime(t);
    if (v != null) return v;
    var secT = chartTimeToUnixSec(t);
    if (!isFinite(secT)) return null;
    var best = null;
    var bestD = Infinity;
    for (var i = 0; i < cachedRsiPoints.length; i++) {
      var p = cachedRsiPoints[i];
      if (p.value == null || p.value === undefined) continue;
      var d = Math.abs(chartTimeToUnixSec(p.time) - secT);
      if (d < bestD) {
        bestD = d;
        best = p.value;
      }
    }
    return best;
  }

  function ensureRsiTimeScaleVisualMatchesMain() {
    if (!mainChart || !rsiChart) return;
    try {
      var mo = mainChart.timeScale().options();
      var key = String(mo.barSpacing) + '|' + String(mo.minBarSpacing) + '|' + String(mo.rightOffset) + '|' +
        String(!!mo.fixLeftEdge) + '|' + String(!!mo.fixRightEdge) + '|' + String(!!mo.rightBarStaysOnScroll);
      if (key === rsiTimeScaleSyncKey) return;
      rsiTimeScaleSyncKey = key;
      rsiChart.timeScale().applyOptions({
        barSpacing: mo.barSpacing,
        minBarSpacing: mo.minBarSpacing,
        rightOffset: mo.rightOffset,
        fixLeftEdge: mo.fixLeftEdge,
        fixRightEdge: mo.fixRightEdge,
        rightBarStaysOnScroll: mo.rightBarStaysOnScroll
      });
    } catch (_) {}
  }

  function candleAtTime(t) {
    if (t == null || !allBars.length) return null;
    for (var i = allBars.length - 1; i >= 0; i--) {
      var b = allBars[i];
      if (b.time === t || String(b.time) === String(t)) return b;
    }
    return null;
  }

  function pushFiniteUniquePrice(set, p) {
    var n = Number(p);
    if (!isFinite(n)) return;
    set[n.toFixed(8)] = n;
  }

  /** OHLC + overlay line values at crosshair time (for vertical snap targets). */
  function collectCrosshairSnapPrices(bar, param) {
    var uniq = {};
    if (bar) {
      pushFiniteUniquePrice(uniq, bar.open);
      pushFiniteUniquePrice(uniq, bar.high);
      pushFiniteUniquePrice(uniq, bar.low);
      pushFiniteUniquePrice(uniq, bar.close);
    }
    if (param && param.seriesData && indicators) {
      indicators.forEach(function (ind) {
        if (!ind.series) return;
        var sd = param.seriesData.get(ind.series);
        if (sd && sd.value != null) pushFiniteUniquePrice(uniq, sd.value);
      });
    }
    var out = [];
    for (var k in uniq) {
      if (Object.prototype.hasOwnProperty.call(uniq, k)) out.push(uniq[k]);
    }
    return out;
  }

  function nearestSnapPriceForPixel(yPx, prices) {
    if (!candleSeries || yPx == null || !isFinite(yPx) || !prices || !prices.length) return null;
    var best = null;
    var bestD = Infinity;
    for (var i = 0; i < prices.length; i++) {
      var p = prices[i];
      var cy = candleSeries.priceToCoordinate(p);
      if (cy == null || !isFinite(cy)) continue;
      var d = Math.abs(cy - yPx);
      if (d < bestD - 1e-6) {
        bestD = d;
        best = p;
      } else if (Math.abs(d - bestD) <= 1e-6 && best != null) {
        var ref = coordinateToPriceExtrapolated(yPx);
        if (ref != null && isFinite(ref) && Math.abs(p - ref) < Math.abs(best - ref)) best = p;
      }
    }
    return best;
  }

  function snapMainCrosshairPriceForBar(bar, param, yHintPx) {
    if (!mainChart || !candleSeries || typeof mainChart.setCrosshairPosition !== 'function') return null;
    if (!bar || param == null || param.time == undefined || param.time === null) return null;
    var targets = collectCrosshairSnapPrices(bar, param);
    if (!targets.length) return null;
    var yRef = yHintPx != null && isFinite(yHintPx) ? yHintPx : (param.point && param.point.y != null ? param.point.y : null);
    if (yRef == null) return null;
    return nearestSnapPriceForPixel(yRef, targets);
  }

  /**
   * Snap main crosshair Y to nearest OHLC / MA in screen space.
   * Uses (time, snapped price) dedupe — param.point.y follows the mouse, so comparing it to
   * priceToCoordinate(snapped) would force setCrosshairPosition every event and break the chart.
   */
  function releaseCrosshairSyncGuardDeferred(expectedGuard) {
    queueMicrotask(function () {
      if (crosshairSyncGuard === expectedGuard) crosshairSyncGuard = 0;
    });
  }

  function tryApplyMainCrosshairOhlcSnap(param) {
    if (chartInteractionMode !== MODE_CROSSHAIR || crosshairOhlcSnapGuard) return;
    if (!param || !param.point) {
      crosshairOhlcSnapApplied = null;
      return;
    }
    var tUse = mainCrosshairDataTime(param);
    if (tUse === undefined || tUse === null) {
      crosshairOhlcSnapApplied = null;
      return;
    }
    var bar = candleForCrosshairTime(tUse);
    if (!bar) {
      crosshairOhlcSnapApplied = null;
      return;
    }
    var yRef = param.point.y;
    var snapped = snapMainCrosshairPriceForBar(bar, param, yRef);
    if (snapped == null || !isFinite(Number(snapped))) {
      crosshairOhlcSnapApplied = null;
      return;
    }
    var newY = candleSeries.priceToCoordinate(snapped);
    if (newY == null || !isFinite(newY)) return;

    var prev = crosshairOhlcSnapApplied;
    var keySnap = levelPriceKey(snapped);
    if (prev && (prev.time === tUse || String(prev.time) === String(tUse)) &&
        levelPriceKey(prev.price) === keySnap &&
        Math.abs(yRef - newY) < 0.5) {
      return;
    }

    crosshairOhlcSnapApplied = { time: tUse, price: snapped };
    crosshairOhlcSnapGuard = 1;
    try {
      mainChart.setCrosshairPosition(snapped, tUse, candleSeries);
    } catch (_) {}
    crosshairOhlcSnapGuard = 0;
  }

  function updateCrosshairLastSnap(param) {
    crosshairLastSnap = null;
    if (chartInteractionMode !== MODE_CROSSHAIR || !param || !param.point) return;
    var tUse = mainCrosshairDataTime(param);
    if (tUse == null) return;
    var bar = candleForCrosshairTime(tUse);
    if (!bar) return;
    var yRef = param.point.y;
    var snapped = snapMainCrosshairPriceForBar(bar, param, yRef);
    if (snapped != null && isFinite(snapped)) {
      crosshairLastSnap = { time: tUse, price: snapped };
    }
  }

  function unbindCrosshairOhlcSnapDom() {
    var el = document.getElementById('sc-main-chart');
    if (!el || !crosshairSnapListenersBound) return;
    crosshairSnapListenersBound = false;
    if (crosshairSnapClick) {
      el.removeEventListener('click', crosshairSnapClick);
      crosshairSnapClick = null;
    }
  }

  function bindCrosshairOhlcSnapDom() {
    var el = document.getElementById('sc-main-chart');
    if (!el) return;
    if (chartInteractionMode !== MODE_CROSSHAIR) {
      unbindCrosshairOhlcSnapDom();
      return;
    }
    if (crosshairSnapListenersBound) return;
    crosshairSnapListenersBound = true;
    crosshairSnapClick = function (ev) {
      if (chartInteractionMode !== MODE_CROSSHAIR || ev.button !== 0) return;
      if (ev.detail !== 1) return;
      var tgt = ev.target;
      if (tgt && tgt.closest && (tgt.closest('button') || tgt.closest('a') || tgt.closest('input') ||
          tgt.closest('select') || tgt.closest('textarea') || tgt.closest('.sc-panel'))) {
        return;
      }
      var wrap = document.getElementById('sc-main-wrap');
      if (wrap && mainChart && candleSeries) {
        var r = wrap.getBoundingClientRect();
        var px = ev.clientX - r.left;
        var py = ev.clientY - r.top;
        var wid = wrap.clientWidth;
        if (hitTestTrendlineIds(px, py, wid) || hitTestLevelId(px, py) || hitTestIndicatorId(px, py)) return;
      }
      if (!crosshairLastSnap || crosshairLastSnap.price == null || !isFinite(crosshairLastSnap.price)) return;
      if (!candleSeries) return;
      if (hasLevelAtPrice(crosshairLastSnap.price)) return;
      levels.push({
        id: uid(),
        price: crosshairLastSnap.price,
        color: '#cbd5e1',
        style: 'solid',
        width: 2,
        label: ''
      });
      restoreLevels();
      scheduleSaveAnnotations();
      renderChips();
    };
    el.addEventListener('click', crosshairSnapClick);
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
    try {
      ensureRsiTimeScaleVisualMatchesMain();
      var tUse = mainCrosshairDataTime(param);
      var rv = rsiValueForCrosshairTime(tUse);
      if (rv != null) rsiChart.setCrosshairPosition(rv, tUse, rsiLineSeries);
      else rsiChart.clearCrosshairPosition();
    } finally {
      releaseCrosshairSyncGuardDeferred(1);
    }
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
    try {
      var tUse = rsiCrosshairDataTime(param);
      var b = candleForCrosshairTime(tUse);
      if (b) {
        var yHint = null;
        if (crosshairOhlcSnapApplied && crosshairOhlcSnapApplied.price != null) {
          var ySnap = candleSeries.priceToCoordinate(Number(crosshairOhlcSnapApplied.price));
          if (ySnap != null && isFinite(ySnap)) yHint = ySnap;
        }
        if (yHint == null) {
          var yClose = candleSeries.priceToCoordinate(Number(b.close));
          var wrap = document.getElementById('sc-main-wrap');
          yHint = yClose != null && isFinite(yClose) ? yClose : (wrap ? wrap.clientHeight * 0.5 : 200);
        }
        var snapped = snapMainCrosshairPriceForBar(b, { time: tUse, point: { y: yHint } }, yHint);
        if (snapped == null) snapped = Number(b.close);
        crosshairOhlcSnapApplied = { time: tUse, price: snapped };
        mainChart.setCrosshairPosition(snapped, tUse, candleSeries);
      } else mainChart.clearCrosshairPosition();
    } finally {
      releaseCrosshairSyncGuardDeferred(2);
    }
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

  function needsTrendlineScalePoll() {
    return trendlines.length > 0 || tlAnchors.length > 0 || selectedTlId != null || tlAnchorPulse != null ||
      levelDrag != null || selectedLevelId != null || selectedIndId != null;
  }

  function tlDisarmDocumentPointerSync() {
    if (!tlDocPointerSyncArmed) return;
    tlDocPointerSyncArmed = false;
    if (tlDocPointerMoveSync) {
      document.removeEventListener('pointermove', tlDocPointerMoveSync, { capture: true });
      tlDocPointerMoveSync = null;
    }
    if (tlDocMouseMoveSync) {
      document.removeEventListener('mousemove', tlDocMouseMoveSync, { capture: true });
      tlDocMouseMoveSync = null;
    }
    if (tlDocDragEndSync) {
      document.removeEventListener('pointerup', tlDocDragEndSync, { capture: true });
      document.removeEventListener('pointercancel', tlDocDragEndSync, { capture: true });
      document.removeEventListener('mouseup', tlDocDragEndSync, { capture: true });
      tlDocDragEndSync = null;
    }
  }

  function tlArmDocumentPointerSync() {
    if (tlDocPointerSyncArmed) return;
    tlDocPointerSyncArmed = true;
    tlDocPointerMoveSync = function (ev) {
      if (needsTrendlineScalePoll() && ev.buttons) scheduleDrawTrendlines();
    };
    tlDocMouseMoveSync = function (ev) {
      if (needsTrendlineScalePoll() && ev.buttons) scheduleDrawTrendlines();
    };
    tlDocDragEndSync = function () {
      if (needsTrendlineScalePoll()) scheduleDrawTrendlines();
      tlDisarmDocumentPointerSync();
    };
    document.addEventListener('pointermove', tlDocPointerMoveSync, { capture: true, passive: true });
    document.addEventListener('mousemove', tlDocMouseMoveSync, { capture: true, passive: true });
    document.addEventListener('pointerup', tlDocDragEndSync, { capture: true });
    document.addEventListener('pointercancel', tlDocDragEndSync, { capture: true });
    document.addEventListener('mouseup', tlDocDragEndSync, { capture: true });
  }

  function bindMainChartTrendlineDomSync() {
    unbindMainChartTrendlineDomSync();
    if (!mainChart || typeof mainChart.chartElement !== 'function') return;
    var el = mainChart.chartElement();
    if (!el) return;
    tlMainDomSyncEl = el;
    var mainWrap = document.getElementById('sc-main-wrap');
    tlDomWheelHandler = function () {
      if (needsTrendlineScalePoll()) scheduleDrawTrendlines();
    };
    tlDomPointerDownHandler = function () {
      if (!needsTrendlineScalePoll()) return;
      scheduleDrawTrendlines();
      tlArmDocumentPointerSync();
    };
    if (mainWrap) {
      tlMainWrapSyncEl = mainWrap;
      mainWrap.addEventListener('pointerdown', tlDomPointerDownHandler, true);
    }
    el.addEventListener('wheel', tlDomWheelHandler, { passive: true, capture: true });
    el.addEventListener('pointerdown', tlDomPointerDownHandler, true);
  }

  function unbindMainChartTrendlineDomSync() {
    tlDisarmDocumentPointerSync();
    var el = tlMainDomSyncEl;
    if (tlMainWrapSyncEl && tlDomPointerDownHandler) {
      tlMainWrapSyncEl.removeEventListener('pointerdown', tlDomPointerDownHandler, true);
      tlMainWrapSyncEl = null;
    }
    if (el) {
      if (tlDomWheelHandler) el.removeEventListener('wheel', tlDomWheelHandler, { capture: true });
      if (tlDomPointerDownHandler) el.removeEventListener('pointerdown', tlDomPointerDownHandler, true);
    }
    tlMainDomSyncEl = null;
    tlDomWheelHandler = null;
    tlDomPointerDownHandler = null;
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
    var res = { x1: x1, y1: y1, x2: x2, y2: y2 };
    if (tl.time3 != null && tl.price3 != null) {
      res.x3 = timeToCoordinateExtrapolated(tl.time3);
      res.y3 = candleSeries.priceToCoordinate(tl.price3);
    }
    return res;
  }

  function getTrendlinePixelEndpoints(tl, w) {
    if (!mainChart || !candleSeries) return null;
    var x1 = timeToCoordinateExtrapolated(tl.time1);
    var y1 = candleSeries.priceToCoordinate(tl.price1);
    var x2 = timeToCoordinateExtrapolated(tl.time2);
    var y2 = candleSeries.priceToCoordinate(tl.price2);
    if (x1 == null || y1 == null || x2 == null || y2 == null) return null;
    
    var res = { x1: x1, y1: y1, x2: x2, y2: y2 };
    
    if (tl.time3 != null && tl.price3 != null) {
      res.x3 = timeToCoordinateExtrapolated(tl.time3);
      res.y3 = candleSeries.priceToCoordinate(tl.price3);
    }

    if (tl.extended) {
      var dx = x2 - x1;
      var dy = y2 - y1;
      if (Math.abs(dx) > 0.001) {
        var nx1 = 0;
        var ny1 = y1 + (0 - x1) * (dy / dx);
        var nx2 = w;
        var ny2 = y1 + (w - x1) * (dy / dx);
        res.x1 = nx1; res.y1 = ny1;
        res.x2 = nx2; res.y2 = ny2;
      } else {
        res.y1 = 0; res.y2 = 10000;
      }
    }
    return res;
  }

  /** 1 = first anchor, 2 = second; null if outside handle radius. */
  function hitTestTrendlineHandle(px, py, tl) {
    var ap = getTrendlineAnchorPixels(tl);
    if (!ap) return null;
    var d1 = Math.hypot(px - ap.x1, py - ap.y1);
    var d2 = Math.hypot(px - ap.x2, py - ap.y2);
    var d3 = ap.x3 != null ? Math.hypot(px - ap.x3, py - ap.y3) : Infinity;
    if (d1 <= TL_HANDLE_HIT_PX && d1 <= d2 && d1 <= d3) return 1;
    if (d2 <= TL_HANDLE_HIT_PX && d2 <= d3) return 2;
    if (d3 <= TL_HANDLE_HIT_PX) return 3;
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
    var d = distPointToSegment(px, py, ep.x1, ep.y1, ep.x2, ep.y2);
    if (ep.x3 != null && ep.y3 != null) {
      var dx = ep.x2 - ep.x1;
      var dy = ep.y2 - ep.y1;
      var x4 = ep.x3 + dx;
      var y4 = ep.y3 + dy;
      var dUpper = distPointToSegment(px, py, ep.x3, ep.y3, x4, y4);
      if (dUpper < d) d = dUpper;
    }
    if (d > TL_HIT_PX) return false;
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
      var plugin = drawingRegistry.get(normalizeDrawingType(tl.type));
      if (plugin && plugin.hitTestMode === 'horizontalLevels') {
        var ap = getTrendlineAnchorPixels(tl);
        if (!ap) continue;
        var diffY = ap.y2 - ap.y1;
        var fibs = FIB_LEVELS || [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
        for (var fi = 0; fi < fibs.length; fi++) {
          var yy = ap.y1 + diffY * fibs[fi];
          var dFib = Math.abs(py - yy);
          if (dFib <= TL_HIT_PX && dFib < bestD) {
            bestD = dFib;
            best = tl.id;
          }
        }
        var dDiag = distPointToSegment(px, py, ap.x1, ap.y1, ap.x2, ap.y2);
        if (dDiag <= TL_HIT_PX && dDiag < bestD) {
          bestD = dDiag;
          best = tl.id;
        }
        continue;
      }
      var ep = getTrendlinePixelEndpoints(tl, w);
      if (!ep) continue;
      var d = distPointToSegment(px, py, ep.x1, ep.y1, ep.x2, ep.y2);
      if (ep.x3 != null && ep.y3 != null) {
        var dx = ep.x2 - ep.x1;
        var dy = ep.y2 - ep.y1;
        var x4 = ep.x3 + dx;
        var y4 = ep.y3 + dy;
        var dUpper = distPointToSegment(px, py, ep.x3, ep.y3, x4, y4);
        if (dUpper < d) d = dUpper;
      }
      if (d <= TL_HIT_PX && d < bestD) {
        bestD = d;
        best = tl.id;
      }
    }
    return best;
  }

  function drawFibRetracementLevels(ctx, x1, y1, x2, y2, w, opts) {
    opts = opts || {};
    var sel = !!opts.selected;
    var lw = Math.max(1, Number(opts.width) || 1);
    var diffY = y2 - y1;
    var fibs = FIB_LEVELS || [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
    var colorMap = ['#f59e0b', '#a855f7', '#3b82f6', '#ef4444', '#22c55e', '#94a3b8', '#f59e0b'];

    ctx.strokeStyle = sel ? 'rgba(255,255,255,.35)' : 'rgba(148,163,184,.25)';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
    ctx.setLineDash([]);

    fibs.forEach(function (pct, i) {
      var yy = y1 + diffY * pct;
      if (sel) {
        ctx.strokeStyle = 'rgba(255,255,255,.28)';
        ctx.lineWidth = lw + 8;
        ctx.setLineDash([]);
        ctx.beginPath();
        ctx.moveTo(0, yy);
        ctx.lineTo(w, yy);
        ctx.stroke();
      }
      ctx.strokeStyle = sel ? 'rgba(255,255,255,.85)' : rgbaFromHex(colorMap[i] || '#f59e0b', 0.55);
      ctx.lineWidth = sel ? lw + 1 : lw;
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.moveTo(0, yy);
      ctx.lineTo(w, yy);
      ctx.stroke();
      if (opts.showLabels !== false) {
        ctx.fillStyle = ctx.strokeStyle;
        ctx.font = '10px sans-serif';
        ctx.fillText((pct * 100).toFixed(1) + '%', 10, yy - 4);
      }
    });
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
      var sel = tl.id === selectedTlId;
      var pluginType = normalizeDrawingType(tl.type);
      var plugin = drawingRegistry.get(pluginType);
      if (plugin) {
        plugin.draw(ctx, tl, ep, { selected: sel, width: w });
      } else {
        var fallbackPlugin = drawingRegistry.get('TRENDLINE');
        if (fallbackPlugin) fallbackPlugin.draw(ctx, tl, ep, { selected: sel, width: w });
      }
      
      if (sel) {
        var ap = getTrendlineAnchorPixels(tl);
        if (ap) {
          drawAnchorMarker(ctx, ap.x1, ap.y1);
          drawAnchorMarker(ctx, ap.x2, ap.y2);
          if (ap.x3 != null && ap.y3 != null) {
            drawAnchorMarker(ctx, ap.x3, ap.y3);
          }
        }
      }
    });

    if (chartInteractionMode === MODE_OBJECTS && tlAnchors.length >= 1 && mainChart && candleSeries) {
      for (var i = 0; i < tlAnchors.length; i++) {
        var a = tlAnchors[i];
        var ax = timeToCoordinateExtrapolated(a.time);
        var ay = candleSeries.priceToCoordinate(a.price);
        if (ax != null && ay != null) {
          drawAnchorMarker(ctx, ax, ay);
        }
      }
      
      if (tlPreviewPx && tlAnchors.length > 0) {
        var a0 = tlAnchors[0];
        var ax = timeToCoordinateExtrapolated(a0.time);
        var ay = candleSeries.priceToCoordinate(a0.price);
        
        var plugin = drawingRegistry.get(getActiveDrawType());
        if (plugin) {
           var draftPixels = { x1: ax, y1: ay, x2: tlPreviewPx.x, y2: tlPreviewPx.y };
           if (tlAnchors.length === 2) {
             var a1 = tlAnchors[1];
             draftPixels.x2 = timeToCoordinateExtrapolated(a1.time);
             draftPixels.y2 = candleSeries.priceToCoordinate(a1.price);
             draftPixels.x3 = tlPreviewPx.x;
             draftPixels.y3 = tlPreviewPx.y;
           }
           plugin.draw(ctx, { color: 'rgba(250,204,21,.75)', style: 'dashed', width: 2 }, draftPixels, { selected: false, width: w });
        }
      }
    }

    if (chartInteractionMode === MODE_OBJECTS && tlPreviewPx) {
      ctx.strokeStyle = 'rgba(150, 150, 150, 0.6)';
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 4]);
      
      ctx.beginPath();
      ctx.moveTo(0, tlPreviewPx.y);
      ctx.lineTo(w, tlPreviewPx.y);
      ctx.stroke();
      
      ctx.beginPath();
      ctx.moveTo(tlPreviewPx.x, 0);
      ctx.lineTo(tlPreviewPx.x, h);
      ctx.stroke();

      ctx.setLineDash([]);
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
      } else if (tlRmbDrag.whichEnd === 2) {
        tlx.time2 = time;
        tlx.price2 = price;
        if (fo) {
          tlx.time1 = fo.time;
          tlx.price1 = fo.price;
        }
      } else if (tlRmbDrag.whichEnd === 3) {
        tlx.time3 = time;
        tlx.price3 = price;
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
    if (ep.x3 != null && ep.y3 != null) {
      var t3 = coordinateToTimeExtrapolated(ep.x3 + ddx);
      var pr3 = coordinateToPriceExtrapolated(ep.y3 + ddy);
      if (t3 != null && pr3 != null) {
        tlx.time3 = t3;
        tlx.price3 = pr3;
      }
    }
    scheduleDrawTrendlines();
  }

  function onTlDragPointerMoveGlob(ev) {
    if (!tlRmbDrag || ev.pointerId !== tlRmbDrag.pointerId) return;
    var p = canvasPixelFromClient(ev.clientX, ev.clientY);
    if (p && (Math.abs(p.x - tlRmbDrag.downPx) > 1.5 || Math.abs(p.y - tlRmbDrag.downPy) > 1.5)) {
      tlRmbDrag.didMove = true;
    }
    onTlRmbDragMove(ev);
  }

  function onTlDragPointerEndGlob(ev) {
    if (!tlRmbDrag || ev.pointerId !== tlRmbDrag.pointerId) return;
    if (tlRmbDrag.didMove && tlRmbDrag.button === 0) tlSuppressSelectClick = true;
    endTlRmbDrag();
  }

  function bindTrendlineCanvas() {
    var canvas = getTlCanvas();
    if (!canvas || canvas.dataset.scTlBound === '1') return;
    canvas.dataset.scTlBound = '1';

    canvas.addEventListener('contextmenu', function (ev) {
      if (!chartAllowsObjectSelection() || !mainChart) return;
      var rect = canvas.getBoundingClientRect();
      var px = ev.clientX - rect.left;
      var py = ev.clientY - rect.top;
      var wrap = document.getElementById('sc-main-wrap');
      var wid = wrap ? wrap.clientWidth : 0;
      var selTl = selectedTlId ? findTrendlineById(selectedTlId) : null;
      var onHandle = selTl && hitTestTrendlineHandle(px, py, selTl);
      var hit = hitTestTrendlineIds(px, py, wid);
      if (onHandle || hit === selectedTlId || !!tlRmbDrag) ev.preventDefault();
    });

    canvas.addEventListener('pointerdown', function (ev) {
      var rect = canvas.getBoundingClientRect();
      var px = ev.clientX - rect.left;
      var py = ev.clientY - rect.top;
      var wrap = document.getElementById('sc-main-wrap');
      var wid = wrap ? wrap.clientWidth : 0;

      if (chartInteractionMode === MODE_OBJECTS && mainChart && candleSeries && tlAnchors.length === 0 && !drawingLevel) {
        if (ev.button !== 0) return;
        if (tlRmbDrag) return;
        if (hitTestTrendlineIds(px, py, wid) != null) return;
        if (selectedTlId) return;
        var ptDown = canvasPointToChart(ev);
        if (!ptDown) return;
        tlAnchors.push(ptDown);
        var primDown = canvasPixelFromClient(ev.clientX, ev.clientY);
        if (primDown) tlPreviewPx = primDown;
        attachTlDraftPreviewListeners();
        scheduleDrawTrendlines();
        tlSuppressNextDrawClick = true;
        applyChartInteractionMode();
        return;
      }
    });

    canvas.addEventListener('click', function (ev) {
      if (!mainChart || !candleSeries) return;

      if (chartInteractionMode === MODE_OBJECTS && drawingLevel) {
        if (tlSuppressSelectClick) {
          tlSuppressSelectClick = false;
          ev.stopPropagation();
          return;
        }
        var pt = canvasPointToChart(ev);
        if (!pt) return;
        levels.push({
          id: uid(),
          price: pt.price,
          color: '#cbd5e1',
          style: 'solid',
          width: 2,
          label: ''
        });
        restoreLevels();
        scheduleSaveAnnotations();
        drawingLevel = false;
        detachTlDraftPreviewListeners();
        setChartInteractionMode(MODE_NONE);
        ev.stopPropagation();
        return;
      }

      if (chartInteractionMode === MODE_OBJECTS && tlAnchors.length >= 1) {
        if (tlSuppressSelectClick) {
          tlSuppressSelectClick = false;
          ev.stopPropagation();
          return;
        }
        if (tlSuppressNextDrawClick) {
          tlSuppressNextDrawClick = false;
          ev.stopPropagation();
          return;
        }
        var pt = canvasPointToChart(ev);
        if (!pt) return;

        var plugin = drawingRegistry.get(getActiveDrawType());
        var pointsNeeded = plugin ? (plugin.pointsNeeded || 2) : 2;

        if (tlAnchors.length < pointsNeeded - 1) {
          tlAnchors.push(pt);
          scheduleDrawTrendlines();
          ev.stopPropagation();
          return;
        }

        var a0 = tlAnchors[0];
        var ax = timeToCoordinateExtrapolated(a0.time);
        var ay = candleSeries.priceToCoordinate(a0.price);
        var bx = timeToCoordinateExtrapolated(pt.time);
        var by = candleSeries.priceToCoordinate(pt.price);
        
        var newObj = {
          id: uid(),
          time1: a0.time,
          price1: a0.price,
          time2: tlAnchors.length >= 2 ? tlAnchors[1].time : pt.time,
          price2: tlAnchors.length >= 2 ? tlAnchors[1].price : pt.price,
          time3: tlAnchors.length >= 2 ? pt.time : null,
          price3: tlAnchors.length >= 2 ? pt.price : null,
          color: '#f59e0b',
          width: 1,
          style: 'solid',
          label: '',
          extended: false,
          type: getActiveDrawType()
        };

        if (tlPulseTimer) {
          clearTimeout(tlPulseTimer);
          tlPulseTimer = null;
        }
        tlAnchorPulse = (ax != null && ay != null && bx != null && by != null)
          ? [{ x: ax, y: ay }, { x: bx, y: by }]
          : null;
        tlAnchors = [];
        detachTlDraftPreviewListeners();
        trendlines.push(newObj);
        
        setSelectedTrendline(null);
        scheduleSaveAnnotations();
        scheduleDrawTrendlines();
        setChartInteractionMode(MODE_NONE);
        if (tlAnchorPulse) {
          tlPulseTimer = setTimeout(function () {
            tlPulseTimer = null;
            tlAnchorPulse = null;
            drawTrendlines();
          }, 450);
        }
        ev.stopPropagation();
        return;
      }

      if (!chartAllowsObjectSelection()) return;

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
      setSelectedTrendline(hit && hit === selectedTlId ? null : hit);
      ev.stopPropagation();
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

    if (chartInteractionMode !== MODE_CROSSHAIR) {
      tip.hidden = true;
      return;
    }

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
        var plugin = indicatorRegistry.get(ind.type);
        if (plugin && typeof plugin.getTooltip === 'function') {
          if (!ind.seriesObj) return;
          var tip = plugin.getTooltip(ind.seriesObj, param.seriesData, ind);
          if (tip) {
            var valStr = tip.value !== '' && tip.value != null ? fmtNum(tip.value) : '';
            parts.push(row(tip.label, valStr));
          }
        } else {
          if (!ind.series) return;
          var sd = param.seriesData.get(ind.series);
          if (sd && sd.value != null) {
            parts.push(row(ind.type + ind.period, fmtNum(sd.value)));
          }
        }
      });
    }

    if (tooltipPrefs.rsi) {
      var tRsi = mainCrosshairDataTime(param);
      var rv = rsiValueForCrosshairTime(tRsi);
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
    document.querySelectorAll('.sc-add-ind-btn').forEach(function(b) {
      b.disabled = full;
      b.title = full ? ('Maximum ' + MAX_MAIN_INDICATORS + ' indicators — remove one to add another') : '';
    });
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

  /** Remove every chip-backed overlay: MAs/EMAs, trendlines, and horizontal levels. */
  function removeAllIndicatorSeries() {
    indicators.forEach(function (ind) {
      detachIndicatorSeries(ind);
    });
  }

  function clearTrendlinesAndLevels() {
    selectedTlId = null;
    selectedLevelId = null;
    trendlines = [];
    drawTrendlines();
    levels = [];
    clearLevelPriceLines();
    scheduleSaveAnnotations();
  }

  function isDefaultChartState() {
    if (trendlines.length || levels.length) return false;
    if (indicators.length !== DEFAULT_MAIN_INDICATORS.length) return false;
    for (var i = 0; i < DEFAULT_MAIN_INDICATORS.length; i++) {
      var def = DEFAULT_MAIN_INDICATORS[i];
      var ind = indicators[i];
      if (!ind) return false;
      if (ind.type !== def.type || ind.period !== def.period) return false;
      if ((ind.color || '') !== def.color) return false;
      if ((Number(ind.lineWidth) || 2) !== def.lineWidth) return false;
      if ((ind.style || 'solid') !== def.style) return false;
      if (ind.visible === false) return false;
    }
    return true;
  }

  function clearAllChips() {
    removeAllIndicatorSeries();
    indicators = [];
    selectedIndId = null;
    clearTrendlinesAndLevels();
    refreshIndicators();
    renderChips();
  }

  /** Restore SMA 21/14 and remove trendlines, levels, and any extra indicators. */
  function resetToDefaultIndicators() {
    removeAllIndicatorSeries();
    indicators = createDefaultIndicators();
    selectedIndId = null;
    clearTrendlinesAndLevels();
    refreshIndicators();
    renderChips();
  }

  function renderChips() {
    var host = document.getElementById('sc-chips');
    if (!host) return;
    host.innerHTML = '';

    indicators.forEach(function (ind) {
      var chip = document.createElement('span');
      chip.className = 'sc-chip';
      if (ind.id === selectedIndId) chip.classList.add('sc-obj-selected');
      applyChipObjectColor(chip, ind.color, '#f59e0b');

      var isVisible = ind.visible !== false;
      if (!isVisible) chip.style.opacity = '0.55';
      var eyeHtml = '<span class="sc-chip-toggle-vis" style="cursor:pointer;margin-right:6px;opacity:' + (isVisible ? '1' : '0.4') + '" title="' + (isVisible ? 'Hide' : 'Show') + '">\uD83D\uDC41</span>';

      chip.innerHTML = '<span class="sc-chip-color-dot" aria-hidden="true"></span>' + eyeHtml + ind.type + ' ' + ind.period +
        '<span class="sc-chip-remove" data-ind="' + ind.id + '">\u00d7</span>';

      chip.addEventListener('click', function (e) {
        if (e.target.closest && e.target.closest('.sc-chip-remove')) return;
        if (e.target.closest && e.target.closest('.sc-chip-toggle-vis')) return;
        if (!chartAllowsObjectSelection()) return;
        setSelectedInd(ind.id === selectedIndId ? null : ind.id);
      });

      chip.querySelector('.sc-chip-toggle-vis').addEventListener('click', function (e) {
        e.stopPropagation();
        ind.visible = (ind.visible === false) ? true : false;
        if (ind.series) {
          try { ind.series.applyOptions({ visible: ind.visible }); } catch (_) {}
        }
        if (ind.seriesArr && ind.seriesArr.length) {
          ind.seriesArr.forEach(function (s) {
            try { s.applyOptions({ visible: ind.visible }); } catch (_) {}
          });
        }
        if (ind.glowSeries) {
          try { ind.glowSeries.applyOptions({ visible: ind.visible && selectedIndId === ind.id }); } catch (_) {}
        }
        syncIndicatorSelectionVisuals();
        scheduleSaveAnnotations();
        renderChips();
      });

      chip.querySelector('.sc-chip-remove').addEventListener('click', function (e) {
        e.stopPropagation();
        // Detach chart series before dropping this row from `indicators`; otherwise
        // refreshIndicators() never sees it and orphaned line series stay on the chart.
        detachIndicatorSeries(ind);
        indicators = indicators.filter(function (x) { return x.id !== ind.id; });
        if (selectedIndId === ind.id) selectedIndId = null;
        syncIndicatorSelectionVisuals();
        refreshIndicators();
        scheduleSaveAnnotations();
        renderChips();
      });
      host.appendChild(chip);
    });

    trendlines.forEach(function (tl, idx) {
      var chip = document.createElement('span');
      chip.className = 'sc-chip';
      if (tl.id === selectedTlId) chip.classList.add('sc-tl-selected');
      applyChipObjectColor(chip, tl.color, '#f59e0b');
      var drawingPlugin = drawingRegistry.get(normalizeDrawingType(tl.type));
      var tlLabel = drawingPlugin ? (drawingPlugin.chipLabel || drawingPlugin.name || drawingPlugin.id) : 'TL';
      chip.innerHTML = '<span class="sc-chip-color-dot" aria-hidden="true"></span>' + tlLabel + ' ' + (idx + 1) +
        '<span class="sc-chip-remove" data-tlid="' + tl.id + '">\u00d7</span>';
      chip.addEventListener('click', function (e) {
        if (e.target.closest && e.target.closest('.sc-chip-remove')) return;
        if (!chartAllowsObjectSelection()) {
          setChartInteractionMode(MODE_OBJECTS);
        }
        setSelectedTrendline(tl.id === selectedTlId ? null : tl.id);
      });
      chip.querySelector('.sc-chip-remove').addEventListener('click', function (e) {
        e.stopPropagation();
        var id = tl.id;
        if (selectedTlId === id) {
          selectedTlId = null;
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
      if (lv.id === selectedLevelId) chip.classList.add('sc-obj-selected');
      applyChipObjectColor(chip, lv.color, '#22c55e');
      chip.innerHTML = '<span class="sc-chip-color-dot" aria-hidden="true"></span>Lv ' + fmtNum(lv.price, 2) +
        '<span class="sc-chip-remove" data-lvid="' + (lv.id || '') + '">\u00d7</span>';
      chip.addEventListener('click', function (e) {
        if (e.target.closest && e.target.closest('.sc-chip-remove')) return;
        if (!chartAllowsObjectSelection()) return;
        setSelectedLevel(lv.id === selectedLevelId ? null : lv.id);
      });
      chip.querySelector('.sc-chip-remove').addEventListener('click', function (e) {
        e.stopPropagation();
        var id = lv.id;
        if (selectedLevelId === id) selectedLevelId = null;
        levels = levels.filter(function (x) { return x.id !== id; });
        if (levelPriceLines[id]) {
          try { candleSeries.removePriceLine(levelPriceLines[id]); } catch (_) {}
          delete levelPriceLines[id];
        }
        if (levelGlowPriceLines[id]) {
          try { candleSeries.removePriceLine(levelGlowPriceLines[id]); } catch (_) {}
          delete levelGlowPriceLines[id];
        }
        scheduleSaveAnnotations();
        renderChips();
      });
      host.appendChild(chip);
    });

    var hasOverlays = indicators.length || trendlines.length || levels.length;

    if (!isDefaultChartState()) {
      var resetDefaultsBtn = document.createElement('button');
      resetDefaultsBtn.type = 'button';
      resetDefaultsBtn.className = 'sc-chip-reset-defaults';
      resetDefaultsBtn.textContent = 'Reset defaults';
      resetDefaultsBtn.setAttribute('aria-label', 'Reset chart to default SMA 21 and SMA 14 indicators');
      resetDefaultsBtn.title = 'Remove trendlines and levels, and restore default SMA 21 and SMA 14 indicators';
      resetDefaultsBtn.addEventListener('click', function (e) {
        e.stopPropagation();
        resetToDefaultIndicators();
      });
      host.appendChild(resetDefaultsBtn);
    }

    if (hasOverlays) {
      var clearAllBtn = document.createElement('button');
      clearAllBtn.type = 'button';
      clearAllBtn.className = 'sc-chip-clear-all';
      clearAllBtn.textContent = 'Clear all';
      clearAllBtn.setAttribute('aria-label', 'Remove all indicators, trendlines, and price levels');
      clearAllBtn.title = 'Remove all indicators, trendlines, and price levels from the chart';
      clearAllBtn.addEventListener('click', function (e) {
        e.stopPropagation();
        clearAllChips();
      });
      host.appendChild(clearAllBtn);
    }

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
    return !!el.closest('#sc-tooltip-btn');
  }

  function setupGlobalUiHandlers() {
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') {
        closeAllPanels(null);
        clearObjectSelections();
        if (chartInteractionMode === MODE_OBJECTS || chartInteractionMode === MODE_CROSSHAIR) {
          setChartInteractionMode(MODE_NONE);
        }
        return;
      }
      if ((ev.key === 'Delete' || ev.key === 'Backspace') && chartAllowsObjectSelection()) {
        if (isTypingTarget(ev.target)) return;
        ev.preventDefault();
        if (selectedTlId) deleteSelectedTrendline();
        else if (selectedLevelId) deleteSelectedLevel();
        else if (selectedIndId) deleteSelectedInd();
        return;
      }
    });

    document.addEventListener('mousedown', function (ev) {
      var t = ev.target;
      if (t.closest && t.closest('.sc-panel')) return;
      if (isPanelOpenTrigger(t)) return;
      closeAllPanels(null);
    });
  }

  function populateIndicatorTypeSelect() {
    var sel = document.getElementById('sc-ind-type');
    if (!sel) return;
    var currentType = normalizeIndicatorType(sel.value || 'SMA');
    sel.innerHTML = '';
    var plugins = getAddableIndicatorPlugins();
    plugins.forEach(function (plugin) {
      var opt = document.createElement('option');
      opt.value = plugin.id;
      opt.textContent = plugin.id;
      if (plugin.id === currentType) opt.selected = true;
      sel.appendChild(opt);
    });
    if (!sel.options.length) {
      var fallback = document.createElement('option');
      fallback.value = 'SMA';
      fallback.textContent = 'SMA';
      sel.appendChild(fallback);
    }
  }

  function renderToolbarPluginButtons() {
    var host = document.getElementById('sc-plugin-toolbar');
    if (!host) return;
    host.innerHTML = '';

    function makeBtn(cls, label, title) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'sc-control-btn ' + cls;
      btn.textContent = label;
      if (title) btn.title = title;
      return btn;
    }

    getAddableIndicatorPlugins().forEach(function (plugin) {
      var label = '+ ' + indicatorAddLabel(plugin);
      var title = plugin.toolbarTitle || ('Add ' + indicatorAddLabel(plugin) + ' Indicator');
      var btn = makeBtn('sc-add-ind-btn', label, title);
      btn.setAttribute('data-ind-type', plugin.id);
      host.appendChild(btn);
    });

    getAddableDrawingPlugins().forEach(function (plugin) {
      var drawLabel = '+ ' + drawingAddLabel(plugin);
      var drawTitle = plugin.toolbarTitle || ('Add ' + drawingAddLabel(plugin));
      var btn = makeBtn('sc-draw-btn', drawLabel, drawTitle);
      btn.setAttribute('data-draw-type', plugin.id);
      btn.setAttribute('aria-pressed', 'false');
      host.appendChild(btn);
    });
  }

  function setupUI() {
    syncIntervalButtons();
    populateIndicatorTypeSelect();
    renderToolbarPluginButtons();
    buildTooltipSettingsPanel();
    buildObjectPropertyPanels();
    buildDefaultAddContextMenu();
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

    var indPanel = document.getElementById('sc-ind-panel');
    document.getElementById('sc-ind-cancel') && document.getElementById('sc-ind-cancel').addEventListener('click', function () {
      if (indPanel) indPanel.hidden = true;
    });
    document.getElementById('sc-ind-add-btn') && document.getElementById('sc-ind-add-btn').addEventListener('click', function () {
      if (indicators.length >= MAX_MAIN_INDICATORS) return;
      var typ = normalizeIndicatorType((document.getElementById('sc-ind-type') || {}).value || 'SMA');
      var period = Math.max(2, Math.min(200, parseInt(document.getElementById('sc-ind-period').value, 10) || 20));
      var color = (document.getElementById('sc-ind-color') || {}).value || '#f59e0b';
      addIndicatorFromContext(typ, { period: period, color: color });
      if (indPanel) indPanel.hidden = true;
    });

    var lvlPanel = document.getElementById('sc-lvl-panel');
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
      if (hasLevelAtPrice(price)) {
        setLvlError('A level at that price already exists.');
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

    var cxModeBtn = document.getElementById('sc-mode-crosshair');
    if (cxModeBtn) {
      cxModeBtn.addEventListener('click', function (ev) {
        ev.stopPropagation();
        closeAllPanels(null);
        setChartInteractionMode(MODE_CROSSHAIR);
      });
    }
    document.querySelectorAll('.sc-add-ind-btn').forEach(function (btn) {
      btn.addEventListener('click', function (ev) {
        ev.stopPropagation();
        closeAllPanels(null);
        var typeId = btn.getAttribute('data-ind-type');
        addIndicatorFromContext(typeId);
      });
    });
    
    document.querySelectorAll('.sc-draw-btn').forEach(function (btn) {
      btn.addEventListener('click', function (ev) {
        ev.stopPropagation();
        closeAllPanels(null);
        var drawType = btn.getAttribute('data-draw-type');
        
        if (drawType === 'LEVEL') {
          if (chartInteractionMode === MODE_OBJECTS && drawingLevel) {
            setChartInteractionMode(MODE_NONE);
          } else {
            beginLevelDrawing();
          }
        } else {
          var normalizedType = normalizeDrawingType(drawType);
          if (chartInteractionMode === MODE_OBJECTS && !drawingLevel && getActiveDrawType() === normalizedType) {
            setChartInteractionMode(MODE_NONE);
          } else {
            beginObjectDrawing(normalizedType);
          }
        }
      });
    });
    var volCb = document.getElementById('sc-show-vol');
    if (volCb) {
      volCb.checked = showVolume;
      volCb.addEventListener('change', function () {
        showVolume = volCb.checked;
        if (volSeries) volSeries.applyOptions({ visible: showVolume });
        scheduleSyncChartPaneSizes();
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
      if (saveTimer) {
        clearTimeout(saveTimer);
        saveTimer = null;
      }
      flushSaveAnnotations();
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

  if (!LW) { showStatus('Chart library not available.'); return; }
  initCharts();
  loadAnnotations(function () {
    loadHistory();
  });
  setupUI();
  renderChips();
}
