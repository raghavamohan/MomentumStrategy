import { drawingRegistry } from '../drawingRegistry.js';

export const TrendlineDrawing = {
  id: 'TRENDLINE',
  name: 'Trend Line',
  pointsNeeded: 2,
  draw: function(ctx, objectState, pixels, options) {
    var lw = Math.max(1, Number(objectState.width) || 1);
    var sel = options.selected;
    
    if (sel) {
      ctx.strokeStyle = 'rgba(255,255,255,.4)';
      ctx.lineWidth = lw + 8;
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.moveTo(pixels.x1, pixels.y1);
      ctx.lineTo(pixels.x2, pixels.y2);
      ctx.stroke();
    }
    
    ctx.strokeStyle = objectState.color || '#f59e0b';
    ctx.lineWidth = sel ? lw + 2 : lw;
    ctx.setLineDash(objectState.style === 'dashed' ? [6, 4] : (objectState.style === 'dotted' ? [2, 4] : []));
    ctx.beginPath();
    ctx.moveTo(pixels.x1, pixels.y1);
    ctx.lineTo(pixels.x2, pixels.y2);
    ctx.stroke();
    ctx.setLineDash([]);
  }
};

drawingRegistry.register(TrendlineDrawing);
