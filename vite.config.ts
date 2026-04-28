import { defineConfig } from 'vitest/config';

export default defineConfig({
  build: {
    target: 'es2022',
    lib: {
      entry: 'card/src/index.ts',
      formats: ['es'],
      fileName: () => 'comfort-band-card.js',
    },
    outDir: 'dist',
    emptyOutDir: true,
    minify: 'esbuild',
    sourcemap: false,
    rollupOptions: {
      output: { inlineDynamicImports: true },
    },
  },
  esbuild: {
    legalComments: 'none',
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['card/test/**/*.test.ts'],
  },
});
