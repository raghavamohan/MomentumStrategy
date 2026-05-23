import { indicatorRegistry } from '../indicatorRegistry.js';

export const EmaIndicator = {
  id: 'EMA',
  name: 'Exponential Moving Average',
  defaultOptions: { period: 21, color: '#3b82f6', lineWidth: 2, style: 'solid' },

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
    var k = 2 / (period + 1);
    var ema = 0;
    var started = false;

    for (var i = 0; i < bars.length; i++) {
      if (i < period - 1) continue;
      if (!started) {
        var s = 0;
        for (var j = 0; j < period; j++) s += bars[j].close;
        ema = s / period;
        started = true;
      } else {
        ema = bars[i].close * k + ema * (1 - k);
      }
      out.push({ time: bars[i].time, value: ema });
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
      return { label: 'EMA' + options.period, value: sd.value };
    }
    return null;
  }
};

indicatorRegistry.register(EmaIndicator);
