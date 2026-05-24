import { indicatorRegistry } from '../indicatorRegistry.js';
import { BaseIndicator, hexToRgba, getStandardTooltip, resolveLineWidth } from '../basePlugin.js';
import { SUPERTREND_PROPERTIES } from '../propertyFields.js';
import { DEFAULT_LINE_WIDTH } from '../constants.js';

export class SuperTrendIndicator extends BaseIndicator {
  constructor() {
    super({
      id: 'SUPERTREND',
      name: 'SuperTrend',
      defaultOptions: { period: 20, multiplier: 3, upColor: '#22c55e', downColor: '#ef4444', lineWidth: DEFAULT_LINE_WIDTH, style: 'solid' },
      editableProperties: SUPERTREND_PROPERTIES
    });
  }

  createSeries(chart, options) {
    var lineStyle = options.style === 'dashed' ? 2 : (options.style === 'dotted' ? 3 : 0);
    var lw = resolveLineWidth(options.lineWidth);
    var mainSeries = chart.addLineSeries({
      color: options.upColor || '#22c55e',
      lineWidth: lw,
      lineStyle: lineStyle,
      crosshairMarkerVisible: false,
      lastValueVisible: false,
      priceLineVisible: false
    });
    var glowSeries = chart.addLineSeries({
      color: 'transparent',
      lineWidth: Math.min(16, lw + 5),
      visible: false,
      crosshairMarkerVisible: false,
      lastValueVisible: false,
      priceLineVisible: false
    });
    return { mainSeries: mainSeries, glowSeries: glowSeries };
  }

  calculate(bars, options) {
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
      var glowPt = { time: bars[i].time, value: superTrend };
      
      out.mainData.push(pt);
      out.tooltipData.push(pt);
      out.glowData.push(glowPt);

      prevFinalUpperBand = finalUpperBand;
      prevFinalLowerBand = finalLowerBand;
      prevDir = dir;
    }

    return out;
  }

  updateSeries(seriesObj, data) {
    if (seriesObj.mainSeries) seriesObj.mainSeries.setData(data.mainData);
    if (seriesObj.glowSeries) seriesObj.glowSeries.setData(data.glowData || []);
  }

  getTooltip(seriesObj, seriesDataMap, options) {
    return getStandardTooltip(seriesObj, seriesDataMap, 'SuperTrend(' + options.period + ',' + options.multiplier + ')');
  }

  getPrimarySeriesData(data) {
    if (data && Array.isArray(data.tooltipData)) return data.tooltipData;
    return [];
  }

  syncSelection(seriesObj, selected, options) {
    var lw = resolveLineWidth(options.lineWidth);
    var lineStyle = options.style === 'dashed' ? 2 : (options.style === 'dotted' ? 3 : 0);

    if (seriesObj.mainSeries) {
      seriesObj.mainSeries.applyOptions({
        lineWidth: lw,
        lineStyle: lineStyle,
        visible: options.visible !== false,
        lastValueVisible: true,
        priceLineVisible: false
      });
    }

    if (seriesObj.glowSeries) {
      var seriesColor = hexToRgba(options.color || options.upColor || '#22c55e', 0.33);
      seriesObj.glowSeries.applyOptions({
        color: selected ? seriesColor : 'transparent',
        lineWidth: Math.min(16, lw + 5),
        lineStyle: 0,
        visible: !!selected && options.visible !== false,
        crosshairMarkerVisible: false,
        lastValueVisible: false,
        priceLineVisible: false
      });
    }
  }
}

indicatorRegistry.register(new SuperTrendIndicator());
