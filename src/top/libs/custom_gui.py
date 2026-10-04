#!/usr/bin/env python3
"""
wifi_gui.py  -  interactive 3D viewer for Mininet-WiFi  (version 2)

No matplotlib. Pure Tkinter (comes with Python: sudo apt install python3-tk).

What you get
  - 3D view you can rotate, pan and zoom, plus a flat top view
  - Drag any node with the mouse to move it, and Mininet-WiFi moves it too
    (hold Shift while dragging to change its height)
  - Range circle for every wireless node: access points AND stations
  - Wireless connections drawn as animated radio waves; APs send out
    expanding "signal" ripples
  - Controller shown with a dashed control link to the APs it manages,
    wired links shown as solid cables
  - Hover a node to see its details; the bar at the bottom lists who is
    connected to whom and warns about nodes without a position

Mouse & keys
  drag empty space ......... rotate          right-drag ....... pan
  mouse wheel .............. zoom            double-click ..... fit to screen
  drag a node .............. move it         Shift + drag ..... change height
  keys: T = top view, 3 = 3D view, F = fit, A = animation on/off

How it works with Mininet-WiFi
  Your script                                Window process
  -----------                                --------------
  net.build()                                (Tkinter only)
  gui = WifiGUI(net); gui.start()  ------>  opens the window
  background thread, every 0.3 s:
      reads positions, ranges,
      associations, links      ---- data --->  redraws the scene
  applies moves  <--- "move sta1 to x,y,z" ---  you drag a node
      (node.setPosition(...))
  CLI(net)   <- you keep typing commands as usual
  gui.stop(); net.stop()

  The window lives in its own process, so it never freezes while the CLI
  waits for input, and a crash in the GUI can't take your network down.

Usage
    from wifi_gui import WifiGUI
    ...
    net.build()
    ap1.start([])                 # or ap1.start([c0]) with a controller
    gui = WifiGUI(net)
    gui.start()
    CLI(net)
    gui.stop()
    net.stop()

  Run with:  sudo -E python3 your_script.py     (-E lets root open a window)
  Don't also call net.plotGraph(): use one viewer or the other.
  Controllers have no position in Mininet. The viewer places them
  automatically, or you can give one: net.addController('c0', position='150,300,0')
"""

import math
import multiprocessing as mp
import queue
import signal
import threading
import time


# ==========================================================================
# Part 1 - reading the network (runs inside your Mininet script)
# ==========================================================================

WIRELESS_LINK_WORDS = ('wifi', 'wireless', 'wmediumd', 'assoc', 'adhoc',
                       'mesh', 'its', 'direct', 'mode')

def _rssi(node):
    """
    Retourne le RSSI de la première interface Wi-Fi de la station.
    """
    try:
        if node.wintfs:
            return float(node.wintfs[0].rssi)
    except (AttributeError, IndexError, TypeError, ValueError):
        pass

    return None

def _xyz(node, overrides):
    """(x, y, z) of a node, or None if it has no position."""
    if node.name in overrides:
        return overrides[node.name]
    pos = None
    try:
        pos = node.getxyz()
    except Exception:
        pos = getattr(node, 'position', None) or \
              getattr(node, 'params', {}).get('position')
    if pos is None:
        return None
    if isinstance(pos, str):
        pos = pos.split(',')
    try:
        vals = [float(v) for v in pos]
    except (TypeError, ValueError):
        return None
    while len(vals) < 3:
        vals.append(0.0)
    return tuple(vals[:3])


def _range(node):
    """Biggest wireless range of a node (0 for wired-only nodes)."""
    ranges = []
    for intf in getattr(node, 'wintfs', {}).values():
        try:
            ranges.append(float(intf.range))
        except (AttributeError, TypeError, ValueError):
            pass
    return max(ranges) if ranges else 0.0


def _associated_ap(node):
    """Name of the AP a station is connected to, or None."""
    for intf in getattr(node, 'wintfs', {}).values():
        ap_intf = getattr(intf, 'associatedTo', None)
        # it can also be a word like 'adhoc' or 'mesh': ignore those
        if ap_intf is not None and not isinstance(ap_intf, str):
            ap_node = getattr(ap_intf, 'node', None)
            if ap_node is not None:
                return ap_node.name
    return None


def snapshot(net, overrides=None):
    """Everything the window needs, as plain data (dicts/lists/tuples)."""
    overrides = overrides or {}
    groups = [('ap', 'aps'), ('sta', 'stations'), ('sta', 'cars'),
              ('switch', 'switches'), ('host', 'hosts'),
              ('ctrl', 'controllers')]
    nodes, by_name = [], {}
    for kind, attr in groups:
        for node in getattr(net, attr, None) or []:
            if node.name in by_name:
                continue
            item = {'name': node.name, 'kind': kind,
                    'xyz': _xyz(node, overrides),
                    'range': _range(node),
                    'ap': _associated_ap(node) if kind == 'sta' else None,
                    'rssi': _rssi(node) if kind == 'sta' else None}
            by_name[node.name] = node
            nodes.append(item)

    links = []
    # wired links (cables between APs, switches, hosts)
    for link in getattr(net, 'links', None) or []:
        try:
            n1, n2 = link.intf1.node, link.intf2.node
            names = (str(link.intf1.name) + str(link.intf2.name)).lower()
        except AttributeError:
            continue
        cls = type(link).__name__.lower()
        if any(w in cls for w in WIRELESS_LINK_WORDS) or 'wlan' in names:
            continue
        if n1.name in by_name and n2.name in by_name:
            links.append((n1.name, n2.name, 'wired'))

    # control links: controller -> every AP/switch that isn't standalone
    ctrls = [n['name'] for n in nodes if n['kind'] == 'ctrl']
    for n in nodes:
        if n['kind'] in ('ap', 'switch') and ctrls:
            node = by_name[n['name']]
            fail = str(getattr(node, 'failMode', '') or
                       node.params.get('failMode', '')).lower()
            if fail != 'standalone':
                for c in ctrls:
                    links.append((c, n['name'], 'control'))
    return {'nodes': nodes, 'links': links}


class WifiGUI(object):
    """Interactive window showing the Mininet-WiFi network."""

    def __init__(self, net, refresh=0.3, size=(900, 700),
                 title='Mininet-WiFi 3D View', allow_move=True):
        self.net = net
        self.refresh = refresh
        self.allow_move = allow_move
        self.cfg = {'size': size, 'title': title, 'allow_move': allow_move}
        self.overrides = {}          # positions for nodes without setPosition
        self._to_win = None
        self._from_win = None
        self._proc = None
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        ctx = mp.get_context('fork')           # Linux only, like Mininet
        self._to_win, self._from_win = ctx.Queue(), ctx.Queue()
        self._to_win.put(snapshot(self.net, self.overrides))
        self._proc = ctx.Process(target=_window_main,
                                 args=(self._to_win, self._from_win, self.cfg),
                                 daemon=True)
        self._proc.start()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _apply_move(self, name, x, y, z):
        node = getattr(self.net, 'nameToNode', {}).get(name)
        if node is None:
            return
        pos = '%.2f,%.2f,%.2f' % (x, y, z)
        if getattr(node, 'wintfs', None) and hasattr(node, 'setPosition'):
            try:
                node.setPosition(pos)          # Mininet-WiFi moves the node
                self.overrides.pop(name, None)
                return
            except Exception as e:
                print('\n*** wifi_gui: could not move %s (%s)' % (name, e))
        self.overrides[name] = (x, y, z)       # controllers, wired hosts...

    def _loop(self):
        last = None
        while not self._stop.is_set():
            # 1) moves requested by the window
            try:
                while True:
                    msg = self._from_win.get_nowait()
                    if msg[0] == 'move' and self.allow_move:
                        self._apply_move(*msg[1:])
            except queue.Empty:
                pass
            except Exception:
                pass
            # 2) send fresh data if something changed
            if not self._proc.is_alive():
                return                         # window was closed
            try:
                data = snapshot(self.net, self.overrides)
                if data != last:
                    self._to_win.put(data)
                    last = data
            except Exception:
                pass
            self._stop.wait(self.refresh)

    def stop(self):
        self._stop.set()
        if self._proc is not None and self._proc.is_alive():
            self._to_win.put(None)
            self._proc.join(2)
            if self._proc.is_alive():
                self._proc.terminate()


# ==========================================================================
# Part 2 - the window (separate process, Tkinter only)
# ==========================================================================

C = {
    'bg': '#f6f6f3', 'grid': '#dedfd9', 'grid_major': '#c9cbc3',
    'axis': '#8a8b85', 'text': '#26272a', 'muted': '#6d6e69',
    'ap': '#2563c9', 'ap_fill': '#e1eafa', 'ap_ring': '#7fa3e3',
    'sta': '#e0761a', 'sta_ring': '#eaa66a',
    'ctrl': '#7b3fc4', 'switch': '#4a4d55', 'host': '#6b6e74',
    'rf': '#16a05a', 'rf_glow': '#cdebd9', 'rf_bad': '#cc3a2f',
    'wired': '#4a4d55', 'control': '#7b3fc4',
    'shadow': '#d4d5cf', 'select': '#111111', 'warn': '#b3261e',
    'tip_bg': '#26272a', 'tip_fg': '#ffffff', 'bar': '#ebebe7',
}

KIND_LABEL = {'ap': 'access point', 'sta': 'station', 'ctrl': 'controller',
              'switch': 'switch', 'host': 'host'}


def _mix(c1, c2, t):
    """Blend two #rrggbb colours (t=0 -> c1, t=1 -> c2)."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return '#%02x%02x%02x' % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _nice_step(span, target=8):
    raw = max(span, 1e-9) / target
    p = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        if raw <= m * p:
            return m * p
    return 10 * p


class _Window(object):
    HIT = 16           # px radius for clicking a node

    def __init__(self, inq, outq, cfg):
        import tkinter as tk
        self.tk = tk
        self.inq, self.outq, self.cfg = inq, outq, cfg
        self.data = {'nodes': [], 'links': []}
        self.local = {}            # name -> (xyz, until_time) while dragging
        self.auto_pos = {}         # auto-placed controllers
        self.hits = []
        self.hover = None
        self.mouse = (0, 0)
        self.drag = None
        self.t0 = time.time()
        self.fitted = False
        self.user_view = False     # becomes True once you rotate/pan/zoom

        # camera
        self.yaw, self.pitch = math.radians(-30), math.radians(55)
        self.scale, self.pan = 1.0, [0.0, 0.0]
        self.target = (0.0, 0.0, 0.0)

        root = self.root = tk.Tk()
        root.title(cfg['title'])
        w, h = cfg['size']

        bar = tk.Frame(root, bg=C['bar'], padx=6, pady=4)
        bar.pack(fill='x')
        for text, cmd in (('3D view', self.view_3d), ('Top view', self.view_top),
                          ('Fit', self.fit)):
            tk.Button(bar, text=text, command=cmd, relief='flat', bg='white',
                      padx=10).pack(side='left', padx=3)
        self.v_ranges = tk.BooleanVar(value=True)
        self.v_sphere = tk.BooleanVar(value=True)
        self.v_anim = tk.BooleanVar(value=True)
        self.v_labels = tk.BooleanVar(value=True)
        for text, var in (('Ranges', self.v_ranges), ('Sphere', self.v_sphere),
                          ('Animation', self.v_anim), ('Labels', self.v_labels)):
            tk.Checkbutton(bar, text=text, variable=var, bg=C['bar'],
                           command=self.redraw).pack(side='left', padx=6)

        self.canvas = tk.Canvas(root, width=w, height=h, bg=C['bg'],
                                highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self.status = tk.Label(root, anchor='w', justify='left', padx=10,
                               pady=6, bg=C['bar'], fg=C['text'],
                               font=('TkDefaultFont', 10))
        self.status.pack(fill='x')

        cv = self.canvas
        cv.bind('<Configure>', self.on_resize)
        cv.bind('<ButtonPress-1>', self.on_press)
        cv.bind('<B1-Motion>', self.on_drag)
        cv.bind('<ButtonRelease-1>', self.on_release)
        cv.bind('<ButtonPress-3>', self.on_pan_start)
        cv.bind('<B3-Motion>', self.on_pan)
        cv.bind('<ButtonPress-2>', self.on_pan_start)
        cv.bind('<B2-Motion>', self.on_pan)
        cv.bind('<Motion>', self.on_motion)
        cv.bind('<Double-Button-1>', lambda e: self.fit())
        cv.bind('<MouseWheel>', lambda e: self.zoom(e, 1 if e.delta > 0 else -1))
        cv.bind('<Button-4>', lambda e: self.zoom(e, 1))
        cv.bind('<Button-5>', lambda e: self.zoom(e, -1))
        root.bind('<Key-t>', lambda e: self.view_top())
        root.bind('<Key-s>', lambda e: (self.v_sphere.set(not self.v_sphere.get()),
                                        self.redraw()))
        root.bind('<Key-3>', lambda e: self.view_3d())
        root.bind('<Key-f>', lambda e: self.fit())
        root.bind('<Key-a>', lambda e: (self.v_anim.set(not self.v_anim.get()),
                                        self.redraw()))
        root.after(50, self.tick)

    def on_resize(self, e):
        if self.user_view or not self.placed():
            self.redraw()
        else:
            self.fit()                 # keep the network centred until you move the view

    # ------------------------------------------------------------ data ----
    def tick(self):
        changed = False
        try:
            while True:
                item = self.inq.get_nowait()
                if item is None:
                    self.root.destroy()
                    return
                self.data, changed = item, True
        except queue.Empty:
            pass
        except (EOFError, OSError):
            pass
        if changed and not self.fitted and self.placed():
            self.fit()
            self.fitted = True
        if changed or self.v_anim.get() or self.drag:
            self.redraw()
        self.root.after(40, self.tick)          # ~25 frames per second

    def positions(self):
        """name -> xyz for every drawable node (incl. auto/local ones)."""
        now = time.time()
        pos = {}
        for n in self.data['nodes']:
            loc = self.local.get(n['name'])
            if loc and (loc[1] > now or (self.drag and self.drag.get('name') == n['name'])):
                pos[n['name']] = loc[0]
            elif n['xyz'] is not None:
                pos[n['name']] = n['xyz']
        # place controllers that have no position: behind the network
        placed = [p for name, p in pos.items()]
        if placed:
            xs = [p[0] for p in placed]; ys = [p[1] for p in placed]
            span = max(max(xs) - min(xs), max(ys) - min(ys), 50)
            cx, top = (min(xs) + max(xs)) / 2, max(ys)
        else:
            span, cx, top = 100, 50, 50
        ctrls = [n for n in self.data['nodes']
                 if n['kind'] == 'ctrl' and n['name'] not in pos]
        for i, n in enumerate(ctrls):
            off = (i - (len(ctrls) - 1) / 2) * span * 0.35
            pos[n['name']] = (cx + off, top + span * 0.45, 0.0)
            self.auto_pos[n['name']] = True
        return pos

    def placed(self):
        return any(n['xyz'] is not None for n in self.data['nodes'])

    # ---------------------------------------------------------- camera ----
    def project(self, x, y, z):
        """world -> (screen x, screen y, depth). Bigger depth = farther."""
        tx, ty, tz = self.target
        dx, dy, dz = x - tx, y - ty, z - tz
        cy, sy = math.cos(self.yaw), math.sin(self.yaw)
        x1 = dx * cy - dy * sy
        y1 = dx * sy + dy * cy
        cp, sp = math.cos(self.pitch), math.sin(self.pitch)
        up = y1 * cp + dz * sp
        depth = y1 * sp - dz * cp
        w, h = self.size()
        return (w / 2 + self.pan[0] + x1 * self.scale,
                h / 2 + self.pan[1] - up * self.scale, depth)

    def unproject(self, sx, sy, z):
        """screen point -> world (x, y) on the horizontal plane at height z."""
        w, h = self.size()
        x1 = (sx - w / 2 - self.pan[0]) / self.scale
        up = (h / 2 + self.pan[1] - sy) / self.scale
        dz = z - self.target[2]
        y1 = (up - dz * math.sin(self.pitch)) / max(math.cos(self.pitch), 0.05)
        cy, syw = math.cos(self.yaw), math.sin(self.yaw)
        dx = x1 * cy + y1 * syw
        dy = -x1 * syw + y1 * cy
        return dx + self.target[0], dy + self.target[1]

    def size(self):
        return (max(self.canvas.winfo_width(), 200),
                max(self.canvas.winfo_height(), 200))

    def world_box(self):
        pos = self.positions()
        if not pos:
            return 0, 0, 100, 100
        ranges = {n['name']: n['range'] for n in self.data['nodes']}
        xs_lo = [p[0] - ranges.get(k, 0) for k, p in pos.items()]
        xs_hi = [p[0] + ranges.get(k, 0) for k, p in pos.items()]
        ys_lo = [p[1] - ranges.get(k, 0) for k, p in pos.items()]
        ys_hi = [p[1] + ranges.get(k, 0) for k, p in pos.items()]
        return min(xs_lo), min(ys_lo), max(xs_hi), max(ys_hi)

    def fit(self):
        lo_x, lo_y, hi_x, hi_y = self.world_box()
        zs = [p[2] for p in self.positions().values()] or [0]
        self.target = ((lo_x + hi_x) / 2, (lo_y + hi_y) / 2, (min(zs) + max(zs)) / 2)
        w, h = self.size()
        span = max(hi_x - lo_x, hi_y - lo_y, 1)
        self.scale = 0.8 * min(w, h) / span
        self.pan = [0.0, 0.0]
        self.redraw()

    def view_top(self):
        self.yaw, self.pitch = 0.0, 0.0
        self.fit()

    def view_3d(self):
        self.yaw, self.pitch = math.radians(-30), math.radians(55)
        self.fit()

    # ----------------------------------------------------------- mouse ----
    def node_at(self, x, y):
        best, bd = None, self.HIT
        for name, sx, sy in self.hits:
            d = math.hypot(sx - x, sy - y)
            if d <= bd:
                best, bd = name, d
        return best

    def on_press(self, e):
        name = self.node_at(e.x, e.y)
        if name and self.cfg.get('allow_move', True):
            xyz = self.positions()[name]
            self.drag = {'mode': 'node', 'name': name, 'xyz': xyz,
                         'start': (e.x, e.y), 'sent': 0}
        else:
            self.drag = {'mode': 'rotate', 'last': (e.x, e.y)}

    def on_drag(self, e):
        d = self.drag
        if not d:
            return
        if d['mode'] == 'rotate':
            self.user_view = True
            lx, ly = d['last']
            self.yaw += (e.x - lx) * 0.01
            self.pitch = min(max(self.pitch + (e.y - ly) * 0.01, 0.0),
                             math.radians(85))
            d['last'] = (e.x, e.y)
        else:
            x, y, z = d['xyz']
            if e.state & 0x0001:                       # Shift: change height
                z = z - (e.y - d['start'][1]) / self.scale
                z = max(z, 0.0)
                d['start'] = (e.x, e.y)
            else:
                x, y = self.unproject(e.x, e.y, z)
            d['xyz'] = (x, y, z)
            self.local[d['name']] = ((x, y, z), time.time() + 2.0)
            if time.time() - d['sent'] > 0.15:         # don't flood Mininet
                self.send_move(d['name'], (x, y, z))
                d['sent'] = time.time()
        self.redraw()

    def on_release(self, e):
        d, self.drag = self.drag, None
        if d and d['mode'] == 'node':
            self.send_move(d['name'], d['xyz'])
            self.local[d['name']] = (d['xyz'], time.time() + 2.0)
        self.redraw()

    def send_move(self, name, xyz):
        try:
            self.outq.put(('move', name) + tuple(round(v, 2) for v in xyz))
        except Exception:
            pass

    def on_pan_start(self, e):
        self.pan_last = (e.x, e.y)

    def on_pan(self, e):
        self.user_view = True
        lx, ly = self.pan_last
        self.pan[0] += e.x - lx
        self.pan[1] += e.y - ly
        self.pan_last = (e.x, e.y)
        self.redraw()

    def zoom(self, e, direction):
        self.user_view = True
        k = 1.15 if direction > 0 else 1 / 1.15
        w, h = self.size()
        mx, my = e.x - w / 2, e.y - h / 2
        self.pan[0] = mx - (mx - self.pan[0]) * k        # zoom toward mouse
        self.pan[1] = my - (my - self.pan[1]) * k
        self.scale *= k
        self.redraw()

    def on_motion(self, e):
        self.mouse = (e.x, e.y)
        h = self.node_at(e.x, e.y)
        if h != self.hover:
            self.hover = h
            self.canvas.config(cursor='fleur' if h else '')
            if not self.v_anim.get():
                self.redraw()
        elif h:
            if not self.v_anim.get():
                self.redraw()

    # --------------------------------------------------------- drawing ----
    def circle_pts(self, x, y, z, r, n=56):
        pts = []
        for i in range(n):
            a = 2 * math.pi * i / n
            sx, sy, _ = self.project(x + r * math.cos(a), y + r * math.sin(a), z)
            pts += [sx, sy]
        return pts

    def sphere(self, p, r, color, width=1.5, dash=None, fill=None,
               rings=(-60, -30, 0, 30, 60), meridians=3, soft=0.55):
        """Wireframe sphere of radius r (world units) centred on p."""
        cv = self.canvas
        x, y, z = p
        cx, cy, _ = self.project(x, y, z)
        R = r * self.scale
        if fill:                                   # translucent body
            cv.create_oval(cx - R, cy - R, cx + R, cy + R,
                           fill=fill, stipple='gray12', outline='')
        inner = _mix(color, C['bg'], soft)         # lighter inner lines
        for lat in rings:                          # horizontal rings
            a = math.radians(lat)
            pts = self.circle_pts(x, y, z + r * math.sin(a),
                                  r * math.cos(a), 40)
            cv.create_polygon(pts, fill='', outline=inner,
                              width=1.5 if lat == 0 else 1, dash=dash)
        for k in range(meridians):                 # vertical great circles
            az = math.pi * k / meridians
            pts = []
            for i in range(41):
                t = 2 * math.pi * i / 40
                sx, sy, _ = self.project(x + r * math.cos(t) * math.cos(az),
                                         y + r * math.cos(t) * math.sin(az),
                                         z + r * math.sin(t))
                pts += [sx, sy]
            cv.create_line(*pts, fill=inner, dash=dash)
        cv.create_oval(cx - R, cy - R, cx + R, cy + R,     # silhouette
                       outline=color, width=width, dash=dash)

    def redraw(self):
        cv = self.canvas
        cv.delete('all')
        t = time.time() - self.t0
        anim = self.v_anim.get()
        pos = self.positions()
        nodes = {n['name']: n for n in self.data['nodes']}

        self.draw_grid()

         # ranges: spheres or flat circles, depending on the toggle
        if self.v_ranges.get():
            sphere = self.v_sphere.get()
            for name, p in pos.items():
                n = nodes[name]
                if n['range'] <= 0:
                    continue
                is_ap = n['kind'] == 'ap'
                if sphere:
                    if is_ap:
                        self.sphere(p, n['range'], C['ap'], fill=C['ap_fill'])
                    else:
                        self.sphere(p, n['range'], C['sta'], width=1.2,
                                    dash=(4, 4), rings=(0,), meridians=1)
                else:
                    pts = self.circle_pts(*p, n['range'])
                    if is_ap:
                        cv.create_polygon(pts, fill=C['ap_fill'], outline='')
                        cv.create_polygon(pts, fill='', outline=C['ap'], width=1.5)
                    else:
                        cv.create_polygon(pts, fill='', outline=C['sta'],
                                          dash=(4, 4))
            if anim:                                   # expanding ripples
                for name, p in pos.items():
                    n = nodes[name]
                    if n['range'] <= 0:
                        continue
                    is_ap = n['kind'] == 'ap'
                    period = 2.4 if is_ap else 3.2
                    count = 3 if is_ap else 1
                    base = C['ap_ring'] if is_ap else C['sta_ring']
                    end = C['ap_fill'] if is_ap else C['bg']
                    cx, cy, _ = self.project(*p)
                    for k in range(count):
                        f = ((t / period) + k / count) % 1.0
                        r = n['range'] * f
                        col = _mix(base, end, f)
                        w = 2 if is_ap else 1
                        if sphere:
                            R = r * self.scale
                            cv.create_oval(cx - R, cy - R, cx + R, cy + R,
                                           outline=col, width=w)
                        cv.create_polygon(self.circle_pts(*p, r, 40),
                                          fill='', outline=col, width=w)

        # height stems + ground shadows (helps reading 3D)
        for name, p in pos.items():
            if abs(p[2]) > 1e-6:
                gx, gy, _ = self.project(p[0], p[1], 0)
                sx, sy, _ = self.project(*p)
                cv.create_oval(gx - 7, gy - 3, gx + 7, gy + 3,
                               fill=C['shadow'], outline='')
                cv.create_line(gx, gy, sx, sy, fill=C['axis'], dash=(2, 3))

        # links
        for a, b, kind in self.data['links']:
            if a in pos and b in pos:
                x1, y1, _ = self.project(*pos[a])
                x2, y2, _ = self.project(*pos[b])
                if kind == 'wired':
                    cv.create_line(x1, y1, x2, y2, fill=C['wired'], width=3)
                else:
                    cv.create_line(x1, y1, x2, y2, fill=C['control'], width=2,
                                   dash=(8, 5))
                    if self.v_labels.get():
                        cv.create_text((x1 + x2) / 2, (y1 + y2) / 2 - 8,
                                       text='OpenFlow', fill=C['control'],
                                       font=('TkDefaultFont', 8))
        for name, n in nodes.items():
            if n['kind'] == 'sta' and n['ap'] in pos and name in pos:
                d = math.dist(pos[name][:3], pos[n['ap']][:3])
                ok = d <= max(nodes[n['ap']]['range'], 1e-9)
                self.draw_rf(pos[name], pos[n['ap']], t if anim else 0, ok, n.get('rssi'))

        # nodes, far ones first
        self.hits = []
        order = sorted(pos.items(), key=lambda kv: -self.project(*kv[1])[2])
        for name, p in order:
            self.draw_node(nodes[name], *self.project(*p)[:2])

        self.draw_overlay()
        self.update_status(pos, nodes)

    def draw_grid(self):
        cv = self.canvas
        lo_x, lo_y, hi_x, hi_y = self.world_box()
        step = _nice_step(max(hi_x - lo_x, hi_y - lo_y))
        gx0 = math.floor(lo_x / step) * step
        gy0 = math.floor(lo_y / step) * step
        gx1 = math.ceil(hi_x / step) * step
        gy1 = math.ceil(hi_y / step) * step
        v = gx0
        while v <= gx1 + 1e-9:
            a = self.project(v, gy0, 0); b = self.project(v, gy1, 0)
            major = abs(v) < 1e-9
            cv.create_line(a[0], a[1], b[0], b[1],
                           fill=C['grid_major'] if major else C['grid'])
            cv.create_text(a[0], a[1] + 12, text='%g' % v, fill=C['axis'],
                           font=('TkDefaultFont', 8))
            v += step
        v = gy0
        while v <= gy1 + 1e-9:
            a = self.project(gx0, v, 0); b = self.project(gx1, v, 0)
            major = abs(v) < 1e-9
            cv.create_line(a[0], a[1], b[0], b[1],
                           fill=C['grid_major'] if major else C['grid'])
            cv.create_text(a[0] - 14, a[1], text='%g' % v, fill=C['axis'],
                           font=('TkDefaultFont', 8))
            v += step
        o = self.project(gx0, gy0, 0)
        ex = self.project(gx0 + step, gy0, 0)
        ey = self.project(gx0, gy0 + step, 0)
        ez = self.project(gx0, gy0, step)
        for end, label, col in ((ex, 'x', '#c0392b'), (ey, 'y', '#1e8449'),
                                (ez, 'z', '#2457a6')):
            cv.create_line(o[0], o[1], end[0], end[1], fill=col, width=2,
                           arrow='last')
            cv.create_text(end[0] + 8, end[1] - 6, text=label, fill=col,
                           font=('TkDefaultFont', 9, 'bold'))

    def draw_rf(self, a, b, t, ok, rssi=None):
        """A radio link: soft glow + a travelling sine wave."""
        cv = self.canvas
        x1, y1, _ = self.project(*a)
        x2, y2, _ = self.project(*b)
        L = math.hypot(x2 - x1, y2 - y1)
        if L < 2:
            return
        ux, uy = (x2 - x1) / L, (y2 - y1) / L
        nx, ny = -uy, ux

        if not ok:
            color = '#cc3a2f'
            glow = '#f3d0cc'
        elif rssi is None:
            color = '#16a05a'
            glow = '#cdebd9'
        elif rssi >= -50:
            # Excellent
            color = '#16a05a'
            glow = '#cdebd9'
        elif rssi >= -65:
            # Bon
            color = '#7cb342'
            glow = '#dcedc8'
        elif rssi >= -75:
            # Moyen
            color = '#f39c12'
            glow = '#fdebd0'
        else:
            # Mauvais
            color = '#cc3a2f'
            glow = '#f3d0cc'

        cv.create_line(x1, y1, x2, y2, fill=glow,
                       width=8, capstyle='round')
        wl, amp, speed = 16.0, 4.5, 40.0
        pts = []
        steps = max(int(L / 2.5), 8)
        for i in range(steps + 1):
            d = L * i / steps
            env = min(1.0, d / 12.0, (L - d) / 12.0)      # taper the ends
            off = amp * env * math.sin(2 * math.pi * (d - speed * t) / wl)
            pts += [x1 + ux * d + nx * off, y1 + uy * d + ny * off]
        cv.create_line(*pts, fill=color, width=3, smooth=True)
        if rssi is not None:
            text = '%.1f dBm' % rssi
            cv.create_text((x1 + x2) / 2,(y1 + y2) / 2 - 15,text=text,fill=color,font=('TkDefaultFont',10,'bold'))
        elif not ok:
            cv.create_text((x1 + x2) / 2,(y1 + y2) / 2 - 12,text='OUT OF RANGE',fill='#cc3a2f',font=('TkDefaultFont',8,'bold'))

    def draw_node(self, n, x, y):
        cv = self.canvas
        kind, name = n['kind'], n['name']
        hl = (self.hover == name) or (self.drag and self.drag.get('name') == name)
        ow = C['select'] if hl else 'white'
        if kind == 'ap':
            cv.create_rectangle(x - 10, y - 7, x + 10, y + 7, fill=C['ap'],
                                outline=ow, width=2)
            cv.create_line(x + 6, y - 7, x + 6, y - 15, fill=C['ap'], width=2)
            for r in (5, 9):                                # wifi arcs
                cv.create_arc(x + 6 - r, y - 15 - r, x + 6 + r, y - 15 + r,
                              start=45, extent=90, style='arc',
                              outline=C['ap'], width=1.5)
            for i in (-5, 0):                               # leds
                cv.create_oval(x + i - 1.5, y - 1.5, x + i + 1.5, y + 1.5,
                               fill='#9ff0b8', outline='')
        elif kind == 'sta':
            cv.create_oval(x - 8, y - 8, x + 8, y + 8, fill=C['sta'],
                           outline=ow, width=2)
            cv.create_oval(x - 2.5, y - 2.5, x + 2.5, y + 2.5, fill='white',
                           outline='')
        elif kind == 'ctrl':
            pts = []
            for i in range(6):
                a = math.pi / 6 + i * math.pi / 3
                pts += [x + 12 * math.cos(a), y + 12 * math.sin(a)]
            cv.create_polygon(pts, fill=C['ctrl'], outline=ow, width=2)
            cv.create_text(x, y, text='C', fill='white',
                           font=('TkDefaultFont', 9, 'bold'))
        elif kind == 'switch':
            cv.create_rectangle(x - 12, y - 6, x + 12, y + 6, fill=C['switch'],
                                outline=ow, width=2)
        else:
            cv.create_rectangle(x - 8, y - 8, x + 8, y + 8, fill=C['host'],
                                outline=ow, width=2)
        if self.v_labels.get():
            label = name + (' (auto)' if kind == 'ctrl' and self.is_auto(name) else '')
            cv.create_text(x, y + 20, text=label, fill=C['text'],
                           font=('TkDefaultFont', 10, 'bold'))
        self.hits.append((name, x, y))

    def is_auto(self, name):
        for n in self.data['nodes']:
            if n['name'] == name:
                return n['xyz'] is None and name not in self.local
        return False

    def draw_overlay(self):
        cv = self.canvas
        w, h = self.size()
        # legend
        x, y = 12, 14
        items = [('ap', 'Access point'), ('sta', 'Station'), ('ctrl', 'Controller')]
        for kind, text in items:
            col = C[kind]
            if kind == 'ap':
                cv.create_rectangle(x, y - 5, x + 12, y + 5, fill=col, outline='')
            elif kind == 'sta':
                cv.create_oval(x + 1, y - 5, x + 11, y + 5, fill=col, outline='')
            else:
                cv.create_polygon(x + 6, y - 6, x + 12, y - 3, x + 12, y + 3,
                                  x + 6, y + 6, x, y + 3, x, y - 3, fill=col)
            cv.create_text(x + 18, y, text=text, anchor='w', fill=C['text'],
                           font=('TkDefaultFont', 9))
            x += 26 + 7 * len(text)
        for kind, text, dash, col, wd in (('rf', 'Wireless', None, C['rf'], 2),
                                          ('wired', 'Cable', None, C['wired'], 3),
                                          ('control', 'Control', (6, 4), C['control'], 2)):
            if kind == 'rf':
                pts = []
                for i in range(13):
                    pts += [x + i * 2, y + 3 * math.sin(i * 1.1)]
                cv.create_line(*pts, fill=col, width=wd, smooth=True)
            else:
                cv.create_line(x, y, x + 24, y, fill=col, width=wd, dash=dash)
            cv.create_text(x + 30, y, text=text, anchor='w', fill=C['text'],
                           font=('TkDefaultFont', 9))
            x += 40 + 7 * len(text)
        # help
        cv.create_text(12, h - 12, anchor='sw', fill=C['muted'],
                       font=('TkDefaultFont', 8),
                       text='drag: rotate   right-drag: pan   wheel: zoom   '
                            'drag node: move   Shift+drag node: height   '
                            'T/3/F: top/3D/fit')
        # tooltip
        if self.hover and not (self.drag and self.drag.get('mode') == 'rotate'):
            self.draw_tip()

    def draw_tip(self):
        cv = self.canvas
        n = next((n for n in self.data['nodes'] if n['name'] == self.hover), None)
        p = self.positions().get(self.hover)
        if not n or not p:
            return
        lines = ['%s  (%s)' % (n['name'], KIND_LABEL.get(n['kind'], n['kind'])),
                 'position: %.1f, %.1f, %.1f' % p]
        if n['range'] > 0:
            lines.append('range: %g m' % n['range'])
        if n['kind'] == 'sta':
            lines.append(
                'connected to: %s'
                % (n['ap'] or 'nothing')
            )

            rssi = n.get('rssi')

            if rssi is not None:
                lines.append(
                    'RSSI: %.1f dBm'
                    % rssi
                )
            else:
                lines.append(
                    'RSSI: unavailable'
                )
        if n['kind'] == 'ctrl' and self.is_auto(n['name']):
            lines.append('(placed automatically, drag to move)')
        mx, my = self.mouse
        tid = cv.create_text(mx + 16, my - 12, text='\n'.join(lines), anchor='sw',
                             fill=C['tip_fg'], font=('TkDefaultFont', 9))
        b = cv.bbox(tid)
        bg = cv.create_rectangle(b[0] - 7, b[1] - 5, b[2] + 7, b[3] + 5,
                                 fill=C['tip_bg'], outline='')
        cv.tag_raise(tid, bg)

    def update_status(self, pos, nodes):
        lines, warn = [], False
        for n in self.data['nodes']:
            if n['name'] not in pos:
                warn = True
                lines.append('!  %s has no position, so it is not drawn  '
                             '(add position="x,y,z")' % n['name'])
            elif n['kind'] == 'sta':
                ap = n['ap']
                if ap and ap in pos:
                    d = math.dist(pos[n['name']], pos[ap])
                    rng = nodes[ap]['range']
                    state = 'in range' if d <= rng else 'OUT OF RANGE'
                    lines.append('%s  ->  %s    %.0f m   (%s, AP range %g m)'
                                 % (n['name'], ap, d, state, rng))
                else:
                    lines.append('%s  ->  not connected' % n['name'])
        if not self.data['nodes']:
            lines.append('Waiting for network data...')
        text = '\n'.join(lines) or ' '
        if text != self.status.cget('text'):
            self.status.config(text=text, fg=C['warn'] if warn else C['text'])

    def run(self):
        self.root.mainloop()


def _window_main(inq, outq, cfg):
    signal.signal(signal.SIGINT, signal.SIG_IGN)    # Ctrl+C belongs to the CLI
    try:
        win = _Window(inq, outq, cfg)
    except Exception as e:
        print('\n*** wifi_gui: could not open the window (%s)\n'
              '*** Try: sudo -E python3 your_script.py\n'
              '*** or run "xhost +local:root" first.\n' % e)
        return
    win.run()