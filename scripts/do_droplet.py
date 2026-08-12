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


def build_user_data(keys: list[dict]) -> str:
    """Inyecta las claves públicas en la plantilla de cloud-init.

    Sustituye la línea marcadora respetando su sangría, que en YAML es lo que
    determina si el fichero es válido.
    """
    template = ROOT / "cloud-init.yaml"
    if not template.exists():
        return ""
    out: list[str] = []
    for line in template.read_text(encoding="utf-8").splitlines():
        # Coincidencia exacta: así una mención del marcador en un comentario
        # cualquiera del fichero no se sustituye por error.
        if line.strip() == "# {{SSH_AUTHORIZED_KEYS}}":
            indent = line[: len(line) - len(line.lstrip())]
            out.extend(f"{indent}- {k['public_key'].strip()}" for k in keys)
        else:
            out.append(line)
    return "\n".join(out) + "\n"


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
        "tags": [cfg("DO_TAG")],
        "monitoring": True,
        "ipv6": True,
    }
    user_data = build_user_data(keys)
    if user_data:
        body["user_data"] = user_data

    log(f"Creando '{name}': {body['size']} · {body['image']} · {body['region']}")
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
                name=name, port=port, repo=args.repo, skip_wait=False
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
    if not args.yes and input("\nEscribe 'si' para confirmar: ").strip().lower() != "si":
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
        probe = subprocess.run(
            ssh_command(ip, port, user="root")
            + [
                "if [ -e /var/lib/cloud/DEV_READY ]; then echo READY; "
                "elif [ -e /var/lib/cloud/DEV_FAILED ]; then echo FAILED; "
                "else echo WAIT; fi"
            ],
            capture_output=True,
            text=True,
        )
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
            log("  instalando Node, Claude Code y gh en el droplet (2-4 min)…")
            warned = True
        time.sleep(10)
    die(
        "Se agotó la espera a que el droplet terminase de instalar las herramientas.\n"
        "  python scripts/do_droplet.py ssh --cmd 'tail -40 /var/log/dev-tools-install.log'"
    )


def build_provision_script(repos: list[str]) -> str:
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
            f'if printf "%s\\n" {shq(github_token)} | '
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

    parts += [
        "",
        "# --- comprobación final",
        'echo "  claude: $(claude --version 2>&1 | head -1)"',
        'echo "  auth:   $(sudo -u "$DEV_USER" -H bash -lc '
        "'claude auth status' 2>&1 | tr -d '\\n ' )\"",
    ]
    return "\n".join(parts) + "\n"


def cmd_provision(args: argparse.Namespace) -> None:
    name = args.name or cfg("DO_DROPLET_NAME")
    _, ip, port = resolve_target(name, args.port or 0)
    log(f"Aprovisionando '{name}' ({ip}:{port})…")

    if not args.skip_wait:
        wait_for_dev_tools(ip, port)

    repos = args.repo or [r for r in cfg("DO_REPOS").split(",") if r.strip()]
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

    code = run_remote_script(ip, port, build_provision_script(repos))
    if code != 0:
        die(f"El aprovisionamiento falló (código {code}).")
    log("Aprovisionamiento terminado.")


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
    p.add_argument("--dry-run", action="store_true", help="muestra la petición sin enviarla")
    p.add_argument(
        "--repo", action="append", default=[], help="owner/repo a clonar (repetible)"
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
        "--skip-wait",
        action="store_true",
        help="no esperar al testigo de instalación de cloud-init",
    )
    p.set_defaults(func=cmd_provision)

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
    p.add_argument("--yes", action="store_true", help="sin confirmación interactiva")
    p.set_defaults(func=cmd_destroy)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
