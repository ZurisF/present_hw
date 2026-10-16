import { defineConfig } from "vite";

export default defineConfig({
  server: {
    host: "0.0.0.0",
    port: 3000,
    strictPort: true,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/doc": "http://127.0.0.1:8000",
      "/openapi.json": "http://127.0.0.1:8000",
    },
  },
});
