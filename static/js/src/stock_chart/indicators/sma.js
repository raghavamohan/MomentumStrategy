import { indicatorRegistry } from '../indicatorRegistry.js';

export const SmaIndicator = {
  id: 'SMA',
  name: 'Simple Moving Average',
  defaultOptions: { period: 21, color: '#f59e0b', lineWidth: 2, style: 'solid' },

  createSeries: function(chart, options) {
    var lineStyle = options.style === 'dashed' ? 2 : (options.style === 'dotted' ? 3 : 0);
    var mainSeries = chart.addLineSeries({
      color: options.color,
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
    return { mainSeries, glowSeries };
  },

  calculate: function(bars, options) {
    var out = [];
    var period = parseInt(options.period, 10);
    for (var i = 0; i < bars.length; i++) {
      if (i < period - 1) continue;
      var sum = 0;
      for (var j = i - period + 1; j <= i; j++) sum += bars[j].close;
      out.push({ time: bars[i].time, value: sum / period });
    }
    return out;
  },

  updateSeries: function(seriesObj, data) {
    if (seriesObj.mainSeries) seriesObj.mainSeries.setData(data);
    if (seriesObj.glowSeries) seriesObj.glowSeries.setData(data);
  },

  getTooltip: function(seriesObj, seriesDataMap, options) {
    var sd = seriesDataMap.get(seriesObj.mainSeries);
    if (sd && sd.value != null) {
      return { label: 'SMA' + options.period, value: sd.value };
    }
    return null;
  }
};

indicatorRegistry.register(SmaIndicator);
