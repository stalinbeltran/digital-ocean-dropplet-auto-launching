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


def wait_for_ssh(ip: str, timeout: int = 300) -> bool:
    """`status: active` no implica que sshd escuche todavía."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((ip, 22), timeout=5):
                return True
        except OSError:
            time.sleep(5)
    return False


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

    log("Esperando a que SSH acepte conexiones…")
    if wait_for_ssh(ip):
        log("SSH listo.")
    else:
        log("Aviso: SSH no respondió a tiempo. El droplet existe; reintenta la conexión.")

    key_file = Path(cfg("DO_SSH_KEY_FILE")).expanduser()
    log("\n" + "=" * 62)
    log(f"  {name}  ·  {ip}")
    log(f"  ssh -i {key_file} {cfg('DO_SSH_USER')}@{ip}")
    log(f"  o simplemente:  python scripts/do_droplet.py ssh {name}")
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
    droplets = find_droplets(name=args.name or cfg("DO_DROPLET_NAME"))
    if not droplets:
        die("No encontré ese droplet. Míralos con: python scripts/do_droplet.py list")
    ip = public_ip(droplets[0])
    key_file = str(Path(cfg("DO_SSH_KEY_FILE")).expanduser())
    cmd = ["ssh", "-i", key_file, f"{cfg('DO_SSH_USER')}@{ip}"]
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
    p.set_defaults(func=cmd_launch)

    p = sub.add_parser("list", help="lista los droplets de la cuenta")
    p.add_argument("--tag")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("ip", help="imprime la IP pública")
    p.add_argument("name", nargs="?")
    p.set_defaults(func=cmd_ip)

    p = sub.add_parser("ssh", help="conecta por SSH")
    p.add_argument("name", nargs="?")
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
