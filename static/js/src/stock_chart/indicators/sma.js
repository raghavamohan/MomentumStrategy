import { indicatorRegistry } from '../indicatorRegistry.js';
import { createStandardIndicatorSeries, updateStandardIndicatorSeries, syncStandardSelection, getStandardTooltip } from '../visualUtils.js';

export const SmaIndicator = {
  id: 'SMA',
  name: 'Simple Moving Average',
  defaultOptions: { period: 21, color: '#f59e0b', lineWidth: 2, style: 'solid' },

  createSeries: function(chart, options) {
    return createStandardIndicatorSeries(chart, options);
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
    updateStandardIndicatorSeries(seriesObj, data);
  },

  syncSelection: function(seriesObj, selected, options) {
    syncStandardSelection(seriesObj, selected, options);
  },

  getTooltip: function(seriesObj, seriesDataMap, options) {
    return getStandardTooltip(seriesObj, seriesDataMap, 'SMA' + options.period);
  }
};

indicatorRegistry.register(SmaIndicator);
