"""Núcleo de MerlynGestorTerm: constantes, persistencia portable, llavero, claves SSH
y construcción de los comandos de cada tipo de sesión.

Portable: toda la configuración vive junto al programa en ./merlyngestorterm-data
(o en $MERLYN_HOME si está definida). Las contraseñas y passphrases se
guardan en el llavero del sistema (Secret Service / GNOME Keyring), nunca en
los JSON; ssh las recibe mediante SSH_ASKPASS (merlyngestorterm-askpass).
"""
import configparser
import json
import os
import shlex
import shutil
import stat
import subprocess
import uuid

import gi

import gtkver  # noqa: F401
from gi.repository import GLib, Gtk  # noqa: E402

try:
    gi.require_version("Secret", "1")
    from gi.repository import Secret
except (ValueError, ImportError):
    Secret = None

APP_NAME = "MerlynGestorTerm"
VERSION = "2.0.0"
APP_DIR = os.path.dirname(os.path.realpath(__file__))
LEGACY_DATA_DIR = os.path.join(APP_DIR, "nexterm-data")  # nombre anterior del proyecto
DATA_DIR = (os.environ.get("MERLYN_HOME") or os.environ.get("NEXTERM_HOME")
            or os.path.join(APP_DIR, "merlyngestorterm-data"))
if not os.path.exists(DATA_DIR) and os.path.isdir(LEGACY_DATA_DIR) and \
        not (os.environ.get("MERLYN_HOME") or os.environ.get("NEXTERM_HOME")):
    os.rename(LEGACY_DATA_DIR, DATA_DIR)  # migra las sesiones de la versión llamada NexTerm
KEYS_DIR = os.path.join(DATA_DIR, "keys")
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
TUNNELS_FILE = os.path.join(DATA_DIR, "tunnels.json")
MACROS_FILE = os.path.join(DATA_DIR, "macros.json")
SERVERS_FILE = os.path.join(DATA_DIR, "servers.json")
CRON_FILE = os.path.join(DATA_DIR, "cron.json")
RECENT_FILE = os.path.join(DATA_DIR, "recent.json")
WOL_FILE = os.path.join(DATA_DIR, "wol.json")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
ASKPASS = os.path.join(APP_DIR, "merlyngestorterm-askpass")

# Mismos tipos de sesión que MobaXterm (lista observada en el binario v26.6),
# menos WSL, que no existe en Linux.
TYPES = ["ssh", "telnet", "rsh", "xdmcp", "rdp", "vnc", "ftp", "sftp", "serial",
         "file", "shell", "browser", "mosh", "s3"]
TYPE_LABELS = {
    "shell": "Shell", "ssh": "SSH", "sftp": "SFTP", "telnet": "Telnet", "rsh": "Rsh",
    "xdmcp": "XDMCP", "serial": "Serial", "rdp": "RDP", "vnc": "VNC", "ftp": "FTP",
    "file": "Archivos", "browser": "Navegador", "mosh": "Mosh", "s3": "Amazon S3",
}
TYPE_ICONS = {
    "shell": "utilities-terminal", "ssh": "network-server", "sftp": "folder-remote",
    "telnet": "preferences-system-network", "rsh": "modem", "xdmcp": "computer",
    "serial": "media-flash", "rdp": "video-display", "vnc": "preferences-desktop-remote-desktop",
    "ftp": "folder-remote", "file": "folder", "browser": "web-browser", "mosh": "network-wireless",
    "s3": "network-workgroup", "copyid": "dialog-password",
}
DEFAULT_PORTS = {"ssh": 22, "sftp": 22, "telnet": 23, "rsh": 513, "xdmcp": 177,
                 "rdp": 3389, "vnc": 5900, "ftp": 21, "mosh": 22}
# Tipos que se muestran con el explorador de archivos en vez de una terminal
BROWSER_TYPES = ("ftp", "file", "s3")

# Códigos de tipo de sesión de MobaXterm.ini (observados en el binario v26.6)
MOBA_TYPES = {"109": "ssh", "98": "telnet", "100": "rsh", "91": "rdp", "128": "vnc",
              "130": "ftp", "140": "sftp", "313": "browser"}

DEFAULT_SETTINGS = {
    "font": "Monospace 11",
    "scrollback": 10000,
    "palette": "omarchy",
    "cursor": "block",
    "copy_on_select": True,
    "right_click_paste": False,
    "confirm_close": True,
    "bell": False,
    "remote_monitoring": True,
    "sftp_browser": True,
    "keepalive": 60,
    "x11_default": True,
    "log_sessions": False,
    "theme": "system",
    "home_tab": True,
}

PALETTES = {
    "omarchy": ("#cdd6f4", "#1e1e2e", [
        "#45475a", "#f38ba8", "#a6e3a1", "#f9e2af", "#89b4fa", "#f5c2e7", "#94e2d5", "#bac2de",
        "#585b70", "#f38ba8", "#a6e3a1", "#f9e2af", "#89b4fa", "#f5c2e7", "#94e2d5", "#a6adc8"]),
    "clasico": ("#e5e5e5", "#000000", [
        "#000000", "#cd0000", "#00cd00", "#cdcd00", "#0000ee", "#cd00cd", "#00cdcd", "#e5e5e5",
        "#7f7f7f", "#ff0000", "#00ff00", "#ffff00", "#5c5cff", "#ff00ff", "#00ffff", "#ffffff"]),
    "claro": ("#2e3440", "#fdfdfd", [
        "#2e3440", "#bf1b1b", "#287a28", "#9c6b00", "#1d4fbf", "#8a2a9c", "#147a7a", "#d8dee9",
        "#4c566a", "#e03131", "#2f9e44", "#e67700", "#1c7ed6", "#ae3ec9", "#0c8599", "#eceff4"]),
    "solarizado": ("#839496", "#002b36", [
        "#073642", "#dc322f", "#859900", "#b58900", "#268bd2", "#d33682", "#2aa198", "#eee8d5",
        "#002b36", "#cb4b16", "#586e75", "#657b83", "#839496", "#6c71c4", "#93a1a1", "#fdf6e3"]),
    "verde": ("#33ff66", "#000000", [
        "#000000", "#cd0000", "#33ff66", "#cdcd00", "#0000ee", "#cd00cd", "#00cdcd", "#e5e5e5",
        "#7f7f7f", "#ff0000", "#66ff99", "#ffff00", "#5c5cff", "#ff00ff", "#00ffff", "#ffffff"]),
}


# ---------------------------------------------------------------- persistencia

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_settings():
    return {**DEFAULT_SETTINGS, **load_json(SETTINGS_FILE, {})}


def save_json(path, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def import_mobaxterm_ini(path):
    """Lee las secciones [Bookmarks*] de un MobaXterm.ini.

    Formato de cada entrada: Nombre=#<tipo>#<n>%host%puerto%usuario%...
    """
    cp = configparser.RawConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    with open(path, encoding="utf-8", errors="replace") as f:
        cp.read_file(f)
    sessions = []
    for section in cp.sections():
        if not section.startswith("Bookmarks"):
            continue
        group = cp.get(section, "SubRep", fallback="").replace("\\", "/")
        for name, value in cp.items(section):
            if name in ("SubRep", "ImgNum") or not value.startswith("#"):
                continue
            head, _, rest = value.partition("%")
            code = head.strip("#").split("#")[0]
            stype = MOBA_TYPES.get(code)
            if not stype:
                continue
            fields = rest.split("%")
            host = fields[0] if fields else ""
            port = fields[1] if len(fields) > 1 and fields[1].isdigit() else DEFAULT_PORTS.get(stype, "")
            user = fields[2] if len(fields) > 2 else ""
            sessions.append(new_session(name=name, type=stype, group=group,
                                        host=host, port=int(port or 0), user=user))
    return sessions


# ---------------------------------------------------------------- llavero

_ATTRS = {"session": Secret.SchemaAttributeType.STRING,
          "kind": Secret.SchemaAttributeType.STRING} if Secret else None  # kind: password | passphrase
SCHEMA = Secret.Schema.new("io.github.merlyngestorterm.Credential", Secret.SchemaFlags.NONE,
                           _ATTRS) if Secret else None
# Credenciales guardadas con el nombre anterior del proyecto (NexTerm): se leen y migran
LEGACY_SCHEMA = Secret.Schema.new("dev.nexterm.Credential", Secret.SchemaFlags.NONE,
                                  _ATTRS) if Secret else None


def secret_get(sid, kind):
    if not SCHEMA:
        return None
    attrs = {"session": sid, "kind": kind}
    try:
        value = Secret.password_lookup_sync(SCHEMA, attrs, None)
        if value is None:
            value = Secret.password_lookup_sync(LEGACY_SCHEMA, attrs, None)
            if value is not None:  # migración al esquema nuevo
                Secret.password_store_sync(SCHEMA, attrs, Secret.COLLECTION_DEFAULT,
                                           f"{APP_NAME}: {sid} ({kind})", value, None)
                Secret.password_clear_sync(LEGACY_SCHEMA, attrs, None)
        return value
    except GLib.Error:
        return None


def secret_set(sid, kind, value, label):
    if not SCHEMA:
        raise RuntimeError("libsecret no disponible (instálalo con ./install-deps.sh)")
    attrs = {"session": sid, "kind": kind}
    if value:
        Secret.password_store_sync(SCHEMA, attrs, Secret.COLLECTION_DEFAULT,
                                   f"{APP_NAME}: {label} ({kind})", value, None)
    else:
        Secret.password_clear_sync(SCHEMA, attrs, None)
        Secret.password_clear_sync(LEGACY_SCHEMA, attrs, None)


def askpass_main(prompt):
    """Modo SSH_ASKPASS: ssh llama a este programa con el texto de la pregunta."""
    sid = os.environ.get("MERLYN_SESSION", "")
    low = prompt.lower()
    kind = "passphrase" if "passphrase" in low else "password" if "password" in low else None
    if kind:
        value = secret_get(sid, kind)
        if value:
            print(value)
            return 0
        dlg = Gtk.MessageDialog(message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.OK_CANCEL, text=prompt)
        entry = Gtk.Entry(visibility=False, activates_default=True)
        dlg.get_content_area().add(entry)
        dlg.set_default_response(Gtk.ResponseType.OK)
        dlg.show_all()
        ok = dlg.run() == Gtk.ResponseType.OK
        if ok:
            print(entry.get_text())
        return 0 if ok else 1
    # Preguntas sí/no (p. ej. huella de un host nuevo)
    dlg = Gtk.MessageDialog(message_type=Gtk.MessageType.QUESTION,
                            buttons=Gtk.ButtonsType.YES_NO, text=prompt)
    ok = dlg.run() == Gtk.ResponseType.YES
    print("yes" if ok else "no")
    return 0


# ---------------------------------------------------------------- claves

def key_path(s):
    """Ruta absoluta de la clave; las relativas son relativas a DATA_DIR (portables)."""
    k = (s.get("key") or "").strip()
    if not k:
        return ""
    k = os.path.expanduser(k)
    return k if os.path.isabs(k) else os.path.join(DATA_DIR, k)


def import_key(src):
    """Copia una clave a merlyngestorterm-data/keys (convierte .ppk) y devuelve la ruta relativa."""
    os.makedirs(KEYS_DIR, mode=0o700, exist_ok=True)
    base = os.path.splitext(os.path.basename(src))[0] if src.endswith(".ppk") else os.path.basename(src)
    dst = os.path.join(KEYS_DIR, base)
    with open(src, "rb") as f:
        is_ppk = f.read(20).startswith(b"PuTTY-User-Key-File")
    if is_ppk:
        if not shutil.which("puttygen"):
            raise RuntimeError("Clave PuTTY (.ppk): falta puttygen (instálalo con ./install-deps.sh)")
        subprocess.run(["puttygen", src, "-O", "private-openssh", "-o", dst], check=True)
    else:
        shutil.copyfile(src, dst)
    os.chmod(dst, 0o600)
    if os.path.exists(src + ".pub"):
        shutil.copyfile(src + ".pub", dst + ".pub")
    return os.path.relpath(dst, DATA_DIR)


def check_key(path):
    """Devuelve un aviso si la clave no sirve tal cual; corrige permisos en las propias."""
    if not os.path.exists(path):
        return f"No existe la clave {path}"
    with open(path, "rb") as f:
        if f.read(20).startswith(b"PuTTY-User-Key-File"):
            return "Es una clave PuTTY (.ppk): impórtala con el botón de la sesión para convertirla."
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        if path.startswith(KEYS_DIR) or os.stat(path).st_uid == os.getuid():
            os.chmod(path, 0o600)
        else:
            return f"Permisos demasiado abiertos en {path} (chmod 600)"
    return None


def new_session(**kw):
    s = {"id": uuid.uuid4().hex, "name": "Nueva sesión", "type": "ssh", "group": "",
         "host": "", "port": 22, "user": "", "key": "", "jump": "", "x11": False,
         "command": "", "extra": "", "device": "/dev/ttyUSB0", "baud": 115200,
         "path": "", "url": "", "compression": False, "agent_forward": False,
         "color": "", "log": False, "monitoring": True, "sftp": True, "profile": ""}
    s.update(kw)
    return s


def recent_add(s):
    """Guarda las últimas sesiones abiertas (pestaña Inicio)."""
    recent = [r for r in load_json(RECENT_FILE, []) if r.get("id") != s.get("id")]
    recent.insert(0, {k: v for k, v in s.items()})
    save_json(RECENT_FILE, recent[:15])


def resolve_path(p):
    p = os.path.expanduser(p or "")
    return p if not p or os.path.isabs(p) else os.path.join(DATA_DIR, p)


# ---------------------------------------------------------------- comandos

def build_argv(s):
    """Devuelve (argv, error) para lanzar la sesión dentro de la terminal."""
    t = s["type"]
    extra = shlex.split(s.get("extra") or "")
    target = f'{s["user"]}@{s["host"]}' if s.get("user") else s.get("host", "")
    port = str(s.get("port") or DEFAULT_PORTS.get(t, ""))

    if t == "shell":
        cmd = s.get("command")
        if cmd:
            return ["/bin/sh", "-c", cmd], None
        return [os.environ.get("SHELL", "/bin/bash"), "-l"], None
    if not s.get("host") and t != "serial":
        return None, "La sesión no tiene host."
    key = key_path(s)
    if key and t in ("ssh", "sftp"):
        err = check_key(key)
        if err:
            return None, err
    if t == "ssh":
        argv = ["ssh"] + ssh_opts(s, "-p")
        if s.get("x11"):
            argv += ["-Y"]
        if s.get("compression"):
            argv += ["-C"]
        if s.get("agent_forward"):
            argv += ["-A"]
        argv += extra + [target]
        if s.get("command"):
            argv += ["-t", s["command"]]
        return argv, None
    if t == "sftp":
        return ["sftp"] + ssh_opts(s, "-P") + extra + [target], None
    if t == "mosh":
        if not shutil.which("mosh"):
            return None, "Falta 'mosh' (instálalo con ./install-deps.sh)."
        ssh = shlex.join(["ssh"] + ssh_opts(s, "-p"))
        return ["mosh", f"--ssh={ssh}"] + extra + [target], None
    if t == "rsh":
        exe = shutil.which("rlogin") or shutil.which("rsh")
        if not exe:
            return None, "Falta 'rlogin'/'rsh' (instálalo con ./install-deps.sh)."
        argv = [exe] + (["-l", s["user"]] if s.get("user") else [])
        return argv + extra + [s["host"]], None
    if t == "xdmcp":
        exe = shutil.which("Xephyr")
        if not exe:
            return None, "XDMCP necesita Xephyr (instálalo con ./install-deps.sh)."
        return [exe, ":9", "-query", s["host"], "-port", port, "-resizeable"] + extra, None
    if t == "telnet":
        if not shutil.which("telnet"):
            return None, "Falta 'telnet' (instálalo con ./install-deps.sh)."
        return ["telnet", s["host"], port] + extra, None
    if t == "serial":
        dev, baud = s.get("device") or "/dev/ttyUSB0", str(s.get("baud") or 115200)
        if shutil.which("picocom"):
            return ["picocom", "-b", baud] + extra + [dev], None
        if shutil.which("screen"):
            return ["screen", dev, baud], None
        if shutil.which("minicom"):
            return ["minicom", "-D", dev, "-b", baud] + extra, None
        return None, "Falta picocom, screen o minicom (instálalo con ./install-deps.sh)."
    if t == "rdp":
        exe = shutil.which("xfreerdp3") or shutil.which("xfreerdp") or shutil.which("wlfreerdp")
        if not exe:
            return None, "Falta FreeRDP (instálalo con ./install-deps.sh)."
        argv = [exe, f'/v:{s["host"]}:{port}', "/dynamic-resolution", "+clipboard", "/cert:tofu"]
        if s.get("user"):
            argv.append(f'/u:{s["user"]}')
        pw = secret_get(s["id"], "password")
        if pw:
            argv.append(f"/p:{pw}")
        return argv + extra, None
    if t == "copyid":
        argv = ["ssh-copy-id", "-p", port]
        if key:
            pub = key + ".pub"
            if not os.path.exists(pub):
                with open(pub, "w") as f:
                    subprocess.run(["ssh-keygen", "-y", "-f", key], stdout=f, check=True,
                                   env={**os.environ, **askpass_env(s)})
            argv += ["-i", pub]
        return argv + [target], None
    if t == "vnc":
        exe = shutil.which("vncviewer") or shutil.which("remmina")
        if not exe:
            return None, "Falta un visor VNC (instálalo con ./install-deps.sh)."
        if exe.endswith("remmina"):
            return [exe, "-c", f'vnc://{s["host"]}:{port}'], None
        return [exe, f'{s["host"]}::{port}'] + extra, None
    return None, f"Tipo desconocido: {t}"


def control_dir():
    base = os.environ.get("XDG_RUNTIME_DIR") or GLib.get_tmp_dir()
    d = os.path.join(base, "merlyngestorterm")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def ssh_target(s):
    return f'{s["user"]}@{s["host"]}' if s.get("user") else s["host"]


def ssh_opts(s, port_flag):
    """Opciones comunes de ssh/sftp. ControlMaster permite que el panel SFTP
    reutilice la conexión ya autenticada de la terminal."""
    opts = [port_flag, str(s.get("port") or 22),
            "-o", f"ControlPath={control_dir()}/cm-%C",
            "-o", "ControlMaster=auto", "-o", "ControlPersist=120",
            "-o", f"ServerAliveInterval={load_settings().get('keepalive', 60)}"]
    key = key_path(s)
    if key:
        opts += ["-i", key, "-o", "IdentitiesOnly=yes"]
    if s.get("jump"):
        opts += ["-J", s["jump"]]
    return opts


def askpass_env(s):
    """Variables para que ssh pida contraseñas/passphrases a nuestro askpass."""
    if not s.get("id") or not os.access(ASKPASS, os.X_OK):
        return {}
    return {"SSH_ASKPASS": ASKPASS, "SSH_ASKPASS_REQUIRE": "force", "MERLYN_SESSION": s["id"]}


