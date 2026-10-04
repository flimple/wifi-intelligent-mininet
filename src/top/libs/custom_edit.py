#!/usr/bin/env python3
"""
wifi_edit.py  -  MiniEdit-style topology editor for Mininet-WiFi

Same look as wifi_gui.py (pure Tkinter, no matplotlib), but it is an
EDITOR: no simulation, no animation. You draw the network, set every
parameter, and save a file that Mininet-WiFi can run.

    sudo apt install python3-tk        (if Tkinter is missing)

What you get
  - 3D view you can rotate, pan and zoom, plus a flat top view
  - Menu bar (File / Edit / Insert / View / Help) and a toolbar of
    captioned groups (FILE, EDIT, DEVICES, CONNECT, PICTURE, VIEW, SHOW,
    RANGE) that wraps onto more rows when the window is narrow
  - Place access points, stations, cars, hosts, switches and controllers;
    connect them with links: wired cable, Wi-Fi (station/car -> AP),
    ad-hoc and mesh (station <-> station)
  - AP client badges: each AP shows how many devices are connected to it
    (Wi-Fi links + stations that would auto-associate because they are in
    its range). Details in the tooltip and in the Inspector
  - Walls: BUILD > Wall, drag a zone on the ground, then set its
    properties (material concrete / brick / wood / drywall / glass / metal
    with preset dB loss, or custom loss + color; height: 0 = flat 2D wall,
    > 0 = 3D wall; base Z for upper floors). Select a wall to move it,
    stretch its ends, change thickness, rotate it or drag its height;
    Ctrl+D duplicates, Del deletes. Each wall crossed subtracts its loss
    from the RSSI: in the editor's estimates (labels, AP clients) and in
    the simulation (tc mode and wmediumd SNR mode - wmediumd interference
    mode computes signals itself and cannot see walls)
  - WALLS > camera: walls always visible, front walls transparent, front
    walls cut down (like building games), or outlines. SHOW > Walls hides
    them. SHOW > Ranges > outline only draws just the range boundaries
  - Pictures (floor plans, maps): Insert > Picture, then drag to move,
    corner squares to scale, round handle to rotate; Inspector sets
    position, floor height, width, rotation, opacity, visible, locked.
    Needs Pillow (sudo apt install python3-pil python3-pil.imagetk).
    Pictures are saved in the JSON (editor section) and ignored by Mininet
  - Range sphere / circle for every wireless node, and a range slider
  - Click a device: it gets local X / Y / Z axes. Drag an arrow to move the
    device along that axis only. Drag the device itself to move it on the
    ground plane, Shift + drag to change its height
  - "Inspector" button: a window that edits everything about the selected
    device or link (name, position, range, band, channel, mode, tx power,
    antenna, IP / MAC, security, OpenFlow, controller, link bw/delay...).
    With nothing selected it edits the network-wide settings (propagation
    model, wmediumd, association control, IP base...)
  - Labels with channel / band / range, undo / redo, snap to grid

Files
  Save asks which format to write:
    normalized  JSON whose "params" are exactly the keyword arguments of
                Mininet-WiFi:  net.addAccessPoint(name, **params) ...
                Ready to run, nothing to interpret.
    custom      JSON grouped by topic (radio / addressing / security /
                openflow ...), typed values, every field present (null when
                unset). Meant for your own interpreter.
  Export .py writes a stand-alone Mininet-WiFi script.
  Both JSON formats can be opened again in the editor.

Usage
  python3 wifi_edit.py                          open the editor
  python3 wifi_edit.py topo.json                open a file in the editor
  sudo -E python3 wifi_edit.py --run topo.json  build it and run it with
                                                Mininet-WiFi (+ CLI)
       add --gui to also open wifi_gui.py on top of the running network
  python3 wifi_edit.py --export topo.json out.py
                                                convert a file to a script

  From your own script:
      from wifi_edit import load_normalized, build_network
      net = build_network(load_normalized('topo.json'))   # built + started
      CLI(net); net.stop()

Mouse & keys
  click a device ........... select it (shows its local axes)
  drag a device ............ move it on the ground    Shift + drag .. height
  drag an axis arrow ....... move along X, Y or Z only
  drag empty space ......... rotate        right-drag ..... pan
  mouse wheel .............. zoom          double-click ... inspector / fit
  AP / Station / ... tool .. click the grid to place a device
  Link tool ................ drag from one device to another
  picture .................. drag: move   corners: scale   round: rotate
  Wall tool ................ drag a zone; selected wall: drag = move,
                             ends = length, side = thickness, round =
                             rotate, blue diamond = height
  Del delete   Esc select tool   I inspector   L link tool   P picture
  W wall tool   Ctrl+D duplicate wall
  T top view   3 3D view   F fit   Ctrl+Z / Ctrl+Y undo / redo
  Ctrl+S save  Ctrl+Shift+S save as  Ctrl+O open  Ctrl+N new  Ctrl+E export
"""

import ast
import copy
import datetime
import ipaddress
import json
import keyword
import math
import os
import re
import sys


# ==========================================================================
# Part 1 - the data model (no Tkinter, no Mininet: usable anywhere)
# ==========================================================================

FORMAT = 'mininet-wifi-topology'
VERSION = 1
GENERATOR = 'wifi_edit.py'

KINDS = ('ap', 'sta', 'car', 'host', 'switch', 'ctrl')
WIRELESS_KINDS = ('ap', 'sta', 'car')
LINK_KINDS = ('wired', 'wifi', 'adhoc', 'mesh')
LINK_LABEL = {'wired': 'Wired (cable)', 'wifi': 'Wi-Fi (station to AP)',
              'adhoc': 'Ad-hoc (station to station)',
              'mesh': 'Mesh (station to station)'}
LINK_HELP = {'wired': 'cable between any two', 'wifi': 'station / car -> AP',
             'adhoc': 'station <-> station', 'mesh': 'station <-> station'}

KIND_LABEL = {'ap': 'access point', 'sta': 'station', 'car': 'car',
              'host': 'host', 'switch': 'switch', 'ctrl': 'controller'}
KIND_NAME = {'ap': 'access_point', 'sta': 'station', 'car': 'car',
             'host': 'host', 'switch': 'switch', 'ctrl': 'controller'}
KIND_FROM_NAME = dict((v, k) for k, v in KIND_NAME.items())
KIND_FROM_NAME.update(dict((k, k) for k in KINDS))
PREFIX = {'ap': 'ap', 'sta': 'sta', 'car': 'car', 'host': 'h',
          'switch': 's', 'ctrl': 'c'}
LIST_KEY = {'ap': 'accessPoints', 'sta': 'stations', 'car': 'cars',
            'host': 'hosts', 'switch': 'switches', 'ctrl': 'controllers'}
ADD_CALL = {'ap': 'addAccessPoint', 'sta': 'addStation', 'car': 'addCar',
            'host': 'addHost', 'switch': 'addSwitch', 'ctrl': 'addController'}

CLASS_NAMES = ('OVSKernelAP', 'UserAP', 'OVSKernelSwitch', 'OVSSwitch',
               'UserSwitch', 'Controller', 'RemoteController',
               'OVSController')

CHANNELS = {
    '2.4': [str(c) for c in range(1, 15)],
    '5': [str(c) for c in (36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108,
                           112, 116, 120, 124, 128, 132, 136, 140, 144, 149,
                           153, 157, 161, 165)],
    '6': [str(c) for c in range(1, 234, 4)],
}
MODES = {'2.4': ['g', 'b', 'n', 'ax'], '5': ['a', 'n', 'ac', 'ax'],
         '6': ['ax']}
ALL_MODES = ['', 'a', 'b', 'g', 'n', 'ac', 'ax']
ENCRYPT = ['', 'wpa', 'wpa2', 'wpa3', 'wep']
FAILMODES = ['', 'secure', 'standalone']
PROTOCOLS = ['', 'OpenFlow10', 'OpenFlow13', 'OpenFlow10,OpenFlow13']

NAME_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,9}$')   # <=10: iface names
RESERVED = set(CLASS_NAMES) | {
    'net', 'info', 'CLI', 'setLogLevel', 'topology', 'adhoc', 'mesh',
    'wmediumd', 'interference', 'TCLink', 'Mininet_wifi', 'sys', 'gui',
    'configure', 'WifiGUI'}


def F(key, label, typ, group, default=None, opts=None, hint=''):
    """One editable field. typ: str int float bool choice mode channel
    range text."""
    return {'key': key, 'label': label, 'type': typ, 'group': group,
            'default': default, 'opts': opts, 'hint': hint}


EXTRA = F('extra', 'key = value', 'text', 'extra', {},
          hint='any other parameter, passed as-is (one per line)')

_STA = [
    F('mode', 'Mode', 'choice', 'radio', '', ALL_MODES,
      hint='blank = follow the AP'),
    F('range', 'Range (m)', 'range', 'radio', 30.0),
    F('txpower', 'Tx power (dBm)', 'float', 'radio', None,
      hint='blank = computed from the range'),
    F('antennaGain', 'Antenna gain (dBi)', 'float', 'radio', 5.0),
    F('antennaHeight', 'Antenna height (m)', 'float', 'radio', 1.0),
    F('wlans', 'Wireless interfaces', 'int', 'radio', 1),
    F('ip', 'IP / mask', 'str', 'addressing', '{ip}'),
    F('mac', 'MAC', 'str', 'addressing', '', hint='blank = automatic'),
    F('defaultRoute', 'Default route', 'str', 'addressing', ''),
    F('encrypt', 'Encryption', 'choice', 'security', '', ENCRYPT),
    F('passwd', 'Password', 'str', 'security', ''),
    EXTRA,
]

SCHEMA = {
    'ap': [
        F('ssid', 'SSID', 'str', 'general', '{name}-ssid'),
        F('band', 'Band (GHz)', 'choice', 'radio', '2.4', ['2.4', '5', '6']),
        F('mode', 'Mode', 'mode', 'radio', 'g'),
        F('channel', 'Channel', 'channel', 'radio', '1'),
        F('range', 'Range (m)', 'range', 'radio', 40.0),
        F('txpower', 'Tx power (dBm)', 'float', 'radio', None,
          hint='blank = computed from the range'),
        F('antennaGain', 'Antenna gain (dBi)', 'float', 'radio', 5.0),
        F('antennaHeight', 'Antenna height (m)', 'float', 'radio', 1.0),
        F('wlans', 'Wireless interfaces', 'int', 'radio', 1),
        F('ht_cap', 'HT capabilities', 'str', 'radio', '',
          hint='e.g. HT40+'),
        F('mac', 'MAC', 'str', 'addressing', '', hint='blank = automatic'),
        F('encrypt', 'Encryption', 'choice', 'security', '', ENCRYPT),
        F('passwd', 'Password', 'str', 'security', ''),
        F('client_isolation', 'Client isolation', 'bool', 'security', False),
        F('cls', 'Class', 'choice', 'openflow', 'OVSKernelAP',
          ['OVSKernelAP', 'UserAP']),
        F('failMode', 'Fail mode', 'choice', 'openflow', '', FAILMODES,
          hint='blank = secure (standalone if there is no controller)'),
        F('protocols', 'OpenFlow version', 'choice', 'openflow', '',
          PROTOCOLS),
        F('dpid', 'DPID', 'str', 'openflow', '',
          hint='blank = from the name (clashes fixed on save)'),
        F('datapath', 'Datapath', 'choice', 'openflow', '',
          ['', 'kernel', 'user']),
        EXTRA,
    ],
    'sta': _STA,
    'car': copy.deepcopy(_STA),
    'host': [
        F('ip', 'IP / mask', 'str', 'addressing', '{ip}'),
        F('mac', 'MAC', 'str', 'addressing', '', hint='blank = automatic'),
        F('defaultRoute', 'Default route', 'str', 'addressing', ''),
        EXTRA,
    ],
    'switch': [
        F('cls', 'Class', 'choice', 'openflow', 'OVSKernelSwitch',
          ['OVSKernelSwitch', 'OVSSwitch', 'UserSwitch']),
        F('failMode', 'Fail mode', 'choice', 'openflow', '', FAILMODES,
          hint='blank = secure (standalone if there is no controller)'),
        F('protocols', 'OpenFlow version', 'choice', 'openflow', '',
          PROTOCOLS),
        F('dpid', 'DPID', 'str', 'openflow', '',
          hint='blank = from the name (clashes fixed on save)'),
        EXTRA,
    ],
    'ctrl': [
        F('cls', 'Class', 'choice', 'controller', 'Controller',
          ['Controller', 'RemoteController', 'OVSController']),
        F('ip', 'IP', 'str', 'controller', '127.0.0.1'),
        F('port', 'Port', 'int', 'controller', 6653),
        F('protocol', 'Protocol', 'choice', 'controller', 'tcp',
          ['tcp', 'ssl']),
        EXTRA,
    ],
}

_WLINK = [
    F('mode', 'Mode', 'choice', 'wireless', 'g', ALL_MODES),
    F('channel', 'Channel', 'str', 'wireless', '5'),
    F('ht_cap', 'HT capabilities', 'str', 'wireless', ''),
    F('intf1', 'Interface (1st)', 'str', 'wireless', '',
      hint='blank = <node>-wlan0'),
    F('intf2', 'Interface (2nd)', 'str', 'wireless', '',
      hint='blank = <node>-wlan0'),
    EXTRA,
]
LINK_SCHEMA = {
    'wired': [
        F('bw', 'Bandwidth (Mbit/s)', 'float', 'link', None),
        F('delay', 'Delay', 'str', 'link', '', hint="e.g. 5ms"),
        F('jitter', 'Jitter', 'str', 'link', '', hint="e.g. 1ms"),
        F('loss', 'Loss (%)', 'float', 'link', None),
        F('max_queue_size', 'Max queue (packets)', 'int', 'link', None),
        EXTRA,
    ],
    'adhoc': [F('ssid', 'SSID', 'str', 'wireless', 'adhocNet')] + _WLINK,
    'mesh': [F('ssid', 'SSID', 'str', 'wireless', 'meshNet')] +
            copy.deepcopy(_WLINK),
    'wifi': [F('extra', 'key = value', 'text', 'extra', {},
               hint='passed to net.addLink(station, ap, ...)')],
}

# pictures (floor plans, maps...) - editor only, Mininet ignores them
PIC_SCHEMA = [
    F('path', 'Image file', 'file', 'picture', ''),
    F('x', 'Center X (m)', 'pos', 'placement', 0.0),
    F('y', 'Center Y (m)', 'pos', 'placement', 0.0),
    F('z', 'Floor height Z (m)', 'pos', 'placement', 0.0),
    F('width', 'Width (m)', 'slider', 'size', 100.0, opts=(1, 500, 0.5)),
    F('rotation', 'Rotation (deg)', 'slider', 'size', 0.0,
      opts=(-180, 180, 1)),
    F('opacity', 'Opacity (%)', 'slider', 'look', 100.0, opts=(0, 100, 1)),
    F('visible', 'Visible', 'bool', 'look', True),
    F('locked', 'Locked', 'bool', 'look', False,
      hint='locked = can be selected but not moved'),
]
PIC_KEYS = [f['key'] for f in PIC_SCHEMA]


# walls - attenuation per crossing (typical values at 2.4 GHz; 5 GHz is
# usually a few dB worse). "custom" lets you type your own.
WALL_MATERIALS = {
    'concrete': (12.0, '#9c9a94'),
    'brick': (8.0, '#b4583d'),
    'wood': (4.0, '#a97a4b'),
    'drywall': (3.0, '#d8d0bd'),
    'glass': (3.0, '#86c5de'),
    'metal': (20.0, '#6e7c88'),
    'custom': (10.0, '#8c6bb1'),
}
# reference thickness (m) of each preset: with a thickness multiplier k,
#   loss = preset dB * (1 + k * (thickness / reference - 1))
# k = 0 (default): thickness is ignored. k = 1: loss proportional to it.
WALL_REF = {'concrete': 0.20, 'brick': 0.20, 'wood': 0.05, 'drywall': 0.10,
            'glass': 0.01, 'metal': 0.01, 'custom': 0.20}
MATERIAL_ORDER = ['concrete', 'brick', 'wood', 'drywall', 'glass', 'metal',
                  'custom']
WALL_MODES = [('solid', 'Walls always visible'),
              ('fade', 'Front walls transparent'),
              ('cutaway', 'Front walls cut down'),
              ('outline', 'Walls as outlines')]

WALL_SCHEMA = [
    F('name', 'Name', 'wallname', 'general', ''),
    F('material', 'Material', 'choice', 'wall', 'concrete', MATERIAL_ORDER),
    F('loss', 'Attenuation (dB)', 'float', 'wall', 12.0,
      hint='preset value, or your own for custom walls'),
    F('thick_mult', 'Thickness multiplier', 'float', 'wall', 0.0,
      hint='presets only: 0 = thickness ignored, 1 = loss grows with it'),
    F('color', 'Color', 'color', 'wall', '#9c9a94',
      hint='editable for custom walls'),
    F('height', 'Height (m)', 'slider', 'geometry', 3.0, opts=(0, 20, 0.1),
      hint='0 = flat 2D wall: blocks every path crossing it'),
    F('z', 'Base height Z (m)', 'pos', 'geometry', 0.0),
    F('length', 'Length (m)', 'slider', 'geometry', 10.0,
      opts=(0.1, 100, 0.1)),
    F('thickness', 'Thickness (m)', 'slider', 'geometry', 0.2,
      opts=(0.02, 5, 0.01)),
    F('rotation', 'Rotation (deg)', 'slider', 'geometry', 0.0,
      opts=(-180, 180, 1)),
    F('x', 'Center X (m)', 'pos', 'placement', 0.0),
    F('y', 'Center Y (m)', 'pos', 'placement', 0.0),
    F('locked', 'Locked', 'bool', 'general', False,
      hint='cannot be clicked, moved or rotated (double-click to inspect)'),
]
WALL_HIDDEN = {'group': '', 'ref_thickness': 0.20}   # saved, not edited here
WALL_KEYS = [f['key'] for f in WALL_SCHEMA] + list(WALL_HIDDEN)


def default_wall(material='concrete'):
    w = dict((f['key'], f['default']) for f in WALL_SCHEMA)
    w.update(WALL_HIDDEN)
    set_material(w, material)
    return w


def set_material(w, material):
    """Apply a material: presets set loss, color and reference thickness."""
    w['material'] = material
    if material != 'custom':
        w['loss'], w['color'] = WALL_MATERIALS[material]
    w['ref_thickness'] = WALL_REF.get(material, 0.20)


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


def wall_zrange(w):
    h = float(w.get('height') or 0)
    if h <= 0:                                   # flat 2D wall: any height
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


def wall_corners(w):
    """Footprint corners (x, y), counter-clockwise."""
    a = math.radians(float(w['rotation']))
    ca, sa = math.cos(a), math.sin(a)
    L, T = float(w['length']) / 2, float(w['thickness']) / 2
    return [(w['x'] + lx * ca - ly * sa, w['y'] + lx * sa + ly * ca)
            for lx, ly in ((-L, -T), (L, -T), (L, T), (-L, T))]


def wall_crossed(w, a, b):
    """(t0, t1) part of the segment a -> b (x, y, z) inside the wall, or
    None. Height 0 = 2D wall (any height). Pure math, also used at run
    time."""
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
    """(total dB, walls counted) between two points. Walls that overlap
    along the path (same stretch of the path inside both) are counted once,
    with the highest loss, so overlapping walls never double the loss."""
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
    """Make Mininet-WiFi subtract wall losses from its RSSI.

    Mininet-WiFi computes RSSI in PropagationModel.__init__. This wraps it.
    Effective without wmediumd (tc mode) and with wmediumd in its default
    SNR mode. In interference mode wmediumd computes signals itself (C
    code), so walls cannot be applied there."""
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


def _walls_in(lst):
    fm = field_map(WALL_SCHEMA)
    out = []
    for w in lst or []:
        mat = w.get('material') if w.get('material') in WALL_MATERIALS \
            else 'custom'
        d = default_wall(mat)
        for k, v in w.items():
            if v is None:
                continue
            f = fm.get(k)
            if k == 'group':
                d[k] = str(v)
            elif k == 'ref_thickness':
                d[k] = float(v)
            elif f is None:
                continue
            elif f['type'] == 'bool':
                d[k] = coerce(f, v)
            elif f['type'] in ('choice', 'color', 'wallname'):
                d[k] = str(v)
            else:
                d[k] = float(v)
        out.append(d)
    return out


def default_pic(path=''):
    p = dict((f['key'], f['default']) for f in PIC_SCHEMA)
    p['path'] = path
    return p

NET_SCHEMA = [
    F('propagation_model', 'Model', 'choice', 'propagation', 'logDistance',
      ['', 'logDistance', 'friis', 'logNormalShadowing', 'ITU',
       'twoRayGround', 'youngModel'], hint='blank = Mininet-WiFi default'),
    F('exp', 'Path-loss exponent', 'float', 'propagation', 4.0),
    F('sL', 'System loss', 'float', 'propagation', None),
    F('variance', 'Variance (logNormal)', 'float', 'propagation', None),
    F('nFloors', 'Floors (ITU)', 'int', 'propagation', None),
    F('lF', 'Floor loss (ITU)', 'float', 'propagation', None),
    F('pL', 'Power loss coef. (ITU)', 'float', 'propagation', None),
    F('wmediumd', 'Use wmediumd', 'bool', 'medium', True),
    F('interference', 'Interference mode', 'bool', 'medium', True,
      hint='walls are not applied in this mode (wmediumd computes signals)'),
    F('noise_th', 'Noise threshold (dBm)', 'float', 'medium', -91.0),
    F('fading_cof', 'Fading coefficient', 'float', 'medium', 0.0),
    F('autoAssociation', 'Auto association', 'bool', 'association', True),
    F('ac_method', 'Association control', 'choice', 'association', '',
      ['', 'ssf', 'llf']),
    F('ipBase', 'IP base', 'str', 'addressing', '10.0.0.0/8'),
]

GROUP_TITLE = {
    'general': 'General', 'position': 'Position', 'radio': 'Radio',
    'addressing': 'Addressing', 'security': 'Security',
    'openflow': 'OpenFlow / datapath', 'controller': 'Controller',
    'link': 'Link (traffic control)', 'wireless': 'Wireless link',
    'propagation': 'Propagation model', 'medium': 'Medium',
    'association': 'Association', 'extra': 'Extra parameters',
    'clients': 'Connected devices', 'picture': 'Picture',
    'placement': 'Placement', 'size': 'Size & rotation', 'look': 'Display',
    'wall': 'Material & signal loss', 'geometry': 'Geometry',
}

PROP_KEYS = ('exp', 'sL', 'variance', 'nFloors', 'lF', 'pL')
TC_KEYS = ('bw', 'delay', 'jitter', 'loss', 'max_queue_size')


def field_map(schema):
    return dict((f['key'], f) for f in schema)


def _fill(default, name='', ip=''):
    if isinstance(default, str):
        return default.replace('{name}', name).replace('{ip}', ip)
    return copy.deepcopy(default)


def default_params(kind, name='', ip=''):
    return dict((f['key'], _fill(f['default'], name, ip))
                for f in SCHEMA[kind])


def default_link_params(kind):
    return dict((f['key'], _fill(f['default'])) for f in LINK_SCHEMA[kind])


def default_net():
    return dict((f['key'], _fill(f['default'])) for f in NET_SCHEMA)


def num(v):
    """float -> int when it is a whole number (nicer files)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    return int(f) if f.is_integer() else round(f, 4)


def fmt_num(v):
    if v is None:
        return ''
    if isinstance(v, float):
        s = ('%.2f' % v).rstrip('0').rstrip('.')
        return '0' if s in ('-0', '') else s
    return str(v)


def fmt_pos(xyz):
    return ','.join(fmt_num(float(v)) for v in xyz)


def parse_pos(pos):
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
    return vals[:3]


def band_of_channel(ch):
    try:
        return '2.4' if int(ch) <= 14 else '5'
    except (TypeError, ValueError):
        return '2.4'


def coerce(f, v):
    """Bring a value read from a file to the field's type."""
    if f is None or v is None:
        return v
    t = f['type']
    try:
        if t in ('float', 'range'):
            return None if v == '' else float(v)
        if t == 'int':
            return None if v == '' else int(float(v))
        if t == 'bool':
            if isinstance(v, str):
                return v.strip().lower() in ('1', 'true', 'yes', 'on')
            return bool(v)
        if t == 'text':
            return dict(v) if isinstance(v, dict) else {}
        return str(v)
    except (TypeError, ValueError):
        return v


def _clean(schema, p, skip=()):
    """Model params -> Mininet kwargs (drop blanks, cast numbers)."""
    out = {}
    for f in schema:
        k = f['key']
        if k in ('extra',) + tuple(skip):
            continue
        v = p.get(k)
        if v is None or v == '' or v is False:
            continue
        t = f['type']
        if t in ('float', 'range'):
            v = num(v)
        elif t == 'int':
            v = int(v)
        elif t == 'channel':
            v = str(v)
        out[k] = v
    for k, v in (p.get('extra') or {}).items():
        out[k] = v
    return out


def assign_dpids(nodes):
    """name -> dpid for APs / switches whose default dpid would clash
    (ap1 and s1 both get dpid 1 in Mininet) or can't be derived."""
    used, result, todo = set(), {}, []
    for n in nodes:
        if n['kind'] not in ('ap', 'switch'):
            continue
        d = str(n['p'].get('dpid') or '').strip()
        if d:
            try:
                used.add(int(d, 16))
            except ValueError:
                pass
        else:
            todo.append(n)
    for n in todo:
        digits = re.findall(r'\d+', n['name'])
        cand = int(digits[0]) if digits else None
        if cand is None or cand == 0 or cand in used:
            cand = max(used | {0}) + 1
            result[n['name']] = '%016x' % cand
        used.add(cand)
    return result


def doc_to_normalized(doc):
    """Editor document -> normalized file (Mininet-WiFi kwargs)."""
    net, nodes, links = doc['net'], doc['nodes'], doc['links']
    ctrls = [n['name'] for n in nodes if n['kind'] == 'ctrl']
    mw = {}
    if net.get('ipBase'):
        mw['ipBase'] = net['ipBase']
    mw['autoAssociation'] = bool(net.get('autoAssociation', True))
    if net.get('ac_method'):
        mw['ac_method'] = net['ac_method']
    for k in ('noise_th', 'fading_cof'):
        if net.get(k) is not None:
            mw[k] = num(net[k])
    prop = {}
    if net.get('propagation_model'):
        prop['model'] = net['propagation_model']
        for k in PROP_KEYS:
            if net.get(k) is not None:
                prop[k] = num(net[k])
    out = {'format': FORMAT, 'schema': 'normalized', 'version': VERSION,
           'generator': GENERATOR,
           'created': datetime.datetime.now().isoformat(timespec='seconds'),
           'network': {'mininet_wifi': mw,
                       'wmediumd': bool(net.get('wmediumd')),
                       'interference': bool(net.get('interference')),
                       'propagation': prop}}
    for kind in KINDS:
        out[LIST_KEY[kind]] = []
    out['links'], out['start'] = [], {}
    dpids = assign_dpids(nodes)
    for n in nodes:
        kind, name = n['kind'], n['name']
        params = _clean(SCHEMA[kind], n['p'], skip=('band',))
        params['position'] = fmt_pos(n['xyz'])
        entry = {'name': name, 'params': params}
        if kind == 'ctrl':
            params['controller'] = params.pop('cls', 'Controller')
        if kind == 'ap':
            entry['band'] = n['p'].get('band') or '2.4'
        if kind in ('ap', 'switch'):
            if name in dpids:
                params['dpid'] = dpids[name]
            fm = params.get('failMode', '')
            if not ctrls and not fm:
                params['failMode'] = fm = 'standalone'
            out['start'][name] = [] if fm == 'standalone' else list(ctrls)
        out[LIST_KEY[kind]].append(entry)
    by_name = {}
    for kind in KINDS:
        for e in out[LIST_KEY[kind]]:
            by_name[e['name']] = e
    for l in links:
        if l['kind'] == 'wifi':
            out['links'].append({'type': 'wifi', 'node1': l['a'],
                                 'node2': l['b'],
                                 'params': _clean(LINK_SCHEMA['wifi'],
                                                  l['p'])})
            # a station joining an encrypted AP needs the same credentials
            sp = by_name[l['a']]['params']
            ap = by_name[l['b']]['params']
            if ap.get('encrypt') and not sp.get('encrypt'):
                sp['encrypt'] = ap['encrypt']
                if ap.get('passwd') and not sp.get('passwd'):
                    sp['passwd'] = ap['passwd']
            continue
        if l['kind'] == 'wired':
            params = _clean(LINK_SCHEMA['wired'], l['p'])
            tc = any(k in params for k in TC_KEYS)
            out['links'].append({'type': 'wired', 'node1': l['a'],
                                 'node2': l['b'],
                                 'cls': 'TCLink' if tc else None,
                                 'params': params})
        else:
            params = _clean(LINK_SCHEMA[l['kind']], l['p'],
                            skip=('intf1', 'intf2'))
            intf = {l['a']: l['p'].get('intf1') or l['a'] + '-wlan0',
                    l['b']: l['p'].get('intf2') or l['b'] + '-wlan0'}
            out['links'].append({'type': l['kind'], 'nodes': [l['a'], l['b']],
                                 'intf': intf, 'params': params})
    out['walls'] = [dict([(k, w[k]) for k in WALL_KEYS] +
                         [('effective_loss', round(wall_db(w), 3))])
                    for w in doc.get('walls', [])]
    out['editor'] = {'pictures': [dict(pc) for pc in doc.get('pictures', [])]}
    return out


def normalized_to_doc(d):
    nw = d.get('network') or {}
    mw = nw.get('mininet_wifi') or {}
    prop = nw.get('propagation') or {}
    net = default_net()
    nf = field_map(NET_SCHEMA)
    for k in ('ipBase', 'autoAssociation', 'ac_method', 'noise_th',
              'fading_cof'):
        if k in mw:
            net[k] = coerce(nf[k], mw[k])
    net['wmediumd'] = bool(nw.get('wmediumd', True))
    net['interference'] = bool(nw.get('interference', True))
    net['propagation_model'] = prop.get('model', '')
    for k in PROP_KEYS:
        net[k] = coerce(nf[k], prop.get(k))
    nodes = []
    for kind in KINDS:
        fm = field_map(SCHEMA[kind])
        for e in d.get(LIST_KEY[kind]) or []:
            params = dict(e.get('params') or {})
            name = e['name']
            xyz = parse_pos(params.pop('position', None)) or [0.0, 0.0, 0.0]
            if kind == 'ctrl' and 'controller' in params:
                params['cls'] = params.pop('controller')
            p = default_params(kind, name, '')
            for k in list(params):
                if k in fm and k != 'extra':
                    p[k] = coerce(fm[k], params.pop(k))
            if kind == 'ap':
                p['band'] = e.get('band') or band_of_channel(p.get('channel'))
            p['extra'] = params
            nodes.append({'kind': kind, 'name': name, 'xyz': xyz, 'p': p})
    links = []
    for l in d.get('links') or []:
        t = l.get('type', 'wired')
        if t not in LINK_SCHEMA:
            continue
        params = dict(l.get('params') or {})
        if t in ('wired', 'wifi'):
            a, b = l['node1'], l['node2']
        else:
            a, b = l['nodes'][:2]
            intf = l.get('intf') or {}
            ia, ib = intf.get(a, ''), intf.get(b, '')
            params['intf1'] = '' if ia == a + '-wlan0' else ia
            params['intf2'] = '' if ib == b + '-wlan0' else ib
        fm = field_map(LINK_SCHEMA[t])
        p = default_link_params(t)
        for k in list(params):
            if k in fm and k != 'extra':
                p[k] = coerce(fm[k], params.pop(k))
        p['extra'] = params
        links.append({'kind': t, 'a': a, 'b': b, 'p': p})
    return {'net': net, 'nodes': nodes, 'links': links,
            'pictures': _pics_in((d.get('editor') or {}).get('pictures')),
            'walls': _walls_in(d.get('walls'))}


def _pics_in(lst):
    out = []
    for pc in lst or []:
        p = default_pic()
        for f in PIC_SCHEMA:
            if f['key'] in pc:
                p[f['key']] = coerce(f, pc[f['key']]) if f['type'] not in (
                    'pos', 'slider', 'file') else (
                    str(pc[f['key']]) if f['type'] == 'file'
                    else float(pc[f['key']]))
        out.append(p)
    return out


def doc_to_custom(doc):
    """Editor document -> custom file (grouped, typed, every field)."""
    norm = doc_to_normalized(doc)          # for start lists / dpids
    out = {'format': FORMAT, 'schema': 'custom', 'version': VERSION,
           'generator': GENERATOR,
           'created': datetime.datetime.now().isoformat(timespec='seconds'),
           'units': {'position': 'm', 'range': 'm', 'txpower': 'dBm',
                     'antennaGain': 'dBi', 'antennaHeight': 'm',
                     'bw': 'Mbit/s', 'loss': '%', 'noise_th': 'dBm'},
           'network': {}, 'devices': [], 'links': []}
    for f in NET_SCHEMA:
        out['network'].setdefault(f['group'], {})[f['key']] = \
            doc['net'].get(f['key'])
    for n in doc['nodes']:
        x, y, z = n['xyz']
        dev = {'name': n['name'], 'kind': KIND_NAME[n['kind']],
               'position': {'x': num(x), 'y': num(y), 'z': num(z)}}
        for f in SCHEMA[n['kind']]:
            if f['key'] == 'extra':
                continue
            dev.setdefault(f['group'], {})[f['key']] = n['p'].get(f['key'])
        if n['kind'] in ('ap', 'switch'):
            dev['controllers'] = norm['start'].get(n['name'], [])
        dev['extra'] = dict(n['p'].get('extra') or {})
        out['devices'].append(dev)
    for l in doc['links']:
        params = dict((f['key'], l['p'].get(f['key']))
                      for f in LINK_SCHEMA[l['kind']] if f['key'] != 'extra')
        out['links'].append({'kind': l['kind'], 'from': l['a'], 'to': l['b'],
                             'params': params,
                             'extra': dict(l['p'].get('extra') or {})})
    out['pictures'] = [dict(pc) for pc in doc.get('pictures', [])]
    out['walls'] = [{'name': w['name'], 'material': w['material'],
                     'loss_db': w['loss'], 'color': w['color'],
                     'thickness_multiplier': w.get('thick_mult', 0.0),
                     'ref_thickness': w.get('ref_thickness', 0.2),
                     'effective_loss_db': round(wall_db(w), 3),
                     'locked': bool(w.get('locked')),
                     'group': w.get('group', ''),
                     'footprint': {'x': w['x'], 'y': w['y'],
                                   'length': w['length'],
                                   'thickness': w['thickness'],
                                   'rotation': w['rotation']},
                     'z': w['z'], 'height': w['height'],
                     'is_3d': float(w['height']) > 0}
                    for w in doc.get('walls', [])]
    return out


def custom_to_doc(d):
    net = default_net()
    nf = field_map(NET_SCHEMA)
    for group in (d.get('network') or {}).values():
        if isinstance(group, dict):
            for k, v in group.items():
                if k in nf:
                    net[k] = coerce(nf[k], v)
    nodes = []
    for dev in d.get('devices') or []:
        kind = KIND_FROM_NAME.get(dev.get('kind'), None)
        if kind is None:
            continue
        fm = field_map(SCHEMA[kind])
        pos = dev.get('position') or {}
        if isinstance(pos, dict):
            xyz = [float(pos.get(a, 0) or 0) for a in 'xyz']
        else:
            xyz = parse_pos(pos) or [0.0, 0.0, 0.0]
        p = default_params(kind, dev['name'], '')
        p['extra'] = dict(dev.get('extra') or {})
        for g, vals in dev.items():
            if g in ('name', 'kind', 'position', 'controllers', 'extra') \
                    or not isinstance(vals, dict):
                continue
            for k, v in vals.items():
                if k in fm:
                    p[k] = coerce(fm[k], v)
                else:
                    p['extra'][k] = v
        nodes.append({'kind': kind, 'name': dev['name'], 'xyz': xyz, 'p': p})
    links = []
    for l in d.get('links') or []:
        t = l.get('kind', 'wired')
        if t not in LINK_SCHEMA:
            continue
        fm = field_map(LINK_SCHEMA[t])
        p = default_link_params(t)
        p['extra'] = dict(l.get('extra') or {})
        for k, v in (l.get('params') or {}).items():
            if k in fm:
                p[k] = coerce(fm[k], v)
            else:
                p['extra'][k] = v
        links.append({'kind': t, 'a': l['from'], 'b': l['to'], 'p': p})
    return {'net': net, 'nodes': nodes, 'links': links,
            'pictures': _pics_in(d.get('pictures')),
            'walls': _walls_in([dict(w.get('footprint') or {}, name=w.get('name'),
                                     material=w.get('material'),
                                     loss=w.get('loss_db', w.get('loss')),
                                     color=w.get('color'), z=w.get('z'),
                                     height=w.get('height'),
                                     thick_mult=w.get('thickness_multiplier'),
                                     ref_thickness=w.get('ref_thickness'),
                                     locked=w.get('locked'),
                                     group=w.get('group'))
                                for w in d.get('walls') or []])}


def read_doc(path):
    with open(path) as fh:
        d = json.load(fh)
    if d.get('format') not in (None, FORMAT):
        raise ValueError('not a %s file' % FORMAT)
    schema = d.get('schema', 'normalized')
    if schema == 'custom':
        doc, fmt = custom_to_doc(d), 'custom'
    else:
        doc, fmt = normalized_to_doc(d), 'normalized'
    base = os.path.dirname(os.path.abspath(path))
    for pc in doc['pictures']:                 # paths are stored relative
        if pc['path'] and not os.path.isabs(pc['path']):
            pc['path'] = os.path.normpath(os.path.join(base, pc['path']))
    return doc, fmt


def write_doc(doc, path, fmt):
    if fmt == 'script':
        text = script_from_normalized(doc_to_normalized(doc))
        with open(path, 'w') as fh:
            fh.write(text)
        try:
            os.chmod(path, 0o755)
        except OSError:
            pass
        return
    doc = dict(doc)
    base = os.path.dirname(os.path.abspath(path))
    pics = []
    for pc in doc.get('pictures', []):
        pc = dict(pc)
        if pc.get('path'):
            try:
                pc['path'] = os.path.relpath(pc['path'], base)
            except ValueError:                 # other drive on Windows
                pass
        pics.append(pc)
    doc['pictures'] = pics
    data = doc_to_custom(doc) if fmt == 'custom' else doc_to_normalized(doc)
    tmp = path + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(data, fh, indent=2)
        fh.write('\n')
    os.replace(tmp, path)


def load_normalized(path):
    """Any file saved by the editor -> normalized dict."""
    with open(path) as fh:
        d = json.load(fh)
    if d.get('schema') == 'custom':
        return doc_to_normalized(custom_to_doc(d))
    return d


# ------------------------------------------------------------ script ----

def _pyvar(name):
    v = re.sub(r'\W', '_', name)
    if not v or v[0].isdigit() or keyword.iskeyword(v) or v in RESERVED:
        v = 'n_' + v
    return v


def _kwargs(params):
    parts, odd = [], {}
    for k, v in params.items():
        if k in ('cls', 'controller') and v in CLASS_NAMES:
            parts.append('%s=%s' % (k, v))
        elif k.isidentifier() and not keyword.iskeyword(k):
            parts.append('%s=%r' % (k, v))
        else:
            odd[k] = v
    if odd:
        parts.append('**%r' % odd)
    return parts


def script_from_normalized(nd):
    nw = nd.get('network') or {}
    L = []
    w = L.append
    w('#!/usr/bin/env python3')
    w('"""')
    w('Mininet-WiFi topology generated by %s on %s' % (
        GENERATOR, datetime.datetime.now().strftime('%Y-%m-%d %H:%M')))
    w('')
    w('Run:  sudo -E python3 %s [--gui]' % 'this_script.py')
    w('      --gui opens wifi_gui.py (must be next to this script)')
    w('"""')
    w('import sys')
    w('')
    w('from mininet.log import setLogLevel, info')
    w('from mininet.node import Controller, RemoteController, OVSController')
    w('from mininet.node import OVSKernelSwitch, OVSSwitch, UserSwitch')
    w('from mininet.link import TCLink')
    w('from mn_wifi.net import Mininet_wifi')
    w('from mn_wifi.node import OVSKernelAP, UserAP')
    w('from mn_wifi.cli import CLI')
    w('from mn_wifi.link import wmediumd, adhoc, mesh')
    w('from mn_wifi.wmediumdConnector import interference')
    walls = nd.get('walls') or []
    if walls:
        import inspect
        w('import math')
        w('')
        w('# Walls drawn in the editor: attenuation (dB) is subtracted from')
        w('# the RSSI of every path that crosses them (tc mode and wmediumd')
        w('# SNR mode; not in wmediumd interference mode).')
        w('WALLS = %s' % json.dumps(walls, indent=4).replace(
            'true', 'True').replace('false', 'False').replace('null', 'None'))
        for fn in (wall_db, wall_crossed, wall_loss, apply_walls):
            w('')
            w('')
            L.extend(inspect.getsource(fn).rstrip().split('\n'))
    w('')
    w('')
    w('def topology(gui=False):')
    if walls:
        w('    apply_walls(WALLS)')
    args = []
    if nw.get('wmediumd'):
        args.append('link=wmediumd')
        if nw.get('interference'):
            args.append('wmediumd_mode=interference')
    args += _kwargs(nw.get('mininet_wifi') or {})
    w('    net = Mininet_wifi(%s)' % ', '.join(args))
    w('')
    w("    info('*** Creating nodes\\n')")
    for kind in KINDS:
        for e in nd.get(LIST_KEY[kind]) or []:
            parts = [repr(e['name'])] + _kwargs(e.get('params') or {})
            w('    %s = net.%s(%s)' % (_pyvar(e['name']), ADD_CALL[kind],
                                      ', '.join(parts)))
    prop = nw.get('propagation') or {}
    if prop.get('model'):
        w('')
        w("    info('*** Configuring propagation model\\n')")
        w('    net.setPropagationModel(%s)' % ', '.join(_kwargs(prop)))
    w('')
    w("    info('*** Configuring nodes\\n')")
    w("    configure = getattr(net, 'configureNodes', None) or "
      "net.configureWifiNodes")
    w('    configure()')
    if nd.get('links'):
        w('')
        w("    info('*** Creating links\\n')")
    for l in nd.get('links') or []:
        params = l.get('params') or {}
        if l.get('type') == 'wifi':
            parts = [_pyvar(l['node1']), _pyvar(l['node2'])]
            w('    net.addLink(%s)' % ', '.join(parts + _kwargs(params)))
        elif l.get('type') == 'wired':
            parts = [_pyvar(l['node1']), _pyvar(l['node2'])]
            if l.get('cls') == 'TCLink':
                parts.append('cls=TCLink')
            w('    net.addLink(%s)' % ', '.join(parts + _kwargs(params)))
        else:
            for n in l['nodes']:
                parts = [_pyvar(n), 'cls=%s' % l['type'],
                         'intf=%r' % l['intf'][n]]
                w('    net.addLink(%s)' % ', '.join(parts + _kwargs(params)))
    w('')
    w("    info('*** Starting network\\n')")
    w('    net.build()')
    for e in nd.get('controllers') or []:
        w('    %s.start()' % _pyvar(e['name']))
    for name, cs in (nd.get('start') or {}).items():
        w('    %s.start([%s])' % (_pyvar(name),
                                  ', '.join(_pyvar(c) for c in cs)))
    w('')
    w('    viewer = None')
    w('    if gui:')
    w('        from wifi_gui import WifiGUI')
    w('        viewer = WifiGUI(net)')
    w('        viewer.start()')
    w('')
    w("    info('*** Running CLI\\n')")
    w('    CLI(net)')
    w('')
    w("    info('*** Stopping network\\n')")
    w('    if viewer:')
    w('        viewer.stop()')
    w('    net.stop()')
    w('')
    w('')
    w("if __name__ == '__main__':")
    w("    setLogLevel('info')")
    w("    topology(gui='--gui' in sys.argv)")
    return '\n'.join(L) + '\n'


# ----------------------------------------------------------- running ----

def build_network(nd):
    """Normalized dict -> built and started Mininet-WiFi network."""
    from mininet.node import (Controller, RemoteController, OVSController,
                              OVSKernelSwitch, OVSSwitch, UserSwitch)
    from mininet.link import TCLink
    from mn_wifi.net import Mininet_wifi
    from mn_wifi.node import OVSKernelAP, UserAP
    from mn_wifi.link import wmediumd, adhoc, mesh
    from mn_wifi.wmediumdConnector import interference
    classes = {'Controller': Controller, 'RemoteController': RemoteController,
               'OVSController': OVSController,
               'OVSKernelSwitch': OVSKernelSwitch, 'OVSSwitch': OVSSwitch,
               'UserSwitch': UserSwitch, 'OVSKernelAP': OVSKernelAP,
               'UserAP': UserAP}
    wl = {'adhoc': adhoc, 'mesh': mesh}

    nw = nd.get('network') or {}
    if nd.get('walls'):
        apply_walls(nd['walls'])
        if nw.get('wmediumd') and nw.get('interference'):
            from mininet.log import info
            info('*** Note: wmediumd interference mode computes signals '
                 'itself; wall losses are not applied in this mode.\n')
    kw = dict(nw.get('mininet_wifi') or {})
    if nw.get('wmediumd'):
        kw['link'] = wmediumd
        if nw.get('interference'):
            kw['wmediumd_mode'] = interference
    net = Mininet_wifi(**kw)
    nodes = {}
    for kind in KINDS:
        for e in nd.get(LIST_KEY[kind]) or []:
            params = dict(e.get('params') or {})
            for ck in ('cls', 'controller'):
                if isinstance(params.get(ck), str):
                    params[ck] = classes[params[ck]]
            nodes[e['name']] = getattr(net, ADD_CALL[kind])(e['name'],
                                                            **params)
    prop = dict(nw.get('propagation') or {})
    if prop.get('model'):
        net.setPropagationModel(**prop)
    (getattr(net, 'configureNodes', None) or net.configureWifiNodes)()
    for l in nd.get('links') or []:
        params = dict(l.get('params') or {})
        if l.get('type') == 'wifi':            # station -> AP association
            net.addLink(nodes[l['node1']], nodes[l['node2']], **params)
        elif l.get('type') == 'wired':
            if l.get('cls') == 'TCLink':
                params['cls'] = TCLink
            net.addLink(nodes[l['node1']], nodes[l['node2']], **params)
        else:
            for n in l['nodes']:
                net.addLink(nodes[n], cls=wl[l['type']], intf=l['intf'][n],
                            **params)
    net.build()
    for c in net.controllers:
        c.start()
    for name, cs in (nd.get('start') or {}).items():
        nodes[name].start([nodes[c] for c in cs])
    return net


def run_topology(path, gui=False):
    from mininet.log import setLogLevel, info
    from mn_wifi.cli import CLI
    setLogLevel('info')
    net = build_network(load_normalized(path))
    viewer = None
    if gui:
        try:
            from wifi_gui import WifiGUI
            viewer = WifiGUI(net)
            viewer.start()
        except ImportError:
            info('*** wifi_gui.py not found, running without the viewer\n')
    CLI(net)
    if viewer:
        viewer.stop()
    net.stop()


# ==========================================================================
# Part 2 - the editor window (Tkinter only)
# ==========================================================================

C = {
    'bg': '#f6f6f3', 'grid': '#dedfd9', 'grid_major': '#c9cbc3',
    'axis': '#8a8b85', 'text': '#26272a', 'muted': '#6d6e69',
    'ap': '#2563c9', 'ap_fill': '#e1eafa', 'ap_ring': '#7fa3e3',
    'sta': '#e0761a', 'sta_ring': '#eaa66a', 'car': '#b9461a',
    'ctrl': '#7b3fc4', 'switch': '#4a4d55', 'host': '#6b6e74',
    'rf': '#16a05a', 'rf_glow': '#cdebd9', 'rf_bad': '#cc3a2f',
    'wired': '#4a4d55', 'control': '#7b3fc4',
    'shadow': '#d4d5cf', 'select': '#111111', 'warn': '#b3261e',
    'tip_bg': '#26272a', 'tip_fg': '#ffffff', 'bar': '#ebebe7',
    'halo': '#a9c1ee', 'bad_entry': '#fbe3e0',
    'x': '#c0392b', 'y': '#1e8449', 'z': '#2457a6',
}

TOOL_HINT = {
    'select': 'Select: click a device or link, drag to move it. Drag an '
              'axis arrow to move along X / Y / Z only.',
    'link': 'Link: drag from one device to another (%s link). Esc: back '
            'to select.',
    'picture': '',
    'wall': 'Wall: drag a zone on the ground (XY plane) to create a wall, '
            'then set its properties. Esc: back to select.',
}
HELP_TEXT = '''Mouse
  click a device ........ select it (local X / Y / Z axes appear)
  drag a device ......... move it on the ground   (Shift: change height)
  drag an axis arrow .... move along X, Y or Z only
  drag empty space ...... rotate the view   right-drag: pan   wheel: zoom
  double-click .......... Inspector for the device / link / picture
  Link tool ............. drag from one device to another
  picture (selected) .... drag: move   corner squares: scale
                          round handle: rotate (Shift: 15 degree steps)
  Wall tool ............. drag a zone on the ground, then set properties;
                          snaps to wall corners / edges, never crosses walls
  wall (selected) ....... drag: move   end squares: length
                          side dot: thickness   round: rotate
                          blue diamond (3D view): height

Walls subtract their attenuation (dB) from the RSSI of every path that
crosses them: in the editor estimates, and in the simulation (tc mode and
wmediumd SNR mode; not wmediumd interference mode).

Keys
  Del delete    Esc select tool    I inspector    L link tool
  W wall tool (drag a zone)   Ctrl+D duplicate wall
  Shift+click walls: multi-select   Ctrl+G group   Ctrl+Shift+G ungroup
  Ctrl+L lock / unlock (locked walls: double-click to inspect)
  P insert picture    T top view    3 3D view    F fit
  Ctrl+Z / Ctrl+Y undo / redo    Ctrl+S save    Ctrl+O open
  Ctrl+N new    Ctrl+E export Python script

AP badges show how many devices are connected to each AP:
explicit Wi-Fi links + stations auto-associating by range
(nearest AP whose range contains them, when auto association is on).'''

for _k in KINDS:
    TOOL_HINT[_k] = ('Click on the grid to place %s %s. Esc: back to select.'
                     % ('an' if KIND_LABEL[_k][0] in 'aeiou' else 'a',
                        KIND_LABEL[_k]))


def _mix(c1, c2, t):
    """Blend two #rrggbb colours (t=0 -> c1, t=1 -> c2)."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return '#%02x%02x%02x' % tuple(int(a[i] + (b[i] - a[i]) * t)
                                   for i in range(3))


def _nice_step(span, target=8):
    raw = max(span, 1e-9) / target
    p = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        if raw <= m * p:
            return m * p
    return 10 * p


def _seg_dist(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    if L2 < 1e-9:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L2))
    return math.hypot(px - x1 - t * dx, py - y1 - t * dy)


def _closest_on_seg(x, y, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    if L2 < 1e-12:
        return a
    t = max(0.0, min(1.0, ((x - a[0]) * dx + (y - a[1]) * dy) / L2))
    return (a[0] + t * dx, a[1] + t * dy)


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


def draw_icon(cv, kind, x, y, ow='white', s=1.0):
    """Device icons, same shapes as wifi_gui.py (+ car)."""
    k = s
    if kind == 'ap':
        cv.create_rectangle(x - 10 * k, y - 7 * k, x + 10 * k, y + 7 * k,
                            fill=C['ap'], outline=ow, width=2)
        cv.create_line(x + 6 * k, y - 7 * k, x + 6 * k, y - 15 * k,
                       fill=C['ap'], width=2)
        for r in (5 * k, 9 * k):
            cv.create_arc(x + 6 * k - r, y - 15 * k - r, x + 6 * k + r,
                          y - 15 * k + r, start=45, extent=90, style='arc',
                          outline=C['ap'], width=1.5)
        for i in (-5, 0):
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
    else:                                                     # host
        cv.create_rectangle(x - 8 * k, y - 8 * k, x + 8 * k, y + 8 * k,
                            fill=C['host'], outline=ow, width=2)
        cv.create_rectangle(x - 4.5 * k, y - 4.5 * k, x + 4.5 * k, y + 1 * k,
                            fill='#c4c6cb', outline='')


def icon_center(kind, w, h):
    """Where to put an icon so it sits nicely in a w x h box."""
    return (w / 2, h / 2 + (4 if kind == 'ap' else 0))


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


class Editor(object):
    HIT = 16           # px radius for clicking a node
    SNAP = 5.0         # metres, when "Snap" is on

    def __init__(self, path=None):
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox
        self.tk, self.ttk = tk, ttk
        self.fd, self.mb = filedialog, messagebox

        # model
        self.nodes, self.links = {}, {}          # id -> dict
        self.pics = {}                           # id -> picture dict
        self.walls = {}                          # id -> wall dict
        self.wall_hits, self.wall_handles = [], []
        self._pic_src, self._pic_tk = {}, {}     # image caches
        self.pic_hits = []
        self.netp = default_net()
        self.next_id = 1
        # state
        self.sel = None              # ('node'|'link'|'pic'|'wall', id)
        self.tool = 'select'
        self.hover = self.hover_axis = None
        self.mouse = (0, 0)
        self.hits, self.link_hits, self.axis_hits = [], [], []
        self.drag = None
        self.msg = ''
        self.undo_stack, self.redo_stack = [], []
        self._undo_key = None
        self.path, self.fmt, self.dirty = None, 'normalized', False
        self.inspector = None
        self._lock = False
        self.user_view = False
        # camera
        self.yaw, self.pitch = math.radians(-30), math.radians(55)
        self.scale, self.pan = 1.0, [0.0, 0.0]
        self.target = (50.0, 50.0, 0.0)

        root = self.root = tk.Tk()
        root.geometry('1300x840')
        root.minsize(640, 480)
        root.configure(bg=C['bar'])
        self.v_ranges = tk.BooleanVar(value=True)
        self.v_sphere = tk.BooleanVar(value=True)
        self.v_labels = tk.BooleanVar(value=True)
        self.v_snap = tk.BooleanVar(value=False)
        self.v_clients = tk.BooleanVar(value=True)
        self.v_outline = tk.BooleanVar(value=False)
        self.v_walls = tk.BooleanVar(value=True)
        self.wall_mode_label = tk.StringVar(value=WALL_MODES[1][1])
        self.wall_ask = True                     # wall dialog after drawing
        self.last_wall = default_wall()
        self.link_kind = tk.StringVar(value='wired')

        self.build_toolbar()
        self.status = tk.Label(root, anchor='nw', justify='left', padx=10,
                               pady=4, bg=C['bar'], fg=C['text'], height=5,
                               font=('TkDefaultFont', 9))  # fixed height:
        #                                    no canvas resize / re-fit
        self.status.pack(side='bottom', fill='x')     # always visible
        self.canvas = tk.Canvas(root, width=1100, height=600, bg=C['bg'],
                                highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)

        cv = self.canvas
        cv.bind('<Configure>', self.on_resize)
        cv.bind('<ButtonPress-1>', self.on_press)
        cv.bind('<B1-Motion>', self.on_drag)
        cv.bind('<ButtonRelease-1>', self.on_release)
        cv.bind('<Double-Button-1>', self.on_double)
        for b in ('2', '3'):
            cv.bind('<ButtonPress-%s>' % b, self.on_pan_start)
            cv.bind('<B%s-Motion>' % b, self.on_pan)
        cv.bind('<Motion>', self.on_motion)
        cv.bind('<Leave>', lambda e: self.set_hover(None, None))
        cv.bind('<MouseWheel>', lambda e: self.zoom(e, 1 if e.delta > 0
                                                    else -1))
        cv.bind('<Button-4>', lambda e: self.zoom(e, 1))
        cv.bind('<Button-5>', lambda e: self.zoom(e, -1))
        root.bind('<Key>', self.on_key)
        for seq, fn in (('<Control-s>', self.save), ('<Control-S>', self.save_as),
                        ('<Control-o>', self.open), ('<Control-n>', self.new),
                        ('<Control-e>', self.export_py),
                        ('<Control-z>', self.undo), ('<Control-y>', self.redo),
                        ('<Control-d>', self.duplicate_wall),
                        ('<Control-g>', self.group_walls),
                        ('<Control-G>', self.ungroup_walls),
                        ('<Control-l>', self.toggle_lock),
                        ('<Control-Z>', self.redo)):
            root.bind(seq, lambda e, f=fn: (f(), 'break')[1])
        root.protocol('WM_DELETE_WINDOW', self.on_close)

        self.set_tool('select')
        self.on_toggle()
        if path:
            self.open_path(path)
        self.update_title()
        root.after(80, self.fit)

    # --------------------------------------------------------- toolbar ----
    def _btn(self, parent, text, cmd, accent=False, side='left', width=None):
        b = self.tk.Button(parent, text=text, command=cmd, relief='flat',
                           bg=C['ap'] if accent else 'white',
                           fg='white' if accent else C['text'],
                           activebackground=C['ap_ring'] if accent
                           else C['grid'], padx=6, pady=1)
        if width:
            b.config(width=width)
        b.pack(side=side, padx=2, pady=1)
        return b

    def _group(self, bar, caption, inline=False):
        """Captioned toolbar group (wraps as one block).
        inline=True puts the caption on the left (slim view bar)."""
        tk = self.tk
        outer = bar.group()
        tk.Frame(outer, width=1, bg=C['grid_major']).pack(side='right',
                                                          fill='y', padx=(8, 0))
        if inline:
            tk.Label(outer, text=caption, bg=C['bar'], fg=C['muted'],
                     font=('TkDefaultFont', 7, 'bold')).pack(side='left',
                                                             padx=(0, 6))
            body = tk.Frame(outer, bg=C['bar'])
            body.pack(side='left')
            return body
        col = tk.Frame(outer, bg=C['bar'])
        col.pack(side='left', fill='y')
        tk.Label(col, text=caption, bg=C['bar'], fg=C['muted'],
                 font=('TkDefaultFont', 7, 'bold')).pack(side='bottom')
        body = tk.Frame(col, bg=C['bar'])
        body.pack(side='top', expand=True)
        return body

    def _icon(self, cv, what):
        if what in KINDS:
            draw_icon(cv, what, *icon_center(what, 30, 24), s=0.8)
        elif what == 'select':
            cv.create_polygon(11, 2, 11, 19, 15, 15, 18, 22, 21, 21, 18, 14,
                              23, 14, fill=C['text'], outline='white')
        elif what == 'picture':
            cv.create_rectangle(4, 3, 26, 21, fill='white', outline=C['text'])
            cv.create_polygon(6, 19, 13, 10, 18, 16, 21, 13, 25, 19,
                              fill=C['y'], outline='')
            cv.create_oval(19, 5, 24, 10, fill=C['sta'], outline='')
        elif what == 'wall':
            col = WALL_MATERIALS['brick'][1]
            cv.create_polygon(5, 9, 19, 4, 26, 8, 12, 13, fill=_mix(
                col, '#ffffff', 0.35), outline=C['text'])
            cv.create_polygon(5, 9, 12, 13, 12, 22, 5, 18, fill=_mix(
                col, '#000000', 0.25), outline=C['text'])
            cv.create_polygon(12, 13, 26, 8, 26, 17, 12, 22, fill=col,
                              outline=C['text'])
            for y in (15, 18):
                cv.create_line(12, y + 1, 26, y - 4, fill=_mix(col, '#ffffff',
                                                               0.4))
        else:                                                   # link
            cv.create_line(7, 19, 23, 6, fill=C['wired'], width=3)
            for x, y in ((7, 19), (23, 6)):
                cv.create_oval(x - 4, y - 4, x + 4, y + 4, fill=C['ap'],
                               outline='white')

    def _tool(self, parent, tool, text, command=None):
        """Icon-over-label button. With command it is an action button,
        otherwise it selects an editing tool."""
        tk = self.tk
        f = tk.Frame(parent, bg=C['bar'], padx=2, pady=1,
                     highlightthickness=1, highlightbackground=C['bar'])
        cv = tk.Canvas(f, width=30, height=24, bg=C['bar'],
                       highlightthickness=0)
        self._icon(cv, tool)
        cv.pack(side='top')
        lb = tk.Label(f, text=text, bg=C['bar'], fg=C['text'],
                      font=('TkDefaultFont', 8), width=8)
        lb.pack(side='top')
        cb = command or (lambda t=tool: self.set_tool(t))
        for w in (f, cv, lb):
            w.bind('<Button-1>', lambda e: cb())
            w.bind('<Enter>', lambda e, ww=(f, cv, lb): self._hl(ww, True))
            w.bind('<Leave>', lambda e, ww=(f, cv, lb): self._hl(ww, False))
        f.pack(side='left', padx=1)
        if command is None:
            self.tool_widgets[tool] = (f, cv, lb)
        return f

    def _hl(self, ww, on):
        f = ww[0]
        if f.cget('bg') == 'white':          # active tool keeps its look
            return
        f.config(highlightbackground=C['grid_major'] if on else C['bar'])

    def build_menu(self):
        tk = self.tk
        m = tk.Menu(self.root)
        fm = tk.Menu(m, tearoff=0)
        for label, cmd, acc in (('New', self.new, 'Ctrl+N'),
                                ('Open...', self.open, 'Ctrl+O'),
                                ('Save', self.save, 'Ctrl+S'),
                                ('Save as...', self.save_as, 'Ctrl+Shift+S'),
                                ('Export Python script...', self.export_py,
                                 'Ctrl+E')):
            fm.add_command(label=label, command=cmd, accelerator=acc)
        fm.add_separator()
        fm.add_command(label='Quit', command=self.on_close)
        m.add_cascade(label='File', menu=fm)
        em = tk.Menu(m, tearoff=0)
        em.add_command(label='Undo', command=self.undo, accelerator='Ctrl+Z')
        em.add_command(label='Redo', command=self.redo, accelerator='Ctrl+Y')
        em.add_separator()
        em.add_command(label='Inspector', command=self.open_inspector,
                       accelerator='I')
        em.add_command(label='Duplicate wall', command=self.duplicate_wall,
                       accelerator='Ctrl+D')
        em.add_command(label='Group selected walls', command=self.group_walls,
                       accelerator='Ctrl+G')
        em.add_command(label='Ungroup walls', command=self.ungroup_walls,
                       accelerator='Ctrl+Shift+G')
        em.add_command(label='Lock / unlock walls', command=self.toggle_lock,
                       accelerator='Ctrl+L')
        em.add_command(label='Delete selection', command=self.delete_selection,
                       accelerator='Del')
        m.add_cascade(label='Edit', menu=em)
        im = tk.Menu(m, tearoff=0)
        for kind in KINDS:
            im.add_command(label=KIND_LABEL[kind].capitalize(),
                           command=lambda k=kind: self.set_tool(k))
        im.add_separator()
        for lk in LINK_KINDS:
            im.add_command(label='%s link' % LINK_LABEL[lk],
                           command=lambda k=lk: self.pick_link(k))
        im.add_separator()
        im.add_command(label='Wall (drag a zone)',
                       command=lambda: self.set_tool('wall'), accelerator='W')
        im.add_command(label='Picture...', command=self.insert_picture,
                       accelerator='P')
        m.add_cascade(label='Insert', menu=im)
        vm = self.view_menu = tk.Menu(m, tearoff=0)
        vm.add_command(label='3D view', command=self.view_3d, accelerator='3')
        vm.add_command(label='Top view', command=self.view_top,
                       accelerator='T')
        vm.add_command(label='Fit', command=self.fit, accelerator='F')
        vm.add_separator()
        for label, var in self.toggles:
            vm.add_checkbutton(label=label, variable=var,
                               command=self.on_toggle)
        vm.add_separator()
        for key, label in WALL_MODES:
            vm.add_radiobutton(label=label, value=label,
                               variable=self.wall_mode_label,
                               command=self.redraw)
        m.add_cascade(label='View', menu=vm)
        hm = tk.Menu(m, tearoff=0)
        hm.add_command(label='Mouse & keys', command=lambda: self.mb.showinfo(
            'Mouse & keys', HELP_TEXT, parent=self.root))
        hm.add_command(label='Wall materials', command=lambda: self.mb.showinfo(
            'Wall materials', '\n'.join(
                '%-9s %5s dB' % (m_, fmt_num(WALL_MATERIALS[m_][0]))
                for m_ in MATERIAL_ORDER if m_ != 'custom') +
            '\n\nTypical attenuation per wall at 2.4 GHz (5 GHz is usually '
            'a few dB worse). Choose "custom" to type your own value.',
            parent=self.root))
        m.add_cascade(label='Help', menu=hm)
        self.root.config(menu=m)

    def pick_link(self, kind):
        self.link_kind.set(kind)
        self.set_tool('link')

    def wall_mode(self):
        lab = self.wall_mode_label.get()
        for k, l in WALL_MODES:
            if l == lab:
                return k
        return 'solid'

    def on_toggle(self):
        """A SHOW checkbox changed: keep dependent controls in sync."""
        if self.v_ranges.get():
            self.cb_outline.grid()
        else:
            self.cb_outline.grid_remove()
        st = 'normal' if self.v_ranges.get() else 'disabled'
        try:
            idx = self.view_menu.index('Ranges: outline only')
            self.view_menu.entryconfig(idx, state=st)
        except Exception:
            pass
        st = 'readonly' if self.v_walls.get() else 'disabled'
        self.cb_wallmode.config(state=st)
        self.redraw()

    def build_toolbar(self):
        """Two bars. Top: editing ribbon (FILE, EDIT, ADD, CONNECT, BUILD).
        Bottom: slim view bar (VIEW, SHOW, WALLS, RANGE). Groups wrap onto
        new rows when the window is too narrow."""
        tk, ttk = self.tk, self.ttk
        self.toggles = (('Ranges', self.v_ranges),
                        ('Ranges: outline only', self.v_outline),
                        ('Sphere', self.v_sphere),
                        ('Labels', self.v_labels),
                        ('AP clients', self.v_clients),
                        ('Walls', self.v_walls),
                        ('Snap %g m' % self.SNAP, self.v_snap))
        self.build_menu()
        self.tool_widgets = {}

        # ---- editing ribbon
        top = self.toolbar = _FlowBar(tk, self.root, C['bar'])
        top.frame.pack(fill='x')

        g = self._group(top, 'FILE')
        r1, r2 = tk.Frame(g, bg=C['bar']), tk.Frame(g, bg=C['bar'])
        r1.pack(anchor='w'); r2.pack(anchor='w')
        self._btn(r1, 'New', self.new, width=5)
        self._btn(r1, 'Open', self.open, width=5)
        self._btn(r1, 'Save', self.save, width=5)
        self._btn(r2, 'Save as', self.save_as, width=8)
        self._btn(r2, 'Export .py', self.export_py, width=8)

        g = self._group(top, 'EDIT')
        r1, r2 = tk.Frame(g, bg=C['bar']), tk.Frame(g, bg=C['bar'])
        r1.pack(anchor='w'); r2.pack(anchor='w')
        self._btn(r1, 'Inspector', self.open_inspector, accent=True, width=8)
        self._btn(r1, 'Delete', self.delete_selection, width=6)
        self._btn(r2, 'Undo', self.undo, width=8)
        self._btn(r2, 'Redo', self.redo, width=6)

        g = self._group(top, 'ADD DEVICE  (click the grid)')
        self._tool(g, 'select', 'Select')
        for kind, text in (('ap', 'AP'), ('sta', 'Station'), ('car', 'Car'),
                           ('host', 'Host'), ('switch', 'Switch'),
                           ('ctrl', 'Controller')):
            self._tool(g, kind, text)

        g = self._group(top, 'CONNECT  (drag device to device)')
        self._tool(g, 'link', 'Link')
        col = tk.Frame(g, bg=C['bar'])
        col.pack(side='left', padx=(2, 0))
        cb = ttk.Combobox(col, textvariable=self.link_kind,
                          values=LINK_KINDS, state='readonly', width=7)
        cb.pack(side='top', pady=(2, 1))
        cb.bind('<<ComboboxSelected>>', lambda e: (self.set_tool('link'),
                                                   self.canvas.focus_set()))
        self.link_help = tk.Label(col, bg=C['bar'], fg=C['muted'],
                                  font=('TkDefaultFont', 7), width=17,
                                  anchor='w')
        self.link_help.pack(side='top')
        self.link_kind.trace_add('write', lambda *a: self.link_help.config(
            text=LINK_HELP[self.link_kind.get()]))
        self.link_help.config(text=LINK_HELP['wired'])

        g = self._group(top, 'BUILD  (Shift+click: multi-select)')
        self._tool(g, 'wall', 'Wall')
        col = tk.Frame(g, bg=C['bar'])
        col.pack(side='left', padx=2)
        self._btn(col, 'Group', self.group_walls, side='top', width=7)
        self._btn(col, 'Ungroup', self.ungroup_walls, side='top', width=7)
        col = tk.Frame(g, bg=C['bar'])
        col.pack(side='left', padx=2)
        self._btn(col, 'Lock', self.toggle_lock, side='top', width=7)
        self._btn(col, 'Duplicate', self.duplicate_wall, side='top', width=7)
        self._tool(g, 'picture', 'Picture', command=self.insert_picture)

        # ---- slim view bar
        vb = _FlowBar(tk, self.root, _mix(C['bar'], '#ffffff', 0.45),
                      gap=10, vgap=3)
        self.viewbar = vb
        vb.frame.pack(fill='x')
        vbg = vb.bg

        def checkbox(parent, text, var, **grid):
            c = tk.Checkbutton(parent, text=text, variable=var, bg=vbg,
                               activebackground=vbg, highlightthickness=0,
                               font=('TkDefaultFont', 9),
                               command=self.on_toggle)
            c.grid(**grid)
            return c

        g = self._group_v(vb, 'VIEW')
        self._btn(g, '3D', self.view_3d, width=3)
        self._btn(g, 'Top', self.view_top, width=3)
        self._btn(g, 'Fit', self.fit, width=3)

        g = self._group_v(vb, 'SHOW')
        checkbox(g, 'Ranges', self.v_ranges, row=0, column=0, sticky='w')
        self.cb_outline = checkbox(g, 'outline only', self.v_outline, row=0,
                                   column=1, sticky='w')
        checkbox(g, 'Sphere', self.v_sphere, row=0, column=2, sticky='w')
        checkbox(g, 'Labels', self.v_labels, row=0, column=3, sticky='w')
        checkbox(g, 'AP clients', self.v_clients, row=0, column=4,
                 sticky='w')
        checkbox(g, 'Snap %g m' % self.SNAP, self.v_snap, row=0, column=5,
                 sticky='w')

        g = self._group_v(vb, 'WALLS')
        checkbox(g, 'Show', self.v_walls, row=0, column=0, sticky='w')
        tk.Label(g, text='camera:', bg=vbg, fg=C['muted'],
                 font=('TkDefaultFont', 8)).grid(row=0, column=1, padx=(6, 2))
        self.cb_wallmode = ttk.Combobox(
            g, textvariable=self.wall_mode_label, state='readonly', width=22,
            values=[l for _, l in WALL_MODES])
        self.cb_wallmode.grid(row=0, column=2)
        self.cb_wallmode.bind('<<ComboboxSelected>>', lambda e: (
            self.redraw(), self.canvas.focus_set()))

        g = self._group_v(vb, 'RANGE')
        self.range_name = tk.Label(g, text='select an AP, station or car',
                                   bg=vbg, fg=C['muted'], anchor='w', width=20,
                                   font=('TkDefaultFont', 9))
        self.range_name.pack(side='left')
        self.slider = tk.Scale(g, from_=0, to=300, orient='horizontal',
                               length=170, resolution=1, showvalue=0,
                               bg=C['ap_ring'], highlightthickness=0, bd=1,
                               troughcolor='white', sliderrelief='flat',
                               activebackground=C['ap'], sliderlength=14,
                               width=11, command=self.on_slider)
        self.slider.pack(side='left', padx=4)
        self.range_val = tk.Label(g, text='', bg=vbg, fg=C['text'], width=6,
                                  anchor='w', font=('TkDefaultFont', 9, 'bold'))
        self.range_val.pack(side='left')

    def _group_v(self, bar, caption):
        """Inline-captioned group for the view bar (uses the bar colour)."""
        tk = self.tk
        outer = bar.group()
        tk.Frame(outer, width=1, bg=C['grid_major']).pack(side='right',
                                                          fill='y', padx=(8, 0))
        tk.Label(outer, text=caption, bg=bar.bg, fg=C['muted'],
                 font=('TkDefaultFont', 7, 'bold')).pack(side='left',
                                                         padx=(0, 6))
        body = tk.Frame(outer, bg=bar.bg)
        body.pack(side='left')
        return body

    def set_tool(self, tool):
        self.tool = tool
        if self.drag and self.drag.get('mode') == 'link':
            self.drag = None
        for t, (f, cv, lb) in self.tool_widgets.items():
            on = t == tool
            bg = 'white' if on else C['bar']
            f.config(bg=bg, highlightbackground=C['axis'] if on else C['bar'])
            cv.config(bg=bg)
            lb.config(bg=bg)
        self.canvas.config(cursor='crosshair' if tool != 'select' else '')
        self.msg = ''
        self.redraw()

    # ----------------------------------------------------------- model ----
    def node_by_name(self, name):
        for n in self.nodes.values():
            if n['name'] == name:
                return n
        return None

    def sel_node(self):
        if self.sel and self.sel[0] == 'node':
            return self.nodes.get(self.sel[1])
        return None

    @staticmethod
    def rng(n):
        if n['kind'] not in WIRELESS_KINDS:
            return 0.0
        try:
            return max(float(n['p'].get('range') or 0), 0.0)
        except (TypeError, ValueError):
            return 0.0

    def unique_name(self, kind):
        names = set(n['name'] for n in self.nodes.values())
        i = 0 if kind == 'ctrl' else 1
        while '%s%d' % (PREFIX[kind], i) in names:
            i += 1
        return '%s%d' % (PREFIX[kind], i)

    def next_ip(self):
        try:
            netw = ipaddress.ip_network(self.netp.get('ipBase') or
                                        '10.0.0.0/8', strict=False)
        except ValueError:
            netw = ipaddress.ip_network('10.0.0.0/8')
        used = set()
        for n in self.nodes.values():
            ip = str(n['p'].get('ip') or '').split('/')[0]
            used.add(ip)
        for i, h in enumerate(netw.hosts()):
            if i > 5000:
                break
            if str(h) not in used:
                return '%s/%d' % (h, netw.prefixlen)
        return ''

    def add_node(self, kind, sx, sy):
        x, y = self.unproject(sx, sy, 0.0)
        x, y = self.snap(x), self.snap(y)
        self.push_undo()
        name = self.unique_name(kind)
        nid = self.next_id
        self.next_id += 1
        ip = self.next_ip() if kind in ('sta', 'car', 'host') else ''
        self.nodes[nid] = {'id': nid, 'kind': kind, 'name': name,
                           'xyz': [round(x, 2), round(y, 2), 0.0],
                           'p': default_params(kind, name, ip)}
        self.msg = 'Added %s %s.' % (KIND_LABEL[kind], name)
        self.select(('node', nid), quiet=True)
        self.changed()

    def add_link(self, a, b):
        kind = self.link_kind.get()
        na, nb = self.nodes[a], self.nodes[b]
        ks = (na['kind'], nb['kind'])
        if 'ctrl' in ks:
            self.msg = ('!  Controllers are connected automatically to every '
                        'AP / switch that is not standalone.')
            return self.redraw()
        if kind == 'wifi':
            if ks[0] == 'ap':                     # store as station -> AP
                a, b, na, nb = b, a, nb, na
                ks = ks[::-1]
            if ks[0] not in ('sta', 'car') or ks[1] != 'ap':
                self.msg = ('!  A Wi-Fi link connects a station or car to an '
                            'access point.')
                return self.redraw()
            used = sum(1 for l in self.links.values()
                       if l['kind'] == 'wifi' and l['a'] == a)
            wl = int(na['p'].get('wlans') or 1)
            if used >= wl:
                self.msg = ('!  %s has %d wireless interface%s, all in use. '
                            'Raise "Wireless interfaces" in the Inspector.'
                            % (na['name'], wl, '' if wl == 1 else 's'))
                return self.redraw()
        elif kind != 'wired' and not all(k in ('sta', 'car') for k in ks):
            self.msg = '!  %s links connect stations / cars only.' % kind
            return self.redraw()
        for l in self.links.values():
            if l['kind'] == kind and {l['a'], l['b']} == {a, b}:
                self.msg = '!  %s and %s already have a %s link.' % (
                    na['name'], nb['name'], kind)
                return self.redraw()
        self.push_undo()
        lid = self.next_id
        self.next_id += 1
        self.links[lid] = {'id': lid, 'kind': kind, 'a': a, 'b': b,
                           'p': default_link_params(kind)}
        self.msg = 'Linked %s and %s (%s).' % (na['name'], nb['name'], kind)
        self.select(('link', lid), quiet=True)
        self.changed()

    def delete_selection(self):
        if not self.sel:
            return
        kind, oid = self.sel
        self.push_undo()
        if kind == 'node' and oid in self.nodes:
            name = self.nodes[oid]['name']
            for lid in [l for l, v in self.links.items()
                        if oid in (v['a'], v['b'])]:
                del self.links[lid]
            del self.nodes[oid]
            self.msg = 'Deleted %s.' % name
        elif kind == 'link' and oid in self.links:
            del self.links[oid]
            self.msg = 'Deleted link.'
        elif kind == 'pic' and oid in self.pics:
            del self.pics[oid]
            self._pic_tk.pop(oid, None)
            self.msg = 'Deleted picture.'
        elif kind in ('wall', 'walls'):
            ids = self.sel_wall_ids()
            locked = [self.walls[i]['name'] for i in ids
                      if self.walls[i].get('locked')]
            if locked:
                self.undo_stack.pop()
                self.msg = '!  Unlock %s before deleting.' % ', '.join(locked)
                return self.redraw()
            for i in ids:
                del self.walls[i]
            self.msg = 'Deleted %d wall%s.' % (len(ids),
                                               '' if len(ids) == 1 else 's')
        self.sel = None
        self.changed()

    # ---------------------------------------------------- associations ----
    def associations(self):
        """ap id -> [(device id, 'link' | 'auto')].
        'link' = explicit Wi-Fi link. 'auto' = no link but inside an AP's
        range while auto association is on (nearest AP wins, like a
        station picking the strongest signal). Pure geometry, no RF."""
        res = dict((i, []) for i, n in self.nodes.items() if n['kind'] == 'ap')
        linked, busy = set(), set()
        for l in self.links.values():
            if l['kind'] == 'wifi' and l['b'] in res:
                res[l['b']].append((l['a'], 'link'))
                linked.add(l['a'])
            elif l['kind'] in ('adhoc', 'mesh'):
                busy.update((l['a'], l['b']))
        if self.netp.get('autoAssociation', True):
            for i, n in self.nodes.items():
                if n['kind'] not in ('sta', 'car') or i in linked:
                    continue
                if i in busy and int(n['p'].get('wlans') or 1) <= 1:
                    continue
                best, br = None, None
                for a in res:
                    r = self.est_rssi(n, self.nodes[a])[0]
                    if r >= self.noise_th() and (br is None or r > br):
                        best, br = a, r
                if best is not None:
                    res[best].append((i, 'auto'))
        return res

    def noise_th(self):
        v = self.netp.get('noise_th')
        return -91.0 if v is None else float(v)

    def est_rssi(self, sta, ap):
        """Estimated RSSI (dBm) at sta from ap, with wall losses.
        Log-distance model like Mininet-WiFi: the AP range is the distance
        where the signal reaches the noise threshold, so
            rssi = noise_th + 10 * exp * log10(range / d) - walls.
        Returns (rssi, wall dB, walls crossed)."""
        exp = self.netp.get('exp') or 3.0
        d = max(math.dist(sta['xyz'], ap['xyz']), 0.1)
        r = max(self.rng(ap), 0.1)
        loss, hit = wall_loss(list(self.walls.values()), sta['xyz'], ap['xyz'])
        rssi = self.noise_th() + 10 * float(exp) * math.log10(r / d) - loss
        return rssi, loss, hit

    def link_ok(self, sta, ap):
        return self.est_rssi(sta, ap)[0] >= self.noise_th()

    def client_of(self, nid, assoc=None):
        """[(ap id, how)] a station / car is connected to."""
        assoc = assoc if assoc is not None else self.associations()
        return [(a, how) for a, lst in assoc.items()
                for (i, how) in lst if i == nid]

    def clients_text(self, aid):
        lst = self.associations().get(aid, [])
        if not lst:
            return 'No device connected.'
        ap = self.nodes[aid]
        lines = ['%d device%s connected' % (len(lst),
                                            '' if len(lst) == 1 else 's')]
        for i, how in lst:
            n = self.nodes[i]
            d = math.dist(n['xyz'], ap['xyz'])
            rssi, loss, hit = self.est_rssi(n, ap)
            lines.append('  %s  -  %s, %s m, est. %d dBm%s%s' % (
                n['name'], 'Wi-Fi link' if how == 'link' else 'auto',
                fmt_num(round(d, 1)), round(rssi),
                ('  (%d wall%s, -%s dB)' % (len(hit), '' if len(hit) == 1
                                            else 's', fmt_num(loss)))
                if hit else '',
                '  NO SIGNAL' if rssi < self.noise_th() else ''))
        return '\n'.join(lines)

    # -------------------------------------------------------- pictures ----
    # ----------------------------------------------------------- walls ----
    def sel_wall(self):
        if self.sel and self.sel[0] == 'wall':
            return self.walls.get(self.sel[1])
        return None

    def sel_wall_ids(self):
        if self.sel and self.sel[0] == 'wall':
            return [self.sel[1]] if self.sel[1] in self.walls else []
        if self.sel and self.sel[0] == 'walls':
            return [i for i in self.sel[1] if i in self.walls]
        return []

    def set_wall_selection(self, ids, quiet=False):
        ids = sorted(set(ids))
        if not ids:
            self.select(None, quiet)
        elif len(ids) == 1:
            self.select(('wall', ids[0]), quiet)
        else:
            self.select(('walls', tuple(ids)), quiet)

    def group_members(self, wid):
        g = self.walls[wid].get('group')
        if not g:
            return [wid]
        return [i for i, w in self.walls.items() if w.get('group') == g]

    def unique_wall_name(self):
        names = set(w['name'] for w in self.walls.values())
        i = 1
        while 'wall%d' % i in names:
            i += 1
        return 'wall%d' % i

    def overlapping_wall(self, w, ignore=()):
        """First other wall that w would overlap (None = fine)."""
        for i, o in self.walls.items():
            if i in ignore or i == w.get('id'):
                continue
            if walls_overlap(w, o):
                return o
        return None

    def check_wall(self, w):
        o = self.overlapping_wall(w)
        if o is not None:
            return ('overlaps %s - change thickness, height or base Z'
                    % o['name'])
        return None

    def add_wall(self, w):
        self.push_undo()
        wid = self.next_id
        self.next_id += 1
        w['id'] = wid
        self.walls[wid] = w
        self.msg = ('Added %s (%s, %s dB%s). Keep drawing, or Esc / Select '
                    'to move it and use its handles.'
                    % (w['name'], w['material'], fmt_num(round(wall_db(w), 2)),
                       ', %s m high' % fmt_num(w['height'])
                       if float(w['height']) > 0 else ', flat 2D'))
        self.select(('wall', wid), quiet=True)
        self.changed()

    # -------- drawing helpers: snapping and no-overlap clamping
    def _blockers(self, zr, ignore=()):
        out = []
        for i, w in self.walls.items():
            if i in ignore:
                continue
            o = wall_zrange(w)
            if min(o[1], zr[1]) - max(o[0], zr[0]) > 0.005:
                out.append(w)
        return out

    def snap_wall_point(self, x, y, zr, ignore=()):
        """Snap to a nearby wall corner (first) or edge, and never inside a
        wall. Returns ((x, y), snapped?)."""
        tol = 12.0 / max(self.scale, 1e-9)
        blockers = self._blockers(zr, ignore)
        best, bd = None, tol * 1.4
        for w in blockers:
            for c in wall_corners(w):
                d = math.hypot(c[0] - x, c[1] - y)
                if d < bd:
                    best, bd = c, d
        if best is None:
            bd = tol
            for w in blockers:
                cs = wall_corners(w)
                for i in range(4):
                    q = _closest_on_seg(x, y, cs[i], cs[(i + 1) % 4])
                    d = math.hypot(q[0] - x, q[1] - y)
                    if d < bd:
                        best, bd = q, d
        pt = best or (x, y)
        for w in blockers:                       # pushed out of any wall
            cs = wall_corners(w)
            if _in_poly(pt[0], pt[1], cs):
                cands = [_closest_on_seg(pt[0], pt[1], cs[i], cs[(i + 1) % 4])
                         for i in range(4)]
                pt = min(cands, key=lambda q: math.hypot(q[0] - pt[0],
                                                         q[1] - pt[1]))
                best = pt
        return (round(pt[0], 3), round(pt[1], 3)), best is not None

    def clamp_zone(self, p0, p1, zr):
        """Largest zone from p0 toward p1 that crosses no other wall."""
        blockers = [wall_corners(w) for w in self._blockers(zr)]

        def ok(q):
            rect = [(p0[0], p0[1]), (q[0], p0[1]), (q[0], q[1]), (p0[0], q[1])]
            return not any(_poly_overlap(rect, b, eps=0.0) for b in blockers)
        if ok(p1):
            return p1, False
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        lo, hi = 0.0, 1.0
        for _ in range(24):
            mid = (lo + hi) / 2
            if ok((p0[0] + mid * dx, p0[1] + mid * dy)):
                lo = mid
            else:
                hi = mid
        q = [p0[0] + lo * dx, p0[1] + lo * dy]
        for ax in ((0, 1) if abs(dx) >= abs(dy) else (1, 0)):
            lo2, hi2 = q[ax], p1[ax]
            for _ in range(24):
                mid = (lo2 + hi2) / 2
                t = list(q)
                t[ax] = mid
                if ok(t):
                    lo2 = mid
                else:
                    hi2 = mid
            q[ax] = lo2
        return (round(q[0], 3), round(q[1], 3)), True

    def zone_zrange(self):
        return wall_zrange(self.last_wall)

    def wall_from_zone(self, p0, p1):
        (x0, y0), (x1, y1) = p0, p1
        dx, dy = abs(x1 - x0), abs(y1 - y0)
        if max(dx, dy) < 0.3:
            self.msg = ('Wall: press and drag on the ground to draw a zone '
                        '(it stops at other walls).')
            return self.redraw()
        w = copy.deepcopy(self.last_wall)
        w.pop('id', None)
        w['name'] = self.unique_wall_name()
        w['group'], w['locked'] = '', False
        w['x'], w['y'] = round((x0 + x1) / 2, 3), round((y0 + y1) / 2, 3)
        if dx >= dy:
            w['length'], w['thickness'], w['rotation'] = dx, dy, 0.0
        else:
            w['length'], w['thickness'], w['rotation'] = dy, dx, 90.0
        w['length'] = round(max(w['length'], 0.1), 3)
        w['thickness'] = round(max(w['thickness'], 0.02), 3)
        if self.overlapping_wall(w):          # tiny min-thickness overlap
            w['thickness'] = round(max(dy if dx >= dy else dx, 0.001), 3)
        if self.wall_ask:
            res = WallDialog(self, w).result
            if res is None:
                self.msg = 'Wall cancelled.'
                return self.redraw()
            w = res
        elif self.check_wall(w):
            self.msg = '!  Wall not created: %s.' % self.check_wall(w)
            return self.redraw()
        keep = dict((k, w[k]) for k in ('material', 'loss', 'color', 'height',
                                         'z', 'thick_mult', 'ref_thickness'))
        self.last_wall.update(keep)
        self.add_wall(w)

    def duplicate_wall(self):
        w = self.sel_wall()
        if not w:
            self.msg = 'Select one wall to duplicate it.'
            return self.redraw()
        n = copy.deepcopy(w)
        n.pop('id', None)
        n['group'], n['locked'] = '', False
        a = math.radians(float(w['rotation']))
        step = max(float(w['thickness']) * 2, 1.0)
        for k in range(1, 40):                # first free spot beside it
            n['x'] = round(w['x'] - math.sin(a) * step * k, 3)
            n['y'] = round(w['y'] + math.cos(a) * step * k, 3)
            if not self.overlapping_wall(n):
                break
        n['name'] = self.unique_wall_name()
        self.add_wall(n)

    def wall_at(self, x, y, include_locked=False):
        for wid, pts in reversed(self.wall_hits):
            if _in_poly(x, y, pts) and (include_locked or
                                        not self.walls[wid].get('locked')):
                return wid
        return None

    def wall_handle_at(self, x, y):
        for h in self.wall_handles:
            if math.hypot(h[1] - x, h[2] - y) <= 8:
                return h
        return None

    # -------- groups and locks
    def group_walls(self):
        ids = self.sel_wall_ids()
        if len(ids) < 2:
            self.msg = ('Shift+click walls to select several, then Group '
                        '(Ctrl+G).')
            return self.redraw()
        names = set(w.get('group') for w in self.walls.values())
        i = 1
        while 'group%d' % i in names:
            i += 1
        self.push_undo()
        for wid in ids:
            self.walls[wid]['group'] = 'group%d' % i
        self.msg = 'Grouped %d walls as group%d: click one to select and ' \
                   'move them together.' % (len(ids), i)
        self.changed()

    def ungroup_walls(self):
        ids = self.sel_wall_ids()
        if not ids or not any(self.walls[i].get('group') for i in ids):
            self.msg = 'Select a grouped wall to ungroup it.'
            return self.redraw()
        self.push_undo()
        for wid in ids:
            for m in self.group_members(wid):
                self.walls[m]['group'] = ''
        self.msg = 'Ungrouped.'
        self.changed()

    def toggle_lock(self):
        ids = self.sel_wall_ids()
        if not ids:
            self.msg = 'Select a wall (double-click works on locked ones).'
            return self.redraw()
        self.push_undo()
        new = not all(self.walls[i].get('locked') for i in ids)
        for i in ids:
            self.walls[i]['locked'] = new
        self.msg = '%s %d wall%s.' % ('Locked' if new else 'Unlocked',
                                      len(ids), '' if len(ids) == 1 else 's')
        self.changed()

    def wall_click(self, wid, shift):
        """Select on click: whole group unless Shift adds / removes."""
        ids = set(self.sel_wall_ids())
        members = set(self.group_members(wid))
        if shift:
            ids = ids - members if wid in ids else ids | members
        elif not (wid in ids and len(ids) > 1):
            ids = members
        self.set_wall_selection(ids)
        return ids

    def move_snap(self, moving, dx, dy):
        """Adjust (dx, dy) so a moving corner docks onto a nearby wall."""
        tol = 12.0 / max(self.scale, 1e-9)
        ids = set(w['id'] for w in moving)
        best, bd = (0.0, 0.0), tol
        others = [o for i, o in self.walls.items() if i not in ids]
        for w in moving:
            zr = wall_zrange(w)
            for c in wall_corners(dict(w, x=w['x'] + dx, y=w['y'] + dy)):
                for o in others:
                    oz = wall_zrange(o)
                    if min(oz[1], zr[1]) - max(oz[0], zr[0]) <= 0.005:
                        continue
                    cs = wall_corners(o)
                    for i in range(4):
                        q = _closest_on_seg(c[0], c[1], cs[i], cs[(i + 1) % 4])
                        d = math.hypot(q[0] - c[0], q[1] - c[1])
                        if d < bd:
                            best, bd = (q[0] - c[0], q[1] - c[1]), d
        return dx + best[0], dy + best[1]

    def walls_crossing(self, w):
        """Station / car -> AP pairs (links or auto) that cross wall w."""
        out = []
        for aid, lst in self.associations().items():
            for i, how in lst:
                if wall_crossed(w, self.nodes[i]['xyz'], self.nodes[aid]['xyz']):
                    out.append('%s -> %s' % (self.nodes[i]['name'],
                                             self.nodes[aid]['name']))
        return out

    def sel_pic(self):
        if self.sel and self.sel[0] == 'pic':
            return self.pics.get(self.sel[1])
        return None

    def pil(self):
        """(Image, ImageTk or None) or None when Pillow is missing."""
        try:
            from PIL import Image
        except ImportError:
            return None
        try:
            from PIL import ImageTk
        except ImportError:
            ImageTk = None
        return Image, ImageTk

    def pic_source(self, pic, reduce=1):
        """Loaded RGBA image (cached, max 2048 px), or None."""
        key = (pic.get('path'), reduce)
        if key in self._pic_src:
            return self._pic_src[key]
        img = None
        lib = self.pil()
        if lib and pic.get('path'):
            Image = lib[0]
            try:
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
                    if base is not None:
                        img = base.reduce(reduce)
            except Exception:
                img = None
        self._pic_src[key] = img
        return img

    def pic_aspect(self, pic):
        src = self.pic_source(pic)
        if src is None:
            return 0.75
        return src.size[1] / float(src.size[0])

    def pic_corners(self, pic):
        """World corners: top-left, top-right, bottom-right, bottom-left."""
        W = float(pic['width'])
        H = W * self.pic_aspect(pic)
        a = math.radians(float(pic['rotation']))
        ca, sa = math.cos(a), math.sin(a)
        out = []
        for lx, ly in ((-W / 2, H / 2), (W / 2, H / 2), (W / 2, -H / 2),
                       (-W / 2, -H / 2)):
            out.append((pic['x'] + lx * ca - ly * sa,
                        pic['y'] + lx * sa + ly * ca, pic['z']))
        return out

    def insert_picture(self):
        if self.pil() is None:
            self.mb.showerror(
                'Insert picture', 'Pictures need Pillow:\n\n'
                '    sudo apt install python3-pil python3-pil.imagetk\n'
                'or  pip install pillow', parent=self.root)
            return
        path = self.fd.askopenfilename(
            parent=self.root, title='Insert picture',
            filetypes=[('Images', '*.png *.jpg *.jpeg *.gif *.bmp *.tif '
                        '*.tiff *.webp'), ('All files', '*')])
        if not path:
            return
        pic = default_pic(path)
        if self.pic_source(pic) is None:
            self._pic_src.pop((path, 1), None)
            self.mb.showerror('Insert picture', 'Could not open %s' % path,
                              parent=self.root)
            return
        self.add_picture(pic)

    def add_picture(self, pic):
        lo_x, lo_y, hi_x, hi_y = self.world_box()
        if not self.nodes and not self.pics:
            lo_x, lo_y, hi_x, hi_y = 0, 0, 100, 100
        pic['x'] = round((lo_x + hi_x) / 2, 2)
        pic['y'] = round((lo_y + hi_y) / 2, 2)
        pic['width'] = round(max(hi_x - lo_x, hi_y - lo_y, 20), 1)
        self.push_undo()
        pid = self.next_id
        self.next_id += 1
        pic['id'] = pid
        self.pics[pid] = pic
        self.msg = ('Inserted %s. Drag it to move, corner squares to scale, '
                    'the round handle to rotate.' % os.path.basename(
                        pic['path']))
        self.select(('pic', pid), quiet=True)
        self.changed()

    def select(self, sel, quiet=False):
        self.sel = sel
        self.sync_slider()
        if self.inspector:
            self.inspector.refresh()
        if not quiet:
            self.redraw()

    def changed(self, src=None):
        """Call after every model change."""
        self.dirty = True
        self.update_title()
        self.sync_slider()
        if self.inspector and src != 'inspector':
            self.inspector.refresh()
        self.redraw()

    def snap(self, v):
        if self.v_snap.get():
            return round(v / self.SNAP) * self.SNAP
        return v

    # ------------------------------------------------------------ undo ----
    def snapshot(self):
        return copy.deepcopy((self.nodes, self.links, self.netp,
                              self.next_id, self.pics, self.walls))

    def push_undo(self, key=None):
        """Save state before a change. Edits with the same key in a row
        (typing in one field, moving one slider) make a single undo step."""
        if key is not None and key == self._undo_key:
            return
        self.undo_stack.append(self.snapshot())
        del self.undo_stack[:-200]
        self.redo_stack = []
        self._undo_key = key

    def _restore(self, snap):
        (self.nodes, self.links, self.netp, self.next_id, self.pics,
         self.walls) = snap
        self._undo_key = None
        pool = {'node': self.nodes, 'link': self.links, 'pic': self.pics,
                'wall': self.walls}
        if self.sel and self.sel[0] == 'walls':
            ids = [i for i in self.sel[1] if i in self.walls]
            self.sel = ('walls', tuple(ids)) if len(ids) > 1 else (
                ('wall', ids[0]) if ids else None)
        elif self.sel and self.sel[1] not in pool[self.sel[0]]:
            self.sel = None
        self.changed()

    def undo(self):
        if self.undo_stack:
            self.redo_stack.append(self.snapshot())
            self._restore(self.undo_stack.pop())

    def redo(self):
        if self.redo_stack:
            self.undo_stack.append(self.snapshot())
            self._restore(self.redo_stack.pop())

    # ----------------------------------------------------- range slider ----
    def sync_slider(self):
        n = self.sel_node()
        self._lock = True
        try:
            if n and n['kind'] in WIRELESS_KINDS:
                r = self.rng(n)
                self.slider.config(state='normal',
                                   to=max(300, int(math.ceil(r * 1.5 / 50))
                                          * 50))
                self.slider.set(r)
                self.range_name.config(text='%s  (%s)' % (
                    n['name'], KIND_LABEL[n['kind']]), fg=C['text'])
                self.range_val.config(text='%s m' % fmt_num(r))
            else:
                self.slider.set(0)
                self.slider.config(state='disabled')
                self.range_name.config(text='select an AP, station or car',
                                       fg=C['muted'])
                self.range_val.config(text='')
        finally:
            self._lock = False

    def on_slider(self, val):
        if self._lock:
            return
        n = self.sel_node()
        if not n or n['kind'] not in WIRELESS_KINDS:
            return
        v = float(val)
        if v == self.rng(n):
            return
        self.push_undo(('slider', n['id']))
        n['p']['range'] = v
        self.range_val.config(text='%s m' % fmt_num(v))
        self.dirty = True
        self.update_title()
        if self.inspector:
            self.inspector.refresh()
        self.redraw()

    # ---------------------------------------------------------- camera ----
    def positions(self):
        return dict((nid, tuple(n['xyz'])) for nid, n in self.nodes.items())

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
        if not self.nodes and not self.pics and not self.walls:
            return 0.0, 0.0, 100.0, 100.0
        xs_lo, xs_hi, ys_lo, ys_hi = [], [], [], []
        for w in self.walls.values():
            for x, y in wall_corners(w):
                xs_lo.append(x); xs_hi.append(x)
                ys_lo.append(y); ys_hi.append(y)
        for pic in self.pics.values():
            if pic.get('visible', True):
                for x, y, _ in self.pic_corners(pic):
                    xs_lo.append(x); xs_hi.append(x)
                    ys_lo.append(y); ys_hi.append(y)
        for n in self.nodes.values():
            r = self.rng(n)
            x, y, _ = n['xyz']
            xs_lo.append(x - r); xs_hi.append(x + r)
            ys_lo.append(y - r); ys_hi.append(y + r)
        if not xs_lo:
            return 0.0, 0.0, 100.0, 100.0
        lo_x, hi_x, lo_y, hi_y = min(xs_lo), max(xs_hi), min(ys_lo), max(ys_hi)
        for lo, hi, i in ((lo_x, hi_x, 0), (lo_y, hi_y, 1)):
            if hi - lo < 40:
                c = (lo + hi) / 2
                if i == 0:
                    lo_x, hi_x = c - 20, c + 20
                else:
                    lo_y, hi_y = c - 20, c + 20
        return lo_x, lo_y, hi_x, hi_y

    def fit(self):
        lo_x, lo_y, hi_x, hi_y = self.world_box()
        zs = [n['xyz'][2] for n in self.nodes.values()] or [0]
        self.target = ((lo_x + hi_x) / 2, (lo_y + hi_y) / 2,
                       (min(zs) + max(zs)) / 2)
        w, h = self.size()
        span = max(hi_x - lo_x, hi_y - lo_y, 1)
        self.scale = 0.75 * min(w, h) / span
        self.pan = [0.0, 0.0]
        self.redraw()

    def view_top(self):
        self.yaw, self.pitch = 0.0, 0.0
        self.fit()

    def view_3d(self):
        self.yaw, self.pitch = math.radians(-30), math.radians(55)
        self.fit()

    def on_resize(self, e):
        if self.user_view:
            self.redraw()
        else:
            self.fit()

    # ----------------------------------------------------------- mouse ----
    def node_at(self, x, y):
        best, bd = None, self.HIT
        for nid, sx, sy in self.hits:
            d = math.hypot(sx - x, sy - y)
            if d <= bd:
                best, bd = nid, d
        return best

    def link_at(self, x, y):
        best, bd = None, 6.0
        for lid, x1, y1, x2, y2 in self.link_hits:
            d = _seg_dist(x, y, x1, y1, x2, y2)
            if d <= bd:
                best, bd = lid, d
        return best

    def pic_at(self, x, y):
        """Topmost picture whose screen quad contains (x, y)."""
        for pid in sorted(self.pics, reverse=True):
            pic = self.pics[pid]
            if not pic.get('visible', True):
                continue
            pts = [self.project(*c)[:2] for c in self.pic_corners(pic)]
            inside = False
            j = len(pts) - 1
            for i in range(len(pts)):
                xi, yi = pts[i]
                xj, yj = pts[j]
                if (yi > y) != (yj > y) and \
                        x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi:
                    inside = not inside
                j = i
            if inside:
                return pid
        return None

    def pic_handle_at(self, x, y):
        for h in self.pic_hits:
            if math.hypot(h[1] - x, h[2] - y) <= 8:
                return h
        return None

    def axis_at(self, x, y):
        for h in self.axis_hits:
            axis, x1, y1, x2, y2, L = h
            if _seg_dist(x, y, x1, y1, x2, y2) <= 7:
                return h
        return None

    def on_press(self, e):
        self.canvas.focus_set()
        self.press = (e.x, e.y)
        w = self.sel_wall()
        if w and self.tool == 'select' and not w.get('locked'):
            h = self.wall_handle_at(e.x, e.y)
            if h:
                c = self.project(w['x'], w['y'], w['z'])
                u = self.project(w['x'], w['y'], w['z'] + 1.0)
                mx, my = self.unproject(e.x, e.y, w['z'])
                self.drag = {'mode': 'wall_' + h[0], 'id': w['id'],
                             'moved': False, 'w0': dict(w), 'sign': h[3],
                             'start': (e.x, e.y),
                             'zv': (u[0] - c[0], u[1] - c[1]),
                             'a0': math.atan2(my - w['y'], mx - w['x'])}
                return
        if self.tool == 'wall':
            x, y = self.unproject(e.x, e.y, 0.0)
            p, snapped = self.snap_wall_point(self.snap(x), self.snap(y),
                                              self.zone_zrange())
            self.drag = {'mode': 'wall_draw', 'p0': p, 'p1': p,
                         'moved': False, 'snap': p if snapped else None,
                         'snap0': snapped, 'blocked': False}
            return
        if self.tool != 'link' and self.sel_node():
            h = self.axis_at(e.x, e.y)
            if h:
                axis, x1, y1, x2, y2, L = h
                n = self.sel_node()
                o = self.project(*n['xyz'])
                self.drag = {'mode': 'axis', 'id': n['id'], 'axis': axis,
                             'start': (e.x, e.y), 'xyz0': list(n['xyz']),
                             'v': ((x2 - o[0]) / L, (y2 - o[1]) / L),
                             'moved': False}
                return
        nid = self.node_at(e.x, e.y)
        if self.tool == 'link':
            if nid:
                self.drag = {'mode': 'link', 'from': nid, 'to': (e.x, e.y)}
            else:
                self.drag = {'mode': 'rotate', 'last': (e.x, e.y),
                             'moved': False}
            return
        if nid:
            if self.sel != ('node', nid):
                self.select(('node', nid))
            n = self.nodes[nid]
            self.drag = {'mode': 'node', 'id': nid, 'xyz': list(n['xyz']),
                         'start': (e.x, e.y), 'moved': False}
            return
        pic = self.sel_pic()
        if pic and self.tool == 'select' and not pic.get('locked'):
            h = self.pic_handle_at(e.x, e.y)
            if h:
                mx, my = self.unproject(e.x, e.y, pic['z'])
                c = (pic['x'], pic['y'])
                self.drag = {'mode': 'pic_' + h[0], 'id': pic['id'],
                             'moved': False, 'w0': pic['width'],
                             'r0': pic['rotation'],
                             'd0': max(math.hypot(mx - c[0], my - c[1]), 1e-6),
                             'a0': math.atan2(my - c[1], mx - c[0])}
                return
        lid = self.link_at(e.x, e.y)
        if lid and self.tool == 'select':
            self.select(('link', lid))
            self.drag = None
            return
        if self.tool in KINDS:
            self.add_node(self.tool, e.x, e.y)
            return
        wid = self.wall_at(e.x, e.y) if self.tool == 'select' else None
        if wid is not None:
            ids = self.wall_click(wid, bool(e.state & 0x0001))
            if e.state & 0x0001 or wid not in ids:   # Shift: just (de)select
                self.drag = None
                return
            moving = [self.walls[i] for i in ids]
            locked = [w['name'] for w in moving if w.get('locked')]
            if locked:
                self.msg = '!  Cannot move: %s locked.' % ', '.join(locked)
                self.drag = None
                return self.redraw()
            z = self.walls[wid]['z']
            mx, my = self.unproject(e.x, e.y, z)
            self.drag = {'mode': 'walls_move', 'ids': sorted(ids),
                         'moved': False, 'm0': (mx, my), 'z': z,
                         'orig': dict((w['id'], (w['x'], w['y']))
                                      for w in moving)}
            return
        pid = self.pic_at(e.x, e.y) if self.tool == 'select' else None
        if pid is not None and pic is not None and pid == pic['id'] and \
                not pic.get('locked'):
            mx, my = self.unproject(e.x, e.y, pic['z'])
            self.drag = {'mode': 'pic_move', 'id': pid, 'moved': False,
                         'off': (pic['x'] - mx, pic['y'] - my)}
            return
        # empty space or an unselected picture: rotate the view;
        # a click without dragging selects the picture
        self.drag = {'mode': 'rotate', 'last': (e.x, e.y), 'moved': False,
                     'pick': pid}

    def on_drag(self, e):
        d = self.drag
        if not d:
            return
        self.mouse = (e.x, e.y)
        far = math.hypot(e.x - self.press[0], e.y - self.press[1]) > 3
        if d['mode'] == 'rotate':
            if far:
                d['moved'] = True
            self.user_view = True
            lx, ly = d['last']
            self.yaw += (e.x - lx) * 0.01
            self.pitch = min(max(self.pitch + (e.y - ly) * 0.01, 0.0),
                             math.radians(85))
            d['last'] = (e.x, e.y)
        elif d['mode'] == 'link':
            d['to'] = (e.x, e.y)
            self.hover = self.node_at(e.x, e.y)
        elif d['mode'] == 'wall_draw':
            x, y = self.unproject(e.x, e.y, 0.0)
            zr = self.zone_zrange()
            p1, snapped = self.snap_wall_point(self.snap(x), self.snap(y), zr)
            p1, blocked = self.clamp_zone(d['p0'], p1, zr)
            d['p1'], d['blocked'] = p1, blocked
            d['snap'] = p1 if (snapped or blocked) else None
            d['moved'] = d['moved'] or far
        elif d['mode'] == 'walls_move':
            if not d['moved']:
                if not far:
                    return
                d['moved'] = True
                self.push_undo()
            mx, my = self.unproject(e.x, e.y, d['z'])
            dx, dy = mx - d['m0'][0], my - d['m0'][1]
            first = d['orig'][d['ids'][0]]
            if self.v_snap.get():
                dx = self.snap(first[0] + dx) - first[0]
                dy = self.snap(first[1] + dy) - first[1]
            moving = [self.walls[i] for i in d['ids'] if i in self.walls]
            ids = set(d['ids'])
            for w in moving:                     # snap from the start pose
                w['x'], w['y'] = d['orig'][w['id']]
            dx, dy = self.move_snap(moving, dx, dy)
            last = d.get('last', (0.0, 0.0))

            def place(ddx, ddy):
                for w in moving:
                    ox, oy = d['orig'][w['id']]
                    w['x'], w['y'] = round(ox + ddx, 3), round(oy + ddy, 3)
                return not any(self.overlapping_wall(w, ids) for w in moving)
            # full move, else slide along the obstacle, else stay
            for cand in ((dx, dy), (dx, last[1]), (last[0], dy), last):
                if place(*cand):
                    d['last'] = cand
                    break
            self.dirty = True
            self.update_title()
            if self.inspector:
                self.inspector.refresh()
        elif d['mode'].startswith('wall_'):
            if not d['moved']:
                if not far:
                    return
                d['moved'] = True
                self.push_undo()
            w = self.walls.get(d['id'])
            if w is None:
                return
            mx, my = self.unproject(e.x, e.y, w['z'])
            prev = dict(w)
            if True:
                w0 = d['w0']
                a = math.radians(float(w0['rotation']))
                ux, uy = math.cos(a), math.sin(a)          # along the wall
                px, py = -uy, ux                           # across
                if d['mode'] == 'wall_end':
                    sg = d['sign']
                    ox = w0['x'] - sg * ux * w0['length'] / 2   # fixed end
                    oy = w0['y'] - sg * uy * w0['length'] / 2
                    L = max(self.snap((mx - ox) * ux * sg + (my - oy) * uy * sg),
                            0.1)
                    w['length'] = round(L, 2)
                    w['x'] = round(ox + sg * ux * L / 2, 2)
                    w['y'] = round(oy + sg * uy * L / 2, 2)
                elif d['mode'] == 'wall_thick':
                    t = abs((mx - w0['x']) * px + (my - w0['y']) * py) * 2
                    w['thickness'] = round(max(t, 0.02), 2)
                elif d['mode'] == 'wall_rot':
                    ang = float(w0['rotation']) + math.degrees(
                        math.atan2(my - w0['y'], mx - w0['x']) - d['a0'])
                    if e.state & 0x0001:                    # Shift: 15 deg
                        ang = round(ang / 15.0) * 15.0
                    w['rotation'] = round((ang + 180) % 360 - 180, 1)
                elif d['mode'] == 'wall_height':
                    vx, vy = d['zv']
                    dx, dy = e.x - d['start'][0], e.y - d['start'][1]
                    t = (dx * vx + dy * vy) / max(vx * vx + vy * vy, 1e-9)
                    h = max(float(w0['height']) + t, 0.0)
                    w['height'] = round(self.snap(h) if self.v_snap.get()
                                        else h, 2)
                if self.overlapping_wall(w):     # would cross another wall
                    w.update(prev)
            self.dirty = True
            self.update_title()
            if self.inspector:
                self.inspector.refresh()
        elif d['mode'].startswith('pic_'):
            if not d['moved']:
                if not far:
                    return
                d['moved'] = True
                self.push_undo()
            pic = self.pics.get(d['id'])
            if pic is None:
                return
            mx, my = self.unproject(e.x, e.y, pic['z'])
            if d['mode'] == 'pic_move':
                pic['x'] = round(self.snap(mx + d['off'][0]), 2)
                pic['y'] = round(self.snap(my + d['off'][1]), 2)
            elif d['mode'] == 'pic_scale':
                k = math.hypot(mx - pic['x'], my - pic['y']) / d['d0']
                pic['width'] = round(max(d['w0'] * k, 0.5), 2)
            else:                                           # pic_rot
                a = d['r0'] + math.degrees(
                    math.atan2(my - pic['y'], mx - pic['x']) - d['a0'])
                if e.state & 0x0001:                        # Shift: 15 deg
                    a = round(a / 15.0) * 15.0
                pic['rotation'] = round((a + 180) % 360 - 180, 1)
            self.dirty = True
            self.update_title()
            if self.inspector:
                self.inspector.refresh()
        elif d['mode'] in ('node', 'axis'):
            if not d['moved']:
                if not far:
                    return
                d['moved'] = True
                self.push_undo()
            n = self.nodes.get(d['id'])
            if n is None:
                return
            if d['mode'] == 'axis':
                vx, vy = d['v']
                dx, dy = e.x - d['start'][0], e.y - d['start'][1]
                t = (dx * vx + dy * vy) / max(vx * vx + vy * vy, 1e-9)
                xyz = list(d['xyz0'])
                xyz[d['axis']] = self.snap(xyz[d['axis']] + t)
                if d['axis'] == 2:
                    xyz[2] = max(xyz[2], 0.0)
            else:
                x, y, z = d['xyz']
                if e.state & 0x0001:                   # Shift: height
                    z = max(z - (e.y - d['start'][1]) / self.scale, 0.0)
                    d['start'] = (e.x, e.y)
                else:
                    x, y = self.unproject(e.x, e.y, z)
                d['xyz'] = [x, y, z]
                xyz = [self.snap(x), self.snap(y), self.snap(z) if
                       e.state & 0x0001 and self.v_snap.get() else z]
            n['xyz'] = [round(v, 2) for v in xyz]
            self.dirty = True
            self.update_title()
            if self.inspector:
                self.inspector.refresh()
        self.redraw()

    def on_release(self, e):
        d, self.drag = self.drag, None
        if not d:
            return
        if d['mode'] == 'rotate' and not d['moved'] and self.tool == 'select':
            self.select(('pic', d['pick']) if d.get('pick') is not None
                        else None)
            return
        if d['mode'] == 'wall_draw':
            self.wall_from_zone(d['p0'], d['p1'])
            return
        if (d['mode'].startswith('wall_') or d['mode'] == 'walls_move') \
                and d['moved']:
            self.changed()
            return
        if d['mode'].startswith('pic_') and d['moved']:
            self.changed()
            return
        if d['mode'] == 'link':
            to = self.node_at(e.x, e.y)
            if to and to != d['from']:
                self.add_link(d['from'], to)
                return
        if d['mode'] in ('node', 'axis') and d['moved']:
            self.changed()
            return
        self.redraw()

    def on_double(self, e):
        nid = self.node_at(e.x, e.y)
        lid = None if nid else self.link_at(e.x, e.y)
        if nid:
            self.select(('node', nid))
            self.open_inspector()
        elif lid:
            self.select(('link', lid))
            self.open_inspector()
        elif self.tool == 'select' and \
                self.wall_at(e.x, e.y, include_locked=True) is not None:
            # one wall, even if grouped or locked: inspect it
            self.select(('wall', self.wall_at(e.x, e.y, include_locked=True)))
            self.open_inspector()
        elif self.tool == 'select' and self.pic_at(e.x, e.y) is not None:
            self.select(('pic', self.pic_at(e.x, e.y)))
            self.open_inspector()
        elif self.tool == 'select':
            self.user_view = False
            self.fit()

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

    def set_hover(self, node, axis):
        if (node, axis) != (self.hover, self.hover_axis):
            self.hover, self.hover_axis = node, axis
            self.redraw()

    def on_motion(self, e):
        self.mouse = (e.x, e.y)
        h = self.axis_at(e.x, e.y) if self.tool != 'link' else None
        axis = h[0] if h else None
        node = None if h else self.node_at(e.x, e.y)
        ph = (self.pic_handle_at(e.x, e.y) or self.wall_handle_at(e.x, e.y)) \
            if self.tool == 'select' else None
        if ph:
            self.canvas.config(cursor='exchange' if ph[0] == 'rot'
                               else 'sizing')
        elif axis is not None:
            self.canvas.config(cursor='hand2')
        elif node:
            self.canvas.config(cursor='fleur' if self.tool != 'link'
                               else 'crosshair')
        else:
            self.canvas.config(cursor='' if self.tool == 'select'
                               else 'crosshair')
        if (node, axis) != (self.hover, self.hover_axis) or node:
            self.hover, self.hover_axis = node, axis
            self.redraw()

    def on_key(self, e):
        cls = e.widget.winfo_class() if hasattr(e.widget, 'winfo_class') \
            else ''
        if cls in ('Entry', 'TEntry', 'TCombobox', 'Text', 'Spinbox'):
            return
        if e.state & 0x0004:                           # Ctrl combos
            return
        k = e.keysym
        if k in ('Delete', 'BackSpace'):
            self.delete_selection()
        elif k == 'Escape':
            self.drag = None
            self.set_tool('select')
        elif k in ('i', 'I'):
            self.open_inspector()
        elif k in ('l', 'L'):
            self.set_tool('link')
        elif k in ('p', 'P'):
            self.insert_picture()
        elif k in ('w', 'W'):
            self.set_tool('wall')
        elif k in ('t', 'T'):
            self.view_top()
        elif k == '3':
            self.view_3d()
        elif k in ('f', 'F'):
            self.fit()

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
        if fill:
            cv.create_oval(cx - R, cy - R, cx + R, cy + R,
                           fill=fill, stipple='gray12', outline='')
        inner = _mix(color, C['bg'], soft)
        for lat in rings:
            a = math.radians(lat)
            pts = self.circle_pts(x, y, z + r * math.sin(a),
                                  r * math.cos(a), 40)
            cv.create_polygon(pts, fill='', outline=inner,
                              width=1.5 if lat == 0 else 1, dash=dash)
        for k in range(meridians):
            az = math.pi * k / meridians
            pts = []
            for i in range(41):
                t = 2 * math.pi * i / 40
                sx, sy, _ = self.project(x + r * math.cos(t) * math.cos(az),
                                         y + r * math.cos(t) * math.sin(az),
                                         z + r * math.sin(t))
                pts += [sx, sy]
            cv.create_line(*pts, fill=inner, dash=dash)
        cv.create_oval(cx - R, cy - R, cx + R, cy + R,
                       outline=color, width=width, dash=dash)

    def redraw(self):
        if not hasattr(self, 'canvas'):
            return
        cv = self.canvas
        cv.delete('all')
        pos = self.positions()
        sel_node = self.sel[1] if self.sel and self.sel[0] == 'node' else None
        sel_link = self.sel[1] if self.sel and self.sel[0] == 'link' else None
        sel_pic = self.sel[1] if self.sel and self.sel[0] == 'pic' else None
        sel_wall = self.sel[1] if self.sel and self.sel[0] == 'wall' else None
        sel_walls = set(self.sel_wall_ids())
        self._assoc = assoc = self.associations()

        for pid in sorted(self.pics):                  # pictures under all
            self.draw_picture(self.pics[pid], pid == sel_pic)
        self.draw_grid()

        # ranges (outline only = just the boundaries, see what is under)
        if self.v_ranges.get():
            sphere = self.v_sphere.get()
            outline = self.v_outline.get()
            order = sorted(self.nodes.values(), key=lambda n: n['kind'] != 'ap')
            for n in order:
                r = self.rng(n)
                if r <= 0:
                    continue
                p = pos[n['id']]
                is_ap = n['kind'] == 'ap'
                col = C['ap'] if is_ap else C[n['kind']]
                wd = 2.8 if n['id'] == sel_node else (1.5 if is_ap else 1.2)
                if sphere:
                    if is_ap and outline:
                        self.sphere(p, r, col, width=wd, rings=(0,),
                                    meridians=0)
                    elif is_ap:
                        self.sphere(p, r, col, width=wd, fill=C['ap_fill'])
                    else:
                        self.sphere(p, r, col, width=wd, dash=(4, 4),
                                    rings=(0,), meridians=1)
                else:
                    pts = self.circle_pts(*p, r)
                    if is_ap:
                        if not outline:
                            cv.create_polygon(pts, fill=C['ap_fill'],
                                              outline='')
                        cv.create_polygon(pts, fill='', outline=col, width=wd)
                    else:
                        cv.create_polygon(pts, fill='', outline=col, width=wd,
                                          dash=(4, 4))

        # walls: flat ones are drawn now, 3D faces are depth-sorted below
        wall_items = self.wall_items(pos, sel_walls)

        # height stems + ground shadows
        for nid, p in pos.items():
            if abs(p[2]) > 1e-6:
                gx, gy, _ = self.project(p[0], p[1], 0)
                sx, sy, _ = self.project(*p)
                cv.create_oval(gx - 7, gy - 3, gx + 7, gy + 3,
                               fill=C['shadow'], outline='')
                cv.create_line(gx, gy, sx, sy, fill=C['axis'], dash=(2, 3))

        # control links (automatic)
        labels = self.v_labels.get()
        ctrls = [n for n in self.nodes.values() if n['kind'] == 'ctrl']
        for n in self.nodes.values():
            if n['kind'] in ('ap', 'switch') and \
                    n['p'].get('failMode') != 'standalone':
                for c in ctrls:
                    x1, y1, _ = self.project(*pos[c['id']])
                    x2, y2, _ = self.project(*pos[n['id']])
                    cv.create_line(x1, y1, x2, y2, fill=C['control'], width=2,
                                   dash=(8, 5))
                    if labels:
                        cv.create_text((x1 + x2) / 2, (y1 + y2) / 2 - 8,
                                       text='OpenFlow', fill=C['control'],
                                       font=('TkDefaultFont', 8))

        # user links
        self.link_hits = []
        for lid, l in self.links.items():
            if l['a'] not in pos or l['b'] not in pos:
                continue
            x1, y1, _ = self.project(*pos[l['a']])
            x2, y2, _ = self.project(*pos[l['b']])
            if lid == sel_link:
                cv.create_line(x1, y1, x2, y2, fill=C['halo'], width=10,
                               capstyle='round')
            if l['kind'] == 'wifi':
                ap = self.nodes[l['b']]
                rssi, wl, whit = self.est_rssi(self.nodes[l['a']], ap)
                ok = rssi >= self.noise_th()
                col = C['rf'] if ok else C['rf_bad']
                cv.create_line(x1, y1, x2, y2, fill=C['rf_glow'] if ok
                               else '#f3d0cc', width=7, capstyle='round')
                cv.create_line(x1, y1, x2, y2, fill=col, width=2.5)
                # little Wi-Fi symbol in the middle
                mx, my = (x1 + x2) / 2, (y1 + y2) / 2
                for r in (4, 8):
                    cv.create_arc(mx - r, my - r + 3, mx + r, my + r + 3,
                                  start=45, extent=90, style='arc',
                                  outline=col, width=2)
                cv.create_oval(mx - 1.5, my + 1.5, mx + 1.5, my + 4.5,
                               fill=col, outline='')
                txt = 'Wi-Fi  %d dBm%s%s' % (
                    round(rssi), ('  (-%s dB walls)' % fmt_num(wl)) if whit
                    else '', '' if ok else '  NO SIGNAL')
                if labels:
                    cv.create_text(mx, my - 14, text=txt, fill=col,
                                   font=('TkDefaultFont', 8, 'bold'))
                self.link_hits.append((lid, x1, y1, x2, y2))
                continue
            if l['kind'] == 'wired':
                cv.create_line(x1, y1, x2, y2, fill=C['wired'], width=3)
                txt = ' '.join(s for s in (
                    ('%s Mb/s' % fmt_num(l['p'].get('bw')))
                    if l['p'].get('bw') is not None else '',
                    l['p'].get('delay') or '') if s)
            else:
                cv.create_line(x1, y1, x2, y2, fill=C['rf'], width=2.5,
                               dash=(6, 4))
                txt = '%s  %s  ch %s' % (l['kind'], l['p'].get('ssid') or '',
                                         l['p'].get('channel') or '-')
            if labels and txt:
                cv.create_text((x1 + x2) / 2, (y1 + y2) / 2 - 10, text=txt,
                               fill=C['wired'] if l['kind'] == 'wired'
                               else C['rf'], font=('TkDefaultFont', 8, 'bold'))
            self.link_hits.append((lid, x1, y1, x2, y2))

        # automatic associations (no explicit link, inside an AP range)
        if self.v_clients.get():
            for aid, lst in assoc.items():
                for i, how in lst:
                    if how == 'auto':
                        x1, y1, _ = self.project(*pos[i])
                        x2, y2, _ = self.project(*pos[aid])
                        cv.create_line(x1, y1, x2, y2, fill=C['rf'], width=1.5,
                                       dash=(2, 4))

        # rubber band while drawing a link
        d = self.drag
        if d and d.get('mode') == 'link' and d['from'] in pos:
            x1, y1, _ = self.project(*pos[d['from']])
            col = C['wired'] if self.link_kind.get() == 'wired' else C['rf']
            cv.create_line(x1, y1, d['to'][0], d['to'][1], fill=col, width=2,
                           dash=(4, 3))

        # nodes and 3D wall faces together, far ones first (painter)
        self.hits = []
        items = list(wall_items)
        for nid, p in pos.items():
            sx, sy, dd = self.project(*p)
            items.append((dd, 'node', nid, sx, sy))
        items.sort(key=lambda it: -it[0])
        for it in items:
            if it[1] == 'node':
                self.draw_node(self.nodes[it[2]], it[3], it[4],
                               it[2] == sel_node)
            else:
                it[2]()
                self.wall_hits.append((it[3], it[4]))
        for fn in self._wall_late:                  # labels, ghost tops
            fn()

        # wall being drawn
        d = self.drag
        if d and d.get('mode') == 'wall_draw':
            (x0, y0), (x1, y1) = d['p0'], d['p1']
            pts = [self.project(x, y, 0)[:2] for x, y in
                   ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
            col = self.last_wall.get('color') or C['wired']
            cv.create_polygon([v for q in pts for v in q], fill=col,
                              stipple='gray50', outline=C['select'],
                              dash=(4, 3), width=1.5)
            cx = sum(q[0] for q in pts) / 4
            cy = sum(q[1] for q in pts) / 4
            cv.create_text(cx, cy, text='%s x %s m%s' % (
                fmt_num(round(abs(x1 - x0), 2)),
                fmt_num(round(abs(y1 - y0), 2)),
                '  (stopped at a wall)' if d.get('blocked') else ''),
                fill=C['warn'] if d.get('blocked') else C['text'],
                font=('TkDefaultFont', 9, 'bold'))
            for q in (d['p0'] if d.get('snap0') else None, d.get('snap')):
                if q:
                    sx, sy, _ = self.project(q[0], q[1], 0)
                    cv.create_oval(sx - 6, sy - 6, sx + 6, sy + 6,
                                   outline=C['ap'], width=2)
        self.wall_handles = []
        if sel_wall in self.walls and self.tool == 'select' and \
                not self.walls[sel_wall].get('locked'):
            self.draw_wall_handles(self.walls[sel_wall])

        # local axes of the selected device
        self.axis_hits = []
        if sel_node in self.nodes:
            self.draw_axes(self.nodes[sel_node])
        self.pic_hits = []
        if sel_pic in self.pics:
            self.draw_pic_handles(self.pics[sel_pic])

        self.draw_overlay()
        self.update_status()

    def view_grad(self):
        """World vector pointing away from the camera (depth gradient)."""
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

    def wall_items(self, pos, sel_wall):
        """Draws flat (2D) walls now; returns depth-sorted items for the
        faces of 3D walls. Camera modes: solid, fade (walls in front of the
        camera become see-through), cutaway (lowered to a stub, like in
        building games), outline."""
        cv = self.canvas
        self.wall_hits, self._wall_late = [], []
        items = []
        if not self.v_walls.get() or not self.walls:
            return items
        mode = self.wall_mode()
        g = self.view_grad()
        tdepth = self.project(*self.target)[2]
        light = (-0.40, -0.55, 0.73)
        labels = self.v_labels.get()
        node_scr = [self.project(*p) for p in pos.values()]
        tilted = self.pitch > math.radians(8)
        for wid, w in sorted(self.walls.items()):
            col = w.get('color') or '#999999'
            dark = _mix(col, '#000000', 0.4)
            glass = w.get('material') == 'glass'
            sel = wid in sel_wall
            h, z0 = float(w['height']), float(w['z'])
            cs = wall_corners(w)
            if h <= 0:                                   # flat 2D wall
                pts = [self.project(x, y, z0)[:2] for x, y in cs]
                cv.create_polygon([v for q in pts for v in q],
                                  fill='' if mode == 'outline' else col,
                                  stipple='gray50',
                                  outline=C['ap'] if sel else dark,
                                  width=2.2 if sel else 1.2)
                self.wall_hits.append((wid, pts))
                if labels:
                    cx = sum(q[0] for q in pts) / 4
                    cy = sum(q[1] for q in pts) / 4
                    self._wall_late.append(lambda cx=cx, cy=cy, w=w, d=dark:
                                           self.wall_label(w, cx, cy, d))
                continue
            faces = [f for f in self.box_faces(cs, z0, z0 + h, w['rotation'])
                     if f[1][0] * g[0] + f[1][1] * g[1] + f[1][2] * g[2]
                     < -1e-6]
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
                                                   w['rotation'])
                         if f[1][0] * g[0] + f[1][1] * g[1] +
                         f[1][2] * g[2] < -1e-6]
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
                depth = sum(q[2] for q in sp) / len(sp)
                flat = [v for q in pts for v in q]

                def draw(flat=flat, fill=fill, stipple=stipple, dash=dash,
                         sel=sel, dark=dark):
                    kw = {'stipple': stipple} if stipple and fill else {}
                    self.canvas.create_polygon(
                        flat, fill=fill, outline=C['ap'] if sel else dark,
                        width=2.2 if sel else 1, dash=dash, **kw)
                items.append((depth, 'face', draw, wid, pts))
            if labels:
                lx, ly, _ = self.project(w['x'], w['y'], z0 + top_h)
                self._wall_late.append(lambda lx=lx, ly=ly, w=w, d=dark:
                                       self.wall_label(w, lx, ly - 9, d))
        return items

    def wall_label(self, w, x, y, col):
        self.canvas.create_text(
            x, y, text='%s · %s %s dB%s%s' % (
                w['name'], w['material'], fmt_num(round(wall_db(w), 1)),
                '  [%s]' % w['group'] if w.get('group') else '',
                '  (locked)' if w.get('locked') else ''),
            fill=col, font=('TkDefaultFont', 8, 'bold'))

    def draw_wall_handles(self, w):
        """Ends: length. Side: thickness. Round: rotate. Top: height."""
        cv = self.canvas
        a = math.radians(float(w['rotation']))
        ux, uy = math.cos(a), math.sin(a)
        px, py = -uy, ux
        h, z0 = float(w['height']), float(w['z'])
        zm = z0 + h / 2
        L, T = float(w['length']) / 2, float(w['thickness']) / 2
        c = self.project(w['x'], w['y'], zm)
        for sg in (1, -1):
            x, y, _ = self.project(w['x'] + sg * ux * L, w['y'] + sg * uy * L,
                                   zm)
            cv.create_rectangle(x - 5, y - 5, x + 5, y + 5, fill='white',
                                outline=C['ap'], width=2)
            self.wall_handles.append(('end', x, y, sg))
        x, y, _ = self.project(w['x'] + px * T, w['y'] + py * T, zm)
        cv.create_oval(x - 4, y - 4, x + 4, y + 4, fill='white',
                       outline=C['ap'], width=2)
        self.wall_handles.append(('thick', x, y, 0))
        dx, dy = x - c[0], y - c[1]
        Ls = math.hypot(dx, dy) or 1
        if Ls < 2:
            dx, dy, Ls = 0, -1, 1
        rx, ry = x + dx / Ls * 26, y + dy / Ls * 26
        cv.create_line(x, y, rx, ry, fill=C['ap'], width=2)
        cv.create_oval(rx - 6, ry - 6, rx + 6, ry + 6, fill=C['ap'],
                       outline='white', width=2)
        self.wall_handles.append(('rot', rx, ry, 0))
        if self.pitch > math.radians(8):                 # height handle
            tx, ty, _ = self.project(w['x'], w['y'], z0 + h)
            cv.create_polygon(tx, ty - 8, tx + 6, ty, tx, ty + 8, tx - 6, ty,
                              fill=C['z'], outline='white', width=1.5)
            self.wall_handles.append(('height', tx, ty, 0))

    def draw_picture(self, pic, selected):
        """Draw a picture lying on the plane z = pic['z'].
        The view is an orthographic projection, so the image is mapped with
        one affine transform (Pillow) and cached until something changes."""
        cv = self.canvas
        corners = [self.project(*c)[:2] for c in self.pic_corners(pic)]
        flat = [v for pt in corners for v in pt]
        if not pic.get('visible', True):
            if selected:
                cv.create_polygon(flat, fill='', outline=C['muted'],
                                  dash=(3, 3))
            return
        lib = self.pil()
        src = self.pic_source(pic)
        if src is None:                                 # placeholder
            cv.create_polygon(flat, fill=C['grid'], outline=C['axis'],
                              dash=(4, 3))
            cx = sum(p[0] for p in corners) / 4
            cy = sum(p[1] for p in corners) / 4
            cv.create_text(cx, cy, fill=C['warn'], justify='center',
                           font=('TkDefaultFont', 9, 'bold'),
                           text='Pillow not installed' if lib is None else
                           'picture not found:\n%s' % os.path.basename(
                               pic.get('path') or '?'))
            return
        (p0x, p0y), (p1x, p1y), _, (p3x, p3y) = corners
        # pick a reduced copy when the image is shrunk a lot (less aliasing)
        px_per_screen = src.size[0] / max(math.hypot(p1x - p0x, p1y - p0y), 1)
        k = 1
        while k < 8 and px_per_screen / (k * 2) >= 1.5:
            k *= 2
        if k > 1:
            src = self.pic_source(pic, k) or src
        iw, ih = src.size
        ax, ay = (p1x - p0x) / iw, (p1y - p0y) / iw
        bx, by = (p3x - p0x) / ih, (p3y - p0y) / ih
        det = ax * by - bx * ay
        if abs(det) < 1e-9:                             # seen edge-on
            cv.create_line(flat[:4], fill=C['axis'])
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
        key = (pic.get('path'), k, x1 - x0, y1 - y0, op,
               tuple(round(c, 6) for c in coef))
        cached = self._pic_tk.get(pic['id'])
        if cached and cached[0] == key:
            img = cached[1]
        else:
            Image, ImageTk = lib
            out = src.transform((x1 - x0, y1 - y0), Image.AFFINE, coef,
                                resample=Image.BILINEAR)
            if op < 100:
                out.putalpha(out.getchannel('A').point(
                    lambda a: a * op // 100))
            if ImageTk is not None:
                img = ImageTk.PhotoImage(out, master=self.root)
            else:                         # no ImageTk: go through PNG data
                import base64
                import io
                buf = io.BytesIO()
                out.save(buf, 'PNG', compress_level=1)
                img = self.tk.PhotoImage(
                    master=self.root,
                    data=base64.b64encode(buf.getvalue()).decode('ascii'))
            self._pic_tk[pic['id']] = (key, img)
        cv.create_image(x0, y0, image=img, anchor='nw')
        if selected:
            cv.create_polygon(flat, fill='', outline=C['ap'], width=2,
                              dash=(6, 3))

    def draw_pic_handles(self, pic):
        """Corner squares (scale) and a round handle (rotate)."""
        if pic.get('locked') or self.tool != 'select':
            return
        cv = self.canvas
        corners = [self.project(*c)[:2] for c in self.pic_corners(pic)]
        for x, y in corners:
            cv.create_rectangle(x - 5, y - 5, x + 5, y + 5, fill='white',
                                outline=C['ap'], width=2)
            self.pic_hits.append(('scale', x, y))
        # rotation handle: beyond the middle of the top edge
        cx = sum(p[0] for p in corners) / 4
        cy = sum(p[1] for p in corners) / 4
        tx = (corners[0][0] + corners[1][0]) / 2
        ty = (corners[0][1] + corners[1][1]) / 2
        L = math.hypot(tx - cx, ty - cy) or 1
        hx, hy = tx + (tx - cx) / L * 28, ty + (ty - cy) / L * 28
        cv.create_line(tx, ty, hx, hy, fill=C['ap'], width=2)
        cv.create_oval(hx - 6, hy - 6, hx + 6, hy + 6, fill=C['ap'],
                       outline='white', width=2)
        self.pic_hits.append(('rot', hx, hy))

    def draw_grid(self):
        cv = self.canvas
        lo_x, lo_y, hi_x, hi_y = self.world_box()
        step = _nice_step(max(hi_x - lo_x, hi_y - lo_y))
        gx0 = math.floor(lo_x / step) * step - step
        gy0 = math.floor(lo_y / step) * step - step
        gx1 = math.ceil(hi_x / step) * step + step
        gy1 = math.ceil(hi_y / step) * step + step
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

    def sub_label(self, n):
        p, kind = n['p'], n['kind']
        ip = str(p.get('ip') or '').split('/')[0]
        r = self.rng(n)
        if kind == 'ap':
            return 'ch %s · %s GHz · %s m' % (p.get('channel') or '-',
                                              p.get('band') or '2.4',
                                              fmt_num(r))
        if kind in ('sta', 'car'):
            return ' · '.join(s for s in (ip, '%s m' % fmt_num(r)) if s)
        if kind == 'host':
            return ip
        if kind == 'switch':
            return p.get('failMode') or ''
        return '%s:%s' % (p.get('ip') or '', p.get('port') or '')

    def draw_node(self, n, x, y, selected):
        cv = self.canvas
        nid = n['id']
        hl = selected or self.hover == nid or \
            (self.drag and self.drag.get('id') == nid)
        if selected:
            cv.create_oval(x - 19, y - 19, x + 19, y + 19, fill='',
                           outline=C['select'], dash=(2, 3))
        draw_icon(cv, n['kind'], x, y, ow=C['select'] if hl else 'white')
        if n['kind'] == 'ap' and self.v_clients.get():
            cnt = len(getattr(self, '_assoc', {}).get(nid, []))
            bx, by = x + 15, y - 13
            cv.create_oval(bx - 8, by - 8, bx + 8, by + 8,
                           fill=C['rf'] if cnt else C['axis'], outline='white',
                           width=1.5)
            cv.create_text(bx, by, text=str(cnt), fill='white',
                           font=('TkDefaultFont', 8, 'bold'))
        if self.v_labels.get():
            cv.create_text(x, y + 20, text=n['name'], fill=C['text'],
                           font=('TkDefaultFont', 10, 'bold'))
            sub = self.sub_label(n)
            if sub:
                cv.create_text(x, y + 33, text=sub, fill=C['muted'],
                               font=('TkDefaultFont', 8))
        self.hits.append((nid, x, y))

    def draw_axes(self, n):
        """Local X / Y / Z arrows on the selected device (draggable)."""
        cv = self.canvas
        p = n['xyz']
        L = 62.0 / self.scale                      # constant screen length
        o = self.project(*p)
        for axis, col, lab in ((0, C['x'], 'X'), (1, C['y'], 'Y'),
                               (2, C['z'], 'Z')):
            q = list(p)
            q[axis] += L
            e = self.project(*q)
            dx, dy = e[0] - o[0], e[1] - o[1]
            ln = math.hypot(dx, dy)
            if ln < 14:                            # axis points at us
                continue
            ux, uy = dx / ln, dy / ln
            sx, sy = o[0] + ux * 13, o[1] + uy * 13
            hot = self.hover_axis == axis or (
                self.drag and self.drag.get('mode') == 'axis'
                and self.drag.get('axis') == axis)
            cv.create_line(sx, sy, e[0], e[1], fill=col, width=4 if hot else 2.5,
                           arrow='last', arrowshape=(11, 13, 5))
            cv.create_text(e[0] + ux * 11, e[1] + uy * 11, text=lab, fill=col,
                           font=('TkDefaultFont', 9, 'bold'))
            self.axis_hits.append((axis, sx, sy, e[0], e[1], L))
        if self.drag and self.drag.get('mode') in ('axis', 'node') and \
                self.drag.get('moved'):
            cv.create_text(o[0] + 22, o[1] - 24, anchor='w',
                           text='x %s   y %s   z %s' % tuple(
                               fmt_num(float(v)) for v in p),
                           fill=C['text'], font=('TkDefaultFont', 9, 'bold'))

    def draw_overlay(self):
        cv = self.canvas
        w, h = self.size()
        x, y = 12, 16
        for kind, text in (('ap', 'Access point'), ('sta', 'Station'),
                           ('car', 'Car'), ('host', 'Host'),
                           ('switch', 'Switch'), ('ctrl', 'Controller')):
            draw_icon(cv, kind, x + 7, y + (3 if kind == 'ap' else 0), s=0.55)
            cv.create_text(x + 18, y, text=text, anchor='w', fill=C['text'],
                           font=('TkDefaultFont', 9))
            x += 26 + 7 * len(text)
        for text, dash, col, wd in (('Cable', None, C['wired'], 3),
                                    ('Wi-Fi', None, C['rf'], 2.5),
                                    ('Auto Wi-Fi', (2, 4), C['rf'], 1.5),
                                    ('Ad-hoc / mesh', (6, 4), C['rf'], 2),
                                    ('Control', (6, 4), C['control'], 2),
                                    ('Wall', None, WALL_MATERIALS['brick'][1],
                                     6)):
            cv.create_line(x, y, x + 24, y, fill=col, width=wd, dash=dash)
            cv.create_text(x + 30, y, text=text, anchor='w', fill=C['text'],
                           font=('TkDefaultFont', 9))
            x += 40 + 7 * len(text)
        cv.create_text(12, h - 12, anchor='sw', fill=C['muted'],
                       font=('TkDefaultFont', 8),
                       text='drag: rotate   right-drag: pan   wheel: zoom   '
                            'drag device: move   Shift+drag: height   '
                            'drag arrow: move on axis   double-click: '
                            'inspector   Del: delete   Ctrl+Z: undo')
        if self.hover and not self.drag and self.hover in self.nodes:
            self.draw_tip()

    def draw_tip(self):
        cv = self.canvas
        n = self.nodes[self.hover]
        p = n['p']
        lines = ['%s  (%s)' % (n['name'], KIND_LABEL[n['kind']]),
                 'position: %s, %s, %s' % tuple(fmt_num(float(v))
                                                for v in n['xyz'])]
        if n['kind'] in WIRELESS_KINDS:
            lines.append('range: %s m' % fmt_num(self.rng(n)))
        assoc = getattr(self, '_assoc', None) or self.associations()
        if n['kind'] == 'ap':
            lines.append('ssid: %s' % (p.get('ssid') or '-'))
            lines.append('%s GHz  ·  channel %s  ·  mode %s' % (
                p.get('band') or '2.4', p.get('channel'), p.get('mode')))
            lst = assoc.get(n['id'], [])
            lines.append('connected devices: %d%s' % (len(lst), (
                '  (' + ', '.join(self.nodes[i]['name'] +
                                  ('' if how == 'link' else ' auto')
                                  for i, how in lst) + ')') if lst else ''))
        if n['kind'] in ('sta', 'car'):
            cov = self.coverage(n)
            lines.append('inside: ' + (', '.join(cov) if cov else 'no AP range'))
            conn = self.client_of(n['id'], assoc)
            lines.append('connected to: ' + (', '.join(
                self.nodes[a]['name'] + (' (Wi-Fi link)' if how == 'link'
                                         else ' (auto)')
                for a, how in conn) if conn else 'nothing'))
            for a, how in conn:
                rssi, wl, hit = self.est_rssi(n, self.nodes[a])
                lines.append('est. RSSI from %s: %d dBm%s' % (
                    self.nodes[a]['name'], round(rssi),
                    ('  (%d wall%s, -%s dB)' % (len(hit), '' if len(hit) == 1
                                                else 's', fmt_num(wl)))
                    if hit else ''))
        if p.get('ip'):
            lines.append('ip: %s' % p['ip'])
        mx, my = self.mouse
        tid = cv.create_text(mx + 16, my - 12, text='\n'.join(lines),
                             anchor='sw', fill=C['tip_fg'],
                             font=('TkDefaultFont', 9))
        b = cv.bbox(tid)
        bg = cv.create_rectangle(b[0] - 7, b[1] - 5, b[2] + 7, b[3] + 5,
                                 fill=C['tip_bg'], outline='')
        cv.tag_raise(tid, bg)

    def coverage(self, n):
        """APs whose range contains this node (pure geometry)."""
        out = []
        for a in self.nodes.values():
            if a['kind'] != 'ap':
                continue
            d = math.dist(n['xyz'], a['xyz'])
            if d <= self.rng(a):
                out.append('%s (%s m)' % (a['name'], fmt_num(round(d, 1))))
        return out

    def update_status(self):
        lines, warn = [], False
        if self.msg:
            lines.append(self.msg)
            warn = self.msg.startswith('!')
        hint = TOOL_HINT[self.tool]
        lines.append(hint % self.link_kind.get() if '%s' in hint else hint)
        n = self.sel_node()
        if n:
            s = '%s (%s)  at %s' % (n['name'], KIND_LABEL[n['kind']],
                                    fmt_pos(n['xyz']))
            if n['kind'] in WIRELESS_KINDS:
                s += '   range %s m' % fmt_num(self.rng(n))
            if n['kind'] in ('sta', 'car'):
                cov = self.coverage(n)
                s += ('   inside: ' + ', '.join(cov)) if cov else \
                     '   outside every AP range'
            if n['kind'] == 'ap':
                c = len(self.associations().get(n['id'], []))
                s += '   %d connected device%s' % (c, '' if c == 1 else 's')
            lines.append(s)
        elif self.sel and self.sel[0] == 'walls':
            ws = [self.walls[i] for i in self.sel_wall_ids()]
            groups = sorted(set(w['group'] for w in ws if w.get('group')))
            lines.append('%d walls selected%s: %s   (drag to move them '
                         'together; Ctrl+G group, Ctrl+Shift+G ungroup)' % (
                             len(ws), ' (%s)' % ', '.join(groups)
                             if groups else '',
                             ', '.join(w['name'] for w in ws)))
        elif self.sel_wall():
            w = self.sel_wall()
            lines.append('%s%s   %s, -%s dB   %s x %s m%s   at %s, %s   '
                         'rotation %s deg   crossed by %d connection(s)' % (
                             w['name'], '  (locked)' if w.get('locked')
                             else '', w['material'],
                             fmt_num(round(wall_db(w), 2)),
                             fmt_num(w['length']), fmt_num(w['thickness']),
                             (' x %s m high' % fmt_num(w['height']))
                             if float(w['height']) > 0 else ' (flat 2D)',
                             fmt_num(w['x']), fmt_num(w['y']),
                             fmt_num(w['rotation']),
                             len(self.walls_crossing(w))))
        elif self.sel_pic():
            pc = self.sel_pic()
            lines.append('picture %s   center %s, %s   floor z %s   width %s m'
                         '   rotation %s deg   opacity %s%%%s' % (
                             os.path.basename(pc['path']), fmt_num(pc['x']),
                             fmt_num(pc['y']), fmt_num(pc['z']),
                             fmt_num(pc['width']), fmt_num(pc['rotation']),
                             fmt_num(pc['opacity']),
                             '   (locked)' if pc.get('locked') else ''))
        elif self.sel and self.sel[0] == 'link' and self.sel[1] in self.links:
            l = self.links[self.sel[1]]
            lines.append('%s link  %s - %s' % (
                l['kind'], self.nodes[l['a']]['name'],
                self.nodes[l['b']]['name']))
        counts = {}
        for m in self.nodes.values():
            counts[m['kind']] = counts.get(m['kind'], 0) + 1
        summary = '   '.join('%d %s%s' % (counts[k], KIND_LABEL[k],
                                           '' if counts[k] == 1 else 's')
                             for k in KINDS if counts.get(k))
        lines.append((summary or 'Empty network: pick a device in the '
                      'toolbar and click on the grid.') +
                     ('   %d link%s' % (len(self.links),
                                        '' if len(self.links) == 1 else 's')
                      if self.links else ''))
        if self.walls and self.netp.get('wmediumd') and \
                self.netp.get('interference'):
            lines.append('Walls: Mininet-WiFi ignores them in wmediumd '
                         'interference mode. Untick "Interference mode" in '
                         'Network settings to simulate them.')
        if not counts.get('ctrl') and (counts.get('ap') or
                                       counts.get('switch')):
            lines.append('No controller: APs / switches will be saved as '
                         'failMode=standalone.')
        text = '\n'.join(lines[:5])
        if text != self.status.cget('text'):
            self.status.config(text=text, fg=C['warn'] if warn else C['text'])

    # ------------------------------------------------------- inspector ----
    def open_inspector(self):
        if self.inspector:
            self.inspector.top.deiconify()
            self.inspector.top.lift()
            self.inspector.refresh()
        else:
            self.inspector = Inspector(self)

    # ----------------------------------------------------------- files ----
    def update_title(self):
        name = os.path.basename(self.path) if self.path else 'untitled'
        fmt = ' [%s]' % self.fmt if self.path else ''
        self.root.title('MiniEdit-WiFi  -  %s%s%s' % (
            name, fmt, '  *' if self.dirty else ''))

    def to_doc(self):
        nodes = [{'kind': n['kind'], 'name': n['name'],
                  'xyz': list(n['xyz']), 'p': copy.deepcopy(n['p'])}
                 for _, n in sorted(self.nodes.items())]
        links = [{'kind': l['kind'], 'a': self.nodes[l['a']]['name'],
                  'b': self.nodes[l['b']]['name'], 'p': copy.deepcopy(l['p'])}
                 for _, l in sorted(self.links.items())]
        pics = [dict((k, pc[k]) for k in PIC_KEYS)
                for _, pc in sorted(self.pics.items())]
        walls = [dict((k, w[k]) for k in WALL_KEYS)
                 for _, w in sorted(self.walls.items())]
        return {'net': copy.deepcopy(self.netp), 'nodes': nodes,
                'links': links, 'pictures': pics, 'walls': walls}

    def from_doc(self, doc):
        self.nodes, self.links, self.next_id = {}, {}, 1
        self.netp = default_net()
        self.netp.update(doc.get('net') or {})
        ids = {}
        for n in doc['nodes']:
            nid = self.next_id
            self.next_id += 1
            p = default_params(n['kind'], n['name'], '')
            p.update(n['p'])
            self.nodes[nid] = {'id': nid, 'kind': n['kind'],
                               'name': n['name'], 'xyz': list(n['xyz']),
                               'p': p}
            ids[n['name']] = nid
        for l in doc['links']:
            if l['a'] in ids and l['b'] in ids:
                lid = self.next_id
                self.next_id += 1
                p = default_link_params(l['kind'])
                p.update(l['p'])
                self.links[lid] = {'id': lid, 'kind': l['kind'],
                                   'a': ids[l['a']], 'b': ids[l['b']], 'p': p}
        self.pics, self._pic_tk = {}, {}
        for pc in doc.get('pictures') or []:
            pid = self.next_id
            self.next_id += 1
            pic = default_pic()
            pic.update(pc)
            pic['id'] = pid
            self.pics[pid] = pic
        self.walls = {}
        for w in doc.get('walls') or []:
            wid = self.next_id
            self.next_id += 1
            ww = default_wall()
            ww.update(w)
            ww['id'] = wid
            self.walls[wid] = ww
        self.sel = None
        self.undo_stack, self.redo_stack, self._undo_key = [], [], None

    def confirm_discard(self):
        """True when it's OK to throw away the current network."""
        if not self.dirty:
            return True
        ans = self.mb.askyesnocancel('Unsaved changes',
                                     'Save the current network first?',
                                     parent=self.root)
        if ans is None:
            return False
        return self.save() if ans else True

    def new(self):
        if not self.confirm_discard():
            return
        self.from_doc({'net': default_net(), 'nodes': [], 'links': []})
        self.path, self.fmt, self.dirty = None, 'normalized', False
        self.msg = 'New network.'
        self.user_view = False
        self.changed()
        self.dirty = False
        self.update_title()
        self.fit()

    def open(self):
        if not self.confirm_discard():
            return
        path = self.fd.askopenfilename(
            parent=self.root, title='Open topology',
            filetypes=[('Topology JSON', '*.json'), ('All files', '*')])
        if path:
            self.open_path(path)

    def open_path(self, path):
        try:
            doc, fmt = read_doc(path)
        except Exception as e:
            self.mb.showerror('Open', 'Could not read %s\n\n%s' % (path, e),
                              parent=self.root)
            return
        self.from_doc(doc)
        self.path, self.fmt = path, fmt
        self.msg = 'Opened %s (%s).' % (os.path.basename(path), fmt)
        self.user_view = False
        self.changed()
        self.dirty = False
        self.update_title()
        self.fit()

    def save(self):
        if not self.path:
            return self.save_as()
        return self.write(self.path, self.fmt)

    def save_as(self):
        fmt = self.ask_format()
        if not fmt:
            return False
        base = os.path.splitext(os.path.basename(self.path or 'topology'))[0]
        path = self.fd.asksaveasfilename(
            parent=self.root, title='Save topology (%s)' % fmt,
            defaultextension='.json', initialfile=base + '.json',
            filetypes=[('Topology JSON', '*.json'), ('All files', '*')])
        if not path:
            return False
        if self.write(path, fmt):
            self.path, self.fmt = path, fmt
            self.update_title()
            return True
        return False

    def export_py(self):
        base = os.path.splitext(os.path.basename(self.path or 'topology'))[0]
        path = self.fd.asksaveasfilename(
            parent=self.root, title='Export Mininet-WiFi script',
            defaultextension='.py', initialfile=base + '.py',
            filetypes=[('Python script', '*.py'), ('All files', '*')])
        if path:
            try:
                write_doc(self.to_doc(), path, 'script')
            except Exception as e:
                self.mb.showerror('Export', str(e), parent=self.root)
                return
            self.msg = ('Exported %s  -  run it with: sudo -E python3 %s'
                        % (path, os.path.basename(path)))
            self.redraw()

    def write(self, path, fmt):
        try:
            write_doc(self.to_doc(), path, fmt)
        except Exception as e:
            self.mb.showerror('Save', 'Could not save %s\n\n%s' % (path, e),
                              parent=self.root)
            return False
        self.dirty = False
        self.msg = 'Saved %s (%s).' % (path, fmt)
        self.update_title()
        self.redraw()
        return True

    def ask_format(self):
        tk = self.tk
        top = tk.Toplevel(self.root)
        top.title('Save format')
        top.configure(bg=C['bg'])
        top.transient(self.root)
        top.resizable(False, False)
        var = tk.StringVar(value=self.fmt if self.fmt in ('normalized',
                                                          'custom')
                           else 'normalized')
        res = {'v': None}
        tk.Label(top, text='Which file format?', bg=C['bg'], fg=C['text'],
                 font=('TkDefaultFont', 12, 'bold')).pack(anchor='w',
                                                         padx=16, pady=(14, 4))
        for val, title, desc in (
                ('normalized', 'Normalized',
                 'Parameters are exactly the Mininet-WiFi keyword arguments\n'
                 '(net.addStation(name, **params) ...). Ready to run:\n'
                 'sudo -E python3 wifi_edit.py --run file.json'),
                ('custom', 'Custom',
                 'Grouped by topic (radio, addressing, security, openflow...),\n'
                 'typed values, every field present (null when unset).\n'
                 'For your own interpreter. --run reads it too.')):
            f = tk.Frame(top, bg=C['bg'])
            f.pack(fill='x', padx=16, pady=4)
            tk.Radiobutton(f, text=title, variable=var, value=val, bg=C['bg'],
                           activebackground=C['bg'], highlightthickness=0,
                           font=('TkDefaultFont', 10, 'bold')).pack(anchor='w')
            tk.Label(f, text=desc, bg=C['bg'], fg=C['muted'], justify='left',
                     font=('TkDefaultFont', 9)).pack(anchor='w', padx=(24, 0))
        bf = tk.Frame(top, bg=C['bar'], padx=8, pady=8)
        bf.pack(fill='x', pady=(10, 0))

        def ok(*a):
            res['v'] = var.get()
            top.destroy()
        tk.Button(bf, text='Save', command=ok, relief='flat', bg=C['ap'],
                  fg='white', padx=14).pack(side='right', padx=4)
        tk.Button(bf, text='Cancel', command=top.destroy, relief='flat',
                  bg='white', padx=10).pack(side='right', padx=4)
        top.bind('<Return>', ok)
        top.bind('<Escape>', lambda e: top.destroy())
        try:
            top.wait_visibility()
            top.grab_set()
        except Exception:
            pass
        self.root.wait_window(top)
        return res['v']

    def on_close(self):
        if not self.dirty and self.path:
            self.root.destroy()
            return
        if not self.nodes and not self.path:
            self.root.destroy()
            return
        if self.confirm_discard():
            self.root.destroy()

    def run(self):
        self.root.mainloop()


class WallDialog(object):
    """Modal dialog shown after drawing a wall zone. .result is the wall
    dict, or None when cancelled."""

    def __init__(self, ed, wall):
        tk, ttk = ed.tk, ed.ttk
        self.ed, self.result = ed, None
        w = self.w = dict(wall)
        top = self.top = tk.Toplevel(ed.root)
        top.title('New wall')
        top.configure(bg=C['bg'])
        top.transient(ed.root)
        top.resizable(False, False)
        head = tk.Frame(top, bg=C['bar'], padx=12, pady=8)
        head.pack(fill='x')
        ic = tk.Canvas(head, width=30, height=24, bg=C['bar'],
                       highlightthickness=0)
        ed._icon(ic, 'wall')
        ic.pack(side='left')
        tk.Label(head, text='Wall properties', bg=C['bar'], fg=C['text'],
                 font=('TkDefaultFont', 12, 'bold')).pack(side='left', padx=8)
        tk.Label(head, text='%s x %s m zone' % (fmt_num(w['length']),
                                               fmt_num(w['thickness'])),
                 bg=C['bar'], fg=C['muted']).pack(side='right')
        body = tk.Frame(top, bg=C['bg'], padx=14, pady=8)
        body.pack(fill='both')
        body.columnconfigure(1, weight=1)
        self.v = {}
        r = [0]

        def row(label, widget, hint=''):
            tk.Label(body, text=label, bg=C['bg'], fg=C['text'],
                     anchor='w').grid(row=r[0], column=0, sticky='w',
                                      pady=3, padx=(0, 10))
            widget.grid(row=r[0], column=1, sticky='ew', pady=3)
            r[0] += 1
            if hint:
                tk.Label(body, text=hint, bg=C['bg'], fg=C['muted'],
                         font=('TkDefaultFont', 8), anchor='w').grid(
                    row=r[0], column=1, sticky='w')
                r[0] += 1

        def entry(key, width=10):
            var = tk.StringVar(value=fmt_num(w[key]) if not isinstance(
                w[key], str) else w[key])
            self.v[key] = var
            return tk.Entry(body, textvariable=var, width=width, relief='flat',
                            bg='white', highlightthickness=1,
                            highlightbackground=C['grid_major'],
                            highlightcolor=C['ap'])

        row('Name', entry('name', 18))
        self.v['material'] = tk.StringVar(value=w['material'])
        mat = ttk.Combobox(body, textvariable=self.v['material'],
                           values=MATERIAL_ORDER, state='readonly', width=16)
        mat.bind('<<ComboboxSelected>>', lambda e: self.on_material())
        row('Material', mat, 'concrete 12, brick 8, wood 4, drywall 3, '
            'glass 3, metal 20 dB')
        self.e_loss = entry('loss')
        row('Attenuation (dB)', self.e_loss,
            'subtracted from the RSSI of every path crossing it')
        self.e_mult = entry('thick_mult')
        row('Thickness multiplier', self.e_mult,
            'presets: 0 = thickness ignored, 1 = loss proportional to it')
        self.eff = tk.Label(body, text='', bg=C['bg'], fg=C['ap'],
                            font=('TkDefaultFont', 9, 'bold'), anchor='w')
        self.eff.grid(row=r[0], column=1, sticky='w')
        r[0] += 1
        cf = tk.Frame(body, bg=C['bg'])
        self.swatch = tk.Label(cf, width=4, bg=w['color'], relief='solid',
                               bd=1)
        self.swatch.pack(side='left')
        self.b_color = tk.Button(cf, text='Pick color...', relief='flat',
                                 bg='white', command=self.pick_color)
        self.b_color.pack(side='left', padx=6)
        row('Color', cf, 'editable when the material is custom')
        hf = tk.Frame(body, bg=C['bg'])
        self.v['height'] = tk.StringVar(value=fmt_num(w['height']))
        tk.Scale(hf, from_=0, to=20, resolution=0.1, orient='horizontal',
                 showvalue=0, length=150, bg=C['ap_ring'], bd=1,
                 highlightthickness=0, troughcolor='white',
                 sliderrelief='flat', sliderlength=14, width=11,
                 variable=tk.DoubleVar(value=float(w['height'])),
                 command=lambda v: self.v['height'].set(fmt_num(float(v)))
                 ).pack(side='left')
        tk.Entry(hf, textvariable=self.v['height'], width=6, relief='flat',
                 bg='white', highlightthickness=1,
                 highlightbackground=C['grid_major']).pack(side='left',
                                                           padx=(6, 0))
        row('Height (m)', hf, '0 = flat 2D wall (blocks every crossing path);'
            ' > 0 = 3D wall')
        row('Base height Z (m)', entry('z'), 'e.g. 3 for a wall on floor 2')
        row('Thickness (m)', entry('thickness'))
        for k in ('loss', 'thick_mult', 'thickness'):
            self.v[k].trace_add('write', lambda *a: self.preview())
        self.v_ask = tk.BooleanVar(value=ed.wall_ask)
        tk.Checkbutton(body, text='Ask for properties every time I draw a '
                       'wall', variable=self.v_ask, bg=C['bg'],
                       activebackground=C['bg'], highlightthickness=0).grid(
            row=r[0], column=0, columnspan=2, sticky='w', pady=(8, 0))
        self.err = tk.Label(body, text='', bg=C['bg'], fg=C['warn'])
        self.err.grid(row=r[0] + 1, column=0, columnspan=2, sticky='w')
        bf = tk.Frame(top, bg=C['bar'], padx=8, pady=8)
        bf.pack(fill='x')
        tk.Button(bf, text='Create wall', relief='flat', bg=C['ap'],
                  fg='white', padx=12, command=self.ok).pack(side='right',
                                                             padx=4)
        tk.Button(bf, text='Cancel', relief='flat', bg='white', padx=10,
                  command=top.destroy).pack(side='right', padx=4)
        top.bind('<Return>', lambda e: self.ok())
        top.bind('<Escape>', lambda e: top.destroy())
        self.on_material(init=True)
        try:
            top.wait_visibility()
            top.grab_set()
        except Exception:
            pass
        ed.root.wait_window(top)

    def on_material(self, init=False):
        m = self.v['material'].get()
        custom = m == 'custom'
        set_material(self.w, m)
        if not custom:
            self.v['loss'].set(fmt_num(self.w['loss']))
            self.swatch.config(bg=self.w['color'])
        self.e_loss.config(state='normal' if custom else 'disabled')
        self.e_mult.config(state='disabled' if custom else 'normal')
        self.b_color.config(state='normal' if custom else 'disabled')
        self.preview()

    def preview(self):
        try:
            t = dict(self.w, loss=float(self.v['loss'].get()),
                     thick_mult=float(self.v['thick_mult'].get() or 0),
                     thickness=float(self.v['thickness'].get()),
                     material=self.v['material'].get())
            self.eff.config(text='effective loss: %s dB' % fmt_num(
                round(wall_db(t), 2)))
        except (ValueError, AttributeError, KeyError):
            pass

    def pick_color(self):
        from tkinter import colorchooser
        c = colorchooser.askcolor(color=self.w['color'], parent=self.top)
        if c and c[1]:
            self.w['color'] = c[1]
            self.swatch.config(bg=c[1])

    def ok(self):
        w = self.w
        try:
            name = self.v['name'].get().strip()
            if not name:
                raise ValueError('name is empty')
            if any(o['name'] == name for o in self.ed.walls.values()):
                raise ValueError('name %s is already used' % name)
            w['name'] = name
            w['material'] = self.v['material'].get()
            for k, lo in (('loss', 0.0), ('height', 0.0),
                          ('thickness', 0.001), ('thick_mult', -100.0)):
                v = float(self.v[k].get())
                if v < lo:
                    raise ValueError('%s must be >= %g' % (k, lo))
                w[k] = round(v, 2)
            w['z'] = round(float(self.v['z'].get()), 2)
            problem = self.ed.check_wall(w)
            if problem:
                raise ValueError(problem)
        except ValueError as e:
            self.err.config(text='!  %s' % e)
            return
        self.ed.wall_ask = self.v_ask.get()
        self.result = w
        self.top.destroy()


class Inspector(object):
    """Pop-up window that edits everything about the selection
    (or the network settings when nothing is selected). Live: every valid
    edit is applied at once; invalid entries turn red."""

    def __init__(self, ed):
        tk = self.tk = ed.tk
        self.ed = ed
        top = self.top = tk.Toplevel(ed.root)
        top.title('Inspector')
        top.configure(bg=C['bg'])
        ed.root.update_idletasks()
        x = ed.root.winfo_rootx() + ed.root.winfo_width() + 8
        sw = ed.root.winfo_screenwidth()
        if x + 400 > sw:
            x = max(sw - 410, 0)
        top.geometry('400x720+%d+%d' % (x, max(ed.root.winfo_rooty() - 30, 0)))
        top.protocol('WM_DELETE_WINDOW', self.close)
        top.bind('<Escape>', lambda e: self.close())

        head = tk.Frame(top, bg=C['bar'], padx=10, pady=8)
        head.pack(fill='x')
        self.icon = tk.Canvas(head, width=42, height=38, bg=C['bar'],
                              highlightthickness=0)
        self.icon.pack(side='left')
        tf = tk.Frame(head, bg=C['bar'])
        tf.pack(side='left', padx=8)
        self.title = tk.Label(tf, bg=C['bar'], fg=C['text'], anchor='w',
                              font=('TkDefaultFont', 13, 'bold'))
        self.title.pack(anchor='w')
        self.sub = tk.Label(tf, bg=C['bar'], fg=C['muted'], anchor='w')
        self.sub.pack(anchor='w')

        foot = tk.Frame(top, bg=C['bar'], padx=6, pady=6)
        foot.pack(side='bottom', fill='x')
        self.b_net = tk.Button(foot, text='Network settings', relief='flat',
                               bg='white', padx=8,
                               command=lambda: ed.select(None))
        self.b_net.pack(side='left', padx=3)
        self.b_del = tk.Button(foot, text='Delete', relief='flat', bg='white',
                               fg=C['warn'], padx=8,
                               command=ed.delete_selection)
        self.b_del.pack(side='left', padx=3)
        tk.Button(foot, text='Close', relief='flat', bg=C['ap'], fg='white',
                  padx=12, command=self.close).pack(side='right', padx=3)

        wrap = tk.Frame(top, bg=C['bg'])
        wrap.pack(fill='both', expand=True)
        self.cv = tk.Canvas(wrap, bg=C['bg'], highlightthickness=0)
        sb = tk.Scrollbar(wrap, orient='vertical', command=self.cv.yview)
        self.cv.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self.cv.pack(side='left', fill='both', expand=True)
        self.body = tk.Frame(self.cv, bg=C['bg'], padx=14, pady=4)
        self.win = self.cv.create_window(0, 0, window=self.body, anchor='nw')
        self.body.bind('<Configure>', lambda e: self.cv.configure(
            scrollregion=self.cv.bbox('all')))
        self.cv.bind('<Configure>', lambda e: self.cv.itemconfigure(
            self.win, width=e.width))
        top.bind('<MouseWheel>', lambda e: self.scroll(-1 if e.delta > 0
                                                       else 1))
        top.bind('<Button-4>', lambda e: self.scroll(-1))
        top.bind('<Button-5>', lambda e: self.scroll(1))

        self.target = None
        self.fields = {}
        self.loading = False
        self.build()

    def scroll(self, d):
        if self.body.winfo_height() > self.cv.winfo_height():
            self.cv.yview_scroll(d * 2, 'units')

    def close(self):
        self.top.destroy()
        self.ed.inspector = None

    # ------------------------------------------------------- structure ----
    def current(self):
        ed = self.ed
        if ed.sel and ed.sel[0] == 'node' and ed.sel[1] in ed.nodes:
            return ed.sel
        if ed.sel and ed.sel[0] == 'link' and ed.sel[1] in ed.links:
            return ed.sel
        if ed.sel and ed.sel[0] == 'pic' and ed.sel[1] in ed.pics:
            return ed.sel
        if ed.sel and ed.sel[0] == 'wall' and ed.sel[1] in ed.walls:
            return ed.sel
        if ed.sel and ed.sel[0] == 'walls' and len(ed.sel_wall_ids()) > 1:
            return ed.sel
        return ('net', None)

    def refresh(self):
        if not self.top.winfo_exists():
            return
        if self.current() != self.target:
            self.build()
        else:
            self.load_values()

    def obj(self):
        kind, oid = self.target
        if kind == 'node':
            return self.ed.nodes.get(oid)
        if kind == 'link':
            return self.ed.links.get(oid)
        if kind == 'pic':
            return self.ed.pics.get(oid)
        if kind == 'wall':
            return self.ed.walls.get(oid)
        return None

    def dyn_row(self, fn):
        """A read-only text block refreshed with every change."""
        lb = self.tk.Label(self.body, bg=C['bg'], fg=C['text'], anchor='w',
                           justify='left', font=('TkDefaultFont', 9))
        lb.grid(row=self.row, column=0, columnspan=2, sticky='w', pady=2)
        self.row += 1
        self.dyn.append((lb, fn))

    def build(self):
        tk = self.tk
        for w in self.body.winfo_children():
            w.destroy()
        self.fields, self.row, self.dyn = {}, 0, []
        self.body.columnconfigure(1, weight=1)
        self.target = self.current()
        kind, oid = self.target
        if kind == 'walls':
            self.section('general')
            self.dyn_row(lambda: self.walls_info())
            bf = tk.Frame(self.body, bg=C['bg'])
            bf.grid(row=self.row, column=0, columnspan=2, sticky='w', pady=6)
            self.row += 1
            for text, cmd in (('Group', self.ed.group_walls),
                              ('Ungroup', self.ed.ungroup_walls),
                              ('Lock / unlock all', self.ed.toggle_lock)):
                tk.Button(bf, text=text, relief='flat', bg='white', padx=8,
                          command=cmd).pack(side='left', padx=(0, 6))
            self.note('Grouping only affects selection and moving: each wall '
                      'keeps its own material, loss and geometry. '
                      'Double-click one wall to edit it on its own.')
            self.b_del.config(state='normal')
            self.b_net.pack(side='left', padx=3, before=self.b_del)
        elif kind == 'wall':
            self.section('general')
            self.add_field(WALL_SCHEMA[0])
            fm = field_map(WALL_SCHEMA)
            self.add_field(fm['locked'])
            self.add_groups(WALL_SCHEMA[1:], skip=('general',))
            self.section('clients')
            self.dyn_row(lambda: self.wall_info())
            self.note('On the canvas: drag the wall to move it. Handles: '
                      'end squares = length, side dot = thickness, round = '
                      'rotate (Shift = 15 degrees), blue diamond = height. '
                      'Ctrl+D duplicates the wall.')
            self.b_del.config(state='normal')
            self.b_net.pack(side='left', padx=3, before=self.b_del)
        elif kind == 'pic':
            self.add_groups(PIC_SCHEMA)
            self.dyn_row(lambda: self.pic_info())
            self.note('On the canvas: drag the picture to move it, drag a '
                      'corner square to scale, the round handle to rotate '
                      '(Shift = 15 degree steps). Raise "Floor height Z" to '
                      'put a floor plan under devices on an upper floor.')
            self.b_del.config(state='normal')
            self.b_net.pack(side='left', padx=3, before=self.b_del)
        elif kind == 'node':
            n = self.ed.nodes[oid]
            schema = SCHEMA[n['kind']]
            self.section('general')
            self.add_field(F('__name', 'Name', 'name', 'general'))
            for f in schema:
                if f['group'] == 'general':
                    self.add_field(f)
            self.section('position')
            for i, a in enumerate('xyz'):
                self.add_field(F('__' + a, '%s (m)' % a.upper(), 'pos',
                                 'position'))
            self.note('Or drag the device: arrows = one axis, '
                      'Shift+drag = height.')
            if n['kind'] == 'ap':
                self.section('clients')
                self.dyn_row(lambda: self.ed.clients_text(oid))
                self.note('Wi-Fi links + stations auto-associating by range. '
                          'Toggle the badges with SHOW > AP clients.')
            elif n['kind'] in ('sta', 'car'):
                self.section('clients')
                self.dyn_row(lambda: self.station_info(oid))
            self.add_groups(schema, skip=('general',))
            self.b_del.config(state='normal')
            self.b_net.pack(side='left', padx=3, before=self.b_del)
        elif kind == 'link':
            l = self.ed.links[oid]
            self.section('general')
            self.info_row('Type', l['kind'])
            self.info_row('Between', '%s  and  %s' % (
                self.ed.nodes[l['a']]['name'], self.ed.nodes[l['b']]['name']))
            self.add_groups(LINK_SCHEMA[l['kind']])
            self.b_del.config(state='normal')
            self.b_net.pack(side='left', padx=3, before=self.b_del)
        else:
            self.add_groups(NET_SCHEMA)
            self.note('Select a device or a link on the canvas to edit it.')
            self.b_del.config(state='disabled')
            self.b_net.pack_forget()
        self.update_header()
        self.load_values()
        self.cv.yview_moveto(0)

    def station_info(self, oid):
        conn = self.ed.client_of(oid)
        if not conn:
            return ('Not connected. Use a Wi-Fi link, or move it inside an '
                    'AP range (auto association).')
        return 'Connected to ' + ', '.join(
            self.ed.nodes[a]['name'] + (' (Wi-Fi link)' if how == 'link'
                                        else ' (auto, in range)')
            for a, how in conn)

    def wall_info(self):
        w = self.obj()
        if w is None:
            return ''
        cr = self.ed.walls_crossing(w)
        kind = ('3D wall, %s m high' % fmt_num(w['height'])
                if float(w['height']) > 0 else 'flat 2D wall (any height)')
        if w.get('material') == 'custom':
            eff = 'Effective loss: %s dB (custom, thickness ignored)' % \
                fmt_num(round(wall_db(w), 2))
        else:
            eff = ('Effective loss: %s dB  = %s dB x (1 + %s x (%s / %s m '
                   '- 1))' % (fmt_num(round(wall_db(w), 2)),
                              fmt_num(w['loss']),
                              fmt_num(w.get('thick_mult') or 0),
                              fmt_num(w['thickness']),
                              fmt_num(w.get('ref_thickness') or 0.2)))
        grp = ''
        if w.get('group'):
            n = len(self.ed.group_members(w['id']))
            grp = '\nGroup: %s (%d walls)' % (w['group'], n)
        return '%s\n%s%s\nCrossed by %d connection%s%s' % (
            eff, kind, grp, len(cr), '' if len(cr) == 1 else 's',
            (':\n  ' + '\n  '.join(cr)) if cr else '')

    def walls_info(self):
        ws = [self.ed.walls[i] for i in self.ed.sel_wall_ids()]
        groups = sorted(set(w['group'] for w in ws if w.get('group')))
        return '%d walls selected%s\n%s' % (
            len(ws), ('  -  group: ' + ', '.join(groups)) if groups else
            '  -  not grouped',
            '\n'.join('  %s  (%s, %s dB%s)' % (
                w['name'], w['material'], fmt_num(round(wall_db(w), 2)),
                ', locked' if w.get('locked') else '') for w in ws))

    def pic_info(self):
        pic = self.obj()
        if pic is None:
            return ''
        src = self.ed.pic_source(pic)
        if src is None:
            return 'Image could not be loaded.'
        h = pic['width'] * src.size[1] / float(src.size[0])
        return 'Covers %s x %s m   (image %d x %d px, %s m / px)' % (
            fmt_num(pic['width']), fmt_num(round(h, 2)), src.size[0],
            src.size[1], fmt_num(round(pic['width'] / src.size[0], 4)))

    def add_groups(self, schema, skip=()):
        groups = []
        for f in schema:
            if f['group'] not in groups and f['group'] not in skip:
                groups.append(f['group'])
        for g in groups:
            self.section(g)
            for f in schema:
                if f['group'] == g:
                    self.add_field(f)

    def update_header(self):
        cv = self.icon
        cv.delete('all')
        kind, oid = self.target
        if kind == 'node':
            n = self.ed.nodes[oid]
            draw_icon(cv, n['kind'], *icon_center(n['kind'], 42, 38))
            self.title.config(text=n['name'])
            self.sub.config(text=KIND_LABEL[n['kind']])
        elif kind == 'link':
            l = self.ed.links[oid]
            wired = l['kind'] == 'wired'
            cv.create_line(8, 30, 34, 8, fill=C['wired'] if wired else C['rf'],
                           width=3, dash=None if l['kind'] in ('wired', 'wifi')
                           else (5, 3))
            for x, y in ((8, 30), (34, 8)):
                cv.create_oval(x - 5, y - 5, x + 5, y + 5, fill=C['ap'],
                               outline='white')
            self.title.config(text='%s - %s' % (self.ed.nodes[l['a']]['name'],
                                                self.ed.nodes[l['b']]['name']))
            self.sub.config(text=LINK_LABEL[l['kind']] + ' link')
        elif kind == 'wall':
            self.ed._icon(cv, 'wall')
            cv.move('all', 6, 7)
            w = self.ed.walls[oid]
            self.title.config(text=w['name'] + ('  (locked)' if w.get('locked')
                                                else ''))
            self.sub.config(text='wall  ·  %s  ·  %s dB%s' % (
                w['material'], fmt_num(round(wall_db(w), 2)),
                ('  ·  ' + w['group']) if w.get('group') else ''))
        elif kind == 'walls':
            self.ed._icon(cv, 'wall')
            cv.move('all', 6, 7)
            ids = self.ed.sel_wall_ids()
            groups = sorted(set(self.ed.walls[i]['group'] for i in ids
                                if self.ed.walls[i].get('group')))
            self.title.config(text='%d walls' % len(ids))
            self.sub.config(text=', '.join(groups) if groups
                            else 'multi-selection (not grouped)')
        elif kind == 'pic':
            self.ed._icon(cv, 'picture')
            cv.move('all', 6, 7)
            pic = self.ed.pics[oid]
            self.title.config(text=os.path.basename(pic['path']) or 'picture')
            self.sub.config(text='picture' + ('  ·  locked' if pic.get('locked')
                                              else ''))
        else:
            cv.create_oval(6, 4, 36, 34, outline=C['ap'], width=2)
            cv.create_oval(14, 4, 28, 34, outline=C['ap'])
            cv.create_line(6, 19, 36, 19, fill=C['ap'])
            self.title.config(text='Network')
            n = len(self.ed.nodes)
            self.sub.config(text='global settings  ·  %d device%s, %d link%s'
                            % (n, '' if n == 1 else 's', len(self.ed.links),
                               '' if len(self.ed.links) == 1 else 's'))

    def section(self, group):
        tk = self.tk
        tk.Label(self.body, text=GROUP_TITLE.get(group, group).upper(),
                 bg=C['bg'], fg=C['muted'], font=('TkDefaultFont', 9, 'bold')
                 ).grid(row=self.row, column=0, columnspan=2, sticky='w',
                        pady=(12, 1))
        self.row += 1
        tk.Frame(self.body, height=1, bg=C['grid_major']).grid(
            row=self.row, column=0, columnspan=2, sticky='ew', pady=(0, 4))
        self.row += 1

    def note(self, text):
        self.tk.Label(self.body, text=text, bg=C['bg'], fg=C['muted'],
                      font=('TkDefaultFont', 8), justify='left',
                      wraplength=340).grid(row=self.row, column=0,
                                           columnspan=2, sticky='w')
        self.row += 1

    def info_row(self, label, value):
        tk = self.tk
        tk.Label(self.body, text=label, bg=C['bg'], fg=C['text'],
                 anchor='w').grid(row=self.row, column=0, sticky='w', pady=2)
        tk.Label(self.body, text=value, bg=C['bg'], fg=C['text'], anchor='w',
                 font=('TkDefaultFont', 10, 'bold')).grid(
            row=self.row, column=1, sticky='w', pady=2)
        self.row += 1

    def _entry(self, parent, var, width=None):
        e = self.tk.Entry(parent, textvariable=var, relief='flat', bg='white',
                          fg=C['text'], highlightthickness=1,
                          highlightbackground=C['grid_major'],
                          highlightcolor=C['ap'], insertbackground=C['text'])
        if width:
            e.config(width=width)
        return e

    def add_field(self, f):
        tk, ttk = self.tk, self.ed.ttk
        key, t = f['key'], f['type']
        tk.Label(self.body, text=f['label'], bg=C['bg'], fg=C['text'],
                 anchor='w').grid(row=self.row, column=0, sticky='nw',
                                  pady=3, padx=(0, 10))
        rec = {'f': f, 'scale': None, 'entry': None}
        if t == 'bool':
            var = tk.BooleanVar()
            w = tk.Checkbutton(self.body, variable=var, bg=C['bg'],
                               activebackground=C['bg'], highlightthickness=0,
                               command=lambda k=key: self.on_edit(k))
            w.grid(row=self.row, column=1, sticky='w')
        elif t in ('choice', 'mode', 'channel'):
            var = tk.StringVar()
            w = ttk.Combobox(self.body, textvariable=var, state='readonly',
                             values=f['opts'] or [])
            w.grid(row=self.row, column=1, sticky='ew', pady=2)
            var.trace_add('write', lambda *a, k=key: self.on_edit(k))
        elif t == 'file':
            var = tk.StringVar()
            w = tk.Frame(self.body, bg=C['bg'])
            w.grid(row=self.row, column=1, sticky='ew', pady=2)
            e = self._entry(w, var)
            e.pack(side='left', fill='x', expand=True)
            tk.Button(w, text='Browse...', relief='flat', bg='white', padx=6,
                      command=lambda k=key: self.browse(k)).pack(
                side='left', padx=(4, 0))
            rec['entry'] = e
            var.trace_add('write', lambda *a, k=key: self.on_edit(k))
        elif t == 'color':
            var = tk.StringVar()
            w = tk.Frame(self.body, bg=C['bg'])
            w.grid(row=self.row, column=1, sticky='ew', pady=2)
            sw = tk.Label(w, width=3, relief='solid', bd=1, bg='#999999')
            sw.pack(side='left')
            e = self._entry(w, var, width=9)
            e.pack(side='left', padx=4)
            b = tk.Button(w, text='Pick...', relief='flat', bg='white',
                          padx=6, command=lambda k=key: self.pick_color(k))
            b.pack(side='left')
            rec['entry'], rec['swatch'], rec['button'] = e, sw, b
            var.trace_add('write', lambda *a, k=key: self.on_edit(k))
        elif t in ('range', 'slider'):
            lo, hi, res = f['opts'] if t == 'slider' else (0, 300, 0.5)
            var = tk.StringVar()
            w = tk.Frame(self.body, bg=C['bg'])
            w.grid(row=self.row, column=1, sticky='ew')
            sc = tk.Scale(w, from_=lo, to=hi, orient='horizontal',
                          resolution=res, showvalue=0, bg=C['ap_ring'], bd=1,
                          highlightthickness=0, troughcolor='white',
                          sliderrelief='flat', activebackground=C['ap'],
                          sliderlength=14, width=12,
                          command=lambda v, k=key: self.on_scale(k, v))
            sc.pack(side='left', fill='x', expand=True)
            e = self._entry(w, var, width=7)
            e.pack(side='left', padx=(6, 0))
            rec['scale'], rec['entry'] = sc, e
            var.trace_add('write', lambda *a, k=key: self.on_edit(k))
        elif t == 'text':
            var = None
            w = tk.Text(self.body, height=4, width=26, relief='flat',
                        bg='white', fg=C['text'], highlightthickness=1,
                        highlightbackground=C['grid_major'],
                        highlightcolor=C['ap'], font=('TkFixedFont', 9),
                        insertbackground=C['text'])
            w.grid(row=self.row, column=1, sticky='ew', pady=2)
            w.bind('<KeyRelease>', lambda e, k=key: self.on_edit(k))
            rec['entry'] = w
        else:                                   # str int float name pos
            var = tk.StringVar()
            w = self._entry(self.body, var)
            w.grid(row=self.row, column=1, sticky='ew', pady=2)
            rec['entry'] = w
            var.trace_add('write', lambda *a, k=key: self.on_edit(k))
        rec['var'], rec['w'] = var, w
        self.fields[key] = rec
        self.row += 1
        if f.get('hint'):
            tk.Label(self.body, text=f['hint'], bg=C['bg'], fg=C['muted'],
                     font=('TkDefaultFont', 8), anchor='w').grid(
                row=self.row, column=1, sticky='w')
            self.row += 1

    def pick_color(self, key):
        from tkinter import colorchooser
        c = colorchooser.askcolor(color=self.get_model(key) or '#999999',
                                  parent=self.top)
        if c and c[1]:
            self.fields[key]['var'].set(c[1])

    def browse(self, key):
        path = self.ed.fd.askopenfilename(
            parent=self.top, title='Choose image',
            filetypes=[('Images', '*.png *.jpg *.jpeg *.gif *.bmp *.tif '
                        '*.tiff *.webp'), ('All files', '*')])
        if path:
            self.fields[key]['var'].set(path)

    @staticmethod
    def scale_to(f, v):
        v = float(v or 0)
        if f['type'] == 'range':
            return max(300, math.ceil(v * 1.5 / 50) * 50)
        hi = f['opts'][1]
        if f['key'] in ('width', 'length'):
            return max(hi, math.ceil(v * 1.5 / 50) * 50)
        return hi

    # ----------------------------------------------------------- values ----
    def get_model(self, key):
        kind, oid = self.target
        o = self.obj()
        if kind in ('pic', 'wall'):
            return o.get(key)
        if kind == 'node':
            if key == '__name':
                return o['name']
            if key in ('__x', '__y', '__z'):
                return o['xyz']['xyz'.index(key[2])]
            return o['p'].get(key)
        if kind == 'link':
            return o['p'].get(key)
        return self.ed.netp.get(key)

    def set_model(self, key, val):
        kind, oid = self.target
        o = self.obj()
        if kind == 'pic':
            if key == 'path':
                self.ed._pic_tk.pop(oid, None)
            o[key] = val
            return
        if kind == 'wall':
            if key == 'material':
                set_material(o, val)
            else:
                o[key] = val
            return
        if kind == 'node':
            if key == '__name':
                if o['p'].get('ssid') == o['name'] + '-ssid':
                    o['p']['ssid'] = val + '-ssid'      # keep default ssid
                o['name'] = val
            elif key in ('__x', '__y', '__z'):
                o['xyz']['xyz'.index(key[2])] = val
            else:
                o['p'][key] = val
        elif kind == 'link':
            o['p'][key] = val
        else:
            self.ed.netp[key] = val

    def band_lists(self):
        """Mode / channel choices that fit the AP's band."""
        if 'band' not in self.fields:
            return
        band = self.get_model('band') or '2.4'
        if 'mode' in self.fields:
            self.fields['mode']['w'].config(values=MODES.get(band, ALL_MODES))
        if 'channel' in self.fields:
            self.fields['channel']['w'].config(values=CHANNELS.get(band, []))

    def load_values(self):
        if self.obj() is None and self.target[0] not in ('net', 'walls'):
            return self.build()
        self.loading = True
        try:
            self.band_lists()
            for key, rec in self.fields.items():
                v = self.get_model(key)
                t = rec['f']['type']
                if t == 'bool':
                    rec['var'].set(bool(v))
                elif t == 'text':
                    w = rec['w']
                    txt = '\n'.join('%s = %r' % kv for kv in (v or {}).items())
                    if w.get('1.0', 'end-1c') != txt:
                        w.delete('1.0', 'end')
                        w.insert('1.0', txt)
                    w.config(bg='white')
                else:
                    s = fmt_num(v) if isinstance(v, (int, float)) and \
                        not isinstance(v, bool) else ('' if v is None
                                                      else str(v))
                    if rec['var'].get() != s:
                        rec['var'].set(s)
                    if rec['entry'] is not None:
                        rec['entry'].config(bg='white')
                    if rec['scale'] is not None:
                        rec['scale'].config(to=self.scale_to(rec['f'], v))
                        rec['scale'].set(float(v or 0))
            if self.target[0] == 'wall':
                custom = self.get_model('material') == 'custom'
                locked = bool(self.get_model('locked'))
                for key, rec in self.fields.items():
                    if key == 'locked':
                        continue
                    on = not locked and not (
                        (key in ('loss', 'color') and not custom) or
                        (key == 'thick_mult' and custom))
                    st = 'normal' if on else 'disabled'
                    for part in ('entry', 'scale', 'button'):
                        if rec.get(part) is not None:
                            rec[part].config(state=st)
                    if rec['f']['type'] in ('choice', 'mode', 'channel'):
                        rec['w'].config(state='readonly' if on
                                        else 'disabled')
                rec = self.fields.get('color')
                if rec:
                    try:
                        rec['swatch'].config(bg=self.get_model('color'))
                    except Exception:
                        pass
            for lb, fn in self.dyn:
                txt = fn()
                if lb.cget('text') != txt:
                    lb.config(text=txt)
            self.update_header()
        finally:
            self.loading = False

    def parse(self, f, raw):
        """-> (ok, value)"""
        t = f['type']
        if t == 'bool':
            return True, bool(raw)
        if t == 'text':
            out = {}
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' not in line:
                    return False, None
                k, v = line.split('=', 1)
                k, v = k.strip(), v.strip()
                if not k:
                    return False, None
                try:
                    out[k] = ast.literal_eval(v)
                except (ValueError, SyntaxError):
                    out[k] = v
            return True, out
        raw = raw.strip()
        if t == 'name':
            if not NAME_RE.match(raw) or raw in RESERVED or \
                    keyword.iskeyword(raw):
                return False, None
            other = self.ed.node_by_name(raw)
            if other is not None and other['id'] != self.target[1]:
                return False, None
            return True, raw
        if t == 'pos':
            try:
                return True, round(float(raw), 2)
            except ValueError:
                return False, None
        if t == 'slider':
            try:
                v = float(raw)
            except ValueError:
                return False, None
            if f['key'] == 'rotation':
                v = (v + 180) % 360 - 180
            elif f['key'] == 'opacity':
                v = max(0.0, min(100.0, v))
            elif f['key'] == 'height':
                if v < 0:
                    return False, None
            elif v <= 0:
                return False, None
            return True, round(v, 2)
        if t == 'wallname':
            if not raw or any(w['name'] == raw and w['id'] != self.target[1]
                              for w in self.ed.walls.values()):
                return False, None
            return True, raw
        if t == 'color':
            try:
                self.top.winfo_rgb(raw)
            except Exception:
                return False, None
            return True, raw
        if t == 'file':
            if not os.path.isfile(raw):
                return False, None
            if self.ed.pic_source({'path': raw}) is None:
                self.ed._pic_src.pop((raw, 1), None)
                return False, None
            return True, raw
        if t in ('float', 'range', 'int'):
            if raw == '':
                return (False, None) if t == 'range' else (True, None)
            try:
                v = int(raw) if t == 'int' else float(raw)
            except ValueError:
                return False, None
            if t == 'range' and v < 0:
                return False, None
            return True, v
        if f['key'] == 'ip' and raw and self.target[0] == 'node':
            try:
                ipaddress.ip_interface(raw)
            except ValueError:
                return False, None
        if f['key'] == 'ipBase' and raw:
            try:
                ipaddress.ip_network(raw, strict=False)
            except ValueError:
                return False, None
        return True, raw

    def on_scale(self, key, v):
        if self.loading:
            return
        self.fields[key]['var'].set(fmt_num(float(v)))   # -> on_edit

    def on_edit(self, key):
        if self.loading or self.obj() is None and \
                self.target[0] not in ('net', 'walls'):
            return
        rec = self.fields[key]
        f = rec['f']
        if f['type'] == 'text':
            raw = rec['w'].get('1.0', 'end-1c')
        else:
            raw = rec['var'].get()
        ok, val = self.parse(f, raw)
        if rec['entry'] is not None:
            rec['entry'].config(bg='white' if ok else C['bad_entry'])
        if not ok or val == self.get_model(key):
            return
        if self.target[0] == 'wall' and key in ('x', 'y', 'z', 'length',
                                                 'thickness', 'rotation',
                                                 'height'):
            test = dict(self.obj())
            test[key] = val
            o = self.ed.overlapping_wall(test)
            if o is not None:
                if rec['entry'] is not None:
                    rec['entry'].config(bg=C['bad_entry'])
                self.ed.msg = '!  That would overlap %s.' % o['name']
                self.ed.redraw()
                return
        self.ed.push_undo((self.target, key))
        self.set_model(key, val)
        if rec['scale'] is not None:
            self.loading = True
            try:
                rec['scale'].config(to=self.scale_to(f, val))
                rec['scale'].set(val)
            finally:
                self.loading = False
        if key == 'band':
            self.band_lists()
            band = val or '2.4'
            for k, opts in (('mode', MODES.get(band)),
                            ('channel', CHANNELS.get(band))):
                if k in self.fields and opts and \
                        self.get_model(k) not in opts:
                    self.fields[k]['var'].set(opts[0])     # -> on_edit
        self.ed.changed('inspector')
        if key in ('__name', 'material', 'locked'):
            self.load_values()   # ssid followed / presets / lock state
        self.update_header()


# ==========================================================================
# Command line
# ==========================================================================

def main(argv):
    if '--run' in argv:
        files = [a for a in argv if not a.startswith('--')]
        if not files:
            sys.exit('usage: sudo -E python3 wifi_edit.py --run topo.json '
                     '[--gui]')
        run_topology(files[0], gui='--gui' in argv)
        return
    if '--export' in argv:
        files = [a for a in argv if not a.startswith('--')]
        if len(files) != 2:
            sys.exit('usage: python3 wifi_edit.py --export topo.json out.py')
        doc, _ = read_doc(files[0])
        write_doc(doc, files[1], 'script')
        print('wrote %s' % files[1])
        return
    files = [a for a in argv if not a.startswith('--')]
    try:
        ed = Editor(files[0] if files else None)
    except Exception as e:
        sys.exit('*** wifi_edit: could not open the window (%s)\n'
                 '*** Install Tkinter: sudo apt install python3-tk' % e)
    ed.run()


if __name__ == '__main__':
    main(sys.argv[1:])