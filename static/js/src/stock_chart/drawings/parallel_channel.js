import { drawingRegistry } from '../drawingRegistry.js';
import { BaseDrawing } from '../basePlugin.js';
import { DRAWING_STD_PROPERTIES } from '../propertyFields.js';

export class ParallelChannelDrawing extends BaseDrawing {
  constructor() {
    super({
      id: 'PARALLEL_CHANNEL',
      name: 'Parallel Channel',
      addLabel: 'Channel',
      chipLabel: 'Channel',
      pointsNeeded: 3,
      editableProperties: DRAWING_STD_PROPERTIES
    });
  }

  drawBackground(ctx, objectState, pixels) {
    var dx = pixels.x2 - pixels.x1;
    var dy = pixels.y2 - pixels.y1;
    var px3 = pixels.x3 != null ? pixels.x3 : pixels.x2;
    var py3 = pixels.y3 != null ? pixels.y3 : pixels.y2;
    var px4 = px3 + dx;
    var py4 = py3 + dy;

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
  }

  drawShape(ctx, pixels) {
    var dx = pixels.x2 - pixels.x1;
    var dy = pixels.y2 - pixels.y1;
    var px3 = pixels.x3 != null ? pixels.x3 : pixels.x2;
    var py3 = pixels.y3 != null ? pixels.y3 : pixels.y2;
    var px4 = px3 + dx;
    var py4 = py3 + dy;

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
}

drawingRegistry.register(new ParallelChannelDrawing());
