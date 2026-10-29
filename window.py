"""Ventana principal de MerlynGestorTerm con la distribución clásica de MobaXterm:
menú, cinta de botones grandes, barra lateral con pestañas verticales
(Sesiones / Herramientas / Macros / SFTP), pestaña de inicio y barra de estado."""
import configparser
import glob
import os
import subprocess
import uuid

import gtkver  # noqa: F401
from gi.repository import Gdk, Gio, GLib, Gtk, Pango

import games
import servers
import tools
from core import (APP_DIR, APP_NAME, BROWSER_TYPES, DATA_DIR, DEFAULT_PORTS, KEYS_DIR, PALETTES, RECENT_FILE,
                  SCHEMA, SESSIONS_FILE, SETTINGS_FILE, TYPE_ICONS, TYPE_LABELS, TYPES, VERSION,
                  import_key, import_mobaxterm_ini, key_path, load_json, load_settings, new_session,
                  recent_add, save_json, secret_get, secret_set, ssh_target)
from terminal import TermTab

CSS = b"""
.nx-ribbon { background-image: linear-gradient(to bottom, shade(@theme_bg_color, 1.10),
                                                 shade(@theme_bg_color, 0.94));
             border-bottom: 1px solid shade(@theme_bg_color, 0.75); padding: 2px 4px; }
.nx-ribbon button { padding: 2px 6px; border-radius: 4px; min-width: 58px; }
.nx-ribbon button label { font-size: 9pt; }
.nx-ribbon separator { margin: 6px 3px; }
.nx-side-tabs tab { padding: 8px 3px; min-width: 0; }
.nx-side-tabs tab label { font-size: 9pt; }
.nx-home-title { font-size: 26pt; font-weight: 300; }
.nx-home-sub { font-size: 11pt; }
.nx-home-card { border: 1px solid alpha(currentColor, 0.15); border-radius: 8px; padding: 12px; }
.nx-big { padding: 10px 18px; font-size: 12pt; }
.nx-monitor { background: #1b1f27; color: #cfd6e4; padding: 2px 8px; font-size: 9pt; }
.nx-monitor image { color: #8fb3ff; }
.nx-status { padding: 2px 8px; font-size: 9pt; border-top: 1px solid alpha(currentColor, 0.12); }
.nx-type-btn { padding: 4px 6px; min-width: 64px; }
.nx-type-btn label { font-size: 9pt; }
.nx-browser-bar button { padding: 2px; }
"""

# Campos del diálogo de sesión: clave -> (etiqueta, tipo)
FIELD_DEFS = {
    "host": ("Host remoto", "str"), "user": ("Usuario", "str"), "port": ("Puerto", "int"),
    "device": ("Puerto serie", "serial"), "baud": ("Velocidad (baudios)", "baud"),
    "path": ("Carpeta / bucket", "str"), "url": ("URL", "str"), "profile": ("Perfil AWS", "str"),
    "command": ("Comando a ejecutar", "str"), "key": ("Clave privada", "key"),
    "jump": ("Jump host (usuario@host:puerto)", "str"), "x11": ("Reenvío X11", "bool"),
    "compression": ("Compresión", "bool"), "agent_forward": ("Reenviar agente SSH", "bool"),
    "monitoring": ("Barra de monitorización remota", "bool"), "sftp": ("Explorador SFTP", "bool"),
    "tls": ("Cifrado TLS (FTPS)", "bool"), "extra": ("Argumentos extra", "str"),
    "log": ("Guardar registro de la sesión", "bool"), "color": ("Color de la pestaña", "color"),
}
BASIC = {
    "ssh": ["host", "user", "port"], "telnet": ["host", "port"], "rsh": ["host", "user"],
    "xdmcp": ["host", "port"], "rdp": ["host", "user", "port"], "vnc": ["host", "port"],
    "ftp": ["host", "user", "port"], "sftp": ["host", "user", "port"], "serial": ["device", "baud"],
    "file": ["path"], "shell": ["command"], "browser": ["url"], "mosh": ["host", "user", "port"],
    "s3": ["profile", "path", "url"],
}
ADVANCED = {
    "ssh": ["key", "jump", "x11", "compression", "agent_forward", "command", "monitoring", "sftp", "extra"],
    "sftp": ["key", "jump", "extra"], "mosh": ["key", "jump", "extra"], "ftp": ["tls", "path"],
    "telnet": ["extra"], "rdp": ["extra"], "vnc": ["extra"], "serial": ["extra"], "xdmcp": ["extra"],
}
CREDS = {"ssh": ("password", "passphrase"), "sftp": ("password", "passphrase"), "mosh": ("password", "passphrase"),
         "rdp": ("password",), "ftp": ("password",)}


# ---------------------------------------------------------------- diálogo

class SessionDialog(Gtk.Dialog):
    def __init__(self, parent, session):
        super().__init__(title="Configuración de sesión", transient_for=parent, modal=True)
        self.set_default_size(760, 520)
        self.session = dict(session)
        self.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Aceptar", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        area = self.get_content_area()
        area.set_spacing(6)

        # fila de tipos de sesión con iconos grandes (como MobaXterm)
        types = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=14,
                            min_children_per_line=7, margin=6)
        self.type_btns = {}
        group = None
        for t in TYPES:
            b = Gtk.RadioButton.new_from_widget(group)
            group = group or b
            b.set_mode(False)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.pack_start(Gtk.Image.new_from_icon_name(TYPE_ICONS[t], Gtk.IconSize.DND), False, False, 0)
            box.pack_start(Gtk.Label(label=TYPE_LABELS[t]), False, False, 0)
            b.add(box)
            b.get_style_context().add_class("nx-type-btn")
            b.connect("toggled", self.on_type, t)
            types.add(b)
            self.type_btns[t] = b
        area.pack_start(types, False, False, 0)
        area.pack_start(Gtk.Separator(), False, False, 0)

        self.basic = Gtk.Grid(column_spacing=10, row_spacing=6, margin=8)
        basic_frame = Gtk.Frame(label=" Configuración básica ", margin=6)
        basic_frame.add(self.basic)
        area.pack_start(basic_frame, False, False, 0)

        self.nb = Gtk.Notebook(margin=6)
        self.adv = Gtk.Grid(column_spacing=10, row_spacing=6, margin=8)
        self.creds = Gtk.Grid(column_spacing=10, row_spacing=6, margin=8)
        self.mark = Gtk.Grid(column_spacing=10, row_spacing=6, margin=8)
        self.nb.append_page(self.adv, Gtk.Label(label="Avanzado"))
        self.nb.append_page(self.creds, Gtk.Label(label="Credenciales"))
        self.nb.append_page(self.mark, Gtk.Label(label="Marcador"))
        area.pack_start(self.nb, True, True, 0)

        self.widgets = {}
        self.secret_entries = {}
        self.portable = Gtk.CheckButton(label="Copiar a la carpeta portable (convierte .ppk)")
        # marcador (comunes)
        self.name = Gtk.Entry(text=self.session["name"], hexpand=True)
        self.group = Gtk.ComboBoxText.new_with_entry()
        for g in sorted({s.get("group", "") for s in getattr(parent, "sessions", []) if s.get("group")}):
            self.group.append_text(g)
        self.group.get_child().set_text(self.session.get("group", ""))
        self.attach(self.mark, 0, "Nombre de la sesión", self.name)
        self.attach(self.mark, 1, "Carpeta", self.group)
        for i, key in enumerate(("color", "log")):
            self.widgets[key] = self.make(key)
            self.attach(self.mark, i + 2, FIELD_DEFS[key][0], self.widgets[key])

        self.show_all()
        self.type_btns[self.session["type"] if self.session["type"] in TYPES else "ssh"].set_active(True)
        self.on_type(self.type_btns[self.session["type"] if self.session["type"] in TYPES else "ssh"],
                     self.session["type"])

    @staticmethod
    def attach(grid, row, label, widget):
        grid.attach(Gtk.Label(label=label, xalign=1), 0, row, 1, 1)
        grid.attach(widget, 1, row, 1, 1)

    def make(self, key):
        label, kind = FIELD_DEFS[key]
        val = self.session.get(key, "")
        if kind == "bool":
            w = Gtk.CheckButton()
            w.set_active(bool(val) if key not in ("monitoring", "sftp") else val is not False)
        elif kind == "int":
            w = Gtk.SpinButton.new_with_range(0, 65535, 1)
            w.set_value(int(val or 0))
        elif kind == "serial":
            w = Gtk.ComboBoxText.new_with_entry()
            for dev in sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyS[0-3]")):
                w.append_text(dev)
            w.get_child().set_text(val or "/dev/ttyUSB0")
        elif kind == "baud":
            w = Gtk.ComboBoxText.new_with_entry()
            for b in (300, 1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600):
                w.append_text(str(b))
            w.get_child().set_text(str(val or 115200))
        elif kind == "color":
            w = Gtk.ColorButton()
            c = Gdk.RGBA()
            w.set_use_alpha(False)
            if val and c.parse(val):
                w.set_rgba(c)
            w.nx_set = bool(val)
            w.connect("color-set", lambda b: setattr(b, "nx_set", True))
        elif kind == "key":
            w = Gtk.Box(spacing=4)
            ent = Gtk.Entry(text=val, hexpand=True, placeholder_text="~/.ssh/id_ed25519 o keys/mi_clave")
            browse = Gtk.Button(label="Examinar…")
            browse.connect("clicked", self.on_browse, ent)
            w.pack_start(ent, True, True, 0)
            w.pack_start(browse, False, False, 0)
            w.entry = ent
        else:
            w = Gtk.Entry(text=str(val), hexpand=True)
        return w

    def value(self, key, w):
        kind = FIELD_DEFS[key][1]
        if kind == "bool":
            return w.get_active()
        if kind == "int":
            return int(w.get_value())
        if kind in ("serial",):
            return w.get_child().get_text().strip()
        if kind == "baud":
            t = w.get_child().get_text().strip()
            return int(t) if t.isdigit() else 115200
        if kind == "color":
            if not w.nx_set:
                return ""
            c = w.get_rgba()
            return "#{:02x}{:02x}{:02x}".format(int(c.red * 255), int(c.green * 255), int(c.blue * 255))
        if kind == "key":
            return w.entry.get_text().strip()
        return w.get_text().strip()

    def on_browse(self, _b, entry):
        dlg = Gtk.FileChooserNative(title="Elegir clave privada", transient_for=self,
                                    action=Gtk.FileChooserAction.OPEN)
        ssh_dir = os.path.expanduser("~/.ssh")
        if os.path.isdir(ssh_dir):
            dlg.set_current_folder(ssh_dir)
        if dlg.run() == Gtk.ResponseType.ACCEPT:
            entry.set_text(dlg.get_filename())
            if dlg.get_filename().endswith(".ppk"):
                self.portable.set_active(True)
        dlg.destroy()

    def collect(self):
        for key, w in self.widgets.items():
            self.session[key] = self.value(key, w)

    def on_type(self, btn, t):
        if not btn.get_active():
            return
        if self.widgets:
            self.collect()
        old = self.session.get("type")
        self.session["type"] = t
        if old != t and int(self.session.get("port") or 0) in (0, *DEFAULT_PORTS.values()):
            self.session["port"] = DEFAULT_PORTS.get(t, 0)
        for grid in (self.basic, self.adv, self.creds):
            for child in grid.get_children():
                grid.remove(child)
        keep = {k: self.widgets[k] for k in ("color", "log")}
        self.widgets = dict(keep)
        for i, key in enumerate(BASIC.get(t, [])):
            self.widgets[key] = self.make(key)
            self.attach(self.basic, i, FIELD_DEFS[key][0], self.widgets[key])
        adv = ADVANCED.get(t, [])
        for i, key in enumerate(adv):
            self.widgets[key] = self.make(key)
            self.attach(self.adv, i, FIELD_DEFS[key][0], self.widgets[key])
        if "key" in adv:
            self.adv.attach(self.portable, 1, len(adv), 1, 1)
        if not adv:
            self.adv.attach(Gtk.Label(label="Este tipo de sesión no tiene opciones avanzadas."), 0, 0, 2, 1)
        self.secret_entries = {}
        kinds = CREDS.get(t, ())
        for i, kind in enumerate(kinds):
            e = Gtk.Entry(visibility=False, hexpand=True, placeholder_text="(vacío = no guardar)")
            e.set_text(secret_get(self.session["id"], kind) or "")
            e.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, "view-reveal-symbolic")
            e.connect("icon-press", lambda en, *_: en.set_visibility(not en.get_visibility()))
            self.attach(self.creds, i, "Contraseña" if kind == "password" else "Passphrase de la clave", e)
            self.secret_entries[kind] = e
        note = Gtk.Label(wrap=True, xalign=0, max_width_chars=60)
        note.set_markup("<small>Se guardan cifradas en el llavero del sistema (GNOME Keyring), "
                        "nunca en los archivos de MerlynGestorTerm." + ("" if SCHEMA else " <b>libsecret no disponible.</b>")
                        + "</small>" if kinds else "<small>Este tipo de sesión no usa contraseñas guardadas.</small>")
        self.creds.attach(note, 0, len(kinds), 2, 1)
        if not self.name.get_text() or self.name.get_text() == "Nueva sesión":
            self.name.set_placeholder_text(TYPE_LABELS[t])
        self.basic.show_all()
        self.adv.show_all()
        self.creds.show_all()

    def result(self):
        self.collect()
        s = self.session
        name = self.name.get_text().strip()
        if name in ("", "Nueva sesión"):
            name = s.get("host") or s.get("url") or s.get("path") or TYPE_LABELS[s["type"]]
        s["name"] = name
        s["group"] = self.group.get_child().get_text().strip().strip("/")
        if s.get("key") and self.portable.get_active() and not key_path(s).startswith(KEYS_DIR):
            s["key"] = import_key(key_path(s))
        for kind, e in self.secret_entries.items():
            if SCHEMA:
                secret_set(s["id"], kind, e.get_text(), s["name"])
        return s


# ---------------------------------------------------------------- ajustes

class SettingsDialog(Gtk.Dialog):
    def __init__(self, win):
        super().__init__(title="Ajustes — MerlynGestorTerm", transient_for=win, modal=True)
        self.set_default_size(560, 440)
        self.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Aceptar", Gtk.ResponseType.OK)
        st = win.settings
        nb = Gtk.Notebook(margin=6)
        self.get_content_area().pack_start(nb, True, True, 0)
        self.w = {}

        def page(title, rows):
            grid = Gtk.Grid(column_spacing=10, row_spacing=8, margin=12)
            for i, (key, label, widget) in enumerate(rows):
                grid.attach(Gtk.Label(label=label, xalign=1), 0, i, 1, 1)
                grid.attach(widget, 1, i, 1, 1)
                self.w[key] = widget
            nb.append_page(grid, Gtk.Label(label=title))

        def check(key):
            c = Gtk.CheckButton()
            c.set_active(bool(st.get(key)))
            return c

        def combo(key, items):
            c = Gtk.ComboBoxText()
            for k, v in items:
                c.append(k, v)
            c.set_active_id(st.get(key))
            return c
        font = Gtk.FontButton(font=st["font"])
        scroll = Gtk.SpinButton.new_with_range(100, 1000000, 1000)
        scroll.set_value(st["scrollback"])
        keep = Gtk.SpinButton.new_with_range(0, 3600, 5)
        keep.set_value(st.get("keepalive", 60))
        page("General", [
            ("theme", "Tema de la ventana", combo("theme", [("system", "El del sistema"), ("light", "Claro"),
                                                            ("dark", "Oscuro")])),
            ("home_tab", "Mostrar pestaña de inicio", check("home_tab")),
            ("confirm_close", "Confirmar al salir con sesiones abiertas", check("confirm_close")),
            ("log_sessions", "Guardar registro de todas las sesiones", check("log_sessions")),
        ])
        page("Terminal", [
            ("font", "Fuente", font),
            ("palette", "Colores", combo("palette", [(k, k.capitalize()) for k in PALETTES])),
            ("cursor", "Cursor", combo("cursor", [("block", "Bloque"), ("ibeam", "Barra"),
                                                   ("underline", "Subrayado")])),
            ("scrollback", "Líneas de historial", scroll),
            ("copy_on_select", "Copiar al seleccionar", check("copy_on_select")),
            ("right_click_paste", "Clic derecho pega (sin menú)", check("right_click_paste")),
            ("bell", "Campana audible", check("bell")),
        ])
        page("SSH", [
            ("sftp_browser", "Mostrar explorador SFTP", check("sftp_browser")),
            ("remote_monitoring", "Barra de monitorización remota", check("remote_monitoring")),
            ("x11_default", "Reenvío X11 en sesiones nuevas", check("x11_default")),
            ("keepalive", "Keepalive (segundos, 0 = no)", keep),
        ])
        self.show_all()

    def apply(self, st):
        for key, w in self.w.items():
            if isinstance(w, Gtk.FontButton):
                st[key] = w.get_font()
            elif isinstance(w, Gtk.SpinButton):
                st[key] = int(w.get_value())
            elif isinstance(w, Gtk.ComboBoxText):
                st[key] = w.get_active_id()
            else:
                st[key] = w.get_active()


# ---------------------------------------------------------------- ventana

TOOLS = [
    # (id, etiqueta, icono, descripción)
    ("editor", "Editor de texto", "accessories-text-editor", "Editor con resaltado de sintaxis"),
    ("diff", "Comparar archivos", "edit-copy", "Diferencias entre archivos o carpetas"),
    ("netscan", "Escáner de red", "network-workgroup", "Busca equipos y servicios en tu red"),
    ("portscan", "Escáner de puertos", "system-search", "Puertos TCP abiertos en un host"),
    ("openports", "Puertos abiertos", "network-wired", "Puertos en escucha y conexiones (netstat)"),
    ("tunnels", "Túneles SSH", "network-vpn", "Redirección de puertos local, remota y SOCKS"),
    ("capture", "Captura de paquetes", "network-receive", "tcpdump en una pestaña"),
    ("wol", "Wake on LAN", "system-shutdown", "Encender equipos por red"),
    ("keygen", "Generador de claves SSH", "dialog-password", "Crear pares de claves"),
    ("passwords", "Contraseñas guardadas", "security-medium", "Credenciales del llavero"),
    ("packages", "Gestor de paquetes", "system-software-install", "Buscar e instalar paquetes"),
    ("admin", "Shell de administrador", "security-high", "Terminal como root (sudo -i)"),
    ("servers", "Servidores", "network-server", "FTP, TFTP, HTTP, SSH, Telnet, NFS, VNC, Cron, Iperf"),
    ("screensaver", "Salvapantallas", "preferences-desktop-screensaver", "Red animada a pantalla completa"),
    ("game", "Juego: Cortafuegos", "applications-games", "Bloquea los paquetes prohibidos"),
]


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.window = self
        self.set_default_size(1280, 800)
        self.settings = load_settings()
        self.sessions = load_json(SESSIONS_FILE, [])
        self.broadcasting = False
        self.macro_recording = None
        self.notebooks = []
        self.servers = servers.ServerManager(self)
        self.apply_css()

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(root)
        root.pack_start(self.build_menubar(), False, False, 0)
        self.ribbon = self.build_ribbon()
        root.pack_start(self.ribbon, False, False, 0)

        self.paned = Gtk.Paned()
        root.pack_start(self.paned, True, True, 0)
        self.sidebar = self.build_sidebar()
        self.paned.pack1(self.sidebar, False, False)
        self.paned.set_position(300)
        self.nb = self.new_notebook()
        self.paned.pack2(self.nb, True, False)

        self.status = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.status.get_style_context().add_class("nx-status")
        root.pack_start(self.status, False, False, 0)
        self.set_status("")

        self.add_shortcuts()
        self.refresh_tree()
        self.connect("delete-event", self.on_delete_event)
        self.show_all()
        if self.settings.get("home_tab"):
            self.open_home()
        else:
            self.on_local()
        tools.TunnelManager.autostart(self)

    # -- apariencia
    def apply_css(self):
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), prov,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        gs = Gtk.Settings.get_default()
        if not hasattr(MainWindow, "system_theme"):
            MainWindow.system_theme = (gs.get_property("gtk-theme-name"),
                                       gs.get_property("gtk-application-prefer-dark-theme"))
        theme = self.settings.get("theme", "system")
        if theme == "system":
            gs.set_property("gtk-theme-name", MainWindow.system_theme[0])
            gs.set_property("gtk-application-prefer-dark-theme", MainWindow.system_theme[1])
        else:
            gs.set_property("gtk-theme-name", "Adwaita")
            gs.set_property("gtk-application-prefer-dark-theme", theme == "dark")

    def set_status(self, text):
        data = DATA_DIR.replace(APP_DIR, ".", 1) if DATA_DIR.startswith(APP_DIR) else DATA_DIR
        base = (f"{APP_NAME} {VERSION} · modo portable · datos en {data} · "
                f"{len(self.sessions)} sesiones")
        n = self.servers.running_count()
        if n:
            base += f" · {n} servidor(es) activo(s)"
        self.status.set_text(text or base)

    # -- menú y cinta
    def build_menubar(self):
        bar = Gtk.MenuBar()

        def menu(title, items):
            top = Gtk.MenuItem(label=title)
            sub = Gtk.Menu()
            for it in items:
                if it is None:
                    sub.append(Gtk.SeparatorMenuItem())
                    continue
                mi = Gtk.MenuItem(label=it[0])
                mi.connect("activate", lambda _m, cb=it[1]: cb())
                sub.append(mi)
            top.set_submenu(sub)
            bar.append(top)
        menu("Terminal", [("Nueva shell local", self.on_local), ("Nueva sesión…", self.on_new), None,
                          ("Buscar en la terminal", lambda: self.pane_do(lambda p: p.search.set_search_mode(True))),
                          ("Guardar salida…", lambda: self.pane_do(self.save_output)),
                          ("Limpiar", lambda: self.pane_do(lambda p: p.term.reset(True, True))),
                          ("Reconectar", lambda: self.pane_do(lambda p: p.reconnect())), None,
                          ("Cerrar pestaña", self.close_current), ("Salir", self.quit)])
        menu("Sesiones", [("Nueva sesión…", self.on_new), ("Importar MobaXterm.ini…", self.on_import),
                          ("Exportar sesiones…", self.on_export), None,
                          ("Abrir carpeta de datos", lambda: Gio.AppInfo.launch_default_for_uri(
                              GLib.filename_to_uri(DATA_DIR), None))])
        menu("Vista", [("Pantalla completa (F11)", self.toggle_fullscreen),
                       ("Mostrar/ocultar barra lateral", self.toggle_sidebar),
                       ("Mostrar/ocultar cinta", lambda: self.ribbon.set_visible(not self.ribbon.get_visible())),
                       None, ("Acercar (Ctrl +)", lambda: self.pane_do(lambda p: p.zoom(1.1))),
                       ("Alejar (Ctrl -)", lambda: self.pane_do(lambda p: p.zoom(1 / 1.1))),
                       ("Tamaño normal (Ctrl 0)", lambda: self.pane_do(lambda p: p.term.set_font_scale(1))),
                       None, ("Cambiar tema (sistema/claro/oscuro)", self.toggle_theme)])
        menu("Servidor X", [("Estado del servidor X", self.on_xserver)])
        menu("Herramientas", [(t[1], lambda tid=t[0]: self.run_tool(tid)) for t in TOOLS if t[0] != "game"])
        menu("Juegos", [("Cortafuegos", lambda: self.run_tool("game"))])
        menu("Ajustes", [("Configuración…", self.on_settings), ("Atajos de teclado", self.on_shortcuts)])
        menu("Macros", [("Grabar / parar", lambda: self.macros.rec.set_active(not self.macros.rec.get_active())),
                        ("Gestionar macros", lambda: self.side_nb.set_current_page(2))])
        menu("Ayuda", [("Ayuda (README)", self.on_help), ("Atajos de teclado", self.on_shortcuts),
                       ("Acerca de MerlynGestorTerm", self.on_about)])
        return bar

    def build_ribbon(self):
        bar = Gtk.Box(spacing=0)
        bar.get_style_context().add_class("nx-ribbon")

        def big(label, icon, cb=None, toggle=False, menu=None):
            if menu is not None:
                b = Gtk.MenuButton(popup=menu)
            elif toggle:
                b = Gtk.ToggleButton()
            else:
                b = Gtk.Button()
            b.set_relief(Gtk.ReliefStyle.NONE)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            box.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.LARGE_TOOLBAR), False, False, 0)
            box.pack_start(Gtk.Label(label=label), False, False, 0)
            b.add(box)
            if cb:
                b.connect("toggled" if toggle else "clicked", lambda *_: cb())
            bar.pack_start(b, False, False, 0)
            return b

        def popup(items):
            m = Gtk.Menu()
            for it in items:
                if it is None:
                    m.append(Gtk.SeparatorMenuItem())
                    continue
                mi = Gtk.ImageMenuItem(label=it[0])
                if len(it) > 2:
                    mi.set_image(Gtk.Image.new_from_icon_name(it[2], Gtk.IconSize.MENU))
                    mi.set_always_show_image(True)
                mi.connect("activate", lambda _m, cb=it[1]: cb())
                m.append(mi)
            m.show_all()
            return m

        big("Sesión", "list-add", self.on_new)
        big("Servidores", "network-server", lambda: self.run_tool("servers"))
        big("Herramientas", "applications-utilities",
            menu=popup([(t[1], lambda tid=t[0]: self.run_tool(tid), t[2]) for t in TOOLS
                        if t[0] not in ("game", "servers")]))
        big("Juegos", "applications-games", menu=popup([("Cortafuegos", lambda: self.run_tool("game"))]))
        self.sessions_btn = big("Sesiones", "bookmark-new", menu=Gtk.Menu())
        self.sessions_btn.connect("pressed", lambda *_: self.sessions_btn.set_popup(self.sessions_menu()))
        bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)
        big("Vista", "view-app-grid-symbolic", menu=popup([
            ("Pantalla completa", self.toggle_fullscreen, "view-fullscreen"),
            ("Barra lateral", self.toggle_sidebar, "view-sidebar"),
            ("Cambiar tema (sistema/claro/oscuro)", self.toggle_theme, "weather-clear-night")]))
        big("Dividir", "view-dual-symbolic", menu=popup([
            ("Una terminal", lambda: self.tab_do(lambda t: t.unsplit()), "view-restore"),
            ("2 terminales en vertical", lambda: self.tab_do(lambda t: t.split(Gtk.Orientation.HORIZONTAL)),
             "view-dual-symbolic"),
            ("2 terminales en horizontal", lambda: self.tab_do(lambda t: t.split(Gtk.Orientation.VERTICAL)),
             "view-paged-symbolic"),
            ("4 terminales", lambda: self.tab_do(lambda t: t.split4()), "view-grid-symbolic")]))
        self.multiexec = big("MultiExec", "edit-select-all", self.on_multiexec, toggle=True)
        self.multiexec.set_tooltip_text("Escribir en todas las terminales a la vez")
        big("Túneles", "network-vpn", lambda: self.run_tool("tunnels"))
        big("Paquetes", "system-software-install", lambda: self.run_tool("packages"))
        big("Ajustes", "preferences-system", self.on_settings)
        big("Ayuda", "help-browser", self.on_help)
        quit_btn = Gtk.Button(relief=Gtk.ReliefStyle.NONE)
        qb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        qb.pack_start(Gtk.Image.new_from_icon_name("application-exit", Gtk.IconSize.LARGE_TOOLBAR), False, False, 0)
        qb.pack_start(Gtk.Label(label="Salir"), False, False, 0)
        quit_btn.add(qb)
        quit_btn.connect("clicked", lambda *_: self.quit())
        bar.pack_end(quit_btn, False, False, 0)
        self.xbtn = Gtk.Button(relief=Gtk.ReliefStyle.NONE)
        xb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        self.ximg = Gtk.Image.new_from_icon_name("video-display", Gtk.IconSize.LARGE_TOOLBAR)
        xb.pack_start(self.ximg, False, False, 0)
        xb.pack_start(Gtk.Label(label="Servidor X"), False, False, 0)
        self.xbtn.add(xb)
        self.xbtn.connect("clicked", lambda *_: self.on_xserver())
        self.xbtn.set_tooltip_text(f"DISPLAY={os.environ.get('DISPLAY', '(ninguno)')}")
        bar.pack_end(self.xbtn, False, False, 0)
        self.srv_btn = Gtk.Button(relief=Gtk.ReliefStyle.NONE, label="")
        self.srv_btn.connect("clicked", lambda *_: self.run_tool("servers"))
        bar.pack_end(self.srv_btn, False, False, 0)
        return bar

    def sessions_menu(self):
        m = Gtk.Menu()
        if not self.sessions:
            m.append(Gtk.MenuItem(label="(sin sesiones guardadas)"))
        groups = {}
        for s in sorted(self.sessions, key=lambda s: (s.get("group", ""), s["name"].lower())):
            g = s.get("group") or ""
            if g not in groups:
                if g:
                    gi = Gtk.MenuItem(label=g)
                    groups[g] = Gtk.Menu()
                    gi.set_submenu(groups[g])
                    m.append(gi)
                else:
                    groups[g] = m
            mi = Gtk.ImageMenuItem(label=s["name"])
            mi.set_image(Gtk.Image.new_from_icon_name(TYPE_ICONS.get(s["type"]), Gtk.IconSize.MENU))
            mi.set_always_show_image(True)
            mi.connect("activate", lambda _m, s=s: self.open_session(dict(s)))
            groups[g].append(mi)
        m.show_all()
        return m

    def update_server_indicator(self):
        n = self.servers.running_count()
        self.srv_btn.set_label(f"● {n} servidor(es)" if n else "")
        self.srv_btn.set_visible(bool(n))
        self.set_status("")

    # -- barra lateral
    def build_sidebar(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.quick = Gtk.Entry(placeholder_text="Conexión rápida: usuario@host[:puerto]", margin=4)
        self.quick.set_icon_from_icon_name(Gtk.EntryIconPosition.PRIMARY, "network-server-symbolic")
        self.quick.connect("activate", self.on_quick)
        box.pack_start(self.quick, False, False, 0)
        nb = self.side_nb = Gtk.Notebook(tab_pos=Gtk.PositionType.LEFT)
        nb.get_style_context().add_class("nx-side-tabs")

        def vtab(text, icon):
            b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            b.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.MENU), False, False, 0)
            lab = Gtk.Label(label=text, angle=90)
            b.pack_start(lab, False, False, 0)
            b.show_all()
            return b

        # Sesiones
        sess = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin=4)
        top = Gtk.Box(spacing=2)
        self.search = Gtk.SearchEntry(placeholder_text="Buscar sesión")
        self.search.connect("search-changed", lambda *_: self.filter.refilter())
        top.pack_start(self.search, True, True, 0)
        for icon, tip, cb in (("list-add-symbolic", "Nueva sesión", self.on_new),
                              ("folder-new-symbolic", "Nueva carpeta", self.on_new_folder)):
            b = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.MENU)
            b.set_tooltip_text(tip)
            b.connect("clicked", lambda *_, cb=cb: cb())
            top.pack_start(b, False, False, 0)
        sess.pack_start(top, False, False, 0)
        self.store = Gtk.TreeStore(str, str, str, str)  # icono, nombre, id, carpeta
        self.filter = self.store.filter_new()
        self.filter.set_visible_func(self.visible)
        self.tree = Gtk.TreeView(model=self.filter, headers_visible=False, enable_search=False)
        col = Gtk.TreeViewColumn()
        ri, rt = Gtk.CellRendererPixbuf(), Gtk.CellRendererText()
        col.pack_start(ri, False)
        col.pack_start(rt, True)
        col.add_attribute(ri, "icon-name", 0)
        col.add_attribute(rt, "text", 1)
        self.tree.append_column(col)
        self.tree.connect("row-activated", self.on_row)
        self.tree.connect("button-press-event", self.on_tree_click)
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.add(self.tree)
        sess.pack_start(sw, True, True, 0)
        nb.append_page(sess, vtab("Sesiones", "starred-symbolic"))

        # Herramientas
        tl = Gtk.ListBox()
        for tid, label, icon, desc in TOOLS:
            row = Gtk.ListBoxRow()
            row.tid = tid
            h = Gtk.Box(spacing=8, margin=4)
            h.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.LARGE_TOOLBAR), False, False, 0)
            lab = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
            lab.set_markup(f"{GLib.markup_escape_text(label)}\n<small>{GLib.markup_escape_text(desc)}</small>")
            h.pack_start(lab, True, True, 0)
            row.add(h)
            tl.add(row)
        tl.connect("row-activated", lambda _l, row: self.run_tool(row.tid))
        sw = Gtk.ScrolledWindow()
        sw.add(tl)
        nb.append_page(sw, vtab("Herramientas", "applications-utilities-symbolic"))

        # Macros
        self.macros = tools.MacroPanel(self)
        nb.append_page(self.macros, vtab("Macros", "media-record-symbolic"))

        # SFTP: muestra el explorador de la pestaña SSH activa
        self.sftp_stack = Gtk.Stack()
        ph = Gtk.Label(label="Abre una sesión SSH para ver\naquí los archivos del servidor.",
                       justify=Gtk.Justification.CENTER)
        ph.get_style_context().add_class("dim-label")
        self.sftp_stack.add_named(ph, "empty")
        nb.append_page(self.sftp_stack, vtab("SFTP", "folder-remote-symbolic"))
        box.pack_start(nb, True, True, 0)
        return box

    def visible(self, model, it, _data):
        q = self.search.get_text().lower()
        if not q:
            return True
        if model[it][2]:
            s = self.find(model[it][2])
            return q in model[it][1].lower() or bool(s and q in (s.get("host") or "").lower())
        child = model.iter_children(it)
        while child:
            if self.visible(model, child, None):
                return True
            child = model.iter_next(child)
        return False

    def refresh_tree(self):
        self.store.clear()
        folders = {}

        def folder(path):
            if not path:
                return None
            if path not in folders:
                parent, _, name = path.rpartition("/")
                folders[path] = self.store.append(folder(parent), ["folder", name, "", path])
            return folders[path]
        for g in load_json(SESSIONS_FILE + ".folders", []):
            folder(g)
        for s in sorted(self.sessions, key=lambda s: (s.get("group", ""), s["name"].lower())):
            g = s.get("group", "").strip("/")
            self.store.append(folder(g), [TYPE_ICONS.get(s["type"], "network-server"), s["name"], s["id"], g])
        self.tree.expand_all()
        self.set_status("")

    # -- pestañas
    def new_notebook(self):
        nb = Gtk.Notebook(scrollable=True)
        nb.set_group_name("merlyngestorterm")
        nb.connect("switch-page", self.on_switch_page)
        nb.connect("create-window", self.on_create_window)
        nb.connect("page-removed", self.on_page_removed)
        plus = Gtk.Button.new_from_icon_name("tab-new-symbolic", Gtk.IconSize.MENU)
        plus.set_relief(Gtk.ReliefStyle.NONE)
        plus.set_tooltip_text("Nueva terminal local")
        plus.connect("clicked", lambda *_: self.on_local())
        plus.show()
        nb.set_action_widget(plus, Gtk.PackType.END)
        self.notebooks.append(nb)
        return nb

    def on_create_window(self, _nb, _page, x, y):
        win = Gtk.Window(title=f"{APP_NAME} — pestaña separada", transient_for=None)
        win.set_default_size(900, 600)
        win.move(x, y)
        nb = self.new_notebook()
        win.add(nb)
        win.connect("destroy", lambda *_: self.notebooks.remove(nb) if nb in self.notebooks else None)
        win.show_all()
        return nb

    def on_page_removed(self, nb, _child, _num):
        if nb is not self.nb and nb.get_n_pages() == 0:
            top = nb.get_toplevel()
            GLib.idle_add(lambda: top.destroy() and False)

    def tabs(self):
        return [nb.get_nth_page(i) for nb in self.notebooks for i in range(nb.get_n_pages())
                if isinstance(nb.get_nth_page(i), TermTab)]

    def all_panes(self):
        return [p for t in self.tabs() for p in t.panes]

    def current_tab(self):
        page = self.nb.get_nth_page(self.nb.get_current_page())
        return page if isinstance(page, TermTab) else None

    def active_pane(self):
        t = self.current_tab()
        return t.active if t else None

    def tab_do(self, fn):
        t = self.current_tab()
        if t and t.panes:
            fn(t)

    def pane_do(self, fn):
        p = self.active_pane()
        if p:
            fn(p)

    def add_tab(self, widget, label_widget):
        idx = self.nb.append_page(widget, label_widget)
        self.nb.set_tab_reorderable(widget, True)
        self.nb.set_tab_detachable(widget, True)
        widget.show_all()
        self.nb.set_current_page(idx)
        return idx

    def tab_header(self, tab, icon, label, color=""):
        head = Gtk.Box(spacing=4)
        head.pack_start(icon, False, False, 0)
        head.pack_start(label, True, True, 0)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.connect("clicked", lambda *_: self.close_tab(tab))
        head.pack_start(close, False, False, 0)
        if color:
            prov = Gtk.CssProvider()
            prov.load_from_data(f"box {{ border-bottom: 3px solid {color}; }}".encode())
            head.get_style_context().add_provider(prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        head.show_all()
        return head

    def open_session(self, s):
        if s["type"] == "browser":
            url = s.get("url") or s.get("host")
            if url:
                Gio.AppInfo.launch_default_for_uri(url if "://" in url else "https://" + url, None)
            return
        if s["type"] == "ssh" and "x11" not in s:
            s["x11"] = self.settings.get("x11_default")
        if s.get("id") and any(x["id"] == s["id"] for x in self.sessions):
            recent_add(s)
        tab = TermTab(self, s)
        if tab.browser and s["type"] not in BROWSER_TYPES:
            self.sftp_stack.add_named(tab.browser, str(id(tab)))
            tab.browser.show_all()
        self.add_tab(tab, self.tab_header(tab, tab.icon, tab.label, s.get("color", "")))
        if tab.browser and s["type"] not in BROWSER_TYPES:
            self.sftp_stack.set_visible_child(tab.browser)
            self.side_nb.set_current_page(3)
        tab.focus()

    def open_home(self):
        for nb in self.notebooks:
            for i in range(nb.get_n_pages()):
                if getattr(nb.get_nth_page(i), "is_home", False):
                    nb.set_current_page(i)
                    return
        page = self.build_home()
        page.is_home = True
        icon = Gtk.Image.new_from_icon_name("go-home-symbolic", Gtk.IconSize.MENU)
        self.add_tab(page, self.tab_header(page, icon, Gtk.Label(label="Inicio")))

    def build_home(self):
        outer = Gtk.ScrolledWindow()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, halign=Gtk.Align.CENTER,
                      valign=Gtk.Align.CENTER, margin=40)
        outer.add(box)
        title = Gtk.Label(label=APP_NAME)
        title.get_style_context().add_class("nx-home-title")
        sub = Gtk.Label(label="Terminal, SSH, SFTP, RDP, VNC y herramientas de red — portable para Linux")
        sub.get_style_context().add_class("nx-home-sub")
        box.pack_start(title, False, False, 0)
        box.pack_start(sub, False, False, 0)
        row = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        for label, icon, cb in (("Iniciar terminal local", "utilities-terminal", self.on_local),
                                ("Nueva sesión", "list-add", self.on_new),
                                ("Servidores", "network-server", lambda: self.run_tool("servers")),
                                ("Herramientas", "applications-utilities", lambda: self.side_nb.set_current_page(1))):
            b = Gtk.Button()
            bb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            bb.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.DIALOG), False, False, 0)
            bb.pack_start(Gtk.Label(label=label), False, False, 0)
            b.add(bb)
            b.get_style_context().add_class("nx-big")
            b.connect("clicked", lambda *_, cb=cb: cb())
            row.pack_start(b, False, False, 0)
        box.pack_start(row, False, False, 0)
        search = Gtk.SearchEntry(placeholder_text="Buscar una sesión guardada o escribir usuario@host",
                                 width_chars=50, halign=Gtk.Align.CENTER)
        search.connect("activate", self.on_home_search)
        box.pack_start(search, False, False, 0)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        card.get_style_context().add_class("nx-home-card")
        head = Gtk.Label(xalign=0)
        head.set_markup("<b>Sesiones recientes</b>")
        card.pack_start(head, False, False, 0)
        recent = load_json(RECENT_FILE, [])
        if not recent:
            card.pack_start(Gtk.Label(label="Todavía no has abierto ninguna sesión guardada.", xalign=0),
                            False, False, 0)
        for s in recent[:8]:
            b = Gtk.Button(relief=Gtk.ReliefStyle.NONE)
            h = Gtk.Box(spacing=8)
            h.pack_start(Gtk.Image.new_from_icon_name(TYPE_ICONS.get(s["type"]), Gtk.IconSize.LARGE_TOOLBAR),
                         False, False, 0)
            lab = Gtk.Label(xalign=0)
            lab.set_markup(f"{GLib.markup_escape_text(s['name'])}  <small>{TYPE_LABELS.get(s['type'], '')} "
                           f"{GLib.markup_escape_text(ssh_target(s) if s.get('host') else '')}</small>")
            h.pack_start(lab, True, True, 0)
            b.add(h)
            b.connect("clicked", lambda *_, s=s: self.open_session(dict(self.find(s["id"]) or s)))
            card.pack_start(b, False, False, 0)
        box.pack_start(card, False, False, 0)
        tips = Gtk.Label(xalign=0, wrap=True, max_width_chars=80)
        tips.set_markup("<small>Consejos: clic derecho en una sesión para SFTP, copiar tu clave o duplicarla · "
                        "Ctrl+Shift+F busca en la terminal · MultiExec escribe en todas las terminales · "
                        "arrastra una pestaña fuera de la ventana para separarla.</small>")
        tips.get_style_context().add_class("dim-label")
        box.pack_start(tips, False, False, 0)
        return outer

    def on_home_search(self, entry):
        q = entry.get_text().strip().lower()
        match = next((s for s in self.sessions if q and (q in s["name"].lower() or q == (s.get("host") or "").lower())),
                     None)
        if match:
            self.open_session(dict(match))
        elif q:
            self.quick.set_text(entry.get_text())
            self.on_quick(self.quick)
        entry.set_text("")

    def on_switch_page(self, _nb, page, _num):
        if isinstance(page, TermTab) and page.browser and page.session["type"] not in BROWSER_TYPES:
            self.sftp_stack.set_visible_child(page.browser)
        elif self.sftp_stack.get_child_by_name("empty"):
            self.sftp_stack.set_visible_child_name("empty")
        if isinstance(page, TermTab):
            GLib.idle_add(lambda: page.focus() and False)

    def close_tab(self, tab):
        for nb in self.notebooks:
            n = nb.page_num(tab)
            if n >= 0:
                nb.remove_page(n)
        if isinstance(tab, TermTab):
            for p in tab.panes:
                if not p.exited and (p.session.get("log") or self.settings.get("log_sessions")):
                    p.save_log()
            if tab.browser and tab.browser.get_parent() is self.sftp_stack:
                self.sftp_stack.remove(tab.browser)
                tab.browser.destroy()
        tab.destroy()

    def close_current(self, *_):
        page = self.nb.get_nth_page(self.nb.get_current_page())
        if page:
            self.close_tab(page)

    def save_output(self, pane):
        dlg = Gtk.FileChooserNative(title="Guardar salida de la terminal", transient_for=self,
                                    action=Gtk.FileChooserAction.SAVE, do_overwrite_confirmation=True)
        dlg.set_current_name(f"{pane.session['name']}.txt")
        if dlg.run() == Gtk.ResponseType.ACCEPT:
            self.set_status(f"Salida guardada en {pane.save_log(dlg.get_filename())}")
        dlg.destroy()

    # -- atajos
    def add_shortcuts(self):
        accel = Gtk.AccelGroup()
        self.add_accel_group(accel)
        for key, cb in (("<Control><Shift>n", self.on_new), ("<Control><Shift>t", self.on_local),
                        ("<Control><Shift>w", self.close_current), ("F11", self.toggle_fullscreen),
                        ("<Control>Page_Down", lambda: self.nb.next_page()),
                        ("<Control>Page_Up", lambda: self.nb.prev_page()),
                        ("<Control><Shift>b", self.toggle_sidebar),
                        ("<Control><Shift>m", lambda: self.multiexec.set_active(not self.multiexec.get_active())),
                        ("<Control><Shift>d", lambda: self.tab_do(lambda t: t.split(Gtk.Orientation.HORIZONTAL))),
                        ("<Control><Shift>e", lambda: self.tab_do(lambda t: t.split(Gtk.Orientation.VERTICAL)))):
            k, m = Gtk.accelerator_parse(key)
            accel.connect(k, m, 0, lambda *a, cb=cb: (cb(), True)[1])

    # -- acciones de sesiones
    def save(self):
        save_json(SESSIONS_FILE, self.sessions)
        self.set_status("")

    def find(self, sid):
        return next((s for s in self.sessions if s["id"] == sid), None)

    def on_local(self, *_):
        self.open_session(new_session(name="Terminal local", type="shell"))

    def on_quick(self, entry):
        text = entry.get_text().strip()
        if not text:
            return
        proto, sep, rest = text.partition("://")
        t = proto if sep and proto in TYPES else "ssh"
        text = rest if sep else text
        user, _, hostport = text.rpartition("@")
        host, _, port = hostport.partition(":")
        self.open_session(new_session(name=text, type=t, host=host, user=user,
                                      port=int(port) if port.isdigit() else DEFAULT_PORTS.get(t, 22)))
        entry.set_text("")

    def edit(self, session):
        dlg = SessionDialog(self, session)
        while True:
            if dlg.run() != Gtk.ResponseType.OK:
                result = None
                break
            try:
                result = dlg.result()
                break
            except (OSError, RuntimeError, subprocess.CalledProcessError, GLib.Error) as e:
                tools.message(dlg, "No se pudo guardar", str(e), Gtk.MessageType.ERROR)
        dlg.destroy()
        return result

    def on_new(self, *_, group=""):
        s = self.edit(new_session(group=group, x11=self.settings.get("x11_default")))
        if s:
            self.sessions.append(s)
            self.save()
            self.refresh_tree()
            self.open_session(dict(s))

    def on_new_folder(self, parent=""):
        d = Gtk.Dialog(title="Nueva carpeta", transient_for=self, modal=True)
        d.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Crear", Gtk.ResponseType.OK)
        d.set_default_response(Gtk.ResponseType.OK)
        e = Gtk.Entry(activates_default=True, margin=10, text=(parent + "/") if parent else "")
        d.get_content_area().add(e)
        d.show_all()
        if d.run() == Gtk.ResponseType.OK and e.get_text().strip("/ "):
            folders = load_json(SESSIONS_FILE + ".folders", [])
            folders.append(e.get_text().strip("/ "))
            save_json(SESSIONS_FILE + ".folders", sorted(set(folders)))
            self.refresh_tree()
        d.destroy()

    def on_row(self, tree, path, _col):
        s = self.find(tree.get_model()[path][2])
        if s:
            self.open_session(dict(s))
        elif tree.row_expanded(path):
            tree.collapse_row(path)
        else:
            tree.expand_row(path, False)

    def on_tree_click(self, tree, ev):
        if ev.button != 3:
            return False
        hit = tree.get_path_at_pos(int(ev.x), int(ev.y))
        row = tree.get_model()[hit[0]] if hit else None
        s = self.find(row[2]) if row else None
        folder = row[3] if row and not row[2] else ""
        items = []
        if s:
            items = [("Conectar", lambda *_: self.open_session(dict(s))),
                     ("Editar sesión", lambda *_: self.on_edit(s)),
                     ("Duplicar", lambda *_: self.on_dup(s)),
                     ("Eliminar", lambda *_: self.on_delete(s)), None]
            if s["type"] == "ssh":
                items[1:1] = [("Abrir explorador SFTP", lambda *_: self.open_session(
                    {**s, "type": "sftp", "name": s["name"] + " (SFTP)"})),
                    ("Abrir en 2 paneles", lambda *_: (self.open_session(dict(s)),
                                                       self.tab_do(lambda t: t.split(Gtk.Orientation.HORIZONTAL)))),
                    ("Instalar mi clave en el servidor (ssh-copy-id)", lambda *_: self.open_session(
                        {**s, "type": "copyid", "name": s["name"] + " (copy-id)"}))]
        items += [("Nueva sesión aquí", lambda *_: self.on_new(group=folder or (s or {}).get("group", ""))),
                  ("Nueva carpeta", lambda *_: self.on_new_folder(folder))]
        if folder:
            items.append(("Eliminar carpeta (si está vacía)", lambda *_: self.on_delete_folder(folder)))
        menu = Gtk.Menu()
        for it in items:
            if it is None:
                menu.append(Gtk.SeparatorMenuItem())
                continue
            mi = Gtk.MenuItem(label=it[0])
            mi.connect("activate", it[1])
            menu.append(mi)
        menu.show_all()
        menu.popup_at_pointer(ev)
        return True

    def on_delete_folder(self, folder):
        if any((s.get("group") or "").startswith(folder) for s in self.sessions):
            tools.message(self, "La carpeta no está vacía.")
            return
        folders = [f for f in load_json(SESSIONS_FILE + ".folders", []) if not f.startswith(folder)]
        save_json(SESSIONS_FILE + ".folders", folders)
        self.refresh_tree()

    def on_edit(self, s):
        res = self.edit(s)
        if res:
            s.update(res)
            self.save()
            self.refresh_tree()

    def on_dup(self, s):
        copy = {**s, "id": uuid.uuid4().hex, "name": s["name"] + " (copia)"}
        for kind in ("password", "passphrase"):
            value = secret_get(s["id"], kind)
            if value:
                secret_set(copy["id"], kind, value, copy["name"])
        self.sessions.append(copy)
        self.save()
        self.refresh_tree()

    def on_delete(self, s):
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                              buttons=Gtk.ButtonsType.YES_NO, text=f"¿Eliminar «{s['name']}»?")
        if d.run() == Gtk.ResponseType.YES:
            if SCHEMA:
                for kind in ("password", "passphrase"):
                    secret_set(s["id"], kind, "", s["name"])
            self.sessions.remove(s)
            self.save()
            self.refresh_tree()
        d.destroy()

    def on_import(self, *_):
        dlg = Gtk.FileChooserNative(title="Importar MobaXterm.ini", transient_for=self,
                                    action=Gtk.FileChooserAction.OPEN)
        flt = Gtk.FileFilter()
        flt.set_name("MobaXterm.ini / sesiones .mxtsessions")
        flt.add_pattern("*.ini")
        flt.add_pattern("*.mxtsessions")
        dlg.add_filter(flt)
        if dlg.run() == Gtk.ResponseType.ACCEPT:
            try:
                found = import_mobaxterm_ini(dlg.get_filename())
                for s in found:
                    if s["type"] == "browser":
                        s["url"] = s["host"]
                self.sessions.extend(found)
                self.save()
                self.refresh_tree()
                tools.message(self, f"Importadas {len(found)} sesiones.")
            except (OSError, configparser.Error) as e:
                tools.message(self, "No se pudo importar", str(e), Gtk.MessageType.ERROR)
        dlg.destroy()

    def on_export(self, *_):
        dlg = Gtk.FileChooserNative(title="Exportar sesiones", transient_for=self,
                                    action=Gtk.FileChooserAction.SAVE, do_overwrite_confirmation=True)
        dlg.set_current_name("merlyngestorterm-sesiones.json")
        if dlg.run() == Gtk.ResponseType.ACCEPT:
            save_json(dlg.get_filename(), self.sessions)
            self.set_status(f"Sesiones exportadas (sin contraseñas) a {dlg.get_filename()}")
        dlg.destroy()

    # -- herramientas
    def run_tool(self, tid):
        actions = {
            "editor": lambda: tools.TextEditor.open_file(self), "diff": lambda: tools.DiffTool(self),
            "netscan": lambda: tools.NetworkScanner(self), "portscan": lambda: tools.PortScanner(self),
            "openports": lambda: tools.OpenPorts(self), "tunnels": lambda: tools.TunnelManager(self),
            "capture": lambda: tools.PacketCapture(self), "wol": lambda: tools.WakeOnLan(self),
            "keygen": lambda: tools.KeyGenerator(self), "passwords": lambda: tools.PasswordManager(self),
            "packages": lambda: tools.PackageManager(self),
            "admin": lambda: self.open_session(new_session(name="root", type="shell", command="sudo -i")),
            "servers": lambda: servers.ServersWindow(self), "screensaver": lambda: tools.Screensaver(self),
            "game": lambda: games.FirewallGame(self),
        }
        actions[tid]()

    # -- vista
    def toggle_fullscreen(self, *_):
        if self.get_window() and self.get_window().get_state() & Gdk.WindowState.FULLSCREEN:
            self.unfullscreen()
        else:
            self.fullscreen()

    def toggle_sidebar(self, *_):
        self.sidebar.set_visible(not self.sidebar.get_visible())

    def toggle_theme(self, *_):
        order = ["system", "light", "dark"]
        self.settings["theme"] = order[(order.index(self.settings.get("theme", "system")) + 1) % 3]
        self.set_status(f"Tema: {dict(system='del sistema', light='claro', dark='oscuro')[self.settings['theme']]}")
        save_json(SETTINGS_FILE, self.settings)
        self.apply_css()

    def on_multiexec(self):
        on = self.multiexec.get_active()
        self.set_status("MultiExec activado: lo que escribas se envía a TODAS las terminales" if on else "")

    def on_settings(self, *_):
        d = SettingsDialog(self)
        if d.run() == Gtk.ResponseType.OK:
            d.apply(self.settings)
            save_json(SETTINGS_FILE, self.settings)
            self.apply_css()
            for p in self.all_panes():
                p.apply_settings()
        d.destroy()

    def on_xserver(self):
        disp = os.environ.get("DISPLAY")
        way = os.environ.get("WAYLAND_DISPLAY")
        tools.message(self, "Servidor X", (
            f"En Linux no hace falta un servidor X propio: el escritorio ya lo ofrece.\n\n"
            f"Wayland: {way or 'no'}\nX11 (XWayland): {disp or 'no disponible'}\n\n"
            "Las sesiones SSH con «Reenvío X11» (-Y) muestran las aplicaciones gráficas remotas "
            "directamente en tu escritorio." + ("" if disp else
                                                 "\n\nInstala xorg-xwayland para usar apps X11 remotas.")))

    def on_shortcuts(self, *_):
        tools.message(self, "Atajos de teclado", (
            "Ctrl+Shift+T  nueva terminal local\nCtrl+Shift+N  nueva sesión\nCtrl+Shift+W  cerrar pestaña\n"
            "Ctrl+RePág / AvPág  cambiar de pestaña\nCtrl+Shift+D / E  dividir vertical / horizontal\n"
            "Ctrl+Shift+M  MultiExec\nCtrl+Shift+B  barra lateral\nCtrl+Shift+F  buscar en la terminal\n"
            "Ctrl+Shift+C / V  copiar / pegar\nCtrl + / - / 0  zoom\nF11  pantalla completa\n"
            "R  reconectar una sesión terminada"))

    def on_help(self, *_):
        readme = os.path.join(os.path.dirname(os.path.realpath(__file__)), "README.md")
        tools.TextEditor.open_file(self, readme, title="Ayuda — README")

    def on_about(self, *_):
        d = Gtk.AboutDialog(transient_for=self, modal=True, program_name=APP_NAME, version=VERSION,
                            comments="Terminal portable con sesiones remotas, explorador SFTP, servidores "
                                     "y herramientas de red para Linux.",
                            logo_icon_name="utilities-terminal")
        d.run()
        d.destroy()

    # -- salida
    def on_delete_event(self, *_):
        return not self.confirm_quit()

    def confirm_quit(self):
        alive = [p for p in self.all_panes() if not p.exited and p.session["type"] != "shell"]
        if alive and self.settings.get("confirm_close"):
            d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                  buttons=Gtk.ButtonsType.YES_NO,
                                  text=f"Hay {len(alive)} sesión(es) remota(s) abierta(s). ¿Salir?")
            ok = d.run() == Gtk.ResponseType.YES
            d.destroy()
            if not ok:
                return False
        for t in self.tabs():
            for p in t.panes:
                if not p.exited and (p.session.get("log") or self.settings.get("log_sessions")):
                    p.save_log()
        self.servers.stop_all()
        tools.TunnelManager.stop_all()
        return True

    def quit(self):
        if self.confirm_quit():
            self.get_application().quit()
