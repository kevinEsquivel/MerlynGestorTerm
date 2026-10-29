#!/usr/bin/env python3
"""Pruebas funcionales sin interfaz gráfica: servidores propios (FTP, TFTP,
HTTP, Telnet, Cron), backends del explorador, Wake on LAN e importación de
MobaXterm.ini. Uso: python3 tests/smoke_test.py"""
import datetime
import ftplib
import os
import socket
import struct
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, ROOT)
WORK = tempfile.mkdtemp(prefix="mgt-test-")
os.environ["MERLYN_HOME"] = os.path.join(WORK, "data")

import browser  # noqa: E402
import core  # noqa: E402
import games  # noqa: E402, F401  (comprueba que el módulo importa)
import servers  # noqa: E402
import terminal  # noqa: E402, F401
import tools  # noqa: E402
import window  # noqa: E402, F401

FAILURES = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FALLO ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeApp:
    def update_server_indicator(self):
        pass


def main():
    share = os.path.join(WORK, "share")
    os.makedirs(os.path.join(share, "sub dir"))
    with open(os.path.join(share, "a.txt"), "w") as f:
        f.write("hola\n")
    mgr = servers.ServerManager(FakeApp())
    mgr.log = lambda _t: None
    by = {s.key: s for s in mgr.servers}

    print("FTP (servidor propio + backend del explorador)")
    ftp = by["ftp"]
    port = free_port()
    ftp.cfg.update(root=share, bind="127.0.0.1", port=port, user="u", password="pw", pasv="50600-50620")
    ftp.start()
    s = core.new_session(type="ftp", host="127.0.0.1", port=port, user="u")
    browser.secret_get = lambda _sid, _kind: "pw"  # en CI no hay llavero del sistema
    be = browser.FtpBackend(s)
    cwd, entries = be.list("")
    check("listar", cwd == "/" and {e.name for e in entries} == {"a.txt", "sub dir"})
    up = os.path.join(WORK, "up.txt")
    with open(up, "w") as f:
        f.write("subido\n")
    be.upload([up], "/")
    be.mkdir("/nueva")
    be.rename("/up.txt", "/nueva/up2.txt")
    check("subir, crear carpeta y renombrar", os.path.exists(os.path.join(share, "nueva", "up2.txt")))
    dl = os.path.join(WORK, "dl")
    os.makedirs(dl)
    be.download([("/sub dir", browser.Entry("sub dir", True))], dl)
    check("descargar carpeta", os.path.isdir(os.path.join(dl, "sub dir")))
    be.delete([("/nueva", browser.Entry("nueva", True))])
    check("borrar carpeta con contenido", not os.path.exists(os.path.join(share, "nueva")))
    try:
        c = ftplib.FTP()
        c.connect("127.0.0.1", port)
        c.login("u", "mala")
        check("rechaza contraseña incorrecta", False)
    except ftplib.error_perm:
        check("rechaza contraseña incorrecta", True)
    try:
        be.ftp.cwd("/../../..")
        check("no sale de la carpeta compartida", be.ftp.pwd() == "/")
    except ftplib.error_perm:
        check("no sale de la carpeta compartida", True)
    ftp.stop()

    print("TFTP")
    tftp = by["tftp"]
    tport = free_port()
    tftp.cfg.update(root=share, bind="127.0.0.1", port=tport, write=True)
    tftp.start()
    payload = os.urandom(1300)
    _tftp_put(tport, "big.bin", payload)
    time.sleep(0.3)
    check("subir y bajar 1300 bytes", _tftp_get(tport, "big.bin") == payload)
    check("archivo inexistente", _tftp_get(tport, "nope") == "ERR no existe")
    check("no sale de la carpeta", _tftp_get(tport, "../../etc/passwd").startswith("ERR"))
    tftp.stop()

    print("HTTP")
    http = by["http"]
    hport = free_port()
    http.cfg.update(root=share, bind="127.0.0.1", port=hport)
    http.start()
    check("descargar archivo", urllib.request.urlopen(f"http://127.0.0.1:{hport}/a.txt").read() == b"hola\n")
    http.stop()

    print("Telnet")
    tn = by["telnet"]
    tport = free_port()
    tn.cfg.update(bind="127.0.0.1", port=tport, password="clave")
    tn.start()
    c = socket.create_connection(("127.0.0.1", tport), timeout=5)
    time.sleep(0.3)
    c.recv(4096)
    c.sendall(b"mala\r\n")
    time.sleep(0.3)
    check("rechaza contraseña incorrecta", b"denegado" in c.recv(4096))
    c.sendall(b"clave\r\n")
    time.sleep(1.0)
    c.recv(65536)
    c.sendall(b"echo MGT_$((40+2))\r\n")
    out = b""
    deadline = time.time() + 5
    while b"MGT_42" not in out and time.time() < deadline:
        try:
            out += c.recv(65536)
        except socket.timeout:
            break
    check("shell tras iniciar sesión", b"MGT_42" in out)
    c.close()
    tn.stop()

    print("Cron")
    now = datetime.datetime(2026, 10, 29, 9, 0)  # jueves
    check("0 9 * * 1-5", servers.cron_due("0 9 * * 1-5", now))
    check("*/10 * * * *", servers.cron_due("*/10 * * * *", now))
    check("fin de semana no", not servers.cron_due("0 9 * * 0,6", now))

    print("Herramientas")
    r = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    r.bind(("127.0.0.1", 0))
    r.settimeout(2)
    tools.WakeOnLan.send("aa:bb:cc:dd:ee:ff", "127.0.0.1", r.getsockname()[1])
    pkt = r.recv(200)
    check("paquete mágico Wake on LAN", pkt == b"\xff" * 6 + bytes.fromhex("aabbccddeeff") * 16)
    check("rangos de puertos", tools.PortScanner.parse("20-22, 80") == [20, 21, 22, 80])
    check("explorador local", len(browser.LocalBackend().list(share)[1]) >= 2)
    pm = tools.PackageManager
    check("paquetes pacman", pm.parse_pacman("extra/htop 3.3-1 [installed]\n    visor\n")
          == [["✔", "extra", "htop", "3.3-1", "visor"]])
    check("paquetes apt", pm.parse_apt("htop - visor\n", {"htop"}) == [["✔", "apt", "htop", "", "visor"]])
    check("paquetes dnf", pm.parse_dnf("htop.x86_64 : visor\n btop.x86_64\totro\n", set())
          == [["", "dnf", "htop", "", "visor"], ["", "dnf", "btop", "", "otro"]])

    print("Importar MobaXterm.ini")
    ini = os.path.join(WORK, "MobaXterm.ini")
    with open(ini, "w") as f:
        f.write("[Bookmarks]\nSubRep=\nImgNum=42\nweb=#109#0%10.0.0.5%2222%root%%-1\n"
                "[Bookmarks_1]\nSubRep=Clientes\\ACME\nImgNum=41\nwin=#91#4%rdp.lan%3389%admin%0\n")
    got = {(x["name"], x["type"], x["group"], x["port"]) for x in core.import_mobaxterm_ini(ini)}
    check("sesiones SSH y RDP con carpetas", got == {("web", "ssh", "", 2222),
                                                     ("win", "rdp", "Clientes/ACME", 3389)})
    argv, err = core.build_argv(core.new_session(host="h", user="u", port=2200))
    check("comando SSH", err is None and argv[0] == "ssh" and "u@h" in argv and "2200" in argv)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} prueba(s) fallida(s): {', '.join(FAILURES)}")
        return 1
    print("Todas las pruebas pasaron.")
    return 0


def _tftp_get(port, name):
    c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    c.settimeout(3)
    c.sendto(struct.pack("!H", 1) + name.encode() + b"\0octet\0", ("127.0.0.1", port))
    data = b""
    while True:
        pkt, addr = c.recvfrom(516)
        op, blk = struct.unpack("!HH", pkt[:4])
        if op == 5:
            return "ERR " + pkt[4:-1].decode()
        data += pkt[4:]
        c.sendto(struct.pack("!HH", 4, blk), addr)
        if len(pkt) < 516:
            return data


def _tftp_put(port, name, payload):
    c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    c.settimeout(3)
    c.sendto(struct.pack("!H", 2) + name.encode() + b"\0octet\0", ("127.0.0.1", port))
    _pkt, addr = c.recvfrom(516)
    blk = 1
    for i in range(0, len(payload) + 1, 512):
        c.sendto(struct.pack("!HH", 3, blk) + payload[i:i + 512], addr)
        c.recvfrom(4)
        blk += 1


if __name__ == "__main__":
    sys.exit(main())
