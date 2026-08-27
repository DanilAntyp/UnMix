import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // during `npm run dev`, forward API calls to the Flask backend
      '^/(yt|separate|convert|progress|karaoke/(start|status|video)|downloads|separated|converted)': {
        target: 'http://localhost:5555',
        changeOrigin: true,
      },
    },
  },
})
