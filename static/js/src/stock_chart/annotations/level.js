import {
  LEVEL_PROPERTIES,
  resolveEditableFields
} from '../propertyFields.js';
import { DEFAULT_LINE_WIDTH } from '../constants.js';

/** Metadata-only plugin for horizontal price levels (rendered as LW price lines). */
export const levelPlugin = {
  id: 'LEVEL',
  name: 'Horizontal Level',
  chipLabel: 'Lv',
  editableProperties: LEVEL_PROPERTIES,
  defaultOptions: { color: '#cbd5e1', style: 'dashed', width: DEFAULT_LINE_WIDTH, label: '' }
};

export function getLevelEditableFields() {
  return resolveEditableFields(levelPlugin);
}
