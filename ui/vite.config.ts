import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  base: "./",
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/connect": "http://localhost:8001",
      "/disconnect": "http://localhost:8001",
      "/connections": "http://localhost:8001",
      "/translate": "http://localhost:8001",
      "/execute": "http://localhost:8001",
      "/execute-aql": "http://localhost:8001",
      "/validate": "http://localhost:8001",
      "/explain": "http://localhost:8001",
      "/aql-profile": "http://localhost:8001",
      "/cypher-profile": "http://localhost:8001",
      "/nl2cypher": "http://localhost:8001",
      "/nl2aql": "http://localhost:8001",
      "/nl-samples": "http://localhost:8001",
      "/sample-queries": "http://localhost:8001",
      "/mapping": "http://localhost:8001",
      "/schema": "http://localhost:8001",
      "/corrections": "http://localhost:8001",
    },
  },
  build: {
    outDir: "dist",
  },
});
