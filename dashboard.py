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
from collections import Counter
from datetime import datetime
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


def _today_iso() -> str:
    return datetime.now().date().isoformat()


def _load_orders(orders_dir: Path) -> list[dict]:
    out: list[dict] = []
    for path in sorted(orders_dir.glob("*.json"), reverse=True):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def _load_calls(logs_dir: Path) -> list[dict]:
    out: list[dict] = []
    if not logs_dir.exists():
        return out
    for path in sorted(logs_dir.glob("*.json"), reverse=True):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def _today_summary(orders: list[dict], calls: list[dict]) -> dict:
    today = _today_iso()
    todays_orders = [o for o in orders if (o.get("created_at") or "").startswith(today)]
    todays_calls = [c for c in calls if (c.get("started_at") or "").startswith(today)]

    revenue = sum(float(o.get("total") or 0) for o in todays_orders)
    n_calls = len(todays_calls)
    n_orders = len(todays_orders)
    conversion = 0 if n_calls == 0 else min(100, round((n_orders / n_calls) * 100))
    avg_ticket = 0 if n_orders == 0 else revenue / n_orders
    durations = [c["duration_s"] for c in todays_calls if c.get("duration_s") is not None]
    avg_duration_s = 0 if not durations else sum(durations) / len(durations)

    # Avg time-to-first-token across every agent turn that recorded latency.
    ttfts: list[float] = []
    for c in todays_calls:
        for t in c.get("turns", []) or []:
            if t.get("role") == "agent" and t.get("latency_ms"):
                ttfts.append(float(t["latency_ms"]))
    avg_ttft_ms = round(sum(ttfts) / len(ttfts)) if ttfts else 0

    # Hourly volume of calls today (24-bucket array, 0..23).
    hourly = [0] * 24
    for c in todays_calls:
        try:
            hour = int(c["started_at"][11:13])
            if 0 <= hour < 24:
                hourly[hour] += 1
        except Exception:
            continue

    item_counter: Counter = Counter()
    for o in todays_orders:
        for item in o.get("items", []) or []:
            item_counter[item.get("name", "?")] += int(item.get("qty") or 0)
    top_items = [
        {"name": name, "qty": qty}
        for name, qty in item_counter.most_common(5)
    ]

    return {
        "calls": n_calls,
        "orders": n_orders,
        "conversion": conversion,
        "revenue": revenue,
        "avg_ticket": avg_ticket,
        "avg_duration_s": round(avg_duration_s, 1),
        "avg_ttft_ms": avg_ttft_ms,
        "hourly_volume": hourly,
        "top_items": top_items,
    }


def _summarise_call(call: dict) -> dict:
    """Produce the slim shape used by the 'Recent calls' panel."""
    turns = call.get("turns") or []
    return {
        "call_sid": call.get("call_sid", ""),
        "caller_phone": call.get("caller_phone", ""),
        "customer_name": call.get("customer_name"),
        "started_at": call.get("started_at"),
        "duration_s": call.get("duration_s"),
        "saved": bool(call.get("order_id")),
        "order_total": call.get("order_total"),
        "turns": turns,
    }


def make_router(orders_dir: Path, logs_dir: Optional[Path] = None) -> APIRouter:
    """Build a FastAPI router exposing the dashboard."""
    logs_dir = logs_dir or (orders_dir.parent / "logs")
    router = APIRouter()

    @router.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_page() -> HTMLResponse:
        return HTMLResponse(content=DASHBOARD_HTML)

    @router.get("/orders.json")
    async def orders_index() -> JSONResponse:
        return JSONResponse(content=_load_orders(orders_dir))

    @router.get("/calls.json")
    async def calls_index() -> JSONResponse:
        return JSONResponse(content=[_summarise_call(c) for c in _load_calls(logs_dir)])

    @router.get("/today.json")
    async def today_summary() -> JSONResponse:
        orders = _load_orders(orders_dir)
        calls = _load_calls(logs_dir)
        today = _today_iso()
        todays_calls = [c for c in calls if (c.get("started_at") or "").startswith(today)]
        return JSONResponse(content={
            "stats": _today_summary(orders, calls),
            "orders": [o for o in orders if (o.get("created_at") or "").startswith(today)],
            "calls": [_summarise_call(c) for c in todays_calls],
        })

    @router.get("/events")
    async def sse(request: Request) -> StreamingResponse:
        queue = bus.subscribe()

        async def stream():
            try:
                yield _sse({"type": "hello"})
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=20.0)
                        yield _sse(event)
                    except asyncio.TimeoutError:
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
  <title>ai-phone · dashboard</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root {
      --bg: #0a0b0d;
      --panel: #111316;
      --panel2: #15181c;
      --text: #e3e6ea;
      --text-2: #b8bdc4;
      --muted: #6c727a;
      --dim: #3b4047;
      --accent: #4ade80;
      --border: #1c2026;
      --border-2: #262a31;
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; margin: 0; }
    body {
      font: 13px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      -webkit-font-smoothing: antialiased;
      letter-spacing: -0.005em;
    }
    .num { font-variant-numeric: tabular-nums; }
    .mono { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }

    header {
      display: grid;
      grid-template-columns: auto 1fr auto;
      align-items: center;
      gap: 16px;
      padding: 14px 24px;
      border-bottom: 1px solid var(--border);
      background: var(--panel);
      position: sticky; top: 0; z-index: 20;
    }
    .brand {
      font-size: 14px;
      font-weight: 600;
      letter-spacing: 0.02em;
    }
    .brand .dot {
      display: inline-block;
      width: 6px; height: 6px;
      border-radius: 50%;
      background: var(--accent);
      margin-right: 8px;
      vertical-align: 2px;
    }
    .meta {
      display: flex;
      gap: 16px;
      align-items: center;
      color: var(--muted);
      font-size: 12px;
    }
    .meta .sep { color: var(--dim); }
    .conn {
      font-size: 11px;
      color: var(--muted);
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    .conn::before {
      content: "";
      display: inline-block;
      width: 5px; height: 5px;
      border-radius: 50%;
      background: var(--muted);
      margin-right: 6px;
      vertical-align: 1px;
    }
    .conn.live { color: var(--accent); }
    .conn.live::before { background: var(--accent); animation: pulse 2.5s infinite; }
    @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.35; } }

    main {
      max-width: 1500px;
      margin: 0 auto;
      padding: 16px 24px 40px;
      display: grid;
      gap: 16px;
    }

    /* Stats strip */
    .stats {
      display: grid;
      grid-template-columns: repeat(8, 1fr);
      border: 1px solid var(--border);
      border-radius: 10px;
      overflow: hidden;
      background: var(--panel);
    }
    .stat {
      padding: 14px 16px;
      border-right: 1px solid var(--border);
      transition: background 120ms;
    }
    .stat:last-child { border-right: none; }
    .stat:hover { background: var(--panel2); }
    .stat .l {
      font-size: 10px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
      font-weight: 500;
    }
    .stat .v {
      font-size: 22px;
      font-weight: 600;
      color: var(--text);
      margin-top: 2px;
      letter-spacing: -0.02em;
    }
    .stat.accent .v { color: var(--accent); }
    @media (max-width: 1100px) { .stats { grid-template-columns: repeat(4, 1fr); } .stat { border-bottom: 1px solid var(--border); } }
    @media (max-width: 600px) { .stats { grid-template-columns: repeat(2, 1fr); } }

    /* Layout columns */
    .grid {
      display: grid;
      grid-template-columns: 1.2fr 1fr;
      gap: 16px;
    }
    @media (max-width: 1100px) { .grid { grid-template-columns: 1fr; } }
    .col { display: flex; flex-direction: column; gap: 16px; }

    section {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
    }
    .head {
      display: flex; align-items: center; gap: 8px;
      padding: 12px 16px;
      border-bottom: 1px solid var(--border);
    }
    .head h2 {
      margin: 0;
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.1em;
      color: var(--muted);
    }
    .head .badge {
      margin-left: auto;
      font-size: 11px;
      color: var(--muted);
      font-variant-numeric: tabular-nums;
    }
    .body { padding: 12px 16px; max-height: 420px; overflow-y: auto; }
    .body::-webkit-scrollbar { width: 6px; }
    .body::-webkit-scrollbar-thumb { background: var(--border-2); border-radius: 3px; }
    .empty { color: var(--muted); padding: 12px; font-size: 12px; }

    /* Active call card */
    .call {
      padding: 10px 12px;
      border: 1px solid var(--border-2);
      border-radius: 8px;
      margin-bottom: 8px;
      background: var(--panel2);
    }
    .call-head {
      display: flex; justify-content: space-between; align-items: baseline;
      margin-bottom: 6px;
      font-size: 12px;
    }
    .call-head .who { color: var(--text); font-weight: 600; }
    .call-head .who small { color: var(--muted); font-weight: 400; margin-left: 6px; font-size: 11px; }
    .call-head .meta { color: var(--muted); font-size: 11px; }
    .turn { display: flex; gap: 8px; padding: 3px 0; animation: fade .25s ease; }
    @keyframes fade { from { opacity: 0; } to { opacity: 1; } }
    .turn .who {
      flex: 0 0 18px;
      font-size: 10px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      padding-top: 2px;
    }
    .turn.user .who { color: #93c5fd; }
    .turn.agent .who { color: #d8b4fe; }
    .turn .text { color: var(--text-2); font-size: 13px; flex: 1; }

    /* Hourly chart */
    .chart {
      padding: 14px 16px 16px;
    }
    .bars {
      display: grid;
      grid-template-columns: repeat(24, 1fr);
      gap: 2px;
      align-items: end;
      height: 88px;
    }
    .bar {
      background: var(--dim);
      border-radius: 2px 2px 0 0;
      min-height: 2px;
      transition: background 120ms;
    }
    .bar:hover { background: var(--accent); }
    .bar.has { background: var(--accent); opacity: 0.85; }
    .bar.now { outline: 1px solid var(--text); outline-offset: 1px; }
    .bars-axis {
      display: grid;
      grid-template-columns: repeat(24, 1fr);
      gap: 2px;
      margin-top: 4px;
      font-size: 9px;
      color: var(--muted);
      font-variant-numeric: tabular-nums;
      text-align: center;
    }
    .bars-axis span { opacity: 0.4; }
    .bars-axis span.label { opacity: 1; }

    /* Top items */
    .top-items { padding: 10px 16px; }
    .top-row {
      display: grid;
      grid-template-columns: minmax(120px, 1fr) 80px 36px;
      align-items: center;
      gap: 10px;
      padding: 6px 0;
    }
    .top-row + .top-row { border-top: 1px solid var(--border); }
    .top-row .name { color: var(--text-2); font-size: 13px; }
    .top-row .qty { color: var(--text); font-weight: 600; text-align: right; font-variant-numeric: tabular-nums; font-size: 13px; }
    .top-row .bar-bg {
      height: 4px;
      background: var(--border-2);
      border-radius: 2px;
      overflow: hidden;
    }
    .top-row .bar-fg {
      height: 100%;
      background: var(--accent);
      border-radius: 2px;
      transition: width .3s ease;
    }

    /* Orders */
    .order {
      padding: 10px 12px;
      border: 1px solid var(--border-2);
      border-radius: 8px;
      margin-bottom: 8px;
      background: var(--panel2);
      animation: fade .25s ease;
    }
    .order.new {
      border-color: var(--accent);
      animation: glow 2s ease;
    }
    @keyframes glow {
      0% { box-shadow: 0 0 0 2px rgba(74,222,128,0.3); }
      100% { box-shadow: 0 0 0 0 rgba(74,222,128,0); }
    }
    .order-head {
      display: flex; justify-content: space-between; align-items: baseline;
      margin-bottom: 6px;
    }
    .order-head .name { color: var(--text); font-weight: 600; font-size: 13px; }
    .order-head .name .mode {
      margin-left: 8px;
      font-size: 10px;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      color: var(--muted);
    }
    .order-head .time { color: var(--muted); font-size: 11px; }
    .order-items { font-size: 12px; color: var(--text-2); margin: 4px 0; padding: 0; list-style: none; }
    .order-items li::before { content: "·"; color: var(--dim); margin: 0 6px; }
    .order-items li { display: inline; }
    .order-items li b { color: var(--text); font-weight: 600; }
    .order-foot { text-align: right; font-size: 14px; font-weight: 600; color: var(--accent); font-variant-numeric: tabular-nums; }
    .order-foot small { color: var(--muted); font-weight: 400; font-size: 10px; margin-left: 4px; letter-spacing: 0.05em; }

    /* Recent calls */
    .recent-list { padding: 4px 0; }
    .recent {
      display: grid;
      grid-template-columns: 12px 1fr auto auto;
      gap: 10px;
      padding: 8px 16px;
      align-items: center;
      cursor: pointer;
      border-bottom: 1px solid var(--border);
      transition: background 120ms;
    }
    .recent:hover { background: var(--panel2); }
    .recent:last-child { border-bottom: none; }
    .recent .dot {
      width: 6px; height: 6px;
      border-radius: 50%;
      background: var(--dim);
      justify-self: center;
    }
    .recent.saved .dot { background: var(--accent); }
    .recent .who { color: var(--text-2); font-size: 13px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .recent .who small { color: var(--muted); font-size: 11px; margin-left: 6px; }
    .recent .total { color: var(--text); font-size: 12px; font-variant-numeric: tabular-nums; }
    .recent .when { color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
    .transcript {
      grid-column: 1 / -1;
      padding: 10px 24px 14px;
      background: var(--bg);
      border-top: 1px solid var(--border);
      display: none;
    }
    .recent.open + .transcript { display: block; }

    /* Toast */
    .toast {
      position: fixed; bottom: 18px; right: 18px;
      background: var(--panel);
      border: 1px solid var(--accent);
      border-radius: 8px;
      padding: 10px 14px;
      font-size: 12px;
      box-shadow: 0 6px 18px rgba(0,0,0,0.4);
      animation: fade .25s ease;
    }
    .toast strong { color: var(--accent); font-size: 13px; }
    .toast small { display: block; color: var(--muted); margin-top: 2px; }
  </style>
</head>
<body>
  <header>
    <span class="brand"><span class="dot"></span>ai-phone</span>
    <div class="meta">
      <span id="date"></span>
      <span class="sep">·</span>
      <span id="clock" class="mono"></span>
      <span class="sep">·</span>
      <span id="last-event">no events yet</span>
    </div>
    <span id="conn" class="conn">connecting</span>
  </header>

  <main>
    <div class="stats">
      <div class="stat"><div class="l">Calls</div><div class="v num" id="s-calls">0</div></div>
      <div class="stat"><div class="l">Orders</div><div class="v num" id="s-orders">0</div></div>
      <div class="stat"><div class="l">Conv</div><div class="v num" id="s-conv">0%</div></div>
      <div class="stat accent"><div class="l">Revenue</div><div class="v num" id="s-rev">$0</div></div>
      <div class="stat"><div class="l">Avg ticket</div><div class="v num" id="s-avg">$0</div></div>
      <div class="stat"><div class="l">Avg call</div><div class="v num" id="s-dur">0s</div></div>
      <div class="stat"><div class="l">TTFT</div><div class="v num" id="s-ttft">0ms</div></div>
      <div class="stat"><div class="l">Active</div><div class="v num" id="s-active">0</div></div>
    </div>

    <div class="grid">
      <div class="col">
        <section>
          <div class="head">
            <h2>Now</h2>
            <span class="badge" id="active-count">0 calls</span>
          </div>
          <div class="body" id="active"><div class="empty">No calls right now.</div></div>
        </section>

        <section>
          <div class="head"><h2>Hourly volume</h2><span class="badge" id="hourly-total">0 today</span></div>
          <div class="chart">
            <div class="bars" id="bars"></div>
            <div class="bars-axis" id="bars-axis"></div>
          </div>
        </section>

        <section>
          <div class="head"><h2>Top items today</h2></div>
          <div class="top-items" id="top-items"><div class="empty">No items sold yet.</div></div>
        </section>
      </div>

      <div class="col">
        <section>
          <div class="head"><h2>Orders today</h2><span class="badge" id="orders-count">0</span></div>
          <div class="body" id="orders"><div class="empty">No orders yet.</div></div>
        </section>

        <section>
          <div class="head"><h2>Recent calls</h2><span class="badge" id="recent-count">0</span></div>
          <div id="recent" class="recent-list"><div class="empty">No calls yet.</div></div>
        </section>
      </div>
    </div>
  </main>

  <div id="toast-host"></div>

  <script>
    const calls = new Map();
    const todaysOrders = [];
    let recentCalls = [];
    let lastEventAt = null;
    let activeCount = 0;

    const mxn = new Intl.NumberFormat("es-MX", { style: "currency", currency: "MXN", maximumFractionDigits: 0 });
    const mxn2 = new Intl.NumberFormat("es-MX", { style: "currency", currency: "MXN", maximumFractionDigits: 2 });
    const $ = (id) => document.getElementById(id);
    const fmtMoney = (n, dec = false) => (dec ? mxn2 : mxn).format(n || 0);
    const fmtDuration = (s) => {
      if (!s && s !== 0) return "—";
      if (s < 60) return Math.round(s) + "s";
      const m = Math.floor(s / 60);
      const r = Math.round(s % 60);
      return r === 0 ? m + "m" : m + "m" + r + "s";
    };
    const fmtTime = (iso) => (iso || "").slice(11, 16);
    const escapeHtml = (s) => (s || "").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));

    function renderActive() {
      const root = $("active");
      activeCount = calls.size;
      $("active-count").textContent = activeCount + (activeCount === 1 ? " call" : " calls");
      $("s-active").textContent = activeCount;
      if (activeCount === 0) {
        root.innerHTML = '<div class="empty">No calls right now.</div>';
        return;
      }
      root.innerHTML = "";
      for (const [sid, c] of calls) {
        const el = document.createElement("div");
        el.className = "call";
        const turns = c.turns.slice(-6).map(t =>
          `<div class="turn ${t.role}"><span class="who">${t.role[0]}</span><span class="text"></span></div>`
        ).join("");
        el.innerHTML = `
          <div class="call-head">
            <span class="who">${escapeHtml(c.name || "Unknown")}<small>${escapeHtml(c.from || "")}</small></span>
            <span class="meta mono">${sid.slice(0, 8)}</span>
          </div>${turns}`;
        // populate text safely (textContent)
        const turnEls = el.querySelectorAll(".turn .text");
        c.turns.slice(-6).forEach((t, i) => { turnEls[i].textContent = t.text; });
        root.appendChild(el);
      }
    }

    function renderStats(s) {
      $("s-calls").textContent = s.calls;
      $("s-orders").textContent = s.orders;
      $("s-conv").textContent = s.conversion + "%";
      $("s-rev").textContent = fmtMoney(s.revenue);
      $("s-avg").textContent = fmtMoney(s.avg_ticket);
      $("s-dur").textContent = fmtDuration(s.avg_duration_s);
      $("s-ttft").textContent = (s.avg_ttft_ms || 0) + "ms";
    }

    function renderHourly(hourly) {
      const max = Math.max(1, ...hourly);
      const total = hourly.reduce((a, b) => a + b, 0);
      const nowH = new Date().getHours();
      $("hourly-total").textContent = total + (total === 1 ? " call" : " calls");
      $("bars").innerHTML = hourly.map((v, i) => {
        const h = (v / max) * 100;
        const cls = (v > 0 ? "has " : "") + (i === nowH ? "now" : "");
        return `<div class="bar ${cls}" style="height:${Math.max(3, h)}%" title="${i.toString().padStart(2,'0')}:00 — ${v} call${v===1?'':'s'}"></div>`;
      }).join("");
      $("bars-axis").innerHTML = Array.from({length: 24}, (_, i) =>
        `<span class="${i % 6 === 0 ? 'label' : ''}">${i % 6 === 0 ? i : ''}</span>`
      ).join("");
    }

    function renderTopItems(items) {
      const root = $("top-items");
      if (!items || items.length === 0) {
        root.innerHTML = '<div class="empty">No items sold yet.</div>';
        return;
      }
      const max = Math.max(...items.map(i => i.qty));
      root.innerHTML = items.map(i => `
        <div class="top-row">
          <span class="name">${escapeHtml(i.name)}</span>
          <div class="bar-bg"><div class="bar-fg" style="width:${(i.qty/max)*100}%"></div></div>
          <span class="qty num">${i.qty}</span>
        </div>`).join("");
    }

    function recomputeFromState() {
      const revenue = todaysOrders.reduce((s, o) => s + (o.total || 0), 0);
      const totalCalls = window.__callsToday || 0;
      const stats = {
        calls: totalCalls,
        orders: todaysOrders.length,
        conversion: totalCalls === 0 ? 0 : Math.min(100, Math.round((todaysOrders.length / totalCalls) * 100)),
        revenue,
        avg_ticket: todaysOrders.length === 0 ? 0 : revenue / todaysOrders.length,
        avg_duration_s: window.__avgDuration || 0,
        avg_ttft_ms: window.__avgTtft || 0,
      };
      renderStats(stats);

      const counter = new Map();
      for (const o of todaysOrders) for (const it of (o.items || [])) {
        counter.set(it.name, (counter.get(it.name) || 0) + (it.qty || 0));
      }
      const items = [...counter.entries()].sort((a,b)=>b[1]-a[1]).slice(0,5).map(([name,qty])=>({name,qty}));
      renderTopItems(items);

      const hourly = window.__hourly || new Array(24).fill(0);
      renderHourly(hourly);
    }

    function renderOrder(order, isNew) {
      const root = $("orders");
      if (root.querySelector(".empty")) root.innerHTML = "";
      const el = document.createElement("div");
      el.className = "order" + (isNew ? " new" : "");
      const items = (order.items || []).map(it =>
        `<li><b>${it.qty}×</b> ${escapeHtml(it.name)}</li>`
      ).join("");
      const mode = order.mode === "pickup" ? "para llevar" : (order.mode === "dine_in" ? "comer aquí" : "");
      el.innerHTML = `
        <div class="order-head">
          <span class="name">${escapeHtml(order.customer_name || "?")}<span class="mode">${mode}</span></span>
          <span class="time mono">${fmtTime(order.created_at)}</span>
        </div>
        <ul class="order-items">${items}</ul>
        <div class="order-foot">${fmtMoney(order.total, true)}<small>MXN</small></div>`;
      root.prepend(el);
      $("orders-count").textContent = todaysOrders.length;
    }

    function renderRecentCalls() {
      const root = $("recent");
      $("recent-count").textContent = recentCalls.length;
      if (recentCalls.length === 0) {
        root.innerHTML = '<div class="empty">No calls yet.</div>';
        return;
      }
      root.innerHTML = "";
      recentCalls.slice(0, 12).forEach((c, i) => {
        const row = document.createElement("div");
        row.className = "recent" + (c.saved ? " saved" : "");
        row.dataset.i = i;
        row.innerHTML = `
          <span class="dot"></span>
          <span class="who">${escapeHtml(c.customer_name || "Unknown")}<small>${escapeHtml(c.caller_phone || "")}</small></span>
          <span class="total">${c.saved ? fmtMoney(c.order_total) : "—"}</span>
          <span class="when mono">${fmtTime(c.started_at)} · ${fmtDuration(c.duration_s)}</span>`;
        const trans = document.createElement("div");
        trans.className = "transcript";
        trans.innerHTML = (c.turns || []).map(t =>
          `<div class="turn ${t.role}"><span class="who">${t.role[0]}</span><span class="text"></span></div>`
        ).join("") || '<div class="empty">No transcript.</div>';
        const turnEls = trans.querySelectorAll(".turn .text");
        (c.turns || []).forEach((t, j) => { if (turnEls[j]) turnEls[j].textContent = t.text; });
        row.addEventListener("click", () => row.classList.toggle("open"));
        root.appendChild(row);
        root.appendChild(trans);
      });
    }

    function showToast(order) {
      const t = document.createElement("div");
      t.className = "toast";
      t.innerHTML = `<strong>+ ${fmtMoney(order.total)}</strong>
        <small>${escapeHtml(order.customer_name)} · ${(order.items || []).length} ítems</small>`;
      $("toast-host").appendChild(t);
      setTimeout(() => t.remove(), 4000);
    }

    function pingEvent() {
      lastEventAt = Date.now();
      $("last-event").textContent = "live";
    }

    setInterval(() => {
      if (!lastEventAt) return;
      const sec = Math.floor((Date.now() - lastEventAt) / 1000);
      $("last-event").textContent = sec < 5 ? "just now" : sec < 60 ? sec + "s ago" : Math.floor(sec/60) + "m ago";
    }, 1000);

    async function loadInitial() {
      const r = await fetch("/today.json");
      const data = await r.json();
      window.__callsToday = data.stats.calls;
      window.__avgDuration = data.stats.avg_duration_s;
      window.__avgTtft = data.stats.avg_ttft_ms;
      window.__hourly = data.stats.hourly_volume;
      todaysOrders.length = 0;
      todaysOrders.push(...data.orders);
      recentCalls = data.calls;
      renderStats(data.stats);
      renderHourly(data.stats.hourly_volume);
      renderTopItems(data.stats.top_items);
      $("orders").innerHTML = "";
      $("orders-count").textContent = data.orders.length;
      if (data.orders.length === 0) {
        $("orders").innerHTML = '<div class="empty">No orders yet.</div>';
      } else {
        for (let i = data.orders.length - 1; i >= 0; i--) renderOrder(data.orders[i], false);
      }
      renderRecentCalls();
    }

    function connect() {
      const es = new EventSource("/events");
      es.onopen = () => { $("conn").textContent = "live"; $("conn").classList.add("live"); };
      es.onerror = () => { $("conn").textContent = "reconnecting"; $("conn").classList.remove("live"); };
      es.onmessage = (m) => {
        const e = JSON.parse(m.data);
        pingEvent();
        if (e.type === "call_started") {
          window.__callsToday = (window.__callsToday || 0) + 1;
          calls.set(e.call_sid, { from: e.from, name: e.customer_name, turns: [], started_at: e.ts });
          renderActive();
          recomputeFromState();
        } else if (e.type === "user_said") {
          const c = calls.get(e.call_sid);
          if (c) { c.turns.push({ role: "user", text: e.text }); renderActive(); }
        } else if (e.type === "agent_said") {
          const c = calls.get(e.call_sid);
          if (c) { c.turns.push({ role: "agent", text: e.text }); renderActive(); }
        } else if (e.type === "order_saved") {
          todaysOrders.push(e.order);
          renderOrder(e.order, true);
          showToast(e.order);
          recomputeFromState();
        } else if (e.type === "call_ended") {
          // Pull a fresh recent-calls list so the call appears with its log
          fetch("/today.json").then(r => r.json()).then(d => {
            recentCalls = d.calls;
            window.__avgDuration = d.stats.avg_duration_s;
            window.__avgTtft = d.stats.avg_ttft_ms;
            window.__hourly = d.stats.hourly_volume;
            renderRecentCalls();
            recomputeFromState();
          });
          calls.delete(e.call_sid);
          renderActive();
        }
      };
    }

    function tickClock() {
      const now = new Date();
      $("clock").textContent = now.toLocaleTimeString("es-MX", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
      $("date").textContent = now.toLocaleDateString("es-MX", { weekday: "short", day: "numeric", month: "short" });
    }

    loadInitial();
    connect();
    setInterval(tickClock, 1000); tickClock();
  </script>
</body>
</html>
"""
