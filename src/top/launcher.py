#!/usr/bin/env python3
"""
launcher.py  -  one window, two buttons: Simulate or Open editor.

    sudo python3 launcher.py

Set the two paths below once. Relative paths are taken from the folder of
this file. Both programs are started with no arguments, exactly like
"sudo python3 <file>.py".

Simulate
  - Hidden terminal (default, Options menu): the simulation runs in the
    background. Its output goes to profile/simulation.log, and
    Options > Simulation output shows it live and can send commands to the
    Mininet CLI. Close the viewer window (or type exit in its CLI terminal)
    and the simulation stops by itself.
  - Hidden terminal off: it runs in a terminal window that closes when the
    simulation ends. As root, xterm/konsole are used (they need no desktop
    D-Bus); gnome-terminal/ptyxis are started as your own user instead
    (sudo then asks for the password in that window).
Open editor
  - opens directly (or in a terminal when sudo needs a password).

Options (dark mode, hidden terminal) are kept in profile/launcher.ini.
"""

import configparser
import os
import shlex
import shutil
import subprocess
import sys
import time

# ---------------------------------------------------------------- paths ----
SIMULATION_SCRIPT = 'main.py'          # your Mininet-WiFi script
EDITOR_SCRIPT = 'libs/custom_edit.py'       # the topology editor
PREFERRED_TERMINAL = ''                # e.g. 'xterm'; '' = pick automatically
# ---------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(HERE, 'profile')
INI = os.path.join(PROFILE, 'launcher.ini')
LOG = os.path.join(PROFILE, 'simulation.log')

# Terminals that work when started by root (no user D-Bus session needed).
# xterm is installed together with Mininet, so it is almost always there.
ROOT_SAFE = ('xterm', 'uxterm', 'konsole', 'xfce4-terminal')
# Terminals that need the desktop session (D-Bus): gnome-terminal, ptyxis...
SESSION = ('ptyxis', 'gnome-terminal', 'x-terminal-emulator', 'kgx',
           'mate-terminal', 'lxterminal', 'tilix', 'terminator')

LIGHT = {'bg': '#f6f6f3', 'bar': '#ebebe7', 'tile': '#ffffff',
         'hover': '#e1eafa', 'text': '#26272a', 'muted': '#6d6e69',
         'ap': '#2563c9', 'sta': '#e0761a', 'rf': '#16a05a',
         'line': '#c9cbc3', 'warn': '#b3261e', 'btn': '#dcdcd6',
         'term': '#16171a', 'term_fg': '#d8d8d4'}
DARK = {'bg': '#1d1e21', 'bar': '#26282c', 'tile': '#2b2d32',
        'hover': '#1c2740', 'text': '#e4e4e0', 'muted': '#9a9ca3',
        'ap': '#4f86f0', 'sta': '#f28c3a', 'rf': '#2ec472',
        'line': '#3a3d43', 'warn': '#ff6b5e', 'btn': '#34373d',
        'term': '#111214', 'term_fg': '#d8d8d4'}


def resolve(path):
    return path if os.path.isabs(path) else os.path.join(HERE, path)


# ------------------------------------------------------------- settings ----
class Settings(object):
    """profile/launcher.ini. The theme follows the editor until you pick
    one here."""

    def __init__(self):
        self.cp = configparser.ConfigParser(interpolation=None)
        try:
            self.cp.read(INI)
        except Exception:
            pass
        if not self.cp.has_section('ui'):
            self.cp.add_section('ui')

    def theme(self):
        t = self.cp.get('ui', 'theme', fallback='')
        if t in ('light', 'dark'):
            return t
        try:
            ed = configparser.ConfigParser(interpolation=None)
            ed.read(os.path.join(PROFILE, 'editor.ini'))
            return 'dark' if ed.get('ui', 'theme', fallback='light') == 'dark' \
                else 'light'
        except Exception:
            return 'light'

    def hide_terminal(self):
        return self.cp.getboolean('ui', 'hide_terminal', fallback=True)

    def set(self, key, value):
        self.cp.set('ui', key, str(value))
        try:
            os.makedirs(PROFILE, exist_ok=True)
            with open(INI, 'w') as fh:
                self.cp.write(fh)
        except OSError:
            pass


# ------------------------------------------------------------ terminals ----
def is_root():
    return hasattr(os, 'geteuid') and os.geteuid() == 0


def command(path, sudo):
    """python <file>, prefixed with sudo when asked."""
    cmd = [sys.executable, path]
    return ['sudo', '-E'] + cmd if sudo else cmd


def terminal_argv(name, exe, cmd, title):
    """Argument list that opens terminal <name> running cmd. The window
    closes when cmd ends."""
    if name in ('xterm', 'uxterm'):
        return [exe, '-T', title, '-fa', 'Monospace', '-fs', '11',
                '-geometry', '120x34', '-e'] + cmd
    if name == 'xfce4-terminal':
        return [exe, '--disable-server', '-T', title, '-x'] + cmd
    if name == 'gnome-terminal':
        return [exe, '--title', title, '--'] + cmd
    if name in ('ptyxis', 'kgx', 'tilix'):
        return [exe, '--'] + cmd
    if name in ('konsole', 'x-terminal-emulator', 'terminator'):
        return [exe, '-e'] + cmd
    return [exe, '-e', shlex.join(cmd)]           # mate / lxterminal


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


def plan(path, title, need_terminal, hidden=False):
    """Decide how to start <path>. Returns (argv, how) or (None, why).

    how: 'direct' | 'hidden' | 'root-term' | 'user-term' | 'term' | 'here'"""
    if is_root():
        if not need_terminal:
            return command(path, False), 'direct'
        if hidden:
            return command(path, False), 'hidden'
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
    if t:                       # (a hidden run would have no place for sudo's
        return terminal_argv(t[0], t[1], command(path, True),   # password)
                             title), 'term'
    return None, 'No terminal program found (sudo apt install xterm).'


# ------------------------------------------------------------- launcher ----
class Launcher(object):
    def __init__(self):
        import tkinter as tk
        self.tk = tk
        self.settings = Settings()
        self.sim = None            # hidden simulation (Popen)
        self.child = None          # simulation in the launcher's terminal
        self.out_win = None
        self.closing = False
        root = self.root = tk.Tk()
        root.title('Mininet-WiFi')
        root.resizable(False, False)
        self.v_dark = tk.BooleanVar(value=self.settings.theme() == 'dark')
        self.v_hide = tk.BooleanVar(value=self.settings.hide_terminal())
        root.protocol('WM_DELETE_WINDOW', self.quit)
        root.bind('<Escape>', lambda e: self.quit())
        self.build()

    # ------------------------------------------------------------ build ----
    def build(self, status=''):
        tk = self.tk
        c = self.c = DARK if self.v_dark.get() else LIGHT
        root = self.root
        for w in root.winfo_children():
            if not isinstance(w, tk.Toplevel):
                w.destroy()
        root.configure(bg=c['bg'])
        head = tk.Frame(root, bg=c['bar'], padx=18, pady=12)
        head.pack(fill='x')
        left = tk.Frame(head, bg=c['bar'])
        left.pack(side='left')
        tk.Label(left, text='Mininet-WiFi', bg=c['bar'], fg=c['text'],
                 font=('TkDefaultFont', 15, 'bold')).pack(anchor='w')
        tk.Label(left, text='simulate a network, or design one in the editor',
                 bg=c['bar'], fg=c['muted']).pack(anchor='w')
        mb = tk.Menubutton(head, text='Options', bg=c['btn'], fg=c['text'],
                           activebackground=c['hover'],
                           activeforeground=c['text'], relief='flat',
                           padx=12, pady=4, cursor='hand2')
        mb.pack(side='right', anchor='n')
        m = self.menu = tk.Menu(mb, tearoff=0, bg=c['tile'], fg=c['text'],
                                activebackground=c['hover'],
                                activeforeground=c['text'],
                                selectcolor=c['text'], bd=1, relief='flat')
        m.add_checkbutton(label='Dark mode', variable=self.v_dark,
                          command=self.toggle_dark)
        m.add_checkbutton(label='Hide the simulation terminal',
                          variable=self.v_hide, command=self.toggle_hide)
        m.add_separator()
        m.add_command(label='Simulation output...', command=self.show_output)
        m.add_command(label='Stop the simulation', command=self.stop_sim)
        mb.config(menu=m)
        body = tk.Frame(root, bg=c['bg'], padx=18, pady=18)
        body.pack()
        self.tile(body, 'Simulate', SIMULATION_SCRIPT, self.draw_simulate,
                  self.simulate).pack(side='left', padx=8)
        self.tile(body, 'Open editor', EDITOR_SCRIPT, self.draw_editor,
                  self.editor).pack(side='left', padx=8)
        self.status = tk.Label(root, text=status, bg=c['bar'], fg=c['muted'],
                               anchor='w', padx=12, pady=6, justify='left',
                               wraplength=520)
        self.status.pack(fill='x')
        if self.out_win is not None:
            self.out_win.theme(c)

    def toggle_dark(self):
        self.settings.set('theme', 'dark' if self.v_dark.get() else 'light')
        self.build(self.status.cget('text'))

    def toggle_hide(self):
        self.settings.set('hide_terminal', self.v_hide.get())
        self.say('The simulation will run %s.' % (
            'hidden (output: Options > Simulation output)'
            if self.v_hide.get() else 'in a terminal window'))

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

    def sim_running(self):
        return self.sim is not None and self.sim.poll() is None

    def start(self, path, title, need_terminal):
        full = resolve(path)
        if not os.path.isfile(full):
            return self.say('%s not found. Edit the paths at the top of '
                            'launcher.py.' % full, warn=True)
        if need_terminal and (self.sim_running() or self.child is not None):
            return self.say('The simulation is already running. Close its '
                            'viewer window (or Options > Stop the '
                            'simulation) first.', warn=True)
        hidden = need_terminal and self.v_hide.get()
        cmd, how = plan(full, title, need_terminal, hidden)
        if cmd is None:
            return self.say(how, warn=True)
        name = os.path.basename(full)
        if how == 'hidden':
            return self.run_hidden(cmd, full, name)
        if how == 'here':
            return self.run_here(cmd, full, name)
        try:
            p = subprocess.Popen(cmd, cwd=os.path.dirname(full),
                                 stdin=subprocess.DEVNULL)
        except Exception as e:
            return self.say('Could not start %s: %s' % (full, e), warn=True)
        note = ''
        if hidden and how != 'hidden':
            note = '  (in a terminal: start the launcher with sudo to hide it)'
        self.say('Started %s  (pid %d)%s' % (name, p.pid, note))

    # ------------------------------------------------ hidden simulation ----
    def run_hidden(self, cmd, full, name):
        """No terminal window: the Mininet CLI reads from a pipe we keep,
        its output goes to profile/simulation.log."""
        try:
            os.makedirs(PROFILE, exist_ok=True)
            with open(LOG, 'w') as fh:
                fh.write('*** %s  %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'),
                                           shlex.join(cmd)))
            log = open(LOG, 'a')
            env = dict(os.environ, PYTHONUNBUFFERED='1')
            self.sim = subprocess.Popen(
                cmd, cwd=os.path.dirname(full), stdin=subprocess.PIPE,
                stdout=log, stderr=subprocess.STDOUT, env=env,
                start_new_session=True)
            log.close()
        except Exception as e:
            self.sim = None
            return self.say('Could not start %s: %s' % (full, e), warn=True)
        self.sim_name = name
        self.say('%s is running (hidden terminal). Close the viewer window, '
                 'or type exit in its CLI terminal, to stop it. Output: '
                 'Options > Simulation output.' % name)
        self.root.after(500, self.watch_sim)

    def watch_sim(self):
        if self.sim is None:
            return
        code = self.sim.poll()
        if code is None:
            self.root.after(500, self.watch_sim)
            return
        self.sim = None
        if self.closing:
            return self.root.destroy()
        if code == 0:
            self.say('%s finished.' % self.sim_name)
        else:
            self.say('%s stopped with exit code %d. Last output: %s  '
                     '(Options > Simulation output shows everything)'
                     % (self.sim_name, code, last_lines(LOG)), warn=True)

    def send_cli(self, line):
        """Type a line into the hidden Mininet CLI."""
        if not self.sim_running():
            return False
        try:
            with open(LOG, 'a') as fh:          # echo, like a terminal
                fh.write(line + '\n')
            self.sim.stdin.write((line + '\n').encode())
            self.sim.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def stop_sim(self):
        if self.child is not None:
            return self.say('The simulation runs in the terminal the launcher '
                            'was started from: type exit there.', warn=True)
        if not self.sim_running():
            return self.say('No hidden simulation is running. (One started '
                            'in a terminal window: type exit there.)')
        self.send_cli('exit')
        self.say('Sent "exit" to the Mininet CLI, waiting for %s to stop...'
                 % self.sim_name)
        sim = self.sim

        def force():
            if sim.poll() is None:
                self.say('%s did not stop: terminating it (run "sudo mn -c" '
                         'if anything is left over).' % self.sim_name,
                         warn=True)
                sim.terminate()
        self.root.after(15000, force)

    def show_output(self):
        if self.out_win is not None and self.out_win.alive():
            self.out_win.top.deiconify()
            self.out_win.top.lift()
            return
        self.out_win = OutputWindow(self)

    def quit(self):
        if self.sim_running():
            from tkinter import messagebox
            if not messagebox.askokcancel(
                    'Mininet-WiFi', 'The simulation is still running.\n'
                    'Stop it and close the launcher?', parent=self.root):
                return
            self.closing = True
            return self.stop_sim()
        self.root.destroy()

    # ------------------------------------------- launcher's own terminal ----
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


def last_lines(path, n=2):
    try:
        with open(path, errors='replace') as fh:
            lines = [l.strip() for l in fh.read()[-4000:].splitlines()
                     if l.strip()]
        return ' | '.join(lines[-n:])[-300:]
    except OSError:
        return '(no output)'


class OutputWindow(object):
    """What the hidden simulation prints (profile/simulation.log), live,
    with a line to type Mininet CLI commands."""

    def __init__(self, app):
        tk = app.tk
        self.app, self.pos = app, 0
        top = self.top = tk.Toplevel(app.root)
        top.title('Simulation output')
        top.geometry('820x480')
        self.bar = tk.Frame(top, padx=6, pady=6)
        self.bar.pack(side='bottom', fill='x')
        self.prompt = tk.Label(self.bar, text='mininet-wifi>',
                               font=('TkFixedFont', 10, 'bold'))
        self.prompt.pack(side='left')
        self.v = tk.StringVar()
        self.ent = tk.Entry(self.bar, textvariable=self.v, relief='flat',
                            font=('TkFixedFont', 10), highlightthickness=0)
        self.ent.pack(side='left', fill='x', expand=True, padx=6)
        self.btns = [
            tk.Button(self.bar, text='Stop simulation', relief='flat',
                      command=app.stop_sim),
            tk.Button(self.bar, text='Clear', relief='flat',
                      command=lambda: self.txt.delete('1.0', 'end'))]
        for b in self.btns:
            b.pack(side='right', padx=2)
        self.txt = tk.Text(top, relief='flat', highlightthickness=0,
                           font=('TkFixedFont', 10), padx=8, pady=6,
                           wrap='char')
        sb = self.sb = tk.Scrollbar(top, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self.txt.pack(fill='both', expand=True)
        self.ent.bind('<Return>', self.send)
        self.ent.bind('<KP_Enter>', self.send)
        self.theme(app.c)
        self.ent.focus_set()
        self.poll()

    def alive(self):
        try:
            return bool(self.top.winfo_exists())
        except Exception:
            return False

    def theme(self, c):
        if not self.alive():
            return
        self.bar.config(bg=c['bar'])
        self.prompt.config(bg=c['bar'], fg=c['ap'])
        self.ent.config(bg=c['tile'], fg=c['text'],
                        insertbackground=c['text'])
        for b in self.btns:
            b.config(bg=c['btn'], fg=c['text'], activebackground=c['hover'],
                     activeforeground=c['text'])
        self.txt.config(bg=c['term'], fg=c['term_fg'],
                        insertbackground=c['term_fg'])
        self.top.config(bg=c['bar'])
        self.sb.config(bg=c['btn'], troughcolor=c['bar'],
                       activebackground=c['hover'], highlightthickness=0,
                       bd=0)

    def send(self, e=None):
        line = self.v.get()
        if not line.strip():
            return
        if self.app.send_cli(line):
            self.v.set('')
        else:
            self.app.say('No hidden simulation is running.', warn=True)

    def poll(self):
        if not self.alive():
            return
        try:
            size = os.path.getsize(LOG)
            if size < self.pos:                 # a new run started
                self.txt.delete('1.0', 'end')
                self.pos = 0
            if size > self.pos:
                with open(LOG, 'rb') as fh:
                    fh.seek(self.pos)
                    data = fh.read()
                self.pos += len(data)
                at_end = self.txt.yview()[1] > 0.98
                self.txt.insert('end', data.decode('utf-8', 'replace'))
                if at_end:
                    self.txt.see('end')
        except OSError:
            pass
        running = self.app.sim_running()
        self.prompt.config(text='mininet-wifi>' if running
                           else '(not running)')
        self.top.after(600, self.poll)


if __name__ == '__main__':
    try:
        Launcher().run()
    except Exception as e:
        sys.exit('*** launcher: could not open the window (%s)\n'
                 '*** Install Tkinter: sudo apt install python3-tk' % e)