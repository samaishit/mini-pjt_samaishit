import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// 개발 서버(npm run dev)는 API 요청을 uvicorn(8000)으로 넘긴다. 빌드 결과(dist)는 FastAPI 가 / 에서 서빙한다.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/query": "http://localhost:8000",
      "/examples": "http://localhost:8000",
      "/health": "http://localhost:8000",
    },
  },
  test: {
    environment: "jsdom",
  },
});
