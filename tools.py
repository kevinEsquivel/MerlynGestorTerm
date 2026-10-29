"""Herramientas de MerlynGestorTerm (equivalentes a las de MobaXterm, hechas para Linux)."""
import concurrent.futures
import difflib
import filecmp
import ipaddress
import math
import os
import random
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
import uuid

import gtkver  # noqa: F401
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

try:
    from gi.repository import GtkSource
except ImportError:  # sin GtkSourceView 4: editor externo y comparador simple
    GtkSource = None

from core import (KEYS_DIR, MACROS_FILE, TUNNELS_FILE, WOL_FILE, askpass_env, load_json,  # noqa: E402
                  new_session, save_json, secret_get, secret_set, ssh_opts, ssh_target)


# ---------------------------------------------------------------- utilidades

def tool_window(app, title, w=760, h=520):
    win = Gtk.Window(title=title, transient_for=app.window, destroy_with_parent=True)
    win.set_default_size(w, h)
    win.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
    return win


def list_view(columns, types=None):
    store = Gtk.ListStore(*(types or [str] * len(columns)))
    view = Gtk.TreeView(model=store)
    for i, title in enumerate(columns):
        c = Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=i)
        c.set_resizable(True)
        c.set_sort_column_id(i)
        view.append_column(c)
    sw = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
    sw.add(view)
    return store, view, sw


def button(label, icon=None, cb=None):
    b = Gtk.Button(label=label)
    if icon:
        b.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))
        b.set_always_show_image(True)
    if cb:
        b.connect("clicked", cb)
    return b


def message(parent, text, secondary=None, kind=Gtk.MessageType.INFO):
    d = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=kind,
                          buttons=Gtk.ButtonsType.OK, text=text, secondary_text=secondary)
    d.run()
    d.destroy()


def bg(work, done):
    """Ejecuta work() en un hilo y done(resultado) en el hilo de GTK."""
    def target():
        try:
            res = work()
        except Exception as e:  # noqa: BLE001
            res = e
        GLib.idle_add(lambda: done(res) and False)
    threading.Thread(target=target, daemon=True).start()


def local_networks():
    out = subprocess.run(["ip", "-o", "-4", "addr", "show", "scope", "global"],
                         capture_output=True, text=True).stdout
    nets = []
    for line in out.splitlines():
        m = re.search(r"^\d+:\s+(\S+)\s+inet\s+(\S+)", line)
        if m:
            net = ipaddress.ip_interface(m.group(2)).network
            if net.prefixlen < 32:
                nets.append((m.group(1), str(net if net.prefixlen >= 22 else
                                             ipaddress.ip_interface(m.group(2)).ip.exploded + "/24")))
    return nets


_OUI = None


def mac_vendor(mac):
    global _OUI
    if _OUI is None:
        _OUI = {}
        try:
            with open("/usr/share/hwdata/oui.txt", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "(base 16)" in line:
                        pre, _, vendor = line.partition("(base 16)")
                        _OUI[pre.strip().upper()] = vendor.strip()
        except OSError:
            pass
    return _OUI.get(mac.replace(":", "").upper()[:6], "")


# ---------------------------------------------------------------- editor

class TextEditor(Gtk.Window):
    """Editor con resaltado de sintaxis (GtkSourceView). Se usa también para
    editar archivos remotos desde el explorador SFTP/FTP."""
    instance = None

    @classmethod
    def open_file(cls, app, path=None, on_saved=None, title=None):
        if GtkSource is None:
            if path:
                Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(path), None)
            else:
                message(app.window, "Editor no disponible", "Instala GtkSourceView 4 "
                        "(ejecuta install-deps.sh) para usar el editor integrado.")
            return None
        if not cls.instance:
            cls.instance = cls(app)
        cls.instance.add_doc(path, on_saved, title)
        cls.instance.present()
        return cls.instance

    def __init__(self, app):
        super().__init__(title="Editor de texto — MerlynGestorTerm", transient_for=app.window)
        self.app = app
        self.set_default_size(900, 640)
        self.connect("destroy", lambda *_: setattr(TextEditor, "instance", None))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(box)
        bar = Gtk.Toolbar()
        bar.get_style_context().add_class("primary-toolbar")
        for icon, tip, cb in (("document-new", "Nuevo", lambda *_: self.add_doc()),
                              ("document-open", "Abrir", self.on_open),
                              ("document-save", "Guardar (Ctrl+S)", lambda *_: self.save()),
                              ("document-save-as", "Guardar como", lambda *_: self.save(as_new=True)),
                              ("edit-undo", "Deshacer", lambda *_: self.cur() and self.cur()[0].undo()),
                              ("edit-redo", "Rehacer", lambda *_: self.cur() and self.cur()[0].redo()),
                              ("edit-find", "Buscar (Ctrl+F)", lambda *_: self.toggle_find())):
            b = Gtk.ToolButton(icon_name=icon, label=tip)
            b.set_tooltip_text(tip)
            b.connect("clicked", cb)
            bar.insert(b, -1)
        box.pack_start(bar, False, False, 0)
        self.findbar = Gtk.SearchBar(show_close_button=True)
        fb = Gtk.Box(spacing=4)
        self.find_entry = Gtk.SearchEntry(placeholder_text="Buscar")
        self.find_entry.connect("search-changed", self.on_find)
        self.find_entry.connect("activate", lambda *_: self.find_next())
        self.repl_entry = Gtk.Entry(placeholder_text="Reemplazar por")
        fb.pack_start(self.find_entry, True, True, 0)
        fb.pack_start(button("Siguiente", cb=lambda *_: self.find_next()), False, False, 0)
        fb.pack_start(self.repl_entry, True, True, 0)
        fb.pack_start(button("Reemplazar todo", cb=self.replace_all), False, False, 0)
        self.findbar.add(fb)
        box.pack_start(self.findbar, False, False, 0)
        self.nb = Gtk.Notebook(scrollable=True)
        box.pack_start(self.nb, True, True, 0)
        self.statusbar = Gtk.Label(xalign=0, margin=4)
        box.pack_start(self.statusbar, False, False, 0)
        accel = Gtk.AccelGroup()
        self.add_accel_group(accel)
        for key, cb in (("<Control>s", lambda: self.save()), ("<Control>f", self.toggle_find),
                        ("<Control>o", self.on_open), ("<Control>n", self.add_doc),
                        ("<Control>w", self.close_cur)):
            k, m = Gtk.accelerator_parse(key)
            accel.connect(k, m, 0, lambda *a, cb=cb: (cb(), True)[1])
        self.show_all()

    def cur(self):
        page = self.nb.get_nth_page(self.nb.get_current_page())
        return page.doc if page else None

    def add_doc(self, path=None, on_saved=None, title=None):
        buf = GtkSource.Buffer()
        scheme = GtkSource.StyleSchemeManager.get_default().get_scheme("tango")
        if scheme:
            buf.set_style_scheme(scheme)
        view = GtkSource.View(buffer=buf, show_line_numbers=True, auto_indent=True,
                              highlight_current_line=True, tab_width=4, monospace=True,
                              show_right_margin=True)
        view.modify_font(Pango.FontDescription.from_string(self.app.settings["font"]))
        if path and os.path.exists(path):
            with open(path, "rb") as f:
                data = f.read()
            buf.begin_not_undoable_action()
            buf.set_text(data.decode("utf-8", errors="replace"))
            buf.end_not_undoable_action()
            buf.place_cursor(buf.get_start_iter())
            lang = GtkSource.LanguageManager.get_default().guess_language(path, None)
            if lang:
                buf.set_language(lang)
        buf.set_modified(False)
        sw = Gtk.ScrolledWindow()
        sw.add(view)
        label = Gtk.Label(label=title or (os.path.basename(path) if path else "Sin título"))
        sw.doc = [buf, path, on_saved, label, title, None]  # ..., contexto de búsqueda
        buf.connect("modified-changed", lambda b: label.set_text(
            ("● " if b.get_modified() else "") + (sw.doc[4] or os.path.basename(sw.doc[1] or "Sin título"))))
        buf.connect("notify::cursor-position", lambda b, _p: self.statusbar.set_text(
            "Línea {}, columna {}".format(b.get_iter_at_mark(b.get_insert()).get_line() + 1,
                                          b.get_iter_at_mark(b.get_insert()).get_line_offset() + 1)))
        head = Gtk.Box(spacing=4)
        head.pack_start(label, True, True, 0)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.connect("clicked", lambda *_: self.close_page(sw))
        head.pack_start(close, False, False, 0)
        head.show_all()
        idx = self.nb.append_page(sw, head)
        sw.show_all()
        self.nb.set_current_page(idx)
        view.grab_focus()

    def on_open(self, *_):
        dlg = Gtk.FileChooserNative(title="Abrir", transient_for=self, action=Gtk.FileChooserAction.OPEN)
        if dlg.run() == Gtk.ResponseType.ACCEPT:
            self.add_doc(dlg.get_filename())
        dlg.destroy()

    def save(self, as_new=False):
        doc = self.cur()
        if not doc:
            return
        buf, path = doc[0], doc[1]
        if as_new or not path:
            dlg = Gtk.FileChooserNative(title="Guardar como", transient_for=self,
                                        action=Gtk.FileChooserAction.SAVE, do_overwrite_confirmation=True)
            ok = dlg.run() == Gtk.ResponseType.ACCEPT
            path = dlg.get_filename() if ok else None
            dlg.destroy()
            if not path:
                return
            doc[1], doc[2], doc[4] = path, None, None
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        buf.set_modified(False)
        self.statusbar.set_text(f"Guardado {path}")
        if doc[2]:
            doc[2](path)
            self.statusbar.set_text("Guardado y enviado al servidor")

    def close_page(self, page):
        buf = page.doc[0]
        if buf.get_modified():
            d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                  buttons=Gtk.ButtonsType.YES_NO, text="Hay cambios sin guardar. ¿Cerrar igualmente?")
            ok = d.run() == Gtk.ResponseType.YES
            d.destroy()
            if not ok:
                return
        self.nb.remove_page(self.nb.page_num(page))
        if not self.nb.get_n_pages():
            self.destroy()

    def close_cur(self):
        page = self.nb.get_nth_page(self.nb.get_current_page())
        if page:
            self.close_page(page)

    def toggle_find(self, *_):
        self.findbar.set_search_mode(not self.findbar.get_search_mode())

    def search_ctx(self):
        doc = self.cur()
        if not doc:
            return None
        settings = GtkSource.SearchSettings(search_text=self.find_entry.get_text() or None,
                                            case_sensitive=False, wrap_around=True)
        doc[5] = GtkSource.SearchContext(buffer=doc[0], settings=settings)
        return doc[5]

    def on_find(self, *_):
        self.search_ctx()
        self.find_next()

    def find_next(self):
        doc = self.cur()
        if not doc or not doc[5]:
            return
        buf = doc[0]
        it = buf.get_iter_at_mark(buf.get_insert())
        it.forward_char()
        found, start, end, _wrap = doc[5].forward(it)
        if found:
            buf.select_range(start, end)
            page = self.nb.get_nth_page(self.nb.get_current_page())
            page.get_child().scroll_to_iter(start, 0.2, False, 0, 0)

    def replace_all(self, *_):
        ctx = self.search_ctx()
        if ctx:
            n = ctx.replace_all(self.repl_entry.get_text(), -1)
            self.statusbar.set_text(f"{n} reemplazos")


# ---------------------------------------------------------------- comparador

class DiffTool:
    def __init__(self, app):
        self.app = app
        win = self.win = tool_window(app, "Comparar archivos y carpetas", 1000, 650)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        win.add(box)
        grid = Gtk.Grid(column_spacing=6, row_spacing=6)
        self.a = Gtk.FileChooserButton(title="Izquierda", action=Gtk.FileChooserAction.OPEN)
        self.b = Gtk.FileChooserButton(title="Derecha", action=Gtk.FileChooserAction.OPEN)
        self.mode = Gtk.ComboBoxText()
        self.mode.append("file", "Archivos")
        self.mode.append("dir", "Carpetas")
        self.mode.set_active_id("file")
        self.mode.connect("changed", self.on_mode)
        grid.attach(Gtk.Label(label="Comparar"), 0, 0, 1, 1)
        grid.attach(self.mode, 1, 0, 1, 1)
        grid.attach(self.a, 2, 0, 1, 1)
        grid.attach(self.b, 3, 0, 1, 1)
        grid.attach(button("Comparar", "edit-find", self.run), 4, 0, 1, 1)
        self.a.set_hexpand(True)
        self.b.set_hexpand(True)
        box.pack_start(grid, False, False, 0)
        if GtkSource:
            self.buf = GtkSource.Buffer(language=GtkSource.LanguageManager.get_default().get_language("diff"))
            view = GtkSource.View(buffer=self.buf, editable=False, monospace=True, show_line_numbers=True)
        else:
            self.buf = Gtk.TextBuffer()
            view = Gtk.TextView(buffer=self.buf, editable=False, monospace=True)
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.add(view)
        box.pack_start(sw, True, True, 0)
        self.summary = Gtk.Label(xalign=0)
        box.pack_start(self.summary, False, False, 0)
        win.show_all()

    def on_mode(self, combo):
        action = Gtk.FileChooserAction.SELECT_FOLDER if combo.get_active_id() == "dir" else \
            Gtk.FileChooserAction.OPEN
        self.a.set_action(action)
        self.b.set_action(action)

    def run(self, *_):
        a, b = self.a.get_filename(), self.b.get_filename()
        if not a or not b:
            return
        if self.mode.get_active_id() == "file":
            def read(p):
                with open(p, encoding="utf-8", errors="replace") as f:
                    return f.readlines()
            diff = list(difflib.unified_diff(read(a), read(b), a, b))
            self.buf.set_text("".join(diff) or "Los archivos son idénticos.\n")
            adds = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
            dels = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
            self.summary.set_text(f"+{adds} líneas añadidas, -{dels} eliminadas")
            return
        lines, counts = [], [0, 0, 0]

        def walk(cmp, rel=""):
            for n in sorted(cmp.left_only):
                lines.append(f"- solo en izquierda: {os.path.join(rel, n)}\n")
                counts[0] += 1
            for n in sorted(cmp.right_only):
                lines.append(f"+ solo en derecha:   {os.path.join(rel, n)}\n")
                counts[1] += 1
            for n in sorted(cmp.diff_files):
                lines.append(f"! distinto:          {os.path.join(rel, n)}\n")
                counts[2] += 1
            for n, sub in sorted(cmp.subdirs.items()):
                walk(sub, os.path.join(rel, n))
        walk(filecmp.dircmp(a, b))
        self.buf.set_text("".join(lines) or "Las carpetas son idénticas.\n")
        self.summary.set_text(f"{counts[0]} solo a la izquierda · {counts[1]} solo a la derecha · "
                              f"{counts[2]} distintos")


# ---------------------------------------------------------------- red

COMMON_PORTS = {21: "ftp", 22: "ssh", 23: "telnet", 80: "http", 443: "https", 445: "smb",
                3389: "rdp", 5900: "vnc", 8080: "http-alt"}


def tcp_open(host, port, timeout=0.6):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class NetworkScanner:
    """Barrido de la red local: ping + puertos habituales + DNS inverso + MAC/fabricante."""

    def __init__(self, app):
        self.app = app
        self.stop = threading.Event()
        win = self.win = tool_window(app, "Escáner de red", 980, 560)
        win.connect("destroy", lambda *_: self.stop.set())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        win.add(box)
        top = Gtk.Box(spacing=6)
        self.net = Gtk.ComboBoxText.new_with_entry()
        for iface, net in local_networks():
            self.net.append(net, f"{net}")
        self.net.set_active(0)
        top.pack_start(Gtk.Label(label="Red"), False, False, 0)
        top.pack_start(self.net, True, True, 0)
        self.go_btn = button("Escanear", "system-search", self.start)
        top.pack_start(self.go_btn, False, False, 0)
        top.pack_start(button("Detener", "process-stop", lambda *_: self.stop.set()), False, False, 0)
        box.pack_start(top, False, False, 0)
        cols = ["IP", "Nombre", "MAC", "Fabricante"] + [v.upper() for v in COMMON_PORTS.values()]
        self.store, self.view, sw = list_view(cols)
        self.view.connect("row-activated", self.on_row)
        box.pack_start(sw, True, True, 0)
        self.progress = Gtk.ProgressBar(show_text=True)
        box.pack_start(self.progress, False, False, 0)
        hint = Gtk.Label(label="Doble clic en un equipo para abrir SSH/RDP/VNC según los puertos abiertos.",
                         xalign=0)
        hint.get_style_context().add_class("dim-label")
        box.pack_start(hint, False, False, 0)
        win.show_all()

    def start(self, *_):
        try:
            net = ipaddress.ip_network(self.net.get_child().get_text().strip(), strict=False)
        except ValueError as e:
            message(self.win, "Red no válida", str(e), Gtk.MessageType.ERROR)
            return
        hosts = list(net.hosts())
        if len(hosts) > 4096:
            message(self.win, "Red demasiado grande (máximo /20).")
            return
        self.store.clear()
        self.stop.clear()
        self.go_btn.set_sensitive(False)
        total, done = len(hosts), [0]

        def probe(ip):
            if self.stop.is_set():
                return None
            ip = str(ip)
            alive = subprocess.run(["ping", "-c", "1", "-W", "1", ip], capture_output=True).returncode == 0
            # también se prueban los puertos de los que no responden a ping (cortafuegos)
            ports = {p: tcp_open(ip, p, 0.4) for p in COMMON_PORTS}
            if not alive and not any(ports.values()):
                return None
            try:
                name = socket.gethostbyaddr(ip)[0]
            except OSError:
                name = ""
            return ip, name, ports

        def worker():
            with concurrent.futures.ThreadPoolExecutor(64) as ex:
                for res in ex.map(probe, hosts):
                    done[0] += 1
                    GLib.idle_add(self.add, res, done[0], total)
            GLib.idle_add(self.finish)
        threading.Thread(target=worker, daemon=True).start()

    def add(self, res, n, total):
        self.progress.set_fraction(n / total)
        self.progress.set_text(f"{n}/{total}")
        if res:
            ip, name, ports = res
            neigh = subprocess.run(["ip", "neigh", "show", ip], capture_output=True, text=True).stdout.split()
            mac = neigh[neigh.index("lladdr") + 1] if "lladdr" in neigh else ""
            self.store.append([ip, name, mac, mac_vendor(mac) if mac else ""] +
                              ["✔" if ports.get(p) else "" for p in COMMON_PORTS])
        return False

    def finish(self):
        self.go_btn.set_sensitive(True)
        self.progress.set_text(f"Terminado: {len(self.store)} equipos encontrados")
        return False

    def on_row(self, view, path, _col):
        row = list(view.get_model()[path])
        open_ports = [p for p, v in zip(COMMON_PORTS, row[4:]) if v]
        for port, t in ((22, "ssh"), (3389, "rdp"), (5900, "vnc"), (23, "telnet"), (21, "ftp")):
            if port in open_ports:
                self.app.open_session(new_session(name=row[1] or row[0], type=t, host=row[0], port=port))
                return


class PortScanner:
    def __init__(self, app):
        self.app = app
        self.stop = threading.Event()
        win = self.win = tool_window(app, "Escáner de puertos", 560, 520)
        win.connect("destroy", lambda *_: self.stop.set())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        win.add(box)
        grid = Gtk.Grid(column_spacing=6, row_spacing=6)
        self.host = Gtk.Entry(placeholder_text="host o IP", hexpand=True)
        self.ports = Gtk.Entry(text="1-1024,3306,3389,5432,5900,6379,8080,8443,27017")
        grid.attach(Gtk.Label(label="Host", xalign=1), 0, 0, 1, 1)
        grid.attach(self.host, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="Puertos", xalign=1), 0, 1, 1, 1)
        grid.attach(self.ports, 1, 1, 1, 1)
        self.btn = button("Escanear", "system-search", self.start)
        grid.attach(self.btn, 2, 0, 1, 2)
        box.pack_start(grid, False, False, 0)
        self.store, _v, sw = list_view(["Puerto", "Estado", "Servicio"])
        box.pack_start(sw, True, True, 0)
        self.progress = Gtk.ProgressBar(show_text=True)
        box.pack_start(self.progress, False, False, 0)
        win.show_all()

    @staticmethod
    def parse(spec):
        ports = set()
        for part in spec.replace(" ", "").split(","):
            if "-" in part:
                a, b = part.split("-", 1)
                ports.update(range(int(a), int(b) + 1))
            elif part:
                ports.add(int(part))
        return sorted(p for p in ports if 0 < p < 65536)

    def start(self, *_):
        host = self.host.get_text().strip()
        try:
            ports = self.parse(self.ports.get_text())
            addr = socket.gethostbyname(host)
        except (ValueError, OSError) as e:
            message(self.win, "Datos no válidos", str(e), Gtk.MessageType.ERROR)
            return
        self.store.clear()
        self.stop.clear()
        self.btn.set_sensitive(False)
        done = [0]

        def probe(p):
            return None if self.stop.is_set() else (p, tcp_open(addr, p, 0.8))

        def worker():
            with concurrent.futures.ThreadPoolExecutor(200) as ex:
                for res in ex.map(probe, ports):
                    done[0] += 1
                    GLib.idle_add(self.add, res, done[0], len(ports))
            GLib.idle_add(lambda: self.btn.set_sensitive(True) or self.progress.set_text(
                f"Terminado: {len(self.store)} abiertos") or False)
        threading.Thread(target=worker, daemon=True).start()

    def add(self, res, n, total):
        self.progress.set_fraction(n / total)
        if res and res[1]:
            try:
                svc = socket.getservbyport(res[0], "tcp")
            except OSError:
                svc = ""
            self.store.append([str(res[0]), "abierto", svc])
        return False


class OpenPorts:
    """Equivalente a 'List open ports' / netstat: usa ss."""

    def __init__(self, app):
        win = self.win = tool_window(app, "Puertos abiertos y conexiones", 980, 560)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        win.add(box)
        top = Gtk.Box(spacing=6)
        top.pack_start(button("Actualizar", "view-refresh", lambda *_: self.refresh()), False, False, 0)
        self.auto = Gtk.CheckButton(label="Auto (2 s)")
        top.pack_start(self.auto, False, False, 0)
        self.filter = Gtk.SearchEntry(placeholder_text="Filtrar")
        self.filter.connect("search-changed", lambda *_: self.refresh())
        top.pack_end(self.filter, False, False, 0)
        box.pack_start(top, False, False, 0)
        nb = Gtk.Notebook()
        cols = ["Proto", "Estado", "Local", "Remoto", "Proceso"]
        self.listen, _v, sw1 = list_view(cols)
        self.conns, _v, sw2 = list_view(cols)
        nb.append_page(sw1, Gtk.Label(label="En escucha"))
        nb.append_page(sw2, Gtk.Label(label="Conexiones"))
        box.pack_start(nb, True, True, 0)
        note = Gtk.Label(label="Los procesos de otros usuarios solo se ven con privilegios de root.", xalign=0)
        note.get_style_context().add_class("dim-label")
        box.pack_start(note, False, False, 0)
        self.alive = True
        win.connect("destroy", lambda *_: setattr(self, "alive", False))
        GLib.timeout_add_seconds(2, lambda: self.alive and (not self.auto.get_active() or self.refresh() or True))
        win.show_all()
        self.refresh()

    def refresh(self):
        q = self.filter.get_text().lower()
        for store, args in ((self.listen, "-tulpnH"), (self.conns, "-tunapH")):
            out = subprocess.run(["ss", args], capture_output=True, text=True).stdout
            store.clear()
            for line in out.splitlines():
                parts = line.split(None, 6)
                if len(parts) < 6 or (store is self.conns and parts[1] in ("LISTEN", "UNCONN")):
                    continue
                proc = parts[6] if len(parts) > 6 else ""
                m = re.search(r'"([^"]+)",pid=(\d+)', proc)
                proc = f"{m.group(1)} ({m.group(2)})" if m else ""
                row = [parts[0], parts[1], parts[4], parts[5], proc]
                if not q or q in " ".join(row).lower():
                    store.append(row)
        return False


class WakeOnLan:
    def __init__(self, app):
        win = self.win = tool_window(app, "Wake on LAN", 520, 400)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        win.add(box)
        grid = Gtk.Grid(column_spacing=6, row_spacing=6)
        self.name = Gtk.Entry(placeholder_text="Nombre (opcional)", hexpand=True)
        self.mac = Gtk.Entry(placeholder_text="AA:BB:CC:DD:EE:FF")
        self.bcast = Gtk.Entry(text="255.255.255.255")
        self.port = Gtk.SpinButton.new_with_range(1, 65535, 1)
        self.port.set_value(9)
        for i, (lab, w) in enumerate((("Nombre", self.name), ("MAC", self.mac), ("Broadcast", self.bcast),
                                      ("Puerto UDP", self.port))):
            grid.attach(Gtk.Label(label=lab, xalign=1), 0, i, 1, 1)
            grid.attach(w, 1, i, 1, 1)
        box.pack_start(grid, False, False, 0)
        row = Gtk.Box(spacing=6)
        row.pack_start(button("Despertar", "system-run", self.wake), False, False, 0)
        row.pack_start(button("Guardar", "document-save", self.save), False, False, 0)
        row.pack_start(button("Borrar", "edit-delete", self.delete), False, False, 0)
        box.pack_start(row, False, False, 0)
        self.store, self.view, sw = list_view(["Nombre", "MAC", "Broadcast", "Puerto"])
        self.view.get_selection().connect("changed", self.on_sel)
        box.pack_start(sw, True, True, 0)
        self.status = Gtk.Label(xalign=0)
        box.pack_start(self.status, False, False, 0)
        self.items = load_json(WOL_FILE, [])
        self.reload()
        win.show_all()

    def reload(self):
        self.store.clear()
        for it in self.items:
            self.store.append([it["name"], it["mac"], it["bcast"], str(it["port"])])

    def on_sel(self, sel):
        model, it = sel.get_selected()
        if it:
            self.name.set_text(model[it][0])
            self.mac.set_text(model[it][1])
            self.bcast.set_text(model[it][2])
            self.port.set_value(int(model[it][3]))

    def current(self):
        return {"name": self.name.get_text() or self.mac.get_text(), "mac": self.mac.get_text().strip(),
                "bcast": self.bcast.get_text().strip(), "port": int(self.port.get_value())}

    @staticmethod
    def send(mac, bcast, port):
        raw = bytes.fromhex(re.sub(r"[^0-9a-fA-F]", "", mac))
        if len(raw) != 6:
            raise ValueError("MAC no válida")
        packet = b"\xff" * 6 + raw * 16
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(packet, (bcast, port))

    def wake(self, *_):
        c = self.current()
        try:
            self.send(c["mac"], c["bcast"], c["port"])
            self.status.set_text(f"Paquete mágico enviado a {c['mac']}")
        except (ValueError, OSError) as e:
            self.status.set_text(f"Error: {e}")

    def save(self, *_):
        c = self.current()
        self.items = [i for i in self.items if i["mac"] != c["mac"]] + [c]
        save_json(WOL_FILE, self.items)
        self.reload()

    def delete(self, *_):
        mac = self.mac.get_text().strip()
        self.items = [i for i in self.items if i["mac"] != mac]
        save_json(WOL_FILE, self.items)
        self.reload()


class PacketCapture:
    """Captura de paquetes con tcpdump (requiere sudo) en una pestaña de terminal."""

    def __init__(self, app):
        self.app = app
        d = Gtk.Dialog(title="Captura de paquetes", transient_for=app.window, modal=True)
        d.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Iniciar captura", Gtk.ResponseType.OK)
        grid = Gtk.Grid(column_spacing=8, row_spacing=6, margin=10)
        d.get_content_area().add(grid)
        iface = Gtk.ComboBoxText()
        iface.append("any", "any (todas)")
        for n in sorted(os.listdir("/sys/class/net")):
            iface.append(n, n)
        iface.set_active_id("any")
        filt = Gtk.Entry(placeholder_text="Filtro BPF, p. ej. port 22 or host 10.0.0.5", hexpand=True)
        save = Gtk.CheckButton(label="Guardar en archivo .pcap (en Descargas)")
        verbose = Gtk.CheckButton(label="Mostrar contenido (ASCII)")
        for i, (lab, w) in enumerate((("Interfaz", iface), ("Filtro", filt))):
            grid.attach(Gtk.Label(label=lab, xalign=1), 0, i, 1, 1)
            grid.attach(w, 1, i, 1, 1)
        grid.attach(save, 1, 2, 1, 1)
        grid.attach(verbose, 1, 3, 1, 1)
        d.show_all()
        ok = d.run() == Gtk.ResponseType.OK
        args = [iface.get_active_id(), filt.get_text().strip(), save.get_active(), verbose.get_active()]
        d.destroy()
        if not ok:
            return
        if not shutil.which("tcpdump"):
            message(app.window, "Falta tcpdump", "Instálalo con ./install-deps.sh", Gtk.MessageType.ERROR)
            return
        cmd = ["sudo", "tcpdump", "-i", args[0], "-n", "-l"]
        if args[3]:
            cmd.append("-A")
        if args[2]:
            out = os.path.join(GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
                               or os.path.expanduser("~"), time.strftime("captura-%Y%m%d-%H%M%S.pcap"))
            cmd += ["-w", out, "--print"]
        cmd += shlex.split(args[1])
        app.open_session(new_session(name=f"Captura {args[0]}", type="shell", command=shlex.join(cmd)))


# ---------------------------------------------------------------- túneles

class TunnelManager:
    """Gestor de túneles SSH (-L local, -R remoto, -D SOCKS dinámico)."""
    procs = {}  # id -> Popen (compartido entre ventanas)

    def __init__(self, app):
        self.app = app
        win = self.win = tool_window(app, "Túneles SSH", 900, 460)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        win.add(box)
        top = Gtk.Box(spacing=6)
        for label, icon, cb in (("Nuevo túnel", "list-add", self.on_new), ("Editar", "document-edit", self.on_edit),
                                ("Iniciar", "media-playback-start", self.on_start),
                                ("Detener", "media-playback-stop", self.on_stop),
                                ("Iniciar todos", "media-seek-forward", self.start_all),
                                ("Eliminar", "edit-delete", self.on_delete)):
            top.pack_start(button(label, icon, cb), False, False, 0)
        box.pack_start(top, False, False, 0)
        self.store, self.view, sw = list_view(["Estado", "Nombre", "Tipo", "Puerto local", "Destino",
                                               "Servidor SSH", "Autoinicio", "id"])
        self.view.get_column(7).set_visible(False)
        self.view.connect("row-activated", lambda *_: self.on_edit())
        box.pack_start(sw, True, True, 0)
        help_ = Gtk.Label(xalign=0, wrap=True)
        help_.set_markup("<small><b>Local</b>: un puerto de tu PC lleva a un destino visto desde el servidor. "
                         "<b>Remoto</b>: un puerto del servidor lleva a un destino visto desde tu PC. "
                         "<b>Dinámico</b>: proxy SOCKS5 en tu PC que sale por el servidor.</small>")
        box.pack_start(help_, False, False, 0)
        self.tunnels = load_json(TUNNELS_FILE, [])
        self.alive = True
        win.connect("destroy", lambda *_: setattr(self, "alive", False))
        GLib.timeout_add_seconds(1, lambda: self.alive and (self.reload() or True))
        self.reload()
        win.show_all()

    @classmethod
    def autostart(cls, app):
        for t in load_json(TUNNELS_FILE, []):
            if t.get("autostart"):
                cls.start(app, t)

    @classmethod
    def running(cls, tid):
        p = cls.procs.get(tid)
        return p is not None and p.poll() is None

    @classmethod
    def start(cls, app, t):
        if cls.running(t["id"]):
            return None
        s = next((x for x in app.sessions if x["id"] == t["session"]), None)
        if not s:
            return "La sesión SSH del túnel ya no existe."
        spec = {"L": ["-L", f"{t['bind']}:{t['lport']}:{t['rhost']}:{t['rport']}"],
                "R": ["-R", f"{t['rport']}:{t['rhost']}:{t['lport']}"],
                "D": ["-D", f"{t['bind']}:{t['lport']}"]}[t["kind"]]
        opts = [o for o in ssh_opts(s, "-p") if not o.startswith(("ControlMaster", "ControlPersist"))]
        cmd = ["ssh", "-N", "-o", "ExitOnForwardFailure=yes", "-o", "ControlMaster=no"] + opts + spec + \
              [ssh_target(s)]
        env = {**os.environ, **askpass_env(s)}
        cls.procs[t["id"]] = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL,
                                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                              start_new_session=True)
        return None

    @classmethod
    def stop_all(cls):
        for p in cls.procs.values():
            if p.poll() is None:
                p.terminate()

    def reload(self):
        sel = self.selected()
        self.store.clear()
        names = {s["id"]: s["name"] for s in self.app.sessions}
        for t in self.tunnels:
            p = self.procs.get(t["id"])
            if self.running(t["id"]):
                state = "● Activo"
            elif p is not None:
                err = p.stderr.read().decode(errors="replace").strip() if p.stderr else ""
                t["_error"] = err.splitlines()[-1] if err else f"terminó ({p.returncode})"
                self.procs.pop(t["id"])
                state = "✖ " + t["_error"]
            else:
                state = ("✖ " + t["_error"]) if t.get("_error") else "○ Detenido"
            dest = "SOCKS5" if t["kind"] == "D" else f"{t['rhost']}:{t['rport']}"
            self.store.append([state, t["name"], {"L": "Local", "R": "Remoto", "D": "Dinámico"}[t["kind"]],
                               f"{t['bind']}:{t['lport']}", dest, names.get(t["session"], "?"),
                               "sí" if t.get("autostart") else "", t["id"]])
        if sel:
            for row in self.store:
                if row[7] == sel["id"]:
                    self.view.get_selection().select_iter(row.iter)
        return False

    def selected(self):
        model, it = self.view.get_selection().get_selected()
        return next((t for t in self.tunnels if it and t["id"] == model[it][7]), None)

    def persist(self):
        save_json(TUNNELS_FILE, [{k: v for k, v in t.items() if not k.startswith("_")} for t in self.tunnels])

    def edit(self, t):
        d = Gtk.Dialog(title="Túnel SSH", transient_for=self.win, modal=True)
        d.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Guardar", Gtk.ResponseType.OK)
        grid = Gtk.Grid(column_spacing=8, row_spacing=6, margin=10)
        d.get_content_area().add(grid)
        name = Gtk.Entry(text=t["name"], hexpand=True)
        kind = Gtk.ComboBoxText()
        for k, lab in (("L", "Local (-L)"), ("R", "Remoto (-R)"), ("D", "Dinámico SOCKS (-D)")):
            kind.append(k, lab)
        kind.set_active_id(t["kind"])
        sess = Gtk.ComboBoxText()
        for s in self.app.sessions:
            if s["type"] == "ssh":
                sess.append(s["id"], f"{s['name']} ({ssh_target(s)})")
        sess.set_active_id(t["session"]) or sess.set_active(0)
        bind = Gtk.Entry(text=t["bind"])
        lport = Gtk.Entry(text=str(t["lport"]))
        rhost = Gtk.Entry(text=t["rhost"])
        rport = Gtk.Entry(text=str(t["rport"]))
        auto = Gtk.CheckButton(label="Iniciar al abrir MerlynGestorTerm")
        auto.set_active(t.get("autostart", False))
        rows = (("Nombre", name), ("Tipo", kind), ("Servidor SSH", sess), ("Dirección local", bind),
                ("Puerto local", lport), ("Host destino", rhost), ("Puerto destino", rport))
        for i, (lab, w) in enumerate(rows):
            grid.attach(Gtk.Label(label=lab, xalign=1), 0, i, 1, 1)
            grid.attach(w, 1, i, 1, 1)
        grid.attach(auto, 1, len(rows), 1, 1)

        def on_kind(*_):
            rhost.set_sensitive(kind.get_active_id() != "D")
            rport.set_sensitive(kind.get_active_id() != "D")
        kind.connect("changed", on_kind)
        on_kind()
        d.show_all()
        if not self.app.sessions or sess.get_active_id() is None:
            message(self.win, "Primero crea una sesión SSH para usarla como servidor del túnel.")
            d.destroy()
            return None
        ok = d.run() == Gtk.ResponseType.OK
        if ok:
            t.update(name=name.get_text() or "Túnel", kind=kind.get_active_id(), session=sess.get_active_id(),
                     bind=bind.get_text() or "127.0.0.1", lport=int(lport.get_text() or 0),
                     rhost=rhost.get_text() or "127.0.0.1", rport=int(rport.get_text() or 0),
                     autostart=auto.get_active())
        d.destroy()
        return t if ok else None

    def on_new(self, *_):
        t = self.edit({"id": uuid.uuid4().hex, "name": "Nuevo túnel", "kind": "L", "session": None,
                       "bind": "127.0.0.1", "lport": 8080, "rhost": "127.0.0.1", "rport": 80})
        if t:
            self.tunnels.append(t)
            self.persist()
            self.reload()

    def on_edit(self, *_):
        t = self.selected()
        if t and self.edit(t):
            self.persist()
            self.reload()

    def on_start(self, *_):
        t = self.selected()
        if t:
            t.pop("_error", None)
            err = self.start(self.app, t)
            if err:
                message(self.win, err)
            self.reload()

    def start_all(self, *_):
        for t in self.tunnels:
            t.pop("_error", None)
            self.start(self.app, t)
        self.reload()

    def on_stop(self, *_):
        t = self.selected()
        if t and self.running(t["id"]):
            self.procs.pop(t["id"]).terminate()
            self.reload()

    def on_delete(self, *_):
        t = self.selected()
        if t:
            self.on_stop()
            self.tunnels.remove(t)
            self.persist()
            self.reload()


# ---------------------------------------------------------------- paquetes

def package_manager():
    """Gestor de paquetes del sistema: "pacman", "apt" o "dnf" (None si no hay ninguno)."""
    for name, exe in (("pacman", "pacman"), ("apt", "apt-get"), ("dnf", "dnf")):
        if shutil.which(exe):
            return name
    return None


class PackageManager:
    """Equivalente a MobApt: busca e instala paquetes del sistema (pacman/yay, apt o dnf)."""

    COMMANDS = {
        "pacman": {"install": ["sudo", "pacman", "-S"], "remove": ["sudo", "pacman", "-Rns"],
                   "upgrade": "sudo pacman -Syu"},
        "apt": {"install": ["sudo", "apt", "install"], "remove": ["sudo", "apt", "remove"],
                "upgrade": "sudo apt update && sudo apt upgrade"},
        "dnf": {"install": ["sudo", "dnf", "install"], "remove": ["sudo", "dnf", "remove"],
                "upgrade": "sudo dnf upgrade"},
    }

    def __init__(self, app):
        self.app = app
        self.pm = package_manager()
        win = self.win = tool_window(app, f"Gestor de paquetes ({self.pm or 'no disponible'})", 900, 560)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        win.add(box)
        top = Gtk.Box(spacing=6)
        self.q = Gtk.SearchEntry(placeholder_text="Buscar paquete…", hexpand=True)
        self.q.connect("activate", self.search)
        self.aur = Gtk.CheckButton(label="Incluir AUR (yay/paru)")
        self.aur.set_sensitive(self.pm == "pacman" and bool(shutil.which("yay") or shutil.which("paru")))
        top.pack_start(self.q, True, True, 0)
        top.pack_start(self.aur, False, False, 0)
        top.pack_start(button("Buscar", "system-search", self.search), False, False, 0)
        box.pack_start(top, False, False, 0)
        self.store, self.view, sw = list_view(["Instalado", "Repositorio", "Paquete", "Versión", "Descripción"])
        self.view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        box.pack_start(sw, True, True, 0)
        row = Gtk.Box(spacing=6)
        row.pack_start(button("Instalar", "list-add", lambda *_: self.act("install")), False, False, 0)
        row.pack_start(button("Desinstalar", "list-remove", lambda *_: self.act("remove")), False, False, 0)
        row.pack_start(button("Actualizar el sistema", "software-update-available", self.upgrade), False, False, 0)
        row.pack_start(button("Ver instalados", "view-list", self.installed), False, False, 0)
        box.pack_start(row, False, False, 0)
        win.show_all()
        if not self.pm:
            message(win, "No se encontró pacman, apt ni dnf en este sistema.")

    @staticmethod
    def output(cmd):
        return subprocess.run(cmd, capture_output=True, text=True).stdout

    # -- búsqueda: cada gestor devuelve filas [instalado, repo, paquete, versión, descripción]
    @staticmethod
    def parse_pacman(out):
        rows, lines = [], out.splitlines()
        for i in range(0, len(lines) - 1, 2):
            m = re.match(r"(\S+)/(\S+)\s+(\S+)(.*)", lines[i])
            if m:
                rows.append(["✔" if "[instal" in m.group(4).lower() else "", m.group(1), m.group(2),
                             m.group(3), lines[i + 1].strip()])
        return rows

    @staticmethod
    def parse_apt(out, installed):
        rows = []
        for line in out.splitlines():
            name, sep, desc = line.partition(" - ")
            if sep:
                rows.append(["✔" if name in installed else "", "apt", name, "", desc])
        return rows

    @staticmethod
    def parse_dnf(out, installed):
        rows, seen = [], set()
        for line in out.splitlines():
            # dnf4: "nombre.arch : resumen"   dnf5: " nombre.arch<tab>resumen"
            m = re.match(r"^\s*([A-Za-z0-9_.+-]+)\.(noarch|x86_64|aarch64|i686|ppc64le|s390x|src)\s*[:\t]\s*(.*)$",
                         line)
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                rows.append(["✔" if m.group(1) in installed else "", "dnf", m.group(1), "", m.group(3)])
        return rows

    def search(self, *_):
        q = self.q.get_text().strip()
        if not q or not self.pm:
            return
        self.store.clear()
        if self.pm == "pacman":
            exe = (shutil.which("yay") or shutil.which("paru")) if self.aur.get_active() else "pacman"
            work = lambda: self.parse_pacman(self.output([exe, "-Ss", q]))
        elif self.pm == "apt":
            work = lambda: self.parse_apt(self.output(["apt-cache", "search", q]),
                                          set(self.output(["dpkg-query", "-W", "-f=${Package}\n"]).split()))
        else:
            work = lambda: self.parse_dnf(self.output(["dnf", "search", "-q", q]),
                                          set(self.output(["rpm", "-qa", "--qf", "%{NAME}\n"]).split()))
        bg(work, self.fill)

    def installed(self, *_):
        if not self.pm:
            return
        self.store.clear()
        cmd = {"pacman": ["pacman", "-Qe"], "apt": ["apt-mark", "showmanual"],
               "dnf": ["rpm", "-qa", "--qf", "%{NAME} %{VERSION}\n"]}[self.pm]
        bg(lambda: [["✔", "local", *(l.split(None, 1) + [""])[:2], ""]
                    for l in sorted(self.output(cmd).splitlines()) if l.strip()], self.fill)

    def fill(self, rows):
        if isinstance(rows, Exception):
            message(self.win, "Error al consultar los paquetes", str(rows), Gtk.MessageType.ERROR)
            return
        for row in rows[:2000]:
            self.store.append(row)

    def act(self, what):
        model, paths = self.view.get_selection().get_selected_rows()
        pkgs = [model[p][2] for p in paths]
        if not pkgs or not self.pm:
            return
        if self.pm == "pacman" and any(model[p][1] == "aur" for p in paths):
            exe = shutil.which("yay") or shutil.which("paru")
            cmd = [exe, "-S" if what == "install" else "-Rns"] + pkgs
        else:
            cmd = self.COMMANDS[self.pm][what] + pkgs
        self.run(shlex.join(cmd))

    def upgrade(self, *_):
        if self.pm:
            self.run(self.COMMANDS[self.pm]["upgrade"])

    def run(self, command):
        self.app.open_session(new_session(name="Paquetes", type="shell",
                                          command=command + "; echo; read -n1 -p 'Pulsa una tecla…'"))


# ---------------------------------------------------------------- claves

class KeyGenerator:
    def __init__(self, app):
        self.app = app
        d = self.d = Gtk.Dialog(title="Generador de claves SSH", transient_for=app.window)
        d.set_default_size(640, 420)
        grid = Gtk.Grid(column_spacing=8, row_spacing=6, margin=10)
        d.get_content_area().add(grid)
        self.kind = Gtk.ComboBoxText()
        for k, lab in (("ed25519", "Ed25519 (recomendada)"), ("rsa", "RSA 4096"), ("ecdsa", "ECDSA 521")):
            self.kind.append(k, lab)
        self.kind.set_active_id("ed25519")
        self.name = Gtk.Entry(text="id_merlyn", hexpand=True)
        self.comment = Gtk.Entry(text=f"{os.environ.get('USER', 'user')}@{socket.gethostname()}")
        self.pw = Gtk.Entry(visibility=False, placeholder_text="(opcional)")
        self.where = Gtk.ComboBoxText()
        self.where.append("portable", "Carpeta portable (merlyngestorterm-data/keys)")
        self.where.append("ssh", "~/.ssh")
        self.where.set_active_id("portable")
        for i, (lab, w) in enumerate((("Tipo", self.kind), ("Nombre de archivo", self.name),
                                      ("Comentario", self.comment), ("Passphrase", self.pw),
                                      ("Guardar en", self.where))):
            grid.attach(Gtk.Label(label=lab, xalign=1), 0, i, 1, 1)
            grid.attach(w, 1, i, 1, 1)
        grid.attach(button("Generar", "system-run", self.generate), 1, 5, 1, 1)
        self.pub = Gtk.TextView(editable=False, wrap_mode=Gtk.WrapMode.CHAR, monospace=True)
        sw = Gtk.ScrolledWindow(vexpand=True, min_content_height=100)
        sw.add(self.pub)
        grid.attach(Gtk.Label(label="Clave pública", xalign=1), 0, 6, 1, 1)
        grid.attach(sw, 1, 6, 1, 1)
        grid.attach(button("Copiar clave pública", "edit-copy", self.copy), 1, 7, 1, 1)
        d.show_all()

    def generate(self, *_):
        folder = KEYS_DIR if self.where.get_active_id() == "portable" else os.path.expanduser("~/.ssh")
        os.makedirs(folder, mode=0o700, exist_ok=True)
        path = os.path.join(folder, self.name.get_text().strip() or "id_merlyn")
        if os.path.exists(path):
            message(self.d, "Ya existe un archivo con ese nombre.", path, Gtk.MessageType.ERROR)
            return
        kind = self.kind.get_active_id()
        cmd = ["ssh-keygen", "-q", "-t", kind, "-f", path, "-C", self.comment.get_text(), "-N",
               self.pw.get_text()] + (["-b", "4096"] if kind == "rsa" else ["-b", "521"] if kind == "ecdsa" else [])
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode:
            message(self.d, "Error al generar la clave", p.stderr, Gtk.MessageType.ERROR)
            return
        with open(path + ".pub") as f:
            self.pub.get_buffer().set_text(f.read().strip())
        message(self.d, "Clave creada", f"{path}\n\nAsígnala en una sesión SSH y usa «Instalar mi clave "
                                        "en el servidor» para copiar la pública.")

    def copy(self, *_):
        buf = self.pub.get_buffer()
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(
            buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False), -1)


# ---------------------------------------------------------------- contraseñas

class PasswordManager:
    def __init__(self, app):
        self.app = app
        win = self.win = tool_window(app, "Contraseñas guardadas", 760, 440)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        win.add(box)
        note = Gtk.Label(xalign=0, wrap=True)
        note.set_markup("Las credenciales están cifradas en el llavero del sistema (GNOME Keyring) "
                        "y se desbloquean con tu inicio de sesión.")
        box.pack_start(note, False, False, 0)
        self.store, self.view, sw = list_view(["Sesión", "Servidor", "Contraseña", "Passphrase", "id"])
        self.view.get_column(4).set_visible(False)
        box.pack_start(sw, True, True, 0)
        row = Gtk.Box(spacing=6)
        self.reveal = Gtk.ToggleButton(label="Mostrar contraseñas")
        self.reveal.connect("toggled", lambda *_: self.reload())
        row.pack_start(self.reveal, False, False, 0)
        row.pack_start(button("Cambiar…", "document-edit", self.change), False, False, 0)
        row.pack_start(button("Borrar credenciales", "edit-delete", self.clear), False, False, 0)
        box.pack_start(row, False, False, 0)
        self.reload()
        win.show_all()

    def reload(self):
        self.store.clear()
        show = self.reveal.get_active()
        for s in self.app.sessions:
            pw, pp = secret_get(s["id"], "password"), secret_get(s["id"], "passphrase")
            if pw or pp:
                fmt = (lambda v: v or "") if show else (lambda v: "••••••" if v else "")
                self.store.append([s["name"], ssh_target(s) if s.get("host") else "", fmt(pw), fmt(pp), s["id"]])

    def sel(self):
        model, it = self.view.get_selection().get_selected()
        return next((s for s in self.app.sessions if it and s["id"] == model[it][4]), None)

    def change(self, *_):
        s = self.sel()
        if not s:
            return
        d = Gtk.Dialog(title=f"Credenciales de {s['name']}", transient_for=self.win, modal=True)
        d.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Guardar", Gtk.ResponseType.OK)
        grid = Gtk.Grid(column_spacing=8, row_spacing=6, margin=10)
        d.get_content_area().add(grid)
        entries = {}
        for i, (kind, lab) in enumerate((("password", "Contraseña"), ("passphrase", "Passphrase"))):
            e = Gtk.Entry(visibility=False, text=secret_get(s["id"], kind) or "", hexpand=True)
            grid.attach(Gtk.Label(label=lab, xalign=1), 0, i, 1, 1)
            grid.attach(e, 1, i, 1, 1)
            entries[kind] = e
        d.show_all()
        if d.run() == Gtk.ResponseType.OK:
            for kind, e in entries.items():
                secret_set(s["id"], kind, e.get_text(), s["name"])
        d.destroy()
        self.reload()

    def clear(self, *_):
        s = self.sel()
        if s:
            for kind in ("password", "passphrase"):
                secret_set(s["id"], kind, "", s["name"])
            self.reload()


# ---------------------------------------------------------------- macros

def macro_encode(text):
    return text.encode("unicode_escape").decode("ascii")


def macro_decode(text):
    return text.encode("ascii", "backslashreplace").decode("unicode_escape")


class MacroPanel(Gtk.Box):
    """Panel lateral de macros: grabar lo que se teclea y reproducirlo."""

    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin=4)
        self.app = app
        self.macros = load_json(MACROS_FILE, [])
        bar = Gtk.Box(spacing=2)
        self.rec = Gtk.ToggleButton()
        self.rec.set_image(Gtk.Image.new_from_icon_name("media-record", Gtk.IconSize.BUTTON))
        self.rec.set_tooltip_text("Grabar macro (lo que escribas en la terminal)")
        self.rec.connect("toggled", self.on_record)
        bar.pack_start(self.rec, False, False, 0)
        for icon, tip, cb in (("media-playback-start", "Ejecutar en la terminal activa", self.play),
                              ("media-seek-forward", "Ejecutar en todas las terminales", self.play_all),
                              ("list-add", "Nueva macro", self.new),
                              ("document-edit", "Editar", self.edit),
                              ("edit-delete", "Eliminar", self.delete)):
            b = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
            b.set_tooltip_text(tip)
            b.connect("clicked", cb)
            bar.pack_start(b, False, False, 0)
        self.pack_start(bar, False, False, 0)
        self.store = Gtk.ListStore(str, int)
        self.view = Gtk.TreeView(model=self.store, headers_visible=False)
        self.view.append_column(Gtk.TreeViewColumn("Macro", Gtk.CellRendererText(), text=0))
        self.view.connect("row-activated", lambda *_: self.play())
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.add(self.view)
        self.pack_start(sw, True, True, 0)
        self.reload()

    def reload(self):
        self.store.clear()
        for i, m in enumerate(self.macros):
            self.store.append([m["name"], i])

    def save(self):
        save_json(MACROS_FILE, self.macros)
        self.reload()

    def current(self):
        model, it = self.view.get_selection().get_selected()
        return self.macros[model[it][1]] if it else None

    def on_record(self, btn):
        if btn.get_active():
            self.app.macro_recording = []
            self.app.set_status("Grabando macro… escribe en cualquier terminal y vuelve a pulsar ● para parar.")
            return
        keys = "".join(self.app.macro_recording or [])
        self.app.macro_recording = None
        self.app.set_status("")
        if keys:
            self.macros.append({"name": f"Macro {len(self.macros) + 1}", "keys": macro_encode(keys)})
            self.save()
            self.edit(index=len(self.macros) - 1)

    def play(self, *_):
        m = self.current()
        pane = self.app.active_pane()
        if m and pane:
            pane.term.feed_child(macro_decode(m["keys"]).encode())
            pane.term.grab_focus()

    def play_all(self, *_):
        m = self.current()
        if m:
            for pane in self.app.all_panes():
                pane.term.feed_child(macro_decode(m["keys"]).encode())

    def new(self, *_):
        self.macros.append({"name": "Nueva macro", "keys": "ls -la\\r"})
        self.save()
        self.edit(index=len(self.macros) - 1)

    def edit(self, *_, index=None):
        m = self.macros[index] if index is not None else self.current()
        if not m:
            return
        d = Gtk.Dialog(title="Editar macro", transient_for=self.app.window, modal=True)
        d.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Guardar", Gtk.ResponseType.OK)
        grid = Gtk.Grid(column_spacing=8, row_spacing=6, margin=10)
        d.get_content_area().add(grid)
        name = Gtk.Entry(text=m["name"], hexpand=True)
        keys = Gtk.Entry(text=m["keys"])
        grid.attach(Gtk.Label(label="Nombre", xalign=1), 0, 0, 1, 1)
        grid.attach(name, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="Teclas", xalign=1), 0, 1, 1, 1)
        grid.attach(keys, 1, 1, 1, 1)
        hint = Gtk.Label(label="\\r = Intro, \\t = Tab, \\x03 = Ctrl+C, \\x1b = Esc", xalign=0)
        hint.get_style_context().add_class("dim-label")
        grid.attach(hint, 1, 2, 1, 1)
        d.show_all()
        if d.run() == Gtk.ResponseType.OK:
            m["name"], m["keys"] = name.get_text(), keys.get_text()
            self.save()
        d.destroy()

    def delete(self, *_):
        m = self.current()
        if m:
            self.macros.remove(m)
            self.save()


# ---------------------------------------------------------------- salvapantallas

class Screensaver(Gtk.Window):
    """Salvapantallas original: una red de nodos por la que viajan paquetes."""

    def __init__(self, app):
        super().__init__(title="Salvapantallas")
        self.fullscreen()
        self.nodes = [[random.random(), random.random(), random.uniform(-1, 1) * 0.0008,
                       random.uniform(-1, 1) * 0.0008] for _ in range(70)]
        self.packets = []
        self.t0 = time.time()
        area = Gtk.DrawingArea()
        area.connect("draw", self.draw)
        self.add(area)
        self.add_events(Gdk.EventMask.KEY_PRESS_MASK | Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("key-press-event", lambda *_: self.destroy())
        self.connect("button-press-event", lambda *_: self.destroy())
        GLib.timeout_add(33, lambda: self.get_realized() and (area.queue_draw() or True))
        self.show_all()

    def draw(self, area, cr):
        w, h = area.get_allocated_width(), area.get_allocated_height()
        cr.set_source_rgb(0.04, 0.05, 0.09)
        cr.paint()
        for n in self.nodes:
            n[0] = (n[0] + n[2]) % 1
            n[1] = (n[1] + n[3]) % 1
        close = []
        for i, a in enumerate(self.nodes):
            for j in range(i + 1, len(self.nodes)):
                b = self.nodes[j]
                d = math.hypot((a[0] - b[0]) * w, (a[1] - b[1]) * h)
                if d < 160:
                    close.append((i, j))
                    cr.set_source_rgba(0.35, 0.6, 1, 1 - d / 160)
                    cr.set_line_width(1)
                    cr.move_to(a[0] * w, a[1] * h)
                    cr.line_to(b[0] * w, b[1] * h)
                    cr.stroke()
        if close and random.random() < 0.3:
            self.packets.append([*random.choice(close), 0.0, random.choice(
                [(0.4, 1, 0.6), (1, 0.8, 0.3), (1, 0.4, 0.5)])])
        for p in self.packets:
            p[2] += 0.03
            a, b = self.nodes[p[0]], self.nodes[p[1]]
            x, y = (a[0] + (b[0] - a[0]) * p[2]) * w, (a[1] + (b[1] - a[1]) * p[2]) * h
            cr.set_source_rgb(*p[3])
            cr.arc(x, y, 3, 0, 2 * math.pi)
            cr.fill()
        self.packets = [p for p in self.packets if p[2] < 1]
        for n in self.nodes:
            cr.set_source_rgb(0.7, 0.85, 1)
            cr.arc(n[0] * w, n[1] * h, 2.5, 0, 2 * math.pi)
            cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.35)
        cr.select_font_face("Sans")
        cr.set_font_size(18)
        cr.move_to(30, h - 30)
        cr.show_text(time.strftime("%H:%M") + "  ·  MerlynGestorTerm  ·  pulsa una tecla para volver")
