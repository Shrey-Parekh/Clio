// Clio HUD. Reads state and transcript from the core over WebSocket and drives
// a signal-scope visualiser. Port must match runtime.core_port in the config.
const PORT = 8765;

const el = (id) => document.getElementById(id);
const link = el("link"), linkText = el("link-text");
const stateEl = el("state"), subEl = el("sub");
const youLine = el("you-line"), youSaid = el("you-said");
const clioLine = el("clio-line"), clioSaid = el("clio-said");
const idleHint = el("idle-hint");

// Each state is a character for the scope, not just a colour: how much the
// signal moves, its colour, and whether a scan sweeps across it.
const AQUA = [53, 240, 208];
const BLUE = [110, 168, 255];
const DIM = [74, 90, 110];
const STATES = {
  standby:   { energy: 0.10, rgb: AQUA, label: "STANDBY",   sub: "waiting on your word",  scan: false },
  listening: { energy: 0.62, rgb: AQUA, label: "LISTENING", sub: "go ahead, I'm hearing you", scan: false },
  thinking:  { energy: 0.30, rgb: BLUE, label: "THINKING",  sub: "working it out",         scan: true },
  speaking:  { energy: 0.95, rgb: AQUA, label: "SPEAKING",  sub: "speaking",               scan: false },
  offline:   { energy: 0.02, rgb: DIM,  label: "OFFLINE",   sub: "core not connected",     scan: false },
};

let target = STATES.offline;
const cur = { energy: 0.02, rgb: [...DIM] };
let speakingTimer = null;

const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

function setState(name) {
  target = STATES[name] || STATES.standby;
  stateEl.textContent = target.label;
  stateEl.style.color = `rgb(${target.rgb.join(",")})`;
  subEl.textContent = target.sub;
  idleHint.hidden = name !== "standby" || (!youLine.hidden || !clioLine.hidden);
  // Speaking has no explicit end event; fall back to standby if none arrives.
  clearTimeout(speakingTimer);
  if (name === "speaking") speakingTimer = setTimeout(() => setState("standby"), 12000);
}

function showTranscript(role, text) {
  if (role === "user") {
    youSaid.textContent = text;
    youLine.hidden = false;
  } else {
    clioSaid.textContent = text;
    clioLine.hidden = false;
  }
  idleHint.hidden = true;
}

// --- the scope ---
const canvas = el("signal");
const ctx = canvas.getContext("2d");
let W = 0, H = 0, dpr = 1;

function resize() {
  dpr = window.devicePixelRatio || 1;
  W = canvas.clientWidth;
  H = canvas.clientHeight;
  canvas.width = Math.max(1, W * dpr);
  canvas.height = Math.max(1, H * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
window.addEventListener("resize", resize);

function lerp(a, b, t) { return a + (b - a) * t; }

function draw(now) {
  const t = now / 1000;
  // Ease the visible energy and colour toward the target so states morph.
  cur.energy = lerp(cur.energy, target.energy, 0.06);
  for (let i = 0; i < 3; i++) cur.rgb[i] = lerp(cur.rgb[i], target.rgb[i], 0.06);
  const [r, g, b] = cur.rgb.map(Math.round);

  ctx.clearRect(0, 0, W, H);
  const mid = H / 2;

  // baseline
  ctx.strokeStyle = `rgba(${r},${g},${b},0.10)`;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, mid);
  ctx.lineTo(W, mid);
  ctx.stroke();

  const amp = H * 0.30 * cur.energy;
  const phase = reduced ? 0 : t;
  const jitter = reduced ? 0 : cur.energy * 0.28;

  // one oscilloscope trace, plus a fainter ghost beneath it for depth
  for (const pass of [{ scale: 1, alpha: 1, glow: 16 }, { scale: -0.45, alpha: 0.28, glow: 0 }]) {
    ctx.beginPath();
    for (let x = 0; x <= W; x += 2) {
      const k = x / W;
      const wave =
        Math.sin(k * 22 + phase * 2.0) * 0.5 +
        Math.sin(k * 11 - phase * 1.3) * 0.3 +
        Math.sin(k * 4.5 + phase * 0.7) * 0.2 +
        (Math.random() - 0.5) * jitter;
      // taper the ends so the trace fades into the edges
      const taper = Math.sin(Math.PI * k);
      const y = mid + wave * amp * pass.scale * taper;
      x === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.strokeStyle = `rgba(${r},${g},${b},${pass.alpha})`;
    ctx.lineWidth = 2;
    ctx.shadowBlur = pass.glow * cur.energy;
    ctx.shadowColor = `rgba(${r},${g},${b},0.9)`;
    ctx.stroke();
    ctx.shadowBlur = 0;
  }

  // thinking: a bright sweep travelling across the trace
  if (target.scan && !reduced) {
    const sx = (t * 0.35 % 1) * W;
    const grad = ctx.createLinearGradient(sx - 60, 0, sx + 60, 0);
    grad.addColorStop(0, `rgba(${r},${g},${b},0)`);
    grad.addColorStop(0.5, `rgba(${r},${g},${b},0.5)`);
    grad.addColorStop(1, `rgba(${r},${g},${b},0)`);
    ctx.fillStyle = grad;
    ctx.fillRect(sx - 60, 0, 120, H);
  }

  requestAnimationFrame(draw);
}

resize();
requestAnimationFrame(draw);

// --- core link ---
function connect() {
  const ws = new WebSocket(`ws://127.0.0.1:${PORT}`);
  ws.onopen = () => {
    link.className = "link up";
    linkText.textContent = "LINK";
    setState("standby");
  };
  ws.onmessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    if (msg.name === "clio.state" && msg.payload?.state) {
      setState(msg.payload.state === "idle" ? "standby" : msg.payload.state);
    } else if (msg.name === "clio.transcript" && msg.payload?.text) {
      showTranscript(msg.payload.role, msg.payload.text);
    } else if (msg.name === "clio.wake") {
      setState("listening");
    }
  };
  ws.onclose = () => {
    link.className = "link down";
    linkText.textContent = "NO LINK";
    setState("offline");
    setTimeout(connect, 1000);
  };
  ws.onerror = () => ws.close();
}

link.className = "link down";
setState("offline");
connect();
