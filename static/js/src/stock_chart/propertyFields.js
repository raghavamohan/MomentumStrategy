/** Canonical field definitions for object property panels. */
export const FIELD_CATALOG = {
  period: {
    type: 'number',
    label: 'Period',
    min: 2,
    max: 200,
    step: 1,
    stateKey: 'period'
  },
  color: {
    type: 'color',
    label: 'Color',
    stateKey: 'color'
  },
  upColor: {
    type: 'color',
    label: 'Up Color',
    stateKey: 'upColor'
  },
  downColor: {
    type: 'color',
    label: 'Down Color',
    stateKey: 'downColor'
  },
  multiplier: {
    type: 'number',
    label: 'Multiplier',
    min: 0.1,
    max: 50,
    step: 0.1,
    stateKey: 'multiplier'
  },
  lineWidth: {
    type: 'number',
    label: 'Width',
    min: 1,
    max: 6,
    step: 1,
    stateKey: 'lineWidth'
  },
  width: {
    type: 'number',
    label: 'Width',
    min: 1,
    max: 6,
    step: 1,
    stateKey: 'width'
  },
  style: {
    type: 'enum',
    label: 'Style',
    stateKey: 'style',
    options: [
      { value: 'solid', label: 'Solid' },
      { value: 'dashed', label: 'Dashed' },
      { value: 'dotted', label: 'Dotted' }
    ]
  },
  label: {
    type: 'text',
    label: 'Label',
    stateKey: 'label',
    maxLength: 48
  },
  extended: {
    type: 'boolean',
    label: 'Extended',
    stateKey: 'extended'
  },
  price: {
    type: 'number',
    label: 'Price',
    stateKey: 'price',
    step: 'any'
  }
};

export const INDICATOR_STD_PROPERTIES = ['period', 'color', 'lineWidth', 'style'];
export const SUPERTREND_PROPERTIES = ['period', 'upColor', 'downColor', 'multiplier', 'lineWidth', 'style'];
export const DRAWING_STD_PROPERTIES = ['color', 'width', 'style', 'label'];
export const TRENDLINE_PROPERTIES = ['color', 'width', 'style', 'extended', 'label'];
export const LEVEL_PROPERTIES = ['price', 'label', 'color', 'style', 'width'];

function normalizePropertyEntry(entry) {
  if (typeof entry === 'string') {
    var base = FIELD_CATALOG[entry];
    if (!base) return null;
    return Object.assign({}, base, { id: entry });
  }
  if (entry && typeof entry === 'object') {
    var id = entry.id || entry.stateKey;
    var catalog = id ? FIELD_CATALOG[id] : null;
    return Object.assign({}, catalog || {}, entry, { id: id || entry.stateKey });
  }
  return null;
}

/** Resolve a plugin's editableProperties array into full field configs. */
export function resolveEditableFields(pluginDef) {
  if (!pluginDef || !Array.isArray(pluginDef.editableProperties)) return [];
  var out = [];
  pluginDef.editableProperties.forEach(function (entry) {
    var field = normalizePropertyEntry(entry);
    if (field) out.push(field);
  });
  return out;
}

export function getStateValue(state, field) {
  if (!state || !field) return undefined;
  var key = field.stateKey || field.id;
  if (key === 'upColor' && (state.upColor == null || state.upColor === '')) {
    return state.color;
  }
  return state[key];
}

export function coerceFieldValue(field, raw) {
  if (!field) return undefined;
  if (field.type === 'boolean') return !!raw;
  if (field.type === 'color') {
    return typeof raw === 'string' && raw ? raw : undefined;
  }
  if (field.type === 'text') {
    return typeof raw === 'string' ? raw : '';
  }
  if (field.type === 'enum') {
    var val = typeof raw === 'string' ? raw : 'solid';
    var allowed = (field.options || []).map(function (o) { return o.value; });
    return allowed.indexOf(val) >= 0 ? val : (allowed[0] || 'solid');
  }
  if (field.type === 'number') {
    var n = parseFloat(String(raw).replace(/,/g, ''));
    if (!isFinite(n)) return undefined;
    if (field.min != null) n = Math.max(field.min, n);
    if (field.max != null) n = Math.min(field.max, n);
    if (field.step === 1) n = Math.round(n);
    return n;
  }
  return raw;
}
