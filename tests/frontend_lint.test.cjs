const assert = require("node:assert/strict");
const { ESLint } = require("eslint");

(async () => {
  const eslint = new ESLint();
  const [invalid] = await eslint.lintText(
    "<html><script>console.log(missingFrontendValue);</script></html>",
    { filePath: "agent/agent_ui/index.html" },
  );
  assert.ok(
    invalid.messages.some((message) => message.ruleId === "no-undef"),
    "Inline HTML scripts must be linted, not silently skipped",
  );
  const [valid] = await eslint.lintText(
    "<html><script>document.title = 'Example';</script></html>",
    { filePath: "agent/agent_ui/index.html" },
  );
  assert.equal(valid.errorCount, 0, "Browser globals should be recognised");
  console.log("Inline-script lint coverage and browser-global checks passed.");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
