import { defineConfig, mergeConfig } from "vitest/config";
import base from "./vite.config.ts";

export default mergeConfig(
  base,
  defineConfig({
    test: {
      globals: true,
      environment: "jsdom",
      setupFiles: ["./src/test/setup.ts"],
      css: false,
      include: ["src/**/*.test.{ts,tsx}"],
    },
  }),
);
