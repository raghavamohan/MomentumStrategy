import { drawingRegistry } from '../drawingRegistry.js';

export const ParallelChannelDrawing = {
  id: 'PARALLEL_CHANNEL',
  name: 'Parallel Channel',
  pointsNeeded: 3,
  draw: function(ctx, objectState, pixels, options) {
    var lw = Math.max(1, Number(objectState.width) || 1);
    var sel = options.selected;

    var px3 = pixels.x3;
    var py3 = pixels.y3;
    if (px3 == null || py3 == null) {
      px3 = pixels.x2;
      py3 = pixels.y2;
    }

    var dx = pixels.x2 - pixels.x1;
    var dy = pixels.y2 - pixels.y1;
    var px4 = px3 + dx;
    var py4 = py3 + dy;

    if (sel) {
      ctx.strokeStyle = 'rgba(255,255,255,.4)';
      ctx.lineWidth = lw + 8;
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.moveTo(pixels.x1, pixels.y1);
      ctx.lineTo(pixels.x2, pixels.y2);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(px3, py3);
      ctx.lineTo(px4, py4);
      ctx.stroke();
    }

    ctx.globalAlpha = 0.1;
    ctx.fillStyle = objectState.color || '#f59e0b';
    ctx.beginPath();
    ctx.moveTo(pixels.x1, pixels.y1);
    ctx.lineTo(pixels.x2, pixels.y2);
    ctx.lineTo(px4, py4);
    ctx.lineTo(px3, py3);
    ctx.closePath();
    ctx.fill();
    ctx.globalAlpha = 1.0;

    ctx.strokeStyle = objectState.color || '#f59e0b';
    ctx.lineWidth = sel ? lw + 1 : lw;
    ctx.setLineDash(objectState.style === 'dashed' ? [6, 4] : (objectState.style === 'dotted' ? [2, 4] : []));
    
    ctx.beginPath();
    ctx.moveTo(pixels.x1, pixels.y1);
    ctx.lineTo(pixels.x2, pixels.y2);
    ctx.stroke();
    
    ctx.beginPath();
    ctx.moveTo(px3, py3);
    ctx.lineTo(px4, py4);
    ctx.stroke();

    ctx.setLineDash([]);
  }
};

drawingRegistry.register(ParallelChannelDrawing);
