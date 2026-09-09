// Connects to the core WebSocket server and shows what Clio is doing.
// The port must match runtime.core_port in config/default.toml.
const PORT = 8765;
const MAX_ROWS = 100;

const dot = document.getElementById("dot");
const status = document.getElementById("status");
const events = document.getElementById("events");
const empty = document.getElementById("empty");

function setStatus(state) {
  dot.className = "dot " + state;
  status.textContent = state === "connected" ? "connected" : "waiting for Clio…";
}

function addEvent(event) {
  empty.hidden = true;
  const row = document.createElement("li");
  const time = new Date((event.ts || Date.now() / 1000) * 1000)
    .toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  row.innerHTML =
    `<span class="t">${time}</span>` +
    `<span class="n">${escape(event.name)}</span>` +
    `<span class="s">${escape(event.source || "")}</span>`;
  events.prepend(row);
  while (events.childElementCount > MAX_ROWS) events.lastElementChild.remove();
}

function escape(text) {
  const d = document.createElement("div");
  d.textContent = String(text);
  return d.innerHTML;
}

function connect() {
  const ws = new WebSocket(`ws://127.0.0.1:${PORT}`);
  ws.onopen = () => setStatus("connected");
  ws.onmessage = (e) => {
    try {
      addEvent(JSON.parse(e.data));
    } catch {
      /* ignore anything that isn't a core event */
    }
  };
  // Reconnect on drop: the core may start after the window, or restart under it.
  ws.onclose = () => {
    setStatus("disconnected");
    setTimeout(connect, 1000);
  };
  ws.onerror = () => ws.close();
}

setStatus("disconnected");
connect();
