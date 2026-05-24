import { drawingRegistry } from '../drawingRegistry.js';
import { applyCanvasMainStyle, applyCanvasGlowStyle } from '../visualUtils.js';

export const FibRetracementDrawing = {
  id: 'FIB',
  name: 'Fib Retracement',
  addLabel: 'Fib Retracement',
  chipLabel: 'Fib',
  hitTestMode: 'horizontalLevels',
  pointsNeeded: 2,
  draw: function(ctx, objectState, pixels, options) {
    var sel = !!options.selected;
    var lw = Math.max(1, Number(objectState.width) || 1);
    var diffY = pixels.y2 - pixels.y1;
    var fibs = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
    var colorMap = ['#f59e0b', '#a855f7', '#3b82f6', '#ef4444', '#22c55e', '#94a3b8', '#f59e0b'];

    ctx.strokeStyle = sel ? 'rgba(255,255,255,.35)' : 'rgba(148,163,184,.25)';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(pixels.x1, pixels.y1);
    ctx.lineTo(pixels.x2, pixels.y2);
    ctx.stroke();
    ctx.setLineDash([]);

    fibs.forEach(function (pct, i) {
      var yy = pixels.y1 + diffY * pct;
      var color = colorMap[i] || '#f59e0b';
      var baseLw = objectState.width;

      applyCanvasMainStyle(ctx, color, baseLw, objectState.style);
      ctx.globalAlpha = sel ? 0.85 : 0.55;
      ctx.beginPath();
      ctx.moveTo(0, yy);
      ctx.lineTo(options.width || 1000, yy);
      ctx.stroke();
      ctx.globalAlpha = 1.0;

      if (sel) {
        ctx.save();
        applyCanvasGlowStyle(ctx, color, baseLw);
        ctx.beginPath();
        ctx.moveTo(0, yy);
        ctx.lineTo(options.width || 1000, yy);
        ctx.stroke();
        ctx.restore();
      }

      if (options.showLabels !== false) {
        ctx.fillStyle = color;
        ctx.font = '10px sans-serif';
        ctx.fillText((pct * 100).toFixed(1) + '%', 10, yy - 4);
      }
    });
  }
};

drawingRegistry.register(FibRetracementDrawing);
