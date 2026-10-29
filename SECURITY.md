# Seguridad

## Cómo reportar una vulnerabilidad

**No abras un issue público.** Usa el reporte privado de GitHub: pestaña **Security → Report a
vulnerability** de este repositorio. Incluye la versión, la distribución y los pasos para
reproducirlo. Intentaremos responder en un plazo de 7 días.

## Versiones con soporte

Solo la última versión publicada recibe correcciones de seguridad.

## Cómo trata MerlynGestorTerm tus datos

- **Contraseñas y passphrases**: se guardan cifradas en el llavero del sistema (Secret Service:
  GNOME Keyring o KWallet). Nunca se escriben en `merlyngestorterm-data/`. ssh las recibe mediante
  `SSH_ASKPASS`, no por la línea de comandos.
- **RDP**: FreeRDP recibe la contraseña con `/p:`; en la terminal se muestra como `****`.
- **Claves privadas**: si eliges «copiar a la carpeta portable», se guardan en
  `merlyngestorterm-data/keys/` con permisos `600`. Esa carpeta viaja contigo: protégela como
  protegerías tu `~/.ssh`.
- **Exportar sesiones** genera un JSON **sin** contraseñas, pero con hosts, usuarios y rutas de claves.
- `merlyngestorterm-data/` está en `.gitignore`: no la subas nunca a un repositorio.

## Servidores integrados

| Servidor | Cifrado | Valor por defecto |
|---|---|---|
| FTP | **no** | todas las interfaces, puerto 2121, usuario y contraseña obligatorios salvo modo anónimo |
| TFTP | **no**, sin autenticación (así es el protocolo) | solo lectura |
| Telnet | **no** | **solo 127.0.0.1**, contraseña obligatoria; da una shell de tu usuario |
| HTTP | no | solo lectura de la carpeta elegida |
| VNC | no, sin contraseña | **solo 127.0.0.1**; úsalo a través de un túnel SSH |
| SSH/SFTP | sí | solo claves públicas (`authorized_keys`), sin contraseñas |

Usa FTP, TFTP, Telnet, HTTP y VNC solo en redes de confianza y detenlos al terminar. Para
transferir archivos por Internet usa SSH/SFTP.
