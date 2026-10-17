import path from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { tanstackRouter } from '@tanstack/router-plugin/vite'

// The console (python -m nmbz portal, or tools/demo_console.py) that the dev server sends API calls to.
const CONSOLE = process.env.PORTAL_CONSOLE ?? 'http://127.0.0.1:8085'

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    // Must come before the React plugin.
    tanstackRouter({ target: 'react', autoCodeSplitting: true }),
    react(),
    tailwindcss(),
  ],
  resolve: {
    alias: { '@': path.resolve(import.meta.dirname, './src') },
  },
  server: {
    proxy: { '/api': { target: CONSOLE, changeOrigin: false }, '/classic': { target: CONSOLE } },
  },
  build: {
    // The console serves this folder at / (nmbz/console.py). Built files are committed so the server needs no Node.
    outDir: path.resolve(import.meta.dirname, '../agentcore/portal/web'),
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 700,
  },
})
