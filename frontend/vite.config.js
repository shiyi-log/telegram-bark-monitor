import { defineConfig } from 'vite'

// Python 包会在运行时托管此目录，将产物放在 frontend/ 外可让 wheel 和 Docker 镜像自包含。
export default defineConfig({
  build: {
    outDir: '../bark_monitor/static',
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    strictPort: false,
  },
})
