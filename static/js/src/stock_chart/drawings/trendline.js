import { drawingRegistry } from '../drawingRegistry.js';

export const TrendlineDrawing = {
  id: 'TRENDLINE',
  name: 'Trend Line',
  addLabel: 'Trendline',
  chipLabel: 'TL',
  pointsNeeded: 2,
  draw: function(ctx, objectState, pixels, options) {
    var lw = Math.max(1, Number(objectState.width) || 1);
    var sel = options.selected;
    
    ctx.strokeStyle = objectState.color || '#f59e0b';
    ctx.lineWidth = lw;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.setLineDash(objectState.style === 'dashed' ? [6, 4] : (objectState.style === 'dotted' ? [2, 4] : []));
    ctx.beginPath();
    ctx.moveTo(pixels.x1, pixels.y1);
    ctx.lineTo(pixels.x2, pixels.y2);
    ctx.stroke();
    ctx.setLineDash([]);
    
    if (sel) {
      ctx.save();
      ctx.globalAlpha = 0.33;
      ctx.strokeStyle = objectState.color || '#f59e0b';
      ctx.lineWidth = Math.min(16, lw + 5);
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.moveTo(pixels.x1, pixels.y1);
      ctx.lineTo(pixels.x2, pixels.y2);
      ctx.stroke();
      ctx.restore();
    }
  }
};

drawingRegistry.register(TrendlineDrawing);
