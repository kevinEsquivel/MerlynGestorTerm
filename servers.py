"""Servidores integrados (los mismos que ofrece MobaXterm): TFTP, FTP, HTTP,
SSH/SFTP, Telnet, NFS, VNC, Cron e Iperf. FTP, TFTP, Telnet y Cron están
implementados aquí; el resto usa herramientas del sistema si existen."""
import datetime
import functools
import hashlib
import http.server
import os
import pty
import select
import shutil
import socket
import socketserver
import stat
import struct
import subprocess
import threading
import time

import gtkver  # noqa: F401
from gi.repository import GLib, Gtk

from core import CRON_FILE, DATA_DIR, SERVERS_FILE, load_json, save_json

SERVERS_DIR = os.path.join(DATA_DIR, "servers")


def find_sshd():
    """sshd está en /usr/bin (Arch) o en /usr/sbin (Debian, Fedora)."""
    for path in (shutil.which("sshd"), "/usr/sbin/sshd", "/usr/bin/sshd"):
        if path and os.access(path, os.X_OK):
            return path
    return None


def local_ips():
    out = subprocess.run(["ip", "-o", "-4", "addr", "show", "scope", "global"],
                         capture_output=True, text=True).stdout
    return [l.split()[3].split("/")[0] for l in out.splitlines() if len(l.split()) > 3]


class Server:
    key = ""
    name = ""
    icon = "network-server"
    fields = []          # (clave, etiqueta, tipo, valor por defecto)
    note = ""

    def __init__(self, manager):
        self.manager = manager
        self.cfg = {f[0]: f[3] for f in self.fields}
        self.cfg.update(manager.saved.get(self.key, {}))
        self.thread = None
        self.proc = None

    def log(self, text):
        self.manager.log(f"[{self.name}] {text}")

    def running(self):
        if self.proc is not None:
            return self.proc.poll() is None
        return self.thread is not None

    def start(self):
        raise NotImplementedError

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
        self.proc = None

    def available(self):
        return None  # texto de error si falta algo

    def run_process(self, cmd, **kw):
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, text=True, start_new_session=True, **kw)

        def pump(p=self.proc):
            for line in p.stdout:
                self.log(line.rstrip())
            self.log(f"terminado (código {p.wait()})")
            GLib.idle_add(self.manager.changed)
        threading.Thread(target=pump, daemon=True).start()


class TcpServer(Server):
    """Servidor basado en socketserver ejecutándose en un hilo."""

    def make(self):
        raise NotImplementedError

    def start(self):
        self.srv = self.make()
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.thread:
            self.srv.shutdown()
            self.srv.server_close()
            self.thread = None


class Threaded(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


# ---------------------------------------------------------------- HTTP

class HttpServer(TcpServer):
    key, name, icon = "http", "HTTP", "web-browser"
    fields = [("root", "Carpeta", "dir", os.path.expanduser("~")), ("bind", "Dirección", "str", "0.0.0.0"),
              ("port", "Puerto", "int", 8080)]
    note = "Comparte una carpeta por HTTP (solo lectura)."

    def make(self):
        server = self

        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, fmt, *args):
                server.log(f"{self.client_address[0]} {fmt % args}")
        handler = functools.partial(Handler, directory=self.cfg["root"])
        srv = http.server.ThreadingHTTPServer((self.cfg["bind"], int(self.cfg["port"])), handler)
        self.log(f"sirviendo {self.cfg['root']} en http://{local_ips()[0] if local_ips() else 'localhost'}:"
                 f"{self.cfg['port']}/")
        return srv


# ---------------------------------------------------------------- FTP

class FtpServer(TcpServer):
    key, name, icon = "ftp", "FTP", "folder-remote"
    fields = [("root", "Carpeta", "dir", os.path.expanduser("~")), ("bind", "Dirección", "str", "0.0.0.0"),
              ("port", "Puerto", "int", 2121), ("user", "Usuario", "str", "merlyn"),
              ("password", "Contraseña", "password", ""), ("anonymous", "Permitir anónimo", "bool", False),
              ("write", "Permitir escritura", "bool", True),
              ("pasv", "Puertos pasivos", "str", "50000-50100")]
    note = "Servidor FTP propio de MerlynGestorTerm (sin cifrar: úsalo solo en redes de confianza)."

    def make(self):
        srv = Threaded((self.cfg["bind"], int(self.cfg["port"])), FtpHandler)
        srv.nx = self
        self.log(f"escuchando en el puerto {self.cfg['port']} · carpeta {self.cfg['root']}")
        return srv


class FtpHandler(socketserver.StreamRequestHandler):
    def reply(self, text):
        self.wfile.write((text + "\r\n").encode("utf-8"))

    def handle(self):
        self.nx = self.server.nx
        self.cfg = self.nx.cfg
        self.root = os.path.realpath(os.path.expanduser(self.cfg["root"]))
        self.cwd = "/"
        self.user = None
        self.authed = False
        self.pasv = None
        self.rnfr = None
        self.rest = 0
        peer = self.client_address[0]
        self.nx.log(f"conexión de {peer}")
        self.reply("220 MerlynGestorTerm FTP listo")
        for raw in self.rfile:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            cmd, _, arg = line.partition(" ")
            cmd = cmd.upper()
            if cmd == "PASS":
                self.nx.log(f"{peer}: PASS ****")
            else:
                self.nx.log(f"{peer}: {line}")
            try:
                if not self.dispatch(cmd, arg):
                    break
            except OSError as e:
                self.reply(f"550 {e.strerror or e}")

    def real(self, path):
        virt = os.path.normpath(os.path.join(self.cwd, path or "."))
        real = os.path.realpath(os.path.join(self.root, virt.lstrip("/")))
        if real != self.root and not real.startswith(self.root + os.sep):
            raise PermissionError(13, "Fuera de la carpeta compartida")
        return real, virt

    def data_conn(self):
        if not self.pasv:
            self.reply("425 Usa PASV o EPSV primero")
            return None
        self.pasv.settimeout(20)
        conn, _ = self.pasv.accept()
        self.pasv.close()
        self.pasv = None
        return conn

    def open_pasv(self):
        lo, _, hi = self.cfg["pasv"].partition("-")
        for port in range(int(lo), int(hi or lo) + 1):
            s = socket.socket()
            try:
                s.bind((self.request.getsockname()[0], port))
                s.listen(1)
                return s
            except OSError:
                s.close()
        raise OSError("Sin puertos pasivos libres")

    def listing(self, path, mlsd=False):
        real, _ = self.real(path)
        names = sorted(os.listdir(real)) if os.path.isdir(real) else [os.path.basename(real)]
        base = real if os.path.isdir(real) else os.path.dirname(real)
        lines = []
        for n in names:
            try:
                st = os.stat(os.path.join(base, n))
            except OSError:
                continue
            t = time.localtime(st.st_mtime)
            if mlsd:
                kind = "dir" if stat.S_ISDIR(st.st_mode) else "file"
                lines.append(f"type={kind};size={st.st_size};modify={time.strftime('%Y%m%d%H%M%S', time.gmtime(st.st_mtime))};"
                             f"unix.mode={oct(st.st_mode & 0o777)[2:]}; {n}")
            else:
                lines.append(f"{stat.filemode(st.st_mode)} 1 owner group {st.st_size:>12} "
                             f"{time.strftime('%b %d %H:%M', t)} {n}")
        return "\r\n".join(lines) + ("\r\n" if lines else "")

    def dispatch(self, cmd, arg):
        open_cmds = {"USER", "PASS", "QUIT", "SYST", "FEAT", "NOOP", "OPTS", "AUTH"}
        if not self.authed and cmd not in open_cmds:
            self.reply("530 Inicia sesión primero")
            return True
        writing = {"STOR", "APPE", "DELE", "MKD", "RMD", "RNFR", "RNTO", "SITE"}
        if cmd in writing and not self.cfg["write"]:
            self.reply("550 Servidor de solo lectura")
            return True
        if cmd == "USER":
            self.user = arg
            if self.cfg["anonymous"] and arg.lower() in ("anonymous", "ftp"):
                self.authed = True
                self.reply("230 Acceso anónimo concedido")
            else:
                self.reply("331 Contraseña, por favor")
        elif cmd == "PASS":
            ok = self.user == self.cfg["user"] and self.cfg["password"] and arg == self.cfg["password"]
            if ok or self.authed:
                self.authed = True
                self.reply("230 Sesión iniciada")
            else:
                self.reply("530 Usuario o contraseña incorrectos")
        elif cmd == "AUTH":
            self.reply("502 TLS no soportado")
        elif cmd == "QUIT":
            self.reply("221 Adiós")
            return False
        elif cmd == "SYST":
            self.reply("215 UNIX Type: L8")
        elif cmd == "FEAT":
            self.wfile.write(b"211-Features\r\n MLSD\r\n SIZE\r\n MDTM\r\n EPSV\r\n UTF8\r\n REST STREAM\r\n211 End\r\n")
        elif cmd == "OPTS":
            self.reply("200 OK")
        elif cmd == "NOOP":
            self.reply("200 OK")
        elif cmd == "PWD":
            self.reply(f'257 "{self.cwd}"')
        elif cmd in ("CWD", "CDUP"):
            real, virt = self.real(".." if cmd == "CDUP" else arg)
            if os.path.isdir(real):
                self.cwd = virt
                self.reply("250 OK")
            else:
                self.reply("550 No es una carpeta")
        elif cmd == "TYPE":
            self.reply("200 Tipo establecido")
        elif cmd == "PASV":
            self.pasv = self.open_pasv()
            ip, port = self.pasv.getsockname()
            self.reply("227 Entering Passive Mode ({},{},{})".format(ip.replace(".", ","), port >> 8, port & 255))
        elif cmd == "EPSV":
            self.pasv = self.open_pasv()
            self.reply(f"229 Entering Extended Passive Mode (|||{self.pasv.getsockname()[1]}|)")
        elif cmd in ("LIST", "NLST", "MLSD"):
            path = " ".join(a for a in arg.split() if not a.startswith("-"))
            data = self.listing(path, cmd == "MLSD")
            if cmd == "NLST":
                data = "\r\n".join(l.rsplit(" ", 1)[-1] for l in data.splitlines()) + "\r\n"
            conn = self.data_conn()
            if conn:
                self.reply("150 Enviando listado")
                conn.sendall(data.encode("utf-8"))
                conn.close()
                self.reply("226 Listado enviado")
        elif cmd == "REST":
            self.rest = int(arg or 0)
            self.reply("350 Posición guardada")
        elif cmd == "RETR":
            real, _ = self.real(arg)
            if not os.path.isfile(real):
                self.reply("550 No existe")
                return True
            conn = self.data_conn()
            if conn:
                self.reply("150 Enviando archivo")
                with open(real, "rb") as f:
                    f.seek(self.rest)
                    self.rest = 0
                    shutil.copyfileobj(f, conn.makefile("wb"))
                conn.close()
                self.reply("226 Transferencia completa")
        elif cmd in ("STOR", "APPE"):
            real, _ = self.real(arg)
            conn = self.data_conn()
            if conn:
                self.reply("150 Recibiendo archivo")
                with open(real, "ab" if cmd == "APPE" else "wb") as f:
                    while chunk := conn.recv(65536):
                        f.write(chunk)
                conn.close()
                self.reply("226 Archivo recibido")
        elif cmd == "SIZE":
            self.reply(f"213 {os.path.getsize(self.real(arg)[0])}")
        elif cmd == "MDTM":
            self.reply("213 " + time.strftime("%Y%m%d%H%M%S", time.gmtime(os.path.getmtime(self.real(arg)[0]))))
        elif cmd == "DELE":
            os.remove(self.real(arg)[0])
            self.reply("250 Borrado")
        elif cmd == "MKD":
            real, virt = self.real(arg)
            os.mkdir(real)
            self.reply(f'257 "{virt}" creada')
        elif cmd == "RMD":
            os.rmdir(self.real(arg)[0])
            self.reply("250 Carpeta borrada")
        elif cmd == "RNFR":
            self.rnfr = self.real(arg)[0]
            self.reply("350 Listo para renombrar")
        elif cmd == "RNTO":
            os.rename(self.rnfr, self.real(arg)[0])
            self.rnfr = None
            self.reply("250 Renombrado")
        elif cmd == "SITE" and arg.upper().startswith("CHMOD"):
            _, mode, path = arg.split(None, 2)
            os.chmod(self.real(path)[0], int(mode, 8))
            self.reply("200 Permisos cambiados")
        else:
            self.reply("502 Comando no implementado")
        return True


# ---------------------------------------------------------------- TFTP

class TftpServer(Server):
    key, name, icon = "tftp", "TFTP", "document-send"
    fields = [("root", "Carpeta", "dir", os.path.expanduser("~")), ("bind", "Dirección", "str", "0.0.0.0"),
              ("port", "Puerto", "int", 6969), ("write", "Permitir subidas", "bool", False)]
    note = "El puerto estándar 69 requiere root; por defecto se usa 6969."

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.cfg["bind"], int(self.cfg["port"])))
        self.stopping = False
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()
        self.log(f"escuchando UDP {self.cfg['port']} · carpeta {self.cfg['root']}")

    def stop(self):
        if self.thread:
            self.stopping = True
            self.sock.close()
            self.thread = None

    def loop(self):
        while not self.stopping:
            try:
                data, addr = self.sock.recvfrom(1024)
            except OSError:
                return
            if len(data) < 4:
                continue
            op = struct.unpack("!H", data[:2])[0]
            parts = data[2:].split(b"\0")
            name = parts[0].decode(errors="replace")
            threading.Thread(target=self.transfer, args=(op, name, addr), daemon=True).start()

    def path(self, name):
        root = os.path.realpath(os.path.expanduser(self.cfg["root"]))
        real = os.path.realpath(os.path.join(root, name.lstrip("/")))
        if not real.startswith(root + os.sep):
            raise PermissionError("fuera de la carpeta")
        return real

    @staticmethod
    def error(sock, addr, code, msg):
        sock.sendto(struct.pack("!HH", 5, code) + msg.encode() + b"\0", addr)

    def transfer(self, op, name, addr):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((self.cfg["bind"], 0))
        sock.settimeout(5)
        try:
            real = self.path(name)
            if op == 1:  # RRQ
                self.log(f"{addr[0]} descarga {name}")
                with open(real, "rb") as f:
                    block = 1
                    while True:
                        chunk = f.read(512)
                        for _ in range(5):
                            sock.sendto(struct.pack("!HH", 3, block & 0xFFFF) + chunk, addr)
                            try:
                                ack, _ = sock.recvfrom(4)
                                if struct.unpack("!HH", ack[:4]) == (4, block & 0xFFFF):
                                    break
                            except socket.timeout:
                                continue
                        else:
                            raise TimeoutError("sin respuesta del cliente")
                        if len(chunk) < 512:
                            break
                        block += 1
            elif op == 2:  # WRQ
                if not self.cfg["write"]:
                    self.error(sock, addr, 2, "solo lectura")
                    return
                self.log(f"{addr[0]} sube {name}")
                with open(real, "wb") as f:
                    sock.sendto(struct.pack("!HH", 4, 0), addr)
                    expected = 1
                    while True:
                        data, _ = sock.recvfrom(516)
                        op2, blk = struct.unpack("!HH", data[:4])
                        if op2 == 3 and blk == expected & 0xFFFF:
                            f.write(data[4:])
                            sock.sendto(struct.pack("!HH", 4, blk), addr)
                            expected += 1
                            if len(data) < 516:
                                break
            self.log(f"{addr[0]} {name}: completado")
        except FileNotFoundError:
            self.error(sock, addr, 1, "no existe")
        except (OSError, TimeoutError) as e:
            self.error(sock, addr, 0, str(e))
            self.log(f"{addr[0]} {name}: {e}")
        finally:
            sock.close()


# ---------------------------------------------------------------- Telnet

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
ECHO, SGA = 1, 3


class TelnetServer(TcpServer):
    key, name, icon = "telnet", "Telnet", "preferences-system-network"
    fields = [("bind", "Dirección", "str", "127.0.0.1"), ("port", "Puerto", "int", 2323),
              ("password", "Contraseña de acceso", "password", "")]
    note = "Da una shell de tu usuario. Telnet no cifra nada: por defecto solo escucha en 127.0.0.1."

    def start(self):
        if not self.cfg["password"]:
            raise RuntimeError("Define una contraseña de acceso en la configuración.")
        super().start()

    def make(self):
        srv = Threaded((self.cfg["bind"], int(self.cfg["port"])), TelnetHandler)
        srv.nx = self
        self.log(f"escuchando en {self.cfg['bind']}:{self.cfg['port']}")
        return srv


class TelnetHandler(socketserver.BaseRequestHandler):
    def clean(self, data):
        """Quita las negociaciones IAC del flujo entrante."""
        out, i = bytearray(), 0
        while i < len(data):
            b = data[i]
            if b == IAC and i + 1 < len(data):
                cmd = data[i + 1]
                if cmd in (DO, DONT, WILL, WONT):
                    i += 3
                    continue
                if cmd == SB:
                    end = data.find(bytes([IAC, SE]), i)
                    i = end + 2 if end >= 0 else len(data)
                    continue
                if cmd == IAC:
                    out.append(IAC)
                i += 2
                continue
            if b != 0:
                out.append(b)
            i += 1
        return bytes(out)

    def readline(self, echo=True):
        buf = b""
        while True:
            data = self.clean(self.request.recv(1024))
            if not data:
                return None
            for ch in data:
                if ch in (13, 10):
                    self.request.sendall(b"\r\n")
                    return buf.decode(errors="replace")
                if ch in (8, 127):
                    if buf:
                        buf = buf[:-1]
                        if echo:
                            self.request.sendall(b"\b \b")
                    continue
                buf += bytes([ch])
                if echo:
                    self.request.sendall(bytes([ch]))

    def handle(self):
        nx = self.server.nx
        peer = self.client_address[0]
        sock = self.request
        sock.sendall(bytes([IAC, WILL, ECHO, IAC, WILL, SGA, IAC, DO, SGA]))
        for attempt in range(3):
            sock.sendall(b"MerlynGestorTerm telnet\r\nPassword: ")
            pw = self.readline(echo=False)
            if pw is None:
                return
            if hashlib.sha256(pw.encode()).digest() == hashlib.sha256(nx.cfg["password"].encode()).digest():
                break
            nx.log(f"{peer}: contraseña incorrecta")
            sock.sendall(b"Acceso denegado\r\n")
        else:
            return
        nx.log(f"{peer}: sesión iniciada")
        pid, fd = pty.fork()
        if pid == 0:
            os.environ["TERM"] = "xterm-256color"
            shell = os.environ.get("SHELL", "/bin/bash")
            os.execv(shell, [shell, "-l"])
        try:
            while True:
                r, _, _ = select.select([sock, fd], [], [])
                if sock in r:
                    data = sock.recv(4096)
                    if not data:
                        break
                    os.write(fd, self.clean(data).replace(b"\r\n", b"\r").replace(b"\r\0", b"\r"))
                if fd in r:
                    try:
                        data = os.read(fd, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    sock.sendall(data.replace(bytes([IAC]), bytes([IAC, IAC])))
        finally:
            try:
                os.kill(pid, 9)
                os.waitpid(pid, 0)
            except OSError:
                pass
            nx.log(f"{peer}: sesión cerrada")


# ---------------------------------------------------------------- SSH / SFTP

class SshServer(Server):
    key, name, icon = "ssh", "SSH / SFTP", "network-server"
    fields = [("bind", "Dirección", "str", "0.0.0.0"), ("port", "Puerto", "int", 2222),
              ("keys", "authorized_keys", "file", os.path.expanduser("~/.ssh/authorized_keys"))]
    note = ("sshd de tu usuario (sin root). Solo acepta claves públicas: añade la clave "
            "del cliente a authorized_keys. Incluye SFTP.")

    def available(self):
        return None if find_sshd() else "Falta el servidor OpenSSH (openssh-server)"

    def start(self):
        d = os.path.join(SERVERS_DIR, "sshd")
        os.makedirs(d, mode=0o700, exist_ok=True)
        hostkey = os.path.join(d, "ssh_host_ed25519_key")
        if not os.path.exists(hostkey):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", hostkey], check=True)
        conf = os.path.join(d, "sshd_config")
        with open(conf, "w") as f:
            f.write(f"Port {int(self.cfg['port'])}\nListenAddress {self.cfg['bind']}\nHostKey {hostkey}\n"
                    f"AuthorizedKeysFile {self.cfg['keys']}\nPidFile {d}/sshd.pid\nUsePAM no\n"
                    "PasswordAuthentication no\nKbdInteractiveAuthentication no\n"
                    "Subsystem sftp internal-sftp\nPrintMotd no\n")
        self.run_process([find_sshd(), "-D", "-e", "-f", conf])
        self.log(f"escuchando en {self.cfg['bind']}:{self.cfg['port']} (usuario {os.environ.get('USER')})")


# ---------------------------------------------------------------- externos

class NfsServer(Server):
    key, name, icon = "nfs", "NFS", "folder-publicshare"
    fields = [("root", "Carpeta", "dir", os.path.expanduser("~")), ("clients", "Clientes", "str", "192.168.0.0/16"),
              ("write", "Lectura/escritura", "bool", False)]
    note = "Usa el servidor NFS del kernel (requiere nfs-utils y contraseña de administrador)."

    def available(self):
        return None if shutil.which("exportfs") else "Falta nfs-utils (instálalo con ./install-deps.sh)"

    def start(self):
        opts = ("rw" if self.cfg["write"] else "ro") + ",sync,no_subtree_check"
        spec = f"{self.cfg['clients']}:{self.cfg['root']}"
        script = f"systemctl start nfs-server && exportfs -o {opts} {spec} && exportfs -v"
        self.run_process(["pkexec", "sh", "-c", script])
        self.exported = spec
        self.started = True

    def running(self):
        return getattr(self, "started", False)

    def stop(self):
        if getattr(self, "started", False):
            subprocess.Popen(["pkexec", "exportfs", "-u", self.exported])
            self.started = False


class VncServer(Server):
    key, name, icon = "vnc", "VNC", "preferences-desktop-remote-desktop"
    fields = [("bind", "Dirección", "str", "127.0.0.1"), ("port", "Puerto", "int", 5900)]
    note = "Comparte tu escritorio Wayland con wayvnc (o x11vnc en X11). Sin contraseña: usa 127.0.0.1 + túnel SSH."

    def available(self):
        return None if shutil.which("wayvnc") or shutil.which("x11vnc") else \
            "Falta wayvnc (instálalo con ./install-deps.sh)"

    def start(self):
        if shutil.which("wayvnc") and os.environ.get("WAYLAND_DISPLAY"):
            self.run_process(["wayvnc", self.cfg["bind"], str(self.cfg["port"])])
        else:
            self.run_process(["x11vnc", "-listen", self.cfg["bind"], "-rfbport", str(self.cfg["port"]),
                              "-forever", "-nopw"])


class IperfServer(Server):
    key, name, icon = "iperf", "Iperf", "utilities-system-monitor"
    fields = [("port", "Puerto", "int", 5201)]
    note = "Servidor de pruebas de ancho de banda (iperf3)."

    def available(self):
        return None if shutil.which("iperf3") else "Falta iperf3 (instálalo con ./install-deps.sh)"

    def start(self):
        self.run_process(["iperf3", "-s", "-p", str(self.cfg["port"])])


# ---------------------------------------------------------------- Cron

def cron_match(field, value, lo, hi):
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/")
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-"))
        else:
            a = b = int(part)
        if a <= value <= b and (value - a) % step == 0:
            return True
    return False


def cron_due(expr, now):
    f = expr.split()
    if len(f) != 5:
        return False
    dow = (now.weekday() + 1) % 7
    return (cron_match(f[0], now.minute, 0, 59) and cron_match(f[1], now.hour, 0, 23) and
            cron_match(f[2], now.day, 1, 31) and cron_match(f[3], now.month, 1, 12) and
            (cron_match(f[4], dow, 0, 7) or (dow == 0 and cron_match(f[4], 7, 0, 7))))


class CronServer(Server):
    key, name, icon = "cron", "Cron", "alarm"
    fields = []
    note = "Programador de tareas propio (formato cron: min hora día mes día_semana). Funciona mientras MerlynGestorTerm está abierto."

    def start(self):
        self.thread = True
        self.last = None
        GLib.timeout_add_seconds(15, self.tick)
        self.log(f"{len(load_json(CRON_FILE, []))} tareas cargadas")

    def stop(self):
        self.thread = None

    def tick(self):
        if not self.thread:
            return False
        now = datetime.datetime.now().replace(second=0, microsecond=0)
        if now != self.last:
            self.last = now
            for job in load_json(CRON_FILE, []):
                if job.get("enabled", True) and cron_due(job["expr"], now):
                    self.log(f"ejecutando: {job['command']}")
                    threading.Thread(target=self.run_job, args=(job,), daemon=True).start()
        return True

    def run_job(self, job):
        p = subprocess.run(["sh", "-c", job["command"]], capture_output=True, text=True)
        for line in (p.stdout + p.stderr).splitlines()[-20:]:
            self.log(f"  {line}")
        self.log(f"«{job['command']}» terminó con código {p.returncode}")


SERVER_CLASSES = [TftpServer, FtpServer, HttpServer, SshServer, TelnetServer, NfsServer,
                  VncServer, CronServer, IperfServer]


class ServerManager:
    """Mantiene los servidores vivos aunque se cierre la ventana."""

    def __init__(self, app):
        self.app = app
        self.saved = load_json(SERVERS_FILE, {})
        self.servers = [cls(self) for cls in SERVER_CLASSES]
        self.lines = []
        self.listeners = []

    def log(self, text):
        line = time.strftime("%H:%M:%S ") + text
        self.lines = (self.lines + [line])[-1000:]
        GLib.idle_add(lambda: [cb(line) for cb in self.listeners] and False)

    def changed(self):
        for cb in self.listeners:
            cb(None)
        self.app.update_server_indicator()
        return False

    def persist(self):
        save_json(SERVERS_FILE, {s.key: s.cfg for s in self.servers})

    def running_count(self):
        return sum(1 for s in self.servers if s.running())

    def stop_all(self):
        for s in self.servers:
            if s.running():
                s.stop()


class ServersWindow:
    def __init__(self, app):
        self.app = app
        self.mgr = app.servers
        win = self.win = Gtk.Window(title="Servidores — MerlynGestorTerm", transient_for=app.window)
        win.set_default_size(820, 600)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=10)
        win.add(box)
        ips = ", ".join(local_ips()) or "sin red"
        head = Gtk.Label(xalign=0)
        head.set_markup(f"<b>Servidores de red</b>   <small>IP de este equipo: {ips}</small>")
        box.pack_start(head, False, False, 0)
        grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        box.pack_start(grid, False, False, 0)
        self.rows = {}
        for i, s in enumerate(self.mgr.servers):
            img = Gtk.Image.new_from_icon_name(s.icon, Gtk.IconSize.DND)
            name = Gtk.Label(xalign=0)
            name.set_markup(f"<b>{s.name}</b>\n<small>{GLib.markup_escape_text(s.note)}</small>")
            name.set_line_wrap(True)
            name.set_max_width_chars(60)
            name.set_hexpand(True)
            state = Gtk.Label(xalign=0, width_chars=14)
            sw = Gtk.Switch(valign=Gtk.Align.CENTER)
            sw.connect("state-set", self.on_switch, s)
            cfg = Gtk.Button.new_from_icon_name("emblem-system-symbolic", Gtk.IconSize.BUTTON)
            cfg.set_tooltip_text("Configurar")
            cfg.connect("clicked", self.on_config, s)
            for j, w in enumerate((img, name, state, sw, cfg)):
                grid.attach(w, j, i, 1, 1)
            self.rows[s.key] = (state, sw)
        box.pack_start(Gtk.Label(label="Registro", xalign=0), False, False, 0)
        self.logbuf = Gtk.TextBuffer()
        view = Gtk.TextView(buffer=self.logbuf, editable=False, monospace=True)
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.add(view)
        self.logview = view
        box.pack_start(sw, True, True, 0)
        self.logbuf.set_text("\n".join(self.mgr.lines))
        self.mgr.listeners.append(self.on_log)
        win.connect("destroy", lambda *_: self.mgr.listeners.remove(self.on_log))
        self.refresh()
        win.show_all()

    def on_log(self, line):
        if line is None:
            self.refresh()
            return
        self.logbuf.insert(self.logbuf.get_end_iter(), ("\n" if self.logbuf.get_char_count() else "") + line)
        self.logview.scroll_to_iter(self.logbuf.get_end_iter(), 0, False, 0, 0)

    def refresh(self):
        for s in self.mgr.servers:
            state, sw = self.rows[s.key]
            missing = s.available()
            run = s.running()
            state.set_markup("<span foreground='#2f9e44'>● En marcha</span>" if run else
                             (f"<span foreground='#c92a2a'>{GLib.markup_escape_text(missing)}</span>" if missing
                              else "○ Detenido"))
            state.set_line_wrap(True)
            sw.handler_block_by_func(self.on_switch)
            sw.set_active(run)
            sw.set_state(run)
            sw.handler_unblock_by_func(self.on_switch)
            sw.set_sensitive(not missing)

    def on_switch(self, sw, active, s):
        try:
            if active and not s.running():
                s.start()
            elif not active and s.running():
                s.stop()
                s.log("detenido")
        except Exception as e:  # noqa: BLE001
            s.log(f"error: {e}")
            d = Gtk.MessageDialog(transient_for=self.win, modal=True, message_type=Gtk.MessageType.ERROR,
                                  buttons=Gtk.ButtonsType.OK, text=f"No se pudo iniciar {s.name}",
                                  secondary_text=str(e))
            d.run()
            d.destroy()
        GLib.timeout_add(300, lambda: self.mgr.changed() and False)
        self.refresh()
        return True

    def on_config(self, _btn, s):
        if isinstance(s, CronServer):
            CronEditor(self.app, self.win)
            return
        d = Gtk.Dialog(title=f"Configurar {s.name}", transient_for=self.win, modal=True)
        d.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Guardar", Gtk.ResponseType.OK)
        grid = Gtk.Grid(column_spacing=8, row_spacing=6, margin=10)
        d.get_content_area().add(grid)
        widgets = {}
        for i, (key, label, kind, _default) in enumerate(s.fields):
            val = s.cfg.get(key)
            if kind == "bool":
                w = Gtk.CheckButton()
                w.set_active(bool(val))
            elif kind == "int":
                w = Gtk.SpinButton.new_with_range(1, 65535, 1)
                w.set_value(int(val))
            elif kind == "dir":
                w = Gtk.FileChooserButton(action=Gtk.FileChooserAction.SELECT_FOLDER)
                w.set_filename(os.path.expanduser(val))
            elif kind == "file":
                w = Gtk.Entry(text=val, hexpand=True)
            else:
                w = Gtk.Entry(text=str(val), hexpand=True, visibility=kind != "password")
            grid.attach(Gtk.Label(label=label, xalign=1), 0, i, 1, 1)
            grid.attach(w, 1, i, 1, 1)
            widgets[key] = (kind, w)
        d.show_all()
        if d.run() == Gtk.ResponseType.OK:
            for key, (kind, w) in widgets.items():
                s.cfg[key] = (w.get_active() if kind == "bool" else int(w.get_value()) if kind == "int"
                              else w.get_filename() if kind == "dir" else w.get_text())
            self.mgr.persist()
            if s.running():
                s.stop()
                s.start()
        d.destroy()


class CronEditor:
    def __init__(self, app, parent):
        d = Gtk.Dialog(title="Tareas programadas (cron)", transient_for=parent, modal=True)
        d.set_default_size(700, 400)
        d.add_buttons("Cerrar", Gtk.ResponseType.CLOSE)
        box = d.get_content_area()
        box.set_spacing(6)
        self.jobs = load_json(CRON_FILE, [])
        self.store = Gtk.ListStore(bool, str, str)
        view = Gtk.TreeView(model=self.store)
        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self.on_toggle)
        view.append_column(Gtk.TreeViewColumn("Activa", toggle, active=0))
        for i, title in ((1, "Cuándo (min hora día mes díasem)"), (2, "Comando")):
            r = Gtk.CellRendererText(editable=True)
            r.connect("edited", self.on_edit, i)
            c = Gtk.TreeViewColumn(title, r, text=i)
            c.set_resizable(True)
            c.set_expand(i == 2)
            view.append_column(c)
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.add(view)
        box.pack_start(sw, True, True, 0)
        row = Gtk.Box(spacing=6, margin=6)
        add = Gtk.Button(label="Añadir tarea")
        add.connect("clicked", lambda *_: self.store.append([True, "*/5 * * * *", "date >> ~/cron-merlyn.log"])
                    or self.save())
        rm = Gtk.Button(label="Eliminar")
        rm.connect("clicked", lambda *_: self.remove(view))
        row.pack_start(add, False, False, 0)
        row.pack_start(rm, False, False, 0)
        hint = Gtk.Label(label="Ejemplos: «0 9 * * 1-5» = 9:00 entre semana · «*/10 * * * *» = cada 10 min")
        hint.get_style_context().add_class("dim-label")
        row.pack_start(hint, False, False, 0)
        box.pack_start(row, False, False, 0)
        for j in self.jobs:
            self.store.append([j.get("enabled", True), j["expr"], j["command"]])
        d.show_all()
        d.run()
        d.destroy()

    def save(self):
        save_json(CRON_FILE, [{"enabled": r[0], "expr": r[1], "command": r[2]} for r in self.store])

    def on_toggle(self, _r, path):
        self.store[path][0] = not self.store[path][0]
        self.save()

    def on_edit(self, _r, path, text, col):
        if col == 1 and len(text.split()) != 5:
            return
        self.store[path][col] = text
        self.save()

    def remove(self, view):
        model, it = view.get_selection().get_selected()
        if it:
            model.remove(it)
            self.save()
