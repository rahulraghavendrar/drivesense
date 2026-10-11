import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the React app talks to the FastAPI backend through this proxy,
// so the browser only ever sees one origin (no CORS setup needed).
export default defineConfig({
  plugins: [react()],
  build: { chunkSizeWarningLimit: 1200 },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
});
