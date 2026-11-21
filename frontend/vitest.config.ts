import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/__tests__/setup.ts'],
    include: ['src/**/*.test.ts'],
    coverage: {
      provider: 'v8',
      include: ['src/**'],
      exclude: ['src/**/*.test.ts', 'src/vite-env.d.ts'],
      reporter: ['text', 'html'],
    },
  },
})
