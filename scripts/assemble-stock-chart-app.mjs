/**
 * One-off transform: _legacy_body.js (IIFE monolith) -> chartApp.js (ESM + imports).
 * Run from repo root: node scripts/assemble-stock-chart-app.mjs
 */
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, '..');
const legacyPath = path.join(root, 'static/js/src/stock_chart/_legacy_body.js');
const outPath = path.join(root, 'static/js/src/stock_chart/chartApp.js');

let s = fs.readFileSync(legacyPath, 'utf8');

const header = `/**
 * Stock chart page (TradingView Lightweight Charts v4).
 */
import {
  TP_STORAGE_KEY,
  RSI_SHOW_KEY,
  RSI_PERIOD,
  MAX_MAIN_INDICATORS,
  SAVE_DEBOUNCE_MS,
  WS_RECONNECT_MS,
  STALE_TICK_MS,
  IV_CFG,
  CHART_OPTS,
  MODE_NONE,
  MODE_CROSSHAIR,
  MODE_OBJECTS
} from './constants.js';
import {
  calcSMA,
  calcEMA,
  calcRSI,
  aggregateBars,
  toTime,
  barStepSec,
  istDayString,
  istDayKeyForBar,
  liveBarTimeToSec,
  chartTimeToUnixSec,
  unixSecToChartTime
} from './barMath.js';

export function mountStockChartPage() {
  'use strict';

`;

// Strip IIFE header + inlined constants (now imported)
s = s.replace(
  /^\/\*[\s\S]*?\*\/\s*\(function \(\) \{\s*'use strict';\s*\n\s*var TP_STORAGE_KEY[\s\S]*?var RIGHT_OFFSET_BARS = 30;\s*\n\s*\n\s*\/\/ ── Bootstrap/m,
  header + '  // ── Bootstrap'
);

// Drop interval + chart opts + mode consts (imported); keep chartInteractionMode
s = s.replace(
  /\s*\/\/ ── Interval config[\s\S]*?var MODE_OBJECTS = 'objects';\s*\n\s*var chartInteractionMode = MODE_NONE;\s*\n/,
  '\n  var chartInteractionMode = MODE_NONE;\n'
);

// Drop pure math block (imported from barMath.js); tolerate CRLF from Windows checkouts
s = s.replace(
  /\s*\/\/ ── Math[\s\S]*?\r?\n  function unixSecToChartTime\(sec, refBar\) \{[\s\S]*?\r?\n  \}\r?\n\r?\n(?=\s*function getTimeScaleExtrapolationBasis)/,
  '\n'
);

const footerOld =
  /  window\._scInit = function \(\) \{\s*if \(!LW\) \{ showStatus\('Chart library not available\.'\); return; \}\s*initCharts\(\);\s*loadAnnotations\(function \(\) \{\s*loadHistory\(\);\s*\}\);\s*setupUI\(\);\s*renderChips\(\);\s*\};\s*\n\s*if \(document\.readyState === 'loading'\) \{\s*document\.addEventListener\('DOMContentLoaded', window\._scInit\);\s*\} else \{\s*window\._scInit\(\);\s*\}\s*\n\}\)\(\);\s*$/;

const footerNew = `  if (!LW) { showStatus('Chart library not available.'); return; }
  initCharts();
  loadAnnotations(function () {
    loadHistory();
  });
  setupUI();
  renderChips();
}
`;

if (!footerOld.test(s)) {
  console.error('assemble-stock-chart-app: footer pattern did not match; file layout may have changed.');
  process.exit(1);
}
s = s.replace(footerOld, footerNew);

fs.writeFileSync(outPath, s, 'utf8');
console.log('Wrote', path.relative(root, outPath));
