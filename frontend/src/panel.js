// Chat + settings + memory panel. Opens its own link to the core, sends
// commands, and matches replies by id. Port matches runtime.core_port.
const PORT = 8765;
const el = (id) => document.getElementById(id);

let socket = null;
let reqId = 0;
const pending = new Map();

function send(obj) {
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(obj));
}

// Ask the core something and wait for its clio.reply (or null on timeout).
function request(cmd, extra = {}) {
  const id = ++reqId;
  return new Promise((resolve) => {
    pending.set(id, resolve);
    send({ cmd, id, ...extra });
    setTimeout(() => {
      if (pending.has(id)) { pending.delete(id); resolve(null); }
    }, 3000);
  });
}

// --- tabs ---
const panels = { chat: el("tab-chat"), settings: el("tab-settings"), memory: el("tab-memory") };
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b === btn));
    for (const [name, node] of Object.entries(panels)) node.hidden = name !== btn.dataset.tab;
    if (btn.dataset.tab === "settings") loadSettings();
    if (btn.dataset.tab === "memory") loadMemory();
  });
});

// --- chat ---
const bubbles = el("bubbles");
function addBubble(role, text) {
  const b = document.createElement("div");
  b.className = "bubble " + (role === "assistant" ? "clio" : "you");
  b.textContent = text;
  bubbles.appendChild(b);
  bubbles.scrollTop = bubbles.scrollHeight;
}
el("chat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = el("chat-input");
  const text = input.value.trim();
  if (!text) return;
  send({ cmd: "say", text }); // the core echoes it back as a transcript event
  input.value = "";
});

// --- settings ---
function row(k, v) {
  return `<div class="row"><span class="k">${k}</span><span class="v">${v}</span></div>`;
}
async function loadSettings() {
  const s = await request("get_settings");
  const box = el("settings");
  if (!s) { box.innerHTML = `<p class="empty-note">Core not connected.</p>`; return; }
  const caps = (s.capabilities || [])
    .map((c) => {
      const cls = c.permission === "confirm" ? "confirm" : c.permission === "blocked" ? "blocked" : "";
      return `<div class="row"><span class="v" style="text-align:left">${esc(c.name)}</span>` +
             `<span class="pill ${cls}">${c.permission}${c.offline ? "" : " · online"}</span></div>`;
    })
    .join("");
  box.innerHTML =
    `<div class="row section">VOICE</div>` +
    row("Persona", esc(s.persona || "—")) +
    row("Voice", esc(s.voice || "—")) +
    row("Wake", esc((s.wake_phrases || []).join(", ") || "—")) +
    `<div class="row"><span class="k">Speaking speed</span><span class="v">` +
      `<input type="range" id="speed" min="0.7" max="1.5" step="0.05" value="${s.tts_speed ?? 1}"></span></div>` +
    `<div class="row"><span class="k">Muted</span><span class="v">` +
      `<span class="toggle ${s.muted ? "on" : ""}" id="mute-toggle"></span></span></div>` +
    `<div class="row section">CAPABILITIES</div>` + caps;

  el("speed").addEventListener("input", (e) => send({ cmd: "tts_speed", value: parseFloat(e.target.value) }));
  el("mute-toggle").addEventListener("click", (e) => {
    const on = !e.target.classList.contains("on");
    e.target.classList.toggle("on", on);
    send({ cmd: "mute", on });
  });
}

// --- memory ---
async function loadMemory() {
  const box = el("facts");
  const m = await request("get_memory");
  const facts = (m && m.facts) || [];
  box.innerHTML = facts.length
    ? facts.map((f) => `<div class="fact">${esc(f)}</div>`).join("")
    : `<p class="empty-note">Nothing remembered yet.</p>`;
}
el("fact-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = el("fact-input");
  const text = input.value.trim();
  if (!text) return;
  await request("add_fact", { text });
  input.value = "";
  loadMemory();
});

function esc(text) {
  const d = document.createElement("div");
  d.textContent = String(text);
  return d.innerHTML;
}

// --- core link ---
const link = el("link"), linkText = el("link-text");
function connect() {
  const ws = new WebSocket(`ws://127.0.0.1:${PORT}`);
  socket = ws;
  ws.onopen = () => { link.className = "link up"; linkText.textContent = "LINK"; };
  ws.onmessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    if (msg.name === "clio.reply" && pending.has(msg.req)) {
      pending.get(msg.req)(msg.payload);
      pending.delete(msg.req);
    } else if (msg.name === "clio.transcript" && msg.payload?.text) {
      addBubble(msg.payload.role, msg.payload.text);
    }
  };
  ws.onclose = () => {
    socket = null;
    link.className = "link down";
    linkText.textContent = "NO LINK";
    setTimeout(connect, 1000);
  };
  ws.onerror = () => ws.close();
}

connect();
