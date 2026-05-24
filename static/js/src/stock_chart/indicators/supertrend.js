import { indicatorRegistry } from '../indicatorRegistry.js';

export const SuperTrendIndicator = {
  id: 'SUPERTREND',
  name: 'SuperTrend',
  defaultOptions: { period: 20, multiplier: 3, upColor: '#22c55e', downColor: '#ef4444', lineWidth: 2, style: 'solid' },

  createSeries: function(chart, options) {
    var lineStyle = options.style === 'dashed' ? 2 : (options.style === 'dotted' ? 3 : 0);
    var mainSeries = chart.addLineSeries({
      color: options.upColor || '#22c55e', // default fallback color
      lineWidth: options.lineWidth,
      lineStyle: lineStyle,
      crosshairMarkerVisible: false,
      lastValueVisible: false,
      priceLineVisible: false
    });
    var glowSeries = chart.addLineSeries({
      color: 'transparent',
      lineWidth: Math.max(12, options.lineWidth + 8),
      crosshairMarkerVisible: false,
      lastValueVisible: false,
      priceLineVisible: false
    });
    return { mainSeries: mainSeries, glowSeries: glowSeries };
  },

  calculate: function(bars, options) {
    var period = parseInt(options.period, 10);
    var multiplier = parseFloat(options.multiplier);
    var out = { mainData: [], tooltipData: [] };

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
      
      // Wilder's Smoothing (RMA) for ATR
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
        // In Lightweight Charts, the 'color' property of a point colors the segment
        // from that point to the NEXT point.
        // When the trend flips, we want the segment from the PREVIOUS point to THIS point
        // to be invisible. So we must retroactively change the previous point's color.
        out.mainData[out.mainData.length - 1].color = 'transparent';
      }
      
      var pt = { time: bars[i].time, value: superTrend, color: pointColor };
      
      out.mainData.push(pt);
      out.tooltipData.push(pt);

      prevFinalUpperBand = finalUpperBand;
      prevFinalLowerBand = finalLowerBand;
      prevDir = dir;
    }

    return out;
  },

  updateSeries: function(seriesObj, data) {
    if (seriesObj.mainSeries) seriesObj.mainSeries.setData(data.mainData);
    if (seriesObj.glowSeries) seriesObj.glowSeries.setData(data.tooltipData);
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
    var finalLw = selected ? Math.min(8, lw + 2) : lw;
    var lineStyle = options.style === 'dashed' ? 2 : (options.style === 'dotted' ? 3 : 0);

    if (seriesObj.mainSeries) {
      seriesObj.mainSeries.applyOptions({
        lineWidth: finalLw,
        lineStyle: lineStyle
      });
    }
    if (seriesObj.glowSeries) {
      seriesObj.glowSeries.applyOptions({
        color: selected ? 'rgba(245, 158, 11, 0.33)' : 'transparent',
        lineWidth: Math.max(12, finalLw + 8)
      });
    }
  }
};

indicatorRegistry.register(SuperTrendIndicator);
