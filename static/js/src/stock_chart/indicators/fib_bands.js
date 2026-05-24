import { indicatorRegistry } from '../indicatorRegistry.js';
import { fibBandColorMap, indicatorGlowColor, styleToLw } from '../visualUtils.js';

export const FibBandsIndicator = {
  id: 'FIB',
  name: 'Rolling Fibonacci Bands',
  addLabel: 'Fib Bands',
  periodLabel: 'Lookback (candles)',
  bandKeys: ['l1000', 'l786', 'l618', 'l500', 'l382', 'l236', 'l0'],
  defaultOptions: { period: 20, color: '#94a3b8', lineWidth: 1, style: 'dashed' },

  createSeries: function(chart, options) {
    var bands = ['l1000', 'l786', 'l618', 'l500', 'l382', 'l236', 'l0'];
    var lineStyle = styleToLw(options.style);
    
    var seriesObj = {
      bands: {},
      glowBands: {},
      mainSeries: null
    };

    bands.forEach(function(band) {
      seriesObj.bands[band] = chart.addLineSeries({
        color: options.color,
        lineWidth: options.lineWidth,
        lineStyle: lineStyle,
        crosshairMarkerVisible: false,
        lastValueVisible: false,
        priceLineVisible: false
      });
      seriesObj.glowBands[band] = chart.addLineSeries({
        color: 'transparent',
        lineWidth: Math.max(12, options.lineWidth + 8),
        lineStyle: 0,
        crosshairMarkerVisible: false,
        lastValueVisible: false,
        priceLineVisible: false
      });
      if (!seriesObj.mainSeries) seriesObj.mainSeries = seriesObj.bands[band];
    });

    return seriesObj;
  },

  calculate: function(bars, options) {
    var period = parseInt(options.period, 10);
    var out = { l0: [], l236: [], l382: [], l500: [], l618: [], l786: [], l1000: [] };
    
    for (var i = 0; i < bars.length; i++) {
      if (i < period - 1) continue;
      var maxH = -Infinity;
      var minL = Infinity;
      for (var j = i - period + 1; j <= i; j++) {
        if (bars[j].high > maxH) maxH = bars[j].high;
        if (bars[j].low < minL) minL = bars[j].low;
      }
      var diff = maxH - minL;
      var t = bars[i].time;
      out.l0.push({ time: t, value: minL });
      out.l236.push({ time: t, value: minL + diff * 0.236 });
      out.l382.push({ time: t, value: minL + diff * 0.382 });
      out.l500.push({ time: t, value: minL + diff * 0.500 });
      out.l618.push({ time: t, value: minL + diff * 0.618 });
      out.l786.push({ time: t, value: minL + diff * 0.786 });
      out.l1000.push({ time: t, value: maxH });
    }
    return out;
  },

  updateSeries: function(seriesObj, data) {
    var bands = ['l1000', 'l786', 'l618', 'l500', 'l382', 'l236', 'l0'];
    bands.forEach(function(band) {
      if (seriesObj.bands[band]) seriesObj.bands[band].setData(data[band] || []);
      if (seriesObj.glowBands[band]) seriesObj.glowBands[band].setData(data[band] || []);
    });
  },

  syncSelection: function(seriesObj, selected, options) {
    var bands = ['l1000', 'l786', 'l618', 'l500', 'l382', 'l236', 'l0'];
    var lw = Math.max(1, Math.min(8, Number(options.lineWidth) || 1));
    var ls = styleToLw(options.style);
    var colors = fibBandColorMap(options);

    bands.forEach(function(band) {
      if (seriesObj.bands[band]) {
        seriesObj.bands[band].applyOptions({
          color: colors[band] || options.color || '#f59e0b',
          lineWidth: lw,
          lineStyle: ls,
          visible: options.visible !== false,
          lastValueVisible: true,
          priceLineVisible: false
        });
      }
      if (seriesObj.glowBands[band]) {
        seriesObj.glowBands[band].applyOptions({
          color: selected ? indicatorGlowColor(colors[band] || options.color) : 'transparent',
          lineWidth: Math.min(16, lw + 5),
          lineStyle: 0,
          visible: !!selected && options.visible !== false
        });
      }
    });
  },

  getTooltip: function(seriesObj, seriesDataMap, options) {
    return { label: 'FIB ' + options.period, value: '' };
  }
};

indicatorRegistry.register(FibBandsIndicator);
