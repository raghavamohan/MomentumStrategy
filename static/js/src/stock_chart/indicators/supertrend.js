import { indicatorRegistry } from '../indicatorRegistry.js';

export const SuperTrendIndicator = {
  id: 'SUPERTREND',
  name: 'SuperTrend',
  defaultOptions: { period: 20, multiplier: 3, upColor: '#22c55e', downColor: '#ef4444', lineWidth: 2, style: 'solid' },

  createSeries: function(chart, options) {
    var lineStyle = options.style === 'dashed' ? 2 : (options.style === 'dotted' ? 3 : 0);
    // SuperTrend often changes color based on trend, but lineSeries only has a single color.
    // To support up/down colors, we can either use two line series, or let Lightweight Charts 
    // handle it if it supports per-bar colors for lines (it does not directly, only for some series types).
    // The easiest way is to use two line series.
    var upSeries = chart.addLineSeries({
      color: options.upColor || '#22c55e',
      lineWidth: options.lineWidth,
      lineStyle: lineStyle,
      crosshairMarkerVisible: false,
      lastValueVisible: false,
      priceLineVisible: false
    });
    var downSeries = chart.addLineSeries({
      color: options.downColor || '#ef4444',
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
    return { mainSeries: upSeries, upSeries, downSeries, glowSeries };
  },

  calculate: function(bars, options) {
    var period = parseInt(options.period, 10);
    var multiplier = parseFloat(options.multiplier);
    var out = { upData: [], downData: [], tooltipData: [] };

    if (bars.length < period) return out;

    var atrVals = [];
    // Calculate True Range and ATR
    for (var i = 1; i < bars.length; i++) {
      var tr = Math.max(
        bars[i].high - bars[i].low,
        Math.abs(bars[i].high - bars[i - 1].close),
        Math.abs(bars[i].low - bars[i - 1].close)
      );
      atrVals.push(tr);
    }

    var atr = 0;
    var finalUpperBand = 0;
    var finalLowerBand = 0;
    var superTrend = 0;
    var prevSuperTrend = 0;
    var prevFinalUpperBand = 0;
    var prevFinalLowerBand = 0;
    var dir = 1;

    for (var i = period; i < bars.length; i++) {
      var trSum = 0;
      for (var j = i - period; j < i; j++) {
        trSum += atrVals[j];
      }
      atr = trSum / period;

      var basicUpperBand = (bars[i].high + bars[i].low) / 2 + multiplier * atr;
      var basicLowerBand = (bars[i].high + bars[i].low) / 2 - multiplier * atr;

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

      if (superTrend === prevFinalUpperBand && bars[i].close <= finalUpperBand) {
        dir = -1;
      } else if (superTrend === prevFinalUpperBand && bars[i].close > finalUpperBand) {
        dir = 1;
      } else if (superTrend === prevFinalLowerBand && bars[i].close >= finalLowerBand) {
        dir = 1;
      } else if (superTrend === prevFinalLowerBand && bars[i].close < finalLowerBand) {
        dir = -1;
      }

      if (dir === 1) superTrend = finalLowerBand;
      else superTrend = finalUpperBand;

      out.tooltipData.push({ time: bars[i].time, value: superTrend });
      if (dir === 1) {
        out.upData.push({ time: bars[i].time, value: superTrend });
      } else {
        out.downData.push({ time: bars[i].time, value: superTrend });
      }

      prevSuperTrend = superTrend;
      prevFinalUpperBand = finalUpperBand;
      prevFinalLowerBand = finalLowerBand;
    }

    return out;
  },

  updateSeries: function(seriesObj, data) {
    if (seriesObj.upSeries) seriesObj.upSeries.setData(data.upData);
    if (seriesObj.downSeries) seriesObj.downSeries.setData(data.downData);
    if (seriesObj.glowSeries) seriesObj.glowSeries.setData(data.tooltipData);
  },

  getTooltip: function(seriesObj, seriesDataMap, options) {
    var sd1 = seriesDataMap.get(seriesObj.upSeries);
    var sd2 = seriesDataMap.get(seriesObj.downSeries);
    var val = (sd1 && sd1.value != null) ? sd1.value : (sd2 && sd2.value != null ? sd2.value : null);
    if (val != null) {
      return { label: 'SuperTrend(' + options.period + ',' + options.multiplier + ')', value: val };
    }
    return null;
  }
};

indicatorRegistry.register(SuperTrendIndicator);
