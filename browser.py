"""Explorador de archivos con backends intercambiables: SFTP (sobre el
ControlMaster de la terminal SSH), FTP/FTPS, Amazon S3 (aws cli) y local."""
import ftplib
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import threading
import time

import gtkver  # noqa: F401
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

from core import control_dir, secret_get, ssh_opts, ssh_target


def sftp_quote(path):
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def human(n):
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


class Entry:
    __slots__ = ("name", "is_dir", "size", "mtime", "perms")

    def __init__(self, name, is_dir, size=0, mtime="", perms=""):
        self.name, self.is_dir, self.size, self.mtime, self.perms = name, is_dir, size, mtime, perms


# ---------------------------------------------------------------- backends

class Backend:
    """Interfaz común. Todas las operaciones se llaman desde un hilo."""
    can_chmod = True

    def ready(self):
        return True

    def join(self, d, name):
        return name if name.startswith("/") else f"{d.rstrip('/')}/{name}"

    def parent(self, d):
        return os.path.dirname(d.rstrip("/")) or "/"


class SftpBackend(Backend):
    def __init__(self, s):
        self.s = s
        self.lock = threading.Lock()

    def ready(self):
        cmd = ["ssh"] + ssh_opts(self.s, "-p") + ["-O", "check", ssh_target(self.s)]
        return subprocess.run(cmd, capture_output=True).returncode == 0

    def run(self, commands):
        cmd = ["sftp", "-q"] + ssh_opts(self.s, "-P") + ["-o", "ControlMaster=no", "-b", "-",
                                                         ssh_target(self.s)]
        with self.lock:
            p = subprocess.run(cmd, input="\n".join(commands) + "\n", capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(p.stderr.strip() or "error de sftp")
        return [l for l in p.stdout.splitlines() if not l.startswith("sftp> ")]

    def list(self, path):
        out = self.run(([f"cd {sftp_quote(path)}"] if path else []) + ["pwd", "ls -la"])
        cwd, entries = path, []
        for i, line in enumerate(out):
            if line.startswith("Remote working directory: "):
                cwd = line.split(": ", 1)[1]
                out = out[i + 1:]
                break
        for line in out:
            parts = line.split(None, 8)
            if len(parts) < 9 or parts[8] in (".", ".."):
                continue
            perms, size, name = parts[0], parts[4], parts[8]
            if perms.startswith("l"):
                name = name.split(" -> ", 1)[0]
            entries.append(Entry(name, perms.startswith(("d", "l")), int(size) if size.isdigit() else 0,
                                 " ".join(parts[5:8]), perms))
        return cwd, entries

    def upload(self, paths, d):
        self.run([f"cd {sftp_quote(d)}"] + [f"put -r {sftp_quote(p)}" for p in paths])

    def download(self, items, local):
        self.run([f"lcd {sftp_quote(local)}"] +
                 [f"get {'-r ' if e.is_dir else ''}{sftp_quote(p)}" for p, e in items])

    def mkdir(self, p):
        self.run([f"mkdir {sftp_quote(p)}"])

    def rename(self, a, b):
        self.run([f"rename {sftp_quote(a)} {sftp_quote(b)}"])

    def chmod(self, p, mode):
        self.run([f"chmod {mode} {sftp_quote(p)}"])

    def delete(self, items):
        files = [f"rm {sftp_quote(p)}" for p, e in items if not e.is_dir]
        if files:
            self.run(files)
        dirs = [p for p, e in items if e.is_dir]
        if dirs:
            # sftp no borra carpetas con contenido: misma conexión, rm -rf
            cmd = ["ssh"] + ssh_opts(self.s, "-p") + ["-o", "ControlMaster=no", ssh_target(self.s),
                                                       "rm -rf -- " + shlex.join(dirs)]
            p = subprocess.run(cmd, capture_output=True, text=True)
            if p.returncode:
                raise RuntimeError(p.stderr.strip())

    def get(self, remote, local):
        self.run([f"get {sftp_quote(remote)} {sftp_quote(local)}"])

    def put(self, local, remote):
        self.run([f"put {sftp_quote(local)} {sftp_quote(remote)}"])


class FtpBackend(Backend):
    def __init__(self, s):
        self.s = s
        self.ftp = None
        self.lock = threading.RLock()

    def conn(self):
        if self.ftp:
            try:
                self.ftp.voidcmd("NOOP")
                return self.ftp
            except (ftplib.all_errors):
                self.ftp = None
        tls = bool(self.s.get("tls"))
        ftp = ftplib.FTP_TLS() if tls else ftplib.FTP()
        ftp.connect(self.s["host"], int(self.s.get("port") or 21), timeout=20)
        ftp.login(self.s.get("user") or "anonymous", secret_get(self.s["id"], "password") or "")
        if tls:
            ftp.prot_p()
        ftp.set_pasv(True)
        ftp.encoding = "utf-8"
        self.ftp = ftp
        return ftp

    def list(self, path):
        with self.lock:
            ftp = self.conn()
            if path:
                ftp.cwd(path)
            cwd = ftp.pwd()
            entries = []
            try:
                for name, facts in ftp.mlsd(facts=["type", "size", "modify", "unix.mode"]):
                    if facts.get("type") in ("cdir", "pdir") or name in (".", ".."):
                        continue
                    m = facts.get("modify", "")
                    mtime = f"{m[:4]}-{m[4:6]}-{m[6:8]} {m[8:10]}:{m[10:12]}" if len(m) >= 12 else ""
                    entries.append(Entry(name, facts.get("type") == "dir", int(facts.get("size", 0) or 0),
                                         mtime, facts.get("unix.mode", "")))
            except ftplib.error_perm:
                lines = []
                ftp.retrlines("LIST", lines.append)
                for line in lines:
                    parts = line.split(None, 8)
                    if len(parts) < 9 or parts[8] in (".", ".."):
                        continue
                    entries.append(Entry(parts[8].split(" -> ")[0], parts[0].startswith(("d", "l")),
                                         int(parts[4]) if parts[4].isdigit() else 0,
                                         " ".join(parts[5:8]), parts[0]))
            return cwd, entries

    def _put_tree(self, ftp, local, remote):
        if os.path.isdir(local):
            try:
                ftp.mkd(remote)
            except ftplib.error_perm:
                pass
            for name in os.listdir(local):
                self._put_tree(ftp, os.path.join(local, name), f"{remote}/{name}")
        else:
            with open(local, "rb") as f:
                ftp.storbinary(f"STOR {remote}", f)

    def _get_tree(self, ftp, remote, local, is_dir):
        if is_dir:
            os.makedirs(local, exist_ok=True)
            for name, facts in ftp.mlsd(remote, facts=["type"]):
                if facts.get("type") in ("dir", "file"):
                    self._get_tree(ftp, f"{remote}/{name}", os.path.join(local, name),
                                   facts["type"] == "dir")
        else:
            with open(local, "wb") as f:
                ftp.retrbinary(f"RETR {remote}", f.write)

    def upload(self, paths, d):
        with self.lock:
            ftp = self.conn()
            for p in paths:
                self._put_tree(ftp, p, self.join(d, os.path.basename(p.rstrip("/"))))

    def download(self, items, local):
        with self.lock:
            ftp = self.conn()
            for p, e in items:
                self._get_tree(ftp, p, os.path.join(local, e.name), e.is_dir)

    def mkdir(self, p):
        with self.lock:
            self.conn().mkd(p)

    def rename(self, a, b):
        with self.lock:
            self.conn().rename(a, b)

    def chmod(self, p, mode):
        with self.lock:
            self.conn().sendcmd(f"SITE CHMOD {mode} {p}")

    def _rm_tree(self, ftp, p):
        for name, facts in ftp.mlsd(p, facts=["type"]):
            if facts.get("type") == "dir":
                self._rm_tree(ftp, f"{p}/{name}")
            elif facts.get("type") == "file":
                ftp.delete(f"{p}/{name}")
        ftp.rmd(p)

    def delete(self, items):
        with self.lock:
            ftp = self.conn()
            for p, e in items:
                self._rm_tree(ftp, p) if e.is_dir else ftp.delete(p)

    def get(self, remote, local):
        with self.lock:
            self._get_tree(self.conn(), remote, local, False)

    def put(self, local, remote):
        with self.lock:
            self._put_tree(self.conn(), local, remote)


class LocalBackend(Backend):
    def list(self, path):
        path = os.path.abspath(os.path.expanduser(path or "~"))
        entries = []
        with os.scandir(path) as it:
            for de in it:
                try:
                    st = de.stat()
                except OSError:
                    continue
                entries.append(Entry(de.name, de.is_dir(), st.st_size,
                                     time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                                     stat.filemode(st.st_mode)))
        return path, entries

    def upload(self, paths, d):
        for p in paths:
            dst = os.path.join(d, os.path.basename(p.rstrip("/")))
            shutil.copytree(p, dst) if os.path.isdir(p) else shutil.copy2(p, dst)

    def download(self, items, local):
        self.upload([p for p, _ in items], local)

    def mkdir(self, p):
        os.mkdir(p)

    def rename(self, a, b):
        os.rename(a, b)

    def chmod(self, p, mode):
        os.chmod(p, int(mode, 8))

    def delete(self, items):
        for p, e in items:
            shutil.rmtree(p) if e.is_dir else os.remove(p)

    def get(self, remote, local):
        shutil.copy2(remote, local)

    def put(self, local, remote):
        shutil.copy2(local, remote)


class S3Backend(Backend):
    """Rutas: /  (lista de buckets), /bucket/prefijo/ ..."""
    can_chmod = False

    def __init__(self, s):
        self.s = s

    def aws(self, *args):
        if not shutil.which("aws"):
            raise RuntimeError("Falta aws cli (instálalo con ./install-deps.sh)")
        cmd = ["aws", "s3"] + list(args)
        if self.s.get("profile"):
            cmd += ["--profile", self.s["profile"]]
        if self.s.get("url"):
            cmd += ["--endpoint-url", self.s["url"]]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode:
            raise RuntimeError(p.stderr.strip() or "error de aws s3")
        return p.stdout.splitlines()

    def list(self, path):
        path = "/" + (path or self.s.get("path") or "").strip("/")
        entries = []
        if path == "/":
            for line in self.aws("ls"):
                parts = line.split(None, 2)
                if len(parts) == 3:
                    entries.append(Entry(parts[2], True, 0, f"{parts[0]} {parts[1]}", "bucket"))
            return "/", entries
        for line in self.aws("ls", "s3://" + path.strip("/") + "/"):
            line = line.strip()
            if line.startswith("PRE "):
                entries.append(Entry(line[4:].rstrip("/"), True))
            else:
                parts = line.split(None, 3)
                if len(parts) == 4 and parts[3]:
                    entries.append(Entry(parts[3], False, int(parts[2]), f"{parts[0]} {parts[1]}"))
        return path, entries

    def upload(self, paths, d):
        for p in paths:
            dst = "s3://" + self.join(d, os.path.basename(p.rstrip("/"))).strip("/")
            self.aws("cp", p, dst + ("/" if os.path.isdir(p) else ""),
                     *(["--recursive"] if os.path.isdir(p) else []))

    def download(self, items, local):
        for p, e in items:
            dst = os.path.join(local, e.name)
            self.aws("cp", "s3://" + p.strip("/") + ("/" if e.is_dir else ""), dst,
                     *(["--recursive"] if e.is_dir else []))

    def mkdir(self, p):
        with tempfile.NamedTemporaryFile() as f:
            self.aws("cp", f.name, "s3://" + p.strip("/") + "/.keep")

    def rename(self, a, b):
        self.aws("mv", "s3://" + a.strip("/"), "s3://" + b.strip("/"), "--recursive")

    def delete(self, items):
        for p, e in items:
            self.aws("rm", "s3://" + p.strip("/") + ("/" if e.is_dir else ""),
                     *(["--recursive"] if e.is_dir else []))

    def get(self, remote, local):
        self.aws("cp", "s3://" + remote.strip("/"), local)

    def put(self, local, remote):
        self.aws("cp", local, "s3://" + remote.strip("/"))


def backend_for(s):
    t = s["type"]
    return {"ssh": SftpBackend, "sftp": SftpBackend, "ftp": FtpBackend,
            "s3": S3Backend}.get(t, lambda _s: LocalBackend())(s)


# ---------------------------------------------------------------- widget

class FileBrowser(Gtk.Box):
    def __init__(self, app, session, compact=True):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.app = app
        self.s = session
        self.be = backend_for(session)
        self.cwd = None
        self.closed = False
        self.edit_dir = tempfile.mkdtemp(prefix="edit-", dir=control_dir())

        bar = Gtk.Box(spacing=0)
        bar.get_style_context().add_class("nx-browser-bar")
        for icon, tip, cb in (("go-up-symbolic", "Subir un nivel", self.on_up),
                              ("go-home-symbolic", "Carpeta inicial", lambda *_: self.go("")),
                              ("view-refresh-symbolic", "Actualizar", lambda *_: self.go(self.cwd)),
                              ("document-send-symbolic", "Subir archivos", self.on_upload),
                              ("folder-download-symbolic", "Descargar", self.on_download),
                              ("document-new-symbolic", "Nuevo archivo", self.on_newfile),
                              ("folder-new-symbolic", "Nueva carpeta", self.on_mkdir),
                              ("edit-delete-symbolic", "Eliminar", self.on_delete)):
            b = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.MENU)
            b.set_tooltip_text(tip)
            b.set_relief(Gtk.ReliefStyle.NONE)
            b.connect("clicked", cb)
            bar.pack_start(b, False, False, 0)
        self.pack_start(bar, False, False, 0)
        self.path = Gtk.Entry()
        self.path.connect("activate", lambda e: self.go(e.get_text()))
        self.pack_start(self.path, False, False, 0)

        # icono, nombre, tamaño, fecha, permisos, es_dir, tamaño (int)
        self.store = Gtk.ListStore(str, str, str, str, str, bool, GObject.TYPE_INT64)
        self.view = Gtk.TreeView(model=self.store)
        self.view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        col = Gtk.TreeViewColumn("Nombre")
        ri, rt = Gtk.CellRendererPixbuf(), Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
        col.pack_start(ri, False)
        col.pack_start(rt, True)
        col.add_attribute(ri, "icon-name", 0)
        col.add_attribute(rt, "text", 1)
        col.set_expand(True)
        col.set_min_width(150)
        col.set_resizable(True)
        col.set_sort_column_id(1)
        self.view.append_column(col)
        for i, title in ((2, "Tamaño"), (3, "Modificado"), (4, "Permisos")):
            c = Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=i)
            c.set_sort_column_id(6 if i == 2 else i)
            c.set_resizable(True)
            self.view.append_column(c)
        self.view.connect("row-activated", self.on_activate)
        self.view.connect("button-press-event", self.on_click)
        self.view.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        self.view.drag_dest_add_uri_targets()
        self.view.connect("drag-data-received", self.on_drop)
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.add(self.view)
        self.pack_start(sw, True, True, 0)
        self.status = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.status.get_style_context().add_class("dim-label")
        self.pack_start(self.status, False, False, 0)
        self.connect("destroy", self.on_destroy)

        self.waits = 0
        self.set_status("Esperando la conexión…")
        GLib.timeout_add(300, self.wait_ready)

    def on_destroy(self, *_):
        self.closed = True
        shutil.rmtree(self.edit_dir, ignore_errors=True)

    def wait_ready(self):
        if self.closed:
            return False
        self.waits += 1
        if self.be.ready():
            self.go(self.s.get("path") or "")
            return False
        if self.waits > 400:
            self.set_status("Sin conexión (pulsa actualizar para reintentar).")
            return False
        return True

    # -- infraestructura
    def run_bg(self, work, done=None, refresh=True):
        def target():
            try:
                res, err = work(), None
            except Exception as e:  # noqa: BLE001 - se muestra al usuario
                res, err = None, str(e) or e.__class__.__name__

            def finish():
                if self.closed:
                    return False
                if err:
                    self.set_status(f"Error: {err}")
                elif done:
                    done(res)
                if refresh and not err and self.cwd is not None:
                    self.go(self.cwd)
                return False
            GLib.idle_add(finish)
        threading.Thread(target=target, daemon=True).start()

    def set_status(self, text):
        self.status.set_text(text)

    def remote(self, name):
        return self.be.join(self.cwd or "/", name)

    def go(self, path):
        self.set_status("Cargando…")
        self.run_bg(lambda: self.be.list(path), self.show_listing, refresh=False)

    def show_listing(self, res):
        self.cwd, entries = res
        self.path.set_text(self.cwd)
        self.store.clear()
        for e in sorted(entries, key=lambda e: (not e.is_dir, e.name.lower())):
            self.store.append(["folder" if e.is_dir else "text-x-generic", e.name,
                               "" if e.is_dir else human(e.size), e.mtime, e.perms, e.is_dir, e.size])
        self.set_status(f"{len(entries)} elementos · {self.cwd}")

    def selected(self):
        model, paths = self.view.get_selection().get_selected_rows()
        out = []
        for p in paths:
            row = model[p]
            out.append((self.remote(row[1]), Entry(row[1], row[5], row[6])))
        return out

    # -- acciones
    def on_up(self, *_):
        if self.cwd:
            self.go(self.be.parent(self.cwd))

    def on_activate(self, _view, path, _col):
        name, is_dir = self.store[path][1], self.store[path][5]
        if is_dir:
            self.go(self.remote(name))
        else:
            self.open_remote(name, internal=True)

    def ask(self, title, action):
        dlg = Gtk.FileChooserNative(title=title, transient_for=self.get_toplevel(), action=action,
                                    select_multiple=action == Gtk.FileChooserAction.OPEN)
        if action == Gtk.FileChooserAction.SELECT_FOLDER:
            dlg.set_current_folder(GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
                                   or os.path.expanduser("~"))
        res = dlg.get_filenames() if dlg.run() == Gtk.ResponseType.ACCEPT else []
        dlg.destroy()
        return res

    def upload(self, paths):
        if paths and self.cwd is not None:
            d = self.cwd
            self.set_status(f"Subiendo {len(paths)} elemento(s)…")
            self.run_bg(lambda: self.be.upload(paths, d))

    def on_upload(self, *_):
        self.upload(self.ask("Subir archivos", Gtk.FileChooserAction.OPEN))

    def on_drop(self, _w, _ctx, _x, _y, data, _info, _time):
        self.upload([GLib.filename_from_uri(u)[0] for u in data.get_uris() if u.startswith("file://")])

    def on_download(self, *_):
        items = self.selected()
        if not items:
            return
        dest = self.ask("Descargar en…", Gtk.FileChooserAction.SELECT_FOLDER)
        if dest:
            self.set_status(f"Descargando {len(items)} elemento(s)…")
            self.run_bg(lambda: self.be.download(items, dest[0]),
                        lambda _r: self.set_status(f"Descargado en {dest[0]}"), refresh=False)

    def on_mkdir(self, *_):
        name = self.prompt("Nueva carpeta", "")
        if name:
            self.run_bg(lambda: self.be.mkdir(self.remote(name)))

    def on_newfile(self, *_):
        name = self.prompt("Nuevo archivo", "nuevo.txt")
        if name:
            local = os.path.join(self.edit_dir, "new-" + name)
            open(local, "w").close()
            self.run_bg(lambda: self.be.put(local, self.remote(name)))

    def on_rename(self, name):
        new = self.prompt("Renombrar", name)
        if new and new != name:
            self.run_bg(lambda: self.be.rename(self.remote(name), self.remote(new)))

    def on_chmod(self, name):
        mode = self.prompt("Permisos (octal, p. ej. 644)", "")
        if mode and mode.isdigit():
            self.run_bg(lambda: self.be.chmod(self.remote(name), mode))

    def on_delete(self, *_):
        items = self.selected()
        if not items:
            return
        dlg = Gtk.MessageDialog(transient_for=self.get_toplevel(), modal=True,
                                message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.YES_NO,
                                text=f"¿Eliminar {len(items)} elemento(s)?",
                                secondary_text="\n".join(e.name for _, e in items[:15]))
        ok = dlg.run() == Gtk.ResponseType.YES
        dlg.destroy()
        if ok:
            self.run_bg(lambda: self.be.delete(items))

    def open_remote(self, name, internal=True):
        """Descarga a temporal y abre en el editor; al guardar se resube."""
        remote = self.remote(name)
        folder = os.path.join(self.edit_dir, str(abs(hash(os.path.dirname(remote)))))
        os.makedirs(folder, exist_ok=True)
        local = os.path.join(folder, name)
        self.set_status(f"Abriendo {name}…")

        def opened(_r):
            import tools
            if internal and tools.GtkSource is not None:
                from tools import TextEditor
                TextEditor.open_file(self.app, local, on_saved=lambda p: self.save_back(p, remote),
                                     title=f"{name} — {self.s.get('host') or 'local'}")
            else:
                Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(local), None)
                mon = Gio.File.new_for_path(local).monitor_file(Gio.FileMonitorFlags.NONE, None)
                mon.connect("changed", lambda _m, _f, _o, ev: ev == Gio.FileMonitorEvent.CHANGES_DONE_HINT
                            and self.save_back(local, remote))
                self._monitors = getattr(self, "_monitors", []) + [mon]
            self.set_status(f"Editando {name}: se subirá al guardar.")
        self.run_bg(lambda: self.be.get(remote, local), opened, refresh=False)

    def save_back(self, local, remote):
        self.run_bg(lambda: self.be.put(local, remote),
                    lambda _r: self.set_status(f"Guardado en el servidor: {remote}"), refresh=False)

    def prompt(self, title, value):
        dlg = Gtk.Dialog(title=title, transient_for=self.get_toplevel(), modal=True)
        dlg.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Aceptar", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        ent = Gtk.Entry(text=value, activates_default=True, margin=10)
        dlg.get_content_area().add(ent)
        dlg.show_all()
        res = ent.get_text().strip() if dlg.run() == Gtk.ResponseType.OK else None
        dlg.destroy()
        return res

    def on_click(self, view, ev):
        if ev.button != 3:
            return False
        hit = view.get_path_at_pos(int(ev.x), int(ev.y))
        sel = view.get_selection()
        if hit and not sel.path_is_selected(hit[0]):
            sel.unselect_all()
            sel.select_path(hit[0])
        items = self.selected()
        entries = []
        if len(items) == 1:
            _p, e = items[0]
            if not e.is_dir:
                entries += [("Abrir con el editor de MerlynGestorTerm", lambda *_: self.open_remote(e.name, True)),
                            ("Abrir con la app predeterminada", lambda *_: self.open_remote(e.name, False))]
            entries.append(("Renombrar", lambda *_: self.on_rename(e.name)))
            if self.be.can_chmod:
                entries.append(("Permisos…", lambda *_: self.on_chmod(e.name)))
        if items:
            entries += [("Descargar…", self.on_download), ("Eliminar", self.on_delete), None]
        entries += [("Subir archivos…", self.on_upload), ("Nuevo archivo", self.on_newfile),
                    ("Nueva carpeta", self.on_mkdir), ("Actualizar", lambda *_: self.go(self.cwd))]
        menu = Gtk.Menu()
        for entry in entries:
            if entry is None:
                menu.append(Gtk.SeparatorMenuItem())
                continue
            mi = Gtk.MenuItem(label=entry[0])
            mi.connect("activate", entry[1])
            menu.append(mi)
        menu.show_all()
        menu.popup_at_pointer(ev)
        return True
