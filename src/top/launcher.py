#!/usr/bin/env python3
"""
launcher.py  -  one window, two buttons: Simulate or Open editor.

    sudo python3 launcher.py

Set the two paths below once. Relative paths are taken from the folder of
this file. Both programs are started with no arguments, exactly like
"sudo python3 <file>.py":
  - Simulate runs in a new terminal window, because the Mininet CLI needs
    one. As root, xterm/konsole are used (they need no desktop D-Bus);
    gnome-terminal/ptyxis are started as your own user instead (sudo then
    asks for the password in that window). With no usable terminal, the
    simulation runs in the terminal you started the launcher from.
  - the editor opens directly (or in a terminal when sudo needs a password)
"""

import os
import shlex
import shutil
import subprocess
import sys

# ---------------------------------------------------------------- paths ----
SIMULATION_SCRIPT = 'main.py'          # your Mininet-WiFi script
EDITOR_SCRIPT = 'libs/custom_edit.py'       # the topology editor
PREFERRED_TERMINAL = ''                # e.g. 'xterm'; '' = pick automatically
# ---------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))

# Terminals that work when started by root (no user D-Bus session needed).
# xterm is installed together with Mininet, so it is almost always there.
ROOT_SAFE = ('xterm', 'uxterm', 'konsole', 'xfce4-terminal')
# Terminals that need the desktop session (D-Bus): gnome-terminal, ptyxis...
SESSION = ('ptyxis', 'gnome-terminal', 'x-terminal-emulator', 'kgx',
           'mate-terminal', 'lxterminal', 'tilix', 'terminator')

LIGHT = {'bg': '#f6f6f3', 'bar': '#ebebe7', 'tile': '#ffffff',
         'hover': '#e1eafa', 'text': '#26272a', 'muted': '#6d6e69',
         'ap': '#2563c9', 'sta': '#e0761a', 'rf': '#16a05a',
         'line': '#c9cbc3', 'warn': '#b3261e'}
DARK = {'bg': '#1d1e21', 'bar': '#26282c', 'tile': '#2b2d32',
        'hover': '#1c2740', 'text': '#e4e4e0', 'muted': '#9a9ca3',
        'ap': '#4f86f0', 'sta': '#f28c3a', 'rf': '#2ec472',
        'line': '#3a3d43', 'warn': '#ff6b5e'}


def resolve(path):
    return path if os.path.isabs(path) else os.path.join(HERE, path)


def theme():
    """Follow the editor's theme (profile/editor.ini), if any."""
    try:
        import configparser
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(os.path.join(HERE, 'profile', 'editor.ini'))
        return DARK if cp.get('ui', 'theme', fallback='light') == 'dark' \
            else LIGHT
    except Exception:
        return LIGHT


def is_root():
    return hasattr(os, 'geteuid') and os.geteuid() == 0


def command(path, sudo):
    """python <file>, prefixed with sudo when asked."""
    cmd = [sys.executable, path]
    return ['sudo', '-E'] + cmd if sudo else cmd


def terminal_argv(name, exe, cmd, title):
    """Argument list that opens terminal <name> running cmd, kept open."""
    script = '%s; echo; read -p "[%s finished - press Enter to close]" _' % (
        shlex.join(cmd), title)
    full = ['bash', '-c', script]
    if name in ('xterm', 'uxterm'):
        return [exe, '-T', title, '-fa', 'Monospace', '-fs', '11',
                '-geometry', '120x34', '-e'] + full
    if name == 'xfce4-terminal':
        return [exe, '--disable-server', '-T', title, '-x'] + full
    if name in ('gnome-terminal', 'ptyxis', 'kgx', 'tilix'):
        if name == 'gnome-terminal':
            return [exe, '--title', title, '--'] + full
        return [exe, '--'] + full
    if name in ('konsole', 'x-terminal-emulator', 'terminator'):
        return [exe, '-e'] + full
    return [exe, '-e', shlex.join(full)]          # mate / lxterminal


def find_terminal(names):
    if PREFERRED_TERMINAL:
        names = (PREFERRED_TERMINAL,) + tuple(n for n in names
                                              if n != PREFERRED_TERMINAL)
    for n in names:
        exe = shutil.which(n)
        if exe:
            return n, exe
    return None


def user_session():
    """(user, env) of the desktop user who ran sudo, or None.

    Under sudo, root has no D-Bus session, so gnome-terminal / ptyxis fail
    with "Failed to connect to user scope bus". Starting them as the real
    user with that user's session variables fixes it."""
    user, uid = os.environ.get('SUDO_USER'), os.environ.get('SUDO_UID')
    if not user or not uid or user == 'root':
        return None
    run = '/run/user/%s' % uid
    if not os.path.isdir(run):
        return None
    env = {'XDG_RUNTIME_DIR': run}
    bus = os.path.join(run, 'bus')
    if os.path.exists(bus):
        env['DBUS_SESSION_BUS_ADDRESS'] = 'unix:path=' + bus
    for k in ('DISPLAY', 'XAUTHORITY', 'WAYLAND_DISPLAY'):
        if os.environ.get(k):
            env[k] = os.environ[k]
    if 'WAYLAND_DISPLAY' not in env and \
            os.path.exists(os.path.join(run, 'wayland-0')):
        env['WAYLAND_DISPLAY'] = 'wayland-0'
    try:
        import pwd
        env['HOME'] = pwd.getpwnam(user).pw_dir
    except Exception:
        pass
    return user, env


def plan(path, title, need_terminal):
    """Decide how to start <path>. Returns (argv, how) or (None, why).

    how: 'direct' | 'root-term' | 'user-term' | 'term' | 'here'"""
    if is_root():
        if not need_terminal:
            return command(path, False), 'direct'
        t = find_terminal(ROOT_SAFE)
        if t:                                    # root terminal, no password
            return terminal_argv(t[0], t[1], command(path, False),
                                 title), 'root-term'
        s, t = user_session(), find_terminal(SESSION)
        if s and t:                              # terminal as the real user
            user, env = s
            argv = terminal_argv(t[0], t[1], command(path, True), title)
            pre = ['sudo', '-u', user, 'env'] + \
                ['%s=%s' % kv for kv in sorted(env.items())]
            return pre + argv, 'user-term'
        return command(path, False), 'here'      # use the launcher's terminal
    t = find_terminal(SESSION + ROOT_SAFE)       # not root: normal session
    if t:
        return terminal_argv(t[0], t[1], command(path, True), title), 'term'
    return None, 'No terminal program found (sudo apt install xterm).'


class Launcher(object):
    def __init__(self):
        import tkinter as tk
        self.tk = tk
        self.c = c = theme()
        root = self.root = tk.Tk()
        root.title('Mininet-WiFi')
        root.configure(bg=c['bg'])
        root.resizable(False, False)
        head = tk.Frame(root, bg=c['bar'], padx=18, pady=12)
        head.pack(fill='x')
        tk.Label(head, text='Mininet-WiFi', bg=c['bar'], fg=c['text'],
                 font=('TkDefaultFont', 15, 'bold')).pack(anchor='w')
        tk.Label(head, text='simulate a network, or design one in the editor',
                 bg=c['bar'], fg=c['muted']).pack(anchor='w')
        body = tk.Frame(root, bg=c['bg'], padx=18, pady=18)
        body.pack()
        self.tile(body, 'Simulate', SIMULATION_SCRIPT, self.draw_simulate,
                  self.simulate).pack(side='left', padx=8)
        self.tile(body, 'Open editor', EDITOR_SCRIPT, self.draw_editor,
                  self.editor).pack(side='left', padx=8)
        self.status = tk.Label(root, text='', bg=c['bar'], fg=c['muted'],
                               anchor='w', padx=12, pady=6, justify='left',
                               wraplength=520)
        self.status.pack(fill='x')
        root.bind('<Escape>', lambda e: root.destroy())

    # ------------------------------------------------------------ tiles ----
    def tile(self, parent, title, path, draw, action):
        tk, c = self.tk, self.c
        f = tk.Frame(parent, bg=c['tile'], highlightthickness=1,
                     highlightbackground=c['line'], cursor='hand2')
        cv = tk.Canvas(f, width=220, height=150, bg=c['tile'],
                       highlightthickness=0)
        cv.pack(padx=10, pady=(12, 4))
        draw(cv)
        t = tk.Label(f, text=title, bg=c['tile'], fg=c['text'],
                     font=('TkDefaultFont', 12, 'bold'))
        t.pack()
        ok = os.path.isfile(resolve(path))
        p = tk.Label(f, text=path if ok else '%s  (not found)' % path,
                     bg=c['tile'], fg=c['muted'] if ok else c['warn'],
                     font=('TkDefaultFont', 8))
        p.pack(pady=(0, 12))
        parts = (f, cv, t, p)

        def hl(on):
            for w in parts:
                w.config(bg=c['hover'] if on else c['tile'])
        for w in parts:
            w.bind('<Button-1>', lambda e: action())
            w.bind('<Enter>', lambda e: hl(True))
            w.bind('<Leave>', lambda e: hl(False))
        return f

    def draw_simulate(self, cv):
        c = self.c
        cx, cy = 110, 78
        for r, col in ((62, c['line']), (46, c['line'])):
            cv.create_oval(cx - r, cy - r, cx + r, cy + r, outline=col,
                           width=2, dash=(4, 4))
        # access point with waves
        cv.create_rectangle(cx - 22, cy - 6, cx + 22, cy + 18, fill=c['ap'],
                            outline='')
        cv.create_line(cx + 12, cy - 6, cx + 12, cy - 26, fill=c['ap'],
                       width=3)
        for r in (10, 18, 26):
            cv.create_arc(cx + 12 - r, cy - 26 - r, cx + 12 + r, cy - 26 + r,
                          start=40, extent=100, style='arc', outline=c['rf'],
                          width=3)
        for i in (-12, -2):
            cv.create_oval(cx + i - 3, cy + 3, cx + i + 3, cy + 9,
                           fill='#9ff0b8', outline='')
        # stations + play button
        for sx, sy in ((cx - 70, cy + 40), (cx + 70, cy + 34)):
            cv.create_oval(sx - 9, sy - 9, sx + 9, sy + 9, fill=c['sta'],
                           outline='')
            cv.create_line(sx, sy, cx, cy + 6, fill=c['rf'], dash=(3, 3))
        cv.create_oval(cx - 18, cy + 30, cx + 18, cy + 66, fill=c['rf'],
                       outline='')
        cv.create_polygon(cx - 6, cy + 38, cx - 6, cy + 58, cx + 11, cy + 48,
                          fill='white', outline='')

    def draw_editor(self, cv):
        c = self.c
        for i in range(0, 221, 22):                  # grid
            cv.create_line(i, 10, i, 140, fill=c['line'])
        for j in range(10, 141, 22):
            cv.create_line(0, j, 220, j, fill=c['line'])
        cv.create_rectangle(44, 54, 92, 78, fill=c['ap'], outline='')
        cv.create_line(80, 54, 80, 38, fill=c['ap'], width=3)
        cv.create_oval(130, 92, 148, 110, fill=c['sta'], outline='')
        cv.create_line(92, 70, 130, 100, fill=c['muted'], width=2,
                       dash=(5, 3))
        cv.create_rectangle(108, 20, 116, 120, fill='#b4583d', outline='')
        # pencil
        cv.create_polygon(150, 30, 196, 76, 186, 86, 140, 40, fill='#f3b23a',
                          outline='')
        cv.create_polygon(196, 76, 202, 96, 186, 86, fill=c['text'],
                          outline='')
        cv.create_polygon(150, 30, 140, 40, 134, 34, 144, 24, fill='#e07a8a',
                          outline='')

    # ---------------------------------------------------------- actions ----
    def say(self, text, warn=False):
        self.status.config(text=text, fg=self.c['warn'] if warn
                           else self.c['muted'])

    def start(self, path, title, need_terminal):
        full = resolve(path)
        if not os.path.isfile(full):
            return self.say('%s not found. Edit the paths at the top of '
                            'launcher.py.' % full, warn=True)
        if getattr(self, 'child', None) is not None:
            return self.say('Something is already running in this terminal.',
                            warn=True)
        cmd, how = plan(full, title, need_terminal)
        if cmd is None:
            return self.say(how, warn=True)
        name = os.path.basename(full)
        if how == 'here':
            return self.run_here(cmd, full, name)
        try:
            p = subprocess.Popen(cmd, cwd=os.path.dirname(full),
                                 stdin=subprocess.DEVNULL)
            self.say('Started %s  (pid %d)' % (name, p.pid))
        except Exception as e:
            self.say('Could not start %s: %s' % (full, e), warn=True)

    def run_here(self, cmd, full, name):
        """No usable terminal window: run in the terminal that started the
        launcher (the Mininet CLI appears there). The launcher hides until
        the program exits."""
        print('\n*** launcher: running %s here (no terminal window could '
              'be opened; install xterm for a separate window)\n'
              % name, flush=True)
        try:
            self.child = subprocess.Popen(cmd, cwd=os.path.dirname(full))
        except Exception as e:
            self.child = None
            return self.say('Could not start %s: %s' % (full, e), warn=True)
        self.root.withdraw()
        self.wait_child(name)

    def wait_child(self, name):
        code = self.child.poll()
        if code is None:
            self.root.after(400, self.wait_child, name)
            return
        self.child = None
        self.root.deiconify()
        self.say('%s finished (exit code %d)' % (name, code),
                 warn=code != 0)

    def simulate(self):
        self.start(SIMULATION_SCRIPT, 'Mininet-WiFi simulation', True)

    def editor(self):
        self.start(EDITOR_SCRIPT, 'Mininet-WiFi editor', False)

    def run(self):
        self.root.mainloop()


if __name__ == '__main__':
    try:
        Launcher().run()
    except Exception as e:
        sys.exit('*** launcher: could not open the window (%s)\n'
                 '*** Install Tkinter: sudo apt install python3-tk' % e)