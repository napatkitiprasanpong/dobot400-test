#!/usr/bin/env python3
"""
dobot 400-leslie  -  a single-file test console for the Dobot MG400 (4-axis) over LAN.

CONTROLS:  turn on "Keyboard drive", then hold  W A S D  to move in X/Y and  Up / Down arrows  to move in Z.
           Space = STOP.  Every other feature is a labelled button on the page.

HOW THE FILE IS ORGANISED (top to bottom):
  1. class Robot    - talks to the arm over TCP (connect, send commands, read live feedback)
  2. class Handler  - tiny web server: gives the browser the page and passes its commands to Robot
  3. HTML = ...     - the whole web page (look, buttons, and the keyboard-driving script)

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
    def send(self, port, cmd, fast=False):
        # fast=True is used by keyboard driving: it sends a tiny move and does NOT wait
        # for a reply or write to the log (otherwise 8 moves/second would spam the console).
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
                    self.move.settimeout(0.01 if fast else 0.4)
                    self.move.sendall(cmd.encode())
                    try:
                        resp = self.move.recv(1024).decode(errors="replace").strip() or "(sent)"
                    except socket.timeout:
                        resp = "(sent, no reply)"
            if not fast:
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
            self._json(robot.send(body.get("port", "dash"), body.get("cmd", ""), bool(body.get("fast"))))
        else:
            self._json({"error": "not found"}, 404)


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>dobot 400-leslie</title>
<style>
/* ================= LOOK & FEEL (colours + layout) ================= */
:root{--bg:#0f1115;--panel:#181b22;--panel2:#1f232c;--line:#2b303b;--tx:#e8eaf0;--mut:#8a92a3;--acc:#ff7a1a;--ok:#2ecc71;--bad:#ff4d4d;--warn:#f5c542}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.4 system-ui,Segoe UI,Roboto,sans-serif}
header{display:flex;align-items:center;gap:16px;padding:12px 20px;border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:5;flex-wrap:wrap}
h1{font-size:18px;margin:0;letter-spacing:.5px}
h1 b{color:var(--acc)}
.pill{padding:3px 10px;border-radius:99px;font-size:12px;background:var(--panel2);border:1px solid var(--line);color:var(--mut)}
.pill.ok{color:var(--ok);border-color:var(--ok)}.pill.bad{color:var(--bad);border-color:var(--bad)}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:14px;padding:16px 20px;max-width:1400px;margin:auto}
section{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
section h2{font-size:12px;text-transform:uppercase;letter-spacing:1.2px;color:var(--mut);margin:0 0 10px}
.wide{grid-column:1/-1}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:6px 0}
.note{color:var(--mut);font-size:12px}
label.sm{color:var(--mut);font-size:12px}
input,select{background:var(--panel2);border:1px solid var(--line);color:var(--tx);border-radius:8px;padding:7px 9px;font:inherit;min-width:0}
input[type=number]{width:80px}input[type=text]{flex:1}
button{background:var(--panel2);color:var(--tx);border:1px solid var(--line);border-radius:8px;padding:7px 12px;font:inherit;cursor:pointer}
button:hover{border-color:var(--acc)}button:active{transform:translateY(1px)}
button.pri{background:var(--acc);border-color:var(--acc);color:#111;font-weight:600}
button.bad{background:var(--bad);border-color:var(--bad);color:#fff;font-weight:700}
button.on{background:var(--ok);color:#111;border-color:var(--ok)}
button:disabled{opacity:.4;cursor:not-allowed}

/* "action cards": a big button with a plain-English explanation underneath */
.acts{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.act{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:10px;display:flex;flex-direction:column;gap:6px}
.act button{width:100%;padding:10px}
.act p{margin:0;color:var(--mut);font-size:12px}

/* the on-screen keyboard (WASD + arrows) */
.pads{display:flex;gap:32px;flex-wrap:wrap;align-items:flex-end;margin:10px 0}
.pad{display:grid;gap:6px}
.pad.xy{grid-template-columns:repeat(3,68px);grid-template-areas:". w ." "a s d"}
.pad.z{grid-template-columns:68px}
.key{width:68px;height:68px;font-size:20px;font-weight:700;display:flex;flex-direction:column;align-items:center;justify-content:center;user-select:none;touch-action:none;padding:0}
.key small{font-size:10px;font-weight:400;color:var(--mut)}
.key.down{background:var(--acc);color:#111;border-color:var(--acc)}.key.down small{color:#222}
.kw{grid-area:w}.ka{grid-area:a}.ks{grid-area:s}.kd{grid-area:d}
.padlabel{color:var(--mut);font-size:12px;margin-bottom:4px}

/* live numbers */
.mode{font-size:16px;font-weight:700;color:var(--acc)}
.tele{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.tele div{background:var(--panel2);border-radius:8px;padding:8px;text-align:center}
.tele small{display:block;color:var(--mut);font-size:11px}
.tele span{font:600 17px ui-monospace,Consolas,monospace}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}

/* saved points + console */
.pt{display:flex;gap:6px;align-items:center;background:var(--panel2);padding:6px 8px;border-radius:8px;margin:4px 0;font-size:12px}
.pt span{flex:1;font-family:ui-monospace,Consolas,monospace}
#log{height:240px;overflow:auto;background:#0a0c10;border-radius:8px;padding:8px;font:12px ui-monospace,Consolas,monospace}
#log .tx{color:var(--acc)}#log .rx{color:#9bd}#log .err{color:var(--bad)}#log .sys{color:var(--ok)}
details{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:10px 14px}
summary{cursor:pointer;color:var(--mut);font-weight:600}
details .inner{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px;margin-top:12px}
details .inner>div{background:var(--panel2);border-radius:10px;padding:12px}
details h3{margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:1px;color:var(--mut)}
</style></head><body>

<!-- ================= TOP BAR: connect + emergency stop ================= -->
<header>
  <h1>dobot <b>400-leslie</b></h1>
  <span id="pConn" class="pill bad">disconnected</span>
  <span id="pMode" class="pill">mode: –</span>
  <div style="flex:1"></div>
  <input id="ip" type="text" value="192.168.1.6" style="flex:none;width:140px" title="MG400 LAN1 default is 192.168.1.6">
  <button class="pri" id="btnConn" onclick="toggleConn()" title="Connect to / disconnect from the robot">Connect</button>
  <button class="bad" onclick="estop()" title="Software stop (turns the arm off). Use the physical E-stop for real emergencies!">STOP (Space)</button>
</header>

<main>

<!-- ================= 1. DRIVE (the video-game part) ================= -->
<section class="wide">
  <h2>1 · Drive the arm like a video game</h2>
  <div class="row">
    <button class="pri" id="btnDrive" onclick="toggleDrive()">🎮 Keyboard drive: OFF</button>
    <span class="note">Turn this ON, then <b>hold</b> a key to move and <b>let go</b> to stop. <b>Space</b> = STOP.</span>
  </div>
  <div id="driveHint" class="note" style="color:var(--warn)"></div>

  <div class="pads">
    <div>
      <div class="padlabel">Move across the table (X / Y) — hold W A S D</div>
      <div class="pad xy">
        <button class="key kw" data-key="w">W<small>forward</small></button>
        <button class="key ka" data-key="a">A<small>left</small></button>
        <button class="key ks" data-key="s">S<small>back</small></button>
        <button class="key kd" data-key="d">D<small>right</small></button>
      </div>
    </div>
    <div>
      <div class="padlabel">Up / down (Z) — hold ↑ ↓</div>
      <div class="pad z">
        <button class="key" data-key="arrowup">↑<small>up</small></button>
        <button class="key" data-key="arrowdown">↓<small>down</small></button>
      </div>
    </div>
    <div style="flex:1;min-width:230px">
      <div class="tele">
        <div><small>X mm</small><span id="tX">–</span></div><div><small>Y mm</small><span id="tY">–</span></div>
        <div><small>Z mm</small><span id="tZ">–</span></div><div><small>R °</small><span id="tR">–</span></div>
      </div>
      <div class="mode" id="modeName" style="margin-top:8px">–</div>
    </div>
  </div>

  <div class="row">
    <label class="sm">Drive speed</label>
    <button id="sp2" onclick="setDriveSpeed(2)">Slow</button>
    <button id="sp5" onclick="setDriveSpeed(5)">Medium</button>
    <button id="sp10" onclick="setDriveSpeed(10)">Fast</button>
    <span class="note" id="spNote"></span>
  </div>
  <div class="row">
    <label class="sm">Arm top speed</label>
    <input type="range" id="p_SpeedFactor" min="1" max="100" value="50" style="flex:1;max-width:260px" oninput="$('pv_SpeedFactor').textContent=this.value" onchange="d('SpeedFactor('+this.value+')')">
    <span id="pv_SpeedFactor">50</span>%
    <span class="note">(limits how fast the arm is allowed to go)</span>
  </div>
  <div class="row">
    <label class="sm"><input type="checkbox" id="flipFB"> flip W/S</label>
    <label class="sm"><input type="checkbox" id="flipLR"> flip A/D</label>
    <span class="note">Tick these if the arm moves the opposite way to what you expect. “Forward” = away from the robot base. Test with the Slow speed first.</span>
  </div>
</section>

<!-- ================= 2. GET READY ================= -->
<section>
  <h2>2 · Get the arm ready</h2>
  <div class="acts">
    <div class="act"><button class="pri" onclick="d('EnableRobot()')">Turn ON</button><p>Powers up the arm so it can move. Do this first, then wait for “Enabled”.</p></div>
    <div class="act"><button onclick="d('DisableRobot()')">Turn OFF</button><p>Powers the arm down. It will go limp, so hold it or lower it first.</p></div>
    <div class="act"><button onclick="d('ClearError()')">Fix error</button><p>Clears an alarm (for example after hitting a limit) so you can carry on.</p></div>
    <div class="act"><button onclick="d('Continue()')">Resume</button><p>If moves stop working, this un-pauses the robot's movement queue.</p></div>
    <div class="act"><button id="btnDrag" onclick="toggleDrag()">Hand-guide: OFF</button><p>Lets you push the arm around by hand. Click again to lock it.</p></div>
    <div class="act"><button onclick="diag()">Check connection</button><p>Safe test. Asks the robot for its status. The arm will NOT move.</p></div>
    <div class="act"><button onclick="d('ResetRobot()')">Reset</button><p>Restarts the robot's state. Use if Fix error doesn't help.</p></div>
  </div>
</section>

<!-- ================= 3. POSITIONS ================= -->
<section>
  <h2>3 · Remember &amp; go to places</h2>
  <div class="row"><input id="ptName" type="text" placeholder="name e.g. home"><button onclick="savePt()">Save this spot</button></div>
  <div class="note">“Save this spot” remembers where the arm is right now. Press Go later to return there.</div>
  <div id="pts"></div>
  <div class="row"><button onclick="runPts()">Play all spots in order</button></div>
  <div class="note" style="margin-top:10px">Or type a place (mm):</div>
  <div class="row">
    <input id="gx" type="number" placeholder="X"><input id="gy" type="number" placeholder="Y"><input id="gz" type="number" placeholder="Z"><input id="gr" type="number" placeholder="R">
  </div>
  <div class="row">
    <button class="pri" onclick="goto('MovJ')" title="Fastest, but the path is curved">Go (curved path)</button>
    <button class="pri" onclick="goto('MovL')" title="Moves in a straight line">Go (straight line)</button>
    <button onclick="fillPose()">Use current spot</button>
  </div>
  <div class="note">MG400 reach is limited – stay near X≈250–400, Y≈±150, Z≈0–150 until you know your workspace.</div>
</section>

<!-- ================= 4. GRIPPER / OUTPUTS ================= -->
<section>
  <h2>4 · Gripper / suction (outputs)</h2>
  <div class="note" style="margin-bottom:8px">Each button switches one electrical output on/off (green = on). A gripper or suction cup is usually wired to one of them — try them one at a time to find yours.</div>
  <div class="grid4" id="doGrid"></div>
</section>

<!-- ================= ADVANCED (everything else, tucked away) ================= -->
<details class="wide">
  <summary>Advanced tools (joints, parameters, raw commands)</summary>
  <div class="inner">

    <div>
      <h3>Joint &amp; rotate jog (one click = one step)</h3>
      <div class="row"><label class="sm">Step</label>
        <select id="step"><option>1</option><option>5</option><option selected>10</option><option>20</option><option>50</option></select>
        <span class="note">mm / degrees</span></div>
      <div class="row"><button onclick="rot(-1)">Rotate R −</button><button onclick="rot(1)">Rotate R +</button><span class="note">spins the end of the arm</span></div>
      <div class="grid4">
        <button onclick="jj(1,-1)">J1 −</button><button onclick="jj(2,-1)">J2 −</button><button onclick="jj(3,-1)">J3 −</button><button onclick="jj(4,-1)">J4 −</button>
        <button onclick="jj(1,1)">J1 +</button><button onclick="jj(2,1)">J2 +</button><button onclick="jj(3,1)">J3 +</button><button onclick="jj(4,1)">J4 +</button>
      </div>
    </div>

    <div>
      <h3>Go to joint angles</h3>
      <div class="row"><input id="j1" type="number" placeholder="J1"><input id="j2" type="number" placeholder="J2"><input id="j3" type="number" placeholder="J3"><input id="j4" type="number" placeholder="J4"></div>
      <div class="row"><button class="pri" onclick="gotoJ()">JointMovJ</button><button onclick="fillJoints()">Use current</button><button onclick="m('Sync()')" title="Wait until the arm has finished its queued moves">Sync</button></div>
      <div class="tele" style="margin-top:8px">
        <div><small>J1 °</small><span id="tJ1">–</span></div><div><small>J2 °</small><span id="tJ2">–</span></div>
        <div><small>J3 °</small><span id="tJ3">–</span></div><div><small>J4 °</small><span id="tJ4">–</span></div>
      </div>
      <div class="note" style="margin-top:8px">Speed scaling: <span id="tSpd">–</span> | DO bits: <span id="tDO">–</span> | DI bits: <span id="tDI">–</span></div>
    </div>

    <div>
      <h3>Motion parameters</h3>
      <div id="params"></div>
    </div>

    <div>
      <h3>Questions &amp; tool outputs</h3>
      <div class="row">
        <button onclick="d('RobotMode()')">RobotMode</button><button onclick="d('GetPose()')">GetPose</button>
        <button onclick="d('GetAngle()')">GetAngle</button><button onclick="d('GetErrorID()')">GetErrorID</button>
      </div>
      <div class="row">
        <label class="sm">ToolDO</label><input id="tdi" type="number" value="1" style="width:60px">
        <button onclick="d('ToolDO('+v('tdi')+',1)')">On</button><button onclick="d('ToolDO('+v('tdi')+',0)')">Off</button>
      </div>
      <div class="row">
        <label class="sm">Read</label><input id="dii" type="number" value="1" style="width:60px">
        <button onclick="d('DI('+v('dii')+')')">DI</button><button onclick="d('DO('+v('dii')+')')">DO</button>
      </div>
    </div>

    <div style="grid-column:1/-1">
      <h3>Console (send any SDK command)</h3>
      <div id="log"></div>
      <div class="row">
        <select id="rport"><option value="dash">Dashboard 29999</option><option value="move">Motion 30003</option></select>
        <input id="raw" type="text" placeholder="Any SDK command, e.g. MovL(300,0,50,0)" onkeydown="if(event.key==='Enter')rawSend()">
        <button class="pri" onclick="rawSend()">Send</button>
        <button onclick="$('log').innerHTML=''">Clear</button>
      </div>
    </div>

  </div>
</details>
</main>

<script>
/* =====================================================================
   HOW THIS SCRIPT IS ORGANISED
     A. helpers            – tiny shortcuts + talking to the Python bridge
     B. connect / status   – the poll() loop that keeps the screen up to date
     C. DRIVING            – the WASD / arrow-key "video game" controls
     D. everything else    – saved spots, go-to, outputs, console
   ===================================================================== */

/* ---------- A. helpers ---------- */
const $=id=>document.getElementById(id);          // find an element by id
const v=id=>$(id).value;                          // read an input's value
const MODES={1:'Initializing',2:'Brake released',3:'Powered off',4:'Disabled',5:'Enabled (idle)',6:'Backdrive',7:'Running',8:'Recording',9:'ERROR',10:'Paused',11:'Jogging'};
let connected=false,lastId=0,pose=[],joints=[];

async function api(p,b){const r=await fetch(p,{method:b?'POST':'GET',headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):undefined});return r.json()}
const send=(port,cmd,fast)=>api('/api/send',{port,cmd,fast:!!fast});   // send a command to the robot
const d=c=>send('dash',c);                        // d('EnableRobot()')  -> "dashboard" port (power, state, questions)
const m=(c,fast)=>send('move',c,fast);            // m('MovJ(...)')      -> "motion" port (moves)

/* ---------- B. connect / status ---------- */
async function toggleConn(){
  if(connected){releaseAll();await api('/api/disconnect',{});return}
  $('btnConn').disabled=true;$('btnConn').textContent='Connecting…';
  const r=await api('/api/connect',{ip:v('ip')});
  $('btnConn').disabled=false;
  if(!r.ok)alert('Could not connect: '+r.msg+'\n\nCheck: LAN cable in LAN1, PC IP set to 192.168.1.x (e.g. 192.168.1.100 / 255.255.255.0), and you can ping '+v('ip'));
}
async function estop(){releaseAll();await d('DisableRobot()')}   // software stop

const READY_MODES=[5,7,11];   // modes in which the arm is powered and can move

async function poll(){        // runs 4 times a second
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
    // friendly hint under the drive switch
    let hint='';
    if(!connected) hint='Not connected – press Connect (top right).';
    else if(t.mode!==undefined && !READY_MODES.includes(t.mode)) hint='The arm is not powered yet – press “Turn ON” in step 2.';
    $('driveHint').textContent=hint;
    if(!connected) releaseAll();
    const L=$('log');let add=false;
    for(const e of s.logs){lastId=e.id;const dv=document.createElement('div');dv.className=e.kind;dv.textContent=e.t+'  '+e.text;L.appendChild(dv);add=true}
    if(add)L.scrollTop=L.scrollHeight;
  }catch(e){$('pConn').className='pill bad';$('pConn').textContent='bridge offline'}
  setTimeout(poll,250);
}

/* ---------- C. DRIVING (WASD = X/Y, arrow keys = Z) ----------
   How it works, in plain words:
   - We remember which keys are being held in the set `held`.
   - While at least one key is held, every 120 ms we send ONE tiny relative move
     (a few millimetres) in the direction of the held keys.
   - Let go of the keys -> we stop sending -> the arm stops (after finishing the
     tiny move it already has). There's no "stop" command to forget.
   - If the browser tab loses focus we also let go of everything, as a safety net. */
const TICK_MS=120;                 // how often we send a tiny move while a key is held
let driveOn=false;                 // is keyboard drive switched on?
let driveStep=2;                   // mm per tick  (2 = slow, 5 = medium, 10 = fast)
const held=new Set();              // keys currently held, e.g. 'w' or 'arrowup'
let driveTimer=null;

// which way each key pushes the arm: [x, y, z]
const KEYMAP={
  w:[ 1, 0, 0],   // forward (away from base)
  s:[-1, 0, 0],   // back
  a:[ 0, 1, 0],   // left   (+Y when you stand behind the robot looking out)
  d:[ 0,-1, 0],   // right
  arrowup:[0,0, 1],     // up
  arrowdown:[0,0,-1]    // down
};

function setDriveSpeed(mm){
  driveStep=mm;
  [2,5,10].forEach(n=>$('sp'+n).classList.toggle('on',n===mm));
  $('spNote').textContent='≈ '+Math.round(mm*1000/TICK_MS)+' mm/s';
}

function toggleDrive(){
  driveOn=!driveOn;
  releaseAll();
  $('btnDrive').textContent='🎮 Keyboard drive: '+(driveOn?'ON':'OFF');
  $('btnDrive').classList.toggle('on',driveOn);
}

function driveTick(){              // called every TICK_MS while keys are held
  if(!connected){releaseAll();return}
  let x=0,y=0,z=0;
  for(const k of held){const dir=KEYMAP[k];if(dir){x+=dir[0];y+=dir[1];z+=dir[2]}}
  if($('flipFB').checked)x=-x;
  if($('flipLR').checked)y=-y;
  const len=Math.hypot(x,y);       // diagonal (W+D) shouldn't be faster than straight
  if(len>1){x/=len;y/=len}
  if(!x&&!y&&!z)return;            // e.g. W and S pressed together cancel out
  const f=n=>+(n*driveStep).toFixed(2);
  m(`RelMovL(${f(x)},${f(y)},${f(z)},0)`,true);   // true = fast, no waiting, no log spam
}

function press(k){
  if(held.has(k))return;
  held.add(k);markKeys();
  if(!driveTimer){driveTick();driveTimer=setInterval(driveTick,TICK_MS)}
}
function release(k){
  if(!held.delete(k))return;
  markKeys();
  if(held.size===0&&driveTimer){clearInterval(driveTimer);driveTimer=null}
}
function releaseAll(){
  held.clear();markKeys();
  if(driveTimer){clearInterval(driveTimer);driveTimer=null}
}
function markKeys(){               // light up the on-screen keys that are held
  document.querySelectorAll('.key').forEach(b=>b.classList.toggle('down',held.has(b.dataset.key)));
}

// keyboard
addEventListener('keydown',e=>{
  if(e.target.matches('input,select,textarea'))return;   // don't steal keys while typing
  if(e.ctrlKey||e.metaKey||e.altKey)return;
  const k=e.key.toLowerCase();
  if(driveOn&&k===' '){e.preventDefault();if(!e.repeat)estop();return}   // Space = STOP
  if(!driveOn||!(k in KEYMAP))return;
  e.preventDefault();            // stops arrow keys scrolling the page
  press(k);
});
addEventListener('keyup',e=>release(e.key.toLowerCase()));
addEventListener('blur',releaseAll);                              // clicked away -> stop
document.addEventListener('visibilitychange',()=>{if(document.hidden)releaseAll()});

// mouse / touch: hold an on-screen key to move
document.querySelectorAll('.key').forEach(b=>{
  const k=b.dataset.key;
  b.addEventListener('pointerdown',e=>{e.preventDefault();press(k)});
  ['pointerup','pointerleave','pointercancel'].forEach(ev=>b.addEventListener(ev,()=>release(k)));
});
setDriveSpeed(2);                  // start on SLOW to be safe

/* ---------- D. everything else ---------- */

/* hand-guide (drag teach) on/off */
let dragOn=false;
async function toggleDrag(){
  dragOn=!dragOn;
  await d(dragOn?'StartDrag()':'StopDrag()');
  $('btnDrag').textContent='Hand-guide: '+(dragOn?'ON':'OFF');
  $('btnDrag').classList.toggle('on',dragOn);
}

/* one-click steps (advanced) */
const stp=()=>parseFloat(v('step'));
function rot(dir){m(`RelMovL(0,0,0,${dir*stp()})`)}
function jj(j,dir){const s=stp(),a=[0,0,0,0];a[j-1]=dir*s;m(`RelJointMovJ(${a.join(',')})`)}

/* go to a typed position */
function nums(ids){return ids.map(i=>parseFloat(v(i)))}
function goto(kind){const a=nums(['gx','gy','gz','gr']);if(a.some(isNaN))return alert('Fill X Y Z R');m(`${kind}(${a.join(',')})`)}
function gotoJ(){const a=nums(['j1','j2','j3','j4']);if(a.some(isNaN))return alert('Fill J1–J4');m(`JointMovJ(${a.join(',')})`)}
function fillPose(){['gx','gy','gz','gr'].forEach((k,i)=>$(k).value=pose[i]??'')}
function fillJoints(){['j1','j2','j3','j4'].forEach((k,i)=>$(k).value=joints[i]??'')}

/* motion parameter sliders (advanced). Arm top speed lives in section 1. */
const PARAMS=[['AccJ','%',1,100,50],['AccL','%',1,100,50],['SpeedJ','%',1,100,50],['SpeedL','%',1,100,50],['CP','%',0,100,0],['User','idx',0,9,0],['Tool','idx',0,9,0]];
$('params').innerHTML=PARAMS.map(([n,u,lo,hi,df])=>`<div class="row"><label class="sm" style="width:90px">${n}</label><input type="range" id="p_${n}" min="${lo}" max="${hi}" value="${df}" style="flex:1" oninput="$('pv_${n}').textContent=this.value"><span id="pv_${n}" style="width:30px;text-align:right">${df}</span><button onclick="d('${n}('+v('p_${n}')+')')">Set</button></div>`).join('');

/* outputs (gripper / suction) */
$('doGrid').innerHTML=Array.from({length:8},(_,i)=>`<button id="do${i+1}" onclick="toggleDO(${i+1})">Output ${i+1}</button>`).join('');
function toggleDO(i){const on=$('do'+i).classList.contains('on');d(`DO(${i},${on?0:1})`)}

/* saved spots (kept in this browser's localStorage) */
let pts=[];try{pts=JSON.parse(localStorage.getItem('leslie_pts')||'[]')}catch(e){}
function renderPts(){
  try{localStorage.setItem('leslie_pts',JSON.stringify(pts))}catch(e){}
  $('pts').innerHTML=pts.map((p,i)=>`<div class="pt"><b>${p.n}</b><span>${p.p.join(', ')}</span><button onclick="m('MovJ(${p.p.join(',')})')">Go</button><button onclick="pts.splice(${i},1);renderPts()">✕</button></div>`).join('')||'<div class="note">no spots saved yet</div>';
}
function savePt(){if(pose.length<4)return alert('Connect first');pts.push({n:v('ptName')||('P'+(pts.length+1)),p:pose.slice(0,4)});$('ptName').value='';renderPts()}
async function runPts(){for(const p of pts){await m(`MovJ(${p.p.join(',')})`);await m('Sync()')}}
renderPts();

/* console + read-only diagnostics */
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
