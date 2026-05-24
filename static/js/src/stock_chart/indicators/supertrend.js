import { indicatorRegistry } from '../indicatorRegistry.js';

export const SuperTrendIndicator = {
  id: 'SUPERTREND',
  name: 'SuperTrend',
  defaultOptions: { period: 20, multiplier: 3, upColor: '#22c55e', downColor: '#ef4444', lineWidth: 2, style: 'solid' },

  createSeries: function(chart, options) {
    var lineStyle = options.style === 'dashed' ? 2 : (options.style === 'dotted' ? 3 : 0);
    var mainSeries = chart.addLineSeries({
      color: options.upColor || '#22c55e',
      lineWidth: options.lineWidth,
      lineStyle: lineStyle,
      crosshairMarkerVisible: false,
      lastValueVisible: false,
      priceLineVisible: false
    });
    var glowSeries = chart.addLineSeries({
      color: 'transparent',
      lineWidth: Math.min(16, options.lineWidth + 5),
      visible: false,
      crosshairMarkerVisible: false,
      lastValueVisible: false,
      priceLineVisible: false
    });
    return { mainSeries: mainSeries, glowSeries: glowSeries };
  },

  calculate: function(bars, options) {
    var period = parseInt(options.period, 10);
    var multiplier = parseFloat(options.multiplier);
    var out = { mainData: [], tooltipData: [], glowData: [] };

    if (bars.length <= period) return out;

    var trueRanges = [];
    for (var i = 1; i < bars.length; i++) {
      var tr = Math.max(
        bars[i].high - bars[i].low,
        Math.abs(bars[i].high - bars[i - 1].close),
        Math.abs(bars[i].low - bars[i - 1].close)
      );
      trueRanges.push(tr);
    }

    var initialTrSum = 0;
    for (var j = 0; j < period; j++) {
      initialTrSum += trueRanges[j];
    }
    var atr = initialTrSum / period;

    var prevFinalUpperBand = 0;
    var prevFinalLowerBand = 0;
    var dir = 1;
    var prevDir = 1;

    for (var i = period; i < bars.length; i++) {
      var currentTr = trueRanges[i - 1];
      
      if (i > period) {
        atr = (atr * (period - 1) + currentTr) / period;
      }

      var hl2 = (bars[i].high + bars[i].low) / 2;
      var basicUpperBand = hl2 + multiplier * atr;
      var basicLowerBand = hl2 - multiplier * atr;
      
      var finalUpperBand, finalLowerBand;

      if (i === period) {
         finalUpperBand = basicUpperBand;
         finalLowerBand = basicLowerBand;
         dir = bars[i].close > finalUpperBand ? 1 : -1;
      } else {
        if (basicUpperBand < prevFinalUpperBand || bars[i - 1].close > prevFinalUpperBand) {
          finalUpperBand = basicUpperBand;
        } else {
          finalUpperBand = prevFinalUpperBand;
        }

        if (basicLowerBand > prevFinalLowerBand || bars[i - 1].close < prevFinalLowerBand) {
          finalLowerBand = basicLowerBand;
        } else {
          finalLowerBand = prevFinalLowerBand;
        }
        
        if (dir === -1 && bars[i].close > finalUpperBand) {
          dir = 1;
        } else if (dir === 1 && bars[i].close < finalLowerBand) {
          dir = -1;
        }
      }

      var superTrend = (dir === 1) ? finalLowerBand : finalUpperBand;
      var pointColor = (dir === 1) ? (options.upColor || '#22c55e') : (options.downColor || '#ef4444');
      
      if (dir !== prevDir && i > period && out.mainData.length > 0) {
        out.mainData[out.mainData.length - 1].color = 'transparent';
        if (out.glowData.length > 0) {
          out.glowData[out.glowData.length - 1].color = 'transparent';
        }
      }
      
      var pt = { time: bars[i].time, value: superTrend, color: pointColor };
      // glowPt has NO per-point color normally, falling back to series color
      var glowPt = { time: bars[i].time, value: superTrend };
      
      out.mainData.push(pt);
      out.tooltipData.push(pt);
      out.glowData.push(glowPt);

      prevFinalUpperBand = finalUpperBand;
      prevFinalLowerBand = finalLowerBand;
      prevDir = dir;
    }

    return out;
  },

  updateSeries: function(seriesObj, data) {
    if (seriesObj.mainSeries) seriesObj.mainSeries.setData(data.mainData);
    if (seriesObj.glowSeries) seriesObj.glowSeries.setData(data.glowData || []);
  },

  getTooltip: function(seriesObj, seriesDataMap, options) {
    var sd = seriesDataMap.get(seriesObj.mainSeries);
    var val = (sd && sd.value != null) ? sd.value : null;
    if (val != null) {
      return { label: 'SuperTrend(' + options.period + ',' + options.multiplier + ')', value: val };
    }
    return null;
  },

  getPrimarySeriesData: function(data) {
    if (data && Array.isArray(data.tooltipData)) return data.tooltipData;
    return [];
  },

  syncSelection: function(seriesObj, selected, options) {
    var lw = options.lineWidth || 2;
    var lineStyle = options.style === 'dashed' ? 2 : (options.style === 'dotted' ? 3 : 0);

    function hexToRgba(hex, alpha) {
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

    if (seriesObj.mainSeries) {
      seriesObj.mainSeries.applyOptions({
        lineWidth: lw, // Do NOT thicken the main line on selection!
        lineStyle: lineStyle
      });
    }
    if (seriesObj.glowSeries) {
      var seriesColor = hexToRgba(options.color || options.upColor || '#22c55e', 0.33);
      seriesObj.glowSeries.applyOptions({
        color: selected ? seriesColor : 'transparent',
        visible: !!selected,
        lineWidth: Math.min(16, lw + 5)
      });
    }
  }
};

indicatorRegistry.register(SuperTrendIndicator);
