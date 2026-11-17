import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    // Three.js (~970 KB) is a legitimately large 3D dependency isolated in its
    // own lazy-loaded chunk. Set the limit just above it so genuine regressions
    // still flag but the expected vendor chunk stays clean.
    chunkSizeWarningLimit: 1024,
    rollupOptions: {
      output: {
        // Isolate the 3D stack so it's cached separately and only fetched with
        // the lazy-loaded viewer route. Function form (Rolldown/vite 8 compatible).
        manualChunks(id) {
          if (/[\\/]node_modules[\\/](three|@react-three|three-stdlib|three-mesh-bvh)[\\/]/.test(id)) {
            return 'three';
          }
        },
      },
    },
  },
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
});
