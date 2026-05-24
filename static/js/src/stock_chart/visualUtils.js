export function normalizeHexColor(color, fallback) {
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

export function rgbaFromHex(hex, alpha) {
  var norm = normalizeHexColor(hex);
  var h = norm.slice(1);
  var r = parseInt(h.slice(0, 2), 16);
  var g = parseInt(h.slice(2, 4), 16);
  var b = parseInt(h.slice(4, 6), 16);
  return 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')';
}

export function hexToRgba(hex, alpha) {
  if (!hex || typeof hex !== 'string') return 'rgba(34, 197, 94, ' + alpha + ')';
  if (hex.startsWith('#')) {
    if (hex.length === 4) hex = '#' + hex[1] + hex[1] + hex[2] + hex[2] + hex[3] + hex[3];
    var r = parseInt(hex.slice(1, 3), 16) || 0;
    var g = parseInt(hex.slice(3, 5), 16) || 0;
    var b = parseInt(hex.slice(5, 7), 16) || 0;
    return 'rgba(' + r + ', ' + g + ', ' + b + ', ' + alpha + ')';
  }
  return hex;
}

export function indicatorGlowColor(color) {
  if (typeof color !== 'string') return 'rgba(255,255,255,.28)';
  var h = color.trim();
  if (h.startsWith('hsl')) {
    return h.replace('hsl', 'hsla').replace(')', ', 0.33)');
  }
  return rgbaFromHex(color, 0.33);
}

export function styleToLw(s) {
  if (s === 'dotted') return 1;
  if (s === 'dashed' || s === 'largeDashed') return 2;
  return 0; // solid
}

export function styleToLineDash(s) {
  if (s === 'dashed') return [6, 4];
  if (s === 'dotted') return [2, 4];
  return []; // solid
}

export function fibBandColorMap(ind) {
  var userC = normalizeHexColor(ind.color, '#94a3b8');
  return {
    l1000: '#ef4444',
    l786: '#f59e0b',
    l618: '#22c55e',
    l500: userC,
    l382: '#3b82f6',
    l236: '#a855f7',
    l0: '#ef4444'
  };
}

// ---------------------------------------------------------
// Standard Lightweight Charts Helpers
// ---------------------------------------------------------

export function createStandardIndicatorSeries(chart, options) {
  var lineStyle = styleToLw(options.style);
  var lw = Math.max(1, Math.min(8, Number(options.lineWidth) || 2));

  var mainSeries = chart.addLineSeries({
    color: options.color || '#f59e0b',
    lineWidth: lw,
    lineStyle: lineStyle,
    crosshairMarkerVisible: false,
    lastValueVisible: false,
    priceLineVisible: false
  });
  
  var glowSeries = chart.addLineSeries({
    color: 'transparent',
    lineWidth: Math.min(16, lw + 5),
    lineStyle: 0,
    crosshairMarkerVisible: false,
    lastValueVisible: false,
    priceLineVisible: false
  });
  
  return { mainSeries: mainSeries, glowSeries: glowSeries };
}

export function updateStandardIndicatorSeries(seriesObj, data) {
  if (seriesObj.mainSeries) seriesObj.mainSeries.setData(data);
  if (seriesObj.glowSeries) seriesObj.glowSeries.setData(data);
}

export function syncStandardSelection(seriesObj, selected, options) {
  var lw = Math.max(1, Math.min(8, Number(options.lineWidth) || 2));
  var ls = styleToLw(options.style);
  
  if (seriesObj.mainSeries) {
    seriesObj.mainSeries.applyOptions({
      color: options.color || '#f59e0b',
      lineWidth: lw,
      lineStyle: ls,
      priceLineVisible: false,
      lastValueVisible: true,
      visible: options.visible !== false
    });
  }
  
  if (seriesObj.glowSeries) {
    seriesObj.glowSeries.applyOptions({
      color: selected ? indicatorGlowColor(options.color) : 'transparent',
      lineWidth: Math.min(16, lw + 5),
      lineStyle: 0,
      visible: !!selected && options.visible !== false
    });
  }
}

export function getStandardTooltip(seriesObj, seriesDataMap, labelText) {
  var sd = seriesDataMap.get(seriesObj.mainSeries);
  if (sd && sd.value != null) {
    return { label: labelText, value: sd.value };
  }
  return null;
}

// ---------------------------------------------------------
// Standard Canvas Drawing Helpers
// ---------------------------------------------------------

export function applyCanvasMainStyle(ctx, color, baseLineWidth, styleString) {
  ctx.strokeStyle = color || '#f59e0b';
  ctx.lineWidth = Math.max(1, Number(baseLineWidth) || 1);
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.setLineDash(styleToLineDash(styleString));
}

export function applyCanvasGlowStyle(ctx, color, baseLineWidth) {
  var lw = Math.max(1, Number(baseLineWidth) || 1);
  ctx.globalAlpha = 0.33;
  ctx.strokeStyle = color || '#f59e0b';
  ctx.lineWidth = Math.min(16, lw + 5);
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.setLineDash([]);
}
