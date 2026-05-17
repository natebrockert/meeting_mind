import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const dashboardPort = Number(process.env.MEETINGMIND_DASHBOARD_PORT || process.env.PORT || 5173);
const backendPort = Number(process.env.MEETINGMIND_BACKEND_PORT || 8000);

// Allow access via `tailscale serve` proxies. Vite's DNS-rebinding guard
// rejects Host headers it doesn't recognise; whitelisting `.ts.net` lets the
// user reach the dev server from their own tailnet devices without binding
// the Vite listener to 0.0.0.0.
const extraAllowedHosts = (process.env.MEETINGMIND_ALLOWED_HOSTS || "")
  .split(",")
  .map((h) => h.trim())
  .filter(Boolean);

export default defineConfig({
  plugins: [react()],
  server: {
    port: dashboardPort,
    strictPort: true,
    allowedHosts: [
      "localhost",
      "127.0.0.1",
      ".ts.net",
      ".tailscale-relay.com",
      ...extraAllowedHosts,
    ],
    proxy: {
      "/api": `http://127.0.0.1:${backendPort}`,
    },
  },
});
