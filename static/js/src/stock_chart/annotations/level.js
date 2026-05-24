import {
  LEVEL_PROPERTIES,
  resolveEditableFields
} from '../propertyFields.js';

/** Metadata-only plugin for horizontal price levels (rendered as LW price lines). */
export const levelPlugin = {
  id: 'LEVEL',
  name: 'Horizontal Level',
  chipLabel: 'Lv',
  editableProperties: LEVEL_PROPERTIES
};

export function getLevelEditableFields() {
  return resolveEditableFields(levelPlugin);
}
