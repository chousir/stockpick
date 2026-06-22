import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// dev：前端 :5173，/api 反代到 FastAPI :8000（docs/17 §3.2）
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
  build: {
    outDir: "dist",
  },
});
