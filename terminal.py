"""Pestañas de terminal: paneles divisibles, búsqueda, zoom, registro,
barra de monitorización remota, Multi-exec y grabación de macros."""
import os
import shlex
import subprocess
import threading
import time

import gtkver  # noqa: F401
from gi.repository import Gdk, Gio, GLib, Gtk, Pango, Vte

from browser import FileBrowser
from core import (BROWSER_TYPES, LOGS_DIR, PALETTES, TYPE_ICONS, askpass_env, build_argv,
                  ssh_opts, ssh_target)

PCRE2_CASELESS = 0x00000008
PCRE2_MULTILINE = 0x00000400
CURSORS = {"block": Vte.CursorShape.BLOCK, "ibeam": Vte.CursorShape.IBEAM,
           "underline": Vte.CursorShape.UNDERLINE}

# Comando remoto de la barra de monitorización (solo POSIX básico)
MONITOR_CMD = (
    "LC_ALL=C; cat /proc/loadavg 2>/dev/null | cut -d' ' -f1; nproc 2>/dev/null || echo 1; "
    "free -m 2>/dev/null | awk 'NR==2{print $2, $3}'; "
    "df -P / 2>/dev/null | awk 'NR==2{print $5}'; "
    "awk '{print int($1)}' /proc/uptime 2>/dev/null; who 2>/dev/null | wc -l; "
    "awk 'NR>2 && $1 !~ /lo:/ {rx+=$2; tx+=$10} END{print rx, tx}' /proc/net/dev 2>/dev/null")


def rgba(c):
    r = Gdk.RGBA()
    r.parse(c)
    return r


class TermPane(Gtk.Box):
    """Un terminal VTE que ejecuta una sesión."""

    def __init__(self, tab, session):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.tab = tab
        self.app = tab.app
        self.session = session
        self.exited = False
        self.started = time.time()
        self.term = Vte.Terminal()
        self.term.set_scrollback_lines(self.app.settings["scrollback"])
        self.term.set_mouse_autohide(True)
        self.term.connect("child-exited", self.on_exit)
        self.term.connect("commit", self.on_commit)
        self.term.connect("button-press-event", self.on_click)
        self.term.connect("key-press-event", self.on_key)
        self.term.connect("selection-changed", self.on_selection)
        self.term.connect("focus-in-event", lambda *_: setattr(self.tab, "active", self))
        self.term.connect("window-title-changed", self.on_title)
        self.apply_settings()

        # barra de búsqueda
        self.search = Gtk.SearchBar(show_close_button=True)
        sbox = Gtk.Box(spacing=4)
        self.search_entry = Gtk.SearchEntry(placeholder_text="Buscar en la terminal")
        self.search_entry.connect("search-changed", self.on_search_changed)
        self.search_entry.connect("activate", lambda *_: self.term.search_find_next())
        self.case = Gtk.CheckButton(label="Mayúsc.")
        self.case.connect("toggled", self.on_search_changed)
        for icon, cb in (("go-up-symbolic", lambda *_: self.term.search_find_previous()),
                         ("go-down-symbolic", lambda *_: self.term.search_find_next())):
            b = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.MENU)
            b.connect("clicked", cb)
            sbox.pack_end(b, False, False, 0)
        sbox.pack_start(self.search_entry, True, True, 0)
        sbox.pack_start(self.case, False, False, 0)
        self.search.add(sbox)
        self.search.connect_entry(self.search_entry)
        self.pack_start(self.search, False, False, 0)

        sw = Gtk.ScrolledWindow()
        sw.add(self.term)
        self.pack_start(sw, True, True, 0)
        self.spawn()

    # -- ajustes
    def apply_settings(self):
        st = self.app.settings
        self.term.set_font(Pango.FontDescription.from_string(st["font"]))
        fg, bg, pal = PALETTES.get(st["palette"], PALETTES["omarchy"])
        self.term.set_colors(rgba(fg), rgba(bg), [rgba(c) for c in pal])
        self.term.set_cursor_shape(CURSORS.get(st.get("cursor"), Vte.CursorShape.BLOCK))
        self.term.set_audible_bell(bool(st.get("bell")))
        self.term.set_scrollback_lines(st["scrollback"])

    # -- proceso
    def spawn(self):
        argv, err = build_argv(self.session)
        if err:
            self.term.feed(f"\r\n\x1b[31m[MerlynGestorTerm] {err}\x1b[0m\r\n".encode())
            return
        shown = [("/p:****" if a.startswith("/p:") else a) for a in argv]
        self.term.feed(f"\x1b[2m[MerlynGestorTerm] $ {shlex.join(shown)}\x1b[0m\r\n".encode())
        extra_env = {"TERM": "xterm-256color", "COLORTERM": "truecolor"}
        if self.session["type"] in ("ssh", "sftp", "copyid", "mosh"):
            extra_env.update(askpass_env(self.session))
        env = [f"{k}={v}" for k, v in {**os.environ, **extra_env}.items()]
        self.started = time.time()
        self.term.spawn_async(Vte.PtyFlags.DEFAULT, os.path.expanduser("~"), argv, env,
                              GLib.SpawnFlags.SEARCH_PATH, None, None, -1, None, None)

    def on_exit(self, _term, status):
        code = os.waitstatus_to_exitcode(status) if status >= 0 else status
        self.term.feed(f"\r\n\x1b[33m[MerlynGestorTerm] Sesión terminada (código {code}). "
                       "Pulsa R para reconectar.\x1b[0m\r\n".encode())
        self.exited = True
        self.tab.update_label()
        if self.session.get("log") or self.app.settings.get("log_sessions"):
            self.save_log()

    def reconnect(self):
        self.exited = False
        self.term.reset(True, False)
        self.spawn()
        self.tab.update_label()

    def save_log(self, path=None):
        os.makedirs(LOGS_DIR, exist_ok=True)
        path = path or os.path.join(LOGS_DIR, "{}-{}.log".format(
            "".join(c if c.isalnum() else "_" for c in self.session["name"]),
            time.strftime("%Y%m%d-%H%M%S", time.localtime(self.started))))
        stream = Gio.File.new_for_path(path).replace(None, False, Gio.FileCreateFlags.NONE, None)
        self.term.write_contents_sync(stream, Vte.WriteFlags.DEFAULT, None)
        stream.close(None)
        return path

    # -- eventos
    def on_title(self, term):
        if self.session["type"] == "shell" and term.get_window_title():
            self.tab.update_label(term.get_window_title())

    def on_selection(self, term):
        if self.app.settings.get("copy_on_select") and term.get_has_selection():
            term.copy_clipboard_format(Vte.Format.TEXT)

    def on_key(self, _w, ev):
        if self.exited and ev.keyval in (Gdk.KEY_r, Gdk.KEY_R):
            self.reconnect()
            return True
        ctrl_shift = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK
        if ev.state & ctrl_shift == ctrl_shift:
            k = Gdk.keyval_to_lower(ev.keyval)
            if k == Gdk.KEY_c:
                self.term.copy_clipboard_format(Vte.Format.TEXT)
                return True
            if k == Gdk.KEY_v:
                self.term.paste_clipboard()
                return True
            if k == Gdk.KEY_f:
                self.search.set_search_mode(True)
                return True
        if ev.state & Gdk.ModifierType.CONTROL_MASK:
            if ev.keyval in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
                self.zoom(1.1)
                return True
            if ev.keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
                self.zoom(1 / 1.1)
                return True
            if ev.keyval == Gdk.KEY_0:
                self.term.set_font_scale(1.0)
                return True
        return False

    def zoom(self, factor):
        self.term.set_font_scale(max(0.4, min(4.0, self.term.get_font_scale() * factor)))

    def on_search_changed(self, *_):
        text = self.search_entry.get_text()
        if not text:
            self.term.search_set_regex(None, 0)
            return
        flags = PCRE2_MULTILINE | (0 if self.case.get_active() else PCRE2_CASELESS)
        try:
            regex = Vte.Regex.new_for_search(GLib.regex_escape_string(text, -1), -1, flags)
        except GLib.Error:
            return
        self.term.search_set_regex(regex, 0)
        self.term.search_set_wrap_around(True)
        self.term.search_find_previous()

    def on_commit(self, _term, text, _size):
        app = self.app
        if app.macro_recording is not None:
            app.macro_recording.append(text)
        if app.multiexec.get_active() and not app.broadcasting:
            app.broadcasting = True
            try:
                for pane in app.all_panes():
                    if pane is not self and not pane.exited:
                        pane.term.feed_child(text.encode())
            finally:
                app.broadcasting = False

    def on_click(self, _w, ev):
        if ev.button == 3:
            if self.app.settings.get("right_click_paste"):
                self.term.paste_clipboard()
                return True
            menu = Gtk.Menu()
            items = [("Copiar", lambda *_: self.term.copy_clipboard_format(Vte.Format.TEXT)),
                     ("Pegar", lambda *_: self.term.paste_clipboard()),
                     ("Seleccionar todo", lambda *_: self.term.select_all()),
                     None,
                     ("Buscar…", lambda *_: self.search.set_search_mode(True)),
                     ("Limpiar pantalla", lambda *_: self.term.reset(True, True)),
                     ("Guardar salida en archivo…", lambda *_: self.app.save_output(self)),
                     None,
                     ("Dividir en vertical", lambda *_: self.tab.split(Gtk.Orientation.HORIZONTAL)),
                     ("Dividir en horizontal", lambda *_: self.tab.split(Gtk.Orientation.VERTICAL)),
                     ("Cerrar este panel", lambda *_: self.tab.close_pane(self))]
            if self.exited:
                items.insert(0, ("Reconectar", lambda *_: self.reconnect()))
            for item in items:
                if item is None:
                    menu.append(Gtk.SeparatorMenuItem())
                    continue
                mi = Gtk.MenuItem(label=item[0])
                mi.connect("activate", item[1])
                menu.append(mi)
            menu.show_all()
            menu.popup_at_pointer(ev)
            return True
        if ev.button == 2:
            self.term.paste_primary()
            return True
        return False


class MonitorBar(Gtk.Box):
    """Barra inferior con el estado del servidor remoto (vía la conexión SSH)."""

    def __init__(self, session):
        super().__init__(spacing=14)
        self.get_style_context().add_class("nx-monitor")
        self.s = session
        self.closed = False
        self.prev = None
        self.labels = {}
        for key, icon in (("host", "network-server-symbolic"), ("cpu", "utilities-system-monitor-symbolic"),
                          ("mem", "drive-harddisk-solidstate-symbolic"), ("disk", "drive-harddisk-symbolic"),
                          ("net", "network-transmit-receive-symbolic"), ("up", "alarm-symbolic"),
                          ("users", "system-users-symbolic")):
            box = Gtk.Box(spacing=4)
            box.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.MENU), False, False, 0)
            lab = Gtk.Label(label="…")
            box.pack_start(lab, False, False, 0)
            self.pack_start(box, False, False, 0)
            self.labels[key] = lab
        self.labels["host"].set_text(ssh_target(session))
        self.connect("destroy", lambda *_: setattr(self, "closed", True))
        GLib.timeout_add_seconds(3, self.tick)

    def tick(self):
        if self.closed:
            return False
        threading.Thread(target=self.poll, daemon=True).start()
        return True

    def poll(self):
        cmd = ["ssh"] + ssh_opts(self.s, "-p") + ["-o", "ControlMaster=no", "-o", "BatchMode=yes",
                                                   ssh_target(self.s), MONITOR_CMD]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            return
        if p.returncode == 0:
            GLib.idle_add(self.show, p.stdout.split("\n"), time.time())

    def show(self, lines, now):
        try:
            load, cpus = float(lines[0]), int(lines[1])
            total, used = (int(x) for x in lines[2].split())
            disk, uptime, users = lines[3], int(lines[4]), lines[5].strip()
            rx, tx = (int(x) for x in lines[6].split())
        except (ValueError, IndexError):
            return False
        self.labels["cpu"].set_text(f"CPU {min(100, load / max(cpus, 1) * 100):.0f}%")
        self.labels["mem"].set_text(f"RAM {used / 1024:.1f}/{total / 1024:.1f} GB")
        self.labels["disk"].set_text(f"Disco / {disk}")
        d, h, m = uptime // 86400, uptime % 86400 // 3600, uptime % 3600 // 60
        self.labels["up"].set_text(f"Activo {d}d {h}h {m}m" if d else f"Activo {h}h {m}m")
        self.labels["users"].set_text(f"{users} usuarios")
        if self.prev:
            dt = max(now - self.prev[2], 0.1)
            self.labels["net"].set_text(f"↓ {(rx - self.prev[0]) / dt / 1024:.0f} KB/s  "
                                        f"↑ {(tx - self.prev[1]) / dt / 1024:.0f} KB/s")
        self.prev = (rx, tx, now)
        return False


class TermTab(Gtk.Box):
    """Contenido de una pestaña: uno o varios TermPane, o un explorador de archivos."""

    def __init__(self, app, session):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.session = session
        self.browser = None
        self.panes = []
        self.active = None
        self.label = Gtk.Label(label=session["name"])
        self.icon = Gtk.Image.new_from_icon_name(TYPE_ICONS.get(session["type"], "utilities-terminal"),
                                                 Gtk.IconSize.MENU)
        t = session["type"]
        if t in BROWSER_TYPES:
            self.browser = FileBrowser(app, session)
            self.pack_start(self.browser, True, True, 0)
            return
        if t in ("ssh", "sftp") and session.get("host") and session.get("sftp", True) \
                and app.settings.get("sftp_browser"):
            self.browser = FileBrowser(app, session)
        self.area = Gtk.Box()
        self.pack_start(self.area, True, True, 0)
        first = TermPane(self, session)
        self.panes.append(first)
        self.active = first
        self.area.pack_start(first, True, True, 0)
        if t == "ssh" and session.get("host") and session.get("monitoring", True) \
                and app.settings.get("remote_monitoring"):
            self.pack_end(MonitorBar(session), False, False, 0)

    def update_label(self, title=None):
        dead = all(p.exited for p in self.panes) if self.panes else False
        text = GLib.markup_escape_text(title or self.session["name"])
        self.label.set_markup(f"<i>{text}</i>" if dead else text)

    def focus(self):
        if self.active:
            self.active.term.grab_focus()

    def split(self, orientation, session=None):
        """Divide el panel activo en dos (abre la misma sesión o la indicada)."""
        target = self.active or (self.panes[-1] if self.panes else None)
        if not target:
            return
        new = TermPane(self, dict(session or self.session))
        parent = target.get_parent()
        paned = Gtk.Paned(orientation=orientation)
        if isinstance(parent, Gtk.Paned):
            first = parent.get_child1() is target
            parent.remove(target)
            parent.pack1(paned, True, False) if first else parent.pack2(paned, True, False)
        else:
            parent.remove(target)
            parent.pack_start(paned, True, True, 0)
        paned.pack1(target, True, False)
        paned.pack2(new, True, False)
        paned.show_all()
        alloc = paned.get_allocation()
        size = alloc.width if orientation == Gtk.Orientation.HORIZONTAL else alloc.height
        if size > 1:
            paned.set_position(size // 2)
        else:
            GLib.idle_add(lambda: paned.set_position(
                (paned.get_allocated_width() if orientation == Gtk.Orientation.HORIZONTAL
                 else paned.get_allocated_height()) // 2) and False)
        self.panes.append(new)
        self.active = new
        new.term.grab_focus()

    def split4(self):
        self.unsplit()
        self.split(Gtk.Orientation.HORIZONTAL)
        left = self.panes[0]
        self.active = left
        self.split(Gtk.Orientation.VERTICAL)
        self.active = self.panes[1]
        self.split(Gtk.Orientation.VERTICAL)

    def close_pane(self, pane):
        if len(self.panes) == 1:
            self.app.close_tab(self)
            return
        parent = pane.get_parent()
        sibling = parent.get_child2() if parent.get_child1() is pane else parent.get_child1()
        parent.remove(pane)
        parent.remove(sibling)
        grand = parent.get_parent()
        if isinstance(grand, Gtk.Paned):
            first = grand.get_child1() is parent
            grand.remove(parent)
            grand.pack1(sibling, True, False) if first else grand.pack2(sibling, True, False)
        else:
            grand.remove(parent)
            grand.pack_start(sibling, True, True, 0)
        pane.destroy()
        self.panes.remove(pane)
        self.active = self.panes[0]
        self.focus()

    def unsplit(self):
        for pane in list(self.panes[1:]):
            self.close_pane(pane)
