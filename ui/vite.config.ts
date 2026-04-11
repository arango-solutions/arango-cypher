import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/connect": "http://localhost:8000",
      "/disconnect": "http://localhost:8000",
      "/connections": "http://localhost:8000",
      "/translate": "http://localhost:8000",
      "/execute": "http://localhost:8000",
      "/validate": "http://localhost:8000",
      "/explain": "http://localhost:8000",
      "/aql-profile": "http://localhost:8000",
      "/cypher-profile": "http://localhost:8000",
    },
  },
  build: {
    outDir: "dist",
  },
});
