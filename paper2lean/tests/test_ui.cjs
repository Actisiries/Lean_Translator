// Dependency-free UI regression checks; run with `node tests/test_ui.cjs`.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

class Node {
  constructor() {
    this.children = [];
    this.textContent = "";
    this.value = "";
    this.dataset = {};
    const classes = new Set();
    this.classList = {
      add: name => classes.add(name), remove: name => classes.delete(name),
      toggle: (name, force) => force ? classes.add(name) : classes.delete(name),
      contains: name => classes.has(name),
    };
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
}
const html = fs.readFileSync(path.join(__dirname, "../static/index.html"), "utf8");
const nodes = new Map([...html.matchAll(/id="([^"]+)"/g)].map(match => [match[1], new Node()]));
const calls = [];
const controls = [...html.matchAll(/<(?:input|button|select|textarea)\b[^>]*\bid="([^"]+)"[^>]*>/g)].map(match => nodes.get(match[1]));
for (const match of html.matchAll(/<(?:input|button)\b[^>]*\bid="([^"]+)"[^>]*\bdisabled[^>]*>/g)) nodes.get(match[1]).disabled = true;
let approved = false;
const runPayloads = [];
const context = vm.createContext({
  document: {
    getElementById: id => { assert(nodes.has(id), `Missing HTML element ${id}`); return nodes.get(id); },
    createElement: () => new Node(), querySelectorAll: selector => selector === "input, button, select, textarea" ? controls : [], body: new Node(),
  },
  localStorage: {getItem: () => null, setItem: () => {}},
  setTimeout: () => 1, clearTimeout: () => {}, setInterval: () => 1, clearInterval: () => {},
  Date, Map, Uint8Array, AbortSignal, crypto: {randomUUID: () => "ui-test"}, window: {confirm: () => approved},
  fetch: async (url, options) => {
    calls.push(url);
    if (url.includes("/api/activity")) throw Error("Monitor offline");
    if (url === "/api/run") {
      runPayloads.push(JSON.parse(options.body));
      return {ok: true, json: async () => ({units: [{id: "bad", status: "succeeded"}], retryable_units: [],
        coverage: 1, counts: {compiled: 1, failed: 0, pending: 0}, assembly: {compiled: true}})};
    }
    return {ok: true, json: async () => ({llm_configured: false})};
  },
});
const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
vm.runInContext(source, context);
vm.runInContext('renderUnits([{id:"one",label:"Lemma 1",kind:"lemma",statement:"<img src=x onerror=alert(1)>",status:"pending"}])', context);
assert.equal(nodes.get("units-list").children.length, 1);
assert.equal(nodes.get("units-list").children[0].children[1].textContent, "<img src=x onerror=alert(1)>");
vm.runInContext('renderUnits([{id:"one",label:"stale:7.1",display_name:"Proposition 3.1",statement:"A statement",proof:"<script>source proof</script>\\nSecond line",proof_status:"linked",proof_evidence:["explicit proof title"]},{id:"two",proof_status:"cited_result"},{id:"three",proof_status:"ambiguous"}])', context);
const cards = nodes.get("units-list").children;
assert.equal(cards[0].children[0].children[0].textContent, "Proposition 3.1");
assert.equal(cards[0].children[2].textContent, "Source reference: stale:7.1");
assert.equal(cards[0].children[3].open, true);
assert.equal(cards[0].children[3].children[1].textContent, "<script>source proof</script>\nSecond line");
assert.match(cards[1].children[2].children[1].textContent, /Cited result/);
assert.match(cards[2].children[2].children[1].textContent, /needs source review/);
vm.runInContext('renderActivity({status:"running",events:Array.from({length:12},(_,n)=>({time:n,stage:"candidate",status:"running",candidate:n}))},"formalization")', context);
assert.equal(nodes.get("recent-steps").children.length, 5);
assert.match(nodes.get("activity-state").textContent, /Formalization: running/);
assert(!source.includes("Inventory is being built."));
assert(!source.includes("/api/job"));
assert(!html.includes('id="procedure"'));
context.operationCalls = 0;
(async () => {
  context.assertLocked = () => controls.forEach(control => assert.equal(control.disabled, true));
  await vm.runInContext('execute("analysis", async () => { assertLocked(); operationCalls += 1; })', context);
  assert.equal(context.operationCalls, 1);
  assert.equal(nodes.get("run-state").textContent, "Analyzing…");
  assert.equal(nodes.get("analyze").disabled, false);
  assert.equal(nodes.get("output").textContent, "");
  assert.equal(nodes.get("url").disabled, undefined);
  assert.equal(nodes.get("run").disabled, true);
  vm.runInContext('analysis = {id:"paper", units:[{id:"bad", label:"Bad theorem", statement:"1=1", status:"failed"}]}; lastReport = {retryable_units:["bad"]}; setBusy(false)', context);
  assert.equal(nodes.get("retry-failed").disabled, false);
  await nodes.get("retry-failed").onclick();
  assert.equal(runPayloads.length, 0, "Cancelling permission must not send requests");
  approved = true;
  await nodes.get("retry-failed").onclick();
  assert.equal(runPayloads.length, 1);
  assert.equal(runPayloads[0].retry_failed, true);
  assert.equal(runPayloads[0].retry_confirmed, true);
  assert.equal(nodes.get("retry-failed").disabled, true);
  assert.equal(nodes.get("units-list").children[0].children[0].children[1].textContent, "unit · succeeded");
  console.log("UI checks passed: rendering, monitoring, control locking, retry permission and updated outcomes.");
})().catch(error => { console.error(error); process.exitCode = 1; });
