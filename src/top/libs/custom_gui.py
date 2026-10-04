#!/usr/bin/env python3
"""
custom_gui.py  -  interactive 3D viewer for Mininet-WiFi  (version 3)

No matplotlib. Pure Tkinter (comes with Python: sudo apt install python3-tk).
Pictures need Pillow (sudo apt install python3-pil python3-pil.imagetk).

What you get
  - 3D view you can rotate, pan and zoom, plus a flat top view
  - Drag any node with the mouse to move it, and Mininet-WiFi moves it too
    (hold Shift while dragging to change its height)
  - Every device of the editor: access points, stations, cars, hosts,
    switches, controllers
  - Range sphere / circle for every wireless node (or outlines only)
  - Wireless connections drawn as animated radio waves (colour = RSSI),
    ad-hoc / mesh groups as dashed green links, wired links as cables,
    controllers with a dashed control link to the APs they manage
  - Walls and pictures (floor plans) from a custom_edit.py file, with the
    same camera modes as the editor (front walls transparent / cut down...)
  - Hover a node to see its details; the bar at the bottom lists who is
    connected to whom, through which walls, and warns about problems

Bridge with custom_edit.py
  The editor saves a JSON file (normalized or custom format). Devices are
  created in YOUR script (net.addStation(...) etc.); the viewer only takes
  the non-device elements from the file:

      gui = WifiGUI(net)
      gui.load_json('topo.json')      # walls + pictures (+ wall losses)
      ...
      net.build()
      gui.start()

  load_json(path, apply_walls=True, fill_positions=True)
      apply_walls     make Mininet-WiFi subtract the walls' dB loss from
                      its RSSI (works without wmediumd and with wmediumd
                      SNR mode; wmediumd interference mode ignores walls).
                      Call load_json BEFORE net.build() for this to count
                      from the first association.
      fill_positions  devices of the file that have no position in your
                      net (controllers, usually) are drawn where the
                      editor placed them
  It also compares the device names of the file with your net and lists
  the differences in the status bar.
  load_layout(dict) does the same from data already in memory
  ({'walls': [...], 'pictures': [...]}), used by scripts the editor exports.

Mouse & keys
  drag empty space ......... rotate          right-drag ....... pan
  mouse wheel .............. zoom            double-click ..... fit to screen
  drag a node .............. move it         Shift + drag ..... change height
  keys: T = top view, 3 = 3D view, F = fit, A = animation on/off,
        S = sphere on/off, W = walls on/off, P = pictures on/off

How it works with Mininet-WiFi
  Your script                                Window process
  -----------                                --------------
  gui = WifiGUI(net)
  gui.load_json('topo.json')  --- layout -->  (walls, pictures)
  net.build(); gui.start()   ------>  opens the window
  background thread, every 0.3 s:
      reads positions, ranges,
      associations, links      ---- data --->  redraws the scene
  applies moves  <--- "move sta1 to x,y,z" ---  you drag a node
  CLI(net)   <- you keep typing commands as usual
  gui.stop(); net.stop()

  The window lives in its own process, so it never freezes while the CLI
  waits for input, and a crash in the GUI can't take your network down.

  Run with:  sudo -E python3 your_script.py     (-E lets root open a window)
  Don't also call net.plotGraph(): use one viewer or the other.
"""

import json
import math
import multiprocessing as mp
import os
import queue
import signal
import threading
import time


# ==========================================================================
# Part 1 - walls (pure math, same rules as custom_edit.py)
# ==========================================================================

def wall_corners(w):
    """Footprint corners (x, y), counter-clockwise."""
    a = math.radians(float(w['rotation']))
    ca, sa = math.cos(a), math.sin(a)
    L, T = float(w['length']) / 2, float(w['thickness']) / 2
    return [(w['x'] + lx * ca - ly * sa, w['y'] + lx * sa + ly * ca)
            for lx, ly in ((-L, -T), (L, -T), (L, T), (-L, T))]


def wall_db(w):
    """Attenuation of one wall (dB), thickness multiplier included."""
    loss = float(w.get('loss') or 0)
    if w.get('material', 'custom') == 'custom':
        return loss
    k = float(w.get('thick_mult') or 0)
    ref = float(w.get('ref_thickness') or 0)
    if not k or ref <= 0:
        return loss
    return max(0.0, loss * (1 + k * (float(w['thickness']) / ref - 1)))


def wall_crossed(w, a, b):
    """(t0, t1) part of the segment a -> b inside the wall, or None.
    Height 0 = flat 2D wall (blocks at any height)."""
    ang = math.radians(float(w['rotation']))
    ca, sa = math.cos(ang), math.sin(ang)

    def local(p):
        dx, dy = p[0] - w['x'], p[1] - w['y']
        return dx * ca + dy * sa, -dx * sa + dy * ca
    (ax, ay), (bx, by) = local(a), local(b)
    dx, dy = bx - ax, by - ay
    t0, t1 = 0.0, 1.0
    L, T = float(w['length']) / 2, float(w['thickness']) / 2
    for p, q in ((-dx, ax + L), (dx, L - ax), (-dy, ay + T), (dy, T - ay)):
        if abs(p) < 1e-12:
            if q < 0:
                return None
            continue
        r = q / p
        if p < 0:
            t0 = max(t0, r)
        else:
            t1 = min(t1, r)
        if t0 > t1:
            return None
    h = float(w.get('height') or 0)
    if h <= 0:
        return (t0, t1)
    za = a[2] + (b[2] - a[2]) * t0
    zb = a[2] + (b[2] - a[2]) * t1
    base = float(w.get('z') or 0)
    if max(za, zb) >= base and min(za, zb) <= base + h:
        return (t0, t1)
    return None


def wall_loss(walls, a, b):
    """(total dB, walls counted) between two points; overlapping walls on
    the same stretch of path are counted once (highest loss)."""
    segs = []
    for w in walls or []:
        iv = wall_crossed(w, a, b)
        if iv:
            segs.append((iv[0], iv[1], wall_db(w), w))
    segs.sort(key=lambda s: s[0])
    total, hit, cur = 0.0, [], None
    for s in segs:
        if cur is not None and s[0] < cur[1] - 1e-6:
            cur[1] = max(cur[1], s[1])
            if s[2] > cur[2]:
                cur[2], cur[3] = s[2], s[3]
            continue
        if cur is not None:
            total += cur[2]
            hit.append(cur[3])
        cur = [s[0], s[1], s[2], s[3]]
    if cur is not None:
        total += cur[2]
        hit.append(cur[3])
    return total, hit


def apply_walls(walls):
    """Make Mininet-WiFi subtract wall losses from its RSSI (wraps
    PropagationModel.__init__). Calling it again replaces the walls."""
    if not walls:
        return
    from mn_wifi.propagationModels import PropagationModel
    orig = getattr(PropagationModel.__init__, '_orig', PropagationModel.__init__)

    def pos(node):
        try:
            return [float(v) for v in node.getxyz()]
        except Exception:
            return [float(v) for v in node.position]

    def __init__(self, intf, apintf, dist=0):
        orig(self, intf, apintf, dist)
        try:
            loss = wall_loss(walls, pos(intf.node), pos(apintf.node))[0]
            if loss:
                self.rssi = self.rssi - loss
        except Exception:
            pass
    __init__._orig = orig
    PropagationModel.__init__ = __init__


# ==========================================================================
# Part 2 - the bridge: read a custom_edit.py file
# ==========================================================================

_WALL_DEFAULTS = {'name': 'wall', 'material': 'custom', 'loss': 0.0,
                  'thick_mult': 0.0, 'ref_thickness': 0.2,
                  'color': '#9c9a94', 'height': 3.0, 'z': 0.0,
                  'length': 1.0, 'thickness': 0.2, 'rotation': 0.0,
                  'x': 0.0, 'y': 0.0, 'locked': False, 'group': ''}
_PIC_DEFAULTS = {'path': '', 'x': 0.0, 'y': 0.0, 'z': 0.0, 'width': 100.0,
                 'rotation': 0.0, 'opacity': 100.0, 'visible': True}
_KIND_OF_LIST = {'accessPoints': 'ap', 'stations': 'sta', 'cars': 'car',
                 'hosts': 'host', 'switches': 'switch', 'controllers': 'ctrl'}
_KIND_OF_NAME = {'access_point': 'ap', 'station': 'sta', 'car': 'car',
                 'host': 'host', 'switch': 'switch', 'controller': 'ctrl'}


def _xyz_of(pos):
    if pos is None:
        return None
    if isinstance(pos, dict):
        pos = [pos.get('x', 0), pos.get('y', 0), pos.get('z', 0)]
    if isinstance(pos, str):
        pos = pos.split(',')
    try:
        vals = [float(v) for v in pos]
    except (TypeError, ValueError):
        return None
    while len(vals) < 3:
        vals.append(0.0)
    return tuple(vals[:3])


def _norm_wall(w):
    d = dict(_WALL_DEFAULTS)
    fp = w.get('footprint') if isinstance(w.get('footprint'), dict) else {}
    src = dict(w, **fp)
    if 'loss_db' in src and 'loss' not in src:
        src['loss'] = src['loss_db']
    if 'thickness_multiplier' in src and 'thick_mult' not in src:
        src['thick_mult'] = src['thickness_multiplier']
    for k, v in _WALL_DEFAULTS.items():
        if src.get(k) is None:
            continue
        if isinstance(v, bool):
            d[k] = bool(src[k])
        elif isinstance(v, float):
            d[k] = float(src[k])
        else:
            d[k] = str(src[k])
    return d


def _norm_pic(p, base):
    d = dict(_PIC_DEFAULTS)
    for k, v in _PIC_DEFAULTS.items():
        if p.get(k) is None:
            continue
        d[k] = bool(p[k]) if isinstance(v, bool) else (
            float(p[k]) if isinstance(v, float) else str(p[k]))
    if d['path'] and not os.path.isabs(d['path']):
        d['path'] = os.path.normpath(os.path.join(base, d['path']))
    return d


def read_layout(path):
    """Parse a custom_edit.py file (normalized or custom schema).
    -> {'walls': [...], 'pictures': [...], 'devices': {name: (kind, xyz)},
        'source': path}"""
    with open(path) as fh:
        d = json.load(fh)
    base = os.path.dirname(os.path.abspath(path))
    devices = {}
    if d.get('schema') == 'custom':
        walls = d.get('walls') or []
        pics = d.get('pictures') or []
        for dev in d.get('devices') or []:
            devices[dev['name']] = (_KIND_OF_NAME.get(dev.get('kind'),
                                                      dev.get('kind')),
                                    _xyz_of(dev.get('position')))
    else:
        walls = d.get('walls') or []
        pics = (d.get('editor') or {}).get('pictures') or []
        for key, kind in _KIND_OF_LIST.items():
            for e in d.get(key) or []:
                devices[e['name']] = (kind, _xyz_of(
                    (e.get('params') or {}).get('position')))
    return {'walls': [_norm_wall(w) for w in walls],
            'pictures': [_norm_pic(p, base) for p in pics],
            'devices': devices, 'source': os.path.abspath(path)}


# ==========================================================================
# Part 3 - reading the network (runs inside your Mininet script)
# ==========================================================================

WIRELESS_LINK_WORDS = ('wifi', 'wireless', 'wmediumd', 'assoc', 'adhoc',
                       'mesh', 'its', 'direct', 'mode')


def _rssi(node):
    """RSSI of the first Wi-Fi interface of a station."""
    try:
        if node.wintfs:
            return float(node.wintfs[0].rssi)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        pass
    return None


def _xyz(node, overrides, fallback=None):
    """(x, y, z) of a node, or None if it has no position."""
    if node.name in overrides:
        return overrides[node.name]
    pos = None
    try:
        pos = node.getxyz()
    except Exception:
        pos = getattr(node, 'position', None) or \
            getattr(node, 'params', {}).get('position')
    xyz = _xyz_of(pos)
    if xyz is None and fallback:
        xyz = fallback.get(node.name)
    return xyz


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


def _peer_mode(intf):
    """'adhoc' / 'mesh' when the interface is in that mode, else None."""
    cls = type(intf).__name__.lower()
    for m in ('adhoc', 'mesh'):
        if m in cls:
            return m
    a = getattr(intf, 'associatedTo', None)
    if isinstance(a, str) and a.lower() in ('adhoc', 'mesh'):
        return a.lower()
    return None


def snapshot(net, overrides=None, fallback=None):
    """Everything the window needs, as plain data (dicts/lists/tuples)."""
    overrides = overrides or {}
    groups = [('ap', 'aps'), ('car', 'cars'), ('sta', 'stations'),
              ('switch', 'switches'), ('host', 'hosts'),
              ('ctrl', 'controllers')]
    nodes, by_name = [], {}
    for kind, attr in groups:
        for node in getattr(net, attr, None) or []:
            if node.name in by_name:
                continue
            client = kind in ('sta', 'car')
            item = {'name': node.name, 'kind': kind,
                    'xyz': _xyz(node, overrides, fallback),
                    'range': _range(node),
                    'ap': _associated_ap(node) if client else None,
                    'rssi': _rssi(node) if client else None}
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

    # ad-hoc / mesh: nodes sharing a mode + ssid talk to each other
    peers = {}
    for name, node in by_name.items():
        for intf in getattr(node, 'wintfs', {}).values():
            mode = _peer_mode(intf)
            if mode:
                key = (mode, str(getattr(intf, 'ssid', None) or ''))
                if name not in peers.setdefault(key, []):
                    peers[key].append(name)
    for (mode, ssid), names in peers.items():
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                links.append((names[i], names[j], mode, ssid))

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

    def __init__(self, net, refresh=0.3, size=(1000, 760),
                 title='Mininet-WiFi 3D View', allow_move=True):
        self.net = net
        self.refresh = refresh
        self.allow_move = allow_move
        self.cfg = {'size': size, 'title': title, 'allow_move': allow_move}
        self.overrides = {}          # positions for nodes without setPosition
        self.fallback = {}           # positions from the editor's file
        self.layout = None           # walls + pictures
        self._to_win = None
        self._from_win = None
        self._proc = None
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------- bridge ----
    def load_json(self, path, apply_walls=True, fill_positions=True):
        """Take walls and pictures from a custom_edit.py file.
        Devices are NOT created: add them to your net as usual."""
        layout = read_layout(path)
        self.load_layout(layout, apply_walls=apply_walls,
                         fill_positions=fill_positions)
        return self.layout

    def load_layout(self, layout, apply_walls=False, fill_positions=True):
        """Same as load_json, from a dict {'walls': [...], 'pictures': [...],
        'devices': {name: (kind, xyz)} (optional)}."""
        base = os.getcwd()
        walls = [_norm_wall(w) for w in layout.get('walls') or []]
        pics = [_norm_pic(p, base) for p in layout.get('pictures') or []]
        devices = layout.get('devices') or {}
        if apply_walls and walls:
            try:
                globals()['apply_walls'](walls)
            except ImportError:
                print('\n*** custom_gui: Mininet-WiFi not found, wall losses '
                      'are only drawn, not simulated')
        warnings = []
        if devices:
            ours = set()
            for attr in ('aps', 'stations', 'cars', 'switches', 'hosts',
                         'controllers'):
                ours.update(n.name for n in getattr(self.net, attr, None)
                            or [])
            missing = sorted(set(devices) - ours)
            extra = sorted(ours - set(devices))
            if missing:
                warnings.append('In the file but not in your net: ' +
                                ', '.join(missing))
            if extra:
                warnings.append('In your net but not in the file: ' +
                                ', '.join(extra))
        if fill_positions:
            self.fallback = dict((name, xyz) for name, (kind, xyz)
                                 in devices.items() if xyz is not None)
        for p in pics:
            if p['path'] and not os.path.isfile(p['path']):
                warnings.append('Picture not found: %s' % p['path'])
        self.layout = {'type': 'layout', 'walls': walls, 'pictures': pics,
                       'warnings': warnings,
                       'source': layout.get('source', '')}
        if self._to_win is not None:
            self._to_win.put(self.layout)
        return self.layout

    # ------------------------------------------------------ process ----
    def start(self):
        ctx = mp.get_context('fork')           # Linux only, like Mininet
        self._to_win, self._from_win = ctx.Queue(), ctx.Queue()
        if self.layout:
            self._to_win.put(self.layout)
        self._to_win.put(snapshot(self.net, self.overrides, self.fallback))
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
                print('\n*** custom_gui: could not move %s (%s)' % (name, e))
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
                data = snapshot(self.net, self.overrides, self.fallback)
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
# Part 4 - the window (separate process, Tkinter only)
# ==========================================================================

C = {
    'bg': '#f6f6f3', 'grid': '#dedfd9', 'grid_major': '#c9cbc3',
    'axis': '#8a8b85', 'text': '#26272a', 'muted': '#6d6e69',
    'ap': '#2563c9', 'ap_fill': '#e1eafa', 'ap_ring': '#7fa3e3',
    'sta': '#e0761a', 'sta_ring': '#eaa66a', 'car': '#b9461a',
    'ctrl': '#7b3fc4', 'switch': '#4a4d55', 'host': '#6b6e74',
    'rf': '#16a05a', 'rf_glow': '#cdebd9', 'rf_bad': '#cc3a2f',
    'wired': '#4a4d55', 'control': '#7b3fc4', 'peer': '#16a05a',
    'shadow': '#d4d5cf', 'select': '#111111', 'warn': '#b3261e',
    'tip_bg': '#26272a', 'tip_fg': '#ffffff', 'bar': '#ebebe7',
    'wall': '#b4583d',
}

KIND_LABEL = {'ap': 'access point', 'sta': 'station', 'car': 'car',
              'ctrl': 'controller', 'switch': 'switch', 'host': 'host'}
CLIENT_KINDS = ('sta', 'car')
WALL_MODES = [('solid', 'Walls always visible'),
              ('fade', 'Front walls transparent'),
              ('cutaway', 'Front walls cut down'),
              ('outline', 'Walls as outlines')]


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


def _in_poly(x, y, pts):
    inside, j = False, len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and \
                x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi:
            inside = not inside
        j = i
    return inside


def _safe_color(c, default='#999999'):
    c = str(c or '')
    if len(c) == 7 and c[0] == '#':
        try:
            int(c[1:], 16)
            return c
        except ValueError:
            pass
    return default


class _Window(object):
    HIT = 16           # px radius for clicking a node

    def __init__(self, inq, outq, cfg):
        import tkinter as tk
        from tkinter import ttk
        self.tk = tk
        self.inq, self.outq, self.cfg = inq, outq, cfg
        self.data = {'nodes': [], 'links': []}
        self.layout = {'walls': [], 'pictures': [], 'warnings': []}
        self.local = {}            # name -> (xyz, until_time) while dragging
        self.auto_pos = {}         # auto-placed controllers
        self.hits = []
        self.hover = None
        self.mouse = (0, 0)
        self.drag = None
        self.t0 = time.time()
        self.fitted = False
        self.user_view = False     # becomes True once you rotate/pan/zoom
        self.pic_src, self.pic_tk = {}, {}

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
        self.v_outline = tk.BooleanVar(value=False)
        self.v_sphere = tk.BooleanVar(value=True)
        self.v_anim = tk.BooleanVar(value=True)
        self.v_labels = tk.BooleanVar(value=True)
        self.v_walls = tk.BooleanVar(value=True)
        self.v_pics = tk.BooleanVar(value=True)
        self.wall_mode_label = tk.StringVar(value=WALL_MODES[1][1])
        self.checks = {}
        for text, var in (('Ranges', self.v_ranges),
                          ('outline only', self.v_outline),
                          ('Sphere', self.v_sphere),
                          ('Animation', self.v_anim), ('Labels', self.v_labels),
                          ('Walls', self.v_walls), ('Pictures', self.v_pics)):
            cb = tk.Checkbutton(bar, text=text, variable=var, bg=C['bar'],
                                activebackground=C['bar'],
                                command=self.on_toggle)
            cb.pack(side='left', padx=4)
            self.checks[text] = cb
        tk.Label(bar, text='camera:', bg=C['bar'], fg=C['muted']).pack(
            side='left', padx=(8, 2))
        self.cb_mode = ttk.Combobox(bar, textvariable=self.wall_mode_label,
                                    values=[l for _, l in WALL_MODES],
                                    state='readonly', width=22)
        self.cb_mode.pack(side='left')
        self.cb_mode.bind('<<ComboboxSelected>>', lambda e: (
            self.redraw(), self.canvas.focus_set()))

        self.status = tk.Label(root, anchor='nw', justify='left', padx=10,
                               pady=6, bg=C['bar'], fg=C['text'],
                               font=('TkDefaultFont', 10))
        self.status.pack(side='bottom', fill='x')
        self.canvas = tk.Canvas(root, width=w, height=h, bg=C['bg'],
                                highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)

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
        for key, fn in (('t', self.view_top), ('3', self.view_3d),
                        ('f', self.fit)):
            root.bind('<Key-%s>' % key, lambda e, f=fn: f())
        for key, var in (('s', self.v_sphere), ('a', self.v_anim),
                         ('w', self.v_walls), ('p', self.v_pics)):
            root.bind('<Key-%s>' % key, lambda e, v=var: (v.set(not v.get()),
                                                          self.on_toggle()))
        self.on_toggle()
        root.after(50, self.tick)

    def on_toggle(self):
        cb = self.checks['outline only']
        if self.v_ranges.get():
            cb.pack(side='left', padx=4, after=self.checks['Ranges'])
        else:
            cb.pack_forget()
        self.cb_mode.config(state='readonly' if self.v_walls.get()
                            else 'disabled')
        self.redraw()

    def wall_mode(self):
        lab = self.wall_mode_label.get()
        for k, l in WALL_MODES:
            if l == lab:
                return k
        return 'solid'

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
                if isinstance(item, dict) and item.get('type') == 'layout':
                    self.layout = item
                    self.pic_src, self.pic_tk = {}, {}
                    if not self.user_view:
                        self.fitted = False
                else:
                    self.data = item
                changed = True
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
        return any(n['xyz'] is not None for n in self.data['nodes']) or \
            bool(self.layout.get('walls') or self.layout.get('pictures'))

    def walls(self):
        return self.layout.get('walls') or []

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
        ranges = {n['name']: n['range'] for n in self.data['nodes']}
        xs_lo = [p[0] - ranges.get(k, 0) for k, p in pos.items()]
        xs_hi = [p[0] + ranges.get(k, 0) for k, p in pos.items()]
        ys_lo = [p[1] - ranges.get(k, 0) for k, p in pos.items()]
        ys_hi = [p[1] + ranges.get(k, 0) for k, p in pos.items()]
        for w in self.walls():
            for x, y in wall_corners(w):
                xs_lo.append(x); xs_hi.append(x)
                ys_lo.append(y); ys_hi.append(y)
        for p in self.layout.get('pictures') or []:
            if p.get('visible', True):
                for x, y, _ in self.pic_corners(p):
                    xs_lo.append(x); xs_hi.append(x)
                    ys_lo.append(y); ys_hi.append(y)
        if not xs_lo:
            return 0, 0, 100, 100
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
        if not hasattr(self, 'canvas'):
            return
        cv = self.canvas
        cv.delete('all')
        t = time.time() - self.t0
        anim = self.v_anim.get()
        pos = self.positions()
        nodes = {n['name']: n for n in self.data['nodes']}

        if self.v_pics.get():                      # pictures under all
            for i, pic in enumerate(self.layout.get('pictures') or []):
                self.draw_picture(i, pic)
        self.draw_grid()

        # ranges: spheres or flat circles, depending on the toggle
        if self.v_ranges.get():
            sphere = self.v_sphere.get()
            outline = self.v_outline.get()
            for name, p in pos.items():
                n = nodes[name]
                if n['range'] <= 0:
                    continue
                is_ap = n['kind'] == 'ap'
                col = C['ap'] if is_ap else C.get(n['kind'], C['sta'])
                if sphere:
                    if is_ap and outline:
                        self.sphere(p, n['range'], col, rings=(0,),
                                    meridians=0)
                    elif is_ap:
                        self.sphere(p, n['range'], col, fill=C['ap_fill'])
                    else:
                        self.sphere(p, n['range'], col, width=1.2,
                                    dash=(4, 4), rings=(0,), meridians=1)
                else:
                    pts = self.circle_pts(*p, n['range'])
                    if is_ap:
                        if not outline:
                            cv.create_polygon(pts, fill=C['ap_fill'],
                                              outline='')
                        cv.create_polygon(pts, fill='', outline=col, width=1.5)
                    else:
                        cv.create_polygon(pts, fill='', outline=col,
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

        # walls: flat ones drawn now, 3D faces depth-sorted with the nodes
        wall_items = self.wall_items(pos)

        # height stems + ground shadows (helps reading 3D)
        for name, p in pos.items():
            if abs(p[2]) > 1e-6:
                gx, gy, _ = self.project(p[0], p[1], 0)
                sx, sy, _ = self.project(*p)
                cv.create_oval(gx - 7, gy - 3, gx + 7, gy + 3,
                               fill=C['shadow'], outline='')
                cv.create_line(gx, gy, sx, sy, fill=C['axis'], dash=(2, 3))

        # links
        labels = self.v_labels.get()
        for link in self.data['links']:
            a, b, kind = link[0], link[1], link[2]
            if a in pos and b in pos:
                x1, y1, _ = self.project(*pos[a])
                x2, y2, _ = self.project(*pos[b])
                if kind == 'wired':
                    cv.create_line(x1, y1, x2, y2, fill=C['wired'], width=3)
                elif kind in ('adhoc', 'mesh'):
                    cv.create_line(x1, y1, x2, y2, fill=C['peer'], width=2.5,
                                   dash=(6, 4))
                    if labels:
                        cv.create_text((x1 + x2) / 2, (y1 + y2) / 2 - 9,
                                       text='%s %s' % (kind, link[3] if
                                                       len(link) > 3 else ''),
                                       fill=C['peer'],
                                       font=('TkDefaultFont', 8, 'bold'))
                else:
                    cv.create_line(x1, y1, x2, y2, fill=C['control'], width=2,
                                   dash=(8, 5))
                    if labels:
                        cv.create_text((x1 + x2) / 2, (y1 + y2) / 2 - 8,
                                       text='OpenFlow', fill=C['control'],
                                       font=('TkDefaultFont', 8))
        for name, n in nodes.items():
            if n['kind'] in CLIENT_KINDS and n['ap'] in pos and name in pos:
                d = math.dist(pos[name][:3], pos[n['ap']][:3])
                ok = d <= max(nodes[n['ap']]['range'], 1e-9)
                wl, hit = wall_loss(self.walls(), pos[name], pos[n['ap']])
                self.draw_rf(pos[name], pos[n['ap']], t if anim else 0, ok,
                             n.get('rssi'), len(hit), wl)

        # nodes and 3D wall faces, far ones first (painter's algorithm)
        self.hits = []
        items = list(wall_items)
        for name, p in pos.items():
            sx, sy, dd = self.project(*p)
            items.append((dd, 'node', name, sx, sy))
        items.sort(key=lambda it: -it[0])
        for it in items:
            if it[1] == 'node':
                self.draw_node(nodes[it[2]], it[3], it[4])
            else:
                it[2]()
        for fn in self._wall_late:                  # labels, ghost tops
            fn()

        self.draw_overlay()
        self.update_status(pos, nodes)

    # ------------------------------------------------------------ walls ----
    def view_grad(self):
        sy, cy = math.sin(self.yaw), math.cos(self.yaw)
        sp, cp = math.sin(self.pitch), math.cos(self.pitch)
        return (sy * sp, cy * sp, -cp)

    @staticmethod
    def box_faces(cs, z0, z1, rot):
        a = math.radians(rot)
        ux, uy = math.cos(a), math.sin(a)
        px, py = -uy, ux
        c0, c1, c2, c3 = cs
        return [
            ([c + (z1,) for c in cs], (0, 0, 1), 'top'),
            ([c + (z0,) for c in reversed(cs)], (0, 0, -1), 'bottom'),
            ([c0 + (z0,), c1 + (z0,), c1 + (z1,), c0 + (z1,)], (-px, -py, 0),
             'side'),
            ([c1 + (z0,), c2 + (z0,), c2 + (z1,), c1 + (z1,)], (ux, uy, 0),
             'side'),
            ([c2 + (z0,), c3 + (z0,), c3 + (z1,), c2 + (z1,)], (px, py, 0),
             'side'),
            ([c3 + (z0,), c0 + (z0,), c0 + (z1,), c3 + (z1,)], (-ux, -uy, 0),
             'side'),
        ]

    def wall_items(self, pos):
        """Draws flat walls now; returns depth-sorted items for 3D faces.
        Camera modes: solid, fade (walls in front of the camera become
        see-through), cutaway (lowered to a stub), outline."""
        cv = self.canvas
        self._wall_late = []
        items = []
        walls = self.walls()
        if not self.v_walls.get() or not walls:
            return items
        mode = self.wall_mode()
        g = self.view_grad()
        tdepth = self.project(*self.target)[2]
        light = (-0.40, -0.55, 0.73)
        labels = self.v_labels.get()
        node_scr = [self.project(*p) for p in pos.values()]
        tilted = self.pitch > math.radians(8)
        for w in walls:
            col = _safe_color(w.get('color'))
            dark = _mix(col, '#000000', 0.4)
            glass = w.get('material') == 'glass'
            h, z0 = float(w['height']), float(w['z'])
            cs = wall_corners(w)
            if h <= 0:                                   # flat 2D wall
                pts = [self.project(x, y, z0)[:2] for x, y in cs]
                cv.create_polygon([v for q in pts for v in q],
                                  fill='' if mode == 'outline' else col,
                                  stipple='gray50', outline=dark, width=1.2)
                if labels:
                    cx = sum(q[0] for q in pts) / 4
                    cy = sum(q[1] for q in pts) / 4
                    self._wall_late.append(lambda cx=cx, cy=cy, w=w, d=dark:
                                           self.wall_label(w, cx, cy, d))
                continue

            def visible(f):
                n = f[1]
                return n[0] * g[0] + n[1] * g[1] + n[2] * g[2] < -1e-6
            faces = [f for f in self.box_faces(cs, z0, z0 + h, w['rotation'])
                     if visible(f)]
            scr = [[self.project(*c) for c in f[0]] for f in faces]
            front = False
            if mode in ('fade', 'cutaway') and tilted:
                cdepth = self.project(w['x'], w['y'], z0 + h / 2)[2]
                if cdepth < tdepth - 0.5:
                    front = True
                else:
                    polys = [[q[:2] for q in sp] for sp in scr]
                    for sx, sy, dd in node_scr:
                        if dd > cdepth and any(_in_poly(sx, sy, pp)
                                               for pp in polys):
                            front = True
                            break
            top_h = h
            if front and mode == 'cutaway':
                top_h = min(h, 0.35)
                faces = [f for f in self.box_faces(cs, z0, z0 + top_h,
                                                   w['rotation']) if visible(f)]
                scr = [[self.project(*c) for c in f[0]] for f in faces]
                ghost = [self.project(x, y, z0 + h)[:2] for x, y in cs]
                self._wall_late.append(
                    lambda gp=ghost, d=dark: self.canvas.create_polygon(
                        [v for q in gp for v in q], fill='', outline=d,
                        dash=(2, 4)))
            for f, sp in zip(faces, scr):
                n = f[1]
                if f[2] == 'top':
                    fill = _mix(col, '#ffffff', 0.2)
                else:
                    k = 0.62 + 0.38 * max(0.0, n[0] * light[0] +
                                          n[1] * light[1] + n[2] * light[2])
                    fill = _mix('#000000', col, k)
                stipple, dash = '', None
                if mode == 'outline':
                    fill = ''
                elif front and mode == 'fade':
                    stipple, dash = 'gray12', (3, 3)
                elif glass:
                    stipple = 'gray50'
                flat = [v for q in sp for v in q[:2]]
                depth = sum(q[2] for q in sp) / len(sp)

                def draw(flat=flat, fill=fill, stipple=stipple, dash=dash,
                         dark=dark):
                    kw = {'stipple': stipple} if stipple and fill else {}
                    self.canvas.create_polygon(flat, fill=fill, outline=dark,
                                               width=1, dash=dash, **kw)
                items.append((depth, 'face', draw))
            if labels:
                lx, ly, _ = self.project(w['x'], w['y'], z0 + top_h)
                self._wall_label_item(w, lx, ly - 9, dark)
        return items

    def _wall_label_item(self, w, x, y, col):
        self._wall_late.append(lambda: self.wall_label(w, x, y, col))

    def wall_label(self, w, x, y, col):
        self.canvas.create_text(
            x, y, text='%s · %s %s dB' % (w['name'], w['material'],
                                          '%g' % round(wall_db(w), 1)),
            fill=col, font=('TkDefaultFont', 8, 'bold'))

    # --------------------------------------------------------- pictures ----
    def pic_source(self, pic, reduce=1):
        key = (pic.get('path'), reduce)
        if key in self.pic_src:
            return self.pic_src[key]
        img = None
        try:
            from PIL import Image
            if reduce == 1:
                im = Image.open(pic['path'])
                im.load()
                im = im.convert('RGBA')
                m = max(im.size)
                if m > 2048:
                    k = 2048.0 / m
                    im = im.resize((max(1, int(im.size[0] * k)),
                                    max(1, int(im.size[1] * k))),
                                   Image.LANCZOS)
                img = im
            else:
                base = self.pic_source(pic, 1)
                img = base.reduce(reduce) if base is not None else None
        except Exception:
            img = None
        self.pic_src[key] = img
        return img

    def pic_corners(self, pic):
        src = self.pic_source(pic)
        aspect = src.size[1] / float(src.size[0]) if src is not None else 0.75
        W = float(pic['width'])
        H = W * aspect
        a = math.radians(float(pic['rotation']))
        ca, sa = math.cos(a), math.sin(a)
        return [(pic['x'] + lx * ca - ly * sa, pic['y'] + lx * sa + ly * ca,
                 pic['z'])
                for lx, ly in ((-W / 2, H / 2), (W / 2, H / 2),
                               (W / 2, -H / 2), (-W / 2, -H / 2))]

    def draw_picture(self, idx, pic):
        """Picture lying on the plane z = pic['z'], mapped with one affine
        transform (orthographic view) and cached until the view changes."""
        if not pic.get('visible', True):
            return
        cv = self.canvas
        corners = [self.project(*c)[:2] for c in self.pic_corners(pic)]
        src = self.pic_source(pic)
        if src is None:
            cv.create_polygon([v for q in corners for v in q], fill='',
                              outline=C['axis'], dash=(4, 3))
            return
        try:
            from PIL import Image
        except ImportError:
            return
        (p0x, p0y), (p1x, p1y), _, (p3x, p3y) = corners
        ratio = src.size[0] / max(math.hypot(p1x - p0x, p1y - p0y), 1)
        k = 1
        while k < 8 and ratio / (k * 2) >= 1.5:
            k *= 2
        if k > 1:
            src = self.pic_source(pic, k) or src
        iw, ih = src.size
        ax, ay = (p1x - p0x) / iw, (p1y - p0y) / iw
        bx, by = (p3x - p0x) / ih, (p3y - p0y) / ih
        det = ax * by - bx * ay
        if abs(det) < 1e-9:
            return
        W, H = self.size()
        xs, ys = [p[0] for p in corners], [p[1] for p in corners]
        x0, y0 = max(int(math.floor(min(xs))), 0), max(int(math.floor(min(ys))), 0)
        x1, y1 = min(int(math.ceil(max(xs))), W), min(int(math.ceil(max(ys))), H)
        if x1 - x0 < 1 or y1 - y0 < 1:
            return
        dx, dy = x0 - p0x, y0 - p0y
        coef = (by / det, -bx / det, (by * dx - bx * dy) / det,
                -ay / det, ax / det, (-ay * dx + ax * dy) / det)
        op = max(0, min(100, int(round(float(pic.get('opacity', 100))))))
        key = (k, x1 - x0, y1 - y0, op, tuple(round(c, 6) for c in coef))
        cached = self.pic_tk.get(idx)
        if cached and cached[0] == key:
            img = cached[1]
        else:
            out = src.transform((x1 - x0, y1 - y0), Image.AFFINE, coef,
                                resample=Image.BILINEAR)
            if op < 100:
                out.putalpha(out.getchannel('A').point(
                    lambda a: a * op // 100))
            try:
                from PIL import ImageTk
                img = ImageTk.PhotoImage(out, master=self.root)
            except ImportError:
                import base64
                import io
                buf = io.BytesIO()
                out.save(buf, 'PNG', compress_level=1)
                img = self.tk.PhotoImage(
                    master=self.root,
                    data=base64.b64encode(buf.getvalue()).decode('ascii'))
            self.pic_tk[idx] = (key, img)
        cv.create_image(x0, y0, image=img, anchor='nw')

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

    def draw_rf(self, a, b, t, ok, rssi=None, nwalls=0, wl=0.0):
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
        wl_, amp, speed = 16.0, 4.5, 40.0
        pts = []
        steps = max(int(L / 2.5), 8)
        for i in range(steps + 1):
            d = L * i / steps
            env = min(1.0, d / 12.0, (L - d) / 12.0)      # taper the ends
            off = amp * env * math.sin(2 * math.pi * (d - speed * t) / wl_)
            pts += [x1 + ux * d + nx * off, y1 + uy * d + ny * off]
        cv.create_line(*pts, fill=color, width=3, smooth=True)
        walls_txt = ('  %d wall%s -%g dB' % (nwalls, '' if nwalls == 1 else 's',
                                             round(wl, 1))) if nwalls else ''
        if rssi is not None:
            text = '%.1f dBm%s' % (rssi, walls_txt)
            cv.create_text((x1 + x2) / 2, (y1 + y2) / 2 - 15, text=text,
                           fill=color, font=('TkDefaultFont', 10, 'bold'))
        elif not ok:
            cv.create_text((x1 + x2) / 2, (y1 + y2) / 2 - 12,
                           text='OUT OF RANGE' + walls_txt, fill='#cc3a2f',
                           font=('TkDefaultFont', 8, 'bold'))
        elif walls_txt:
            cv.create_text((x1 + x2) / 2, (y1 + y2) / 2 - 12,
                           text=walls_txt.strip(), fill=color,
                           font=('TkDefaultFont', 8, 'bold'))

    def draw_icon(self, kind, x, y, ow='white', s=1.0):
        cv, k = self.canvas, s
        if kind == 'ap':
            cv.create_rectangle(x - 10 * k, y - 7 * k, x + 10 * k, y + 7 * k,
                                fill=C['ap'], outline=ow, width=2)
            cv.create_line(x + 6 * k, y - 7 * k, x + 6 * k, y - 15 * k,
                           fill=C['ap'], width=2)
            for r in (5 * k, 9 * k):                     # wifi arcs
                cv.create_arc(x + 6 * k - r, y - 15 * k - r, x + 6 * k + r,
                              y - 15 * k + r, start=45, extent=90,
                              style='arc', outline=C['ap'], width=1.5)
            for i in (-5, 0):                            # leds
                cv.create_oval(x + i * k - 1.5, y - 1.5, x + i * k + 1.5,
                               y + 1.5, fill='#9ff0b8', outline='')
        elif kind == 'sta':
            cv.create_oval(x - 8 * k, y - 8 * k, x + 8 * k, y + 8 * k,
                           fill=C['sta'], outline=ow, width=2)
            cv.create_oval(x - 2.5 * k, y - 2.5 * k, x + 2.5 * k, y + 2.5 * k,
                           fill='white', outline='')
        elif kind == 'car':
            cv.create_polygon(x - 7 * k, y - 4 * k, x - 3 * k, y - 10 * k,
                              x + 5 * k, y - 10 * k, x + 8 * k, y - 4 * k,
                              fill=C['car'], outline=ow, width=1.5)
            cv.create_rectangle(x - 1 * k, y - 8.5 * k, x + 4.5 * k,
                                y - 5 * k, fill='#f3e3d8', outline='')
            cv.create_rectangle(x - 12 * k, y - 4 * k, x + 12 * k, y + 4 * k,
                                fill=C['car'], outline=ow, width=2)
            for wx in (-6, 6):
                cv.create_oval(x + (wx - 3.5) * k, y + 0.5 * k,
                               x + (wx + 3.5) * k, y + 7.5 * k,
                               fill=C['text'], outline=ow, width=1)
        elif kind == 'ctrl':
            pts = []
            for i in range(6):
                a = math.pi / 6 + i * math.pi / 3
                pts += [x + 12 * k * math.cos(a), y + 12 * k * math.sin(a)]
            cv.create_polygon(pts, fill=C['ctrl'], outline=ow, width=2)
            cv.create_text(x, y, text='C', fill='white',
                           font=('TkDefaultFont', max(int(9 * k), 6), 'bold'))
        elif kind == 'switch':
            cv.create_rectangle(x - 12 * k, y - 6 * k, x + 12 * k, y + 6 * k,
                                fill=C['switch'], outline=ow, width=2)
            cv.create_line(x - 7 * k, y - 2 * k, x + 7 * k, y - 2 * k,
                           fill='white', arrow='last', arrowshape=(3, 4, 2))
            cv.create_line(x + 7 * k, y + 2 * k, x - 7 * k, y + 2 * k,
                           fill='white', arrow='last', arrowshape=(3, 4, 2))
        else:                                            # host
            cv.create_rectangle(x - 8 * k, y - 8 * k, x + 8 * k, y + 8 * k,
                                fill=C['host'], outline=ow, width=2)
            cv.create_rectangle(x - 4.5 * k, y - 4.5 * k, x + 4.5 * k,
                                y + 1 * k, fill='#c4c6cb', outline='')

    def draw_node(self, n, x, y):
        cv = self.canvas
        kind, name = n['kind'], n['name']
        hl = (self.hover == name) or (self.drag and self.drag.get('name') == name)
        self.draw_icon(kind, x, y, ow=C['select'] if hl else 'white')
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
        x, y = 12, 16
        for kind, text in (('ap', 'Access point'), ('sta', 'Station'),
                           ('car', 'Car'), ('host', 'Host'),
                           ('switch', 'Switch'), ('ctrl', 'Controller')):
            self.draw_icon(kind, x + 7, y + (3 if kind == 'ap' else 0),
                           s=0.55)
            cv.create_text(x + 18, y, text=text, anchor='w', fill=C['text'],
                           font=('TkDefaultFont', 9))
            x += 26 + 7 * len(text)
        for kind, text, dash, col, wd in (
                ('rf', 'Wireless', None, C['rf'], 2),
                ('peer', 'Ad-hoc / mesh', (6, 4), C['peer'], 2),
                ('wired', 'Cable', None, C['wired'], 3),
                ('control', 'Control', (6, 4), C['control'], 2),
                ('wall', 'Wall', None, C['wall'], 6)):
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
                            'T/3/F: top/3D/fit   W/P: walls/pictures')
        # tooltip
        if self.hover and not (self.drag and self.drag.get('mode') == 'rotate'):
            self.draw_tip()

    def draw_tip(self):
        cv = self.canvas
        n = next((n for n in self.data['nodes'] if n['name'] == self.hover), None)
        pos = self.positions()
        p = pos.get(self.hover)
        if not n or not p:
            return
        lines = ['%s  (%s)' % (n['name'], KIND_LABEL.get(n['kind'], n['kind'])),
                 'position: %.1f, %.1f, %.1f' % p]
        if n['range'] > 0:
            lines.append('range: %g m' % n['range'])
        if n['kind'] in CLIENT_KINDS:
            lines.append('connected to: %s' % (n['ap'] or 'nothing'))
            rssi = n.get('rssi')
            lines.append('RSSI: %.1f dBm' % rssi if rssi is not None
                         else 'RSSI: unavailable')
            if n['ap'] in pos:
                wl, hit = wall_loss(self.walls(), p, pos[n['ap']])
                if hit:
                    lines.append('walls to %s: %s  (-%g dB)' % (
                        n['ap'], ', '.join(w['name'] for w in hit),
                        round(wl, 1)))
        if n['kind'] == 'ap':
            clients = [m['name'] for m in self.data['nodes']
                       if m.get('ap') == n['name']]
            lines.append('connected devices: %d%s' % (
                len(clients), ('  (%s)' % ', '.join(clients)) if clients else ''))
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
        for wtxt in self.layout.get('warnings') or []:
            warn = True
            lines.append('!  ' + wtxt)
        for n in self.data['nodes']:
            if n['name'] not in pos:
                warn = True
                lines.append('!  %s has no position, so it is not drawn  '
                             '(add position="x,y,z")' % n['name'])
            elif n['kind'] in CLIENT_KINDS:
                ap = n['ap']
                if ap and ap in pos:
                    d = math.dist(pos[n['name']], pos[ap])
                    rng = nodes[ap]['range']
                    state = 'in range' if d <= rng else 'OUT OF RANGE'
                    wl, hit = wall_loss(self.walls(), pos[n['name']], pos[ap])
                    lines.append('%s  ->  %s    %.0f m   (%s, AP range %g m)%s'
                                 % (n['name'], ap, d, state, rng,
                                    ('   through %s (-%g dB)' % (
                                        ', '.join(w['name'] for w in hit),
                                        round(wl, 1))) if hit else ''))
                else:
                    lines.append('%s  ->  not connected' % n['name'])
        walls, pics = self.walls(), self.layout.get('pictures') or []
        if walls or pics:
            src = os.path.basename(self.layout.get('source') or '') or 'layout'
            lines.append('%s: %d wall%s, %d picture%s' % (
                src, len(walls), '' if len(walls) == 1 else 's', len(pics),
                '' if len(pics) == 1 else 's'))
        if not self.data['nodes']:
            lines.append('Waiting for network data...')
        text = '\n'.join(lines[:12]) or ' '
        if text != self.status.cget('text'):
            self.status.config(text=text, fg=C['warn'] if warn else C['text'])

    def run(self):
        self.root.mainloop()


def _window_main(inq, outq, cfg):
    signal.signal(signal.SIGINT, signal.SIG_IGN)    # Ctrl+C belongs to the CLI
    try:
        win = _Window(inq, outq, cfg)
    except Exception as e:
        print('\n*** custom_gui: could not open the window (%s)\n'
              '*** Try: sudo -E python3 your_script.py\n'
              '*** or run "xhost +local:root" first.\n' % e)
        return
    win.run()