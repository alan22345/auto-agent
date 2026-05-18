import { defineConfig } from 'vitest/config';
import path from 'node:path';

// `esbuild.jsx: 'automatic'` enables React 17+ JSX transform without
// requiring `import React from 'react'` in every component / test file.
// Mirrors what Next.js does at build time so component tests behave
// the same as they would in the app.
export default defineConfig({
  test: { environment: 'jsdom', globals: true, setupFiles: [] },
  esbuild: { jsx: 'automatic', jsxImportSource: 'react' },
  resolve: { alias: { '@': path.resolve(__dirname, './') } },
});
