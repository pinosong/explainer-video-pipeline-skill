// Record a live anatomy page to JPEG frames via CDP screencast.
// usage: node record.mjs <page.html> <outdir> <seconds> [scale] [originX%] [originY%]
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync, rmSync } from "node:fs";
import { join, resolve } from "node:path";

const [page, outdir, secsRaw, scaleArg, oxArg, oyArg] = process.argv.slice(2);
const SECONDS = Number(secsRaw || 15);
const PORT = 9333;
const PROFILE = "/tmp/rec-profile-" + process.pid;
rmSync(PROFILE, { recursive: true, force: true });
mkdirSync(outdir, { recursive: true });

const chrome = spawn("/opt/meta-chromium/chrome", [
  "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
  "--window-size=1240,900", "--force-device-scale-factor=2",
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${PROFILE}`, "about:blank",
], { stdio: ["ignore", "ignore", "pipe"] });
chrome.stderr.on("data", () => {});

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitEndpoint() {
  for (let i = 0; i < 60; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/version`);
      const j = await r.json();
      if (j.webSocketDebuggerUrl) return j.webSocketDebuggerUrl;
    } catch {}
    await sleep(300);
  }
  throw new Error("no CDP endpoint");
}

const wsUrl = await waitEndpoint();
const ws = new WebSocket(wsUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let nextId = 1;
const pending = new Map();
const listeners = [];
ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  else if (msg.method) for (const fn of listeners) fn(msg);
};
const send = (method, params = {}, sessionId) => new Promise((res) => {
  const id = nextId++;
  pending.set(id, res);
  ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
});

const { result: { targetId } } = await send("Target.createTarget", { url: "about:blank" });
const { result: { sessionId } } = await send("Target.attachToTarget", { targetId, flatten: true });
const S = (m, p) => send(m, p, sessionId);
await S("Page.enable");
await S("Runtime.enable");
await S("Page.navigate", { url: "file://" + resolve(page) });
await sleep(1600);

// plate rect in CSS px
const rectRes = await S("Runtime.evaluate", {
  expression: `(() => { const el = document.querySelector('.iso-plate'); const r = el.getBoundingClientRect(); return {x: r.x, y: r.y, w: r.width, h: r.height, iw: innerWidth, ih: innerHeight}; })()`,
  returnByValue: true,
});
const rect = rectRes.result.result.value;
writeFileSync(join(outdir, "plate-rect.json"), JSON.stringify(rect));
console.log("plate rect", rect);
if (scaleArg) {
  await S("Runtime.evaluate", {
    expression: `document.body.style.transform = 'scale(${scaleArg})'; document.body.style.transformOrigin = '${oxArg || 50}% ${oyArg || 50}%';`,
  });
  writeFileSync(join(outdir, "transform.json"), JSON.stringify({ scale: Number(scaleArg), ox: Number(oxArg || 50), oy: Number(oyArg || 50) }));
  await sleep(400);
  console.log("transform applied", scaleArg, oxArg, oyArg);
}

let frames = 0;
const stamps = [];
const t0 = Date.now();
listeners.push(async (msg) => {
  if (msg.method === "Page.screencastFrame" && msg.sessionId === sessionId) {
    const { data, metadata } = msg.params;
    const name = `f_${String(frames).padStart(5, "0")}.jpg`;
    writeFileSync(join(outdir, name), Buffer.from(data, "base64"));
    stamps.push({ f: name, ts: metadata.timestamp, wall: (Date.now() - t0) / 1000 });
    frames++;
    await S("Page.screencastFrameAck", { frameId: msg.params.sessionId ?? undefined, sessionId: msg.params.sessionId }).catch(() => {});
    // correct ack: needs the screencast sessionId from params
  }
});
// ack properly: CDP expects Page.screencastFrameAck with {sessionId: <frame's sessionId>}
listeners.length = 0;
listeners.push((msg) => {
  if (msg.method === "Page.screencastFrame" && msg.sessionId === sessionId) {
    const { data, metadata } = msg.params;
    const name = `f_${String(frames).padStart(5, "0")}.jpg`;
    writeFileSync(join(outdir, name), Buffer.from(data, "base64"));
    stamps.push({ f: name, ts: metadata.timestamp, wall: (Date.now() - t0) / 1000 });
    frames++;
    send("Page.screencastFrameAck", { sessionId: msg.params.sessionId }, sessionId);
  }
});

await S("Page.startScreencast", { format: "jpeg", quality: 88, maxWidth: 2480, maxHeight: 1800, everyNthFrame: 1 });
await sleep(SECONDS * 1000);
await S("Page.stopScreencast").catch(() => {});
writeFileSync(join(outdir, "stamps.json"), JSON.stringify(stamps, null, 1));
console.log("frames:", frames, "span:", stamps.length ? (stamps[stamps.length - 1].wall).toFixed(2) : 0);
ws.close();
chrome.kill("SIGKILL");
rmSync(PROFILE, { recursive: true, force: true });
process.exit(0);
