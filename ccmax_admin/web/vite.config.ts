import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    host: '127.0.0.1', port: 3333,
    proxy: { '/api': { target: 'http://127.0.0.1:4001', changeOrigin: true } },
  },
  build: { target: 'es2020', outDir: 'dist', emptyOutDir: true },
})
