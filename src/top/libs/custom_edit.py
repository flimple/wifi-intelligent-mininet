#!/usr/bin/env python3
"""
wifi_edit.py  -  MiniEdit-style topology editor for Mininet-WiFi

Same look as wifi_gui.py (pure Tkinter, no matplotlib), but it is an
EDITOR: no simulation, no animation. You draw the network, set every
parameter, and save a file that Mininet-WiFi can run.

    sudo apt install python3-tk        (if Tkinter is missing)

What you get
  - 3D view you can rotate, pan and zoom, plus a flat top view
  - Toolbar to place access points, stations, cars, hosts, switches and
    controllers, and to draw links (wired cable, ad-hoc, mesh)
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
  Del delete   Esc select tool   I inspector   L link tool
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
LINK_KINDS = ('wired', 'adhoc', 'mesh')

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
}

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
    F('interference', 'Interference mode', 'bool', 'medium', True),
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
    for l in links:
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
        if t == 'wired':
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
    return {'net': net, 'nodes': nodes, 'links': links}


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
    return {'net': net, 'nodes': nodes, 'links': links}


def read_doc(path):
    with open(path) as fh:
        d = json.load(fh)
    if d.get('format') not in (None, FORMAT):
        raise ValueError('not a %s file' % FORMAT)
    schema = d.get('schema', 'normalized')
    if schema == 'custom':
        return custom_to_doc(d), 'custom'
    return normalized_to_doc(d), 'normalized'


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
    w('')
    w('')
    w('def topology(gui=False):')
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
        if l.get('type') == 'wired':
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
        if l.get('type') == 'wired':
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
}
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
        self.netp = default_net()
        self.next_id = 1
        # state
        self.sel = None                          # ('node'|'link', id)
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
        root.minsize(900, 600)
        root.configure(bg=C['bar'])
        self.v_ranges = tk.BooleanVar(value=True)
        self.v_sphere = tk.BooleanVar(value=True)
        self.v_labels = tk.BooleanVar(value=True)
        self.v_snap = tk.BooleanVar(value=False)
        self.link_kind = tk.StringVar(value='wired')

        self.build_toolbar()
        self.canvas = tk.Canvas(root, width=1100, height=640, bg=C['bg'],
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
                        ('<Control-Z>', self.redo)):
            root.bind(seq, lambda e, f=fn: (f(), 'break')[1])
        root.protocol('WM_DELETE_WINDOW', self.on_close)

        self.set_tool('select')
        if path:
            self.open_path(path)
        self.update_title()
        root.after(80, self.fit)

    # --------------------------------------------------------- toolbar ----
    def _btn(self, parent, text, cmd, accent=False):
        b = self.tk.Button(parent, text=text, command=cmd, relief='flat',
                           bg=C['ap'] if accent else 'white',
                           fg='white' if accent else C['text'],
                           activebackground=C['ap_ring'] if accent
                           else C['grid'], padx=7)
        b.pack(side='left', padx=3)
        return b

    def _sep(self, parent):
        self.tk.Frame(parent, width=1, bg=C['grid_major']).pack(
            side='left', fill='y', padx=7, pady=2)

    def _tool(self, parent, tool, text):
        tk = self.tk
        f = tk.Frame(parent, bg=C['bar'], padx=3, pady=1,
                     highlightthickness=1, highlightbackground=C['bar'])
        cv = tk.Canvas(f, width=28, height=26, bg=C['bar'],
                       highlightthickness=0)
        if tool in KINDS:
            draw_icon(cv, tool, *icon_center(tool, 28, 26), s=0.8)
        elif tool == 'select':
            cv.create_polygon(9, 4, 9, 21, 13, 17, 16, 24, 19, 23, 16, 16,
                              21, 16, fill=C['text'], outline='white')
        else:                                                   # link
            cv.create_line(6, 20, 22, 7, fill=C['wired'], width=3)
            for x, y in ((6, 20), (22, 7)):
                cv.create_oval(x - 4, y - 4, x + 4, y + 4, fill=C['ap'],
                               outline='white')
        cv.pack(side='left')
        lb = tk.Label(f, text=text, bg=C['bar'], fg=C['text'])
        lb.pack(side='left', padx=(0, 3))
        for w in (f, cv, lb):
            w.bind('<Button-1>', lambda e, t=tool: self.set_tool(t))
        f.pack(side='left', padx=1)
        self.tool_widgets[tool] = (f, cv, lb)

    def build_toolbar(self):
        tk, ttk = self.tk, self.ttk
        bar = tk.Frame(self.root, bg=C['bar'], padx=6, pady=4)
        bar.pack(fill='x')
        for text, cmd in (('New', self.new), ('Open', self.open),
                          ('Save', self.save), ('Save as', self.save_as),
                          ('Export .py', self.export_py)):
            self._btn(bar, text, cmd)
        self._sep(bar)
        self.tool_widgets = {}
        self._tool(bar, 'select', 'Select')
        for kind, text in (('ap', 'AP'), ('sta', 'Station'), ('car', 'Car'),
                           ('host', 'Host'), ('switch', 'Switch'),
                           ('ctrl', 'Controller')):
            self._tool(bar, kind, text)
        self._tool(bar, 'link', 'Link')
        cb = ttk.Combobox(bar, textvariable=self.link_kind, values=LINK_KINDS,
                          state='readonly', width=7)
        cb.pack(side='left', padx=(0, 2))
        cb.bind('<<ComboboxSelected>>', lambda e: (self.set_tool('link'),
                                                   self.canvas.focus_set()))
        self._sep(bar)
        self._btn(bar, 'Inspector', self.open_inspector, accent=True)
        self._btn(bar, 'Delete', self.delete_selection)

        bar2 = tk.Frame(self.root, bg=C['bar'], padx=6, pady=3)
        bar2.pack(fill='x')
        for text, cmd in (('3D view', self.view_3d), ('Top view', self.view_top),
                          ('Fit', self.fit)):
            self._btn(bar2, text, cmd)
        for text, var in (('Ranges', self.v_ranges), ('Sphere', self.v_sphere),
                          ('Labels', self.v_labels),
                          ('Snap %g m' % self.SNAP, self.v_snap)):
            tk.Checkbutton(bar2, text=text, variable=var, bg=C['bar'],
                           activebackground=C['bar'], highlightthickness=0,
                           command=self.redraw).pack(side='left', padx=6)
        self._sep(bar2)
        self._btn(bar2, 'Undo', self.undo)
        self._btn(bar2, 'Redo', self.redo)
        self._sep(bar2)
        tk.Label(bar2, text='Range', bg=C['bar'], fg=C['text'],
                 font=('TkDefaultFont', 10, 'bold')).pack(side='left')
        self.range_name = tk.Label(bar2, text='(select a wireless device)',
                                   bg=C['bar'], fg=C['muted'], width=22,
                                   anchor='w')
        self.range_name.pack(side='left', padx=(6, 4))
        self.slider = tk.Scale(bar2, from_=0, to=300, orient='horizontal',
                               length=220, resolution=1, showvalue=0,
                               bg=C['ap_ring'], highlightthickness=0, bd=1,
                               troughcolor='white', sliderrelief='flat',
                               activebackground=C['ap'], sliderlength=14,
                               width=12,
                               command=self.on_slider)
        self.slider.pack(side='left', padx=4)
        self.range_val = tk.Label(bar2, text='', bg=C['bar'], fg=C['text'],
                                  width=7, anchor='w',
                                  font=('TkDefaultFont', 10, 'bold'))
        self.range_val.pack(side='left')

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
        if kind != 'wired' and not all(k in ('sta', 'car') for k in ks):
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
        self.sel = None
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
                              self.next_id))

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
        self.nodes, self.links, self.netp, self.next_id = snap
        self._undo_key = None
        if self.sel and self.sel[1] not in (self.nodes if self.sel[0] == 'node'
                                            else self.links):
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
                self.range_name.config(text='(select a wireless device)',
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
        if not self.nodes:
            return 0.0, 0.0, 100.0, 100.0
        xs_lo, xs_hi, ys_lo, ys_hi = [], [], [], []
        for n in self.nodes.values():
            r = self.rng(n)
            x, y, _ = n['xyz']
            xs_lo.append(x - r); xs_hi.append(x + r)
            ys_lo.append(y - r); ys_hi.append(y + r)
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

    def axis_at(self, x, y):
        for h in self.axis_hits:
            axis, x1, y1, x2, y2, L = h
            if _seg_dist(x, y, x1, y1, x2, y2) <= 7:
                return h
        return None

    def on_press(self, e):
        self.canvas.focus_set()
        self.press = (e.x, e.y)
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
        lid = self.link_at(e.x, e.y)
        if lid and self.tool == 'select':
            self.select(('link', lid))
            self.drag = None
            return
        if self.tool in KINDS:
            self.add_node(self.tool, e.x, e.y)
            return
        self.drag = {'mode': 'rotate', 'last': (e.x, e.y), 'moved': False}

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
            self.select(None)
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
        if axis is not None:
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

        self.draw_grid()

        # ranges
        if self.v_ranges.get():
            sphere = self.v_sphere.get()
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
                    if is_ap:
                        self.sphere(p, r, col, width=wd, fill=C['ap_fill'])
                    else:
                        self.sphere(p, r, col, width=wd, dash=(4, 4),
                                    rings=(0,), meridians=1)
                else:
                    pts = self.circle_pts(*p, r)
                    if is_ap:
                        cv.create_polygon(pts, fill=C['ap_fill'], outline='')
                        cv.create_polygon(pts, fill='', outline=col, width=wd)
                    else:
                        cv.create_polygon(pts, fill='', outline=col, width=wd,
                                          dash=(4, 4))

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

        # rubber band while drawing a link
        d = self.drag
        if d and d.get('mode') == 'link' and d['from'] in pos:
            x1, y1, _ = self.project(*pos[d['from']])
            col = C['wired'] if self.link_kind.get() == 'wired' else C['rf']
            cv.create_line(x1, y1, d['to'][0], d['to'][1], fill=col, width=2,
                           dash=(4, 3))

        # nodes, far ones first
        self.hits = []
        order = sorted(pos.items(), key=lambda kv: -self.project(*kv[1])[2])
        for nid, p in order:
            sx, sy, _ = self.project(*p)
            self.draw_node(self.nodes[nid], sx, sy, nid == sel_node)

        # local axes of the selected device
        self.axis_hits = []
        if sel_node in self.nodes:
            self.draw_axes(self.nodes[sel_node])

        self.draw_overlay()
        self.update_status()

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
                                    ('Ad-hoc / mesh', (6, 4), C['rf'], 2),
                                    ('Control', (6, 4), C['control'], 2)):
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
        if n['kind'] == 'ap':
            lines.append('ssid: %s' % (p.get('ssid') or '-'))
            lines.append('%s GHz  ·  channel %s  ·  mode %s' % (
                p.get('band') or '2.4', p.get('channel'), p.get('mode')))
        if n['kind'] in ('sta', 'car'):
            cov = self.coverage(n)
            lines.append('inside: ' + (', '.join(cov) if cov else 'no AP range'))
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
            lines.append(s)
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
        if not counts.get('ctrl') and (counts.get('ap') or
                                       counts.get('switch')):
            lines.append('No controller: APs / switches will be saved as '
                         'failMode=standalone.')
        text = '\n'.join(lines)
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
        return {'net': copy.deepcopy(self.netp), 'nodes': nodes,
                'links': links}

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
        return None

    def build(self):
        tk = self.tk
        for w in self.body.winfo_children():
            w.destroy()
        self.fields, self.row = {}, 0
        self.body.columnconfigure(1, weight=1)
        self.target = self.current()
        kind, oid = self.target
        if kind == 'node':
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
                           width=3, dash=None if wired else (5, 3))
            for x, y in ((8, 30), (34, 8)):
                cv.create_oval(x - 5, y - 5, x + 5, y + 5, fill=C['ap'],
                               outline='white')
            self.title.config(text='%s - %s' % (self.ed.nodes[l['a']]['name'],
                                                self.ed.nodes[l['b']]['name']))
            self.sub.config(text='%s link' % l['kind'])
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
        elif t == 'range':
            var = tk.StringVar()
            w = tk.Frame(self.body, bg=C['bg'])
            w.grid(row=self.row, column=1, sticky='ew')
            sc = tk.Scale(w, from_=0, to=300, orient='horizontal',
                          resolution=0.5, showvalue=0, bg=C['ap_ring'], bd=1,
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

    # ----------------------------------------------------------- values ----
    def get_model(self, key):
        kind, oid = self.target
        o = self.obj()
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
        if self.obj() is None and self.target[0] != 'net':
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
                        r = float(v or 0)
                        rec['scale'].config(to=max(300, math.ceil(
                            r * 1.5 / 50) * 50))
                        rec['scale'].set(r)
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
        if self.loading or self.obj() is None and self.target[0] != 'net':
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
        self.ed.push_undo((self.target, key))
        self.set_model(key, val)
        if rec['scale'] is not None:
            self.loading = True
            try:
                rec['scale'].config(to=max(300, math.ceil(val * 1.5 / 50) * 50))
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
        if key == '__name':
            self.load_values()                  # ssid may have followed
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