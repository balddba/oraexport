import { fileURLToPath } from "node:url";
import { env } from "node:process";
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    fs: { allow: [".."] },
    proxy: { "/api": env.ORAEXPORT_DEV_API || "http://127.0.0.1:8000" },
  },
  test: {
    alias: [
      {
        find: /^(react(?:\/.*)?|react-dom(?:\/.*)?|@testing-library\/.*|@tanstack\/.*|vitest)$/,
        replacement: fileURLToPath(
          new URL("./node_modules/$1", import.meta.url),
        ),
      },
    ],
    environment: "jsdom",
    include: ["../tests/frontend/**/*.test.{ts,tsx}"],
    setupFiles: "../tests/frontend/setup.ts",
    clearMocks: true,
  },
});
