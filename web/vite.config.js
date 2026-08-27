import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The Flask app (app.py) serves the built bundle from web/dist and owns every
// API route below. In `npm run dev` those routes are proxied to it so the
// React app can hot-reload against the real backend on :5555.
const API = [
  '/yt', '/separate', '/convert', '/progress',
  '/downloads', '/separated', '/converted',
  '/karaoke/start', '/karaoke/status', '/karaoke/video',
]

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      API.map(p => [p, { target: 'http://localhost:5555', changeOrigin: true }])
    ),
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
