import { indicatorRegistry } from '../indicatorRegistry.js';
import { createStandardIndicatorSeries, updateStandardIndicatorSeries, syncStandardSelection, getStandardTooltip } from '../visualUtils.js';

export const RsiIndicator = {
  id: 'RSI',
  name: 'Relative Strength Index',
  defaultOptions: { period: 14, color: '#c084fc', lineWidth: 2, style: 'solid' },
  isOscillator: true,
  userAddable: false,

  createSeries: function(chart, options) {
    return createStandardIndicatorSeries(chart, options);
  },

  calculate: function(bars, options) {
    var period = parseInt(options.period, 10);
    var out = [];
    var prefixCount = Math.min(period, bars.length);
    for (var k = 0; k < prefixCount; k++) {
      out.push({ time: bars[k].time });
    }
    if (bars.length < period + 1) return out;
    var gains = 0, losses = 0;
    for (var i = 1; i <= period; i++) {
      var d = bars[i].close - bars[i - 1].close;
      if (d >= 0) gains += d; else losses -= d;
    }
    var ag = gains / period, al = losses / period;
    out.push({ time: bars[period].time, value: al === 0 ? 100 : 100 - 100 / (1 + ag / al) });
    for (var i = period + 1; i < bars.length; i++) {
      var d2 = bars[i].close - bars[i - 1].close;
      ag = (ag * (period - 1) + Math.max(d2, 0)) / period;
      al = (al * (period - 1) + Math.max(-d2, 0)) / period;
      out.push({ time: bars[i].time, value: al === 0 ? 100 : 100 - 100 / (1 + ag / al) });
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
    return getStandardTooltip(seriesObj, seriesDataMap, 'RSI' + options.period);
  }
};

indicatorRegistry.register(RsiIndicator);
