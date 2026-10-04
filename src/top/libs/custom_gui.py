#!/usr/bin/env python3
"""
wifi_gui.py - a small live viewer for Mininet-WiFi (no matplotlib needed)

Why use it instead of net.plotGraph()?
  - It uses Tkinter, which comes with Python, instead of matplotlib, so it
    avoids the "x must be a sequence" bug that hides nodes in plotGraph.
  - It runs in its own process, so the window keeps refreshing while
    the Mininet CLI is waiting for your commands.
  - Circles stay round (same scale on x and y), and it auto-zooms to your nodes.
  - Nodes without a position are listed at the bottom instead of
    silently disappearing.

How it works with Mininet-WiFi
  1. You build your network as usual (addStation, addAccessPoint, ...).
  2. After net.build(), you create WifiGUI(net) and call .start().
  3. WifiGUI starts a separate window process. A background thread in your
     script reads every node's position, range and association twice per
     second and sends it to the window, which redraws it.
  4. You use CLI(net) like normal. If you move a node in the CLI, e.g.
         mininet-wifi> py sta1.setPosition('250,100,0')
     the window updates by itself.
  5. Call gui.stop() before net.stop().

Usage
    from wifi_gui import WifiGUI
    ...
    net.build()
    ap1.start([])
    gui = WifiGUI(net)          # or WifiGUI(net, max_x=300, max_y=300)
    gui.start()
    CLI(net)
    gui.stop()
    net.stop()

Do NOT also call net.plotGraph(): use one viewer or the other.
Run with:  sudo -E python3 your_script.py   (-E lets root open the window)
"""

import math
import multiprocessing as mp
import queue
import signal
import threading


# --------------------------------------------------------------------------
# Part 1: reading the network (runs inside your Mininet script)
# --------------------------------------------------------------------------

def _xy(node):
    """Return (x, y) of a node, or None if it has no position."""
    try:
        x, y, _ = node.getxyz()
        return float(x), float(y)
    except Exception:
        pass
    pos = getattr(node, 'position', None) or node.params.get('position')
    if pos is None:
        return None
    if isinstance(pos, str):
        pos = pos.split(',')
    try:
        return float(pos[0]), float(pos[1])
    except (ValueError, IndexError, TypeError):
        return None


def _range(node):
    """Biggest wireless range of the node (0 if none)."""
    ranges = []
    for intf in getattr(node, 'wintfs', {}).values():
        try:
            ranges.append(float(intf.range))
        except (AttributeError, TypeError, ValueError):
            pass
    return max(ranges) if ranges else 0.0


def _associated_ap(node):
    """Name of the AP this station is connected to, or None."""
    for intf in getattr(node, 'wintfs', {}).values():
        ap_intf = getattr(intf, 'associatedTo', None)
        # associatedTo can also be a string like 'adhoc' or 'mesh': ignore those
        if ap_intf is not None and not isinstance(ap_intf, str):
            ap_node = getattr(ap_intf, 'node', None)
            if ap_node is not None:
                return ap_node.name
    return None


def snapshot(net):
    """Collect everything the window needs to draw, as plain data."""
    groups = [('ap', 'aps'), ('sta', 'stations'), ('sta', 'cars'),
              ('host', 'hosts')]
    nodes, seen = [], set()
    for kind, attr in groups:
        for node in getattr(net, attr, []) or []:
            if node.name in seen:
                continue
            seen.add(node.name)
            nodes.append({
                'name': node.name,
                'kind': kind,
                'xy': _xy(node),
                'range': _range(node),
                'ap': _associated_ap(node) if kind == 'sta' else None,
            })
    return nodes


class WifiGUI(object):
    """Live window that shows APs, stations, their ranges and connections."""

    def __init__(self, net, min_x=None, min_y=None, max_x=None, max_y=None,
                 refresh=0.5, size=640, title='Mininet-WiFi View'):
        self.net = net
        self.refresh = refresh
        self.cfg = {'bounds': (min_x, min_y, max_x, max_y),
                    'size': size, 'title': title}
        self._queue = None
        self._proc = None
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        ctx = mp.get_context('fork')            # Linux only, like Mininet
        self._queue = ctx.Queue()
        self._queue.put(snapshot(self.net))     # first picture right away
        self._proc = ctx.Process(target=_window_main,
                                 args=(self._queue, self.cfg), daemon=True)
        self._proc.start()
        self._thread = threading.Thread(target=self._sender, daemon=True)
        self._thread.start()
        return self

    def _sender(self):
        last = None
        while not self._stop.wait(self.refresh):
            if not self._proc.is_alive():       # window was closed
                return
            try:
                data = snapshot(self.net)
            except Exception:
                continue
            if data != last:                    # only send changes
                self._queue.put(data)
                last = data

    def stop(self):
        self._stop.set()
        if self._proc is not None and self._proc.is_alive():
            self._queue.put(None)               # ask the window to close
            self._proc.join(2)
            if self._proc.is_alive():
                self._proc.terminate()


# --------------------------------------------------------------------------
# Part 2: the window (runs in its own process, only uses Tkinter)
# --------------------------------------------------------------------------

COLORS = {
    'bg': '#f7f7f5', 'grid': '#e3e3df', 'axis': '#8a8a85', 'text': '#2b2b2b',
    'ap': '#2f6fd6', 'ap_range': '#2f6fd6', 'ap_fill': '#e3ecfb',
    'sta': '#e07a1f',
    'host': '#6b6b6b', 'link': '#2a9d55', 'warn': '#b3261e',
}


def _nice_step(span, target=8):
    """Pick a grid step like 1, 2, 5, 10, 20, 50 ... giving ~target lines."""
    raw = max(span, 1e-9) / target
    power = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        if raw <= m * power:
            return m * power
    return 10 * power


class _Window(object):
    PAD = 48          # space around the plot for axis labels

    def __init__(self, q, cfg):
        import tkinter as tk
        self.tk = tk
        self.q = q
        self.cfg = cfg
        self.nodes = []

        self.root = tk.Tk()
        self.root.title(cfg['title'])
        size = cfg['size']
        self.canvas = tk.Canvas(self.root, width=size, height=size,
                                bg=COLORS['bg'], highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self.status = tk.Label(self.root, anchor='w', justify='left',
                               font=('TkDefaultFont', 10), padx=8, pady=6,
                               bg='#ececea', fg=COLORS['text'])
        self.status.pack(fill='x')
        self.tip = None

        self.canvas.bind('<Configure>', lambda e: self.redraw())
        self.root.after(100, self.poll)

    # ---- data -----------------------------------------------------------
    def poll(self):
        latest, got = None, False
        try:
            while True:
                item = self.q.get_nowait()
                if item is None:                 # stop() was called
                    self.root.destroy()
                    return
                latest, got = item, True
        except queue.Empty:
            pass
        if got:
            self.nodes = latest
            self.redraw()
        self.root.after(200, self.poll)

    # ---- coordinates ----------------------------------------------------
    def world_bounds(self):
        min_x, min_y, max_x, max_y = self.cfg['bounds']
        placed = [n for n in self.nodes if n['xy']]
        if placed:
            xs_lo = [n['xy'][0] - n['range'] for n in placed if n['kind'] == 'ap'] + \
                    [n['xy'][0] for n in placed]
            xs_hi = [n['xy'][0] + n['range'] for n in placed if n['kind'] == 'ap'] + \
                    [n['xy'][0] for n in placed]
            ys_lo = [n['xy'][1] - n['range'] for n in placed if n['kind'] == 'ap'] + \
                    [n['xy'][1] for n in placed]
            ys_hi = [n['xy'][1] + n['range'] for n in placed if n['kind'] == 'ap'] + \
                    [n['xy'][1] for n in placed]
            auto = (min(xs_lo), min(ys_lo), max(xs_hi), max(ys_hi))
        else:
            auto = (0, 0, 100, 100)
        lo_x = auto[0] if min_x is None else min_x
        lo_y = auto[1] if min_y is None else min_y
        hi_x = auto[2] if max_x is None else max_x
        hi_y = auto[3] if max_y is None else max_y
        # small margin when auto-fitting
        if max_x is None or min_x is None:
            m = (hi_x - lo_x) * 0.05 or 10
            lo_x, hi_x = (lo_x - m if min_x is None else lo_x,
                          hi_x + m if max_x is None else hi_x)
        if max_y is None or min_y is None:
            m = (hi_y - lo_y) * 0.05 or 10
            lo_y, hi_y = (lo_y - m if min_y is None else lo_y,
                          hi_y + m if max_y is None else hi_y)
        return lo_x, lo_y, max(hi_x, lo_x + 1), max(hi_y, lo_y + 1)

    def setup_transform(self):
        w = max(self.canvas.winfo_width(), 100)
        h = max(self.canvas.winfo_height(), 100)
        lo_x, lo_y, hi_x, hi_y = self.world_bounds()
        # same scale on both axes so circles stay round
        scale = min((w - 2 * self.PAD) / (hi_x - lo_x),
                    (h - 2 * self.PAD) / (hi_y - lo_y))
        used_w, used_h = (hi_x - lo_x) * scale, (hi_y - lo_y) * scale
        self.ox = (w - used_w) / 2
        self.oy = (h + used_h) / 2
        self.scale = scale
        self.bounds = lo_x, lo_y, hi_x, hi_y

    def to_px(self, x, y):
        lo_x, lo_y, _, _ = self.bounds
        return (self.ox + (x - lo_x) * self.scale,
                self.oy - (y - lo_y) * self.scale)       # y grows upward

    # ---- drawing --------------------------------------------------------
    def redraw(self):
        c = self.canvas
        c.delete('all')
        self.setup_transform()

        by_name = {n['name']: n for n in self.nodes}
        placed = [n for n in self.nodes if n['xy']]

        # 1) ranges (behind everything)
        for n in placed:
            if n['range'] <= 0:
                continue
            x, y = self.to_px(*n['xy'])
            r = n['range'] * self.scale
            if n['kind'] == 'ap':      # only AP coverage matters for connecting
                c.create_oval(x - r, y - r, x + r, y + r,
                              outline=COLORS['ap_range'], width=1.5,
                              fill=COLORS['ap_fill'])

        self.draw_grid()          # grid on top of the light range fill

        # 2) station -> AP connections
        for n in placed:
            ap = by_name.get(n['ap']) if n['ap'] else None
            if ap and ap['xy']:
                x1, y1 = self.to_px(*n['xy'])
                x2, y2 = self.to_px(*ap['xy'])
                c.create_line(x1, y1, x2, y2, fill=COLORS['link'], width=2)

        # 3) nodes and labels
        for n in placed:
            x, y = self.to_px(*n['xy'])
            tag = 'node_' + n['name']
            if n['kind'] == 'ap':
                c.create_rectangle(x - 8, y - 8, x + 8, y + 8,
                                   fill=COLORS['ap'], outline='white',
                                   width=2, tags=(tag,))
            elif n['kind'] == 'sta':
                c.create_oval(x - 7, y - 7, x + 7, y + 7, fill=COLORS['sta'],
                              outline='white', width=2, tags=(tag,))
            else:
                c.create_oval(x - 6, y - 6, x + 6, y + 6, fill=COLORS['host'],
                              outline='white', width=2, tags=(tag,))
            c.create_text(x, y + 18, text=n['name'], fill=COLORS['text'],
                          font=('TkDefaultFont', 10, 'bold'), tags=(tag,))
            c.tag_bind(tag, '<Enter>', lambda e, n=n: self.show_tip(e, n))
            c.tag_bind(tag, '<Leave>', lambda e: self.hide_tip())

        self.draw_legend()
        self.update_status(by_name)

    def draw_grid(self):
        c = self.canvas
        lo_x, lo_y, hi_x, hi_y = self.bounds
        step = _nice_step(max(hi_x - lo_x, hi_y - lo_y))
        x0, y0 = self.to_px(lo_x, lo_y)
        x1, y1 = self.to_px(hi_x, hi_y)
        v = math.ceil(lo_x / step) * step
        while v <= hi_x + 1e-9:
            px, _ = self.to_px(v, lo_y)
            c.create_line(px, y1, px, y0, fill=COLORS['grid'])
            c.create_text(px, y0 + 12, text='%g' % v, fill=COLORS['axis'],
                          font=('TkDefaultFont', 8))
            v += step
        v = math.ceil(lo_y / step) * step
        while v <= hi_y + 1e-9:
            _, py = self.to_px(lo_x, v)
            c.create_line(x0, py, x1, py, fill=COLORS['grid'])
            c.create_text(x0 - 6, py, text='%g' % v, anchor='e',
                          fill=COLORS['axis'], font=('TkDefaultFont', 8))
            v += step
        c.create_rectangle(x0, y1, x1, y0, outline=COLORS['axis'])
        c.create_text((x0 + x1) / 2, y0 + 28, text='meters',
                      fill=COLORS['axis'], font=('TkDefaultFont', 8))

    def draw_legend(self):
        c = self.canvas
        x, y = 12, 12
        c.create_rectangle(x, y, x + 10, y + 10, fill=COLORS['ap'], outline='')
        c.create_text(x + 16, y + 5, text='Access point', anchor='w',
                      fill=COLORS['text'], font=('TkDefaultFont', 9))
        c.create_oval(x + 100, y, x + 110, y + 10, fill=COLORS['sta'], outline='')
        c.create_text(x + 116, y + 5, text='Station', anchor='w',
                      fill=COLORS['text'], font=('TkDefaultFont', 9))
        c.create_line(x + 172, y + 5, x + 190, y + 5, fill=COLORS['link'], width=2)
        c.create_text(x + 196, y + 5, text='Connected', anchor='w',
                      fill=COLORS['text'], font=('TkDefaultFont', 9))

    def update_status(self, by_name):
        lines = []
        for n in self.nodes:
            if not n['xy']:
                lines.append('! %s has no position, so it is not drawn '
                             '(add position="x,y,0")' % n['name'])
            elif n['kind'] == 'sta':
                ap = by_name.get(n['ap']) if n['ap'] else None
                if ap and ap['xy']:
                    d = math.dist(n['xy'], ap['xy'])
                    lines.append('%s  ->  %s   (%.0f m)' % (n['name'], ap['name'], d))
                else:
                    lines.append('%s  ->  not connected' % n['name'])
        if not self.nodes:
            lines.append('Waiting for network data...')
        warn = any(l.startswith('!') for l in lines)
        self.status.config(text='\n'.join(lines) or ' ',
                           fg=COLORS['warn'] if warn else COLORS['text'])

    # ---- hover tooltip --------------------------------------------------
    def show_tip(self, event, n):
        self.hide_tip()
        x, y = n['xy']
        text = '%s (%s)\nposition: %g, %g\nrange: %g m' % (
            n['name'], {'ap': 'access point', 'sta': 'station'}.get(n['kind'], n['kind']),
            x, y, n['range'])
        if n['kind'] == 'sta':
            text += '\nconnected to: %s' % (n['ap'] or 'nothing')
        self.tip = self.canvas.create_text(event.x + 14, event.y - 10, text=text,
                                           anchor='sw', fill='white',
                                           font=('TkDefaultFont', 9))
        bbox = self.canvas.bbox(self.tip)
        bg = self.canvas.create_rectangle(bbox[0] - 6, bbox[1] - 4,
                                          bbox[2] + 6, bbox[3] + 4,
                                          fill='#2b2b2b', outline='')
        self.canvas.tag_raise(self.tip, bg)
        self.tip = (self.tip, bg)

    def hide_tip(self):
        if self.tip:
            for item in self.tip:
                self.canvas.delete(item)
            self.tip = None

    def run(self):
        self.root.mainloop()


def _window_main(q, cfg):
    # Ctrl+C in the Mininet CLI must not kill the window process
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        win = _Window(q, cfg)
    except Exception as e:      # usually: no display available
        print('\n*** wifi_gui: could not open the window (%s)\n'
              '*** Try: sudo -E python3 your_script.py\n'
              '*** or run "xhost +local:root" first.\n' % e)
        return
    win.run()