"""
dashboard.py — локальный дашборд радара. Ничего не пишет на диск.

    from dashboard import Dashboard
    dash = Dashboard()
    dash.start()
    ...
    dash.publish(engine)   # вызывается из главного цикла
"""
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from collector_settings import Settings, load_settings

log = logging.getLogger("dashboard")

PAGE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TrendDetector — режим рынка</title>
<style>
:root{--bg:#fff;--fg:#1a1d21;--mut:#6b7280;--card:#f3f4f6;--card2:#e9ecef;--bar:#dfe3e8;
      --acc:#2563eb;--bad:#dc2626;--good:#16a34a;--warn:#d97706;--neutral:#9ca3af}
@media(prefers-color-scheme:dark){
  :root{--bg:#111418;--fg:#e5e7eb;--mut:#9ca3af;--card:#1b1f26;--card2:#232832;--bar:#2a303a;
        --acc:#60a5fa;--bad:#f87171;--good:#4ade80;--warn:#fbbf24;--neutral:#6b7280}}
*{box-sizing:border-box}
body{margin:0;padding:20px;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
h1{font-size:18px;margin:0 0 4px}
.sub{color:var(--mut);font-size:13px;margin-bottom:16px}
.sub .bad{color:var(--bad)}.sub .good{color:var(--good)}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));margin-bottom:12px}
.card{background:var(--card);border-radius:10px;padding:14px 16px;margin-bottom:12px}
.card h2{font-size:13px;font-weight:600;color:var(--mut);margin:0 0 10px;text-transform:uppercase;letter-spacing:.04em}
.big{font-size:26px;font-weight:600}.big small{font-size:14px;color:var(--mut);font-weight:400}
.meta{color:var(--mut);font-size:13px}
.kv{display:grid;grid-template-columns:1fr auto;gap:4px 12px;font-size:14px}
.kv span:nth-child(odd){color:var(--mut)}.kv span:nth-child(even){text-align:right;font-variant-numeric:tabular-nums}
table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:4px 10px 4px 0;white-space:nowrap}th{color:var(--mut);font-weight:500}

/* режим рынка */
.regime{padding:14px 18px;border-radius:10px;font-size:22px;font-weight:700;letter-spacing:.05em;text-align:center;margin-bottom:10px}
.regime.trend-up{background:rgba(22,163,74,.15);color:var(--good);border:2px solid var(--good)}
.regime.trend-down{background:rgba(220,38,38,.15);color:var(--bad);border:2px solid var(--bad)}
.regime.accum{background:rgba(217,119,6,.15);color:var(--warn);border:2px solid var(--warn)}
.regime.flat{background:var(--card2);color:var(--mut);border:2px solid var(--neutral)}

/* полоса long-short */
.ls-bar{display:flex;height:22px;border-radius:6px;overflow:hidden;background:var(--bar);margin:6px 0 8px;position:relative}
.ls-bar .l{background:var(--good);transition:width .6s}
.ls-bar .r{background:var(--bad);transition:width .6s}
.ls-bar .n{background:var(--neutral);transition:width .6s}
.ls-mark{position:absolute;top:-4px;bottom:-4px;width:2px;background:var(--fg);opacity:.4}
.ls-lbl{display:flex;justify-content:space-between;font-size:13px;color:var(--mut);margin-bottom:10px}
.ls-lbl b{color:var(--fg);font-weight:600}

/* счётчики */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:12px 0 0}
.stat{background:var(--card2);border-radius:7px;padding:8px 12px;border-left:3px solid var(--neutral)}
.stat.up{border-left-color:var(--good)}
.stat.down{border-left-color:var(--bad)}
.stat.fuel{border-left-color:var(--good)}
.stat.acc{border-left-color:var(--warn)}
.stat .lbl{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.05em}
.stat .val{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums;margin-top:2px}
.stat .sub{font-size:11px;color:var(--mut);margin-top:1px}

/* токсичность — карточки монет */
.mono{padding:8px 12px;border-radius:7px;background:var(--card2);margin-bottom:6px;
      border-left:4px solid transparent;display:grid;grid-template-columns:110px 1fr 140px;gap:14px;align-items:center}
.mono.fuel{border-left-color:var(--good)}
.mono.acc{border-left-color:var(--warn)}
.mono .sym{font-weight:600;font-size:15px;letter-spacing:.02em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mono .sym .dir{margin-left:6px;font-weight:400;color:var(--mut)}
.mono.fuel .sym .dir{color:var(--good)}
.mono.acc .sym .dir{color:var(--warn)}
.mono .bars{display:flex;flex-direction:column;gap:4px;min-width:0}
.mono .brow{display:grid;grid-template-columns:44px 1fr 52px;gap:8px;align-items:center;font-size:12px}
.mono .blab{color:var(--mut)}
.mono .btrk{height:8px;background:var(--bar);border-radius:4px;overflow:hidden}
.mono .btrk>i{display:block;height:100%;background:var(--acc);transition:width .6s}
.mono.fuel .btrk>i{background:var(--good)}
.mono.acc .btrk>i{background:var(--warn)}
.mono .bval{text-align:right;font-variant-numeric:tabular-nums;color:var(--mut)}
.mono .bval.hot{color:var(--fg)}
.mono .side{text-align:right;font-size:12px;color:var(--mut);font-variant-numeric:tabular-nums}
.mono .side b{color:var(--fg);font-size:13px;display:block;font-weight:600}
.mono .side .verdict{margin-top:3px;font-size:10px;padding:1px 6px;border-radius:4px;font-weight:600;letter-spacing:.04em;display:inline-block}
.mono.fuel .side .verdict{background:rgba(22,163,74,.18);color:var(--good)}
.mono.acc  .side .verdict{background:rgba(217,119,6,.18);color:var(--warn)}
.empty{color:var(--mut);font-size:13px;padding:6px 0}

/* ignition */
.ign-idle{color:var(--mut);font-size:13px;padding:4px 0}
.ign-grid{display:grid;gap:8px}
.ign-item{padding:10px 14px;border-radius:8px;background:var(--card2);
          display:grid;grid-template-columns:120px 90px 1fr 110px;gap:12px;align-items:center}
.ign-item.up{border:1px solid rgba(22,163,74,.35);border-left:5px solid var(--good)}
.ign-item.down{border:1px solid rgba(220,38,38,.35);border-left:5px solid var(--bad)}
.ign-sym{font-weight:700;font-size:15px}
.ign-dir{font-weight:700;font-size:13px}
.ign-dir.up{color:var(--good)}.ign-dir.down{color:var(--bad)}
.ign-pulse{display:inline-block;animation:ign-anim 1.2s infinite;margin-right:4px}
@keyframes ign-anim{0%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(1.2)}100%{opacity:1;transform:scale(1)}}
.ign-ctx{font-size:11px;padding:2px 7px;border-radius:4px;background:var(--bar);display:inline-block;font-weight:600}
.ign-meta{font-size:12px;color:var(--mut)}
.ign-ret{font-size:15px;font-weight:700;text-align:right;font-variant-numeric:tabular-nums}
.ign-ret.pos{color:var(--good)}.ign-ret.neg{color:var(--bad)}

#errc{display:none}
</style></head><body>
<h1>TrendDetector: сканер зарождения тренда</h1>
<div class="sub"><span id="st">загрузка…</span> · <span id="up"></span></div>

<div class="card">
  <div class="regime flat" id="regime">—</div>
  <div class="ls-bar" id="ls_bar">
    <div class="l" style="width:0"></div>
    <div class="n" style="width:100%"></div>
    <div class="r" style="width:0"></div>
    <div class="ls-mark" id="ls_mark" style="left:50%"></div>
  </div>
  <div class="ls-lbl" id="ls_lbl"></div>
  <div class="stats" id="tstats"></div>
</div>

<div class="card" id="ign_card">
  <h2>🔥 Зарождение тренда (ignition)</h2>
  <div id="ign_content"></div>
</div>

<div class="card">
  <h2>Токсичность потока · топ-20</h2>
  <div id="tox"></div>
</div>

<div class="grid">
  <div class="card"><h2>Сейчас</h2><div class="kv" id="now"></div></div>
  <div class="card"><h2>Стримы Binance</h2><div class="kv" id="streams"></div></div>
</div>

<div class="card" id="errc">
  <h2>Ошибки обработчиков</h2>
  <div class="kv" id="err" style="color:var(--bad)"></div>
</div>

<script>
const $=id=>document.getElementById(id);
const n=x=>x==null?'—':Number(x).toLocaleString('ru-RU');
const usd=x=>{
  if(x==null) return '—';
  const a=Math.abs(x);
  if(a>=1e9) return '$'+(x/1e9).toFixed(2)+'B';
  if(a>=1e6) return '$'+(x/1e6).toFixed(2)+'M';
  if(a>=1e3) return '$'+(x/1e3).toFixed(0)+'k';
  return '$'+Math.round(x);
};
const pct=x=>x==null?'—':Math.round(x*100)+'%';
const eta=h=>h==null?'—':h<1?Math.round(h*60)+' мин':h<48?h.toFixed(1)+' ч':(h/24).toFixed(1)+' сут';
const dirGlyph=d=>d>0?'▲':d<0?'▼':'·';
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function kv(id,rows){
  const e=$(id); e.replaceChildren();
  for(const [a,b] of rows){
    const x=document.createElement('span'); x.textContent=a;
    const y=document.createElement('span'); y.textContent=b;
    e.append(x,y);
  }
}

function renderRegime(b){
  b = b || {};
  const total = b.total||0;
  const lp = b.long_pressure||0, sp = b.short_pressure||0;
  const diff = b.diff_pp||0, move = b.move_pp||40;
  const el = $('regime');
  let cls='flat', txt='FLAT';
  if (diff >= move) { cls='trend-up'; txt='TREND UP'; }
  else if (diff <= -move) { cls='trend-down'; txt='TREND DOWN'; }
  else if ((b.trend_fuel||0)===0 && (b.accumulation||0)>=5) { cls='accum'; txt='ACCUMULATION'; }
  el.className = 'regime '+cls;
  el.textContent = txt;

  // полоса long-short с центром
  const lEl = $('ls_bar').children[0];
  const nEl = $('ls_bar').children[1];
  const rEl = $('ls_bar').children[2];
  if (total>0) {
    const lpPct = lp/total*100, spPct = sp/total*100;
    const rest = Math.max(0, 100-lpPct-spPct);
    lEl.style.width = lpPct+'%';
    rEl.style.width = spPct+'%';
    nEl.style.width = rest+'%';
  } else {
    lEl.style.width='0'; rEl.style.width='0'; nEl.style.width='100%';
  }
  // метки порога
  const markPct = 50 + (move/2);
  $('ls_mark').style.left = Math.min(98, Math.max(2, markPct))+'%';

  $('ls_lbl').innerHTML =
    '<span><b class="good">'+lp+'</b> LONG pressure</span>'+
    '<span><b>'+diff.toFixed(1)+'</b> п.п. / порог ±'+move+'</span>'+
    '<span><b class="bad">'+sp+'</b> SHORT pressure</span>';

  const stats = $('tstats');
  stats.innerHTML =
    '<div class="stat"><div class="lbl">монет с метриками</div><div class="val">'+total+'</div></div>'+
    '<div class="stat up"><div class="lbl">LONG pressure</div><div class="val">'+lp+'</div><div class="sub">OBI↑</div></div>'+
    '<div class="stat down"><div class="lbl">SHORT pressure</div><div class="val">'+sp+'</div><div class="sub">OBI↓</div></div>'+
    '<div class="stat fuel"><div class="lbl">trend_fuel</div><div class="val">'+(b.trend_fuel||0)+'</div><div class="sub">поток + цена</div></div>'+
    '<div class="stat acc"><div class="lbl">accumulation</div><div class="val">'+(b.accumulation||0)+'</div><div class="sub">деньги без движения</div></div>';
}

function renderToxic(rows){
  const order={ 'trend_fuel':0, 'accumulation':1, '':2 };
  rows=(rows||[]).slice().sort((a,b)=>{
    const oa=order[a.verdict||'']??3, ob=order[b.verdict||'']??3;
    if(oa!==ob) return oa-ob;
    return (b.toxic_1h||0)-(a.toxic_1h||0);
  });
  const box=$('tox');
  if(!rows.length){ box.innerHTML='<div class="empty">пока пусто — сканер набирает данные</div>'; return; }
  box.innerHTML=rows.map(r=>{
    const v=r.verdict||'';
    const cls=v==='trend_fuel'?'mono fuel':(v==='accumulation'?'mono acc':'mono');
    const tag=v==='trend_fuel'?'TREND FUEL':(v==='accumulation'?'НАКОПЛЕНИЕ':'');
    const t1=r.toxic_1h||0, t4=r.toxic_4h||0;
    const w1=Math.min(100,t1*100), w4=Math.min(100,t4*100);
    const hot1=t1>=0.7?' hot':'', hot4=t4>=0.7?' hot':'';
    const obi = (r.obi_now!=null)?('OBI '+r.obi_now.toFixed(2)):'';
    const obiv = (r.obi_vel!=null)?((r.obi_vel>=0?'+':'')+r.obi_vel.toFixed(3)):'';
    return '<div class="'+cls+'">'+
      '<div class="sym" title="'+esc(r.symbol)+'">'+esc(r.symbol)+
        '<span class="dir">'+dirGlyph(r.dir)+'</span></div>'+
      '<div class="bars">'+
        '<div class="brow"><span class="blab">1ч</span>'+
          '<span class="btrk"><i style="width:'+w1+'%"></i></span>'+
          '<span class="bval'+hot1+'">'+pct(t1)+'</span></div>'+
        '<div class="brow"><span class="blab">4ч</span>'+
          '<span class="btrk"><i style="width:'+w4+'%"></i></span>'+
          '<span class="bval'+hot4+'">'+pct(t4)+'</span></div>'+
      '</div>'+
      '<div class="side">'+
        '<b>'+usd(r.vol_1h_usd)+'</b>'+
        (r.ret_30s_bps==null?'':'<span>'+r.ret_30s_bps.toFixed(2)+' bps</span>')+
        (obi?'<span>'+obi+' '+obiv+'</span>':'')+
        (tag?'<div class="verdict">'+tag+'</div>':'')+
      '</div>'+
    '</div>';
  }).join('');
}

function renderIgnitions(items){
  const box=$('ign_content');
  if(!items || !items.length){
    box.innerHTML='<div class="ign-idle">Активных ignition нет (рынок в балансе)</div>';
    return;
  }
  box.innerHTML='<div class="ign-grid">'+items.map(it=>{
    const isUp=it.dir>0;
    const cls=isUp?'ign-item up':'ign-item down';
    const dirTxt=isUp?'▲ UP':'▼ DOWN';
    const dirCls=isUp?'ign-dir up':'ign-dir down';
    const pulse=(it.age_sec<60)?'<span class="ign-pulse">⚡</span>':'';
    const ageTxt=it.age_sec<60?Math.round(it.age_sec)+' с':(it.age_sec/60).toFixed(1)+' мин';
    const retVal=it.ret_bps||0;
    const retCls=retVal>=0?'ign-ret pos':'ign-ret neg';
    const retTxt=(retVal>=0?'+':'')+retVal.toFixed(1)+' bps';
    const slopeTxt=(it.slope!=null)?((it.slope>=0?'+':'')+it.slope.toFixed(2)):'—';
    return '<div class="'+cls+'">'+
      '<div><div class="ign-sym">'+esc(it.symbol)+'</div><div class="ign-meta">с момента: '+ageTxt+'</div></div>'+
      '<div class="'+dirCls+'">'+pulse+dirTxt+'</div>'+
      '<div>'+
        '<div class="ign-meta">toxic: <b>'+pct(it.toxic_1h)+'</b> · slope 5м: <b>'+slopeTxt+'</b> · breadth: <b>'+(it.breadth||0)+'</b></div>'+
        '<div style="margin-top:4px"><span class="ign-ctx">'+esc(it.context||'—')+'</span></div>'+
      '</div>'+
      '<div class="'+retCls+'">'+retTxt+'</div>'+
    '</div>';
  }).join('')+'</div>';
}

function renderStreams(s){
  const el=$('streams');
  if(!s || !s.streams){ el.innerHTML='<span>нет данных</span>'; return; }
  const rows=s.streams.map(x=>[x.name, x.state+' ('+(x.silence_sec==null?'—':x.silence_sec+'с')+')']);
  kv('streams', rows);
}

function render(d){
  if(d.waiting){ $('st').textContent='ждём первых данных…'; return; }
  $('up').textContent='работает '+eta(d.uptime_sec/3600)+' · код: '+d.integrity;

  kv('now', [
    ['монет с OBI', n(d.sizes.books)],
    ['монет с токсичностью', n(d.sizes.toxic_pairs)],
    ['ignition в трекинге', n(d.sizes.ign_track)],
    ['toxic_signals в трекинге', n(d.sizes.sig_track)],
  ]);

  renderRegime(d.toxic_breadth);
  renderToxic(d.toxic_top);
  renderIgnitions(d.active_ignitions);
  renderStreams(d.streams);

  const er=Object.entries(d.errors||{});
  $('errc').style.display=er.length?'block':'none';
  kv('err',er);
}

async function tick(){
  try{
    const r=await fetch('/api',{cache:'no-store'});
    render(await r.json());
    $('st').textContent='обновлено '+new Date().toLocaleTimeString();
    $('st').className='';
  }catch(e){
    $('st').textContent='нет связи со сборщиком';
    $('st').className='bad';
  }
}
tick();
setInterval(tick,2000);
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
