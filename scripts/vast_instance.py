#!/usr/bin/env python3
"""Alquila máquinas en Vast.ai, corre un benchmark en cada una y las destruye.

Es el hermano de `do_droplet.py` para el segundo proveedor. No lo sustituye: la
máquina de control -donde corre Claude y desde donde se dispara el barrido- sigue
siendo un droplet de DigitalOcean. Lo que se alquila aquí son las máquinas de
medir, que viven minutos.

    python scripts/vast_instance.py offers --cpus 8
    python scripts/vast_instance.py register-key
    python scripts/vast_instance.py launch prueba --cpus 8
    python scripts/vast_instance.py list
    python scripts/vast_instance.py destroy prueba
    python scripts/vast_instance.py sweep --cpus 2,4,8,16 --bench foveal-cpu

Sólo stdlib de Python 3.9+ y `ssh`, igual que el resto del repo: esto tiene que
correr dentro de un droplet recién nacido sin `pip install`.

Lo que hay que saber antes de tocarlo está en CLAUDE.md, sección "Vast.ai".
Los dos que más muerden:

- **`num_gpus: 0` NO devuelve máquinas sin GPU: devuelve ofertas de disco.** Las
  64 que salen traen `resource_type: "disk"`, `cpu_ram: 0` y 256 núcleos por
  0,01 $/h, que es demasiado bueno para ser verdad porque no es una máquina, es
  almacenamiento. Por eso `buscar_ofertas()` filtra por `resource_type == "gpu"`
  y el barrido de CPU alquila máquinas CON GPU y usa sólo sus vCPU. Medido el
  2026-08-20.
- **Ningún secreto viaja a una máquina de Vast.** Son ordenadores de
  desconocidos alquilados por minutos. El código y el dataset se empujan por SSH
  como un tar; el token de GitHub, el de Claude y el de DigitalOcean se quedan en
  la máquina de control. Es el objetivo 5 del proyecto llevado a su extremo
  lógico: allí el problema no es el `user_data`, es el host.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENCH_DIR = ROOT / "benchmarks"
RESULT_DIR = ROOT / "results"
# Sin barra final: las rutas la llevan ya, tal cual salen del OpenAPI de Vast.ai
# (https://docs.vast.ai/api-reference/openapi.json).
API = "https://console.vast.ai"

DEFAULTS = {
    # La imagen oficial de Vast.ai: Ubuntu con python3, sshd y poco más. Un
    # benchmark que necesite otra cosa la pide en su descriptor.
    "VAST_IMAGE": "vastai/base-image:@vastai-automatic-tag",
    "VAST_DISK_GB": "24",
    # Freno de coste, hermano de DO_MAX_PRICE_MONTHLY. Aquí en $/hora porque Vast
    # factura por segundo y estas máquinas viven minutos: un mensual no dice
    # nada. 0,50 $/h deja pasar toda la gama de medir y para una H200 a 2,07.
    "VAST_MAX_PRICE_HOURLY": "0.50",
    "VAST_SSH_KEY_FILE": str(Path.home() / ".ssh" / "do_droplet"),
    "VAST_SSH_USER": "root",
    # Cuánto se espera a que la instancia arranque. Vast tiene que descargar la
    # imagen Docker en la máquina del host, y eso depende de la red del host, no
    # de la nuestra: 10 minutos no es exagerado.
    "VAST_BOOT_TIMEOUT": "600",
}


# ---------------------------------------------------------------- configuración


def load_env() -> None:
    """Carga .env sin dependencias. Las variables reales del entorno mandan.

    Repetido a propósito desde do_droplet.py y vast_check.py: cada script tiene
    que poder ejecutarse suelto, sin que uno arrastre a los otros.
    """
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
    """El token de Vast.ai, o un mensaje que dice de dónde sacarlo."""
    tok = (
        os.environ.get("VAST_AI_API_TOKEN")
        or os.environ.get("VAST_API_KEY")  # el nombre que usa el CLI oficial
        or ""
    )
    if not tok:
        die(
            "Falta el token de Vast.ai. Ponlo en .env como VAST_AI_API_TOKEN.\n"
            "  Se crea en https://cloud.vast.ai/manage-keys/ (Account -> Keys).\n"
            "  Ojo: las claves de Vast.ai pueden ir con permisos recortados; para\n"
            "  alquilar hace falta una con permiso de escritura sobre instancias.\n"
            "  Compruébalo sin gastar nada:  python scripts/vast_check.py"
        )
    return tok


def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"\nERROR: {msg}\n", file=sys.stderr)
    raise SystemExit(1)


def log(msg: str) -> None:
    print(msg, flush=True)


def confirmar(pregunta: str) -> bool:
    """Pregunta sí/no. Sin terminal se niega en vez de dar por hecho que sí."""
    if not sys.stdin.isatty():
        die(
            f"{pregunta}\n"
            "  No hay terminal para preguntar. Repite el comando con --yes si "
            "estás seguro."
        )
    return input(f"{pregunta} [s/N] ").strip().lower() in ("s", "si", "sí", "y", "yes")


def force_utf8_output() -> None:
    """La consola de Windows usa cp1252 por defecto y peta con acentos."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass


def ahora_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ------------------------------------------------------------------ cliente API


class ApiError(Exception):
    pass


def api(method: str, path: str, body: dict | None = None) -> dict | list:
    """Petición a la API de Vast.ai, con reintentos ante 429 y 5xx.

    Vast.ai limita a unas 3 peticiones por segundo POR ENDPOINT y contesta 429
    con `API requests too frequent endpoint threshold=3.0`. Sin el reintento, un
    barrido que consulte el catálogo varias veces seguidas da un falso fallo.

    Los errores de alquilar se traducen a algo que se entienda: `no_such_ask`
    significa que la oferta se la llevó otro entre la búsqueda y la compra, que
    en un marketplace pasa constantemente y no es un fallo del programa.
    """
    url = f"{API}{path}"
    payload = json.dumps(body).encode("utf-8") if body is not None else None

    last_error = ""
    for attempt in range(5):
        req = urllib.request.Request(url, data=payload, method=method)
        req.add_header("Authorization", f"Bearer {token()}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            if exc.code == 429 and attempt < 4:
                time.sleep(1 + attempt)
                continue
            if exc.code >= 500 and attempt < 4:
                time.sleep(2**attempt)
                continue
            if exc.code in (401, 403):
                raise ApiError(
                    f"HTTP {exc.code}: el token no vale para {method} {path}.\n"
                    "  O está mal copiado, o la clave se creó con permisos "
                    "recortados: una de sólo lectura autentica, lista el catálogo\n"
                    "  y parece correcta, y falla justo aquí, al alquilar.\n"
                    f"  Respuesta: {detail}"
                )
            if exc.code in (404, 410) and "/asks/" in path:
                raise ApiError(
                    "la oferta ya no está disponible: se la ha llevado otro entre "
                    "la búsqueda y la compra.\n"
                    "  En un marketplace es normal; reintenta y saldrá otra.\n"
                    f"  Respuesta: {detail}"
                )
            raise ApiError(f"HTTP {exc.code} en {method} {path}: {detail}")
        except (urllib.error.URLError, OSError) as exc:
            # urlopen() puede volver bien y expirar al leer el cuerpo; eso llega
            # como TimeoutError, que no es URLError. Por eso se capturan los dos.
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(2**attempt)
    raise ApiError(f"Sin respuesta tras varios reintentos: {last_error}")


# --------------------------------------------------------------------- catálogo


def buscar_ofertas(
    cpus: int = 0,
    max_cpus: int = 0,
    min_ram_gb: float = 0.0,
    max_price: float = 0.0,
    limit: int = 64,
    orden: str = "dph_total",
) -> list[dict]:
    """Ofertas alquilables del marketplace, ya limpias.

    Dos filtros que no son opcionales, y por qué:

    - `resource_type == "gpu"`: el catálogo mezcla ofertas de ALMACENAMIENTO con
      las de máquina. Salen con `num_gpus: 0`, `cpu_ram: 0` y precios de risa, y
      alquilar una no da ninguna máquina. Medido el 2026-08-20: las 64 que
      devuelve `num_gpus: {eq: 0}` son las 64 de disco.
    - `verified`: Vast marca `deverified` a los hosts que fallaron sus
      comprobaciones. Medir velocidad en uno de ésos es medir ruido.

    El nivel de CPU sale de `cpu_cores_effective`, que es lo que de verdad toca a
    la porción alquilada, no los núcleos del host entero.
    """
    consulta: dict = {
        "limit": min(max(limit, 1), 64),  # la API corta en 64 aunque pidas más
        "rentable": {"eq": True},
        "verified": {"eq": True},
        "num_gpus": {"eq": 1},
        "order": [[orden, "asc"]],
    }
    rango: dict = {}
    if cpus:
        rango["gte"] = cpus
    if max_cpus:
        rango["lt"] = max_cpus
    if rango:
        consulta["cpu_cores_effective"] = rango
    if min_ram_gb:
        consulta["cpu_ram"] = {"gte": int(min_ram_gb * 1024)}
    if max_price:
        consulta["dph_total"] = {"lte": max_price}

    resp = api("POST", "/api/v0/bundles/", consulta)
    ofertas = resp.get("offers") if isinstance(resp, dict) else None
    return [o for o in (ofertas or []) if o.get("resource_type") == "gpu"]


def limite_precio() -> float:
    try:
        return float(cfg("VAST_MAX_PRICE_HOURLY"))
    except ValueError:
        die(f"VAST_MAX_PRICE_HOURLY no es un número: {cfg('VAST_MAX_PRICE_HOURLY')!r}")


def oferta_fila(o: dict) -> str:
    ram = (o.get("cpu_ram") or 0) / 1024
    return (
        f"{o['id']:>10}  {o.get('cpu_cores_effective', 0):>6.1f}  {ram:>7.1f}  "
        f"{o.get('disk_space', 0):>6.0f}  {o.get('dph_total', 0):>8.4f}  "
        f"{(o.get('gpu_name') or '?')[:14]:<14}  {o.get('geolocation') or '?'}"
    )


def cabecera_ofertas() -> str:
    return (
        f"{'OFERTA':>10}  {'vCPU':>6}  {'RAM GB':>7}  {'DISCO':>6}  {'$/HORA':>8}  "
        f"{'GPU':<14}  UBICACION"
    )


def cmd_offers(args: argparse.Namespace) -> None:
    tope = args.max_price or limite_precio()
    ofertas = buscar_ofertas(
        cpus=args.cpus,
        max_cpus=args.max_cpus,
        min_ram_gb=args.min_ram,
        max_price=tope,
        orden="cpu_cores_effective" if args.by_cpus else "dph_total",
    )
    if not ofertas:
        log(
            f"Ninguna oferta con {args.cpus or 'cualquier'} vCPU por debajo de "
            f"{tope:.2f} $/h.\n"
            "  Prueba a bajar --cpus, subir --max-price o quitar --min-ram."
        )
        return
    log(cabecera_ofertas())
    for o in ofertas[: args.limit]:
        log(oferta_fila(o))
    log(
        f"\n{len(ofertas)} ofertas (la API corta en 64 por consulta). "
        "El barrido coge la mas barata de cada nivel."
    )
    log(
        "Se alquila una maquina CON GPU y se usa solo su CPU: en Vast.ai las\n"
        "ofertas sin GPU son de almacenamiento, no de computo (CLAUDE.md)."
    )


# ------------------------------------------------------------------- claves SSH


def clave_privada() -> Path:
    return Path(cfg("VAST_SSH_KEY_FILE")).expanduser()


def clave_publica() -> Path:
    return Path(str(clave_privada()) + ".pub")


def asegurar_clave_local(comentario: str = "vast") -> str:
    """Devuelve la pública, generando el par si no existe.

    Que la genere aquí importa: este script corre DENTRO de la máquina de
    control, que nace sin claves propias. Sin esto habría que acordarse de
    crearlas a mano antes de lanzar nada, y el olvido no se ve hasta que la
    máquina ya está alquilada y facturando sin dejarte entrar.
    """
    priv, pub = clave_privada(), clave_publica()
    if not pub.exists():
        priv.parent.mkdir(parents=True, exist_ok=True)
        try:
            priv.parent.chmod(0o700)
        except OSError:
            pass  # en Windows no aplica, y no es un fallo
        log(f"No hay clave en {priv}; generando un par ed25519...")
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(priv), "-N", "", "-C", comentario],
            check=True,
        )
    return pub.read_text(encoding="utf-8").strip()


def claves_cuenta() -> list[dict]:
    resp = api("GET", "/api/v0/ssh/")
    claves = resp.get("ssh_keys") if isinstance(resp, dict) else resp
    return claves or []


def material(clave: str) -> str:
    """La parte que identifica a una clave: ni el tipo ni el comentario.

    Comparar la línea entera daría duplicados en cuanto cambiara el comentario, y
    el síntoma sería una cuenta llena de claves que en realidad son la misma.
    """
    partes = clave.split()
    return partes[1] if len(partes) > 1 else clave


def cmd_keys(args: argparse.Namespace) -> None:
    claves = claves_cuenta()
    if not claves:
        log("No hay ninguna clave SSH registrada en la cuenta de Vast.ai.")
        log("  Registrala con:  python scripts/vast_instance.py register-key")
        return
    mia = ""
    if clave_publica().exists():
        mia = material(clave_publica().read_text(encoding="utf-8"))
    for k in claves:
        pub = k.get("public_key") or k.get("ssh_key") or ""
        marca = "   <- la de esta maquina" if mia and material(pub) == mia else ""
        log(f"{str(k.get('id')):>10}  {pub[:52]}...{marca}")


def cmd_register_key(args: argparse.Namespace) -> None:
    """Sube la pública de esta máquina a la cuenta de Vast.ai.

    Es la mitad que falta para que la máquina de control pueda usar lo que
    alquila. Con el token solo, puede CREAR instancias pero no ENTRAR en ellas, y
    eso se descubre tarde: la máquina existe, factura y su creador se queda
    fuera. Misma lección que `--make-launcher` en DigitalOcean.
    """
    if args.file:
        pub = Path(args.file).expanduser().read_text(encoding="utf-8").strip()
    else:
        pub = asegurar_clave_local(comentario=args.comment or socket.gethostname())

    if not pub.startswith(("ssh-", "ecdsa-", "sk-")):
        die(f"Eso no parece una clave publica SSH: {pub[:40]!r}")

    for k in claves_cuenta():
        existente = k.get("public_key") or k.get("ssh_key") or ""
        if material(existente) == material(pub):
            log(f"Ya estaba registrada en Vast.ai (id {k.get('id')}). No se duplica.")
            return
    api("POST", "/api/v0/ssh/", {"ssh_key": pub})
    log("Clave registrada en Vast.ai. Las instancias que alquiles la aceptaran.")
    log("  (las alquiladas ANTES, no: la lista se fija al crearlas)")


# ------------------------------------------------------------------ instancias


def instancias() -> list[dict]:
    resp = api("GET", "/api/v1/instances/")
    return (resp.get("instances") if isinstance(resp, dict) else None) or []


def instancia(iid: int) -> dict:
    resp = api("GET", f"/api/v0/instances/{iid}/")
    if isinstance(resp, dict):
        return resp.get("instances") or resp.get("instance") or resp
    return {}


def buscar_instancia(ref: str) -> dict:
    """Localiza una instancia por etiqueta o por id, y dice qué hay si no está."""
    vivas = instancias()
    for i in vivas:
        if str(i.get("id")) == str(ref) or (i.get("label") or "") == ref:
            return i
    if not vivas:
        die("No tienes ninguna instancia viva en Vast.ai.")
    nombres = ", ".join(f"{i.get('label') or '?'} ({i.get('id')})" for i in vivas)
    die(f"No encuentro '{ref}'. Vivas ahora mismo: {nombres}")


def coste_hora(i: dict) -> float:
    return float(i.get("dph_total") or 0)


def ssh_destino(i: dict) -> tuple[str, int]:
    """Host y puerto por los que se entra.

    Vast.ai ofrece dos caminos: el proxy (`ssh_host` = sshN.vast.ai) y el directo
    (la IP del host con un puerto mapeado). Se usa el proxy porque es el único
    que funciona siempre: muchas máquinas del marketplace están detrás de NAT y
    no tienen puerto directo abierto (`direct_port_count: 0`).
    """
    host = i.get("ssh_host") or ""
    port = int(i.get("ssh_port") or 0)
    return host, port


def ssh_command(host: str, port: int) -> list[str]:
    return [
        "ssh",
        "-p", str(port),
        "-i", str(clave_privada()),
        # Una máquina de Vast vive minutos y el proxy reutiliza host y puerto
        # entre alquileres distintos, así que la clave de host cambia cada vez.
        # Fijarla sólo produciría el aviso de "REMOTE HOST IDENTIFICATION HAS
        # CHANGED" y una negativa a conectar en el segundo barrido.
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        # Keepalives y timeout por la misma razón que en do_droplet.py: sin
        # ellos, una conexión que se queda medio abierta cuelga al proceso que
        # la lanzó para siempre, y el síntoma es "se quedo pensando".
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=4",
        "-o", "ConnectTimeout=15",
        f"{cfg('VAST_SSH_USER')}@{host}",
    ]


def ssh_banner_ok(host: str, port: int, timeout: int = 8) -> bool:
    """Comprueba que hay un sshd DE VERDAD contestando, no sólo un puerto TCP.

    Misma lección que en DigitalOcean: el proxy de Vast.ai acepta la conexión TCP
    desde el primer segundo, mucho antes de que el contenedor tenga sshd. Esperar
    al `connect()` daría por listo lo que aún no lo está, y el siguiente comando
    fallaría con un error de conexión que no dice nada.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            return sock.recv(64).startswith(b"SSH-")
    except OSError:
        return False


def esperar_estado(iid: int, timeout: int) -> dict:
    """Espera a que la instancia esté `running`, con backoff.

    Vast tiene que descargar la imagen Docker en la máquina del host antes de
    arrancar nada, así que `loading` puede durar minutos y no es un fallo. Lo que
    sí lo es: si acaba en un estado terminal distinto de `running`, se devuelve
    igualmente para que quien llame la destruya. Una instancia rota factura lo
    mismo que una buena.
    """
    fin = time.time() + timeout
    espera = 5.0
    ultimo = ""
    while time.time() < fin:
        info = instancia(iid)
        estado = (info.get("actual_status") or info.get("cur_state") or "").lower()
        if estado != ultimo:
            log(f"  estado: {estado or '(sin estado todavia)'}")
            ultimo = estado
        if estado == "running":
            return info
        if estado in ("exited", "error", "offline"):
            return info
        time.sleep(espera)
        espera = min(espera * 1.4, 20.0)
    return instancia(iid)


def esperar_ssh(host: str, port: int, timeout: int = 300) -> bool:
    fin = time.time() + timeout
    while time.time() < fin:
        if ssh_banner_ok(host, port):
            return True
        time.sleep(5)
    return False


def alquilar(oferta: dict, label: str, image: str, disk_gb: float) -> int:
    """PUT /api/v0/asks/{id}/ -- esto es lo que cuesta dinero.

    Todo lo demás de este fichero es de lectura. Aquí empieza la facturación por
    segundo, y por eso quien llama tiene que tener ya escrito su camino de
    destrucción antes de llamar.
    """
    cuerpo = {
        "image": image,
        "disk": disk_gb,
        "label": label,
        "runtype": "ssh",
        "target_state": "running",
        # Si no se puede arrancar ya, que no se quede en cola: el barrido prefiere
        # probar otra oferta a esperar a una máquina que quiza no llegue nunca.
        "cancel_unavail": True,
    }
    resp = api("PUT", f"/api/v0/asks/{oferta['id']}/", cuerpo)
    if not isinstance(resp, dict) or not resp.get("success"):
        raise ApiError(f"Vast.ai no acepto el alquiler: {json.dumps(resp)[:300]}")
    return int(resp["new_contract"])


def destruir(iid: int) -> None:
    api("DELETE", f"/api/v0/instances/{iid}/", {})


def elegir_oferta(cpus: int, max_cpus: int, min_ram_gb: float, max_price: float) -> dict:
    ofertas = buscar_ofertas(
        cpus=cpus, max_cpus=max_cpus, min_ram_gb=min_ram_gb, max_price=max_price
    )
    if not ofertas:
        die(
            f"No hay ninguna oferta con >= {cpus} vCPU"
            + (f" y < {max_cpus}" if max_cpus else "")
            + (f", >= {min_ram_gb:g} GB de RAM" if min_ram_gb else "")
            + f" por debajo de {max_price:.2f} $/h.\n"
            "  Mira que hay con:  python scripts/vast_instance.py offers "
            f"--cpus {cpus}"
        )
    return ofertas[0]


def cmd_launch(args: argparse.Namespace) -> None:
    label = args.label
    tope = args.max_price or limite_precio()
    image = args.image or cfg("VAST_IMAGE")
    disk = args.disk or float(cfg("VAST_DISK_GB"))

    if args.offer:
        candidatas = [o for o in buscar_ofertas(limit=64) if str(o["id"]) == str(args.offer)]
        oferta = candidatas[0] if candidatas else {"id": int(args.offer)}
    else:
        oferta = elegir_oferta(args.cpus, args.max_cpus, args.min_ram, tope)

    precio = float(oferta.get("dph_total") or 0)
    if precio > tope:
        die(
            f"La oferta {oferta['id']} cuesta {precio:.4f} $/h y el tope es "
            f"{tope:.2f} $/h (VAST_MAX_PRICE_HOURLY).\n"
            "  Súbelo con --max-price si de verdad quieres esa máquina."
        )

    log(cabecera_ofertas())
    if oferta.get("gpu_name"):
        log(oferta_fila(oferta))

    if args.dry_run:
        log("\n--dry-run: no se alquila nada. Se enviaria:")
        log(
            json.dumps(
                {
                    "PUT": f"/api/v0/asks/{oferta['id']}/",
                    "image": image,
                    "disk": disk,
                    "label": label,
                    "runtype": "ssh",
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    iid = alquilar(oferta, label, image, disk)
    log(f"\nAlquilada. Instancia {iid} ('{label}'), {precio:.4f} $/h. Arrancando...")

    info = esperar_estado(iid, int(cfg("VAST_BOOT_TIMEOUT")))
    estado = (info.get("actual_status") or info.get("cur_state") or "?").lower()
    if estado != "running":
        log(
            f"AVISO: la instancia acabo en estado '{estado}', no 'running'.\n"
            "  Sigue facturando mientras exista. Destruyela con:\n"
            f"  python scripts/vast_instance.py destroy {iid} --yes"
        )
        return

    host, port = ssh_destino(info)
    log(f"Running. SSH en {host}:{port}. Esperando al banner de sshd...")
    if esperar_ssh(host, port):
        log("SSH listo.")
    else:
        log("AVISO: sshd no contesto. La instancia existe y factura igual.")

    log("\n" + "=" * 62)
    log(f"  {label}  ·  instancia {iid}  ·  {precio:.4f} $/h")
    log(f"  ssh -p {port} -i {clave_privada()} {cfg('VAST_SSH_USER')}@{host}")
    log(f"  o simplemente:  python scripts/vast_instance.py ssh {label}")
    log(f"\n  Al terminar:    python scripts/vast_instance.py destroy {label}")
    log("  (factura por segundo mientras exista)")
    log("=" * 62)


def cmd_list(args: argparse.Namespace) -> None:
    vivas = instancias()
    if not vivas:
        log("No hay ninguna instancia viva en Vast.ai. No se esta facturando nada.")
        return
    log(f"{'ID':>10}  {'ETIQUETA':<18}  {'ESTADO':<10}  {'vCPU':>6}  {'$/H':>8}  SSH")
    total = 0.0
    for i in vivas:
        total += coste_hora(i)
        host, port = ssh_destino(i)
        log(
            f"{i.get('id'):>10}  {(i.get('label') or '-')[:18]:<18}  "
            f"{(i.get('actual_status') or '?')[:10]:<10}  "
            f"{float(i.get('cpu_cores_effective') or 0):>6.1f}  "
            f"{coste_hora(i):>8.4f}  {host}:{port}"
        )
    log(f"\nGastando ahora: {total:.4f} $/h  ({total * 24:.2f} $/dia si se quedan).")
    log("Se corta destruyendolas:  python scripts/vast_instance.py destroy <id> --yes")


def cmd_ssh(args: argparse.Namespace) -> None:
    i = buscar_instancia(args.label)
    host, port = ssh_destino(i)
    if not host:
        die("Esa instancia no tiene todavia datos de SSH. Mirala con `list`.")
    cmd = ssh_command(host, port)
    if args.cmd:
        cmd.append(args.cmd)
    raise SystemExit(subprocess.run(cmd).returncode)


def cmd_destroy(args: argparse.Namespace) -> None:
    if args.all:
        objetivo = instancias()
        if not objetivo:
            log("No hay nada que destruir.")
            return
    else:
        if not args.label:
            die("Dime que instancia destruir, o usa --all.")
        objetivo = [buscar_instancia(args.label)]

    gasto = sum(coste_hora(i) for i in objetivo)
    listado = ", ".join(f"{i.get('label') or '?'} ({i.get('id')})" for i in objetivo)
    if not args.yes and not confirmar(
        f"Voy a destruir {len(objetivo)} instancia(s) [{listado}], "
        f"{gasto:.4f} $/h. ¿Sigo?"
    ):
        log("Cancelado. Siguen vivas y facturando.")
        return

    for i in objetivo:
        try:
            destruir(int(i["id"]))
            log(f"  destruida {i.get('label') or ''} ({i['id']})")
        except ApiError as exc:
            log(f"  AVISO: no pude destruir {i['id']}: {exc}")
    log(f"Dejas de pagar {gasto:.4f} $/h.")


# ------------------------------------------------------------------ benchmarks
#
# Un benchmark es un fichero JSON de benchmarks/, igual que un servicio es un
# JSON de services/ y un tipo de maquina uno de types/. Es DATO, no codigo:
# anadir un benchmark nuevo no debe requerir tocar este fichero.


CAMPOS_BENCH = ("descripcion", "envia", "install", "run", "recoge")


def load_bench(name: str) -> dict:
    path = BENCH_DIR / f"{name}.json"
    if not path.exists():
        disponibles = ", ".join(sorted(b["name"] for b in all_benches())) or "(ninguno)"
        die(f"No existe el benchmark '{name}'. Los que hay: {disponibles}")
    try:
        datos = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"{path.name} no es JSON valido: {exc}")
    faltan = [c for c in CAMPOS_BENCH if c not in datos]
    if faltan:
        die(f"A {path.name} le faltan campos obligatorios: {', '.join(faltan)}")
    datos["name"] = name
    return datos


def all_benches() -> list[dict]:
    salida = []
    if not BENCH_DIR.exists():
        return salida
    for path in sorted(BENCH_DIR.glob("*.json")):
        try:
            datos = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        datos["name"] = path.stem
        salida.append(datos)
    return salida


def cmd_benchs(args: argparse.Namespace) -> None:
    todos = all_benches()
    if not todos:
        log("No hay ningun benchmark en benchmarks/.")
        return
    for b in todos:
        log(f"{b['name']:<16} {b.get('descripcion', '')}")
        for envio in b.get("envia", []):
            log(f"{'':<16}   envia {envio.get('origen')}")
        log(f"{'':<16}   recoge {b.get('recoge')}")


def ruta_origen(origen: str) -> Path:
    """Resuelve el origen de un envio: absoluto, ~, $VAR o relativo a este repo.

    `$BENCH_SRC` es donde viven los repos que se van a medir. Por defecto
    `~/src`, que es donde los deja `do_droplet.py provision`, asi que en la
    maquina de control no hay nada que configurar. Existe la variable para poder
    probar el barrido desde la laptop, donde los repos estan en otro sitio
    (`BENCH_SRC=c:/Desarrollo`), sin tocar el descriptor: el descriptor describe
    QUE se manda, no donde esta guardado en cada maquina.
    """
    # `setdefault` no vale: .env.example trae `BENCH_SRC=` vacío, y load_env lo
    # mete en el entorno como cadena vacía. Con setdefault, `$BENCH_SRC/repo` se
    # expandiría a `/repo` y el fallo sería un "no encuentro /foveal-vision" que
    # no se parece en nada a "te falta configurar una variable".
    if not os.environ.get("BENCH_SRC"):
        os.environ["BENCH_SRC"] = str(Path.home() / "src")
    p = Path(os.path.expandvars(origen)).expanduser()
    return p if p.is_absolute() else (ROOT / p)


def construir_tar(envios: list[dict]) -> Path:
    """Empaqueta lo que hay que subir a la maquina alquilada.

    Se construye con `tarfile` en vez de llamar a `tar` para que esto funcione
    igual desde Windows, donde el barrido se depura antes de llevarlo al droplet.

    Va por SSH y no por `git clone` a proposito: clonar exigiria darle a un
    ordenador de un desconocido un token de GitHub, y el dato del benchmark ni
    siquiera esta en git. Lo que viaja es codigo y datos publicos, nada mas.
    """
    tmp = Path(tempfile.mkdtemp(prefix="vast-bench-")) / "payload.tar.gz"
    with tarfile.open(tmp, "w:gz") as tar:
        for envio in envios:
            origen = ruta_origen(envio["origen"])
            if not origen.exists():
                die(
                    f"No encuentro {origen}, que pide el benchmark.\n"
                    "  Si estas en la maquina de control, comprueba que el repo "
                    "esta clonado en ~/src\n"
                    "  y que el dataset del benchmark esta en su sitio."
                )
            destino = envio.get("destino") or origen.name
            excluidos = set(envio.get("excluye") or [])
            # Cuantos componentes hay que quitar del nombre dentro del tar para
            # que lo que se compara con `excluye` sea la ruta RELATIVA al origen.
            # El destino puede tener varios ("foveal-vision/data/sources/x"), y
            # dar por hecho que es uno solo hacia que los excluidos no casaran.
            raiz = len(Path(destino).parts)

            def filtro(
                info: tarfile.TarInfo, excluidos: set = excluidos, raiz: int = raiz
            ) -> "tarfile.TarInfo | None":
                partes = Path(info.name).parts[raiz:]
                for i in range(1, len(partes) + 1):
                    if "/".join(partes[:i]) in excluidos or partes[i - 1] in excluidos:
                        return None
                return info

            tar.add(str(origen), arcname=destino, filter=filtro)
    return tmp


def ssh_script(host: str, port: int, script: str, timeout: int) -> int:
    """Ejecuta un script en la instancia pasandolo por stdin.

    Por stdin y no como argumento de ssh por la misma razon que en do_droplet.py:
    lo que va en la linea de comandos acaba en el `ps` de la maquina remota.
    Aqui ademas la maquina es de un desconocido.
    """
    proc = subprocess.run(
        ssh_command(host, port) + ["bash -s"],
        input=script.encode("utf-8"),
        timeout=timeout,
    )
    return proc.returncode


def ssh_capture(host: str, port: int, script: str, timeout: int) -> tuple[int, str]:
    proc = subprocess.run(
        ssh_command(host, port) + ["bash -s"],
        input=script.encode("utf-8"),
        capture_output=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace")


def subir_payload(host: str, port: int, tar_path: Path, timeout: int) -> None:
    with tar_path.open("rb") as fh:
        proc = subprocess.run(
            ssh_command(host, port) + ["cat > /root/payload.tar.gz"],
            stdin=fh,
            timeout=timeout,
        )
    if proc.returncode != 0:
        raise RuntimeError("no pude subir el payload a la instancia")
    if ssh_script(
        host,
        port,
        "set -eu\nmkdir -p /root/bench\ntar -xzf /root/payload.tar.gz -C /root/bench\n",
        timeout,
    ) != 0:
        raise RuntimeError("el payload subio pero no se pudo desempaquetar")


def correr_bench(host: str, port: int, bench: dict, timeout: int) -> dict:
    """Instala, mide y devuelve el JSON que dejo el benchmark.

    `install` y `run` van en dos pasos para que el log diga cual de los dos
    fallo: instalar torch tarda minutos y falla por motivos (red, disco) que no
    tienen nada que ver con los del benchmark en si.
    """
    prologo = "set -eu\ncd /root/bench\n"

    log("  instalando dependencias...")
    inicio = time.time()
    if ssh_script(host, port, prologo + bench["install"] + "\n", timeout) != 0:
        raise RuntimeError("fallo la instalacion de dependencias en la instancia")
    log(f"  instalado en {time.time() - inicio:.0f}s. Midiendo...")

    inicio = time.time()
    if ssh_script(host, port, prologo + bench["run"] + "\n", timeout) != 0:
        raise RuntimeError("el benchmark fallo al ejecutarse")
    segundos = time.time() - inicio
    log(f"  medido en {segundos:.0f}s. Recogiendo el reporte...")

    code, salida = ssh_capture(
        host, port, f"set -eu\ncat {bench['recoge']}\n", timeout=120
    )
    if code != 0 or not salida.strip():
        raise RuntimeError(
            f"el benchmark termino pero no dejo nada en {bench['recoge']}"
        )
    try:
        reporte = json.loads(salida)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"lo que dejo en {bench['recoge']} no es JSON: {exc}")
    reporte["_segundos_de_medida"] = round(segundos, 1)
    return reporte


def valor_metrica(reporte: dict, ruta: str) -> float | None:
    """Saca del reporte el numero que se compara, siguiendo `a.b.c`."""
    actual: object = reporte
    for parte in ruta.split("."):
        if not isinstance(actual, dict) or parte not in actual:
            return None
        actual = actual[parte]
    return float(actual) if isinstance(actual, (int, float)) else None


# ---------------------------------------------------------------- resultados


def guardar_resultado(bench: dict, resultado: dict) -> Path:
    destino = RESULT_DIR / bench["name"]
    destino.mkdir(parents=True, exist_ok=True)
    marca = resultado["medido"].replace(":", "").replace("-", "")[:15]
    cores = resultado.get("maquina", {}).get("vcpu") or 0
    path = destino / f"{marca}-{cores:g}vcpu-{resultado['instancia']}.json"
    path.write_text(
        json.dumps(resultado, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path


def escribir_tabla(bench: dict) -> Path | None:
    """Regenera la tabla comparativa a partir de los JSON guardados.

    Se regenera entera en vez de ir anadiendo lineas: asi borrar una medida mala
    es borrar su fichero, y la tabla no puede quedar diciendo algo que ya no
    esta respaldado por ningun dato.
    """
    destino = RESULT_DIR / bench["name"]
    if not destino.exists():
        return None
    medidas = []
    for path in sorted(destino.glob("*.json")):
        try:
            medidas.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    if not medidas:
        return None

    metrica = bench.get("metrica", "")
    unidad = bench.get("unidad", "")
    filas = []
    for m in medidas:
        maq = m.get("maquina", {})
        valor = m.get("metrica")
        precio = float(m.get("usd_hora") or 0)
        filas.append(
            {
                "vcpu": float(maq.get("vcpu") or 0),
                "cpu": maq.get("cpu") or "?",
                "ram": float(maq.get("ram_gb") or 0),
                "sitio": maq.get("ubicacion") or "?",
                "valor": valor,
                "usd_hora": precio,
                "usd_medida": m.get("usd_medida"),
                "medido": m.get("medido", "")[:10],
            }
        )
    filas.sort(key=lambda f: f["vcpu"])

    lineas = [
        f"# {bench['name']}: {bench.get('descripcion', '')}",
        "",
        "Generada por `vast_instance.py sweep`. **No se edita a mano**: se rehace",
        "entera a partir de los JSON de este directorio, que son el dato.",
        "",
        f"Metrica: `{metrica}`" + (f" ({unidad})" if unidad else ""),
        "",
        "| vCPU | CPU | RAM GB | metrica | $/h | $ por medida | ubicacion | fecha |",
        "|---:|---|---:|---:|---:|---:|---|---|",
    ]
    for f in filas:
        valor = f"{f['valor']:.3f}" if isinstance(f["valor"], (int, float)) else "-"
        coste = f"{f['usd_medida']:.4f}" if isinstance(f["usd_medida"], (int, float)) else "-"
        lineas.append(
            f"| {f['vcpu']:g} | {f['cpu'][:34]} | {f['ram']:.1f} | {valor} | "
            f"{f['usd_hora']:.4f} | {coste} | {f['sitio']} | {f['medido']} |"
        )
    lineas.append("")
    path = destino / "tabla.md"
    path.write_text("\n".join(lineas), encoding="utf-8")
    return path


def limpiar_texto(valor: str) -> str:
    """Quita de un texto de la API lo que no sea ASCII imprimible.

    Vast.ai devuelve los nombres de CPU con la codificacion rota: el simbolo (R)
    de "Xeon(R) E5-2630 v4" llega como un byte invalido que json lo convierte en
    U+FFFD. Eso acabaria escrito en los ficheros de results/, que son el dato que
    se commitea y se compara dentro de seis meses; un caracter de reemplazo ahi
    ensucia diffs y busquedas para siempre. Se limpia al guardar, no al mostrar.
    """
    limpio = "".join(c if 32 <= ord(c) < 127 else " " for c in valor)
    return " ".join(limpio.split())


def resumen_maquina(oferta: dict) -> dict:
    return {
        "vcpu": float(oferta.get("cpu_cores_effective") or 0),
        "cpu": limpiar_texto(oferta.get("cpu_name") or "?"),
        "ram_gb": round((oferta.get("cpu_ram") or 0) / 1024, 1),
        "ghz": oferta.get("cpu_ghz"),
        "gpu": oferta.get("gpu_name"),
        "ubicacion": oferta.get("geolocation"),
        "host": oferta.get("machine_id"),
        "fiabilidad": round(float(oferta.get("reliability2") or 0), 4),
    }


# ------------------------------------------------------------ medir de verdad


def medir_en_oferta(bench: dict, oferta: dict, tar_path: Path, etiqueta: str) -> dict:
    """Alquila, mide y destruye. La destruccion va en `finally` y no es opcional.

    Es el objetivo 2 del proyecto: todo camino de creacion tiene su camino de
    destruccion, incluido el fallo a mitad. Si el benchmark revienta, la maquina
    se destruye igual; lo que se pierde es la medida, no el dinero.
    """
    image = bench.get("image") or cfg("VAST_IMAGE")
    disk = float(bench.get("disk_gb") or cfg("VAST_DISK_GB"))
    timeout = int(bench.get("timeout") or 3600)
    precio = float(oferta.get("dph_total") or 0)

    log(f"\n--- {etiqueta}")
    log(cabecera_ofertas())
    log(oferta_fila(oferta))

    iid = alquilar(oferta, etiqueta, image, disk)
    arrancada = time.time()
    log(f"  instancia {iid} alquilada a {precio:.4f} $/h")
    try:
        info = esperar_estado(iid, int(cfg("VAST_BOOT_TIMEOUT")))
        estado = (info.get("actual_status") or info.get("cur_state") or "?").lower()
        if estado != "running":
            raise RuntimeError(f"la instancia acabo en '{estado}', no arranco")

        host, port = ssh_destino(info)
        if not esperar_ssh(host, port):
            raise RuntimeError(f"sshd no contesto en {host}:{port}")
        log(f"  SSH listo en {host}:{port}. Subiendo {tar_path.stat().st_size / 1e6:.1f} MB...")

        subir_payload(host, port, tar_path, timeout=600)
        reporte = correr_bench(host, port, bench, timeout)

        vivida = time.time() - arrancada
        return {
            "format_version": 1,
            "benchmark": bench["name"],
            "proveedor": "vast.ai",
            "medido": ahora_iso(),
            "instancia": iid,
            "oferta": oferta.get("id"),
            "maquina": resumen_maquina(oferta),
            "usd_hora": round(precio, 5),
            "usd_medida": round(precio * vivida / 3600, 5),
            "segundos_vivida": round(vivida, 1),
            "metrica": valor_metrica(reporte, bench.get("metrica", "")),
            "reporte": reporte,
        }
    finally:
        log(f"  destruyendo la instancia {iid}...")
        try:
            destruir(iid)
            log(f"  destruida. Vivio {(time.time() - arrancada) / 60:.1f} min, "
                f"{precio * (time.time() - arrancada) / 3600:.4f} $.")
        except ApiError as exc:
            log(
                f"  AVISO GRAVE: no pude destruir la instancia {iid}: {exc}\n"
                f"  SIGUE FACTURANDO. Destruyela a mano ya:\n"
                f"    python scripts/vast_instance.py destroy {iid} --yes"
            )


def cmd_bench(args: argparse.Namespace) -> None:
    """Alquila UNA maquina del nivel pedido, mide y la destruye."""
    bench = load_bench(args.bench)
    tope = args.max_price or limite_precio()
    oferta = elegir_oferta(args.cpus, args.max_cpus, args.min_ram, tope)

    if args.dry_run:
        log(cabecera_ofertas())
        log(oferta_fila(oferta))
        log("\n--dry-run: no se alquila nada.")
        return

    tar_path = construir_tar(bench["envia"])
    resultado = medir_en_oferta(
        bench, oferta, tar_path, f"bench-{args.cpus or 'x'}vcpu"
    )
    path = guardar_resultado(bench, resultado)
    tabla = escribir_tabla(bench)
    log(f"\nResultado: {resultado['metrica']} ({bench.get('unidad', '')})")
    log(f"Guardado en {path.relative_to(ROOT)}")
    if tabla:
        log(f"Tabla actualizada: {tabla.relative_to(ROOT)}")


def cmd_sweep(args: argparse.Namespace) -> None:
    """El barrido: una maquina por nivel de CPU, medida y destruida.

    Los niveles se piden por numero de vCPU. Cada uno se busca en el rango
    [n, 2n) para que dos niveles seguidos no acaben en la misma maquina: pedir
    solo `>= n` devuelve la mas barata, que a menudo tiene muchos mas nucleos de
    los pedidos, y entonces el barrido mide tres veces lo mismo sin decirlo.
    """
    bench = load_bench(args.bench)
    tope = args.max_price or limite_precio()
    try:
        niveles = [int(n) for n in args.cpus.split(",") if n.strip()]
    except ValueError:
        die(f"--cpus quiere numeros separados por coma, no {args.cpus!r}")
    if not niveles:
        die("--cpus vacio: dime al menos un nivel, p.ej. --cpus 2,4,8")

    log(f"Barrido '{bench['name']}' sobre {len(niveles)} niveles: {niveles}")
    log(f"Tope de precio: {tope:.2f} $/h por maquina.\n")

    elegidas = []
    for n in niveles:
        ofertas = buscar_ofertas(cpus=n, max_cpus=n * 2, min_ram_gb=args.min_ram,
                                 max_price=tope)
        if not ofertas:
            log(f"  nivel {n:>3} vCPU: sin ofertas en [{n}, {n * 2}) -- se salta")
            continue
        elegidas.append((n, ofertas[0]))
        log(f"  nivel {n:>3} vCPU: oferta {ofertas[0]['id']}, "
            f"{ofertas[0]['cpu_cores_effective']:.1f} vCPU, "
            f"{ofertas[0]['dph_total']:.4f} $/h")

    if not elegidas:
        die("Ningun nivel tenia oferta. Sube --max-price o cambia los niveles.")

    coste_max = sum(float(o["dph_total"]) for _, o in elegidas) * (args.horas_max)
    log(
        f"\n{len(elegidas)} maquinas. Coste maximo si cada una viviera "
        f"{args.horas_max:g} h: {coste_max:.2f} $."
    )
    if args.dry_run:
        log("--dry-run: no se alquila nada.")
        return
    if not args.yes and not confirmar("¿Lanzo el barrido?"):
        log("Cancelado. No se ha alquilado nada.")
        return

    tar_path = construir_tar(bench["envia"])
    log(f"Payload listo: {tar_path.stat().st_size / 1e6:.1f} MB")

    hechos, fallados = [], []
    for n, oferta in elegidas:
        try:
            resultado = medir_en_oferta(bench, oferta, tar_path, f"sweep-{n}vcpu")
        except (ApiError, RuntimeError, subprocess.TimeoutExpired) as exc:
            log(f"  FALLO en el nivel {n} vCPU: {exc}")
            fallados.append((n, str(exc)))
            continue
        path = guardar_resultado(bench, resultado)
        hechos.append(resultado)
        log(f"  -> {resultado['metrica']} {bench.get('unidad', '')}  "
            f"({path.relative_to(ROOT)})")

    tabla = escribir_tabla(bench)
    log("\n" + "=" * 62)
    log(f"  Medidas buenas: {len(hechos)}   fallidas: {len(fallados)}")
    for n, motivo in fallados:
        log(f"    {n} vCPU: {motivo[:80]}")
    gasto = sum(float(r.get("usd_medida") or 0) for r in hechos)
    log(f"  Gastado: {gasto:.4f} $")
    if tabla:
        log(f"  Tabla: {tabla.relative_to(ROOT)}")
    log("  Comprueba que no queda nada vivo:  python scripts/vast_instance.py list")
    log("=" * 62)


# ------------------------------------------------------------------------ parser


def main() -> None:
    force_utf8_output()
    load_env()
    parser = argparse.ArgumentParser(
        prog="vast_instance.py",
        description="Maquinas efimeras en Vast.ai para medir velocidad.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("offers", help="catalogo de maquinas alquilables")
    p.add_argument("--cpus", type=int, default=0, help="vCPU efectivas minimas")
    p.add_argument("--max-cpus", type=int, default=0, help="vCPU efectivas maximas")
    p.add_argument("--min-ram", type=float, default=0.0, metavar="GB")
    p.add_argument("--max-price", type=float, default=0.0, metavar="USD_HORA")
    p.add_argument("--limit", type=int, default=20, help="cuantas filas imprimir")
    p.add_argument(
        "--by-cpus",
        action="store_true",
        help="ordena por vCPU en vez de por precio",
    )
    p.set_defaults(func=cmd_offers)

    p = sub.add_parser("keys", help="claves SSH registradas en la cuenta de Vast.ai")
    p.set_defaults(func=cmd_keys)

    p = sub.add_parser(
        "register-key",
        help="sube la clave publica de esta maquina a Vast.ai (la crea si no hay)",
    )
    p.add_argument("file", nargs="?")
    p.add_argument("--comment", default="")
    p.set_defaults(func=cmd_register_key)

    p = sub.add_parser("launch", help="alquila una maquina y espera a que sea usable")
    p.add_argument("label", help="etiqueta con la que la reconoceras")
    p.add_argument("--cpus", type=int, default=0, help="vCPU efectivas minimas")
    p.add_argument("--max-cpus", type=int, default=0)
    p.add_argument("--min-ram", type=float, default=0.0, metavar="GB")
    p.add_argument("--offer", help="alquilar esta oferta concreta, sin buscar")
    p.add_argument("--image", help=f"imagen Docker (por defecto {DEFAULTS['VAST_IMAGE']})")
    p.add_argument("--disk", type=float, default=0.0, metavar="GB")
    p.add_argument("--max-price", type=float, default=0.0, metavar="USD_HORA")
    p.add_argument("--dry-run", action="store_true", help="ensena la peticion sin enviarla")
    p.set_defaults(func=cmd_launch)

    p = sub.add_parser("list", help="que hay vivo y cuanto cuesta tenerlo asi")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("ssh", help="entra en una instancia (o ejecuta un comando)")
    p.add_argument("label")
    p.add_argument("--cmd", help="comando a ejecutar en vez de abrir sesion")
    p.set_defaults(func=cmd_ssh)

    p = sub.add_parser("destroy", help="destruye una instancia (deja de facturar)")
    p.add_argument("label", nargs="?")
    p.add_argument("--all", action="store_true", help="todas las vivas")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_destroy)

    p = sub.add_parser("benchs", help="benchmarks disponibles en benchmarks/")
    p.set_defaults(func=cmd_benchs)

    p = sub.add_parser(
        "bench", help="alquila UNA maquina, corre el benchmark y la destruye"
    )
    p.add_argument("--bench", required=True, help="nombre de un fichero de benchmarks/")
    p.add_argument("--cpus", type=int, default=0)
    p.add_argument("--max-cpus", type=int, default=0)
    p.add_argument("--min-ram", type=float, default=0.0, metavar="GB")
    p.add_argument("--max-price", type=float, default=0.0, metavar="USD_HORA")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser(
        "sweep", help="una maquina por nivel de CPU: mide, guarda y destruye"
    )
    p.add_argument("--bench", required=True)
    p.add_argument(
        "--cpus",
        default="2,4,8,16",
        help="niveles de vCPU separados por coma (por defecto 2,4,8,16)",
    )
    p.add_argument("--min-ram", type=float, default=0.0, metavar="GB")
    p.add_argument("--max-price", type=float, default=0.0, metavar="USD_HORA")
    p.add_argument(
        "--horas-max",
        type=float,
        default=1.0,
        help="solo para estimar el coste maximo antes de empezar",
    )
    p.add_argument("--yes", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_sweep)

    args = parser.parse_args()
    try:
        args.func(args)
    except ApiError as exc:
        die(str(exc))
    except KeyboardInterrupt:
        die(
            "Cortado a mano. OJO: si habia una instancia alquilada puede seguir "
            "viva.\n  Compruebalo:  python scripts/vast_instance.py list"
        )


if __name__ == "__main__":
    main()
