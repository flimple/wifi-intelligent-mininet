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

Options (dark mode, hidden terminal, NetworkManager pause, clean-up before
each run) are kept in profile/launcher.ini. Options > System check (F1)
tells you whether this computer is ready and fixes the usual problems.
Keys: S = simulate, E = editor, F1 = system check, Esc = close.
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
EDITOR_SCRIPT = 'libs/custom_edit.py'       # the editor: a .py, or a compiled
#                                        editor program (e.g. 'miniedit')
PREFERRED_TERMINAL = ''                # e.g. 'xterm'; '' = pick automatically
PYTHON = ''                            # interpreter for the .py files;
#                     '' = this Python, or python3 when the launcher is compiled
# ---------------------------------------------------------------------------

# Compiled with Nuitka (or PyInstaller)? Then __file__ is inside a temporary
# folder and sys.executable is the launcher itself: use the program's own
# folder and the system python3 instead.
COMPILED = '__compiled__' in globals() or getattr(sys, 'frozen', False)
HERE = os.path.dirname(os.path.realpath(sys.argv[0] if COMPILED
                                        else __file__))


def python_exe():
    if PYTHON:
        return PYTHON
    if COMPILED:
        return shutil.which('python3') or '/usr/bin/python3'
    return sys.executable
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

    def flag(self, key, default=False):
        try:
            return self.cp.getboolean('ui', key, fallback=default)
        except ValueError:
            return default

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
    if not path.endswith('.py') and os.access(path, os.X_OK):
        cmd = [path]                     # a compiled program
    else:
        cmd = [python_exe(), path]
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
        self.v_pause_nm = tk.BooleanVar(value=self.settings.flag('pause_nm'))
        self.v_clean = tk.BooleanVar(value=self.settings.flag('clean_first'))
        self.nm_paused = False
        self.busy = False
        root.protocol('WM_DELETE_WINDOW', self.quit)
        root.bind('<Escape>', lambda e: self.quit())
        root.bind('<Key-s>', lambda e: self.simulate())
        root.bind('<Key-e>', lambda e: self.editor())
        root.bind('<F1>', lambda e: self.system_check())
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
        m.add_checkbutton(label='Stop NetworkManager during simulations',
                          variable=self.v_pause_nm,
                          command=lambda: self.settings.set(
                              'pause_nm', self.v_pause_nm.get()))
        m.add_checkbutton(label='Clean up before each simulation (mn -c)',
                          variable=self.v_clean,
                          command=lambda: self.settings.set(
                              'clean_first', self.v_clean.get()))
        m.add_separator()
        m.add_command(label='Simulation output...', command=self.show_output)
        m.add_command(label='Stop the simulation', command=self.stop_sim)
        m.add_separator()
        m.add_command(label='System check...  (F1)',
                      command=self.system_check)
        m.add_command(label='Clean up now (mn -c)', command=self.clean_now)
        m.add_command(label='Start NetworkManager again',
                      command=lambda: self.restore_nm(force=True))
        mb.config(menu=m)
        body = tk.Frame(root, bg=c['bg'], padx=18, pady=18)
        body.pack()
        self.tile(body, 'Simulate', SIMULATION_SCRIPT, self.draw_simulate,
                  self.simulate, 'S').pack(side='left', padx=8)
        self.tile(body, 'Open editor', EDITOR_SCRIPT, self.draw_editor,
                  self.editor, 'E').pack(side='left', padx=8)
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
    def tile(self, parent, title, path, draw, action, key=''):
        tk, c = self.tk, self.c
        f = tk.Frame(parent, bg=c['tile'], highlightthickness=1,
                     highlightbackground=c['line'], cursor='hand2')
        cv = tk.Canvas(f, width=220, height=150, bg=c['tile'],
                       highlightthickness=0)
        cv.pack(padx=10, pady=(12, 4))
        draw(cv)
        t = tk.Label(f, text=title + ('   (%s)' % key if key else ''),
                     bg=c['tile'], fg=c['text'],
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
        if how == 'root-term':
            self.root.after(1000, self.watch_term, p)
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
        self.restore_nm()
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
        self.restore_nm()
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
        self.restore_nm()
        self.root.deiconify()
        self.say('%s finished (exit code %d)' % (name, code),
                 warn=code != 0)

    def simulate(self):
        if self.busy:
            return
        if is_root() and (self.v_pause_nm.get() or self.v_clean.get()) and \
                not (self.sim_running() or self.child is not None):
            steps = []
            if self.v_pause_nm.get():
                steps.append('systemctl stop NetworkManager wpa_supplicant')
            if self.v_clean.get():
                steps.append('mn -c')
            self.say('Preparing: %s...' % ' ; '.join(steps))
            self.busy = True

            def done(out):
                self.busy = False
                if self.v_pause_nm.get():
                    self.nm_paused = True
                self.start(SIMULATION_SCRIPT, 'Mininet-WiFi simulation', True)
            return self.run_bg(' ; '.join(s_ + ' >/dev/null 2>&1'
                                          for s_ in steps), done)
        self.start(SIMULATION_SCRIPT, 'Mininet-WiFi simulation', True)

    # -------------------------------------------------- system helpers ----
    def run_bg(self, shell_cmd, then=None, timeout=120):
        """Run a shell command without freezing the window."""
        import threading
        box = {}

        def work():
            try:
                box['out'] = subprocess.run(
                    shell_cmd, shell=True, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    timeout=timeout).stdout.decode('utf-8', 'replace')
            except Exception as e:
                box['out'] = str(e)
        th = threading.Thread(target=work, daemon=True)
        th.start()

        def wait():
            if th.is_alive():
                return self.root.after(200, wait)
            if then:
                then(box.get('out', ''))
        self.root.after(200, wait)

    def clean_now(self):
        if not is_root():
            return self.say('Start the launcher with sudo to clean up.',
                            warn=True)
        if self.sim_running():
            return self.say('Stop the simulation first.', warn=True)
        self.say('Cleaning up Mininet (mn -c)...')
        self.run_bg('mn -c', lambda out: self.say(
            'Mininet cleaned up (leftover interfaces, switches and '
            'processes removed).'))

    def restore_nm(self, force=False):
        if not (self.nm_paused or force) or not is_root():
            if force and not is_root():
                self.say('Start the launcher with sudo for this.', warn=True)
            return
        self.nm_paused = False
        subprocess.Popen('systemctl start NetworkManager >/dev/null 2>&1',
                         shell=True)
        self.say('NetworkManager started again.')

    def watch_term(self, p):
        """A root terminal (xterm...) with the simulation: when it closes,
        NetworkManager can come back."""
        if p.poll() is None:
            return self.root.after(1000, self.watch_term, p)
        self.restore_nm()

    def system_check(self):
        SystemCheck(self)

    def editor(self):
        self.start(EDITOR_SCRIPT, 'Mininet-WiFi editor', False)

    def run(self):
        self.root.mainloop()


NM_CONF = '/etc/NetworkManager/conf.d/99-mininet-wifi.conf'
NM_TEXT = ('[keyfile]\nunmanaged-devices=driver:mac80211_hwsim;'
           'interface-name:*-eth*;interface-name:hwsim*\n')


def sh(cmd, timeout=15):
    """(exit code, output) of a shell command."""
    try:
        r = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                           timeout=timeout)
        return r.returncode, r.stdout.decode('utf-8', 'replace').strip()
    except Exception as e:
        return 1, str(e)


def system_checks(sim_running=False):
    """[(level, what, fix)] - level: ok / warn / bad / info."""
    res = []
    add = res.append
    if is_root():
        add(('ok', 'Running as root (sudo)', ''))
    else:
        add(('warn', 'Not running as root', 'start it with: sudo python3 '
             'launcher.py (the simulation needs root)'))
    py = python_exe()
    code, out = sh('"%s" -c "import mn_wifi"' % py)
    add(('ok', 'Mininet-WiFi is installed for %s' % py, '')
        if code == 0 else
        ('bad', 'Mininet-WiFi cannot be imported by %s' % py,
         'install Mininet-WiFi (sudo util/install.sh -Wlnfv) or set the '
         'right python in the launcher'))
    code, _ = sh('"%s" -c "import PIL.ImageTk"' % py)
    add(('ok', 'Pillow is installed (pictures)', '') if code == 0 else
        ('info', 'Pillow is missing: pictures are not shown',
         'sudo apt install python3-pil python3-pil.imagetk'))
    for prog, lvl, why, fix in (
            ('ovs-vsctl', 'bad', 'Open vSwitch (access points, switches)',
             'sudo apt install openvswitch-switch'),
            ('hostapd', 'bad', 'hostapd (access points)',
             'sudo apt install hostapd'),
            ('iw', 'bad', 'iw (Wi-Fi configuration)', 'sudo apt install iw'),
            ('xterm', 'warn', 'xterm (device terminals, terminal windows)',
             'sudo apt install xterm'),
            ('iperf', 'info', 'iperf (traffic tests)',
             'sudo apt install iperf')):
        add(('ok', '%s found' % why, '') if shutil.which(prog) else
            (lvl, '%s not found' % why, fix))
    if shutil.which('controller') or shutil.which('ovs-testcontroller'):
        add(('ok', 'An OpenFlow controller program is installed', ''))
    else:
        add(('info', 'No reference controller program (controller / '
             'ovs-testcontroller)', 'use standalone APs (editor: Network > '
             'Auto-configure) or sudo apt install openvswitch-testcontroller'))
    code, out = sh('systemctl is-active openvswitch-switch 2>/dev/null')
    if out == 'active':
        add(('ok', 'Open vSwitch service is running', ''))
    elif shutil.which('systemctl'):
        add(('bad', 'Open vSwitch service is not running (%s)' % (
            out or 'unknown'), 'sudo systemctl start openvswitch-switch'))
    code, out = sh('systemctl is-active openvswitch-testcontroller 2>/dev/null')
    if out == 'active':
        add(('warn', 'openvswitch-testcontroller service holds port 6653',
             'sudo systemctl disable --now openvswitch-testcontroller'))
    code, _ = sh('modinfo mac80211_hwsim')
    add(('ok', 'Simulated radios available (mac80211_hwsim)', '') if code == 0
        else ('bad', 'Kernel module mac80211_hwsim not found',
              'sudo apt install linux-modules-extra-$(uname -r)'))
    code, out = sh('rfkill list 2>/dev/null | grep -c "Soft blocked: yes"')
    if out.isdigit() and int(out) > 0:
        add(('bad', '%s radio(s) blocked by rfkill' % out,
             'sudo rfkill unblock wifi  (and turn Wi-Fi on in Ubuntu)'))
    else:
        add(('ok', 'No radio blocked (rfkill)', ''))
    code, out = sh('systemctl is-active NetworkManager 2>/dev/null')
    if out == 'active':
        conf = os.path.isfile(NM_CONF)
        if not conf:
            _, grep = sh('grep -rl mac80211_hwsim /etc/NetworkManager 2>/dev/null')
            conf = bool(grep)
        add(('ok', 'NetworkManager ignores simulated radios', '') if conf else
            ('warn', 'NetworkManager (and its wpa_supplicant) may take the '
             'simulated radios: stations then never connect',
             'button "Fix NetworkManager" below, or Options > Stop '
             'NetworkManager during simulations'))
    else:
        add(('ok', 'NetworkManager is not running', ''))
    if not sim_running:
        _, out = sh("ip -o link show 2>/dev/null | grep -cE "
                    "'(ap|sta|car|s|h)[0-9]+-(wlan|eth|mp)[0-9]'")
        if out.isdigit() and int(out) > 0:
            add(('warn', '%s leftover interface(s) of an old simulation'
                 % out, 'button "Clean up (mn -c)" below'))
    return res


class SystemCheck(object):
    """Window: is this computer ready for Mininet-WiFi? With fixes."""

    def __init__(self, app):
        tk = app.tk
        self.app = app
        c = app.c
        top = self.top = tk.Toplevel(app.root)
        top.title('System check')
        top.configure(bg=c['bg'])
        top.geometry('700x520')
        tk.Label(top, text='Is this computer ready for Mininet-WiFi?',
                 bg=c['bg'], fg=c['text'],
                 font=('TkDefaultFont', 13, 'bold')).pack(anchor='w', padx=16,
                                                         pady=(14, 6))
        bar = tk.Frame(top, bg=c['bg'])
        bar.pack(side='bottom', fill='x', padx=12, pady=10)
        for text, fn, accent in (('Close', top.destroy, False),
                                 ('Check again', self.fill, True),
                                 ('Fix NetworkManager', self.fix_nm, False),
                                 ('Clean up (mn -c)', self.clean, False)):
            tk.Button(bar, text=text, command=fn, relief='flat',
                      bg=c['ap'] if accent else c['btn'],
                      fg='white' if accent else c['text'], padx=10,
                      cursor='hand2').pack(side='right', padx=3)
        self.txt = tk.Text(top, bg=c['tile'], fg=c['text'], relief='flat',
                           wrap='word', padx=12, pady=8, highlightthickness=0,
                           font=('TkDefaultFont', 10), spacing1=3)
        self.txt.pack(fill='both', expand=True, padx=12)
        cols = {'ok': c['rf'], 'warn': '#d08a17', 'bad': c['warn'],
                'info': c['ap']}
        for k, col in cols.items():
            self.txt.tag_configure(k, foreground=col,
                                   font=('TkDefaultFont', 9, 'bold'))
        self.txt.tag_configure('fix', foreground=c['muted'], lmargin1=78,
                               lmargin2=78)
        top.bind('<Escape>', lambda e: top.destroy())
        top.after(50, self.fill)

    def fill(self):
        t = self.txt
        t.config(state='normal')
        t.delete('1.0', 'end')
        t.insert('end', 'checking...\n')
        self.top.update_idletasks()
        res = system_checks(self.app.sim_running())
        t.delete('1.0', 'end')
        marks = {'ok': '  OK   ', 'warn': ' WARN  ', 'bad': ' FAIL  ',
                 'info': ' NOTE  '}
        for lvl, what, fix in res:
            t.insert('end', marks[lvl], lvl)
            t.insert('end', ' %s\n' % what)
            if fix:
                t.insert('end', 'fix: %s\n' % fix, 'fix')
        bad = sum(1 for r in res if r[0] == 'bad')
        warn = sum(1 for r in res if r[0] == 'warn')
        t.insert('end', '\n%s\n' % ('Ready to simulate.' if not bad and not warn
                                    else '%d problem%s, %d warning%s.' % (
                                        bad, '' if bad == 1 else 's', warn,
                                        '' if warn == 1 else 's')))
        t.config(state='disabled')

    def fix_nm(self):
        from tkinter import messagebox
        if not is_root():
            return messagebox.showinfo(
                'Fix NetworkManager', 'Start the launcher with sudo, or run '
                'in a terminal:\n\n' + "printf '%s' | sudo tee %s\nsudo "
                'systemctl restart NetworkManager' % (
                    NM_TEXT.replace('\n', '\\n'), NM_CONF), parent=self.top)
        if not messagebox.askokcancel(
                'Fix NetworkManager', 'Write %s so NetworkManager never '
                'touches simulated radios, and restart NetworkManager?\n\n'
                '(Your normal network reconnects in a few seconds.)'
                % NM_CONF, parent=self.top):
            return
        try:
            os.makedirs(os.path.dirname(NM_CONF), exist_ok=True)
            with open(NM_CONF, 'w') as fh:
                fh.write(NM_TEXT)
        except OSError as e:
            return messagebox.showerror('Fix NetworkManager', str(e),
                                        parent=self.top)
        self.app.run_bg('systemctl restart NetworkManager', lambda out:
                        self.fill())

    def clean(self):
        self.app.clean_now()
        self.app.root.after(4000, self.fill)


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