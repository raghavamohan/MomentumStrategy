/** Shared chart page constants (no DOM). */

export const TP_STORAGE_KEY = 'sc_chart_tooltip_prefs';
export const RSI_SHOW_KEY = 'sc_chart_show_rsi';
export const RSI_PERIOD = 14;
export const MAX_MAIN_INDICATORS = 10;
export const SAVE_DEBOUNCE_MS = 500;
export const WS_RECONNECT_MS = 4000;
export const STALE_TICK_MS = 90000;
export const RIGHT_OFFSET_BARS = 30;

export const IV_CFG = {
  minute: { kite: 'minute', days: 60, label: '1m', timeVisible: true },
  '5minute': { kite: '5minute', days: 100, label: '5m', timeVisible: true },
  '15minute': { kite: '15minute', days: 200, label: '15m', timeVisible: true },
  '60minute': { kite: '60minute', days: 400, label: '1hr', timeVisible: true },
  day: { kite: 'day', days: 3650, label: '1D', timeVisible: false },
  week: { kite: 'day', days: 3650, label: '1W', timeVisible: false, agg: 'week' },
  month: { kite: 'day', days: 3650, label: '1M', timeVisible: false, agg: 'month' }
};

/** Default crosshair off (LightweightCharts.CrosshairMode.Hidden === 2). */
export const CHART_OPTS = {
  layout: { background: { color: '#0f172a' }, textColor: '#94a3b8' },
  grid: { vertLines: { color: 'rgba(148,163,184,.08)' }, horzLines: { color: 'rgba(148,163,184,.08)' } },
  crosshair: { mode: 2 },
  rightPriceScale: { borderColor: 'rgba(148,163,184,.15)' },
  timeScale: { borderColor: 'rgba(148,163,184,.15)', fixLeftEdge: true, rightOffset: RIGHT_OFFSET_BARS }
};

export const MODE_NONE = 'none';
export const MODE_CROSSHAIR = 'crosshair';
export const MODE_OBJECTS = 'objects';
