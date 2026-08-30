import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Tauri drives this dev server; the port is fixed because tauri.conf.json
// points at it and a moving port would silently load nothing.
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: { port: 1420, strictPort: true },
  build: { target: "safari15", sourcemap: true },
});
