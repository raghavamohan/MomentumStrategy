import { defineConfig } from 'vite';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  build: {
    rollupOptions: {
      input: path.resolve(__dirname, 'static/js/src/stock_chart/main.js'),
      output: {
        format: 'iife',
        name: 'StockChartPage',
        entryFileNames: 'stock_chart.js',
        inlineDynamicImports: true
      }
    },
    outDir: path.resolve(__dirname, 'static/js'),
    emptyOutDir: false,
    sourcemap: false
  }
});
