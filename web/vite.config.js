import { fileURLToPath } from "node:url";
import path from "node:path";
import { defineConfig } from "vite";
import tailwindcss from "@tailwindcss/vite";

const rootDir = path.dirname(fileURLToPath(import.meta.url));
export default defineConfig({
  root: rootDir,
  plugins: [tailwindcss()],
  build: {
    rollupOptions: {
      input: {
        connect: path.resolve(rootDir, "index.html"),
        dashboard: path.resolve(rootDir, "dashboard.html"),
      },
    },
    outDir: path.resolve(rootDir, "dist"),
    emptyOutDir: true,
  },
  server: { host: "0.0.0.0", port: 5173, strictPort: true },
});
