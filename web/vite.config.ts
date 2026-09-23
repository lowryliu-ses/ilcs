import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(() => {
  const apiTarget = process.env.VITE_API_PROXY_TARGET || "http://127.0.0.1:8011";
  return {
    plugins: [react()],
    server: {
      port: 5174,
      host: "127.0.0.1",
      strictPort: true,
      // 后端自身以 /api 为前缀挂载路由，代理只转发不改写路径。
      proxy: { "/api": { target: apiTarget, changeOrigin: true } },
    },
  };
});
