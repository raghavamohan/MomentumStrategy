import { mountStockChartPage } from './chartApp.js';

/** Optional hook for debugging / tests (same name as legacy global). */
window._scInit = mountStockChartPage;

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', function () {
    mountStockChartPage();
  });
} else {
  mountStockChartPage();
}
