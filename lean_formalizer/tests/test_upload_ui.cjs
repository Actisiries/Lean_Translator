// Exercise the browser upload flow with DOM/API substitutes, without a server.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const elements = new Map();
const events = {};
const input = { value: "", files: [], addEventListener: (name, fn) => { events[name] = fn; } };
const label = { textContent: "" };
const dropzone = { addEventListener() {}, classList: { add() {}, remove() {} } };
elements.set("#input-pdf", input);
elements.set("#pdf-label", label);
elements.set("#dropzone", dropzone);
elements.set(".tab.active", { dataset: { tab: "pdf" } });
elements.set("#input-text", { value: "Old text from the other tab" });
elements.set("#input-statement", { value: "Target claim" });
elements.set("#theorem-label", { value: "thm:main" });

class FormDataStub {
  constructor() { this.fields = new Map(); }
  append(name, value) { this.fields.set(name, value); }
}
const context = vm.createContext({
  document: { querySelector: (name) => elements.get(name) || null, querySelectorAll: () => [], addEventListener() {} },
  window: { addEventListener() {} },
  FormData: FormDataStub,
  console,
});
const source = fs.readFileSync(path.join(__dirname, "../src/lean_formalizer/static/app.js"), "utf8");
vm.runInContext(source + `
  globalThis.testHooks = { wireDropzone, collectConfig, onRun, state, fillDetails };
  checkBackendConfig = async () => {};
  saveOptionsFromForm = () => {};
  refreshHealth = () => {};
  clearPreviousResult = () => {};
  renderResult = () => {};
  api = async (url, options) => {
    if (url === "/api/health") return {};
    globalThis.uploadRequest = options;
    return { success: false, best_code: "" };
  };
`, context);

(async () => {
  const hooks = context.testHooks;
  hooks.wireDropzone();
  const file = { name: "paper.TeX", size: 1234 };
  input.files = [file];
  events.change();
  assert.equal(hooks.state.pdfFile, file);
  assert.equal(hooks.collectConfig().theorem_label, "thm:main");
  await hooks.onRun();
  assert.equal(context.uploadRequest.body.fields.get("file"), file);
  assert.equal(context.uploadRequest.body.fields.has("pdf"), false);
  assert.equal(context.uploadRequest.body.fields.has("text"), false);
  const paperDetails = { textContent: "" };
  elements.set("#out-structure", paperDetails);
  hooks.fillDetails({ paper_report: { name: "paper.pdf", format: "pdf", coverage: 0.5,
    selected_units: [{ label: "Theorem 1", status: "failed", message: "unsolved goals" }] } });
  assert.match(paperDetails.textContent, /Paper: paper.pdf/);
  assert.match(paperDetails.textContent, /Theorem 1: failed/);
  assert.match(paperDetails.textContent, /unsolved goals/);
  assert.equal(context.uploadRequest.body.fields.get("target_statement"), "Target claim");
  assert.equal(context.uploadRequest.body.fields.get("theorem_label"), "thm:main");
  assert.equal(hooks.state.lastInput.text, "");

  // Selecting an invalid file or cancelling must discard a previous selection.
  input.files = [{ name: "bad.exe", size: 5 }];
  events.change();
  assert.equal(hooks.state.pdfFile, null);
  input.files = [];
  events.change();
  assert.equal(hooks.state.pdfFile, null);

  input.files = [{ name: "paper.pdf", size: 1234 }];
  events.change();
  assert.equal(hooks.collectConfig().theorem_label, "");
  await hooks.onRun();
  assert.equal(context.uploadRequest.body.fields.get("file").name, "paper.pdf");
  assert.equal(context.uploadRequest.body.fields.has("text"), false);
  console.log("PASS: TeX/PDF upload dispatch, theorem label, target statement, and stale-file/text handling");
})().catch((error) => { console.error(error); process.exitCode = 1; });
