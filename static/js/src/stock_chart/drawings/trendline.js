import { drawingRegistry } from '../drawingRegistry.js';
import { applyCanvasMainStyle, applyCanvasGlowStyle } from '../visualUtils.js';

export const TrendlineDrawing = {
  id: 'TRENDLINE',
  name: 'Trend Line',
  addLabel: 'Trendline',
  chipLabel: 'TL',
  pointsNeeded: 2,
  draw: function(ctx, objectState, pixels, options) {
    var sel = options.selected;
    var baseLw = objectState.width;
    
    applyCanvasMainStyle(ctx, objectState.color, baseLw, objectState.style);
    ctx.beginPath();
    ctx.moveTo(pixels.x1, pixels.y1);
    ctx.lineTo(pixels.x2, pixels.y2);
    ctx.stroke();
    ctx.setLineDash([]);
    
    if (sel) {
      ctx.save();
      applyCanvasGlowStyle(ctx, objectState.color, baseLw);
      ctx.beginPath();
      ctx.moveTo(pixels.x1, pixels.y1);
      ctx.lineTo(pixels.x2, pixels.y2);
      ctx.stroke();
      ctx.restore();
    }
  }
};

drawingRegistry.register(TrendlineDrawing);
