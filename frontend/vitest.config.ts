import { defineConfig } from 'vitest/config';
import path from 'path';

// Vitest is scoped to pure logic (geometry). Interactive 3D/UI is verified
// manually against the mock server. `environment: node` is sufficient because
// the tested code (fitBox, pointInOBB) does not touch the DOM or WebGL.
export default defineConfig({
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
});
