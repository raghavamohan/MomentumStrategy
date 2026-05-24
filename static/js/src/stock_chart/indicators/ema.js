import { indicatorRegistry } from '../indicatorRegistry.js';
import { createStandardIndicatorSeries, updateStandardIndicatorSeries, syncStandardSelection, getStandardTooltip } from '../visualUtils.js';

export const EmaIndicator = {
  id: 'EMA',
  name: 'Exponential Moving Average',
  defaultOptions: { period: 21, color: '#3b82f6', lineWidth: 2, style: 'solid' },

  createSeries: function(chart, options) {
    return createStandardIndicatorSeries(chart, options);
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
    updateStandardIndicatorSeries(seriesObj, data);
  },

  syncSelection: function(seriesObj, selected, options) {
    syncStandardSelection(seriesObj, selected, options);
  },

  getTooltip: function(seriesObj, seriesDataMap, options) {
    return getStandardTooltip(seriesObj, seriesDataMap, 'EMA' + options.period);
  }
};

indicatorRegistry.register(EmaIndicator);
