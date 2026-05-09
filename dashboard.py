"""Live dashboard: in-memory event bus + Server-Sent Events + a single-page UI.

Anywhere in the server, call `bus.publish({"type": "...", ...})` and every
connected dashboard receives the event over SSE in real time.

Event shapes:
    {"type": "call_started",  "call_sid", "from", "customer_name", "ts"}
    {"type": "user_said",     "call_sid", "text", "ts"}
    {"type": "agent_said",    "call_sid", "text", "ts"}
    {"type": "order_saved",   "call_sid", "order"}
    {"type": "call_ended",    "call_sid", "duration_s", "saved": bool}
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)


class EventBus:
    """In-process pub/sub for dashboard updates."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def publish(self, event: dict) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("dashboard subscriber queue full, dropping event")


bus = EventBus()


def make_router(orders_dir: Path) -> APIRouter:
    """Build a FastAPI router exposing the dashboard."""
    router = APIRouter()

    @router.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_page() -> HTMLResponse:
        return HTMLResponse(content=DASHBOARD_HTML)

    @router.get("/orders.json")
    async def orders_index() -> JSONResponse:
        records = []
        for path in sorted(orders_dir.glob("*.json"), reverse=True):
            try:
                records.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
        return JSONResponse(content=records)

    @router.get("/events")
    async def sse(request: Request) -> StreamingResponse:
        queue = bus.subscribe()

        async def stream():
            try:
                # Send a hello event so the client knows it's connected.
                yield _sse({"type": "hello"})
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=20.0)
                        yield _sse(event)
                    except asyncio.TimeoutError:
                        # keep-alive comment so proxies don't kill the stream
                        yield ": keep-alive\n\n"
            finally:
                bus.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return router


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


DASHBOARD_HTML = """<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <title>ai-phone · live dashboard</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root {
      --bg: #0b0d10;
      --panel: #14171c;
      --panel2: #1a1e25;
      --text: #e8edf2;
      --muted: #8c97a3;
      --accent: #4ade80;
      --accent-dim: #166534;
      --user: #60a5fa;
      --agent: #f472b6;
      --warning: #fbbf24;
      --border: #232830;
      --radius: 10px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font: 14px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    header {
      padding: 16px 24px;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      gap: 16px;
      background: var(--panel);
    }
    header h1 { margin: 0; font-size: 18px; font-weight: 600; }
    .pill {
      font-size: 11px;
      padding: 3px 8px;
      border-radius: 999px;
      background: var(--panel2);
      color: var(--muted);
      border: 1px solid var(--border);
    }
    .pill.live::before {
      content: "●";
      color: var(--accent);
      margin-right: 4px;
      animation: pulse 2s infinite;
    }
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }

    main {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
      padding: 16px 24px;
      max-width: 1400px;
      margin: 0 auto;
    }
    .col { display: flex; flex-direction: column; gap: 16px; }
    section {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      overflow: hidden;
    }
    section h2 {
      margin: 0;
      padding: 12px 16px;
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      border-bottom: 1px solid var(--border);
    }
    .body { padding: 12px 16px; max-height: 480px; overflow-y: auto; }
    .empty { color: var(--muted); padding: 12px; font-style: italic; }

    .stats {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 1px;
      background: var(--border);
    }
    .stat {
      background: var(--panel);
      padding: 16px;
      text-align: center;
    }
    .stat .v {
      display: block;
      font-size: 24px;
      font-weight: 700;
      color: var(--text);
    }
    .stat .l {
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--muted);
      margin-top: 2px;
    }

    .call {
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      margin-bottom: 12px;
      background: var(--panel2);
    }
    .call-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
      font-size: 12px;
    }
    .call-head .who { font-weight: 600; }
    .call-head .meta { color: var(--muted); font-family: ui-monospace, monospace; }
    .turn {
      padding: 6px 0;
      display: flex;
      gap: 8px;
    }
    .turn .role {
      flex: 0 0 56px;
      font-size: 10px;
      text-transform: uppercase;
      font-weight: 700;
      letter-spacing: 0.06em;
      padding-top: 2px;
    }
    .turn.user .role { color: var(--user); }
    .turn.agent .role { color: var(--agent); }
    .turn .text { color: var(--text); }

    .order {
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      margin-bottom: 8px;
      background: var(--panel2);
    }
    .order-head {
      display: flex;
      justify-content: space-between;
      font-size: 12px;
      color: var(--muted);
      font-family: ui-monospace, monospace;
    }
    .order-name { color: var(--text); font-weight: 600; }
    .order-total { color: var(--accent); font-weight: 700; font-size: 16px; }
    .order-items { font-size: 13px; color: var(--muted); margin-top: 4px; }
    .order-items li { margin-left: 16px; }
  </style>
</head>
<body>
  <header>
    <h1>ai-phone</h1>
    <span class="pill live" id="conn">connecting…</span>
    <span class="pill" id="clock"></span>
  </header>

  <main>
    <section style="grid-column: 1 / -1;">
      <h2>Today</h2>
      <div class="stats">
        <div class="stat"><span class="v" id="s-calls">0</span><span class="l">calls</span></div>
        <div class="stat"><span class="v" id="s-orders">0</span><span class="l">orders</span></div>
        <div class="stat"><span class="v" id="s-conv">0%</span><span class="l">conversion</span></div>
        <div class="stat"><span class="v" id="s-rev">$0</span><span class="l">revenue</span></div>
      </div>
    </section>

    <div class="col">
      <section>
        <h2>Active calls</h2>
        <div class="body" id="active"><div class="empty">No calls right now.</div></div>
      </section>
    </div>

    <div class="col">
      <section>
        <h2>Orders today</h2>
        <div class="body" id="orders"><div class="empty">No orders yet.</div></div>
      </section>
    </div>
  </main>

  <script>
    const calls = new Map(); // sid -> {from, name, turns:[]}
    let totalCalls = 0;
    let totalOrders = 0;
    let totalRevenue = 0;

    const $ = (id) => document.getElementById(id);

    function fmtMoney(n) {
      return "$" + n.toLocaleString("es-MX", { minimumFractionDigits: 0, maximumFractionDigits: 2 });
    }

    function renderActive() {
      const root = $("active");
      if (calls.size === 0) {
        root.innerHTML = '<div class="empty">No calls right now.</div>';
        return;
      }
      root.innerHTML = "";
      for (const [sid, c] of calls) {
        const card = document.createElement("div");
        card.className = "call";
        const head = document.createElement("div");
        head.className = "call-head";
        head.innerHTML = `<span class="who">${c.name || c.from || "Unknown"}</span><span class="meta">${sid.slice(0, 8)}</span>`;
        card.appendChild(head);
        for (const t of c.turns.slice(-8)) {
          const div = document.createElement("div");
          div.className = "turn " + t.role;
          div.innerHTML = `<span class="role">${t.role}</span><span class="text"></span>`;
          div.querySelector(".text").textContent = t.text;
          card.appendChild(div);
        }
        root.appendChild(card);
      }
    }

    function renderStats() {
      $("s-calls").textContent = totalCalls;
      $("s-orders").textContent = totalOrders;
      $("s-conv").textContent = totalCalls === 0 ? "0%" : Math.round((totalOrders / totalCalls) * 100) + "%";
      $("s-rev").textContent = fmtMoney(totalRevenue);
    }

    function addOrderToList(order) {
      const root = $("orders");
      if (root.querySelector(".empty")) root.innerHTML = "";
      const card = document.createElement("div");
      card.className = "order";
      const items = (order.items || []).map(it => `<li>${it.qty} × ${it.name}</li>`).join("");
      const time = (order.created_at || "").slice(11, 16);
      card.innerHTML = `
        <div class="order-head">
          <span class="order-name">${order.customer_name || "?"}</span>
          <span>${time} · ${order.mode || ""}</span>
        </div>
        <ul class="order-items">${items}</ul>
        <div style="text-align:right; margin-top: 6px;"><span class="order-total">${fmtMoney(order.total || 0)}</span></div>`;
      root.prepend(card);
    }

    async function loadOrders() {
      const r = await fetch("/orders.json");
      const data = await r.json();
      const today = new Date().toISOString().slice(0, 10);
      const todayOrders = data.filter(o => (o.created_at || "").startsWith(today));
      totalOrders = todayOrders.length;
      totalRevenue = todayOrders.reduce((s, o) => s + (o.total || 0), 0);
      renderStats();
      $("orders").innerHTML = "";
      if (todayOrders.length === 0) {
        $("orders").innerHTML = '<div class="empty">No orders yet.</div>';
      } else {
        todayOrders.forEach(addOrderToList);
      }
    }

    function connect() {
      const es = new EventSource("/events");
      es.onopen = () => { $("conn").textContent = "live"; };
      es.onerror = () => { $("conn").textContent = "reconnecting…"; };
      es.onmessage = (m) => {
        const e = JSON.parse(m.data);
        if (e.type === "call_started") {
          totalCalls += 1;
          calls.set(e.call_sid, { from: e.from, name: e.customer_name, turns: [] });
          renderActive(); renderStats();
        } else if (e.type === "user_said") {
          const c = calls.get(e.call_sid);
          if (c) { c.turns.push({ role: "user", text: e.text }); renderActive(); }
        } else if (e.type === "agent_said") {
          const c = calls.get(e.call_sid);
          if (c) { c.turns.push({ role: "agent", text: e.text }); renderActive(); }
        } else if (e.type === "order_saved") {
          totalOrders += 1;
          totalRevenue += (e.order.total || 0);
          addOrderToList(e.order);
          renderStats();
        } else if (e.type === "call_ended") {
          calls.delete(e.call_sid);
          renderActive();
        }
      };
    }

    function tickClock() {
      const now = new Date();
      $("clock").textContent = now.toLocaleTimeString("es-MX", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    }

    loadOrders();
    connect();
    setInterval(tickClock, 1000); tickClock();
  </script>
</body>
</html>
"""
