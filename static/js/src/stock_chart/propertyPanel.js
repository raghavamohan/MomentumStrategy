import { getStateValue, coerceFieldValue } from './propertyFields.js';

function fieldDomId(prefix, field) {
  return prefix + '-' + (field.id || field.stateKey);
}

function makeRow(labelText, controlEl) {
  var row = document.createElement('div');
  row.className = 'sc-panel-row';
  var label = document.createElement('label');
  label.textContent = labelText;
  row.appendChild(label);
  row.appendChild(controlEl);
  return row;
}

function createControl(field, prefix) {
  var id = fieldDomId(prefix, field);
  if (field.type === 'enum') {
    var sel = document.createElement('select');
    sel.id = id;
    (field.options || []).forEach(function (opt) {
      var o = document.createElement('option');
      o.value = opt.value;
      o.textContent = opt.label || opt.value;
      sel.appendChild(o);
    });
    return sel;
  }
  if (field.type === 'boolean') {
    var cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.id = id;
    return cb;
  }
  if (field.type === 'color') {
    var color = document.createElement('input');
    color.type = 'color';
    color.id = id;
    return color;
  }
  var inp = document.createElement('input');
  inp.id = id;
  if (field.type === 'text') {
    inp.type = 'text';
    if (field.maxLength) inp.maxLength = field.maxLength;
  } else {
    inp.type = 'number';
    if (field.min != null) inp.min = String(field.min);
    if (field.max != null) inp.max = String(field.max);
    if (field.step != null) inp.step = String(field.step);
  }
  return inp;
}

/** Render property fields into a container (replaces existing field rows). */
export function renderPropertyFields(container, fields, prefix) {
  if (!container) return;
  container.innerHTML = '';
  fields.forEach(function (field) {
    var control = createControl(field, prefix);
    var row = makeRow(field.label || field.id, control);
    if (field.type === 'boolean') {
      row = document.createElement('div');
      row.className = 'sc-panel-row';
      var boolLabel = document.createElement('label');
      boolLabel.appendChild(control);
      boolLabel.appendChild(document.createTextNode(' ' + (field.label || field.id)));
      row.appendChild(boolLabel);
    }
    container.appendChild(row);
  });
}

/** Populate rendered controls from object state. */
export function populatePropertyFields(fields, state, prefix) {
  fields.forEach(function (field) {
    var el = document.getElementById(fieldDomId(prefix, field));
    if (!el) return;
    var value = getStateValue(state, field);
    if (field.type === 'boolean') {
      el.checked = !!value;
      return;
    }
    if (value == null || value === '') return;
    el.value = String(value);
  });
}

/** Read values from rendered controls. */
export function readPropertyFields(fields, prefix) {
  var values = {};
  fields.forEach(function (field) {
    var el = document.getElementById(fieldDomId(prefix, field));
    if (!el) return;
    if (field.type === 'boolean') {
      values[field.stateKey || field.id] = el.checked === true;
      return;
    }
    var raw = el.value;
    var coerced = coerceFieldValue(field, raw);
    if (coerced !== undefined) {
      values[field.stateKey || field.id] = coerced;
    }
  });
  return values;
}
