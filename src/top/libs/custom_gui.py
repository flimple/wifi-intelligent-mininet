#!/usr/bin/env python3
"""
custom_gui.py  -  interactive 3D viewer for Mininet-WiFi  (version 4)

No matplotlib. Pure Tkinter (comes with Python: sudo apt install python3-tk).
Pictures need Pillow (sudo apt install python3-pil python3-pil.imagetk).

What you get
  - 3D view you can rotate, pan and zoom, plus a flat top view
  - Word-like ribbon (View / Show / Packets / Tools), collapsible
    (button on the right, double-click a tab, or Ctrl+F1), dark mode
  - Drag any device to move it, and Mininet-WiFi moves it too (Shift +
    drag: height). Devices can never be dragged inside a wall
  - Every device of the editor: access points, stations, cars, hosts,
    switches, controllers; AP badges with the number of connected devices
  - Wireless connections as animated radio waves (colour = RSSI simulated
    by Mininet-WiFi), ad-hoc / mesh groups, cables, control links
  - Live packets: dots travel along the links at the rate the interfaces
    really send (read from /proc, no shell commands), with speed
    (real time ... 0.05x, paused) and multiplier settings
  - Walls and pictures from a custom_edit.py file (camera modes, picture
    adjustments). Locked walls / pictures are fixed; unlocked ones can be
    dragged (moving a wall updates the simulated wall losses)
  - Tooltips: basic or advanced (everything the editor shows: radio,
    addressing, walls on the path, traffic...), and wall tooltips with
    the connections that cross each wall
  - Devices list in a git-graph style: go to a device, see its details,
    change the AP of a station, open its terminal (xterm)
  - Live config (Tools tab / devices list): change range, tx power,
    antenna, channel, mode, IP, position and tc (bandwidth, delay, jitter,
    loss) of a running device, for testing - nothing is saved
  - CLI terminal (Tools tab, C): a Mininet CLI inside the viewer; commands
    run in the Mininet process and their output is shown in the window
  - Show / hide the links (Show tab); options are kept in
    profile/gui.ini next to this file; the camera starts where the editor
    left it (topology 'view'), View > Save view writes it back to the file

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

  WifiGUI(net, theme=None)   theme 'light' / 'dark' (None = last used)

Mouse & keys
  drag empty space ......... rotate          right-drag ....... pan
  mouse wheel .............. zoom            double-click ..... fit to screen
  drag a device ............ move it         Shift + drag ..... change height
  drag an unlocked wall / picture .......... move it
  keys: T = top view, 3 = 3D view, F = fit, A = animation on/off,
        S = sphere on/off, W = walls on/off, P = pictures on/off,
        K = packets on/off, D = devices list, C = CLI terminal,
        Ctrl+F1 = ribbon

Terminals and "change AP" (devices list) run in your Mininet process:
use them while the CLI is waiting at its prompt.

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


def wall_contains_point(w, p, eps=1e-6):
    """True when point p (x, y, z) is inside the wall's volume
    (flat 2D walls: inside the footprint, any height)."""
    a = math.radians(float(w['rotation']))
    dx, dy = p[0] - w['x'], p[1] - w['y']
    lx = dx * math.cos(a) + dy * math.sin(a)
    ly = -dx * math.sin(a) + dy * math.cos(a)
    if abs(lx) >= float(w['length']) / 2 - eps or \
            abs(ly) >= float(w['thickness']) / 2 - eps:
        return False
    h = float(w.get('height') or 0)
    if h <= 0:
        return True
    z0 = float(w.get('z') or 0)
    return z0 - eps <= p[2] <= z0 + h + eps


def wall_zrange(w):
    h = float(w.get('height') or 0)
    if h <= 0:
        return (-1e9, 1e9)
    z = float(w.get('z') or 0)
    return (z, z + h)


def _poly_overlap(A, B, eps=0.005):
    """Convex polygons overlap by more than eps (touching is fine)."""
    for P in (A, B):
        for i in range(len(P)):
            x1, y1 = P[i]
            x2, y2 = P[(i + 1) % len(P)]
            nx, ny = y1 - y2, x2 - x1
            L = math.hypot(nx, ny)
            if L < 1e-12:
                continue
            nx, ny = nx / L, ny / L
            pa = [x * nx + y * ny for x, y in A]
            pb = [x * nx + y * ny for x, y in B]
            if min(max(pa), max(pb)) - max(min(pa), min(pb)) <= eps:
                return False
    return True


def walls_overlap(w1, w2, eps=0.005):
    za, zb = wall_zrange(w1), wall_zrange(w2)
    if min(za[1], zb[1]) - max(za[0], zb[0]) <= eps:
        return False
    return _poly_overlap(wall_corners(w1), wall_corners(w2), eps)


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
                 'rotation': 0.0, 'opacity': 100.0, 'visible': True,
                 'locked': False, 'crop_l': 0.0, 'crop_t': 0.0,
                 'crop_r': 0.0, 'crop_b': 0.0, 'brightness': 100.0,
                 'contrast': 100.0, 'saturation': 100.0, 'grayscale': False,
                 'invert': False, 'flip_h': False, 'flip_v': False,
                 'rot90': '0', 'key_white': False, 'key_level': 235.0}
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
        'locked_devices': [...], 'source': path}"""
    with open(path) as fh:
        d = json.load(fh)
    base = os.path.dirname(os.path.abspath(path))
    devices, locked = {}, []
    if d.get('schema') == 'custom':
        walls = d.get('walls') or []
        pics = d.get('pictures') or []
        for dev in d.get('devices') or []:
            devices[dev['name']] = (_KIND_OF_NAME.get(dev.get('kind'),
                                                      dev.get('kind')),
                                    _xyz_of(dev.get('position')))
            if dev.get('locked'):
                locked.append(dev['name'])
    else:
        walls = d.get('walls') or []
        pics = (d.get('editor') or {}).get('pictures') or []
        for key, kind in _KIND_OF_LIST.items():
            for e in d.get(key) or []:
                devices[e['name']] = (kind, _xyz_of(
                    (e.get('params') or {}).get('position')))
                if e.get('locked'):
                    locked.append(e['name'])
    return {'walls': [_norm_wall(w) for w in walls],
            'pictures': [_norm_pic(p, base) for p in pics],
            'devices': devices, 'locked_devices': locked,
            'view': d.get('view') or {},
            'source': os.path.abspath(path)}


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


def _plain(v):
    """Something safe to send to the window process."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return str(v)


def _info(node, kind):
    """Static details for the advanced tooltip."""
    d = {}
    for k in ('failMode', 'dpid', 'protocols'):
        v = getattr(node, k, None) or getattr(node, 'params', {}).get(k)
        if v:
            d[k] = _plain(v)
    if kind == 'ctrl':
        for k in ('ip', 'port', 'protocol'):
            if getattr(node, k, None) is not None:
                d[k] = _plain(getattr(node, k))
        d['class'] = type(node).__name__
    intfs = []
    for intf in getattr(node, 'wintfs', {}).values():
        e = {'name': getattr(intf, 'name', '?')}
        for k in ('mode', 'ssid', 'channel', 'freq', 'txpower',
                  'antennaGain', 'antennaHeight', 'range', 'ip', 'mac'):
            v = getattr(intf, k, None)
            if v not in (None, ''):
                e[k] = _plain(v)
        a = getattr(intf, 'associatedTo', None)
        if isinstance(a, str):
            e['assoc'] = a
        elif a is not None and getattr(a, 'node', None) is not None:
            e['assoc'] = a.node.name
        pm = _peer_mode(intf)
        if pm:
            e['peer'] = pm
        intfs.append(e)
    d['wintfs'] = intfs
    wired = []
    try:
        for intf in node.intfList():
            n = str(intf.name)
            if 'wlan' in n or n == 'lo':
                continue
            wired.append({'name': n, 'ip': _plain(getattr(intf, 'ip', None)),
                          'mac': _plain(getattr(intf, 'mac', None))})
    except Exception:
        pass
    d['intfs'] = wired
    if kind in ('host', 'sta', 'car'):
        try:
            d['ip'] = _plain(node.IP())
        except Exception:
            pass
    d['class'] = d.get('class') or type(node).__name__
    return d


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
                    'rssi': _rssi(node) if client else None,
                    'info': _info(node, kind)}
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
    for (mode, ssid), names in _peer_groups(by_name).items():
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                links.append((names[i][0], names[j][0], mode, ssid))

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


def _peer_groups(by_name):
    """(mode, ssid) -> [(node name, interface)] for ad-hoc / mesh."""
    peers = {}
    for name, node in by_name.items():
        for intf in getattr(node, 'wintfs', {}).values():
            mode = _peer_mode(intf)
            if mode:
                key = (mode, str(getattr(intf, 'ssid', None) or ''))
                peers.setdefault(key, []).append((name, intf))
    return peers


def _read_netdev(path):
    """iface -> (rx_bytes, rx_packets, tx_bytes, tx_packets)."""
    out = {}
    try:
        with open(path) as fh:
            for line in fh.readlines()[2:]:
                if ':' not in line:
                    continue
                name, rest = line.split(':', 1)
                f = rest.split()
                out[name.strip()] = (int(f[0]), int(f[1]), int(f[8]),
                                     int(f[9]))
    except (OSError, ValueError, IndexError):
        pass
    return out


class TrafficMeter(object):
    """Packet / byte rates on every link, from the interface counters of
    each node's network namespace (/proc/<pid>/net/dev). No shell command
    is sent to the nodes, so it never disturbs the CLI."""

    def __init__(self, net):
        self.net = net
        self.prev = {}
        self.t = None

    def _stats(self, node, cache):
        pid = getattr(node, 'pid', None)
        path = '/proc/%d/net/dev' % pid if pid and \
            getattr(node, 'inNamespace', True) else '/proc/net/dev'
        if path not in cache:
            cache[path] = _read_netdev(path)
        return cache[path]

    def rates(self):
        """[(src, dst, kind, packets/s, bytes/s)] for active links."""
        now = time.time()
        dt = (now - self.t) if self.t else None
        self.t = now
        cache, flows, cur = {}, {}, {}

        def delta(key, node, ifname):
            c = self._stats(node, cache).get(ifname)
            if c is None:
                return None
            cur[key] = c
            p = self.prev.get(key)
            if p is None or not dt or dt <= 0:
                return None
            return tuple(max(c[i] - p[i], 0) / dt for i in range(4))

        def flow(a, b, kind, pk, by):
            if pk > 0:
                f = flows.setdefault((a, b, kind), [0.0, 0.0])
                f[0] += pk
                f[1] += by

        by_name = {}
        for attr in ('aps', 'stations', 'cars', 'hosts', 'switches'):
            for node in getattr(self.net, attr, None) or []:
                by_name[node.name] = node
        aps = set(n.name for n in getattr(self.net, 'aps', None) or [])
        try:
            for node in list(by_name.values()):            # Wi-Fi clients
                if node.name in aps:
                    continue
                for intf in getattr(node, 'wintfs', {}).values():
                    ap = getattr(intf, 'associatedTo', None)
                    apn = None if isinstance(ap, str) else \
                        getattr(ap, 'node', None)
                    if apn is None:
                        continue
                    d = delta((node.name, intf.name), node, intf.name)
                    if d:
                        flow(node.name, apn.name, 'wifi', d[3], d[2])
                        flow(apn.name, node.name, 'wifi', d[1], d[0])
            for link in getattr(self.net, 'links', None) or []:  # cables
                try:
                    i1, i2 = link.intf1, link.intf2
                    n1, n2 = i1.node, i2.node
                except AttributeError:
                    continue
                names = (str(i1.name) + str(i2.name)).lower()
                cls = type(link).__name__.lower()
                if any(w in cls for w in WIRELESS_LINK_WORDS) or \
                        'wlan' in names:
                    continue
                d = delta((n1.name, str(i1.name)), n1, str(i1.name))
                if d:
                    flow(n1.name, n2.name, 'wired', d[3], d[2])
                    flow(n2.name, n1.name, 'wired', d[1], d[0])
            for (mode, ssid), members in _peer_groups(by_name).items():
                for name, intf in members:                 # ad-hoc / mesh
                    others = [o for o, _ in members if o != name]
                    d = delta((name, intf.name), by_name[name], intf.name)
                    if d and others:
                        for o in others:
                            flow(name, o, mode, d[3] / len(others),
                                 d[2] / len(others))
        except Exception:
            pass
        self.prev = cur
        return [(a, b, kind, round(v[0], 2), round(v[1], 1))
                for (a, b, kind), v in sorted(flows.items())]


class WifiGUI(object):
    """Interactive window showing the Mininet-WiFi network."""

    def __init__(self, net, refresh=0.3, size=(1100, 780),
                 title='Mininet-WiFi 3D View', allow_move=True, theme=None):
        self.net = net
        self.refresh = refresh
        self.allow_move = allow_move
        self.cfg = {'size': size, 'title': title, 'allow_move': allow_move,
                    'theme': theme}
        self.overrides = {}          # positions for nodes without setPosition
        self.fallback = {}           # positions from the editor's file
        self.layout = None           # walls + pictures
        self.meter = TrafficMeter(net)
        self._cli, self._cli_thread = None, None
        self._cli_q = queue.Queue()
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
                       'view': layout.get('view') or {},
                       'locked_devices': list(layout.get('locked_devices')
                                              or []),
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
        self._to_win.put(self._data())
        self._proc = ctx.Process(target=_window_main,
                                 args=(self._to_win, self._from_win, self.cfg),
                                 daemon=True)
        self._proc.start()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _data(self):
        data = snapshot(self.net, self.overrides, self.fallback)
        try:
            data['traffic'] = self.meter.rates()
        except Exception:
            data['traffic'] = []
        return data

    def _notice(self, text):
        print('\n*** custom_gui: %s' % text)
        try:
            self._to_win.put({'type': 'notice', 'text': text})
        except Exception:
            pass

    def _node(self, name):
        return getattr(self.net, 'nameToNode', {}).get(name)

    def _apply_move(self, name, x, y, z):
        node = self._node(name)
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

    def _term(self, name):
        node = self._node(name)
        if node is None:
            return self._notice('no device called %s' % name)
        try:
            from mininet.term import makeTerm
            makeTerm(node, title='%s - custom_gui' % name)
            self._notice('terminal opened for %s' % name)
        except Exception as e:
            self._notice('could not open a terminal for %s (%s) - is xterm '
                         'installed?' % (name, e))

    def _assoc(self, sta, ap):
        node, apn = self._node(sta), self._node(ap)
        if node is None or apn is None:
            return self._notice('unknown device %s / %s' % (sta, ap))
        try:
            intf = list(node.wintfs.values())[0]
        except Exception:
            return self._notice('%s has no wireless interface' % sta)
        try:
            node.setAssociation(apn, intf=intf.name)
            self._notice('%s associated with %s' % (sta, ap))
        except Exception as e:
            try:
                ssid = list(apn.wintfs.values())[0].ssid
                node.cmd('iw dev %s connect %s' % (intf.name, ssid))
                self._notice('%s: iw connect %s sent' % (sta, ssid))
            except Exception as e2:
                self._notice('could not associate %s with %s (%s / %s)'
                             % (sta, ap, e, e2))

    def _disassoc(self, sta):
        node = self._node(sta)
        try:
            intf = list(node.wintfs.values())[0]
            node.cmd('iw dev %s disconnect' % intf.name)
            self._notice('%s disconnected' % sta)
        except Exception as e:
            self._notice('could not disconnect %s (%s)' % (sta, e))

    # ----------------------------------------------- live configuration ----
    def _live(self, name, changes):
        """Change a running device (not saved anywhere)."""
        node = self._node(name)
        if node is None:
            return self._notice('no device called %s' % name)
        try:
            wintf = list(node.wintfs.values())[0].name
        except Exception:
            wintf = None
        done, failed = [], []
        for k, v in changes.items():
            try:
                if k == 'position':
                    self._apply_move(name, *[float(c) for c in v])
                elif k == 'ip':
                    ip, _, plen = str(v).partition('/')
                    node.setIP(ip, prefixLen=int(plen or 8), intf=wintf) \
                        if wintf else node.setIP(ip, prefixLen=int(plen or 8))
                elif k in ('range', 'txpower', 'antennaGain',
                           'antennaHeight', 'channel', 'mode'):
                    fn = {'range': 'setRange', 'txpower': 'setTxPower',
                          'antennaGain': 'setAntennaGain',
                          'antennaHeight': 'setAntennaHeight',
                          'channel': 'setChannel', 'mode': 'setMode'}[k]
                    val = v if k == 'mode' else (
                        int(float(v)) if k == 'channel' else float(v))
                    getattr(node, fn)(val, intf=wintf)
                else:
                    raise ValueError('unknown setting')
                done.append('%s=%s' % (k, v))
            except Exception as e:
                failed.append('%s (%s)' % (k, e))
        self._notice('%s live: %s%s' % (
            name, ', '.join(done) or 'nothing changed',
            ('   FAILED: ' + '; '.join(failed)) if failed else ''))

    def _live_tc(self, name, ifname, params):
        """bw (Mbit/s), delay, jitter, loss (%) on one interface."""
        node = self._node(name)
        if node is None:
            return
        intf = getattr(node, 'nameToIntf', {}).get(ifname)
        p = dict((k, v) for k, v in params.items() if v not in (None, ''))
        for k in ('bw', 'loss'):                 # numbers for TCIntf
            if k in p:
                try:
                    p[k] = float(p[k])
                except ValueError:
                    return self._notice('%s must be a number' % k)
        try:
            intf.config(**p)                     # TCIntf (wired links)
            return self._notice('%s %s: %s' % (name, ifname, p))
        except Exception:
            pass
        try:
            netem = []
            if p.get('delay'):
                netem += ['delay', str(p['delay'])]
                if p.get('jitter'):
                    netem.append(str(p['jitter']))
            if p.get('loss') is not None:
                netem += ['loss', '%s%%' % p['loss']]
            if p.get('bw'):
                netem += ['rate', '%smbit' % p['bw']]
            out = node.cmd('tc qdisc replace dev %s root netem %s' % (
                ifname, ' '.join(netem) or 'delay 0ms'))
            self._notice('%s %s (netem): %s %s' % (name, ifname,
                                                   ' '.join(netem), out.strip()))
        except Exception as e:
            self._notice('traffic control on %s failed (%s)' % (ifname, e))

    def _save_view(self, cam):
        path = (self.layout or {}).get('source')
        if not path:
            return self._notice('no topology file loaded: the view can only '
                                'be saved with load_json(...)')
        try:
            with open(path) as fh:
                d = json.load(fh)
            d.setdefault('view', {})['saved'] = dict(
                cam, by='custom_gui', time=time.strftime('%Y-%m-%dT%H:%M:%S'))
            tmp = path + '.tmp'
            with open(tmp, 'w') as fh:
                json.dump(d, fh, indent=2)
            os.replace(tmp, path)
            self.layout.setdefault('view', {})['saved'] = d['view']['saved']
            self._notice('view saved in %s (the editor can restore it)' %
                         os.path.basename(path))
        except Exception as e:
            self._notice('could not save the view (%s)' % e)

    # ------------------------------------------------- CLI terminal ----
    def _cli_worker(self):
        while True:
            line = self._cli_q.get()
            if line is None:
                return
            self._cli_exec(line)

    def _cli_exec(self, line):
        """Run one Mininet CLI command and stream its output back."""
        import contextlib
        import logging
        import tempfile
        out = self._to_win

        def send(text):
            if text:
                out.put({'type': 'cli_out', 'text': text})

        class Handler(logging.Handler):
            def emit(self, record):
                try:
                    send(record.getMessage())
                except Exception:
                    pass

        class Writer(object):
            def write(self, t):
                send(t)

            def flush(self):
                pass
        cmd = line.strip()
        if cmd in ('exit', 'quit', 'EOF'):
            send('(use the terminal you started Mininet from to exit)\n')
            out.put({'type': 'cli_done'})
            return
        lg = None
        h = Handler()
        try:
            from mininet.log import lg
            if self._cli is None:
                try:
                    from mn_wifi.cli import CLI
                except ImportError:
                    from mininet.cli import CLI
                fd, empty = tempfile.mkstemp(suffix='.cli')
                os.close(fd)
                try:
                    self._cli = CLI(self.net, script=empty)   # batch: returns
                finally:
                    os.remove(empty)
            lg.addHandler(h)
            with contextlib.redirect_stdout(Writer()):
                self._cli.onecmd(cmd)
        except Exception as e:
            send('error: %s\n' % e)
        finally:
            if lg is not None:
                lg.removeHandler(h)
            out.put({'type': 'cli_done'})

    def _handle(self, msg):
        kind = msg[0]
        if kind == 'live':
            return self._live(msg[1], msg[2])
        if kind == 'live_tc':
            return self._live_tc(msg[1], msg[2], msg[3])
        if kind == 'save_view':
            return self._save_view(msg[1])
        if kind == 'cli':
            if self._cli_thread is None:
                self._cli_thread = threading.Thread(target=self._cli_worker,
                                                    daemon=True)
                self._cli_thread.start()
            return self._cli_q.put(msg[1])
        if kind == 'move' and self.allow_move:
            self._apply_move(*msg[1:])
        elif kind == 'wall_move' and self.layout:
            i, x, y = msg[1:]
            if 0 <= i < len(self.layout['walls']):
                self.layout['walls'][i].update(x=x, y=y)  # used by RSSI hook
        elif kind == 'pic_move' and self.layout:
            i, x, y = msg[1:]
            if 0 <= i < len(self.layout['pictures']):
                self.layout['pictures'][i].update(x=x, y=y)
        elif kind == 'term':
            self._term(msg[1])
        elif kind == 'assoc':
            self._assoc(msg[1], msg[2])
        elif kind == 'disassoc':
            self._disassoc(msg[1])

    def _loop(self):
        last = None
        while not self._stop.is_set():
            # 1) requests from the window
            try:
                while True:
                    self._handle(self._from_win.get_nowait())
            except queue.Empty:
                pass
            except Exception:
                pass
            # 2) send fresh data if something changed
            if not self._proc.is_alive():
                return                         # window was closed
            try:
                data = self._data()
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

# ==========================================================================
# Shared UI toolkit (same code in custom_edit.py and custom_gui.py):
# themes (light / dark), Word-like collapsible ribbon, git-graph device
# tree, picture adjustments.
# ==========================================================================

THEMES = {
    'light': {
        'bg': '#f6f6f3', 'grid': '#dedfd9', 'grid_major': '#c9cbc3',
        'axis': '#8a8b85', 'text': '#26272a', 'muted': '#6d6e69',
        'ap': '#2563c9', 'ap_fill': '#e1eafa', 'ap_ring': '#7fa3e3',
        'sta': '#e0761a', 'sta_ring': '#eaa66a', 'car': '#b9461a',
        'ctrl': '#7b3fc4', 'switch': '#4a4d55', 'host': '#6b6e74',
        'rf': '#16a05a', 'rf_glow': '#cdebd9', 'rf_bad': '#cc3a2f',
        'wired': '#4a4d55', 'control': '#7b3fc4', 'peer': '#0f9b8e',
        'shadow': '#d4d5cf', 'select': '#111111', 'warn': '#b3261e',
        'tip_bg': '#26272a', 'tip_fg': '#ffffff', 'bar': '#ebebe7',
        'halo': '#a9c1ee', 'bad_entry': '#fbe3e0', 'btn': 'white',
        'tabs': '#dcdcd6', 'x': '#c0392b', 'y': '#1e8449', 'z': '#2457a6',
        'pkt_wifi': '#11a85a', 'pkt_wired': '#2563c9', 'pkt_peer': '#0f9b8e',
        'wall': '#b4583d', 'outline': 'white',
    },
    'dark': {
        'bg': '#1d1e21', 'grid': '#2c2e33', 'grid_major': '#3a3d43',
        'axis': '#7e818a', 'text': '#e4e4e0', 'muted': '#9a9ca3',
        'ap': '#4f86f0', 'ap_fill': '#1c2740', 'ap_ring': '#335a9e',
        'sta': '#f28c3a', 'sta_ring': '#8a5226', 'car': '#e0602a',
        'ctrl': '#9a63e0', 'switch': '#a3a7b0', 'host': '#8e9299',
        'rf': '#2ec472', 'rf_glow': '#1c3b2b', 'rf_bad': '#ff5a4d',
        'wired': '#a3a7b0', 'control': '#9a63e0', 'peer': '#2ec4a0',
        'shadow': '#2a2b2f', 'select': '#f2f2f2', 'warn': '#ff6b5e',
        'tip_bg': '#eeeeea', 'tip_fg': '#1d1e21', 'bar': '#26282c',
        'halo': '#3d5a8f', 'bad_entry': '#5b2b2b', 'btn': '#34373d',
        'tabs': '#18191c', 'x': '#e05a4a', 'y': '#3cbf6a', 'z': '#5a8de0',
        'pkt_wifi': '#3ee08a', 'pkt_wired': '#6fa0ff', 'pkt_peer': '#3ee0c0',
        'wall': '#c96a4f', 'outline': '#1d1e21',
    },
}
C = dict(THEMES['light'])
_BG_KEYS = ('bg', 'bar', 'btn', 'tabs', 'grid', 'grid_major', 'ap', 'ap_ring',
            'halo', 'bad_entry', 'select', 'tip_bg', 'axis')
_FG_KEYS = ('text', 'muted', 'warn', 'ap', 'tip_fg')
_BG_OPTS = ('background', 'activebackground', 'highlightbackground',
            'troughcolor', 'selectcolor', 'disabledbackground',
            'readonlybackground', 'selectbackground')
_FG_OPTS = ('foreground', 'activeforeground', 'insertbackground',
            'disabledforeground', 'highlightcolor', 'selectforeground')
PREFS_PATH = os.path.expanduser('~/.config/mininet_custom_ui.json')


def load_prefs():
    try:
        with open(PREFS_PATH) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_prefs(**kw):
    p = load_prefs()
    p.update(kw)
    try:
        os.makedirs(os.path.dirname(PREFS_PATH), exist_ok=True)
        with open(PREFS_PATH, 'w') as fh:
            json.dump(p, fh, indent=2)
    except Exception:
        pass


def _norm_col(root, c):
    try:
        r, g, b = root.winfo_rgb(c)
        return '#%02x%02x%02x' % (r // 256, g // 256, b // 256)
    except Exception:
        return str(c).lower()


def style_ttk(root):
    """ttk widgets (comboboxes) follow the palette."""
    from tkinter import ttk
    st = ttk.Style(root)
    try:
        st.theme_use('clam')
    except Exception:
        pass
    st.configure('TCombobox', fieldbackground=C['btn'], background=C['bar'],
                 foreground=C['text'], arrowcolor=C['text'],
                 bordercolor=C['grid_major'], lightcolor=C['bar'],
                 darkcolor=C['bar'], selectbackground=C['ap'],
                 selectforeground='white')
    st.map('TCombobox',
           fieldbackground=[('readonly', C['btn']), ('disabled', C['bar'])],
           foreground=[('readonly', C['text']), ('disabled', C['muted'])],
           background=[('readonly', C['bar'])])
    root.option_add('*TCombobox*Listbox.background', C['btn'])
    root.option_add('*TCombobox*Listbox.foreground', C['text'])
    root.option_add('*TCombobox*Listbox.selectBackground', C['ap'])
    return st


def apply_theme(root, name):
    """Switch palette and recolour every Tk widget already on screen."""
    old = dict(C)
    C.clear()
    C.update(THEMES[name])
    bgmap, fgmap = {}, {}
    for keys, m in ((_BG_KEYS, bgmap), (_FG_KEYS, fgmap)):
        for k in keys:
            m[_norm_col(root, old[k])] = C[k]

    def walk(w):
        for opts, m in ((_BG_OPTS, bgmap), (_FG_OPTS, fgmap)):
            for o in opts:
                try:
                    v = w.cget(o)
                except Exception:
                    continue
                if not v:
                    continue
                nv = m.get(_norm_col(root, v))
                if nv:
                    try:
                        w.configure(**{o: nv})
                    except Exception:
                        pass
        for ch in w.winfo_children():
            walk(ch)
    walk(root)
    style_ttk(root)


APP_DIR = os.path.dirname(os.path.abspath(__file__))


class Properties(object):
    """profile/<app>.ini next to the program: options that are not part of a
    topology file (theme, toggles, window size, recent files...)."""

    def __init__(self, app):
        import configparser
        self.path = os.path.join(APP_DIR, 'profile', '%s.ini' % app)
        self.cp = configparser.ConfigParser(interpolation=None)
        try:
            self.cp.read(self.path)
        except Exception:
            pass

    def get(self, sec, key, default=None):
        try:
            v = self.cp.get(sec, key)
        except Exception:
            return default
        try:
            if isinstance(default, bool):
                return v.strip().lower() in ('1', 'true', 'yes', 'on')
            if isinstance(default, int):
                return int(float(v))
            if isinstance(default, float):
                return float(v)
        except ValueError:
            return default
        return v

    def set(self, sec, key, value):
        if not self.cp.has_section(sec):
            self.cp.add_section(sec)
        self.cp.set(sec, key, str(value))

    def items(self, sec):
        try:
            return self.cp.items(sec)
        except Exception:
            return []

    def clear(self, sec):
        if self.cp.has_section(sec):
            self.cp.remove_section(sec)

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + '.tmp'
            with open(tmp, 'w') as fh:
                self.cp.write(fh)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def bind(self, sec, pairs):
        """Load tk variables from the file, and save them on every change."""
        for key, var in pairs:
            cur = var.get()
            var.set(self.get(sec, key, cur))

            def store(*a, k=key, v=var):
                try:
                    self.set(sec, k, v.get())
                    self.save()
                except Exception:
                    pass
            var.trace_add('write', store)


def camera_state(win):
    """Camera as plain data, independent of the window size."""
    w, h = win.size()
    s = max(win.scale, 1e-9)
    return {'yaw': round(win.yaw, 5), 'pitch': round(win.pitch, 5),
            'target': [round(float(v), 3) for v in win.target],
            'span': round(min(w, h) / s, 4),
            'pan': [round(win.pan[0] / s, 4), round(win.pan[1] / s, 4)]}


def apply_camera(win, cam):
    try:
        w, h = win.size()
        win.scale = min(w, h) / max(float(cam.get('span') or 100), 1e-6)
        win.yaw = float(cam.get('yaw', win.yaw))
        win.pitch = float(cam.get('pitch', win.pitch))
        t = cam.get('target') or win.target
        win.target = (float(t[0]), float(t[1]), float(t[2]))
        p = cam.get('pan') or [0, 0]
        win.pan = [float(p[0]) * win.scale, float(p[1]) * win.scale]
        win.user_view = True
        return True
    except (TypeError, ValueError, IndexError, AttributeError):
        return False


class _FlowBar(object):
    """A frame whose child groups flow left-to-right and wrap to new rows."""

    def __init__(self, tk, parent, bg, gap=10, vgap=4):
        self.tk, self.bg, self.gap, self.vgap = tk, bg, gap, vgap
        self.frame = tk.Frame(parent, bg=bg, height=40)
        self.groups = []
        self.frame.bind('<Configure>', lambda e: self.layout())
        self.frame.after_idle(self.layout)

    def group(self):
        g = self.tk.Frame(self.frame, bg=self.bg)
        self.groups.append(g)
        return g

    def layout(self):
        W = self.frame.winfo_width()
        if W <= 1:
            W = self.frame.winfo_toplevel().winfo_width()
        x, y, row_h = 6, self.vgap, 0
        for g in self.groups:
            w, h = g.winfo_reqwidth(), g.winfo_reqheight()
            if x > 6 and x + w > W - 6:            # wrap to a new row
                x, y, row_h = 6, y + row_h + self.vgap, 0
            g.place(x=x, y=y)
            x += w + self.gap
            row_h = max(row_h, h)
        total = y + row_h + self.vgap
        if int(self.frame.cget('height')) != total:
            self.frame.config(height=total)


class _Ribbon(object):
    """Word-like ribbon: tabs (categories) with captioned groups.
    Collapse with the button on the right, double-click on a tab, or
    Ctrl+F1; clicking a tab while collapsed opens it again."""

    def __init__(self, tk, parent):
        self.tk = tk
        self.frame = tk.Frame(parent, bg=C['bar'])
        self.head = tk.Frame(self.frame, bg=C['tabs'])
        self.head.pack(fill='x')
        self.body = tk.Frame(self.frame, bg=C['bar'])
        self.body.pack(fill='x')
        self.tabs, self.current, self.collapsed = {}, None, False
        self.btn = tk.Label(self.head, text='Collapse ribbon', bg=C['tabs'],
                            fg=C['muted'], cursor='hand2',
                            font=('TkDefaultFont', 8))
        self.btn.pack(side='right', padx=10)
        self.btn.bind('<Button-1>', lambda e: self.toggle())
        self.extra = tk.Frame(self.head, bg=C['tabs'])  # right side widgets
        self.extra.pack(side='right')

    def tab(self, name):
        tk = self.tk
        lb = tk.Label(self.head, text=name, padx=14, pady=4, bg=C['tabs'],
                      fg=C['muted'], cursor='hand2',
                      font=('TkDefaultFont', 9, 'bold'))
        lb.pack(side='left')
        lb.bind('<Button-1>', lambda e, n=name: self.select(n))
        lb.bind('<Double-Button-1>', lambda e: self.toggle())
        page = _FlowBar(tk, self.body, C['bar'])
        self.tabs[name] = (lb, page)
        if self.current is None:
            self.select(name)
        return page

    def group(self, page, caption):
        """Captioned group inside a tab page (caption under it, like Word)."""
        tk = self.tk
        outer = page.group()
        tk.Frame(outer, width=1, bg=C['grid_major']).pack(side='right',
                                                          fill='y', padx=(8, 0))
        col = tk.Frame(outer, bg=C['bar'])
        col.pack(side='left', fill='y')
        tk.Label(col, text=caption, bg=C['bar'], fg=C['muted'],
                 font=('TkDefaultFont', 7, 'bold')).pack(side='bottom')
        body = tk.Frame(col, bg=C['bar'])
        body.pack(side='top', expand=True)
        return body

    def select(self, name):
        if self.collapsed:
            self.toggle()
        for n, (lb, page) in self.tabs.items():
            if n == name:
                lb.config(bg=C['bar'], fg=C['ap'])
                page.frame.pack(fill='x')
                page.frame.after_idle(page.layout)
            else:
                lb.config(bg=C['tabs'], fg=C['muted'])
                page.frame.pack_forget()
        self.current = name

    def toggle(self):
        self.collapsed = not self.collapsed
        if self.collapsed:
            self.body.pack_forget()
            self.btn.config(text='Expand ribbon')
        else:
            self.body.pack(fill='x')
            self.btn.config(text='Collapse ribbon')


LANE_COL = {'wifi': 'rf', 'auto': 'rf', 'wired': 'wired',
            'control': 'control', 'adhoc': 'peer', 'mesh': 'peer'}
KIND_RANK = {'ctrl': 0, 'switch': 1, 'ap': 2, 'host': 3, 'sta': 3, 'car': 3}


def build_tree(nodes, edges):
    """Turn devices + links into git-graph rows.
    nodes: [(name, kind)]; edges: [(a, b, link_type, text)].
    Each device appears once; parents are higher in the hierarchy
    (controller > switch > AP > host / station). Extra links of an already
    placed device are listed in its 'also' field."""
    kind = dict(nodes)
    kids, also = {}, {}
    for a, b, lt, txt in edges:
        if a not in kind or b not in kind:
            continue
        ra, rb = KIND_RANK.get(kind[a], 9), KIND_RANK.get(kind[b], 9)
        if (rb, b) < (ra, a):
            a, b = b, a
        kids.setdefault(a, []).append((b, lt, txt))
    order = sorted(kind, key=lambda n: (KIND_RANK.get(kind[n], 9), n))
    rows, placed = [], {}

    def visit(n, depth, parent, lt, txt):
        placed[n] = len(rows)
        rows.append({'name': n, 'kind': kind[n], 'depth': depth,
                     'parent': parent, 'link': lt, 'link_text': txt,
                     'also': []})
        me = placed[n]
        for c, clt, ctxt in sorted(kids.get(n, []),
                                   key=lambda k: (KIND_RANK.get(kind[k[0]], 9),
                                                  k[0])):
            if c in placed:
                rows[placed[c]]['also'].append('%s %s' % (clt, n))
                rows[me]['also'].append('%s %s' % (clt, c))
                continue
            visit(c, depth + 1, me, clt, ctxt)
    for n in order:
        if n not in placed:
            visit(n, 0, None, None, '')
    return rows


class _TreePanel(object):
    """Device / connection list drawn like `git log --graph`.
    provider must offer: tree_rows() -> rows (see build_tree, plus
    optional 'detail'), tree_menu(menu, row), tree_activate(row),
    tree_select(row), draw_kind_icon(canvas, kind, x, y)."""
    ROW = 30

    def __init__(self, tk, root, provider, title='Devices & connections'):
        self.tk, self.p = tk, provider
        top = self.top = tk.Toplevel(root)
        top.title(title)
        top.configure(bg=C['bg'])
        top.geometry('460x620')
        head = tk.Frame(top, bg=C['bar'], padx=8, pady=6)
        head.pack(fill='x')
        tk.Label(head, text=title, bg=C['bar'], fg=C['text'],
                 font=('TkDefaultFont', 11, 'bold')).pack(side='left')
        self.filter = tk.StringVar()
        e = tk.Entry(head, textvariable=self.filter, width=14, relief='flat',
                     bg=C['btn'], fg=C['text'], insertbackground=C['text'],
                     highlightthickness=1, highlightbackground=C['grid_major'])
        e.pack(side='right')
        tk.Label(head, text='filter', bg=C['bar'], fg=C['muted']).pack(
            side='right', padx=4)
        self.filter.trace_add('write', lambda *a: self.refresh())
        tk.Label(top, text='click: select   double-click: go to + inspect   '
                 'right-click: actions', bg=C['bg'], fg=C['muted'],
                 font=('TkDefaultFont', 8)).pack(fill='x')
        wrap = tk.Frame(top, bg=C['bg'])
        wrap.pack(fill='both', expand=True)
        self.cv = tk.Canvas(wrap, bg=C['bg'], highlightthickness=0)
        sb = tk.Scrollbar(wrap, orient='vertical', command=self.cv.yview)
        self.cv.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self.cv.pack(side='left', fill='both', expand=True)
        self.cv.bind('<Button-1>', self.on_click)
        self.cv.bind('<Double-Button-1>', self.on_double)
        self.cv.bind('<Button-3>', self.on_menu)
        for ev, d in (('<Button-4>', -1), ('<Button-5>', 1)):
            self.cv.bind(ev, lambda e, d=d: self.cv.yview_scroll(d * 2, 'units'))
        self.cv.bind('<MouseWheel>', lambda e: self.cv.yview_scroll(
            -1 if e.delta > 0 else 1, 'units'))
        top.protocol('WM_DELETE_WINDOW', self.close)
        self.rows, self.sel = [], None
        self.alive = True
        self.refresh()

    def close(self):
        self.alive = False
        self.top.destroy()

    def row_at(self, y):
        i = int(self.cv.canvasy(y) // self.ROW)
        return self.rows[i] if 0 <= i < len(self.rows) else None

    def on_click(self, e):
        r = self.row_at(e.y)
        if r:
            self.sel = r['name']
            self.p.tree_select(r)
            self.refresh()

    def on_double(self, e):
        r = self.row_at(e.y)
        if r:
            self.p.tree_activate(r)

    def on_menu(self, e):
        r = self.row_at(e.y)
        if not r:
            return
        self.sel = r['name']
        self.refresh()
        m = self.tk.Menu(self.top, tearoff=0, bg=C['btn'], fg=C['text'],
                         activebackground=C['ap'], activeforeground='white')
        self.p.tree_menu(m, r)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def refresh(self):
        if not self.alive or not self.top.winfo_exists():
            return
        rows = self.p.tree_rows()
        f = self.filter.get().strip().lower()
        if f:
            keep = set()
            for i, r in enumerate(rows):
                if f in (r['name'] + ' ' + r.get('detail', '')).lower():
                    j = i
                    while j is not None:               # keep the branch
                        keep.add(j)
                        j = rows[j]['parent']
            remap, out = {}, []
            for i, r in enumerate(rows):
                if i in keep:
                    remap[i] = len(out)
                    out.append(dict(r))
            for r in out:
                r['parent'] = remap.get(r['parent'])
            rows = out
        self.rows = rows
        cv, R = self.cv, self.ROW
        cv.delete('all')
        cv.configure(bg=C['bg'])
        maxd = max([r['depth'] for r in rows] or [0])
        tx = 22 + (maxd + 1) * 22
        for i, r in enumerate(rows):
            y = i * R + R / 2
            if r['name'] == self.sel:
                cv.create_rectangle(0, i * R + 1, 2000, (i + 1) * R - 1,
                                    fill=C['halo'], outline='')
        for i, r in enumerate(rows):                       # branches
            if r['parent'] is None:
                continue
            p = r['parent']
            px, py = 18 + rows[p]['depth'] * 22, p * R + R / 2
            cx, cy = 18 + r['depth'] * 22, i * R + R / 2
            col = C.get(LANE_COL.get(r['link'] or '', ''), C['axis'])
            cv.create_line(px, py + 7, px, cy - 12, px, cy - 2, px + 4, cy,
                           cx - 7, cy, fill=col, width=2.5, smooth=True,
                           dash=(4, 3) if r['link'] in ('control', 'auto',
                                                        'adhoc', 'mesh')
                           else None)
        for i, r in enumerate(rows):                       # dots + text
            x, y = 18 + r['depth'] * 22, i * R + R / 2
            self.p.draw_kind_icon(cv, r['kind'], x, y)
            cv.create_text(tx, y - 6, anchor='w', text=r['name'],
                           fill=C['text'], font=('TkDefaultFont', 10, 'bold'))
            sub = r.get('detail', '')
            if r['link']:
                sub = ('%s %s' % (r['link'], r.get('link_text') or '')).strip() \
                    + ('  ·  ' + sub if sub else '')
            if r['also']:
                sub += '  ·  also: ' + ', '.join(r['also'])
            cv.create_text(tx, y + 8, anchor='w', text=sub, fill=C['muted'],
                           font=('TkDefaultFont', 8))
        cv.configure(scrollregion=(0, 0, 600, max(len(rows) * R, 10)))


PIC_EDIT_KEYS = ('crop_l', 'crop_t', 'crop_r', 'crop_b', 'brightness',
                 'contrast', 'saturation', 'grayscale', 'invert', 'flip_h',
                 'flip_v', 'rot90', 'key_white', 'key_level')
PIC_EDIT_DEFAULTS = {'crop_l': 0.0, 'crop_t': 0.0, 'crop_r': 0.0,
                     'crop_b': 0.0, 'brightness': 100.0, 'contrast': 100.0,
                     'saturation': 100.0, 'grayscale': False, 'invert': False,
                     'flip_h': False, 'flip_v': False, 'rot90': '0',
                     'key_white': False, 'key_level': 235.0}


def pic_edits_key(p):
    return tuple(p.get(k, PIC_EDIT_DEFAULTS[k]) for k in PIC_EDIT_KEYS)


def apply_pic_edits(img, p):
    """Non-destructive picture adjustments (RGBA in, RGBA out):
    rotate (clockwise), flips, crop (% of each side), grayscale,
    brightness / contrast / saturation (%), invert, white -> transparent."""
    from PIL import Image, ImageChops, ImageEnhance, ImageOps
    g = lambda k: p.get(k, PIC_EDIT_DEFAULTS[k])
    rot = str(g('rot90'))
    if rot == '90':
        img = img.transpose(Image.ROTATE_270)
    elif rot == '180':
        img = img.transpose(Image.ROTATE_180)
    elif rot == '270':
        img = img.transpose(Image.ROTATE_90)
    if g('flip_h'):
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    if g('flip_v'):
        img = img.transpose(Image.FLIP_TOP_BOTTOM)
    w, h = img.size  # crop what you see (after rotate / flip)
    l, t = float(g('crop_l')) / 100.0, float(g('crop_t')) / 100.0
    r, b = float(g('crop_r')) / 100.0, float(g('crop_b')) / 100.0
    if (l or t or r or b) and l + r < 0.98 and t + b < 0.98:
        img = img.crop((int(w * l), int(h * t), max(int(w * (1 - r)),
                                                     int(w * l) + 1),
                        max(int(h * (1 - b)), int(h * t) + 1)))
    alpha = img.getchannel('A')
    rgb = img.convert('RGB')
    if g('grayscale'):
        rgb = ImageOps.grayscale(rgb).convert('RGB')
    for key, enh in (('brightness', ImageEnhance.Brightness),
                     ('contrast', ImageEnhance.Contrast),
                     ('saturation', ImageEnhance.Color)):
        v = float(g(key))
        if abs(v - 100.0) > 1e-6:
            rgb = enh(rgb).enhance(max(v, 0.0) / 100.0)
    if g('invert'):
        rgb = ImageOps.invert(rgb)
    if g('key_white'):
        lvl = int(float(g('key_level')))
        mask = rgb.convert('L').point(lambda v: 0 if v >= lvl else 255)
        alpha = ImageChops.multiply(alpha, mask)
    out = rgb.convert('RGBA')
    out.putalpha(alpha)
    return out


KIND_LABEL = {'ap': 'access point', 'sta': 'station', 'car': 'car',
              'ctrl': 'controller', 'switch': 'switch', 'host': 'host'}
CLIENT_KINDS = ('sta', 'car')
WALL_MODES = [('solid', 'Walls always visible'),
              ('fade', 'Front walls transparent'),
              ('cutaway', 'Front walls cut down'),
              ('outline', 'Walls as outlines')]
PKT_SPEEDS = [('Real time (1x)', 1.0), ('Slowed 0.5x', 0.5),
              ('Slowed 0.25x', 0.25), ('Slowed 0.1x', 0.1),
              ('Slowed 0.05x', 0.05), ('Paused', 0.0)]
PKT_MULTS = [('x0.01', 0.01), ('x0.1', 0.1), ('x0.25', 0.25), ('x0.5', 0.5),
             ('x1', 1.0), ('x2', 2.0), ('x5', 5.0), ('x10', 10.0)]
PKT_TRAVEL = 0.45          # seconds of "real time" a dot takes on a link
PKT_MAX_RATE = 40.0        # dots per second per link, at most
PKT_MAX = 900              # dots on screen, at most


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


def _g(v):
    try:
        return '%g' % round(float(v), 2)
    except (TypeError, ValueError):
        return str(v)


def _rate(pps, bps):
    if bps >= 1e6:
        b = '%.1f MB/s' % (bps / 1e6)
    elif bps >= 1e3:
        b = '%.1f kB/s' % (bps / 1e3)
    else:
        b = '%d B/s' % bps
    return '%s pkt/s, %s' % (_g(pps), b)


def draw_icon(cv, kind, x, y, ow='white', s=1.0):
    """Device icons, same shapes as custom_edit.py."""
    k = s
    if kind == 'ap':
        cv.create_rectangle(x - 10 * k, y - 7 * k, x + 10 * k, y + 7 * k,
                            fill=C['ap'], outline=ow, width=2)
        cv.create_line(x + 6 * k, y - 7 * k, x + 6 * k, y - 15 * k,
                       fill=C['ap'], width=2)
        for r in (5 * k, 9 * k):                         # wifi arcs
            cv.create_arc(x + 6 * k - r, y - 15 * k - r, x + 6 * k + r,
                          y - 15 * k + r, start=45, extent=90, style='arc',
                          outline=C['ap'], width=1.5)
        for i in (-5, 0):                                # leds
            cv.create_oval(x + i * k - 1.5, y - 1.5, x + i * k + 1.5, y + 1.5,
                           fill='#9ff0b8', outline='')
    elif kind == 'sta':
        cv.create_oval(x - 8 * k, y - 8 * k, x + 8 * k, y + 8 * k,
                       fill=C['sta'], outline=ow, width=2)
        cv.create_oval(x - 2.5 * k, y - 2.5 * k, x + 2.5 * k, y + 2.5 * k,
                       fill='white', outline='')
    elif kind == 'car':
        cv.create_polygon(x - 7 * k, y - 4 * k, x - 3 * k, y - 10 * k,
                          x + 5 * k, y - 10 * k, x + 8 * k, y - 4 * k,
                          fill=C['car'], outline=ow, width=1.5)
        cv.create_rectangle(x - 1 * k, y - 8.5 * k, x + 4.5 * k, y - 5 * k,
                            fill='#f3e3d8', outline='')
        cv.create_rectangle(x - 12 * k, y - 4 * k, x + 12 * k, y + 4 * k,
                            fill=C['car'], outline=ow, width=2)
        for wx in (-6, 6):
            cv.create_oval(x + (wx - 3.5) * k, y + 0.5 * k,
                           x + (wx + 3.5) * k, y + 7.5 * k,
                           fill='#26272a', outline=ow, width=1)
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
    else:                                                # host
        cv.create_rectangle(x - 8 * k, y - 8 * k, x + 8 * k, y + 8 * k,
                            fill=C['host'], outline=ow, width=2)
        cv.create_rectangle(x - 4.5 * k, y - 4.5 * k, x + 4.5 * k, y + 1 * k,
                            fill='#c4c6cb', outline='')


class _Window(object):
    HIT = 16           # px radius for clicking a node

    def __init__(self, inq, outq, cfg):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.inq, self.outq, self.cfg = inq, outq, cfg
        self.props = Properties('gui')
        theme = cfg.get('theme') or self.props.get('ui', 'theme', 'light')
        if theme not in THEMES:
            theme = 'light'
        C.clear()
        C.update(THEMES[theme])
        self.data = {'nodes': [], 'links': [], 'traffic': []}
        self.layout = {'walls': [], 'pictures': [], 'warnings': [],
                       'locked_devices': []}
        self.local = {}            # name -> (xyz, until_time) while dragging
        self.auto_pos = {}         # auto-placed controllers
        self.hits, self.wall_hits = [], []
        self.hover = self.hover_wall = None
        self.mouse = (0, 0)
        self.drag = None
        self.t0 = time.time()
        self.fitted = False
        self.user_view = False     # becomes True once you rotate/pan/zoom
        self.pic_src, self.pic_tk, self.pic_raw = {}, {}, {}
        self.selected = None       # device chosen in the devices list
        self.sel_item = None       # ('wall' | 'pic', index)
        self.dots, self.acc = [], {}
        self.sim_t, self.last_frame = 0.0, time.time()
        self.notice = ('', 0)
        self.tree, self.details = None, {}
        self._tree_t = 0

        # camera
        self.yaw, self.pitch = math.radians(-30), math.radians(55)
        self.scale, self.pan = 1.0, [0.0, 0.0]
        self.target = (0.0, 0.0, 0.0)

        root = self.root = tk.Tk()
        root.title(cfg['title'])
        root.configure(bg=C['bar'])
        style_ttk(root)
        w, h = cfg['size']
        V = tk.BooleanVar
        self.v_ranges, self.v_outline = V(value=True), V(value=False)
        self.v_sphere, self.v_anim = V(value=True), V(value=True)
        self.v_labels, self.v_walls = V(value=True), V(value=True)
        self.v_pics, self.v_badges = V(value=True), V(value=True)
        self.v_pkts, self.v_rates = V(value=True), V(value=False)
        self.v_locks = V(value=False)
        self.v_links = V(value=True)
        self.cli_win, self.live_wins = None, {}
        self.view_applied = False
        self.v_dark = V(value=theme == 'dark')
        self.wall_mode_label = tk.StringVar(value=WALL_MODES[1][1])
        self.pkt_speed = tk.StringVar(value=PKT_SPEEDS[0][0])
        self.pkt_mult = tk.StringVar(value='x1')
        self.tip_mode = tk.StringVar(value='basic')
        self.props.bind('ui', [
            ('ranges', self.v_ranges), ('outline', self.v_outline),
            ('sphere', self.v_sphere), ('animation', self.v_anim),
            ('labels', self.v_labels), ('walls', self.v_walls),
            ('pictures', self.v_pics), ('badges', self.v_badges),
            ('links', self.v_links), ('packets', self.v_pkts),
            ('rates', self.v_rates), ('respect_locks', self.v_locks),
            ('wall_mode', self.wall_mode_label),
            ('packet_speed', self.pkt_speed), ('packet_mult', self.pkt_mult),
            ('tooltips', self.tip_mode)])
        geo = self.props.get('window', 'geometry', '')
        if geo:
            root.geometry(geo)
        root.protocol('WM_DELETE_WINDOW', self.on_close)
        self.build_ribbon()
        tab = self.props.get('window', 'ribbon_tab', 'View')
        if tab in self.ribbon.tabs:
            self.ribbon.select(tab)
        if self.props.get('window', 'ribbon_collapsed', False):
            self.ribbon.toggle()

        self.status = tk.Label(root, anchor='nw', justify='left', padx=10,
                               pady=4, bg=C['bar'], fg=C['text'], height=6,
                               font=('TkDefaultFont', 9))
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
        cv.bind('<Leave>', lambda e: self.set_hover(None, None))
        cv.bind('<Double-Button-1>', self.on_double)
        cv.bind('<MouseWheel>', lambda e: self.zoom(e, 1 if e.delta > 0 else -1))
        cv.bind('<Button-4>', lambda e: self.zoom(e, 1))
        cv.bind('<Button-5>', lambda e: self.zoom(e, -1))
        for key, fn in (('t', self.view_top), ('3', self.view_3d),
                        ('f', self.fit), ('d', self.open_tree),
                        ('c', self.open_cli)):
            root.bind('<Key-%s>' % key, lambda e, f=fn: f())
        for key, var in (('s', self.v_sphere), ('a', self.v_anim),
                         ('w', self.v_walls), ('p', self.v_pics),
                         ('k', self.v_pkts)):
            root.bind('<Key-%s>' % key, lambda e, v=var: (v.set(not v.get()),
                                                          self.on_toggle()))
        root.bind('<Control-F1>', lambda e: self.ribbon.toggle())
        self.on_toggle()
        root.after(50, self.tick)

    # ---------------------------------------------------------- ribbon ----
    def _btn(self, parent, text, cmd, width=None, side='left', accent=False):
        b = self.tk.Button(parent, text=text, command=cmd, relief='flat',
                           bg=C['ap'] if accent else C['btn'],
                           fg='white' if accent else C['text'],
                           activebackground=C['grid'],
                           activeforeground=C['text'], highlightthickness=0,
                           padx=6, pady=1)
        if width:
            b.config(width=width)
        b.pack(side=side, padx=2, pady=1)
        return b

    def _check(self, parent, text, var, cmd=None, side='left'):
        c = self.tk.Checkbutton(parent, text=text, variable=var, bg=C['bar'],
                                fg=C['text'], selectcolor=C['btn'],
                                activebackground=C['bar'],
                                activeforeground=C['text'],
                                highlightthickness=0,
                                font=('TkDefaultFont', 9),
                                command=cmd or self.on_toggle)
        c.pack(side=side, padx=2, anchor='w')
        return c

    def build_ribbon(self):
        tk, ttk = self.tk, self.ttk
        rb = self.ribbon = _Ribbon(tk, self.root)
        rb.frame.pack(fill='x')

        def rows(g):
            r1, r2 = tk.Frame(g, bg=C['bar']), tk.Frame(g, bg=C['bar'])
            r1.pack(anchor='w')
            r2.pack(anchor='w')
            return r1, r2

        def combo(parent, var, values, width):
            cb = ttk.Combobox(parent, textvariable=var, values=values,
                              state='readonly', width=width)
            cb.pack(side='left', padx=2)
            cb.bind('<<ComboboxSelected>>', lambda e: (
                self.on_toggle(), self.canvas.focus_set()))
            return cb

        vt = rb.tab('View')
        g = rb.group(vt, 'CAMERA')
        r1, r2 = rows(g)
        self._btn(r1, '3D view', self.view_3d, width=8)
        self._btn(r1, 'Top view', self.view_top, width=8)
        self._btn(r2, 'Fit', self.fit, width=8)
        self._btn(r2, 'Saved view', self.restore_view, width=8)
        self._btn(r1, 'Save view', self.save_view, width=8)
        g = rb.group(vt, 'WALLS IN FRONT OF THE CAMERA')
        self.cb_mode = combo(g, self.wall_mode_label,
                             [l for _, l in WALL_MODES], 24)
        g = rb.group(vt, 'THEME')
        self._check(g, 'Dark mode', self.v_dark, self.toggle_theme)

        st = rb.tab('Show')
        g = rb.group(st, 'RANGES')
        r1, r2 = rows(g)
        self.cb_ranges = self._check(r1, 'Ranges', self.v_ranges)
        self.cb_outline = self._check(r1, 'outline only', self.v_outline)
        self._check(r2, 'Sphere', self.v_sphere)
        g = rb.group(st, 'SCENE')
        r1, r2 = rows(g)
        self._check(r1, 'Labels', self.v_labels)
        self._check(r1, 'Links', self.v_links)
        self._check(r1, 'Walls', self.v_walls)
        self._check(r2, 'Pictures', self.v_pics)
        self._check(r2, 'Animation', self.v_anim)
        g = rb.group(st, 'ACCESS POINTS')
        self._check(g, 'Connected devices badge', self.v_badges)

        pt = rb.tab('Packets')
        g = rb.group(pt, 'LIVE TRAFFIC  (K)')
        r1, r2 = rows(g)
        self._check(r1, 'Show packets', self.v_pkts)
        self._check(r2, 'Rates on links', self.v_rates)
        g = rb.group(pt, 'SPEED')
        combo(g, self.pkt_speed, [l for l, _ in PKT_SPEEDS], 14)
        g = rb.group(pt, 'MULTIPLIER  (dots per packet)')
        combo(g, self.pkt_mult, [l for l, _ in PKT_MULTS], 7)
        g = rb.group(pt, 'NOW')
        self.lbl_rate = tk.Label(g, text='no traffic', bg=C['bar'],
                                 fg=C['muted'], width=30, anchor='w',
                                 font=('TkDefaultFont', 9))
        self.lbl_rate.pack()

        tt = rb.tab('Tools')
        g = rb.group(tt, 'DEVICES  (D)')
        r1, r2 = rows(g)
        self._btn(r1, 'Devices list', self.open_tree, width=12, accent=True)
        self._btn(r2, 'Live config...', self.live_for_selected, width=12)
        g = rb.group(tt, 'MININET')
        self._btn(g, 'CLI terminal', self.open_cli, width=12, accent=True)
        g = rb.group(tt, 'TOOLTIPS')
        for val, text in (('basic', 'Basic'), ('advanced', 'Advanced')):
            tk.Radiobutton(g, text=text, value=val, variable=self.tip_mode,
                           bg=C['bar'], fg=C['text'], selectcolor=C['btn'],
                           activebackground=C['bar'],
                           activeforeground=C['text'], highlightthickness=0,
                           command=self.redraw).pack(side='left', padx=2)
        g = rb.group(tt, 'LOCKS')
        self._check(g, 'Respect device locks of the file', self.v_locks)

    def on_toggle(self):
        if self.v_ranges.get():
            self.cb_outline.pack(side='left', after=self.cb_ranges)
        else:
            self.cb_outline.pack_forget()
        self.cb_mode.config(state='readonly' if self.v_walls.get()
                            else 'disabled')
        if not self.v_pkts.get():
            self.dots = []
        self.redraw()

    def toggle_theme(self):
        name = 'dark' if self.v_dark.get() else 'light'
        apply_theme(self.root, name)
        self.props.set('ui', 'theme', name)
        self.props.save()
        if self.tree and self.tree.alive:
            self.tree.refresh()
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
                elif isinstance(item, dict) and item.get('type') == 'cli_out':
                    if self.cli_win:
                        self.cli_win.append(item.get('text', ''))
                    continue
                elif isinstance(item, dict) and item.get('type') == 'cli_done':
                    if self.cli_win:
                        self.cli_win.done()
                    continue
                elif isinstance(item, dict) and item.get('type') == 'notice':
                    self.notice = (item.get('text', ''), time.time() + 8)
                else:
                    self.data = item
                    self.data.setdefault('traffic', [])
                    self.update_rate_label()
                changed = True
        except queue.Empty:
            pass
        except (EOFError, OSError):
            pass
        now = time.time()
        dt, self.last_frame = min(now - self.last_frame, 0.5), now
        self.update_packets(dt)
        if changed and not self.fitted and self.placed():
            cam = (self.layout.get('view') or {}).get('last')
            if not self.view_applied and cam and apply_camera(self, cam):
                self.view_applied = True        # where the editor left it
                self.redraw()
            else:
                self.fit()
            self.fitted = True
        if changed or self.v_anim.get() or self.drag or self.dots:
            self.redraw()
        if changed and self.tree and self.tree.alive and \
                now - self._tree_t > 1.0:
            self._tree_t = now
            self.tree.refresh()
        self.root.after(40, self.tick)          # ~25 frames per second

    def node(self, name):
        for n in self.data['nodes']:
            if n['name'] == name:
                return n
        return None

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

    def in_wall(self, p):
        for w in self.walls():
            if wall_contains_point(w, p):
                return w
        return None

    def clients_of(self, ap):
        return [n for n in self.data['nodes']
                if n['kind'] in CLIENT_KINDS and n.get('ap') == ap]

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

    def go_to(self, name):
        p = self.positions().get(name)
        if p is None:
            return
        self.target = tuple(p)
        self.pan = [0.0, 0.0]
        self.user_view = True
        self.selected = name
        self.redraw()

    # ----------------------------------------------------------- mouse ----
    def node_at(self, x, y):
        best, bd = None, self.HIT
        for name, sx, sy in self.hits:
            d = math.hypot(sx - x, sy - y)
            if d <= bd:
                best, bd = name, d
        return best

    def wall_at(self, x, y, unlocked=False):
        for i, pts in reversed(self.wall_hits):
            if _in_poly(x, y, pts) and not (
                    unlocked and self.walls()[i].get('locked')):
                return i
        return None

    def pic_at(self, x, y, unlocked=True):
        if not self.v_pics.get():
            return None
        pics = self.layout.get('pictures') or []
        for i in range(len(pics) - 1, -1, -1):
            p = pics[i]
            if not p.get('visible', True) or (unlocked and p.get('locked')):
                continue
            if _in_poly(x, y, [self.project(*c)[:2]
                               for c in self.pic_corners(p)]):
                return i
        return None

    def movable(self, name):
        return self.cfg.get('allow_move', True) and not (
            self.v_locks.get() and
            name in (self.layout.get('locked_devices') or []))

    def on_press(self, e):
        self.press = (e.x, e.y)
        name = self.node_at(e.x, e.y)
        if name and self.movable(name):
            xyz = self.positions()[name]
            self.drag = {'mode': 'node', 'name': name, 'xyz': xyz,
                         'start': (e.x, e.y), 'sent': 0}
            return
        if name:
            self.notice = ('%s is locked in the file (Tools tab: '
                           '"Respect device locks")' % name, time.time() + 4)
        i = self.wall_at(e.x, e.y, unlocked=True) if self.v_walls.get() \
            else None
        if i is not None:
            w = self.walls()[i]
            mx, my = self.unproject(e.x, e.y, w['z'])
            self.sel_item = ('wall', i)
            self.drag = {'mode': 'wall', 'i': i, 'moved': False,
                         'off': (w['x'] - mx, w['y'] - my)}
            return
        i = self.pic_at(e.x, e.y)
        if i is not None:
            p = self.layout['pictures'][i]
            mx, my = self.unproject(e.x, e.y, p['z'])
            self.sel_item = ('pic', i)
            self.drag = {'mode': 'pic', 'i': i, 'moved': False,
                         'off': (p['x'] - mx, p['y'] - my)}
            return
        self.drag = {'mode': 'rotate', 'last': (e.x, e.y), 'moved': False}

    def on_drag(self, e):
        d = self.drag
        if not d:
            return
        self.mouse = (e.x, e.y)
        if d['mode'] == 'rotate':
            d['moved'] = d['moved'] or math.hypot(
                e.x - self.press[0], e.y - self.press[1]) > 3
            self.user_view = True
            lx, ly = d['last']
            self.yaw += (e.x - lx) * 0.01
            self.pitch = min(max(self.pitch + (e.y - ly) * 0.01, 0.0),
                             math.radians(85))
            d['last'] = (e.x, e.y)
        elif d['mode'] in ('wall', 'pic'):
            d['moved'] = True
            obj = (self.walls() if d['mode'] == 'wall'
                   else self.layout['pictures'])[d['i']]
            mx, my = self.unproject(e.x, e.y, obj['z'])
            nx, ny = mx + d['off'][0], my + d['off'][1]
            if d['mode'] == 'wall':
                test = dict(obj, x=nx, y=ny)
                pos = self.positions()
                bad = any(walls_overlap(test, o) for j, o in
                          enumerate(self.walls()) if j != d['i']) or \
                    any(wall_contains_point(test, p) for p in pos.values())
                if bad:
                    self.notice = ('walls cannot overlap other walls or '
                                   'devices', time.time() + 2)
                    self.redraw()
                    return
            obj['x'], obj['y'] = round(nx, 3), round(ny, 3)
        else:
            x, y, z = d['xyz']
            if e.state & 0x0001:                       # Shift: change height
                z = z - (e.y - d['start'][1]) / self.scale
                z = max(z, 0.0)
                d['start'] = (e.x, e.y)
            else:
                x, y = self.unproject(e.x, e.y, z)
            new = (x, y, z)
            if self.in_wall(new):                      # never inside a wall
                ox, oy, oz = d['xyz']
                for cand in ((x, oy, z), (ox, y, z), d['xyz']):
                    if not self.in_wall(cand):
                        new = cand
                        break
                self.notice = ('devices cannot go inside walls',
                               time.time() + 2)
            d['xyz'] = new
            self.local[d['name']] = (new, time.time() + 2.0)
            if time.time() - d['sent'] > 0.15:         # don't flood Mininet
                self.send_move(d['name'], new)
                d['sent'] = time.time()
        self.redraw()

    def on_release(self, e):
        d, self.drag = self.drag, None
        if d and d['mode'] == 'node':
            self.send_move(d['name'], d['xyz'])
            self.local[d['name']] = (d['xyz'], time.time() + 2.0)
        elif d and d['mode'] in ('wall', 'pic') and d['moved']:
            obj = (self.walls() if d['mode'] == 'wall'
                   else self.layout['pictures'])[d['i']]
            self.send(('%s_move' % d['mode'], d['i'], obj['x'], obj['y']))
        elif d and d['mode'] == 'rotate' and not d['moved']:
            self.sel_item = None
            self.selected = None
        self.redraw()

    def on_double(self, e):
        name = self.node_at(e.x, e.y)
        if name:
            self.open_details(name)
        else:
            self.fit()

    def send(self, msg):
        try:
            self.outq.put(msg)
        except Exception:
            pass

    def send_move(self, name, xyz):
        self.send(('move', name) + tuple(round(v, 2) for v in xyz))

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

    def set_hover(self, node, wall):
        if (node, wall) != (self.hover, self.hover_wall):
            self.hover, self.hover_wall = node, wall
            if not self.v_anim.get():
                self.redraw()

    def on_motion(self, e):
        self.mouse = (e.x, e.y)
        h = self.node_at(e.x, e.y)
        wl = None
        if h is None and self.v_walls.get():
            wl = self.wall_at(e.x, e.y)
        movable_item = (wl is not None and not self.walls()[wl].get('locked')) \
            or (h is None and wl is None and self.pic_at(e.x, e.y) is not None)
        self.canvas.config(cursor='fleur' if (h and self.movable(h)) or
                           movable_item else '')
        if (h, wl) != (self.hover, self.hover_wall) or h or wl is not None:
            self.hover, self.hover_wall = h, wl
            if not self.v_anim.get():
                self.redraw()

    # --------------------------------------------------------- packets ----
    def speed(self):
        return dict(PKT_SPEEDS).get(self.pkt_speed.get(), 1.0)

    def mult(self):
        return dict(PKT_MULTS).get(self.pkt_mult.get(), 1.0)

    def update_rate_label(self):
        tr = self.data.get('traffic') or []
        if not tr:
            self.lbl_rate.config(text='no traffic')
            return
        pps = sum(t[3] for t in tr)
        bps = sum(t[4] for t in tr)
        self.lbl_rate.config(text='%d active link directions, %s' % (
            len(tr), _rate(pps, bps)))

    def update_packets(self, dt):
        """Spawn and move the packet dots (simulation clock = real time x
        speed, so 'slowed' also slows the rate at which dots appear)."""
        sp = self.speed()
        self.sim_t += dt * sp
        if not self.v_pkts.get():
            return
        if sp > 0:
            m = self.mult()
            for a, b, kind, pps, bps in self.data.get('traffic') or []:
                key = (a, b, kind)
                rate = min(pps * m * sp, PKT_MAX_RATE)
                acc = self.acc.get(key, 0.0) + rate * dt
                n = int(acc)
                self.acc[key] = acc - n
                size = bps / pps if pps else 100.0
                for i in range(n):
                    if len(self.dots) >= PKT_MAX:
                        break
                    self.dots.append((a, b, kind,
                                      self.sim_t - dt * sp * i / max(n, 1),
                                      size))
        self.dots = [d for d in self.dots
                     if self.sim_t - d[3] <= PKT_TRAVEL]

    def draw_packets(self, pos):
        cv = self.canvas
        col = {'wifi': C['pkt_wifi'], 'wired': C['pkt_wired'],
               'adhoc': C['pkt_peer'], 'mesh': C['pkt_peer']}
        for a, b, kind, t0, size in self.dots:
            if a not in pos or b not in pos:
                continue
            f = max(0.0, min(1.0, (self.sim_t - t0) / PKT_TRAVEL))
            x1, y1, _ = self.project(*pos[a])
            x2, y2, _ = self.project(*pos[b])
            # lanes: each direction slightly offset so both are visible
            L = math.hypot(x2 - x1, y2 - y1) or 1
            ox, oy = -(y2 - y1) / L * 3, (x2 - x1) / L * 3
            x, y = x1 + (x2 - x1) * f + ox, y1 + (y2 - y1) * f + oy
            r = 2.5 if size < 200 else (3.5 if size < 1000 else 4.5)
            cv.create_oval(x - r, y - r, x + r, y + r,
                           fill=col.get(kind, C['pkt_wired']),
                           outline=C['bg'])

    def draw_rates(self, pos):
        pairs = {}
        for a, b, kind, pps, bps in self.data.get('traffic') or []:
            if a in pos and b in pos:
                k = tuple(sorted((a, b)))
                v = pairs.setdefault(k, [0.0, 0.0])
                v[0] += pps
                v[1] += bps
        for (a, b), (pps, bps) in pairs.items():
            x1, y1, _ = self.project(*pos[a])
            x2, y2, _ = self.project(*pos[b])
            self.canvas.create_text((x1 + x2) / 2, (y1 + y2) / 2 + 13,
                                    text=_rate(pps, bps), fill=C['ap'],
                                    font=('TkDefaultFont', 8, 'bold'))

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
        cv.configure(bg=C['bg'])
        t = time.time() - self.t0
        anim = self.v_anim.get()
        pos = self.positions()
        nodes = {n['name']: n for n in self.data['nodes']}

        self.pic_hits = []
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
        show_links = self.v_links.get()
        for link in (self.data['links'] if show_links else ()):
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
            if n['kind'] in CLIENT_KINDS and n['ap'] in pos and name in pos \
                    and show_links:
                d = math.dist(pos[name][:3], pos[n['ap']][:3])
                ok = d <= max(nodes[n['ap']]['range'], 1e-9)
                wl, hit = wall_loss(self.walls(), pos[name], pos[n['ap']])
                self.draw_rf(pos[name], pos[n['ap']], t if anim else 0, ok,
                             n.get('rssi'), len(hit), wl)
        if self.v_pkts.get():
            self.draw_packets(pos)
        if self.v_rates.get():
            self.draw_rates(pos)

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
                self.wall_hits.append((it[3], it[4]))
        for fn in self._wall_late:                  # labels, ghost tops
            fn()
        self.draw_selection()

        self.draw_overlay()
        self.update_status(pos, nodes)

    def draw_selection(self):
        if not self.sel_item:
            return
        kind, i = self.sel_item
        cv = self.canvas
        try:
            if kind == 'wall':
                w = self.walls()[i]
                pts = [self.project(x, y, w['z'] + max(float(w['height']), 0))
                       [:2] for x, y in wall_corners(w)]
            else:
                pts = [self.project(*c)[:2] for c in
                       self.pic_corners(self.layout['pictures'][i])]
        except (IndexError, KeyError):
            self.sel_item = None
            return
        cv.create_polygon([v for q in pts for v in q], fill='',
                          outline=C['ap'], width=2, dash=(6, 3))

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
        self._wall_late, self.wall_hits = [], []
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
        for wi, w in enumerate(walls):
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
                self.wall_hits.append((wi, pts))
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
                pts = [q[:2] for q in sp]
                flat = [v for q in pts for v in q]
                depth = sum(q[2] for q in sp) / len(sp)

                def draw(flat=flat, fill=fill, stipple=stipple, dash=dash,
                         dark=dark):
                    kw = {'stipple': stipple} if stipple and fill else {}
                    self.canvas.create_polygon(flat, fill=fill, outline=dark,
                                               width=1, dash=dash, **kw)
                items.append((depth, 'face', draw, wi, pts))
            if labels:
                lx, ly, _ = self.project(w['x'], w['y'], z0 + top_h)
                self._wall_late.append(lambda lx=lx, ly=ly, w=w, d=dark:
                                       self.wall_label(w, lx, ly - 9, d))
        return items

    def wall_label(self, w, x, y, col):
        self.canvas.create_text(
            x, y, text='%s · %s %s dB%s' % (
                w['name'], w['material'], _g(wall_db(w)),
                '' if w.get('locked') else '  (movable)'),
            fill=col if not self.v_dark.get() else _mix(col, '#ffffff', 0.5),
            font=('TkDefaultFont', 8, 'bold'))

    def wall_connections(self, w):
        """Connections (Wi-Fi associations, ad-hoc / mesh) crossing w."""
        pos = self.positions()
        out = []
        for n in self.data['nodes']:
            if n['kind'] in CLIENT_KINDS and n.get('ap') in pos and \
                    n['name'] in pos:
                if wall_crossed(w, pos[n['name']], pos[n['ap']]):
                    out.append('%s -> %s%s' % (
                        n['name'], n['ap'],
                        ('  (RSSI %.1f dBm)' % n['rssi'])
                        if n.get('rssi') is not None else ''))
        for link in self.data['links']:
            if link[2] in ('adhoc', 'mesh') and link[0] in pos and \
                    link[1] in pos and wall_crossed(w, pos[link[0]],
                                                    pos[link[1]]):
                out.append('%s <-> %s  (%s)' % (link[0], link[1], link[2]))
        return out

    # --------------------------------------------------------- pictures ----
    def pic_source(self, pic, reduce=1):
        key = (pic.get('path'), reduce, pic_edits_key(pic))
        if key in self.pic_src:
            return self.pic_src[key]
        img = None
        try:
            from PIL import Image
            if reduce == 1:
                raw = self.pic_raw.get(pic['path'])
                if raw is None:
                    im = Image.open(pic['path'])
                    im.load()
                    im = im.convert('RGBA')
                    m = max(im.size)
                    if m > 2048:
                        k = 2048.0 / m
                        im = im.resize((max(1, int(im.size[0] * k)),
                                        max(1, int(im.size[1] * k))),
                                       Image.LANCZOS)
                    raw = self.pic_raw[pic['path']] = im
                img = apply_pic_edits(raw, pic)
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
        key = (pic_edits_key(pic), k, x1 - x0, y1 - y0, op,
               tuple(round(c, 6) for c in coef))
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
        for end, label, col in ((ex, 'x', C['x']), (ey, 'y', C['y']),
                                (ez, 'z', C['z'])):
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
        dark = self.v_dark.get()

        if not ok:
            color, glow = '#cc3a2f', '#f3d0cc'
        elif rssi is None or rssi >= -50:
            color, glow = '#16a05a', '#cdebd9'          # excellent
        elif rssi >= -65:
            color, glow = '#7cb342', '#dcedc8'          # good
        elif rssi >= -75:
            color, glow = '#f39c12', '#fdebd0'          # fair
        else:
            color, glow = '#cc3a2f', '#f3d0cc'          # poor
        if dark:
            glow = _mix(glow, C['bg'], 0.8)

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
        walls_txt = ('  %d wall%s -%s dB' % (nwalls, '' if nwalls == 1 else 's',
                                             _g(wl))) if nwalls else ''
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

    def draw_node(self, n, x, y):
        cv = self.canvas
        kind, name = n['kind'], n['name']
        hl = (self.hover == name) or (self.drag and self.drag.get('name') == name)
        if name == self.selected:
            cv.create_oval(x - 19, y - 19, x + 19, y + 19, fill='',
                           outline=C['select'], dash=(2, 3))
        draw_icon(cv, kind, x, y, ow=C['select'] if hl else C['outline'])
        if kind == 'ap' and self.v_badges.get():
            cnt = len(self.clients_of(name))
            bx, by = x + 15, y - 13
            cv.create_oval(bx - 8, by - 8, bx + 8, by + 8,
                           fill=C['rf'] if cnt else C['axis'],
                           outline=C['outline'], width=1.5)
            cv.create_text(bx, by, text=str(cnt), fill='white',
                           font=('TkDefaultFont', 8, 'bold'))
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
            draw_icon(cv, kind, x + 7, y + (3 if kind == 'ap' else 0),
                      ow=C['outline'], s=0.55)
            cv.create_text(x + 18, y, text=text, anchor='w', fill=C['text'],
                           font=('TkDefaultFont', 9))
            x += 26 + 7 * len(text)
        for kind, text, dash, col, wd in (
                ('rf', 'Wireless', None, C['rf'], 2),
                ('peer', 'Ad-hoc / mesh', (6, 4), C['peer'], 2),
                ('wired', 'Cable', None, C['wired'], 3),
                ('control', 'Control', (6, 4), C['control'], 2),
                ('wall', 'Wall', None, C['wall'], 6),
                ('pkt', 'Packet', None, C['pkt_wifi'], 0)):
            if kind == 'rf':
                pts = []
                for i in range(13):
                    pts += [x + i * 2, y + 3 * math.sin(i * 1.1)]
                cv.create_line(*pts, fill=col, width=wd, smooth=True)
            elif kind == 'pkt':
                for i, c in enumerate((C['pkt_wifi'], C['pkt_wired'],
                                       C['pkt_peer'])):
                    cv.create_oval(x + i * 8, y - 3, x + i * 8 + 6, y + 3,
                                   fill=c, outline='')
            else:
                cv.create_line(x, y, x + 24, y, fill=col, width=wd, dash=dash)
            cv.create_text(x + 30, y, text=text, anchor='w', fill=C['text'],
                           font=('TkDefaultFont', 9))
            x += 40 + 7 * len(text)
        # help
        cv.create_text(12, h - 12, anchor='sw', fill=C['muted'],
                       font=('TkDefaultFont', 8),
                       text='drag: rotate   right-drag: pan   wheel: zoom   '
                            'drag device: move   Shift+drag: height   '
                            'double-click device: details   D: devices list'
                            '   T/3/F: top/3D/fit')
        # tooltip
        if not (self.drag and self.drag.get('mode') == 'rotate'):
            if self.hover:
                self.draw_tip(self.node_tip(self.hover))
            elif self.hover_wall is not None:
                self.draw_tip(self.wall_tip(self.hover_wall))

    def node_tip(self, name, advanced=None):
        """Tooltip lines. Basic: who / where / connection. Advanced: every
        detail the editor shows, with the values Mininet-WiFi simulates."""
        if advanced is None:
            advanced = self.tip_mode.get() == 'advanced'
        n = self.node(name)
        pos = self.positions()
        p = pos.get(name)
        if not n or not p:
            return []
        info = n.get('info') or {}
        L = ['%s  (%s)' % (n['name'], KIND_LABEL.get(n['kind'], n['kind'])),
             'position: %.1f, %.1f, %.1f' % tuple(p)]
        if n['kind'] in CLIENT_KINDS:
            L.append('connected to: %s' % (n['ap'] or 'nothing'))
            L.append('RSSI (simulated): %.1f dBm' % n['rssi']
                     if n.get('rssi') is not None else 'RSSI: unavailable')
        if n['kind'] == 'ap':
            cl = self.clients_of(name)
            L.append('connected devices: %d%s' % (
                len(cl), ('  (%s)' % ', '.join(c['name'] for c in cl))
                if cl else ''))
        if info.get('ip') and not advanced:
            L.append('ip: %s' % info['ip'])
        if not advanced:
            return L
        L.append('class: %s' % info.get('class', '?'))
        if n['range'] > 0:
            L.append('range: %s m' % _g(n['range']))
        for e in info.get('wintfs') or []:
            bits = [e['name']]
            for k, lab in (('mode', 'mode'), ('ssid', 'ssid'),
                           ('channel', 'ch'), ('freq', 'freq'),
                           ('txpower', 'txpower'), ('antennaGain', 'gain'),
                           ('ip', 'ip'), ('mac', 'mac')):
                if e.get(k) not in (None, ''):
                    bits.append('%s=%s' % (lab, _g(e[k])))
            if e.get('assoc'):
                bits.append('-> %s' % e['assoc'])
            L.append('  ' + '  '.join(bits))
        for e in info.get('intfs') or []:
            L.append('  %s  ip=%s  mac=%s' % (e['name'], e.get('ip') or '-',
                                              e.get('mac') or '-'))
        for k in ('dpid', 'failMode', 'protocols'):
            if info.get(k):
                L.append('%s: %s' % (k, info[k]))
        if n['kind'] == 'ctrl' and info.get('ip'):
            L.append('controller: %s:%s %s' % (info['ip'], _g(info.get(
                'port', '')), info.get('protocol', '')))
        if n['kind'] in CLIENT_KINDS and n.get('ap') in pos:
            ap = self.node(n['ap'])
            d = math.dist(p, pos[n['ap']])
            L.append('distance to %s: %s m  (AP range %s m)' % (
                n['ap'], _g(d), _g(ap['range'] if ap else 0)))
            wl, hit = wall_loss(self.walls(), p, pos[n['ap']])
            L.append('walls on the path: %s' % (
                '%s  (-%s dB)' % (', '.join(w['name'] for w in hit), _g(wl))
                if hit else 'none'))
        if n['kind'] == 'ap':
            for c in self.clients_of(name):
                if c['name'] in pos:
                    wl, hit = wall_loss(self.walls(), pos[c['name']], p)
                    L.append('  %s  %s m  RSSI %s%s' % (
                        c['name'], _g(math.dist(pos[c['name']], p)),
                        ('%.1f dBm' % c['rssi']) if c.get('rssi') is not None
                        else '?', ('  %d wall(s) -%s dB' % (len(hit), _g(wl)))
                        if hit else ''))
        out = [t for t in self.data.get('traffic') or [] if t[0] == name]
        inn = [t for t in self.data.get('traffic') or [] if t[1] == name]
        if out or inn:
            L.append('traffic out: %s' % _rate(sum(t[3] for t in out),
                                               sum(t[4] for t in out)))
            L.append('traffic in:  %s' % _rate(sum(t[3] for t in inn),
                                               sum(t[4] for t in inn)))
        if name in (self.layout.get('locked_devices') or []):
            L.append('locked in the editor file')
        return L

    def wall_tip(self, i):
        try:
            w = self.walls()[i]
        except IndexError:
            return []
        cr = self.wall_connections(w)
        L = ['%s  (wall, %s)' % (w['name'], w['material']),
             'loss: %s dB%s' % (_g(wall_db(w)), (
                 '  (base %s dB, thickness x%s)' % (_g(w['loss']),
                                                    _g(w['thick_mult'])))
                 if w.get('thick_mult') and w['material'] != 'custom' else ''),
             '%s x %s m, %s' % (_g(w['length']), _g(w['thickness']),
                                ('%s m high from z=%s' % (_g(w['height']),
                                                         _g(w['z'])))
                                if float(w['height']) > 0 else 'flat 2D wall'),
             'connections through it: %d' % len(cr)]
        L += ['  ' + c for c in cr[:12]]
        if len(cr) > 12:
            L.append('  ... %d more' % (len(cr) - 12))
        L.append('locked (fixed)' if w.get('locked') else
                 'unlocked: drag to move it')
        if w.get('group'):
            L.append('group: %s' % w['group'])
        return L

    def draw_tip(self, lines):
        if not lines:
            return
        cv = self.canvas
        mx, my = self.mouse
        tid = cv.create_text(mx + 16, my - 12, text='\n'.join(lines),
                             anchor='sw', fill=C['tip_fg'],
                             font=('TkDefaultFont', 9))
        b = cv.bbox(tid)
        W, H = self.size()
        dx = min(0, W - 6 - b[2])                  # keep it on screen
        dy = max(0, 6 - b[1])
        if dx or dy:
            cv.move(tid, dx, dy)
            b = cv.bbox(tid)
        bg = cv.create_rectangle(b[0] - 7, b[1] - 5, b[2] + 7, b[3] + 5,
                                 fill=C['tip_bg'], outline='')
        cv.tag_raise(tid, bg)

    def update_status(self, pos, nodes):
        lines, warn = [], False
        txt, until = self.notice
        if txt and until > time.time():
            lines.append('>  ' + txt)
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
                                    ('   through %s (-%s dB)' % (
                                        ', '.join(w['name'] for w in hit),
                                        _g(wl))) if hit else ''))
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
        text = '\n'.join(lines[:6]) or ' '
        if text != self.status.cget('text'):
            self.status.config(text=text, fg=C['warn'] if warn else C['text'])

    # ---------------------------------------------------- devices list ----
    def open_tree(self):
        if self.tree and self.tree.alive:
            self.tree.top.lift()
            self.tree.refresh()
        else:
            self.tree = _TreePanel(self.tk, self.root, self)

    def draw_kind_icon(self, cv, kind, x, y):
        draw_icon(cv, kind, x, y + (3 if kind == 'ap' else 0), s=0.6,
                  ow=C['outline'])

    def tree_rows(self):
        nodes = [(n['name'], n['kind']) for n in self.data['nodes']]
        edges = []
        for link in self.data['links']:
            edges.append((link[0], link[1], link[2],
                          link[3] if len(link) > 3 else ''))
        for n in self.data['nodes']:
            if n['kind'] in CLIENT_KINDS and n.get('ap'):
                edges.append((n['ap'], n['name'], 'wifi',
                              ('%.1f dBm' % n['rssi'])
                              if n.get('rssi') is not None else ''))
        rows = build_tree(nodes, edges)
        tr = self.data.get('traffic') or []
        for r in rows:
            n = self.node(r['name'])
            info = n.get('info') or {}
            bits = [KIND_LABEL.get(n['kind'], n['kind'])]
            if info.get('ip'):
                bits.append(str(info['ip']))
            if n['kind'] == 'ap':
                bits.append('%d clients' % len(self.clients_of(n['name'])))
            if n['kind'] in CLIENT_KINDS and not n.get('ap') and \
                    not any(l[2] in ('adhoc', 'mesh') and n['name'] in l[:2]
                            for l in self.data['links']):
                bits.append('not connected')
            pk = sum(t[3] for t in tr if r['name'] in t[:2])
            if pk:
                bits.append('%s pkt/s' % _g(pk))
            r['detail'] = '  '.join(bits)
        return rows

    def tree_select(self, row):
        self.selected = row['name']
        self.redraw()

    def tree_activate(self, row):
        self.go_to(row['name'])
        self.open_details(row['name'])

    def tree_menu(self, m, row):
        name = row['name']
        n = self.node(name)
        m.add_command(label='Go to %s' % name, command=lambda: self.go_to(name))
        m.add_command(label='Details...', command=lambda: self.open_details(
            name))
        if n and n['kind'] in CLIENT_KINDS:
            m.add_separator()
            sub = self.tk.Menu(m, tearoff=0, bg=C['btn'], fg=C['text'],
                               activebackground=C['ap'],
                               activeforeground='white')
            for ap in (x for x in self.data['nodes'] if x['kind'] == 'ap'):
                sub.add_command(label=ap['name'] + (
                    '  (current)' if ap['name'] == n.get('ap') else ''),
                    command=lambda a=ap['name']: self.request(
                        ('assoc', name, a),
                        'asking %s to associate with %s...' % (name, a)))
            m.add_cascade(label='Associate with', menu=sub)
            m.add_command(label='Disconnect', command=lambda: self.request(
                ('disassoc', name), 'disconnecting %s...' % name))
        m.add_separator()
        m.add_command(label='Live config (not saved)...',
                      command=lambda: self.open_live(name))
        m.add_command(label='Open terminal (xterm)', command=lambda: self.request(
            ('term', name), 'opening a terminal for %s...' % name))

    def request(self, msg, text):
        self.notice = (text, time.time() + 6)
        self.send(msg)
        self.redraw()

    def open_details(self, name):
        """Live details window (advanced tooltip) for one device."""
        old = self.details.get(name)
        if old is not None and old.winfo_exists():
            old.lift()
            return
        tk = self.tk
        top = tk.Toplevel(self.root)
        top.title('%s - details' % name)
        top.configure(bg=C['bg'])
        top.geometry('430x420')
        txt = tk.Text(top, bg=C['btn'], fg=C['text'], relief='flat',
                      font=('TkFixedFont', 9), highlightthickness=0,
                      padx=8, pady=6, wrap='none')
        bar = tk.Frame(top, bg=C['bar'], padx=6, pady=6)
        bar.pack(side='bottom', fill='x')
        txt.pack(fill='both', expand=True)
        self._btn(bar, 'Go to', lambda: self.go_to(name))
        self._btn(bar, 'Terminal', lambda: self.request(
            ('term', name), 'opening a terminal for %s...' % name))
        self._btn(bar, 'Live config', lambda: self.open_live(name))
        self._btn(bar, 'Close', top.destroy, side='right')
        self.details[name] = top

        def refresh():
            if not top.winfo_exists():
                return
            lines = self.node_tip(name, advanced=True) or ['(gone)']
            body = '\n'.join(lines)
            if txt.get('1.0', 'end-1c') != body:
                txt.config(state='normal')
                txt.delete('1.0', 'end')
                txt.insert('1.0', body)
                txt.config(state='disabled')
            top.after(500, refresh)
        refresh()

    # ------------------------------------------------- camera / props ----
    def save_view(self):
        self.request(('save_view', camera_state(self)),
                     'saving the view in the topology file...')

    def restore_view(self):
        cam = (self.layout.get('view') or {}).get('saved')
        if cam and apply_camera(self, cam):
            self.notice = ('saved view restored', time.time() + 4)
        else:
            self.notice = ('no saved view in the topology file yet',
                           time.time() + 4)
        self.redraw()

    def on_close(self):
        try:
            self.props.set('window', 'geometry', self.root.geometry())
            self.props.set('window', 'ribbon_tab', self.ribbon.current)
            self.props.set('window', 'ribbon_collapsed', self.ribbon.collapsed)
            self.props.save()
        except Exception:
            pass
        self.root.destroy()

    # --------------------------------------------- live config / CLI ----
    def live_for_selected(self):
        name = self.selected or self.hover
        if not name:
            self.notice = ('pick a device in the Devices list (or click its '
                           'row) first', time.time() + 5)
            return self.open_tree()
        self.open_live(name)

    def open_live(self, name):
        w = self.live_wins.get(name)
        if w is not None and w.top.winfo_exists():
            return w.top.lift()
        self.live_wins[name] = _LiveConfig(self, name)

    def open_cli(self):
        if self.cli_win is not None and self.cli_win.top.winfo_exists():
            return self.cli_win.top.lift()
        self.cli_win = _CliWindow(self)

    def run(self):
        self.root.mainloop()


class _LiveConfig(object):
    """Change a running device for testing - nothing is saved."""
    WIRELESS = (('range', 'Range (m)'), ('txpower', 'Tx power (dBm)'),
                ('antennaGain', 'Antenna gain (dBi)'),
                ('antennaHeight', 'Antenna height (m)'),
                ('channel', 'Channel'), ('mode', 'Mode (a b g n ac ax)'))

    def __init__(self, win, name):
        tk = win.tk
        self.win, self.name = win, name
        n = win.node(name) or {'kind': '?', 'info': {}}
        info = n.get('info') or {}
        w0 = (info.get('wintfs') or [{}])[0]
        top = self.top = tk.Toplevel(win.root)
        top.title('%s - live config' % name)
        top.configure(bg=C['bg'])
        head = tk.Frame(top, bg=C['bar'], padx=10, pady=8)
        head.pack(fill='x')
        ic = tk.Canvas(head, width=34, height=30, bg=C['bar'],
                       highlightthickness=0)
        draw_icon(ic, n['kind'], 17, 17 + (3 if n['kind'] == 'ap' else 0),
                  ow=C['outline'])
        ic.pack(side='left')
        tk.Label(head, text='%s  -  live configuration' % name, bg=C['bar'],
                 fg=C['text'], font=('TkDefaultFont', 11, 'bold')).pack(
            side='left', padx=8)
        tk.Label(top, text='Changes go to the running network only, for '
                 'testing. Nothing is saved to any file.', bg=C['bg'],
                 fg=C['warn'], font=('TkDefaultFont', 8)).pack(anchor='w',
                                                              padx=10)
        body = tk.Frame(top, bg=C['bg'], padx=12, pady=6)
        body.pack(fill='both', expand=True)
        self.vars, self.orig, r = {}, {}, 0

        def section(t):
            nonlocal r
            tk.Label(body, text=t, bg=C['bg'], fg=C['muted'],
                     font=('TkDefaultFont', 8, 'bold')).grid(
                row=r, column=0, columnspan=2, sticky='w', pady=(8, 2))
            r += 1

        def field(key, label, value):
            nonlocal r
            tk.Label(body, text=label, bg=C['bg'], fg=C['text']).grid(
                row=r, column=0, sticky='w', pady=2, padx=(0, 10))
            v = tk.StringVar(value='' if value is None else _g(value)
                             if isinstance(value, float) else str(value))
            tk.Entry(body, textvariable=v, width=18, relief='flat',
                     bg=C['btn'], fg=C['text'], insertbackground=C['text'],
                     highlightthickness=1, highlightbackground=C['grid_major']
                     ).grid(row=r, column=1, sticky='w')
            self.vars[key], self.orig[key] = v, v.get()
            r += 1
        pos = win.positions().get(name)
        section('POSITION')
        field('position', 'x, y, z (m)', ', '.join(_g(c) for c in pos)
              if pos else '')
        if n['kind'] in ('ap', 'sta', 'car'):
            section('RADIO  (%s)' % w0.get('name', 'wlan'))
            for key, label in self.WIRELESS:
                field(key, label, n.get('range') if key == 'range'
                      else w0.get(key))
        if n['kind'] in ('sta', 'car', 'host'):
            section('ADDRESSING')
            field('ip', 'IP / mask', w0.get('ip') or info.get('ip'))
        ifnames = [e['name'] for e in (info.get('intfs') or [])] + \
            [e['name'] for e in (info.get('wintfs') or [])]
        self.v_if = tk.StringVar(value=ifnames[0] if ifnames else '')
        if ifnames:
            section('TRAFFIC CONTROL  (tc / netem)')
            tk.Label(body, text='Interface', bg=C['bg'], fg=C['text']).grid(
                row=r, column=0, sticky='w')
            win.ttk.Combobox(body, textvariable=self.v_if, values=ifnames,
                             state='readonly', width=16).grid(
                row=r, column=1, sticky='w')
            r += 1
            for key, label in (('bw', 'Bandwidth (Mbit/s)'),
                               ('delay', 'Delay (e.g. 20ms)'),
                               ('jitter', 'Jitter (e.g. 5ms)'),
                               ('loss', 'Loss (%)')):
                field('tc_' + key, label, '')
        bar = tk.Frame(top, bg=C['bar'], padx=6, pady=6)
        bar.pack(fill='x', side='bottom')
        win._btn(bar, 'Apply', self.apply, accent=True)
        win._btn(bar, 'Close', top.destroy, side='right')

    def apply(self):
        ch, tc = {}, {}
        for k, v in self.vars.items():
            val = v.get().strip()
            if val == self.orig[k] or val == '':
                continue
            if k == 'position':
                try:
                    ch[k] = [float(c) for c in val.replace(',', ' ').split()]
                    if len(ch[k]) != 3:
                        raise ValueError
                    if self.win.in_wall(ch[k]):
                        self.win.notice = ('that position is inside a wall',
                                           time.time() + 4)
                        ch.pop(k)
                except ValueError:
                    ch.pop(k, None)
            elif k.startswith('tc_'):
                tc[k[3:]] = val
            else:
                ch[k] = val
        if ch:
            self.win.request(('live', self.name, ch), 'applying %s to %s...'
                             % (', '.join(ch), self.name))
            if 'position' in ch:
                self.win.local[self.name] = (tuple(ch['position']),
                                             time.time() + 2.0)
        if tc and self.v_if.get():
            self.win.request(('live_tc', self.name, self.v_if.get(), tc),
                             'traffic control on %s...' % self.v_if.get())
        for k, v in self.vars.items():
            self.orig[k] = v.get().strip()


class _CliWindow(object):
    """A Mininet CLI inside the GUI: commands run in the Mininet process,
    their output is shown here."""

    def __init__(self, win):
        tk = win.tk
        self.win, self.hist, self.hpos, self.busy = win, [], 0, False
        top = self.top = tk.Toplevel(win.root)
        top.title('Mininet-WiFi CLI')
        top.configure(bg='#16171a')
        top.geometry('760x460')
        self.txt = tk.Text(top, bg='#16171a', fg='#d8d8d4', relief='flat',
                           insertbackground='#d8d8d4', highlightthickness=0,
                           font=('TkFixedFont', 10), padx=8, pady=6,
                           wrap='char')
        self.txt.tag_configure('cmd', foreground='#6fa0ff')
        self.txt.tag_configure('info', foreground='#8a8c93')
        bar = tk.Frame(top, bg='#22242a', padx=6, pady=6)
        bar.pack(side='bottom', fill='x')
        self.prompt = tk.Label(bar, text='mininet-wifi>', bg='#22242a',
                               fg='#6fa0ff', font=('TkFixedFont', 10, 'bold'))
        self.prompt.pack(side='left')
        self.v = tk.StringVar()
        self.ent = tk.Entry(bar, textvariable=self.v, bg='#16171a',
                            fg='#d8d8d4', insertbackground='#d8d8d4',
                            relief='flat', font=('TkFixedFont', 10),
                            highlightthickness=0)
        self.ent.pack(side='left', fill='x', expand=True, padx=6)
        tk.Button(bar, text='Clear', relief='flat', bg='#2c2f36',
                  fg='#d8d8d4', command=lambda: self.txt.delete('1.0', 'end')
                  ).pack(side='right')
        sb = tk.Scrollbar(top, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self.txt.pack(fill='both', expand=True)
        self.ent.bind('<Return>', self.send)
        self.ent.bind('<KP_Enter>', self.send)
        self.ent.bind('<Up>', lambda e: self.recall(-1))
        self.ent.bind('<Down>', lambda e: self.recall(1))
        self.append('Mininet-WiFi CLI inside the viewer. Commands run in your '
                    'Mininet process\n(same as the CLI: nodes, net, pingall, '
                    'sta1 ping -c 3 sta2, iperf, py ...).\nUse it while the '
                    'real CLI is waiting at its prompt.\n\n', 'info')
        self.ent.focus_set()

    def append(self, text, tag=None):
        self.txt.insert('end', text, tag)
        self.txt.see('end')

    def send(self, e=None):
        line = self.v.get()
        if not line.strip() or self.busy:
            return
        self.hist.append(line)
        self.hpos = len(self.hist)
        self.v.set('')
        self.append('mininet-wifi> %s\n' % line, 'cmd')
        self.busy = True
        self.prompt.config(text='running...  ')
        self.win.send(('cli', line))

    def done(self):
        self.busy = False
        self.prompt.config(text='mininet-wifi>')

    def recall(self, d):
        if not self.hist:
            return
        self.hpos = max(0, min(len(self.hist), self.hpos + d))
        self.v.set(self.hist[self.hpos] if self.hpos < len(self.hist) else '')
        self.ent.icursor('end')


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