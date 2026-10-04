"""
dashboard.py — локальный дашборд радара. Ничего не пишет на диск.
"""
import json
import logging
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from collector_settings import Settings, load_settings

log = logging.getLogger("dashboard")

PAGE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TrendDetector</title>
<style>
:root {
  --bg: #111418; --fg: #e5e7eb; --mut: #9ca3af; --card: #1b1f26; --card2: #232832;
  --acc: #60a5fa; --bad: #f87171; --good: #4ade80; --warn: #fbbf24; --neutral: #6b7280;
  --c-trend-up: #4ade80; --c-trend-down: #f87171;
  --c-sqz-up: #a78bfa; --c-sqz-down: #f472b6;
  --c-acc-long: #fbbf24; --c-acc-short: #fb923c;
  --c-flat: #6b7280;
}
* { box-sizing: border-box; }
body { margin: 0; padding: 12px; background: var(--bg); color: var(--fg); font: 14px/1.4 system-ui, sans-serif; }

.header { display: flex; gap: 12px; align-items: stretch; margin-bottom: 12px; }

.market-card { background: var(--card); padding: 16px; border-radius: 8px; flex: 1; text-align: center; }
.market-state { font-size: 24px; font-weight: bold; text-transform: uppercase; margin-bottom: 8px; }
.ms-LONG_TREND { color: var(--c-trend-up); }
.ms-SHORT_TREND { color: var(--c-trend-down); }
.ms-SQUEEZE_RISK { color: var(--c-sqz-up); }
.ms-MIXED { color: var(--warn); }
.ms-CHOP { color: var(--mut); }
.market-age { color: var(--mut); font-size: 13px; }

.chips { display: flex; flex-wrap: wrap; gap: 8px; flex: 2; align-content: center; }
.chip { background: var(--card); padding: 8px 12px; border-radius: 6px; cursor: pointer; border-left: 4px solid transparent; font-weight: bold; display: flex; justify-content: space-between; min-width: 130px; user-select: none; }
.chip.dim { opacity: 0.4; }
.chip-trend_up { border-left-color: var(--c-trend-up); color: var(--c-trend-up); }
.chip-trend_down { border-left-color: var(--c-trend-down); color: var(--c-trend-down); }
.chip-squeeze_up { border-left-color: var(--c-sqz-up); color: var(--c-sqz-up); }
.chip-squeeze_down { border-left-color: var(--c-sqz-down); color: var(--c-sqz-down); }
.chip-acc_long { border-left-color: var(--c-acc-long); color: var(--c-acc-long); }
.chip-acc_short { border-left-color: var(--c-acc-short); color: var(--c-acc-short); }
.chip-flat { border-left-color: var(--c-flat); color: var(--c-flat); }
.chip span { color: var(--fg); }

.layout { display: flex; gap: 12px; align-items: flex-start; }
.main-grid { flex: 3; display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 8px; }
.side-panel { flex: 1; min-width: 300px; display: flex; flex-direction: column; gap: 12px; }

.coin { background: var(--card); border-radius: 6px; padding: 10px; border-top: 3px solid transparent; cursor: pointer; position: relative; overflow: hidden; }
.c-trend_up { border-top-color: var(--c-trend-up); }
.c-trend_down { border-top-color: var(--c-trend-down); }
.c-squeeze_up { border-top-color: var(--c-sqz-up); }
.c-squeeze_down { border-top-color: var(--c-sqz-down); }
.c-acc_long { border-top-color: var(--c-acc-long); }
.c-acc_short { border-top-color: var(--c-acc-short); }
.c-flat { border-top-color: var(--c-flat); opacity: 0.6; }

.coin .sym { font-weight: bold; font-size: 15px; }
.coin .age { float: right; color: var(--mut); font-size: 12px; }
.coin .metrics { display: flex; justify-content: space-between; margin-top: 6px; font-size: 12px; }
.coin .metrics > div { display: flex; flex-direction: column; }
.coin .lbl { color: var(--mut); font-size: 10px; }
.coin .val { font-variant-numeric: tabular-nums; }

.phase-box { background: var(--card); border-radius: 6px; padding: 12px; }
.phase-box h3 { margin: 0 0 10px 0; font-size: 14px; color: var(--mut); text-transform: uppercase; }
.phase-item { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid var(--card2); font-size: 13px; }
.phase-item:last-child { border-bottom: none; }
.phase-item .sym { font-weight: bold; }
.phase-item .age { color: var(--mut); }

.controls { margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center; color: var(--mut); font-size: 13px; }
button { background: var(--card); border: 1px solid var(--neutral); color: var(--fg); padding: 4px 12px; border-radius: 4px; cursor: pointer; }
button:hover { background: var(--card2); }
.paused { border-color: var(--warn); color: var(--warn); }

.flash { animation: flash-bg 0.5s ease-out; }
@keyframes flash-bg { 0% { background: var(--good); } 100% { background: var(--bg); } }
</style>
</head><body>

<div class="controls">
  <span id="status">Загрузка...</span>
  <button id="btn_pause">Пауза: ВЫКЛ</button>
</div>

<div class="header">
  <div class="market-card">
    <div id="ms_label" class="market-state ms-CHOP">CHOP</div>
    <div id="ms_age" class="market-age">0 мин</div>
  </div>
  <div class="chips" id="chips"></div>
</div>

<div class="layout">
  <div class="main-grid" id="grid"></div>
  <div class="side-panel">
    <div class="phase-box">
      <h3>Ожидают прорыв вверх</h3>
      <div id="ph_acc_long"></div>
    </div>
    <div class="phase-box">
      <h3>Ожидают пролив вниз</h3>
      <div id="ph_acc_short"></div>
    </div>
    <div class="phase-box">
      <h3>Едут (тренд)</h3>
      <div id="ph_trend"></div>
    </div>
    <div class="phase-box">
      <h3>Опасные (сквиз)</h3>
      <div id="ph_sqz"></div>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
let paused = false;
let filterState = null;
let lastMarketState = null;

const STATES = [
  { id: 'trend_up', label: 'TREND UP' },
  { id: 'trend_down', label: 'TREND DOWN' },
  { id: 'squeeze_up', label: 'SQUEEZE UP' },
  { id: 'squeeze_down', label: 'SQUEEZE DOWN' },
  { id: 'acc_long', label: 'ACC LONG' },
  { id: 'acc_short', label: 'ACC SHORT' },
  { id: 'flat', label: 'FLAT' },
];

$('btn_pause').onclick = function() {
  paused = !paused;
  this.textContent = paused ? 'Пауза: ВКЛ' : 'Пауза: ВЫКЛ';
  this.className = paused ? 'paused' : '';
};

function playSound() {
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.connect(gain);
  gain.connect(ctx.destination);
  osc.type = 'sine';
  osc.frequency.setValueAtTime(880, ctx.currentTime);
  gain.gain.setValueAtTime(0.1, ctx.currentTime);
  gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.5);
  osc.start(ctx.currentTime);
  osc.stop(ctx.currentTime + 0.5);
}

function flashScreen() {
  document.body.classList.remove('flash');
  void document.body.offsetWidth;
  document.body.classList.add('flash');
}

const fmtAge = sec => {
  if (sec == null) return '—';
  if (sec < 60) return Math.round(sec) + 'с';
  if (sec < 3600) return Math.round(sec/60) + 'м';
  return (sec/3600).toFixed(1) + 'ч';
};

function renderChips(counts) {
  let html = '';
  for (const s of STATES) {
    const active = filterState === s.id || !filterState;
    const cls = `chip chip-${s.id} ${active ? '' : 'dim'}`;
    html += `<div class="${cls}" onclick="setFilter('${s.id}')">
               ${s.label} <span>${counts[s.id] || 0}</span>
             </div>`;
  }
  $('chips').innerHTML = html;
}

window.setFilter = function(id) {
  filterState = filterState === id ? null : id;
  // wait for next tick
};

function renderGrid(coins) {
  const order = { 'trend_up': 1, 'trend_down': 2, 'squeeze_up': 3, 'squeeze_down': 4, 'acc_long': 5, 'acc_short': 6, 'flat': 7 };
  let filtered = coins;
  if (filterState) {
    filtered = coins.filter(c => c.coin_state === filterState);
  }
  filtered.sort((a, b) => {
    const oa = order[a.coin_state] || 99;
    const ob = order[b.coin_state] || 99;
    if (oa !== ob) return oa - ob;
    return (a.state_age_sec || 0) - (b.state_age_sec || 0);
  });
  
  let html = '';
  for (const c of filtered) {
    const age = fmtAge(c.state_age_sec);
    const tox = (c.toxic_1h * 100).toFixed(0) + '%';
    const ret = c.ret_30s_bps != null ? c.ret_30s_bps.toFixed(1) : '—';
    const oi = c.oi_trend_15m != null ? c.oi_trend_15m.toFixed(1) + '%' : '—';
    html += `<div class="coin c-${c.coin_state}" onclick="alert(JSON.stringify(${JSON.stringify(c).replace(/"/g, '&quot;')}, null, 2))">
      <div><span class="sym">${c.symbol}</span> <span class="age">${age}</span></div>
      <div class="metrics">
        <div><span class="lbl">TOXIC</span><span class="val">${tox}</span></div>
        <div><span class="lbl">RET 30s</span><span class="val">${ret}</span></div>
        <div><span class="lbl">OI 15m</span><span class="val">${oi}</span></div>
      </div>
    </div>`;
  }
  $('grid').innerHTML = html;
}

function renderPhases(coins) {
  const byState = id => coins.filter(c => c.coin_state === id).sort((a, b) => (a.state_age_sec||0) - (b.state_age_sec||0));
  
  const mapPhase = arr => arr.map(c => `<div class="phase-item"><span class="sym">${c.symbol}</span><span class="age">${fmtAge(c.state_age_sec)}</span></div>`).join('');
  
  $('ph_acc_long').innerHTML = mapPhase(byState('acc_long').filter(c => c.state_age_sec < 900)) || '<div class="phase-item">пусто</div>';
  $('ph_acc_short').innerHTML = mapPhase(byState('acc_short').filter(c => c.state_age_sec < 900)) || '<div class="phase-item">пусто</div>';
  
  const trends = coins.filter(c => c.coin_state === 'trend_up' || c.coin_state === 'trend_down').sort((a,b)=>(a.state_age_sec||0)-(b.state_age_sec||0));
  $('ph_trend').innerHTML = mapPhase(trends) || '<div class="phase-item">пусто</div>';
  
  const sqz = coins.filter(c => c.coin_state === 'squeeze_up' || c.coin_state === 'squeeze_down').sort((a,b)=>(a.state_age_sec||0)-(b.state_age_sec||0));
  $('ph_sqz').innerHTML = mapPhase(sqz) || '<div class="phase-item">пусто</div>';
}

// Track previous coin states for audio alert
let lastCoinStates = {};

async function tick() {
  if (paused) return;
  try {
    const r = await fetch('/api', {cache: 'no-store'});
    const d = await r.json();
    
    if (d.waiting) {
      $('status').textContent = 'Ждём данных...';
      return;
    }
    
    const br = d.toxic_breadth || {};
    const ms = br.market_state || 'CHOP';
    
    let playAlert = false;
    
    if (lastMarketState && lastMarketState !== ms) {
      playAlert = true;
      flashScreen();
    }
    lastMarketState = ms;
    
    // Check ACC -> TREND transitions
    const coins = d.toxic_top || [];
    for (const c of coins) {
      const p = lastCoinStates[c.symbol];
      if (p && (p.startsWith('acc_') && c.coin_state.startsWith('trend_'))) {
        playAlert = true;
      }
      lastCoinStates[c.symbol] = c.coin_state;
    }
    
    if (playAlert) {
      playSound();
    }
    
    $('ms_label').textContent = ms;
    $('ms_label').className = 'market-state ms-' + ms;
    $('ms_age').textContent = fmtAge(br.market_state_age_sec || 0);
    
    renderChips(br.state_counts || {});
    renderGrid(coins);
    renderPhases(coins);
    
    $('status').textContent = 'Обновлено ' + new Date().toLocaleTimeString();
  } catch (e) {
    $('status').textContent = 'Ошибка соединения!';
  }
}

tick();
setInterval(tick, 2000);
</script></body></html>
"""


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


class Dashboard:
    def __init__(self, settings: Optional[Settings] = None, **overrides):
        S = settings or load_settings()
        g = lambda name: overrides.get(name, getattr(S, name))  # noqa: E731
        self.enabled = bool(g("dash_enabled"))
        self.host = str(g("dash_host"))
        self.port = int(g("dash_port"))
        self._t0 = time.time()
        self._every = 2.0
        self._last_pub = 0.0
        self._lock = threading.Lock()
        self._state: Dict[str, Any] = {"waiting": True}
        self._server: Optional[_Server] = None

    def start(self) -> bool:
        if not self.enabled:
            return False
        try:
            dash = self

            class H(BaseHTTPRequestHandler):
                def log_message(self, *a):
                    pass

                def do_GET(self):  # noqa: N802
                    path = self.path.split("?")[0]
                    if path == "/api":
                        body = json.dumps(dash.state(), ensure_ascii=False, default=str).encode("utf-8")
                        ctype = "application/json; charset=utf-8"
                    elif path in ("/", "/index.html"):
                        body = PAGE.encode("utf-8")
                        ctype = "text/html; charset=utf-8"
                    else:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)

            self._server = _Server((self.host, self.port), H)
        except OSError as e:
            log.error(f"[dashboard] не удалось занять {self.host}:{self.port}: {e}")
            return False
        threading.Thread(target=self._server.serve_forever, name="dashboard", daemon=True).start()
        log.info(f"[dashboard] http://{self.host}:{self.port}")
        return True

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None

    def state(self) -> Dict[str, Any]:
        with self._lock:
            return self._state

    def publish(self, engine, streams=None) -> None:
        try:
            now = time.time()
            if now - self._last_pub < self._every:
                return
            self._last_pub = now
            snap = engine.get_snapshot()
            h = snap["health"]
            state = {
                "ts": now,
                "uptime_sec": h["uptime_sec"],
                "integrity": h["integrity"],
                "sizes": h["sizes"],
                "active_ignitions": snap.get("active_ignitions", []),
                "toxic_top": snap.get("toxic_top", []),
                "toxic_breadth": snap.get("toxic_breadth", {}),
                "errors": h["errors"],
                "streams": streams,
            }
            with self._lock:
                self._state = state
        except Exception as e:
            log.error(f"[dashboard] publish failed: {e}", exc_info=True)
