import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

const pythonPackageBuild = process.env.TEMPO_BUILD_TARGET === 'python';

export default defineConfig({
  plugins: [react()],
  base: pythonPackageBuild ? '/' : './',
  build: {
    outDir: pythonPackageBuild ? 'dist-python' : 'dist',
    emptyOutDir: true,
    commonjsOptions: {
      transformMixedEsModules: true,
    },
    rollupOptions: {
      input: path.resolve(__dirname, 'index.html'),
    },
  },
  server: {
    port: 5173,
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
});
