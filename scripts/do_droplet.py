#!/usr/bin/env python3
"""Lanza y destruye Droplets efímeros en DigitalOcean.

Sólo biblioteca estándar: basta con Python 3.9+ y `ssh` en el PATH. No hay que
instalar nada, para que el mismo script funcione en cualquier máquina desde la
que quieras conectarte.

    python scripts/do_droplet.py --help
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Sin el /v2: las rutas lo llevan ya, igual que en la documentación de la API.
API = "https://api.digitalocean.com"

# Valores por defecto; cualquiera se puede sobrescribir desde .env
DEFAULTS = {
    "DO_REGION": "nyc1",
    "DO_SIZE": "s-2vcpu-4gb",
    "DO_IMAGE": "ubuntu-24-04-x64",
    "DO_DROPLET_NAME": "proyecto-01",
    "DO_TAG": "ephemeral",
    # Plantilla de primer arranque. Hay más de una porque no todas las máquinas
    # quieren lo mismo: cloud-init.mini.yaml es para el control, que con 512 MB
    # no puede con Claude Code pero sí lanza droplets grandes.
    "DO_CLOUD_INIT": "cloud-init.yaml",
    "DO_SSH_KEY_FILE": str(Path.home() / ".ssh" / "do_droplet"),
    "DO_SSH_KEYS": "",  # nombres/fingerprints/IDs separados por coma; vacío = todas
    "DO_SSH_USER": "root",
    # El droplet escucha en ambos; se prueba en este orden y se usa el primero
    # que responda. Sirve para redes que bloquean el 22 saliente.
    "DO_SSH_PORTS": "22,443",
    # Usuario del droplet que acaba con las credenciales y los repos. Lo crea
    # cloud-init. El aprovisionamiento entra siempre como root (hace falta para
    # escribir en el home de otro usuario), pase lo que pase con DO_SSH_USER.
    "DO_DEV_USER": "deploy",
    # Repos que se clonan solos al aprovisionar: "owner/repo,owner/otro".
    "DO_REPOS": "",
    # Servicios que quedan corriendo en el droplet, por nombre de descriptor en
    # services/: "telegram-coordinator,otro". Su repo se clona solo.
    "DO_SERVICES": "",
    "GIT_USER_NAME": "",
    "GIT_USER_EMAIL": "",
}


# ---------------------------------------------------------------- configuración


def load_env() -> None:
    """Carga .env sin dependencias. Las variables reales del entorno mandan."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def cfg(key: str) -> str:
    return os.environ.get(key) or DEFAULTS.get(key, "")


def token() -> str:
    tok = os.environ.get("DO_TOKEN") or os.environ.get("DIGITALOCEAN_TOKEN") or os.environ.get(
        "DIGITALOCEAN_ACCESS_TOKEN"
    )
    if not tok:
        die(
            "Falta el token. Copia .env.example a .env y pon ahí tu Personal Access Token\n"
            "  (créalo en https://cloud.digitalocean.com/account/api/tokens)"
        )
    return tok


def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"\nERROR: {msg}\n", file=sys.stderr)
    raise SystemExit(1)


def log(msg: str) -> None:
    print(msg, flush=True)


def confirmar(pregunta: str) -> bool:
    """Pregunta por teclado; True sólo si contestan 'si'.

    Donde no hay terminal no se puede confirmar nada, y a `destroy` se le llama
    también desde ahí: el bot de Telegram que opera el lanzador desde el móvil le
    cierra el stdin al comando, igual que `ssh maquina 'comando'` o cron. En esos
    sitios `input()` levanta EOFError y suelta un traceback que no dice qué hacer.
    Se exige `--yes` explícito en vez de dar por buena una confirmación que nadie
    ha escrito. Un stdin con datos (`echo si | ... destroy`) sigue valiendo.
    """
    try:
        return input(pregunta).strip().lower() == "si"
    except EOFError:
        die("No hay terminal donde confirmar. Repite el comando con --yes.")


def force_utf8_output() -> None:
    """La consola de Windows usa cp1252 por defecto y peta con acentos y símbolos."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass


# ------------------------------------------------------------------ cliente API


def api(method: str, path: str, body: dict | None = None) -> dict:
    """Petición a la API v2 con reintentos ante 429 y errores 5xx."""
    url = path if path.startswith("http") else f"{API}{path}"
    payload = json.dumps(body).encode("utf-8") if body is not None else None

    last_error = ""
    for attempt in range(6):
        req = urllib.request.Request(url, data=payload, method=method)
        req.add_header("Authorization", f"Bearer {token()}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429:  # burst limit: la API dice cuánto esperar
                wait = int(exc.headers.get("retry-after", "10"))
                log(f"  rate limit alcanzado, esperando {wait}s…")
                time.sleep(wait)
                continue
            if exc.code >= 500 and attempt < 5:
                time.sleep(2**attempt)
                continue
            die(f"HTTP {exc.code} en {method} {url}\n{detail}")
        except urllib.error.URLError as exc:
            last_error = str(exc.reason)
            time.sleep(2**attempt)
        except OSError as exc:
            # urlopen() puede volver bien y expirar luego, al leer el cuerpo:
            # eso llega como TimeoutError, que NO es un URLError y se escapaba
            # del bucle reventando el comando entero. Visto de verdad contra la
            # API de DigitalOcean, dos veces en una misma sesión.
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(2**attempt)
    die(f"Sin respuesta de la API tras varios reintentos: {last_error}")


def paged(path: str, key: str) -> list[dict]:
    """Recorre todas las páginas de una colección."""
    items: list[dict] = []
    sep = "&" if "?" in path else "?"
    url = f"{path}{sep}per_page=200"
    while url:
        data = api("GET", url)
        items.extend(data.get(key, []))
        url = data.get("links", {}).get("pages", {}).get("next", "")
    return items


# -------------------------------------------------------------------- claves SSH


def account_keys() -> list[dict]:
    return paged("/v2/account/keys", "ssh_keys")


def selected_keys() -> list[dict]:
    """Claves de la cuenta que se embeberán en el droplet.

    DO_SSH_KEYS vacío significa "todas las de la cuenta", que es justo lo que
    quieres cuando trabajas desde varias máquinas: cada laptop registra su clave
    y todas entran automáticamente en los droplets nuevos.
    """
    keys = account_keys()
    if not keys:
        die(
            "No hay ninguna clave SSH en la cuenta. Registra una primero:\n"
            "  python scripts/do_droplet.py keygen        # si aún no tienes par de claves\n"
            "  python scripts/do_droplet.py register-key"
        )
    wanted = [w.strip() for w in cfg("DO_SSH_KEYS").split(",") if w.strip()]
    if not wanted:
        return keys

    chosen, missing = [], []
    for want in wanted:
        match = next(
            (k for k in keys if want in (k["name"], k["fingerprint"], str(k["id"]))), None
        )
        if match:
            chosen.append(match)
        else:
            missing.append(want)
    if missing:
        die(
            f"No encontré estas claves en la cuenta: {', '.join(missing)}\n"
            "Míralas con: python scripts/do_droplet.py keys"
        )
    return chosen


def cmd_keygen(args: argparse.Namespace) -> None:
    path = Path(args.file or cfg("DO_SSH_KEY_FILE")).expanduser()
    if path.exists():
        log(f"Ya existe {path}, no se toca.")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(path), "-N", "", "-C", args.comment],
            check=True,
        )
        log(f"Par de claves creado en {path}")
    log(f"\nClave pública:\n{path.with_suffix('.pub').read_text().strip()}")
    log("\nSiguiente paso: python scripts/do_droplet.py register-key")


def cmd_keys(args: argparse.Namespace) -> None:
    for key in account_keys():
        log(f"{key['id']:<12} {key['name']:<28} {key['fingerprint']}")


def cmd_register_key(args: argparse.Namespace) -> None:
    pub_path = Path(args.file or (cfg("DO_SSH_KEY_FILE") + ".pub")).expanduser()
    if not pub_path.exists():
        die(f"No existe {pub_path}. Genera el par con: python scripts/do_droplet.py keygen")
    public_key = pub_path.read_text(encoding="utf-8").strip()

    for key in account_keys():
        if key["public_key"].split()[1] == public_key.split()[1]:
            log(f"Esa clave ya está registrada como '{key['name']}' (id {key['id']}).")
            return

    name = args.name or f"{socket.gethostname()}-do-droplet"
    key = api("POST", "/v2/account/keys", {"name": name, "public_key": public_key})["ssh_key"]
    log(f"Clave registrada: {key['name']} (id {key['id']}, {key['fingerprint']})")


# ----------------------------------------------------------------- descubrimiento


def cmd_sizes(args: argparse.Namespace) -> None:
    region = args.region or cfg("DO_REGION")
    log(f"Tamaños disponibles en {region} (RAM >= {args.min_memory} MB):\n")
    log(f"{'SLUG':<26} {'vCPU':>4} {'RAM':>8} {'DISCO':>7} {'$/MES':>8}")
    for size in paged("/v2/sizes", "sizes"):
        if not size["available"] or region not in size["regions"]:
            continue
        if size["memory"] < args.min_memory:
            continue
        log(
            f"{size['slug']:<26} {size['vcpus']:>4} {size['memory']:>6} MB "
            f"{size['disk']:>4} GB {size['price_monthly']:>8.2f}"
        )


def cmd_regions(args: argparse.Namespace) -> None:
    for region in paged("/v2/regions", "regions"):
        if region["available"]:
            log(f"{region['slug']:<8} {region['name']}")


def cmd_images(args: argparse.Namespace) -> None:
    for image in paged("/v2/images?type=distribution", "images"):
        if args.filter.lower() in image["slug"].lower():
            log(f"{image['slug']:<28} {image['distribution']} {image['name']}")


# ------------------------------------------------------------------- ciclo de vida


def check_user_data_encoding(text: str) -> None:
    """Rechaza los caracteres que hacen que cloud-init tire el fichero entero.

    Por el camino hasta el droplet, el user_data acaba releyéndose como latin-1.
    Un carácter cuya codificación UTF-8 contenga un byte entre 0x80 y 0x9F se
    convierte así en un carácter de control C1, y el parser de YAML de
    cloud-init lo rechaza con:

        Failed loading yaml blob. unacceptable character #x0097

    Y no falla sólo esa línea: **descarta la configuración completa**. El droplet
    arranca sin usuario deploy, sin ufw, sin el 443 y sin el watchdog de sshd,
    pero con la IP puesta y SSH de root funcionando, así que parece correcto.
    Nos pasó con un simple '×' en un comentario.

    Las minúsculas acentuadas (á 0xC3 0xA1, ñ 0xC3 0xB1) se salvan porque su
    segundo byte cae por encima de 0x9F. Las MAYÚSCULAS acentuadas (Á 0xC3 0x81,
    Ñ 0xC3 0x91) y la raya (— 0xE2 0x80 0x94) no. De ahí que esto se compruebe
    en vez de confiar en la vista.
    """
    for number, line in enumerate(text.splitlines(), start=1):
        for char in line:
            if any(0x80 <= byte <= 0x9F for byte in char.encode("utf-8")):
                die(
                    f"cloud-init.yaml tiene un carácter que rompería el arranque:\n"
                    f"  línea {number}: {char!r} (U+{ord(char):04X})\n"
                    f"  {line.strip()[:70]}\n\n"
                    "Su codificación UTF-8 lleva un byte entre 0x80 y 0x9F, que al\n"
                    "releerse como latin-1 se vuelve un carácter de control y hace que\n"
                    "cloud-init DESCARTE TODA la configuración en silencio: el droplet\n"
                    "arrancaría sin deploy, sin ufw y sin el watchdog de sshd.\n"
                    "Cámbialo por ASCII ('x' en vez de '×', '-' en vez de '—')."
                )


def build_user_data(keys: list[dict], perfil: str = "") -> str:
    """Inyecta las claves públicas en la plantilla de cloud-init.

    Sustituye la línea marcadora respetando su sangría, que en YAML es lo que
    determina si el fichero es válido.

    El perfil elige la plantilla: no todas las máquinas quieren lo mismo. Una de
    512 MB no puede con Claude Code (es Node, y los droplets vienen sin swap:
    el kernel mata el proceso), pero sí le sobra para lanzar droplets grandes.
    """
    nombre = perfil or cfg("DO_CLOUD_INIT")
    template = ROOT / nombre
    if not template.exists():
        disponibles = ", ".join(sorted(p.name for p in ROOT.glob("cloud-init*.yaml")))
        die(f"No existe la plantilla '{nombre}'.\n  Disponibles: {disponibles}")
    out: list[str] = []
    for line in template.read_text(encoding="utf-8").splitlines():
        # Coincidencia exacta: así una mención del marcador en un comentario
        # cualquiera del fichero no se sustituye por error.
        if line.strip() == "# {{SSH_AUTHORIZED_KEYS}}":
            indent = line[: len(line) - len(line.lstrip())]
            out.extend(f"{indent}- {k['public_key'].strip()}" for k in keys)
        else:
            out.append(line)
    rendered = "\n".join(out) + "\n"
    check_user_data_encoding(rendered)
    return rendered


def wait_for_action(action_id: int, timeout: int = 420) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        action = api("GET", f"/v2/actions/{action_id}")["action"]
        if action["status"] == "completed":
            return
        if action["status"] == "errored":
            die(
                f"La acción {action_id} falló. El droplet puede existir a medias: "
                "revísalo con `list` y destrúyelo para no seguir pagándolo."
            )
        time.sleep(8)
    die(f"La acción {action_id} no completó en {timeout}s.")


def public_ip(droplet: dict) -> str:
    return next(
        (n["ip_address"] for n in droplet["networks"]["v4"] if n["type"] == "public"), ""
    )


def ssh_ports() -> list[int]:
    return [int(p) for p in cfg("DO_SSH_PORTS").split(",") if p.strip()]


def ssh_banner_ok(ip: str, port: int, timeout: int = 6) -> bool:
    """¿Hay un sshd de verdad al otro lado?

    No basta con que el TCP conecte: en redes con proxy transparente el
    appliance acepta la conexión al 443 de *cualquier* destino, incluso de IPs
    inexistentes, y luego corta lo que no sea TLS. El único indicio fiable es
    que llegue el banner "SSH-2.0-…" del protocolo.
    """
    try:
        with socket.create_connection((ip, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            return sock.recv(16).startswith(b"SSH-")
    except OSError:
        return False


def wait_for_ssh(ip: str, timeout: int = 300) -> int | None:
    """Devuelve el primer puerto con un sshd que responde, o None.

    `status: active` no implica que sshd escuche todavía. Y si tu red filtra el
    22 saliente, el droplet puede estar perfecto y aun así no alcanzarse por ahí,
    de modo que se prueba también el 443.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        for port in ssh_ports():
            if ssh_banner_ok(ip, port):
                return port
        time.sleep(5)
    return None


def find_droplets(name: str = "", tag: str = "") -> list[dict]:
    path = f"/v2/droplets?tag_name={tag}" if tag else "/v2/droplets"
    droplets = paged(path, "droplets")
    return [d for d in droplets if not name or d["name"] == name]


def cmd_launch(args: argparse.Namespace) -> None:
    name = args.name or cfg("DO_DROPLET_NAME")
    if find_droplets(name=name):
        die(f"Ya existe un droplet llamado '{name}'. Usa otro nombre o destrúyelo primero.")

    keys = selected_keys()
    body = {
        "name": name,
        "region": args.region or cfg("DO_REGION"),
        "size": args.size or cfg("DO_SIZE"),
        "image": args.image or cfg("DO_IMAGE"),
        "ssh_keys": [k["id"] for k in keys],
        # El tag decide qué se barre con `destroy --tag`. Una máquina de control
        # no puede llevar el de los efímeros: se la llevaría por delante.
        "tags": [args.tag or cfg("DO_TAG")],
        "monitoring": True,
        "ipv6": True,
    }
    user_data = build_user_data(keys, args.cloud_init or "")
    if user_data:
        body["user_data"] = user_data

    log(
        f"Creando '{name}': {body['size']} · {body['image']} · {body['region']}"
        f" · tag {body['tags'][0]} · {args.cloud_init or cfg('DO_CLOUD_INIT')}"
    )
    log(f"Claves SSH autorizadas: {', '.join(k['name'] for k in keys)}")
    if args.dry_run:
        log("\n--dry-run, no se envía nada. Cuerpo de la petición:\n")
        log(json.dumps(body, indent=2, ensure_ascii=False))
        return

    created = api("POST", "/v2/droplets", body)
    droplet_id = created["droplet"]["id"]
    action_id = created["links"]["actions"][0]["id"]
    log(f"Aceptado (202). Droplet id {droplet_id}. Esperando a que aprovisione…")

    wait_for_action(action_id)
    droplet = api("GET", f"/v2/droplets/{droplet_id}")["droplet"]
    ip = public_ip(droplet)
    if not ip:
        die("El droplet está activo pero no tiene IP pública asignada.")
    log(f"Activo. IP pública: {ip}")

    log(f"Esperando a que SSH acepte conexiones (puertos {cfg('DO_SSH_PORTS')})…")
    port = wait_for_ssh(ip)
    if port:
        log(f"SSH listo en el puerto {port}.")
    else:
        log(
            "Aviso: SSH no respondió por ninguno de los puertos. El droplet existe.\n"
            "  Si tu red bloquea el 22 y el 443 saliente, entra por la consola web:\n"
            f"  https://cloud.digitalocean.com/droplets/{droplet_id}/console"
        )

    if port and not args.no_provision:
        log("")
        cmd_provision(
            argparse.Namespace(
                name=name,
                port=port,
                repo=args.repo,
                service=args.service,
                push_do_token=args.push_do_token,
                push_env=args.push_env,
                skip_wait=False,
            )
        )

    key_file = Path(cfg("DO_SSH_KEY_FILE")).expanduser()
    port_flag = f"-p {port} " if port and port != 22 else ""
    log("\n" + "=" * 62)
    log(f"  {name}  ·  {ip}")
    log(f"  ssh {port_flag}-i {key_file} {cfg('DO_SSH_USER')}@{ip}")
    log(f"  o simplemente:  python scripts/do_droplet.py ssh {name}")
    if not args.no_provision:
        dev_user = cfg("DO_DEV_USER")
        if cfg("DO_SSH_USER") == dev_user:
            log("\n  Dentro:  cd ~/src/<repo> && claude")
        else:
            # Las credenciales están en el home del usuario de desarrollo, no
            # en el de root: hay que cambiar de usuario para encontrarlas.
            log(f"\n  Dentro:  su - {dev_user}   →   cd ~/src/<repo> && claude")
            log(f"  (o pon DO_SSH_USER={dev_user} en .env y entrarás ahí directamente)")
        for svc in selected_services(args.service):
            log(f"\n  Servicio '{svc['name']}' corriendo. Estado y logs:")
            log(f"  python scripts/do_droplet.py service logs {svc['name']}")
    log(f"\n  Al terminar:    python scripts/do_droplet.py destroy {name}")
    log("  (el droplet factura por segundo mientras exista)")
    log("=" * 62)


def cmd_list(args: argparse.Namespace) -> None:
    droplets = find_droplets(tag=args.tag or "")
    if not droplets:
        log("No hay droplets.")
        return
    log(f"{'ID':<12} {'NOMBRE':<24} {'ESTADO':<8} {'TAMAÑO':<16} IP")
    for d in droplets:
        log(
            f"{d['id']:<12} {d['name']:<24} {d['status']:<8} "
            f"{d['size_slug']:<16} {public_ip(d) or '-'}"
        )


def cmd_ip(args: argparse.Namespace) -> None:
    droplets = find_droplets(name=args.name or cfg("DO_DROPLET_NAME"))
    if not droplets:
        die("No encontré ese droplet.")
    log(public_ip(droplets[0]))


def cmd_ssh(args: argparse.Namespace) -> None:
    _, ip, port = resolve_target(args.name or "", args.port or 0)
    cmd = ssh_command(ip, port)
    if args.cmd:
        cmd.append(args.cmd)
    log(f"$ {' '.join(cmd)}")
    raise SystemExit(subprocess.call(cmd))


def cmd_service(args: argparse.Namespace) -> None:
    """systemctl/journalctl del servicio, sin tener que recordar la sintaxis."""
    unit = load_service(args.service)["name"]
    remoto = {
        "status": f"systemctl status {unit} --no-pager --lines=20",
        "logs": f"journalctl -u {unit} -n {args.lines} --no-pager",
        "follow": f"journalctl -u {unit} -f",
        "restart": f"systemctl restart {unit} && systemctl is-active {unit}",
        "stop": f"systemctl stop {unit}",
        "start": f"systemctl start {unit} && systemctl is-active {unit}",
    }[args.action]

    _, ip, port = resolve_target(args.name or "", args.port or 0)
    raise SystemExit(
        subprocess.call(ssh_command(ip, port, user="root") + [remoto])
    )


def cmd_destroy(args: argparse.Namespace) -> None:
    if args.tag:
        droplets = find_droplets(tag=args.tag)
    else:
        droplets = find_droplets(name=args.name or cfg("DO_DROPLET_NAME"))
    if not droplets:
        log("No hay nada que destruir.")
        return

    log("Se van a DESTRUIR estos droplets (irreversible):")
    for d in droplets:
        log(f"  - {d['name']} (id {d['id']}, {public_ip(d) or 'sin IP'})")
    if not args.yes and not confirmar("\nEscribe 'si' para confirmar: "):
        log("Cancelado.")
        return

    for d in droplets:
        api("DELETE", f"/v2/droplets/{d['id']}")
        log(f"Destruido {d['name']}.")

    wait_until_gone([d["id"] for d in droplets])


def wait_until_gone(droplet_ids: list[int], timeout: int = 120) -> None:
    """Espera a que los droplets dejen de aparecer en la cuenta.

    El DELETE contesta 204 enseguida pero el borrado es asíncrono: durante unos
    segundos siguen saliendo en `GET /v2/droplets`. Sin esta espera, destruir y
    volver a crear con el mismo nombre falla con un 'ya existe' que es mentira.
    """
    pending = set(droplet_ids)
    deadline = time.time() + timeout
    while time.time() < deadline:
        alive = {d["id"] for d in find_droplets()} & pending
        if not alive:
            return
        time.sleep(4)
    log("Aviso: la cuenta todavía lista algún droplet recién destruido.")


# ------------------------------------------------------------- aprovisionamiento


def shq(value: str) -> str:
    """Entrecomilla para sh. Imprescindible: aquí viajan tokens."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


def ssh_command(ip: str, port: int, user: str = "") -> list[str]:
    key_file = str(Path(cfg("DO_SSH_KEY_FILE")).expanduser())
    return [
        "ssh",
        "-p", str(port),
        "-i", key_file,
        "-o", "StrictHostKeyChecking=accept-new",
        # Los keepalives no son un lujo: cloud-init reinicia ssh.socket en pleno
        # arranque y deja medio abierta cualquier conexión de ese momento. Sin
        # esto, ssh se queda esperando para siempre a un servidor que ya no
        # está, y con él el proceso que lo llamó. Nos colgó un launch 20 minutos.
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=4",
        "-o", "ConnectTimeout=15",
        f"{user or cfg('DO_SSH_USER')}@{ip}",
    ]


def resolve_target(name: str, port_override: int = 0) -> tuple[dict, str, int]:
    """Localiza el droplet y el puerto SSH por el que se le llega."""
    droplets = find_droplets(name=name or cfg("DO_DROPLET_NAME"))
    if not droplets:
        die("No encontré ese droplet. Míralos con: python scripts/do_droplet.py list")
    ip = public_ip(droplets[0])
    if not ip:
        die("El droplet no tiene IP pública.")
    port = port_override or wait_for_ssh(ip, timeout=20) or 0
    if not port:
        die(
            f"No se alcanza {ip} por ninguno de los puertos {cfg('DO_SSH_PORTS')}.\n"
            "Si tu red los filtra, usa la consola web: "
            "https://cloud.digitalocean.com/droplets"
        )
    return droplets[0], ip, port


def run_remote_script(ip: str, port: int, script: str) -> int:
    """Ejecuta un script en el droplet pasándolo por stdin.

    Por stdin y no como argumento a propósito: lo que va en la línea de comandos
    de ssh acaba en el `ps` del droplet, donde cualquier usuario lo vería, y este
    script lleva tokens dentro.

    Siempre como root, aunque DO_SSH_USER sea otro: hay que crear ficheros en el
    home de otro usuario y hacer chown. DigitalOcean instala las claves de la
    cuenta también para root, así que la conexión existe igualmente.
    """
    proc = subprocess.run(
        ssh_command(ip, port, user="root") + ["bash -s"],
        input=script.encode("utf-8"),
    )
    return proc.returncode


def wait_for_dev_tools(ip: str, port: int, timeout: int = 900) -> None:
    """Espera a que cloud-init termine de instalar Node, Claude Code y gh.

    SSH responde bastante antes de que cloud-init acabe, así que inyectar los
    secretos nada más conectar pillaría la máquina a medio hacer.
    """
    deadline = time.time() + timeout
    warned = False
    while time.time() < deadline:
        try:
            probe = subprocess.run(
                ssh_command(ip, port, user="root")
                + [
                    "if [ -e /var/lib/cloud/DEV_READY ]; then echo READY; "
                    "elif [ -e /var/lib/cloud/DEV_FAILED ]; then echo FAILED; "
                    "else echo WAIT; fi"
                ],
                capture_output=True,
                text=True,
                # Cinturón además de los keepalives: si una sonda se atasca, se
                # corta y se vuelve a intentar. Antes bloqueaba el bucle entero
                # y el deadline de aquí abajo no se comprobaba nunca.
                timeout=90,
            )
        except subprocess.TimeoutExpired:
            log("  (la comprobación se atascó, reintentando)")
            continue
        state = probe.stdout.strip()
        if state == "READY":
            return
        if state == "FAILED":
            die(
                "La instalación de herramientas falló en el droplet. Mira el log:\n"
                "  python scripts/do_droplet.py ssh --cmd "
                "'tail -40 /var/log/dev-tools-install.log'"
            )
        if not warned:
            # La espera larga no son las herramientas (30 s medidos), sino el
            # package_upgrade de Ubuntu que corre antes: 154 s en la medición.
            log("  esperando a que cloud-init termine de instalar (unos 4 min)…")
            warned = True
        time.sleep(10)
    die(
        "Se agotó la espera a que el droplet terminase de instalar las herramientas.\n"
        "  python scripts/do_droplet.py ssh --cmd 'tail -40 /var/log/dev-tools-install.log'"
    )


# -------------------------------------------------------------------- servicios


SERVICES_DIR = ROOT / "services"


def load_service(name: str) -> dict:
    """Lee services/<nombre>.json, el descriptor de un proceso de larga vida.

    El lanzador no sabe nada de ningún proyecto en concreto: sabe clonar un repo,
    instalarlo y dejarlo corriendo como unidad de systemd. Lo que cambia de un
    servicio a otro vive en el descriptor, no aquí.
    """
    path = SERVICES_DIR / f"{name}.json"
    if not path.exists():
        disponibles = ", ".join(sorted(p.stem for p in SERVICES_DIR.glob("*.json")))
        die(
            f"No existe el servicio '{name}' (falta {path}).\n"
            f"  Definidos: {disponibles or 'ninguno'}"
        )
    try:
        svc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"{path} no es JSON válido: {exc}")
    for field in ("repo", "start"):
        if not svc.get(field):
            die(f"{path}: falta el campo obligatorio '{field}'.")
    svc["name"] = name
    # El repo se clona en ~/src/<nombre del repo>, igual que los de DO_REPOS.
    svc.setdefault("dir", svc["repo"].rstrip("/").split("/")[-1].removesuffix(".git"))
    svc.setdefault("install", "")
    svc.setdefault("env_prefix", "")
    svc.setdefault("env_file", ".env")
    # Ficheros que el servicio necesita y que no están en su repo. Sin esto hay
    # configuración que sólo vive dentro del droplet y se pierde al destruirlo,
    # que es justo lo contrario de poder tirar y rehacer una máquina.
    svc.setdefault("files", {})
    if not isinstance(svc["files"], dict):
        die(f"{path}: 'files' tiene que ser un objeto de ruta -> contenido.")
    return svc


def selected_services(extra: list[str]) -> list[dict]:
    names: list[str] = []
    for raw in (extra or cfg("DO_SERVICES").split(",")):
        name = raw.strip()
        if name and name not in names:
            names.append(name)
    servicios = [load_service(n) for n in names]
    # Dos servicios del mismo repo comparten directorio, y con él el .env y los
    # datos: el segundo pisaría al primero. Pasa con telegram-coordinator y
    # telegram-launcher, que son el mismo programa con distinto bot y por eso
    # van en máquinas distintas, nunca juntos.
    por_dir: dict[str, str] = {}
    for svc in servicios:
        otro = por_dir.get(svc["dir"])
        if otro:
            die(
                f"Los servicios '{otro}' y '{svc['name']}' usan el mismo directorio "
                f"(~/src/{svc['dir']}).\n"
                "  Comparten .env y datos, así que no pueden convivir en un droplet.\n"
                "  Pon cada uno en una máquina."
            )
        por_dir[svc["dir"]] = svc["name"]
    return servicios


def service_env_lines(svc: dict) -> list[str]:
    """Variables del .env del lanzador que se copian al .env del servicio.

    `TG_BOT_TOKEN=xxx` aquí se convierte en `BOT_TOKEN=xxx` allí. Hace falta un
    puente así porque estos valores son secretos: no pueden estar en el repo del
    servicio, y menos aún en cloud-init, cuyo user_data lee cualquier usuario del
    droplet sin sudo.
    """
    prefix = svc["env_prefix"]
    if not prefix:
        return []
    out = []
    for key, value in sorted(os.environ.items()):
        if key.startswith(prefix) and len(key) > len(prefix) and value.strip():
            out.append(f"{key[len(prefix):]}={value}")
    return out


def build_service_section(svc: dict, dev_user: str) -> list[str]:
    """Trozo de script que instala un servicio y lo deja corriendo.

    Nada de aquí es fatal: si un servicio falla, el droplet ya tiene credenciales
    y repos, y se depura entrando. Abortar el aprovisionamiento entero sería peor.
    """
    unit = svc["name"]
    env_file = svc["env_file"]
    parts = [
        "",
        f"# --- servicio {unit}",
        f'DIR="$H/src/{svc["dir"]}"',
        'if [ ! -d "$DIR" ]; then',
        f'  echo "  AVISO: {unit}: no existe $DIR, ¿falló el clonado? Me lo salto."',
        "else",
    ]

    env_lines = service_env_lines(svc)
    if env_lines:
        parts += [
            f'  cat > "$DIR/{env_file}" <<\'FIN_ENV\'',
            "\n".join(env_lines),
            "FIN_ENV",
            f'  chmod 600 "$DIR/{env_file}"',
            f'  chown "$DEV_USER:$DEV_USER" "$DIR/{env_file}"',
        ]
    elif svc["env_prefix"]:
        parts.append(
            f'  echo "  AVISO: {unit}: no hay ninguna variable {svc["env_prefix"]}* '
            f'en el .env, arrancará sin configuración."'
        )

    for ruta, contenido in svc["files"].items():
        if ruta.startswith("/") or ".." in ruta:
            die(f"{unit}: la ruta '{ruta}' de 'files' tiene que ser relativa al repo.")
        texto = (
            contenido
            if isinstance(contenido, str)
            else json.dumps(contenido, indent=2, ensure_ascii=False) + "\n"
        )
        parts += [
            f'  install -d -o "$DEV_USER" -g "$DEV_USER" "$(dirname "$DIR/{ruta}")"',
            f'  cat > "$DIR/{ruta}" <<\'FIN_FICHERO\'',
            texto.rstrip("\n"),
            "FIN_FICHERO",
            f'  chown "$DEV_USER:$DEV_USER" "$DIR/{ruta}"',
            f'  echo "  {unit}: escrito {ruta}"',
        ]

    if svc["install"]:
        parts += [
            f'  echo "  {unit}: instalando ({svc["install"]})…"',
            f'  (cd "$DIR" && sudo -u "$DEV_USER" -H bash -lc {shq(svc["install"])}) '
            f'|| echo "  AVISO: {unit}: falló la instalación."',
        ]

    # bash -l en el ExecStart no es adorno: el proceso necesita los tokens de
    # ~/.config/dev-secrets.env, y systemd no puede leer ese fichero con
    # EnvironmentFile porque sus líneas llevan `export`, que no admite. Con el
    # shell de login se sourcea .profile -> .bashrc, donde provision puso la
    # línea que lo carga.
    unit_file = "\n".join(
        [
            "[Unit]",
            f"Description={unit} (instalado por do_droplet.py provision)",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"User={dev_user}",
            "WorkingDirectory=@DIR@",
            f"ExecStart=/bin/bash -lc {shq('exec ' + svc['start'])}",
            "Restart=always",
            "RestartSec=5",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
        ]
    )
    parts += [
        f"  cat > /etc/systemd/system/{unit}.service <<'FIN_UNIT'",
        unit_file,
        "FIN_UNIT",
        # El home real no se conoce hasta ejecutar el script, así que la ruta se
        # sustituye aquí en vez de expandirla en el heredoc (que expandiría
        # también lo que traiga el comando de arranque).
        f'  sed -i "s|@DIR@|$DIR|" /etc/systemd/system/{unit}.service',
        "  systemctl daemon-reload",
        f"  systemctl enable {unit}.service >/dev/null 2>&1 || true",
        # restart y no start: al reaprovisionar hay que recoger el código nuevo.
        f"  systemctl restart {unit}.service || true",
        "  sleep 3",
        f"  if systemctl is-active --quiet {unit}.service; then",
        f'    echo "  {unit}: activo"',
        "  else",
        f'    echo "  AVISO: {unit} no arrancó. Últimas líneas del log:"',
        f"    journalctl -u {unit}.service -n 15 --no-pager || true",
        "  fi",
        "fi",
    ]
    return parts


def push_env_names(valores: list[str]) -> list[str]:
    """Nombres de variables a copiar, aceptando repetición y comas."""
    nombres: list[str] = []
    for bruto in valores:
        for nombre in bruto.split(","):
            nombre = nombre.strip()
            if nombre and nombre not in nombres:
                nombres.append(nombre)
    return nombres


def build_provision_script(
    repos: list[str],
    services: list[dict] | None = None,
    push_do_token: bool = False,
    push_env: list[str] | None = None,
) -> str:
    """Script que deja el droplet listo para trabajar.

    Todo lo secreto se escribe con umask 077 y acaba en modo 600 del usuario de
    desarrollo. Nada de esto puede ir en cloud-init: el user_data lo sirve la API
    de metadatos y lo lee cualquier proceso del droplet sin privilegios.
    """
    dev_user = cfg("DO_DEV_USER")
    claude_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()

    exports = ["# Generado por do_droplet.py provision. Modo 600, no lo copies."]
    if claude_token:
        exports.append(f"export CLAUDE_CODE_OAUTH_TOKEN={shq(claude_token)}")
    if github_token:
        # GH_TOKEN lo lee gh; GITHUB_TOKEN lo esperan casi todas las herramientas.
        exports.append(f"export GITHUB_TOKEN={shq(github_token)}")
        exports.append(f"export GH_TOKEN={shq(github_token)}")
    if push_do_token:
        # Sólo para la máquina de control, y sólo pidiéndolo a mano: con este
        # token el droplet puede crear y destruir máquinas en la cuenta, o sea
        # gastar dinero. Va aquí y no sólo en el .env del bot para que también
        # lo tengan las sesiones de la máquina; si no, `register-key` desde
        # dentro falla con un "falta el token" que despista.
        exports.append(f"export DO_TOKEN={shq(token())}")

    # Config del lanzador que se lleva la máquina de control, para que los
    # droplets que cree ella salgan iguales que los que creas tú: mismos repos,
    # misma autoría de git, mismos servicios. Sin esto lanza máquinas peores y
    # no se nota hasta que entras en una.
    for nombre in push_env_names(push_env or []):
        valor = os.environ.get(nombre, "").strip()
        if not valor:
            log(f"  AVISO: --push-env {nombre} no tiene valor aquí, no se envía.")
            continue
        exports.append(f"export {nombre}={shq(valor)}")

    parts = [
        "set -eu",
        "umask 077",
        f"DEV_USER={shq(dev_user)}",
        'H=$(getent passwd "$DEV_USER" | cut -d: -f6)',
        '[ -n "$H" ] || { echo "no existe el usuario $DEV_USER" >&2; exit 1; }',
        'install -d -m 700 -o "$DEV_USER" -g "$DEV_USER" "$H/.config"',
        "",
        "# --- variables de entorno con los tokens",
        'cat > "$H/.config/dev-secrets.env" <<\'FIN_SECRETOS\'',
        "\n".join(exports),
        "FIN_SECRETOS",
        'chmod 600 "$H/.config/dev-secrets.env"',
        'chown "$DEV_USER:$DEV_USER" "$H/.config/dev-secrets.env"',
        "",
        "# --- cargarlas en cada shell",
        "# La línea va al PRINCIPIO de .bashrc, antes del corte que Ubuntu pone",
        "# para shells no interactivas. Así el token existe en los tres casos:",
        "# sesión interactiva, shell de login y `ssh droplet 'claude -p ...'`.",
        'if ! grep -q dev-secrets.env "$H/.bashrc" 2>/dev/null; then',
        "  TMP=$(mktemp)",
        "  {",
        "    echo '# Secretos de desarrollo (los inyecta do_droplet.py provision).'",
        '    echo \'[ -f "$HOME/.config/dev-secrets.env" ] && . "$HOME/.config/dev-secrets.env"\'',
        "    echo",
        '    cat "$H/.bashrc" 2>/dev/null || true',
        '  } > "$TMP"',
        '  cat "$TMP" > "$H/.bashrc"',
        '  rm -f "$TMP"',
        '  chown "$DEV_USER:$DEV_USER" "$H/.bashrc"',
        "fi",
        "",
        "# --- git",
        'sudo -u "$DEV_USER" -H git config --global credential.helper store',
        'sudo -u "$DEV_USER" -H git config --global init.defaultBranch main',
    ]

    if cfg("GIT_USER_NAME"):
        parts.append(
            f'sudo -u "$DEV_USER" -H git config --global user.name {shq(cfg("GIT_USER_NAME"))}'
        )
    if cfg("GIT_USER_EMAIL"):
        parts.append(
            f'sudo -u "$DEV_USER" -H git config --global user.email {shq(cfg("GIT_USER_EMAIL"))}'
        )

    if github_token:
        parts += [
            "",
            "# --- credenciales de GitHub para git y para gh",
            f'printf "https://x-access-token:%s@github.com\\n" {shq(github_token)} '
            '> "$H/.git-credentials"',
            'chmod 600 "$H/.git-credentials"',
            'chown "$DEV_USER:$DEV_USER" "$H/.git-credentials"',
            # Que un token caducado no tumbe el resto del aprovisionamiento:
            # las credenciales de git ya están puestas y `gh` es un extra.
            # Y si no hay gh (la máquina de control no lo lleva), no se avisa de
            # un rechazo que no ha ocurrido.
            'if ! command -v gh >/dev/null; then',
            '  echo "  gh: no instalado en esta máquina, git sí tiene el token"',
            f'elif printf "%s\\n" {shq(github_token)} | '
            'sudo -u "$DEV_USER" -H gh auth login --with-token 2>/dev/null; then',
            '  echo "  gh: $(sudo -u "$DEV_USER" -H gh api user --jq .login '
            '2>/dev/null || echo "?")"',
            "else",
            '  echo "  AVISO: GitHub rechazó el token (¿caducado o sin permisos?)."',
            '  echo "         Revísalo en https://github.com/settings/personal-access-tokens"',
            "fi",
        ]

    if repos:
        parts += [
            "",
            "# --- repos",
            'install -d -m 755 -o "$DEV_USER" -g "$DEV_USER" "$H/src"',
        ]
        for repo in repos:
            slug = repo.strip().rstrip("/")
            if not slug:
                continue
            name = slug.split("/")[-1].removesuffix(".git")
            parts += [
                f'DEST="$H/src/{name}"',
                'if [ -d "$DEST/.git" ]; then',
                f'  echo "  {slug} ya estaba clonado"',
                "else",
                f'  echo "  clonando {slug}…"',
                f'  sudo -u "$DEV_USER" -H git clone -q '
                f'https://github.com/{slug}.git "$DEST" '
                f'|| echo "  AVISO: no pude clonar {slug}"',
                "fi",
            ]

    for svc in services or []:
        parts += build_service_section(svc, dev_user)

    parts += [
        "",
        "# --- comprobación final",
        # La máquina de control no lleva Claude Code (no cabe en 512 MB), así
        # que preguntarle por su versión sólo produciría un error confuso.
        "if command -v claude >/dev/null; then",
        '  echo "  claude: $(claude --version 2>&1 | head -1)"',
        '  echo "  auth:   $(sudo -u "$DEV_USER" -H bash -lc '
        "'claude auth status' 2>&1 | tr -d '\\n ' )\"",
        "else",
        '  echo "  claude: no instalado en esta máquina"',
        "fi",
    ]
    return "\n".join(parts) + "\n"


def cmd_provision(args: argparse.Namespace) -> None:
    name = args.name or cfg("DO_DROPLET_NAME")

    # La configuración se valida antes de ir a buscar el droplet: un servicio mal
    # escrito debe fallar al instante y no tras esperar a que arranque la máquina.
    repos = args.repo or [r for r in cfg("DO_REPOS").split(",") if r.strip()]
    services = selected_services(getattr(args, "service", []) or [])
    # El repo de un servicio se clona aunque no esté en DO_REPOS: sin código no
    # hay nada que instalar, y obligarte a listarlo dos veces sólo genera fallos.
    for svc in services:
        if svc["repo"] not in repos:
            repos.append(svc["repo"])

    _, ip, port = resolve_target(name, args.port or 0)
    log(f"Aprovisionando '{name}' ({ip}:{port})…")

    if not args.skip_wait:
        wait_for_dev_tools(ip, port)

    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
        log(
            "  AVISO: no hay CLAUDE_CODE_OAUTH_TOKEN en el entorno ni en .env.\n"
            "  Genéralo UNA vez en tu máquina con:  claude setup-token\n"
            "  y pégalo en .env. Sin él, Claude Code pedirá login en el droplet."
        )
    if not os.environ.get("GITHUB_TOKEN", "").strip():
        log(
            "  AVISO: no hay GITHUB_TOKEN. No podrás clonar repos privados.\n"
            "  Créalo en https://github.com/settings/personal-access-tokens"
        )

    if getattr(args, "push_do_token", False):
        log("  AVISO: se envía también el DO_TOKEN. Quien tenga acceso a esta")
        log("         máquina podrá crear y destruir droplets en tu cuenta.")

    code = run_remote_script(
        ip,
        port,
        build_provision_script(
            repos,
            services,
            getattr(args, "push_do_token", False),
            getattr(args, "push_env", []),
        ),
    )
    if code != 0:
        die(f"El aprovisionamiento falló (código {code}).")
    log("Aprovisionamiento terminado.")


# ----------------------------------------------------- actualizar desde dentro


# Marca que build_service_section deja en la Description de cada unidad. Con
# ella se reconocen luego los servicios que instaló este script, sin tener que
# guardar una lista aparte que se desincronizaría.
PROVISION_MARK = "instalado por do_droplet.py provision"


def dentro_del_droplet() -> Path:
    """Comprueba que corremos en el droplet y devuelve el ~/src a actualizar.

    `update` es el único comando que actúa sobre la máquina donde se ejecuta en
    vez de sobre la API: se lanza dentro del droplet, por SSH o desde el bot.
    Ejecutarlo por error en la laptop haría `git pull` en repos que estás
    tocando a mano y reiniciaría servicios; de ahí la comprobación.
    """
    if sys.platform != "linux" or not Path("/var/lib/cloud").exists():
        die(
            "`update` se ejecuta DENTRO del droplet, no en la máquina lanzadora.\n"
            "  Desde aquí:  python scripts/do_droplet.py ssh mini --cmd \\\n"
            "    'cd ~/src/digital-ocean-dropplet-auto-launching && "
            "python3 scripts/do_droplet.py update'"
        )

    candidatos = [Path.home() / "src"]
    if cfg("DO_DEV_USER"):
        # Entrando como root el home es /root y ahí no hay nada: los repos son
        # del usuario de desarrollo.
        try:
            import pwd

            candidatos.append(Path(pwd.getpwnam(cfg("DO_DEV_USER")).pw_dir) / "src")
        except (ImportError, KeyError):
            pass
    for base in candidatos:
        if base.is_dir():
            return base
    die(f"No existe {candidatos[0]}: aquí no hay repos que actualizar.")


def owner_of(path: Path) -> str:
    try:
        import pwd

        return pwd.getpwuid(path.stat().st_uid).pw_name
    except (ImportError, KeyError, OSError):
        return ""


def run_local(
    cmd: list[str], cwd: Path | None = None, timeout: int = 180, owner: str = ""
) -> tuple[int, str]:
    """Ejecuta un comando de la máquina y devuelve (código, salida completa).

    Con `owner` y siendo root se ejecuta como ese usuario: los repos son del
    usuario de desarrollo y git se niega a trabajar en el repo de otro
    ("detected dubious ownership"). El `timeout` no es opcional por costumbre de
    esta casa: un `git` o un `npm` colgado dejaría al bot esperando en silencio.
    """
    if owner and owner != "root" and os.geteuid() == 0:
        cmd = ["sudo", "-u", owner, "-H", *cmd]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, f"se agotó el tiempo ({timeout}s) en: {' '.join(cmd)}"
    except OSError as exc:
        return 127, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def pull_repo(repo: Path) -> dict:
    """`git pull --ff-only` en un repo, contando qué cambió.

    --ff-only y no merge: si el repo del droplet tiene commits propios, lo que
    hace falta es enterarse, no fabricar un merge a ciegas desde un bot.
    """
    owner = owner_of(repo)

    def git(*args: str, timeout: int = 180) -> tuple[int, str]:
        return run_local(["git", *args], cwd=repo, timeout=timeout, owner=owner)

    code, antes = git("rev-parse", "HEAD")
    if code != 0:
        return {"changed": False, "msg": f"{repo.name}: no pude leer HEAD ({antes})"}

    code, salida = git("pull", "--ff-only", timeout=300)
    if code != 0:
        detalle = (salida.splitlines() or ["sin salida"])[-1]
        return {"changed": False, "msg": f"{repo.name}: FALLO el pull -> {detalle}"}

    _, despues = git("rev-parse", "HEAD")
    if antes == despues:
        return {"changed": False, "msg": f"{repo.name}: ya estaba al día ({antes[:7]})"}

    _, cuantos = git("rev-list", "--count", f"{antes}..{despues}")
    _, ficheros = git("diff", "--name-only", antes, despues)
    return {
        "changed": True,
        "files": ficheros.split(),
        "msg": f"{repo.name}: {antes[:7]} -> {despues[:7]} "
        f"({cuantos.strip() or '?'} commits)",
    }


def reinstalar_dependencias(repo: Path, ficheros: list[str]) -> str:
    """Reinstala los paquetes de Node si el pull tocó el manifiesto.

    Sólo cuando cambió package.json o el lock: un `npm ci` innecesario tarda
    minutos en una máquina de 512 MB y deja el servicio parado mientras tanto.
    """
    if not (repo / "package.json").exists():
        return ""
    if not any(Path(f).name in ("package.json", "package-lock.json") for f in ficheros):
        return ""

    cmd = ["npm", "ci"] if (repo / "package-lock.json").exists() else ["npm", "install"]
    code, salida = run_local(cmd, cwd=repo, timeout=900, owner=owner_of(repo))
    if code != 0:
        detalle = (salida.splitlines() or ["sin salida"])[-1]
        return f"AVISO: `{' '.join(cmd)}` falló -> {detalle}"
    return f"dependencias reinstaladas ({' '.join(cmd)})"


def unidades_de_provision() -> list[tuple[str, Path | None]]:
    """Servicios instalados por `provision`, con el repo del que viven."""
    fuera = []
    for fichero in sorted(Path("/etc/systemd/system").glob("*.service")):
        try:
            texto = fichero.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if PROVISION_MARK not in texto:
            continue
        directorio = None
        for linea in texto.splitlines():
            if linea.startswith("WorkingDirectory="):
                directorio = Path(linea.split("=", 1)[1].strip())
        fuera.append((fichero.stem, directorio))
    return fuera


def unidad_propia() -> str:
    """Unidad de systemd dentro de la que corre este proceso, si es que hay una.

    Cuando el update lo pide el bot, el bot es quien lo está ejecutando: al
    reiniciar su unidad, systemd mata el cgroup entero y con él este proceso y
    la respuesta que aún no ha salido hacia Telegram. Hay que saberlo para
    tratar ese caso aparte.
    """
    try:
        cgroup = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return ""
    for trozo in cgroup.replace("/", " ").split():
        if trozo.endswith(".service"):
            return trozo[: -len(".service")]
    return ""


def reiniciar_unidad(unit: str, propia: bool) -> str:
    if propia:
        # systemd-run crea una unidad transitoria, fuera de nuestro cgroup: así
        # el reinicio sobrevive a que systemd nos mate a nosotros. Un `sleep &
        # systemctl restart` normal moriría con el propio servicio y el bot se
        # quedaría con el código viejo corriendo.
        code, salida = run_local(
            [
                "sudo",
                "systemd-run",
                "--on-active=3",
                "--collect",
                "--quiet",
                "/bin/systemctl",
                "restart",
                f"{unit}.service",
            ],
            timeout=60,
        )
        if code != 0:
            return f"{unit}: NO pude programar el reinicio -> {salida.strip()}"
        return f"{unit}: se reinicia en 3 s (es quien está ejecutando esto)"

    code, salida = run_local(
        ["sudo", "systemctl", "restart", f"{unit}.service"], timeout=180
    )
    if code != 0:
        return f"{unit}: FALLO al reiniciar -> {salida.strip()}"
    time.sleep(3)
    code, estado = run_local(["systemctl", "is-active", f"{unit}.service"], timeout=30)
    if estado.strip() != "active":
        _, log_unit = run_local(
            ["journalctl", "-u", f"{unit}.service", "-n", "10", "--no-pager"],
            timeout=60,
        )
        return f"{unit}: NO arrancó ({estado.strip() or '?'})\n{log_unit}"
    return f"{unit}: reiniciado y activo"


def cmd_update(args: argparse.Namespace) -> None:
    """Trae el código nuevo de GitHub a esta máquina y reinicia lo que lo usa.

    Es lo que se dispara desde el bot con el ejecutor `actualizar`: sin esto,
    corregir algo en la laptop no cambiaba nada en el droplet hasta entrar por
    SSH a hacer el pull a mano, y el servicio seguía con el código viejo cargado
    sin que nada lo delatase.
    """
    base = dentro_del_droplet()
    repos = sorted(p for p in base.iterdir() if (p / ".git").is_dir())
    if not repos:
        die(f"No hay ningún repo en {base}.")

    log(f"Actualizando {socket.gethostname()} ({base}):")
    cambiados: set[Path] = set()
    for repo in repos:
        info = pull_repo(repo)
        log(f"  {info['msg']}")
        if info["changed"]:
            cambiados.add(repo.resolve())
            aviso = reinstalar_dependencias(repo, info.get("files", []))
            if aviso:
                log(f"    {aviso}")

    unidades = unidades_de_provision()
    if not unidades:
        log("Servicios: ninguno instalado por provision en esta máquina.")
        return

    propia = unidad_propia()
    pendientes = [
        unit
        for unit, directorio in unidades
        if args.restart_all or (directorio and directorio.resolve() in cambiados)
    ]
    if not pendientes:
        log("Servicios: sin cambios, no hace falta reiniciar nada.")
        return

    log("Servicios:")
    # El propio el último: en cuanto se programe su reinicio, a este proceso le
    # quedan segundos de vida.
    for unit in sorted(pendientes, key=lambda u: u == propia):
        log(f"  {reiniciar_unidad(unit, propia=unit == propia)}")


# ------------------------------------------------------------------------ parser


def main() -> None:
    force_utf8_output()
    load_env()
    parser = argparse.ArgumentParser(
        prog="do_droplet.py", description="Droplets efímeros en DigitalOcean."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("keygen", help="genera un par de claves ed25519 local")
    p.add_argument("--file")
    p.add_argument("--comment", default="do-droplet")
    p.set_defaults(func=cmd_keygen)

    p = sub.add_parser("keys", help="lista las claves SSH de la cuenta")
    p.set_defaults(func=cmd_keys)

    p = sub.add_parser("register-key", help="sube una clave pública a la cuenta")
    p.add_argument("file", nargs="?")
    p.add_argument("--name")
    p.set_defaults(func=cmd_register_key)

    p = sub.add_parser("sizes", help="tamaños disponibles en una región")
    p.add_argument("--region")
    p.add_argument("--min-memory", type=int, default=4096)
    p.set_defaults(func=cmd_sizes)

    p = sub.add_parser("regions", help="regiones disponibles")
    p.set_defaults(func=cmd_regions)

    p = sub.add_parser("images", help="imágenes de distribución")
    p.add_argument("--filter", default="ubuntu")
    p.set_defaults(func=cmd_images)

    p = sub.add_parser("launch", help="crea el droplet y espera a que esté usable")
    p.add_argument("name", nargs="?")
    p.add_argument("--region")
    p.add_argument("--size")
    p.add_argument("--image")
    p.add_argument(
        "--cloud-init",
        help="plantilla de primer arranque (por defecto cloud-init.yaml; "
        "cloud-init.mini.yaml para la máquina de control)",
    )
    p.add_argument(
        "--tag",
        help="etiqueta del droplet (por defecto DO_TAG). Usa otra para las "
        "máquinas que no quieras barrer con destroy --tag",
    )
    p.add_argument("--dry-run", action="store_true", help="muestra la petición sin enviarla")
    p.add_argument(
        "--repo", action="append", default=[], help="owner/repo a clonar (repetible)"
    )
    p.add_argument(
        "--service",
        action="append",
        default=[],
        help="servicio de services/ que dejar corriendo (repetible)",
    )
    p.add_argument(
        "--push-env",
        action="append",
        default=[],
        metavar="VARS",
        help="variables de tu .env que copiar al droplet, separadas por coma. "
        "Para la máquina de control: sin ellas lanzaría droplets sin tus repos "
        "ni tus servicios",
    )
    p.add_argument(
        "--push-do-token",
        action="store_true",
        help="envía también el DO_TOKEN al droplet, para que pueda lanzar otros. "
        "Sólo para la máquina de control: quien entre ahí podrá gastar tu dinero",
    )
    p.add_argument(
        "--no-provision",
        action="store_true",
        help="no inyectar credenciales ni clonar repos",
    )
    p.set_defaults(func=cmd_launch)

    p = sub.add_parser(
        "provision", help="inyecta credenciales y clona repos en un droplet ya creado"
    )
    p.add_argument("name", nargs="?")
    p.add_argument("--port", type=int)
    p.add_argument(
        "--repo", action="append", default=[], help="owner/repo a clonar (repetible)"
    )
    p.add_argument(
        "--service",
        action="append",
        default=[],
        help="servicio de services/ que dejar corriendo (repetible)",
    )
    p.add_argument(
        "--push-env",
        action="append",
        default=[],
        metavar="VARS",
        help="variables de tu .env que copiar al droplet, separadas por coma. "
        "Para la máquina de control: sin ellas lanzaría droplets sin tus repos "
        "ni tus servicios",
    )
    p.add_argument(
        "--push-do-token",
        action="store_true",
        help="envía también el DO_TOKEN al droplet, para que pueda lanzar otros. "
        "Sólo para la máquina de control: quien entre ahí podrá gastar tu dinero",
    )
    p.add_argument(
        "--skip-wait",
        action="store_true",
        help="no esperar al testigo de instalación de cloud-init",
    )
    p.set_defaults(func=cmd_provision)

    p = sub.add_parser(
        "update",
        help="DENTRO del droplet: trae el código nuevo de GitHub y reinicia los "
        "servicios afectados",
    )
    p.add_argument(
        "--restart-all",
        action="store_true",
        help="reinicia los servicios aunque su repo no haya cambiado",
    )
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("service", help="estado, logs y reinicio de un servicio")
    p.add_argument(
        "action", choices=["status", "logs", "follow", "restart", "start", "stop"]
    )
    p.add_argument("service", help="nombre del descriptor en services/")
    p.add_argument("--name", help="droplet, si no es el de .env")
    p.add_argument("--port", type=int)
    p.add_argument("--lines", type=int, default=50, help="líneas de log (por defecto 50)")
    p.set_defaults(func=cmd_service)

    p = sub.add_parser("list", help="lista los droplets de la cuenta")
    p.add_argument("--tag")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("ip", help="imprime la IP pública")
    p.add_argument("name", nargs="?")
    p.set_defaults(func=cmd_ip)

    p = sub.add_parser("ssh", help="conecta por SSH")
    p.add_argument("name", nargs="?")
    p.add_argument("--port", type=int, help="fuerza un puerto en vez de autodetectarlo")
    p.add_argument("--cmd", help="ejecuta este comando en remoto en vez de abrir sesión")
    p.set_defaults(func=cmd_ssh)

    p = sub.add_parser("destroy", help="destruye el droplet")
    p.add_argument("name", nargs="?")
    p.add_argument("--tag", help="destruye todos los que lleven este tag")
    p.add_argument(
        "--yes",
        action="store_true",
        help="sin confirmación interactiva; obligatorio donde no hay terminal "
        "(Telegram, cron, ssh no interactivo)",
    )
    p.set_defaults(func=cmd_destroy)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
