#!/usr/bin/env bash
# Instala las dependencias de MerlynGestorTerm en distribuciones Debian/Ubuntu, Arch y Fedora
# (y sus derivadas: Mint, Pop!_OS, Manjaro, EndeavourOS, Omarchy, Nobara, RHEL/Rocky/Alma...).
#
# Uso: ./install-deps.sh [opciones]
#   -y, --si           no preguntar nada (instala directamente)
#   --minimo           solo lo imprescindible para abrir MerlynGestorTerm (terminal, SSH, SFTP, FTP)
#   --servidor-ssh     instala también el servidor OpenSSH (para la función Servidores > SSH)
#   --escritorio       crea un acceso directo en el menú de aplicaciones
#   --comprobar        solo muestra qué está instalado y qué falta
#   --simular          muestra los comandos sin ejecutarlos
#   -h, --ayuda        esta ayuda
set -uo pipefail

DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
ASSUME_YES=0 MINIMAL=0 SSH_SERVER=0 DESKTOP=0 CHECK_ONLY=0 DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    -y|--si|--yes) ASSUME_YES=1 ;;
    --minimo|--minimal) MINIMAL=1 ;;
    --servidor-ssh) SSH_SERVER=1 ;;
    --escritorio|--desktop) DESKTOP=1 ;;
    --comprobar|--check) CHECK_ONLY=1 ;;
    --simular|--dry-run) DRY_RUN=1 ;;
    -h|--ayuda|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Opción desconocida: $arg (usa --ayuda)" >&2; exit 2 ;;
  esac
done

if [ -t 1 ]; then B=$'\e[1m' G=$'\e[32m' Y=$'\e[33m' R=$'\e[31m' N=$'\e[0m'; else B='' G='' Y='' R='' N=''; fi
info() { printf '%s==>%s %s\n' "$B" "$N" "$*"; }
ok()   { printf '  %s✔%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
fail() { printf '  %s✖%s %s\n' "$R" "$N" "$*"; }

# ------------------------------------------------------------------ distribución
OS_RELEASE="${MERLYN_OS_RELEASE:-/etc/os-release}"
[ -r "$OS_RELEASE" ] || { echo "No encuentro $OS_RELEASE: no sé qué distribución es." >&2; exit 1; }
# shellcheck disable=SC1090
. "$OS_RELEASE"
FAMILY=""
for id in ${ID:-} ${ID_LIKE:-}; do
  case "$id" in
    debian|ubuntu|linuxmint|pop|elementary|zorin|kali|raspbian) FAMILY=debian; break ;;
    arch|archlinux|manjaro|endeavouros|omarchy|garuda|cachyos|artix) FAMILY=arch; break ;;
    fedora|rhel|centos|rocky|almalinux|nobara|ol) FAMILY=fedora; break ;;
  esac
done

# ------------------------------------------------------------------ paquetes
# Imprescindibles: Python + GTK3 + VTE (terminal) + libsecret/llavero + cliente SSH.
# Recomendado: GtkSourceView 4 (editor integrado). Si no existe en la distro, MerlynGestorTerm
# funciona igual y abre los archivos con el editor predeterminado.
case "$FAMILY" in
  debian)
    REQUIRED=(python3 python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91 gir1.2-secret-1 gnome-keyring
              openssh-client iproute2 iputils-ping)
    RECOMMENDED=(gir1.2-gtksource-4)
    # alternativas separadas por | : se instala la primera que exista
    OPTIONAL=("freerdp3-x11|freerdp2-x11" "tigervnc-viewer|xtightvncviewer" picocom telnet rsh-client mosh
              putty-tools iperf3 wayvnc nfs-kernel-server xserver-xephyr tcpdump awscli hwdata xwayland)
    SSHD=(openssh-server) ;;
  arch)
    REQUIRED=(python python-gobject gtk3 vte3 libsecret gnome-keyring openssh iproute2 iputils)
    RECOMMENDED=(gtksourceview4)
    OPTIONAL=(freerdp tigervnc picocom inetutils mosh putty iperf3 wayvnc nfs-utils xorg-server-xephyr
              tcpdump aws-cli-v2 hwdata xorg-xwayland)
    SSHD=() ;;  # en Arch el paquete openssh ya incluye sshd
  fedora)
    REQUIRED=(python3 python3-gobject gtk3 vte291 libsecret gnome-keyring openssh-clients iproute iputils)
    RECOMMENDED=(gtksourceview4)
    OPTIONAL=(freerdp tigervnc picocom telnet rsh mosh putty iperf3 wayvnc nfs-utils xorg-x11-server-Xephyr
              tcpdump awscli2 hwdata xorg-x11-server-Xwayland)
    SSHD=(openssh-server) ;;
esac

# ------------------------------------------------------------------ comprobación
check() {
  info "Comprobando MerlynGestorTerm en este equipo"
  NX_COLOR=$([ -t 1 ] && echo 1 || echo 0) python3 - <<'PY' 2>/dev/null || fail "Python 3 o PyGObject no están instalados"
import os
import gi
C = os.environ.get("NX_COLOR") == "1"
paint = lambda code, s: f"\033[{code}m{s}\033[0m" if C else s
def mod(ns, ver, label, needed=True):
    try:
        gi.require_version(ns, ver)
        __import__("gi.repository", fromlist=[ns])
        print(f"  {paint(32, '✔')} {label}")
        return True
    except (ValueError, ImportError):
        print(f"  {paint(31, '✖') if needed else paint(33, '!')} {label} {'(NECESARIO)' if needed else '(opcional)'}")
        return False
mod("Gtk", "3.0", "GTK 3")
mod("Vte", "2.91", "VTE (terminal)")
mod("Secret", "1", "libsecret (guardar contraseñas)")
mod("GtkSource", "4", "GtkSourceView 4 (editor integrado)", needed=False)
PY
  tools=(
    "ssh:SSH" "sftp:SFTP" "ssh-keygen:generador de claves"
    "xfreerdp3|xfreerdp|wlfreerdp:RDP" "vncviewer|remmina:VNC" "picocom|screen|minicom:Serial"
    "telnet:Telnet" "rlogin|rsh:Rsh" "mosh:Mosh" "Xephyr:XDMCP" "puttygen:convertir claves .ppk"
    "tcpdump:captura de paquetes" "aws:Amazon S3" "iperf3:servidor Iperf"
    "wayvnc|x11vnc:servidor VNC" "exportfs:servidor NFS" "sshd|/usr/sbin/sshd:servidor SSH"
  )
  for t in "${tools[@]}"; do
    cmds="${t%%:*}" label="${t#*:}" found=""
    IFS='|' read -ra alts <<<"$cmds"
    for c in "${alts[@]}"; do
      if command -v "$c" >/dev/null 2>&1 || [ -x "$c" ]; then found="$c"; break; fi
    done
    if [ -n "$found" ]; then ok "$label ($found)"; else warn "$label: no instalado (opcional)"; fi
  done
  local buses=""
  command -v busctl >/dev/null && buses="$(busctl --user list 2>/dev/null)"
  if grep -q org.freedesktop.secrets <<<"$buses"; then
    ok "Servicio de llavero activo"
  else
    warn "No hay servicio de llavero activo: las contraseñas no se podrán guardar hasta iniciar sesión" \
         "con gnome-keyring (o KWallet) en marcha"
  fi
}

if [ "$CHECK_ONLY" = 1 ]; then check; exit 0; fi

if [ -z "$FAMILY" ]; then
  echo "${R}Distribución no soportada:${N} ${PRETTY_NAME:-$ID}"
  echo "Instala a mano: Python 3, PyGObject, GTK 3, VTE 2.91 (GTK3), libsecret, gnome-keyring y OpenSSH."
  exit 1
fi

# ------------------------------------------------------------------ gestor de paquetes
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null; then SUDO="sudo"; else echo "Necesito root o sudo." >&2; exit 1; fi
fi

run() {
  printf '  $ %s\n' "$*"
  [ "$DRY_RUN" = 1 ] && return 0
  "$@"
}

case "$FAMILY" in
  debian)
    PKGTOOL=apt-cache
    # sin tuberías con grep -q: con pipefail, el SIGPIPE de apt-cache daría falso negativo
    available() { local out; out="$(apt-cache policy "$1" 2>/dev/null)"; grep -q 'Candidate: [^(]' <<<"$out"; }
    refresh()   { run $SUDO apt-get update; }
    install()   { run $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"; } ;;
  arch)
    PKGTOOL=pacman
    available() { pacman -Si "$1" >/dev/null 2>&1; }
    # Nunca "pacman -Sy" suelto (actualización parcial). Si no hay base de datos de
    # paquetes (instalación nueva), se sincroniza con una actualización completa.
    refresh()   { pacman -Si pacman >/dev/null 2>&1 || run $SUDO pacman -Syu --noconfirm; }
    install()   { run $SUDO pacman -S --needed --noconfirm "$@" ||
                  { warn "Si pacman no encuentra algún paquete, actualiza antes: $SUDO pacman -Syu"; return 1; }; } ;;
  fedora)
    PKGTOOL=dnf
    available() { dnf -q list --available "$1" >/dev/null 2>&1 || rpm -q "$1" >/dev/null 2>&1; }
    refresh()   { run $SUDO dnf -q makecache; }
    install()   { run $SUDO dnf install -y "$@"; } ;;
esac

# Devuelve la primera alternativa disponible de "a|b|c"
pick() {
  local alt
  IFS='|' read -ra alts <<<"$1"
  for alt in "${alts[@]}"; do
    if [ "$DRY_RUN" = 1 ] && ! command -v "$PKGTOOL" >/dev/null 2>&1; then
      echo "$alt"; return 0   # simulando en otra distro: no se puede consultar el repositorio
    fi
    if available "$alt"; then echo "$alt"; return 0; fi
  done
  return 1
}

PRETTY="${PRETTY_NAME:-$ID}"
info "Distribución: $PRETTY (familia $FAMILY)"
echo "  Imprescindible: ${REQUIRED[*]}"
echo "  Recomendado:    ${RECOMMENDED[*]}"
[ "$MINIMAL" = 0 ] && echo "  Opcional:       ${OPTIONAL[*]}"
[ "$SSH_SERVER" = 1 ] && [ ${#SSHD[@]} -gt 0 ] && echo "  Servidor SSH:   ${SSHD[*]}"
if [ "$ASSUME_YES" = 0 ] && [ "$DRY_RUN" = 0 ]; then
  read -rp "¿Continuar con la instalación? [S/n] " answer
  case "${answer,,}" in n|no) echo "Cancelado."; exit 0 ;; esac
fi

if [ "$FAMILY" = fedora ] && [ "${ID:-}" != fedora ] && [ "$MINIMAL" = 0 ]; then
  warn "En RHEL/Rocky/Alma varios paquetes opcionales están en EPEL: $SUDO dnf install epel-release"
fi

info "Actualizando la lista de paquetes"
refresh || warn "No se pudo actualizar la lista de paquetes; sigo igualmente"

info "Instalando lo imprescindible"
if ! install "${REQUIRED[@]}"; then
  fail "Falló la instalación de los paquetes imprescindibles."
  exit 1
fi

SKIPPED=()
install_list() {
  local spec pkg list=()
  for spec in "$@"; do
    if pkg="$(pick "$spec")"; then list+=("$pkg"); else SKIPPED+=("$spec"); fi
  done
  [ ${#list[@]} -eq 0 ] && return 0
  if ! install "${list[@]}"; then
    warn "La instalación conjunta falló; pruebo paquete a paquete"
    for pkg in "${list[@]}"; do install "$pkg" || SKIPPED+=("$pkg"); done
  fi
}

info "Instalando lo recomendado"
install_list "${RECOMMENDED[@]}"
if [ "$MINIMAL" = 0 ]; then
  info "Instalando lo opcional (RDP, VNC, serie, Telnet, Mosh, servidores, S3...)"
  install_list "${OPTIONAL[@]}"
fi
if [ "$SSH_SERVER" = 1 ] && [ ${#SSHD[@]} -gt 0 ]; then
  info "Instalando el servidor OpenSSH"
  install_list "${SSHD[@]}"
  warn "Algunas distros activan el servicio sshd del sistema al instalarlo (puerto 22)."
  warn "MerlynGestorTerm no lo necesita; si no lo quieres: $SUDO systemctl disable --now ssh sshd 2>/dev/null"
fi
[ ${#SKIPPED[@]} -gt 0 ] && warn "No disponibles en esta distro (se omiten): ${SKIPPED[*]}"

chmod +x "$DIR/merlyngestorterm" "$DIR/merlyngestorterm-askpass" "$DIR/merlyngestorterm.py" 2>/dev/null || true

if [ "$DESKTOP" = 1 ]; then
  info "Creando acceso directo en el menú"
  apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
  if [ "$DRY_RUN" = 0 ]; then
    mkdir -p "$apps"
    cat >"$apps/merlyngestorterm.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=MerlynGestorTerm
Comment=Terminal con SSH, SFTP, RDP, VNC y herramientas de red
Exec="$DIR/merlyngestorterm"
Icon=utilities-terminal
Terminal=false
Categories=Network;System;TerminalEmulator;
EOF
    ok "$apps/merlyngestorterm.desktop"
  else
    echo "  (simulado) $apps/merlyngestorterm.desktop"
  fi
fi

[ "$DRY_RUN" = 0 ] && check
echo
info "Listo. Abre MerlynGestorTerm con: $DIR/merlyngestorterm"
