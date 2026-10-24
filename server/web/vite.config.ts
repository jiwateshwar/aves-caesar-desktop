import { defineConfig } from "vite";
import preact from "@preact/preset-vite";

export default defineConfig({
  plugins: [preact()],
  build: {
    outDir: "../aves_server/static",
    emptyOutDir: true,
  },
  server: {
    // `npm run dev` talks to a locally running `uvicorn aves_server.api:app --port 8080`.
    proxy: { "/api": "http://127.0.0.1:8080" },
  },
});
