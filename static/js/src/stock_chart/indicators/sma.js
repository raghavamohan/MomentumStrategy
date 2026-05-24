import { indicatorRegistry } from '../indicatorRegistry.js';
import { BaseIndicator } from '../basePlugin.js';
import { INDICATOR_STD_PROPERTIES } from '../propertyFields.js';
import { DEFAULT_LINE_WIDTH } from '../constants.js';

export class SmaIndicator extends BaseIndicator {
  constructor() {
    super({
      id: 'SMA',
      name: 'Simple Moving Average',
      defaultOptions: { period: 21, color: '#f59e0b', lineWidth: DEFAULT_LINE_WIDTH, style: 'solid' },
      editableProperties: INDICATOR_STD_PROPERTIES
    });
  }

  calculate(bars, options) {
    var out = [];
    var period = parseInt(options.period, 10);
    for (var i = 0; i < bars.length; i++) {
      if (i < period - 1) continue;
      var sum = 0;
      for (var j = i - period + 1; j <= i; j++) sum += bars[j].close;
      out.push({ time: bars[i].time, value: sum / period });
    }
    return out;
  }
}

indicatorRegistry.register(new SmaIndicator());
