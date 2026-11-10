import js from "@eslint/js";
import html from "eslint-plugin-html";
import globals from "globals";

const rules = {
  ...js.configs.recommended.rules,
  eqeqeq: ["error", "always"],
  "no-var": "error",
  "prefer-const": "error",
};

export default [
  { ignores: ["node_modules/**", "tmp/**", ".venv*/**", ".data/**", "redteam/results/**"] },
  {
    files: ["agent/agent_ui/**/*.html", "agent/agent_ui/**/*.js"],
    plugins: { html },
    languageOptions: { sourceType: "script", globals: globals.browser },
    rules,
  },
  {
    files: ["tests/**/*.{js,cjs,mjs}", "eslint.config.mjs"],
    languageOptions: { globals: globals.node },
    rules,
  },
];
