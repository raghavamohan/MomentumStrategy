export class DrawingRegistry {
  constructor() {
    this.drawings = new Map();
  }

  register(drawingDef) {
    if (!drawingDef || !drawingDef.id) {
      throw new Error("Invalid drawing definition");
    }
    this.drawings.set(drawingDef.id, drawingDef);
  }

  get(id) {
    return this.drawings.get(id);
  }

  getAll() {
    return Array.from(this.drawings.values());
  }
}

export const drawingRegistry = new DrawingRegistry();
