import { drawingRegistry } from '../drawingRegistry.js';
import { BaseDrawing } from '../basePlugin.js';
import { TRENDLINE_PROPERTIES } from '../propertyFields.js';

export class TrendlineDrawing extends BaseDrawing {
  constructor() {
    super({
      id: 'TRENDLINE',
      name: 'Trend Line',
      addLabel: 'Trendline',
      chipLabel: 'TL',
      pointsNeeded: 2,
      editableProperties: TRENDLINE_PROPERTIES
    });
  }

  drawShape(ctx, pixels) {
    ctx.beginPath();
    ctx.moveTo(pixels.x1, pixels.y1);
    ctx.lineTo(pixels.x2, pixels.y2);
    ctx.stroke();
    ctx.setLineDash([]);
  }
}

drawingRegistry.register(new TrendlineDrawing());
