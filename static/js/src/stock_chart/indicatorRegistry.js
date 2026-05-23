export class IndicatorRegistry {
  constructor() {
    this.indicators = new Map();
  }
  
  register(indicatorDef) {
    if (!indicatorDef || !indicatorDef.id) {
      throw new Error("Invalid indicator definition");
    }
    this.indicators.set(indicatorDef.id, indicatorDef);
  }

  get(id) {
    return this.indicators.get(id);
  }

  getAll() {
    return Array.from(this.indicators.values());
  }
}

export const indicatorRegistry = new IndicatorRegistry();
