import { mergeConfig } from "vite";
import config from "./vite.config.ts";

// Fixed loopback test ports; never proxy into the user's 8000 trial process.
export default mergeConfig(config, {
  server: {
    host: "localhost",
    port: 5175,
    strictPort: true,
    proxy: {
      "/api": { target: "http://127.0.0.1:8001", changeOrigin: false },
      "/ws/v1": {
        target: "ws://127.0.0.1:8001",
        ws: true,
        changeOrigin: false,
        rewriteWsOrigin: false,
      },
    },
  },
});
