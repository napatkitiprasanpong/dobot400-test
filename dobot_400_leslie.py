#!/usr/bin/env python3
"""
dobot 400-leslie  -  a single-file test console for the Dobot MG400 (4-axis) over LAN.

Why Python? Browsers can't open raw TCP sockets, and the MG400 talks plain TCP
(ports 29999 / 30003 / 30004). This script:
  1. serves the UI at http://localhost:8400
  2. bridges the UI <-> robot over TCP

Requirements: Python 3.8+  (standard library only, nothing to pip install)
Run:          python dobot_400_leslie.py
"""
import json
import math
import re
import socket
import struct
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

UI_PORT = 8400
DASH_PORT, MOVE_PORT, FEED_PORT = 29999, 30003, 30004
CMD_RE = re.compile(r"^[A-Za-z0-9_]+\([^;]*\)$")


class Robot:
    def __init__(self):
        self.ip = None
        self.dash = self.move = self.feed = None
        self.lock_d = threading.Lock()
        self.lock_m = threading.Lock()
        self.telemetry = {}
        self.last_feed = 0
        self.logs = []
        self.seq = 0
        self.gen = 0
        self.connected = False

    # ---------- logging ----------
    def log(self, kind, text):
        self.seq += 1
        self.logs.append({"id": self.seq, "t": time.strftime("%H:%M:%S"), "kind": kind, "text": text})
        self.logs = self.logs[-400:]

    # ---------- connection ----------
    def connect(self, ip):
        self.disconnect(quiet=True)
        try:
            self.dash = socket.create_connection((ip, DASH_PORT), timeout=4)
            self.move = socket.create_connection((ip, MOVE_PORT), timeout=4)
            self.feed = socket.create_connection((ip, FEED_PORT), timeout=4)
        except Exception as e:
            self.disconnect(quiet=True)
            self.log("err", f"connect failed: {e}")
            return False, str(e)
        self.ip = ip
        self.connected = True
        self.gen += 1
        threading.Thread(target=self._feed_loop, args=(self.gen,), daemon=True).start()
        self.log("sys", f"connected to {ip} (dashboard {DASH_PORT}, motion {MOVE_PORT}, feedback {FEED_PORT})")
        return True, "ok"

    def disconnect(self, quiet=False):
        self.gen += 1
        for name in ("dash", "move", "feed"):
            s = getattr(self, name)
            if s:
                try:
                    s.close()
                except Exception:
                    pass
            setattr(self, name, None)
        if self.connected and not quiet:
            self.log("sys", "disconnected")
        self.connected = False
        self.telemetry = {}

    # ---------- feedback (port 30004, 1440-byte packets every 8 ms) ----------
    def _feed_loop(self, gen):
        buf = b""
        self.feed.settimeout(2)
        while gen == self.gen:
            try:
                chunk = self.feed.recv(4096)
                if not chunk:
                    raise ConnectionError("feedback socket closed")
                buf += chunk
            except socket.timeout:
                continue
            except Exception as e:
                if gen == self.gen:
                    self.log("err", f"feedback lost: {e}")
                    self.connected = False
                return
            if len(buf) > 14400:
                buf = buf[-1440:]
            pkt = None
            while len(buf) >= 1440:
                pkt, buf = buf[:1440], buf[1440:]
            if pkt:
                self._parse(pkt)

    @staticmethod
    def _clean(vals):
        return [round(v, 3) if math.isfinite(v) else None for v in vals]

    def _parse(self, p):
        try:
            self.telemetry = {
                "mode": struct.unpack_from("<Q", p, 24)[0],
                "speed_scaling": self._clean([struct.unpack_from("<d", p, 64)[0]])[0],
                "di": struct.unpack_from("<Q", p, 8)[0],
                "do": struct.unpack_from("<Q", p, 16)[0],
                "joints": self._clean(struct.unpack_from("<6d", p, 432))[:4],
                "pose": self._clean(struct.unpack_from("<6d", p, 624))[:4],
            }
            self.last_feed = time.time()
        except Exception as e:
            self.log("err", f"parse error: {e}")

    # ---------- commands ----------
    def send(self, port, cmd):
        if not self.connected:
            return {"ok": False, "resp": "not connected"}
        cmd = cmd.strip()
        if not CMD_RE.match(cmd) or len(cmd) > 250:
            return {"ok": False, "resp": "rejected: command must look like Name(args)"}
        try:
            if port == "dash":
                with self.lock_d:
                    self.dash.settimeout(10)
                    self.dash.sendall(cmd.encode())
                    data = b""
                    while not data.endswith(b";"):
                        chunk = self.dash.recv(1024)
                        if not chunk:
                            raise ConnectionError("dashboard closed")
                        data += chunk
                    resp = data.decode(errors="replace").strip()
            else:
                with self.lock_m:
                    self.move.settimeout(0.4)
                    self.move.sendall(cmd.encode())
                    try:
                        resp = self.move.recv(1024).decode(errors="replace").strip() or "(sent)"
                    except socket.timeout:
                        resp = "(sent, no reply)"
            self.log("tx", f"[{port}] {cmd}")
            self.log("rx", resp)
            return {"ok": True, "resp": resp}
        except Exception as e:
            self.log("err", f"{cmd} -> {e}")
            return {"ok": False, "resp": str(e)}


robot = Robot()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            b = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif u.path == "/api/state":
            since = int(parse_qs(u.query).get("since", ["0"])[0])
            live = robot.connected and (time.time() - robot.last_feed) < 2
            self._json({
                "connected": robot.connected,
                "live": live,
                "ip": robot.ip,
                "telemetry": robot.telemetry,
                "logs": [l for l in robot.logs if l["id"] > since],
            })
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/connect":
            ok, msg = robot.connect(str(body.get("ip", "192.168.1.6")).strip())
            self._json({"ok": ok, "msg": msg})
        elif self.path == "/api/disconnect":
            robot.disconnect()
            self._json({"ok": True})
        elif self.path == "/api/send":
            self._json(robot.send(body.get("port", "dash"), body.get("cmd", "")))
        else:
            self._json({"error": "not found"}, 404)


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>dobot 400-leslie</title>
<style>
:root{--bg:#0f1115;--panel:#181b22;--panel2:#1f232c;--line:#2b303b;--tx:#e8eaf0;--mut:#8a92a3;--acc:#ff7a1a;--ok:#2ecc71;--bad:#ff4d4d;--warn:#f5c542}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.4 system-ui,Segoe UI,Roboto,sans-serif}
header{display:flex;align-items:center;gap:16px;padding:12px 20px;border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:5;flex-wrap:wrap}
h1{font-size:18px;margin:0;letter-spacing:.5px}
h1 b{color:var(--acc)}
.pill{padding:3px 10px;border-radius:99px;font-size:12px;background:var(--panel2);border:1px solid var(--line);color:var(--mut)}
.pill.ok{color:var(--ok);border-color:var(--ok)}.pill.bad{color:var(--bad);border-color:var(--bad)}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px;padding:16px 20px;max-width:1500px;margin:auto}
section{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
section h2{font-size:12px;text-transform:uppercase;letter-spacing:1.2px;color:var(--mut);margin:0 0 10px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:6px 0}
input,select{background:var(--panel2);border:1px solid var(--line);color:var(--tx);border-radius:8px;padding:7px 9px;font:inherit;min-width:0}
input[type=number]{width:80px}input[type=text]{flex:1}
button{background:var(--panel2);color:var(--tx);border:1px solid var(--line);border-radius:8px;padding:7px 12px;font:inherit;cursor:pointer}
button:hover{border-color:var(--acc)}button:active{transform:translateY(1px)}
button.pri{background:var(--acc);border-color:var(--acc);color:#111;font-weight:600}
button.bad{background:var(--bad);border-color:var(--bad);color:#fff;font-weight:700}
button.on{background:var(--ok);color:#111;border-color:var(--ok)}
button:disabled{opacity:.4;cursor:not-allowed}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.tele{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.tele div{background:var(--panel2);border-radius:8px;padding:8px;text-align:center}
.tele small{display:block;color:var(--mut);font-size:11px}
.tele span{font:600 17px ui-monospace,Consolas,monospace}
.mode{font-size:16px;font-weight:700;color:var(--acc)}
.jog{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
label.sm{color:var(--mut);font-size:12px}
#log{height:260px;overflow:auto;background:#0a0c10;border-radius:8px;padding:8px;font:12px ui-monospace,Consolas,monospace}
#log .tx{color:var(--acc)}#log .rx{color:#9bd}#log .err{color:var(--bad)}#log .sys{color:var(--ok)}
.pt{display:flex;gap:6px;align-items:center;background:var(--panel2);padding:6px 8px;border-radius:8px;margin:4px 0;font-size:12px}
.pt span{flex:1;font-family:ui-monospace,Consolas,monospace}
.note{color:var(--mut);font-size:12px}
.wide{grid-column:1/-1}
</style></head><body>
<header>
  <h1>dobot <b>400-leslie</b></h1>
  <span id="pConn" class="pill bad">disconnected</span>
  <span id="pMode" class="pill">mode: –</span>
  <div style="flex:1"></div>
  <input id="ip" type="text" value="192.168.1.6" style="flex:none;width:140px" title="MG400 LAN1 default is 192.168.1.6">
  <button class="pri" id="btnConn" onclick="toggleConn()">Connect</button>
  <button class="bad" onclick="estop()" title="Software stop. Use the physical E-stop for real emergencies!">STOP</button>
</header>
<main>

<section>
  <h2>Live feedback (port 30004)</h2>
  <div class="mode" id="modeName">–</div>
  <div class="tele" style="margin-top:8px">
    <div><small>X mm</small><span id="tX">–</span></div><div><small>Y mm</small><span id="tY">–</span></div>
    <div><small>Z mm</small><span id="tZ">–</span></div><div><small>R °</small><span id="tR">–</span></div>
    <div><small>J1 °</small><span id="tJ1">–</span></div><div><small>J2 °</small><span id="tJ2">–</span></div>
    <div><small>J3 °</small><span id="tJ3">–</span></div><div><small>J4 °</small><span id="tJ4">–</span></div>
  </div>
  <div class="note" style="margin-top:8px">Speed scaling: <span id="tSpd">–</span> &nbsp;|&nbsp; DO bits: <span id="tDO">–</span> &nbsp;|&nbsp; DI bits: <span id="tDI">–</span></div>
</section>

<section>
  <h2>Power &amp; state (dashboard 29999)</h2>
  <div class="row">
    <button class="pri" onclick="d('EnableRobot()')">Enable</button>
    <button onclick="d('DisableRobot()')">Disable</button>
    <button onclick="d('ClearError()')">Clear error</button>
    <button onclick="d('ResetRobot()')">Reset</button>
  </div>
  <div class="row">
    <button onclick="d('Continue()')">Continue queue</button>
    <button onclick="d('StartDrag()')">Start drag-teach</button>
    <button onclick="d('StopDrag()')">Stop drag-teach</button>
  </div>
  <div class="row">
    <button onclick="d('RobotMode()')">RobotMode</button>
    <button onclick="d('GetPose()')">GetPose</button>
    <button onclick="d('GetAngle()')">GetAngle</button>
    <button onclick="d('GetErrorID()')">GetErrorID</button>
  </div>
  <div class="row"><button onclick="diag()">Run read-only diagnostics</button></div>
  <div class="note">Enable first, then wait for mode “Enabled”. Drag-teach lets you move the arm by hand.</div>
</section>

<section>
  <h2>Jog (relative moves on 30003)</h2>
  <div class="row"><label class="sm">Step</label>
    <select id="step"><option>1</option><option>5</option><option selected>10</option><option>20</option><option>50</option></select>
    <span class="note">mm / degrees</span></div>
  <div class="jog">
    <button onclick="jog(0,0,1,0)">Z +</button><button onclick="jog(1,0,0,0)">X +</button><button onclick="jog(0,0,0,1)">R +</button>
    <button onclick="jog(0,-1,0,0)">Y −</button><button onclick="jog(0,0,0,0,true)">·</button><button onclick="jog(0,1,0,0)">Y +</button>
    <button onclick="jog(0,0,-1,0)">Z −</button><button onclick="jog(-1,0,0,0)">X −</button><button onclick="jog(0,0,0,-1)">R −</button>
  </div>
  <div class="row" style="margin-top:10px"><span class="note">Joints:</span></div>
  <div class="grid4">
    <button onclick="jj(1,-1)">J1 −</button><button onclick="jj(2,-1)">J2 −</button><button onclick="jj(3,-1)">J3 −</button><button onclick="jj(4,-1)">J4 −</button>
    <button onclick="jj(1,1)">J1 +</button><button onclick="jj(2,1)">J2 +</button><button onclick="jj(3,1)">J3 +</button><button onclick="jj(4,1)">J4 +</button>
  </div>
</section>

<section>
  <h2>Go to position</h2>
  <div class="row">
    <input id="gx" type="number" placeholder="X"><input id="gy" type="number" placeholder="Y"><input id="gz" type="number" placeholder="Z"><input id="gr" type="number" placeholder="R">
  </div>
  <div class="row">
    <button class="pri" onclick="goto('MovJ')">MovJ (joint-interp)</button>
    <button class="pri" onclick="goto('MovL')">MovL (straight line)</button>
    <button onclick="fillPose()">Fill from current</button>
  </div>
  <div class="row">
    <input id="j1" type="number" placeholder="J1"><input id="j2" type="number" placeholder="J2"><input id="j3" type="number" placeholder="J3"><input id="j4" type="number" placeholder="J4">
  </div>
  <div class="row">
    <button class="pri" onclick="gotoJ()">JointMovJ</button>
    <button onclick="fillJoints()">Fill from current</button>
    <button onclick="m('Sync()')">Sync</button>
  </div>
  <div class="note">MG400 reach is limited – stay near X≈250–400, Y≈±150, Z≈0–150 until you know your workspace. Keep clear of the arm.</div>
</section>

<section>
  <h2>Motion parameters</h2>
  <div id="params"></div>
</section>

<section>
  <h2>I/O &amp; end-effector</h2>
  <div class="row"><span class="note">Digital outputs (click to toggle):</span></div>
  <div class="grid4" id="doGrid"></div>
  <div class="row" style="margin-top:10px">
    <label class="sm">ToolDO</label><input id="tdi" type="number" value="1" style="width:60px">
    <button onclick="d('ToolDO('+v('tdi')+',1)')">On</button><button onclick="d('ToolDO('+v('tdi')+',0)')">Off</button>
  </div>
  <div class="row">
    <label class="sm">Read DI</label><input id="dii" type="number" value="1" style="width:60px">
    <button onclick="d('DI('+v('dii')+')')">DI</button>
    <button onclick="d('DO('+v('dii')+')')">DO</button>
  </div>
  <div class="note">Gripper / suction cup are driven through DO or ToolDO depending on how it's wired – check which channel yours uses.</div>
</section>

<section>
  <h2>Saved points</h2>
  <div class="row"><input id="ptName" type="text" placeholder="name e.g. home"><button onclick="savePt()">Save current pose</button></div>
  <div id="pts"></div>
  <div class="row"><button onclick="runPts()">Run all (MovJ in order)</button></div>
</section>

<section class="wide">
  <h2>Console</h2>
  <div id="log"></div>
  <div class="row">
    <select id="rport"><option value="dash">Dashboard 29999</option><option value="move">Motion 30003</option></select>
    <input id="raw" type="text" placeholder="Any SDK command, e.g. MovL(300,0,50,0)" onkeydown="if(event.key==='Enter')rawSend()">
    <button class="pri" onclick="rawSend()">Send</button>
    <button onclick="$('log').innerHTML=''">Clear</button>
  </div>
</section>
</main>

<script>
const $=id=>document.getElementById(id);
const v=id=>$(id).value;
const MODES={1:'Initializing',2:'Brake released',3:'Powered off',4:'Disabled',5:'Enabled (idle)',6:'Backdrive',7:'Running',8:'Recording',9:'ERROR',10:'Paused',11:'Jogging'};
let connected=false,lastId=0,pose=[],joints=[];

async function api(p,b){const r=await fetch(p,{method:b?'POST':'GET',headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):undefined});return r.json()}
const send=(port,cmd)=>api('/api/send',{port,cmd});
const d=c=>send('dash',c), m=c=>send('move',c);

async function toggleConn(){
  if(connected){await api('/api/disconnect',{});return}
  $('btnConn').disabled=true;$('btnConn').textContent='Connecting…';
  const r=await api('/api/connect',{ip:v('ip')});
  $('btnConn').disabled=false;
  if(!r.ok)alert('Could not connect: '+r.msg+'\n\nCheck: LAN cable in LAN1, PC IP set to 192.168.1.x (e.g. 192.168.1.100 / 255.255.255.0), and you can ping '+v('ip'));
}
async function estop(){await d('DisableRobot()')}

async function poll(){
  try{
    const s=await api('/api/state?since='+lastId);
    connected=s.connected;
    $('pConn').className='pill '+(s.live?'ok':'bad');
    $('pConn').textContent=s.live?'connected · '+s.ip:(s.connected?'no feedback':'disconnected');
    $('btnConn').textContent=connected?'Disconnect':'Connect';
    const t=s.telemetry||{};
    if(t.mode!==undefined){
      const mn=MODES[t.mode]||('mode '+t.mode);
      $('pMode').textContent='mode: '+mn;$('modeName').textContent=mn;
      $('modeName').style.color=t.mode===9?'var(--bad)':'var(--acc)';
      pose=t.pose||[];joints=t.joints||[];
      ['tX','tY','tZ','tR'].forEach((k,i)=>$(k).textContent=pose[i]??'–');
      ['tJ1','tJ2','tJ3','tJ4'].forEach((k,i)=>$(k).textContent=joints[i]??'–');
      $('tSpd').textContent=t.speed_scaling;
      $('tDO').textContent=t.do.toString(2).padStart(16,'0').slice(-16);
      $('tDI').textContent=t.di.toString(2).padStart(16,'0').slice(-16);
      for(let i=1;i<=8;i++){$('do'+i).classList.toggle('on',!!((t.do>>(i-1))&1))}
    }
    const L=$('log');let add=false;
    for(const e of s.logs){lastId=e.id;const dv=document.createElement('div');dv.className=e.kind;dv.textContent=e.t+'  '+e.text;L.appendChild(dv);add=true}
    if(add)L.scrollTop=L.scrollHeight;
  }catch(e){$('pConn').className='pill bad';$('pConn').textContent='bridge offline'}
  setTimeout(poll,250);
}

/* ---- jog ---- */
const stp=()=>parseFloat(v('step'));
function jog(x,y,z,r,noop){if(noop)return;const s=stp();m(`RelMovL(${x*s},${y*s},${z*s},${r*s})`)}
function jj(j,dir){const s=stp(),a=[0,0,0,0];a[j-1]=dir*s;m(`RelJointMovJ(${a.join(',')})`)}

/* ---- goto ---- */
function nums(ids){return ids.map(i=>parseFloat(v(i)))}
function goto(kind){const a=nums(['gx','gy','gz','gr']);if(a.some(isNaN))return alert('Fill X Y Z R');m(`${kind}(${a.join(',')})`)}
function gotoJ(){const a=nums(['j1','j2','j3','j4']);if(a.some(isNaN))return alert('Fill J1–J4');m(`JointMovJ(${a.join(',')})`)}
function fillPose(){['gx','gy','gz','gr'].forEach((k,i)=>$(k).value=pose[i]??'')}
function fillJoints(){['j1','j2','j3','j4'].forEach((k,i)=>$(k).value=joints[i]??'')}

/* ---- params ---- */
const PARAMS=[['SpeedFactor','%',1,100,50],['AccJ','%',1,100,50],['AccL','%',1,100,50],['SpeedJ','%',1,100,50],['SpeedL','%',1,100,50],['CP','%',0,100,0],['User','idx',0,9,0],['Tool','idx',0,9,0]];
$('params').innerHTML=PARAMS.map(([n,u,lo,hi,df])=>`<div class="row"><label class="sm" style="width:90px">${n}</label><input type="range" id="p_${n}" min="${lo}" max="${hi}" value="${df}" style="flex:1" oninput="$('pv_${n}').textContent=this.value"><span id="pv_${n}" style="width:30px;text-align:right">${df}</span><button onclick="d('${n}('+v('p_${n}')+')')">Set</button></div>`).join('');

/* ---- IO ---- */
$('doGrid').innerHTML=Array.from({length:8},(_,i)=>`<button id="do${i+1}" onclick="toggleDO(${i+1})">DO ${i+1}</button>`).join('');
function toggleDO(i){const on=$('do'+i).classList.contains('on');d(`DO(${i},${on?0:1})`)}

/* ---- saved points ---- */
let pts=[];try{pts=JSON.parse(localStorage.getItem('leslie_pts')||'[]')}catch(e){}
function renderPts(){
  try{localStorage.setItem('leslie_pts',JSON.stringify(pts))}catch(e){}
  $('pts').innerHTML=pts.map((p,i)=>`<div class="pt"><b>${p.n}</b><span>${p.p.join(', ')}</span><button onclick="m('MovJ(${p.p.join(',')})')">Go</button><button onclick="pts.splice(${i},1);renderPts()">✕</button></div>`).join('')||'<div class="note">none yet</div>';
}
function savePt(){if(pose.length<4)return alert('Connect first');pts.push({n:v('ptName')||('P'+(pts.length+1)),p:pose.slice(0,4)});$('ptName').value='';renderPts()}
async function runPts(){for(const p of pts){await m(`MovJ(${p.p.join(',')})`);await m('Sync()')}}
renderPts();

/* ---- console / diag ---- */
function rawSend(){const c=v('raw').trim();if(!c)return;send(v('rport'),c);$('raw').value=''}
async function diag(){for(const c of ['RobotMode()','GetPose()','GetAngle()','GetErrorID()','DI(1)','DO(1)'])await d(c)}

poll();
</script></body></html>
"""

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else UI_PORT
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}"
    print(f"dobot 400-leslie running at {url}  (Ctrl+C to quit)")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        robot.disconnect()
        print("bye")
