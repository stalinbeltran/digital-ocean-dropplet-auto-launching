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
    # Tipo de máquina por defecto: nombre de un descriptor de types/, que fija
    # de una vez plan, imagen, región y plantilla de arranque. Vacío = se usan
    # las variables sueltas de aquí arriba, como siempre.
    "DO_TYPE": "",
    # Freno de mano contra un lanzamiento caro por error. Un plan con GPU cuesta
    # de 565 a 3.281 dólares al mes (hasta 137 veces el droplet de trabajo), y
    # desde el móvil un tipo mal escrito se manda igual de rápido que el bueno.
    # Por encima de este precio mensual, `launch` se niega y pide --accept-cost.
    # 0 = sin freno.
    "DO_MAX_PRICE_MONTHLY": "100",
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
    # Volumen de bloques: el único almacenamiento de la cuenta que sobrevive a
    # su droplet. Vacío = no se usa ninguno. 10 GB es el escalón cómodo para el
    # dataset del benchmark (unos 300 MB con todo) y cuesta 1 $/mes.
    "DO_VOLUME": "",
    "DO_VOLUME_SIZE_GB": "10",
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


def token_opcional() -> str:
    return (
        os.environ.get("DO_TOKEN")
        or os.environ.get("DIGITALOCEAN_TOKEN")
        or os.environ.get("DIGITALOCEAN_ACCESS_TOKEN")
        or ""
    )


def token() -> str:
    tok = token_opcional()
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
            # El cupo de GPU es el único límite que no se puede comprobar antes:
            # no está en /v2/sizes (el plan sale disponible y con regiones) ni en
            # /v2/account (que sólo trae droplet_limit). Sólo aparece aquí, y el
            # mensaje en crudo no dice qué hacer.
            pista = ""
            if "gpu limit" in detail.lower():
                pista = (
                    "\n\nTu cuenta no tiene cupo de GPU.\n"
                    "  No es el slug ni la región: la API acepta el plan y rechaza la\n"
                    "  creación. El cupo se pide en el panel de DigitalOcean (Account ->\n"
                    "  Limits, o por soporte) y NO se puede consultar por la API, así que\n"
                    "  no hay forma de avisarte antes de intentarlo.\n"
                    "  No se ha creado ningún droplet y no se te ha facturado nada."
                )
            die(f"HTTP {exc.code} en {method} {url}\n{detail}{pista}")
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


# ------------------------------------------------------------- tipos de máquina


TYPES_DIR = ROOT / "types"


def load_type(name: str) -> dict:
    """Lee types/<nombre>.json: un tipo de máquina con nombre.

    Elegir máquina no es elegir un `size`: una GPU necesita ADEMÁS su imagen con
    los drivers puestos (`gpu-h100x1-base`) y una región donde haya GPUs. Pedir
    el plan a secas te da una máquina cara sin drivers, o un 422 según el día.
    El tipo agrupa esa combinación bajo un nombre que sí se puede escribir de
    memoria desde el móvil.

    Es DATO, no código, como services/: añadir un tipo es añadir un fichero,
    nunca tocar este script. Nada de lo que hay aquí se valida contra una lista
    cableada; el plan se comprueba en el momento del lanzamiento contra
    /v2/sizes, que es la única fuente de verdad sobre qué existe y qué cuesta.
    """
    path = TYPES_DIR / f"{name}.json"
    if not path.exists():
        disponibles = ", ".join(sorted(p.stem for p in TYPES_DIR.glob("*.json")))
        die(
            f"No existe el tipo '{name}' (falta {path}).\n"
            f"  Definidos: {disponibles or 'ninguno'}\n"
            "  Míralos con: python scripts/do_droplet.py types"
        )
    try:
        tipo = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"{path} no es JSON válido: {exc}")
    if not tipo.get("size"):
        die(f"{path}: falta el campo obligatorio 'size'.")
    tipo["name"] = name
    return tipo


def all_types() -> list[dict]:
    if not TYPES_DIR.exists():
        return []
    return [load_type(p.stem) for p in sorted(TYPES_DIR.glob("*.json"))]


# ----------------------------------------------------------------- descubrimiento


def sizes_index(opcional: bool = False) -> dict[str, dict]:
    """Todos los planes de /v2/sizes indexados por slug.

    Con `opcional`, quedarse sin índice no aborta el comando: se usa para
    ponerle precio a listados que valen igual sin él (`types`, `list`). Sin
    token no se intenta siquiera, que es el caso de mirar los tipos definidos en
    una máquina que no lanza nada.
    """
    if opcional and not token_opcional():
        return {}
    try:
        return {s["slug"]: s for s in paged("/v2/sizes", "sizes")}
    except SystemExit:
        if not opcional:
            raise
        return {}


def precio_mes(size: dict) -> float:
    """Precio mensual del plan, en dólares, tal y como lo publica la API.

    **No lo calcules multiplicando el precio por hora**: no hay una constante
    que valga. Medido contra la API el 2026-08-16, DigitalOcean usa 672 h para
    la gama básica (24 $/mes = 0,035714 $/h) y 744 h para las de GPU
    (3.281,04 $/mes = 4,41 $/h). Con 730 h, que es lo que dice su propia página
    de precios, la suma de dos droplets daba 30,41 $ donde eran 28,00 $.

    El factor sólo se usa para un plan que no publique mensual, caso que hoy no
    se da en ninguno; ahí es una estimación y punto.
    """
    mensual = size.get("price_monthly") or 0
    if mensual:
        return float(mensual)
    return float(size.get("price_hourly") or 0) * 730


def precio_hora(size: dict) -> float:
    return float(size.get("price_hourly") or 0)


def gpu_desc(size: dict) -> str:
    """'1x nvidia h100 80 GiB', o cadena vacía si el plan no lleva GPU."""
    gpu = size.get("gpu_info") or {}
    if not gpu:
        return ""
    modelo = str(gpu.get("model") or "gpu").replace("_", " ")
    trozos = [f"{gpu.get('count', 1)}x {modelo}"]
    vram = gpu.get("vram") or {}
    if vram.get("amount"):
        # La API contesta las unidades en minúsculas ("gib"). En mayúsculas del
        # todo ("GIB") se leen peor que escritas como se escriben.
        bruta = str(vram.get("unit") or "")
        unidad = {"gib": "GiB", "mib": "MiB", "tib": "TiB"}.get(bruta.lower(), bruta.upper())
        trozos.append(f"{vram['amount']} {unidad}")
    return " ".join(trozos)


def size_resumen(size: dict) -> str:
    """Una línea con lo que cuesta y lo que trae, para logs y avisos."""
    gpu = gpu_desc(size)
    return (
        f"{size['slug']} · ${precio_mes(size):,.2f}/mes (${precio_hora(size):.4f}/h)"
        f" · {size['vcpus']} vCPU · {size['memory'] / 1024:g} GB RAM"
        f" · {size['disk']} GB" + (f" · {gpu}" if gpu else "")
    )


SIZES_HEADER = f"{'SLUG':<24} {'vCPU':>4} {'RAM':>8} {'DISCO':>8} {'$/MES':>10} {'$/HORA':>9}"


def size_fila(size: dict) -> str:
    return (
        f"{size['slug']:<24} {size['vcpus']:>4} {size['memory'] / 1024:>5g} GB "
        f"{size['disk']:>5} GB {precio_mes(size):>10,.2f} {precio_hora(size):>9.4f}"
    )


def cmd_sizes(args: argparse.Namespace) -> None:
    """El catálogo de planes de DigitalOcean, con precio. De aquí salen los tipos.

    Dos cosas que costaron un rato entender y que este comando ya no esconde:

    - **Las GPU no están en todas las regiones.** Filtrar por la región del .env
      (nyc1 por defecto) las escondía todas, y la conclusión fácil era "mi cuenta
      no tiene GPU". Por eso --gpu mira todas las regiones salvo que se pida una,
      y la línea de detalle dice en cuáles hay.
    - **Un plan no disponible no es lo mismo que inexistente.** Se ocultaban
      igual, así que --all los muestra marcados: si el que buscas sale como no
      disponible, el problema es tu cuenta o esa región, no el nombre.

    Siempre se imprime qué filtros están puestos: un listado corto por un filtro
    olvidado se lee igual que "no hay nada", y son cosas muy distintas.
    """
    # Una región explícita manda siempre. Sin ella, --gpu y --all-regions miran
    # todas: es justo el caso en que filtrar por la del .env engaña.
    region = args.region or ("" if (args.all_regions or args.gpu) else cfg("DO_REGION"))
    detalle = bool(args.gpu or args.all_regions or not region)

    filtros = [f"región {region}" if region else "todas las regiones"]
    if args.gpu:
        filtros.append("sólo con GPU")
    if args.filter:
        filtros.append(f"slug contiene '{args.filter}'")
    if args.min_memory:
        filtros.append(f"RAM >= {args.min_memory} MB")
    if args.max_price:
        filtros.append(f"hasta ${args.max_price:,.2f}/mes")
    if args.all:
        filtros.append("incluidos los no disponibles")

    log("Planes de DigitalOcean (" + "; ".join(filtros) + "):\n")
    log(SIZES_HEADER)

    mostrados = 0
    for size in sorted(paged("/v2/sizes", "sizes"), key=precio_mes):
        if not size.get("available") and not args.all:
            continue
        if region and region not in size.get("regions", []):
            continue
        if args.gpu and not size.get("gpu_info"):
            continue
        if args.filter and args.filter.lower() not in size["slug"].lower():
            continue
        if size["memory"] < args.min_memory:
            continue
        if args.max_price and precio_mes(size) > args.max_price:
            continue

        log(size_fila(size))
        mostrados += 1
        if detalle:
            # Un plan sin regiones se leía como una línea a medias, y es justo el
            # caso que hay que ver: existe, tu cuenta lo tiene, y aun así no se
            # puede lanzar en ningún sitio. Pasa de verdad y con varias GPU.
            regiones = ", ".join(size.get("regions", [])) or "SIN CAPACIDAD en ninguna región"
            partes = [p for p in (gpu_desc(size), regiones) if p]
            if not size.get("available"):
                partes.insert(0, "NO DISPONIBLE en tu cuenta")
            log("  " + " · ".join(partes))

    log(f"\n{mostrados} planes.")
    if not mostrados:
        log(
            "  Ninguno pasa esos filtros. Prueba a quitarlos:\n"
            "    sizes --all-regions --min-memory 0 --all"
        )
    if args.gpu and not mostrados:
        log(
            "  Si no sale ninguna GPU ni con --all, tu cuenta aún no tiene acceso\n"
            "  a GPU Droplets: hay que pedirlo desde el panel de DigitalOcean."
        )
    if mostrados and args.gpu:
        log(
            "  La imagen normal de Ubuntu NO trae drivers: para GPU hay que\n"
            "  lanzar con la imagen 'gpu-h100x1-base' (o el tipo ya hecho de\n"
            "  types/, que la lleva puesta). Míralas con: images --kind all --filter gpu"
        )


def cmd_types(args: argparse.Namespace) -> None:
    """Los tipos con nombre de types/, con su precio traído en vivo.

    El precio no se guarda en el descriptor a propósito: un número copiado a
    mano envejece sin avisar, y aquí un número viejo se traduce en dinero.
    """
    tipos = all_types()
    if not tipos:
        log(f"No hay ningún tipo definido en {TYPES_DIR}.")
        return

    def efectivo(tipo: dict, campo: str, variable: str) -> str:
        """Lo que acabaría usando el lanzador, marcando lo que no fija el tipo."""
        return tipo.get(campo) or f"{cfg(variable)} (de .env)"

    index = sizes_index(opcional=True)
    log("Tipos definidos en types/ (precio en vivo de /v2/sizes):\n")
    for tipo in tipos:
        size = index.get(tipo["size"])
        precio = f"${precio_mes(size):,.2f}/mes (${precio_hora(size):.4f}/h)" if size else ""
        marca = " (por defecto)" if tipo["name"] == cfg("DO_TYPE") else ""
        log(f"{tipo['name']}{marca}  ·  {tipo['size']}" + (f"  ·  {precio}" if precio else ""))
        if tipo.get("descripcion"):
            log(f"  {tipo['descripcion']}")
        log(
            f"  imagen {efectivo(tipo, 'image', 'DO_IMAGE')}"
            f" · región {efectivo(tipo, 'region', 'DO_REGION')}"
            f" · tag {efectivo(tipo, 'tag', 'DO_TAG')}"
            f" · arranque {efectivo(tipo, 'cloud_init', 'DO_CLOUD_INIT')}"
        )
        if size and gpu_desc(size):
            log(f"  {gpu_desc(size)} · regiones con este plan: {', '.join(size['regions'])}")
        if tipo.get("notas"):
            log(f"  Ojo: {tipo['notas']}")
        log("")

    if not index:
        log("(sin precios: no hay token de DigitalOcean o la API no respondió)\n")
    log("Se usa con:  launch <nombre-droplet> --type <tipo>")
    log("Y el catálogo completo de planes está en:  sizes  /  sizes --gpu")


def cmd_regions(args: argparse.Namespace) -> None:
    for region in paged("/v2/regions", "regions"):
        if region["available"]:
            log(f"{region['slug']:<8} {region['name']}")


def cmd_images(args: argparse.Namespace) -> None:
    """Imágenes de arranque.

    Por defecto sólo las distribuciones, que es lo que se quiere el 99% de las
    veces. Las de GPU con drivers NO son distribuciones y por eso no salían
    aquí: hay que pedir --kind all.
    """
    path = "/v2/images" if args.kind == "all" else f"/v2/images?type={args.kind}"
    for image in paged(path, "images"):
        if args.filter.lower() in (image.get("slug") or "").lower():
            log(f"{image['slug']:<28} {image.get('distribution', '')} {image['name']}")


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


def resolver_maquina(args: argparse.Namespace) -> dict:
    """Decide plan, imagen, región, arranque y tag combinando las tres fuentes.

    Manda lo más explícito: una opción de la línea de comandos por encima del
    tipo, y el tipo por encima del .env. Así `--type gpu-h100 --region tor1`
    hace lo que parece, sin tener que editar el descriptor para un lanzamiento
    suelto.
    """
    nombre = args.type or cfg("DO_TYPE")
    tipo = load_type(nombre) if nombre else {}
    return {
        "tipo": tipo,
        "size": args.size or tipo.get("size") or cfg("DO_SIZE"),
        "image": args.image or tipo.get("image") or cfg("DO_IMAGE"),
        "region": args.region or tipo.get("region") or cfg("DO_REGION"),
        "cloud_init": args.cloud_init or tipo.get("cloud_init") or cfg("DO_CLOUD_INIT"),
        "tag": args.tag or tipo.get("tag") or cfg("DO_TAG"),
    }


def limite_precio() -> float:
    bruto = cfg("DO_MAX_PRICE_MONTHLY").strip()
    if not bruto:
        return 0.0
    try:
        return float(bruto)
    except ValueError:
        die(f"DO_MAX_PRICE_MONTHLY tiene que ser un número de dólares al mes, no '{bruto}'.")


def comprobar_size(slug: str, region: str, aceptar_coste: bool) -> dict:
    """Valida el plan contra /v2/sizes y frena los lanzamientos caros.

    Una llamada de lectura antes de gastar nada. Convierte un 422 de la API -o,
    peor, una máquina de GPU facturando a 4,42 $/h en la región equivocada- en un
    mensaje que dice qué pasa y qué escribir. Que `available` sea falso no es lo
    mismo que no existir: con las GPU casi siempre significa que falta pedir
    acceso, y esos dos casos se confundían en un mismo silencio.
    """
    index = sizes_index()
    size = index.get(slug)
    if not size:
        parecidos = sorted(s for s in index if s.startswith(slug.split("-")[0] + "-"))[:8]
        die(
            f"El plan '{slug}' no aparece en /v2/sizes.\n"
            + (f"  Parecidos: {', '.join(parecidos)}\n" if parecidos else "")
            + "  Míralos con: python scripts/do_droplet.py sizes --all-regions\n"
            "  Los planes por contrato no se publican ahí: para ésos, --no-check."
        )
    if not size.get("available"):
        die(
            f"El plan '{slug}' existe pero no está disponible para tu cuenta.\n"
            "  Con las GPU suele ser que falta pedir el acceso en el panel de\n"
            "  DigitalOcean; no es que el nombre esté mal."
        )
    if not size.get("regions"):
        # Caso real y desconcertante: `available` es true y aun así no hay dónde
        # crearlo. Es capacidad, no permisos. Le pasa hoy a varios planes de GPU.
        die(
            f"El plan '{slug}' existe y tu cuenta lo tiene, pero ahora mismo NO\n"
            "  se ofrece en ninguna región: no hay dónde crearlo.\n"
            "  Es falta de capacidad, no de permisos, y cambia con el tiempo.\n"
            "  Mira qué alternativa hay hoy con: do_droplet.py sizes --gpu"
        )
    if region not in size["regions"]:
        die(
            f"El plan '{slug}' no existe en la región '{region}'.\n"
            f"  Sí lo hay en: {', '.join(size['regions'])}\n"
            "  Repite el comando con --region <una de ésas>.\n"
            "  (Las GPU sólo están en unas pocas: por eso no salen filtrando por\n"
            "   la región del .env.)"
        )
    limite = limite_precio()
    if limite and precio_mes(size) > limite and not aceptar_coste:
        die(
            f"Freno de coste: {size_resumen(size)}\n"
            f"  Pasa del límite DO_MAX_PRICE_MONTHLY = ${limite:,.2f}/mes.\n"
            "  Si es justo lo que quieres, repite el comando con --accept-cost.\n"
            "  Factura desde que el droplet existe, no desde que lo usas, y sólo\n"
            "  se corta destruyéndolo:  destroy <nombre> --yes"
        )
    return size


def cmd_launch(args: argparse.Namespace) -> None:
    name = args.name or cfg("DO_DROPLET_NAME")
    if find_droplets(name=name):
        die(f"Ya existe un droplet llamado '{name}'. Usa otro nombre o destrúyelo primero.")

    maquina = resolver_maquina(args)
    size = None if args.no_check else comprobar_size(
        maquina["size"], maquina["region"], args.accept_cost
    )

    keys = selected_keys()

    # El volumen se comprueba antes de crear el droplet: si la región no cuadra
    # o el nombre está mal escrito, el fallo tiene que salir gratis y no
    # dejarte una máquina facturando sin el disco que ibas a usar.
    vol_name = args.volume or cfg("DO_VOLUME")
    vol = None
    if vol_name:
        vol = find_volume(vol_name)
        if not vol:
            die(
                f"No existe el volumen '{vol_name}'. No se ha creado ningún droplet.\n"
                f"  Créalo con:  python scripts/do_droplet.py volume create {vol_name}\n"
                "  o míralos con: python scripts/do_droplet.py volume list"
            )
        if vol["region"]["slug"] != maquina["region"]:
            die(
                f"El volumen '{vol_name}' está en {vol['region']['slug']} y el droplet "
                f"iría a {maquina['region']}.\n"
                "  Un volumen no se mueve de región. Lanza con "
                f"--region {vol['region']['slug']}, o usa otro volumen.\n"
                "  No se ha creado ningún droplet."
            )
        if vol.get("droplet_ids"):
            die(
                f"El volumen '{vol_name}' ya está conectado al droplet "
                f"{vol['droplet_ids'][0]}.\n"
                "  Un volumen sólo va en una máquina a la vez. Desconéctalo antes\n"
                f"  (volume detach {vol_name}) o copia el dato por SSH desde ella.\n"
                "  No se ha creado ningún droplet."
            )

    body = {
        "name": name,
        "region": maquina["region"],
        "size": maquina["size"],
        "image": maquina["image"],
        "ssh_keys": [k["id"] for k in keys],
        # El tag decide qué se barre con `destroy --tag`. Una máquina de control
        # no puede llevar el de los efímeros: se la llevaría por delante.
        "tags": [maquina["tag"]],
        "monitoring": True,
        "ipv6": True,
    }
    if vol:
        # Conectado desde la creación: así el disco ya está ahí cuando el
        # droplet arranca, y sólo queda montarlo al aprovisionar.
        body["volumes"] = [vol["id"]]
    user_data = build_user_data(keys, maquina["cloud_init"])
    if user_data:
        body["user_data"] = user_data

    tipo = maquina["tipo"]
    log(
        f"Creando '{name}'" + (f" (tipo {tipo['name']})" if tipo else "") + ":"
        f" {body['size']} · {body['image']} · {body['region']}"
        f" · tag {body['tags'][0]} · {maquina['cloud_init']}"
    )
    # El precio, antes de crear nada y en las dos unidades: la mensual es la que
    # se entiende, la horaria la que de verdad pagas por una máquina efímera.
    if size:
        log(f"Coste: ${precio_mes(size):,.2f}/mes (${precio_hora(size):.4f}/h) mientras exista.")
    if tipo.get("notas"):
        log(f"Ojo: {tipo['notas']}")
    log(f"Claves SSH autorizadas: {', '.join(k['name'] for k in keys)}")
    if vol:
        log(
            f"Volumen: '{vol['name']}' ({vol['size_gigabytes']} GB) se montará en "
            f"{volume_mount_point(vol['name'])}"
        )
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
                make_launcher=args.make_launcher,
                skip_wait=False,
            )
        )

    if vol and port:
        log(f"\nMontando el volumen '{vol['name']}'…")
        if run_remote_script(ip, port, build_mount_script(vol["name"], cfg("DO_DEV_USER"))) != 0:
            log(
                "  AVISO: el volumen está conectado pero no se pudo montar.\n"
                f"  Reintenta con: python scripts/do_droplet.py volume attach {vol['name']}"
                f" --droplet {name}"
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
    """Qué hay vivo y cuánto cuesta tenerlo así.

    El precio no es decoración: un droplet olvidado se paga por segundo, y con
    los planes de GPU un despiste vale 4,42 $/h. Ver el total al pie es lo que
    convierte 'tengo tres máquinas' en 'estoy gastando esto'.
    """
    droplets = find_droplets(tag=args.tag or "")
    if not droplets:
        log("No hay droplets.")
        return

    index = sizes_index(opcional=True)
    log(f"{'ID':<11} {'NOMBRE':<20} {'ESTADO':<8} {'TAMAÑO':<20} {'$/MES':>9}  IP")
    total_hora = total_mes = 0.0
    for d in droplets:
        size = index.get(d["size_slug"])
        # Los dos totales se suman de lo que publica la API para cada plan. El
        # mensual NO sale de multiplicar el horario: no hay factor único (672 h
        # en la gama básica, 744 en las de GPU) y con uno inventado la suma sale
        # mal -daba 30,41 $ donde eran 28,00 $-.
        total_hora += precio_hora(size) if size else 0.0
        total_mes += precio_mes(size) if size else 0.0
        precio = f"{precio_mes(size):>9,.2f}" if size else f"{'?':>9}"
        log(
            f"{d['id']:<11} {d['name']:<20} {d['status']:<8} "
            f"{d['size_slug']:<20} {precio}  {public_ip(d) or '-'}"
        )
    if total_mes:
        log(f"\nGastando ahora: ${total_hora:.4f}/h  ·  ${total_mes:,.2f}/mes en total.")
        log("Se corta destruyéndolos, no apagándolos:  destroy <nombre> --yes")


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


# ------------------------------------------------------------------- volúmenes
#
# Un volumen es la única cosa de esta cuenta que NO es efímera. Los droplets se
# rehacen sin aviso y con ellos se va su disco; el volumen sobrevive a su
# droplet, y por eso es donde va lo que cuesta caro reconstruir: aquí, el
# dataset del benchmark (mil imágenes renderizadas con Chromium, ver
# docs/benchmark-vcpu.md). Regenerarlo es reproducible pero lento; recuperarlo
# de un volumen es un `mount`.
#
# Un volumen se conecta a UN droplet a la vez -no es un disco compartido-, así
# que el reparto a varias máquinas de medición no se hace conectándolo a todas,
# sino copiando desde la que lo tiene. Que es además lo que se quiere para
# medir: el benchmark debe leer de disco local, no de la red.


def volumes(region: str = "") -> list[dict]:
    path = f"/v2/volumes?region={region}" if region else "/v2/volumes"
    return paged(path, "volumes")


def find_volume(name: str, region: str = "") -> dict | None:
    return next((v for v in volumes(region) if v["name"] == name), None)


def volume_device(name: str) -> str:
    """Ruta estable del disco dentro del droplet.

    DigitalOcean expone cada volumen por su nombre bajo /dev/disk/by-id. El
    /dev/sda de turno depende del orden en que se conectaron los discos y
    cambia entre arranques: en fstab pondría a la máquina a arrancar contra el
    disco equivocado, o a no arrancar.
    """
    return f"/dev/disk/by-id/scsi-0DO_Volume_{name}"


def volume_mount_point(name: str) -> str:
    return f"/mnt/{name}"


def build_mount_script(vol_name: str, dev_user: str) -> str:
    """Formatea (sólo si hace falta), monta y deja el volumen en fstab.

    El `mkfs` va condicionado a que el disco no tenga ya sistema de ficheros:
    un volumen creado con filesystem_type=ext4 viene formateado, y volver a
    formatearlo borraría justo lo que se quiere conservar. `blkid` es quien
    decide, no una suposición sobre cómo se creó el volumen.
    """
    dev = volume_device(vol_name)
    mnt = volume_mount_point(vol_name)
    return "\n".join(
        [
            "set -eu",
            f"DEV={shq(dev)}",
            f"MNT={shq(mnt)}",
            f"DEV_USER={shq(dev_user)}",
            # El disco tarda un momento en aparecer tras el attach.
            'for i in 1 2 3 4 5 6 7 8 9 10; do',
            '  [ -e "$DEV" ] && break',
            '  sleep 3',
            'done',
            '[ -e "$DEV" ] || { echo "no aparece $DEV: ¿está conectado el volumen?" >&2; exit 1; }',
            'if [ -z "$(blkid -o value -s TYPE "$DEV" 2>/dev/null || true)" ]; then',
            '  echo "  volumen sin formato, creando ext4…"',
            '  mkfs.ext4 -F -L "$(basename "$DEV" | tail -c 17)" "$DEV"',
            "else",
            '  echo "  volumen ya formateado ($(blkid -o value -s TYPE "$DEV")), no se toca"',
            "fi",
            'mkdir -p "$MNT"',
            'if ! mountpoint -q "$MNT"; then mount -o discard,defaults,noatime "$DEV" "$MNT"; fi',
            # La entrada de fstab lleva nofail a propósito: si algún día se
            # arranca la máquina sin el volumen conectado, debe arrancar igual
            # y no quedarse en la consola de emergencia, donde no se entra por
            # SSH y el droplet sólo es una factura.
            'if ! grep -q "^$DEV" /etc/fstab; then',
            '  echo "$DEV $MNT ext4 defaults,nofail,discard,noatime 0 2" >> /etc/fstab',
            "fi",
            'chown "$DEV_USER:$DEV_USER" "$MNT"',
            'echo "  montado en $MNT ($(df -h "$MNT" | tail -1 | awk "{print \\$4}") libres)"',
        ]
    ) + "\n"


def cmd_volume(args: argparse.Namespace) -> None:
    accion = args.action

    if accion == "list":
        vols = volumes()
        if not vols:
            log("No hay volúmenes en la cuenta.")
            return
        total = 0.0
        for v in vols:
            gb = v["size_gigabytes"]
            total += gb * 0.10  # $0,10 por GB y mes, tarifa única de DO
            conectado = ", ".join(str(i) for i in v.get("droplet_ids") or []) or "suelto"
            log(f"{v['name']:<24} {gb:>5} GB  {v['region']['slug']:<6} → {conectado}")
        log(f"\nTotal: ${total:,.2f}/mes mientras existan (se pagan conectados o no).")
        return

    name = args.name or cfg("DO_VOLUME")
    if not name:
        die("Falta el nombre del volumen (o define DO_VOLUME en .env).")

    if accion == "create":
        region = args.region or cfg("DO_REGION")
        existente = find_volume(name)
        if existente:
            log(
                f"Ya existe '{name}' ({existente['size_gigabytes']} GB en "
                f"{existente['region']['slug']}), no se crea otro."
            )
            return
        size = args.size_gb or int(cfg("DO_VOLUME_SIZE_GB"))
        log(f"Creando volumen '{name}': {size} GB en {region}, ext4.")
        log(f"Coste: ${size * 0.10:,.2f}/mes mientras exista, esté conectado o no.")
        vol = api(
            "POST",
            "/v2/volumes",
            {
                "name": name,
                "region": region,
                "size_gigabytes": size,
                "filesystem_type": "ext4",
                "description": args.description or "dato que debe sobrevivir a los droplets",
            },
        )["volume"]
        log(f"Creado (id {vol['id']}). Conéctalo con: volume attach {name} --droplet <nombre>")
        return

    vol = find_volume(name)
    if not vol:
        die(f"No existe el volumen '{name}'. Míralos con: volume list")

    if accion == "attach":
        droplet_name = args.droplet or cfg("DO_DROPLET_NAME")
        droplet, ip, port = resolve_target(droplet_name, args.port or 0)
        if droplet["region"]["slug"] != vol["region"]["slug"]:
            die(
                f"El volumen está en {vol['region']['slug']} y el droplet en "
                f"{droplet['region']['slug']}.\n"
                "  Un volumen sólo se conecta a droplets de su misma región; no se mueve.\n"
                "  Lanza el droplet en la región del volumen (--region), o crea otro volumen."
            )
        if droplet["id"] in (vol.get("droplet_ids") or []):
            log(f"'{name}' ya está conectado a '{droplet['name']}'.")
        else:
            if vol.get("droplet_ids"):
                die(
                    f"'{name}' está conectado al droplet {vol['droplet_ids'][0]}.\n"
                    "  Un volumen no se comparte entre máquinas: desconéctalo primero\n"
                    f"  (volume detach {name}) o copia el dato por SSH desde la que lo tiene."
                )
            log(f"Conectando '{name}' a '{droplet['name']}'…")
            accion_api = api(
                "POST",
                f"/v2/volumes/{vol['id']}/actions",
                {"type": "attach", "droplet_id": droplet["id"], "region": vol["region"]["slug"]},
            )["action"]
            wait_for_action(accion_api["id"])
        if args.no_mount:
            log(f"Conectado. Sin montar (--no-mount): el disco es {volume_device(name)}")
            return
        # Conectar sin montar deja un disco que no ve nadie: el dato "está" y
        # ningún programa lo encuentra. Montar es parte de conectar.
        if run_remote_script(ip, port, build_mount_script(name, cfg("DO_DEV_USER"))) != 0:
            die("El volumen quedó conectado pero no se pudo montar. La salida de ssh está arriba.")
        log(f"Listo: {volume_mount_point(name)} en '{droplet['name']}'.")
        return

    if accion == "detach":
        if not vol.get("droplet_ids"):
            log(f"'{name}' no está conectado a nada.")
            return
        droplet_id = vol["droplet_ids"][0]
        # Desmontar antes de desconectar. Al revés se pierde lo que el kernel
        # tenga sin escribir, y el dato del volumen es justo lo que no se
        # quiere reconstruir.
        try:
            droplet = api("GET", f"/v2/droplets/{droplet_id}")["droplet"]
            ip = public_ip(droplet)
            port = wait_for_ssh(ip) if ip else 0
            if port:
                log(f"Desmontando en '{droplet['name']}'…")
                run_remote_script(
                    ip,
                    port,
                    f"umount {shq(volume_mount_point(name))} 2>/dev/null || true\n"
                    f"sed -i '\\|^{volume_device(name)} |d' /etc/fstab\n",
                )
        except SystemExit:
            log("  Aviso: no pude desmontar por SSH; se desconecta igualmente.")
        log(f"Desconectando '{name}' del droplet {droplet_id}…")
        accion_api = api(
            "POST",
            f"/v2/volumes/{vol['id']}/actions",
            {"type": "detach", "droplet_id": droplet_id, "region": vol["region"]["slug"]},
        )["action"]
        wait_for_action(accion_api["id"])
        log("Desconectado.")
        return

    if accion == "destroy":
        log(f"Se va a DESTRUIR el volumen '{name}' ({vol['size_gigabytes']} GB) y todo su")
        log("contenido. Esto es irreversible y el dato NO está en ningún otro sitio.")
        if not args.yes and not confirmar("\nEscribe 'si' para confirmar: "):
            log("Cancelado.")
            return
        if vol.get("droplet_ids"):
            die(
                f"'{name}' sigue conectado. Desconéctalo primero: volume detach {name}"
            )
        api("DELETE", f"/v2/volumes/{vol['id']}")
        log(f"Destruido '{name}'.")
        return

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


def run_remote_capture(ip: str, port: int, script: str) -> tuple[int, str]:
    """Como run_remote_script, pero devolviendo también lo que imprimió.

    Hace falta para el camino de ida y vuelta de `--make-launcher`: la clave
    privada se genera DENTRO del droplet y no sale de ahí nunca; lo que vuelve
    es la pública, que no es secreta, para registrarla en la cuenta.
    """
    proc = subprocess.run(
        ssh_command(ip, port, user="root") + ["bash -s"],
        input=script.encode("utf-8"),
        capture_output=True,
    )
    salida = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    return proc.returncode, salida


# Repo del propio lanzador. Una máquina que va a lanzar droplets lo necesita
# clonado: es el programa que sabe hablar con la API.
REPO_LANZADOR = "stalinbeltran/digital-ocean-dropplet-auto-launching"


def hacer_lanzador(name: str, ip: str, port: int) -> None:
    """Deja al droplet en condiciones de crear y usar otros droplets.

    El token por sí solo no basta, y esa es la parte que se olvida: con él la
    máquina puede CREAR droplets, pero no ENTRAR en ellos. Un droplet acepta las
    claves públicas que estén registradas en la cuenta en el momento de crearlo,
    así que la máquina lanzadora necesita un par propio y su pública registrada
    ANTES de lanzar nada. Sin esto se crean máquinas a las que su creador no
    puede conectarse: existen, facturan y no sirven.

    La privada se genera en el destino y no viaja: aquí sólo vuelve la pública.
    """
    dev_user = cfg("DO_DEV_USER")
    key_file = cfg("DO_SSH_KEY_FILE").replace("~", "$H", 1) if cfg(
        "DO_SSH_KEY_FILE"
    ).startswith("~") else "$H/.ssh/do_droplet"

    script = "\n".join(
        [
            "set -eu",
            f"DEV_USER={shq(dev_user)}",
            'H=$(getent passwd "$DEV_USER" | cut -d: -f6)',
            f'KEY="{key_file}"',
            'install -d -m 700 -o "$DEV_USER" -g "$DEV_USER" "$H/.ssh"',
            'if [ ! -f "$KEY" ]; then',
            '  sudo -u "$DEV_USER" -H ssh-keygen -t ed25519 -f "$KEY" -N "" '
            f'-C "lanzador-{name}" >/dev/null',
            "fi",
            'chown "$DEV_USER:$DEV_USER" "$KEY" "$KEY.pub"',
            'chmod 600 "$KEY"',
            "echo CLAVE_PUBLICA_INICIO",
            'cat "$KEY.pub"',
            "echo CLAVE_PUBLICA_FIN",
        ]
    )
    code, salida = run_remote_capture(ip, port, script)
    if code != 0:
        log(f"  AVISO: no pude crear el par de claves en el droplet.\n{salida.strip()}")
        return

    publica = ""
    dentro = False
    for linea in salida.splitlines():
        if linea.strip() == "CLAVE_PUBLICA_INICIO":
            dentro = True
        elif linea.strip() == "CLAVE_PUBLICA_FIN":
            dentro = False
        elif dentro and linea.strip():
            publica = linea.strip()
    if not publica:
        log("  AVISO: el droplet no devolvió ninguna clave pública. Sigue sin poder")
        log("         entrar en los droplets que lance.")
        return

    huella = publica.split()[1]
    for key in account_keys():
        if key["public_key"].split()[1] == huella:
            log(f"  clave del lanzador ya registrada en la cuenta como '{key['name']}'")
            return
    registrada = api(
        "POST", "/v2/account/keys", {"name": f"lanzador-{name}", "public_key": publica}
    )["ssh_key"]
    log(f"  clave del lanzador registrada en la cuenta: '{registrada['name']}'")
    log("  (los droplets que cree esta máquina la aceptarán; los creados ANTES, no)")


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
        # El script corre con umask 077 por los secretos, y así la unidad salía
        # en modo 600: systemd avisa de que es "world-inaccessible" y nadie que
        # no sea root puede leerla. Aquí no hay ningún secreto (los tokens están
        # en dev-secrets.env), y `update` necesita poder mirarla.
        f"  chmod 644 /etc/systemd/system/{unit}.service",
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


def bloque_cargar_secretos() -> list[str]:
    """Líneas de sh que hacen que dev-secrets.env se cargue en cada shell.

    Espera `$H` (home del usuario) y `$DEV_USER` ya puestos por quien las use, y
    es idempotente: se puede reejecutar sin duplicar nada.

    Vive aparte porque hacen falta en dos sitios -el aprovisionamiento completo y
    el empujón suelto del token- y no pueden divergir: si esta línea falta, el
    fichero de secretos existe pero no lo carga nadie, y el síntoma es un
    "falta el token" en una máquina donde el token sí está.
    """
    return [
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
    ]


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
        *bloque_cargar_secretos(),
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

    # Una máquina lanzadora necesita las tres cosas a la vez, y pedirlas por
    # separado es la forma de que falte una y no se note hasta que falla:
    # el token (para crear), el repo del lanzador (el programa que crea) y un
    # par de claves propio registrado en la cuenta (para poder entrar en lo que
    # cree). --make-launcher implica las tres.
    make_launcher = getattr(args, "make_launcher", False)
    if make_launcher:
        args.push_do_token = True
        if REPO_LANZADOR not in repos:
            repos.append(REPO_LANZADOR)
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

    if make_launcher:
        log("\nDejando la máquina en condiciones de lanzar droplets…")
        hacer_lanzador(name, ip, port)

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
    """Servicios instalados por `provision`, con el repo del que viven.

    Se le pregunta a systemd en vez de leer los ficheros de unidad: `provision`
    los escribe bajo `umask 077`, o sea en modo 600 de root, y esto suele correr
    como el usuario de desarrollo, que no puede abrirlos. Leyendo el fichero la
    lista salía vacía y el update terminaba con un "no hay nada que reiniciar"
    que era mentira: el servicio se quedaba con el código viejo.
    """
    code, salida = run_local(
        [
            "systemctl",
            "show",
            "--no-pager",
            "--property=Id",
            "--property=Description",
            "--property=WorkingDirectory",
            "*.service",
        ],
        timeout=60,
    )
    if code != 0:
        log(f"  AVISO: no pude preguntar a systemd por los servicios: {salida}")
        return []

    fuera = []
    for bloque in salida.split("\n\n"):
        campos = {}
        for linea in bloque.splitlines():
            clave, sep, valor = linea.partition("=")
            if sep:
                campos[clave.strip()] = valor.strip()
        if PROVISION_MARK not in campos.get("Description", ""):
            continue
        unit = campos.get("Id", "").removesuffix(".service")
        directorio = campos.get("WorkingDirectory", "")
        if unit:
            fuera.append((unit, Path(directorio) if directorio else None))
    return sorted(fuera)


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


def cmd_push_do_token(args: argparse.Namespace) -> None:
    """Da a un droplet ya creado el token de DigitalOcean, y sólo eso.

    Existe aparte de `provision --push-do-token` por una razón concreta y cara:
    `provision` reescribe `dev-secrets.env` ENTERO (`cat >`), a propósito, para
    que el fichero sea exactamente lo que diga el comando. Usarlo sólo para
    añadir el token borra del destino todo lo que el emisor no tenga a mano -el
    de Claude, el de GitHub-, y eso no se nota al momento: se nota cuando algo
    dentro de esa máquina deja de autenticar sin motivo aparente. Esto toca una
    línea y deja el resto del fichero como estaba.

    El token viaja por SSH y dentro del script que va por **stdin**, nunca como
    argumento: lo que va en la línea de comandos de ssh sale en el `ps` del
    destino, donde lo lee cualquier usuario sin privilegios.

    Repetir el comando ROTA el token: quita la línea anterior y pone la nueva.
    """
    if args.from_env == "DO_TOKEN":
        valor = token()  # acepta también DIGITALOCEAN_TOKEN y _ACCESS_TOKEN
    else:
        valor = os.environ.get(args.from_env, "").strip()
        if not valor:
            die(
                f"La variable '{args.from_env}' no tiene valor en esta máquina.\n"
                "  Es la que se iba a enviar como DO_TOKEN al destino."
            )

    dev_user = cfg("DO_DEV_USER")
    droplet, ip, port = resolve_target(args.name or "", args.port or 0)
    log(f"Enviando el token de DigitalOcean a '{droplet['name']}' ({ip}:{port}).")
    log("  AVISO: con este token, quien entre a esa máquina puede crear y")
    log("  destruir droplets en tu cuenta, es decir gastar dinero. Dáselo sólo")
    log("  a máquinas tuyas y destrúyelas cuando acabes.")

    script = "\n".join(
        [
            "set -eu",
            "umask 077",
            f"DEV_USER={shq(dev_user)}",
            'H=$(getent passwd "$DEV_USER" | cut -d: -f6)',
            '[ -n "$H" ] || { echo "no existe el usuario $DEV_USER" >&2; exit 1; }',
            'install -d -m 700 -o "$DEV_USER" -g "$DEV_USER" "$H/.config"',
            'F="$H/.config/dev-secrets.env"',
            'T="$F.nuevo"',
            "# Se copia el fichero SIN la línea del token y se le añade la nueva:",
            "# así esto sirve igual para ponerlo la primera vez que para rotarlo,",
            "# y ningún otro secreto del destino se toca.",
            'if [ -f "$F" ]; then grep -v "^export DO_TOKEN=" "$F" > "$T" || true;'
            ' else : > "$T"; fi',
            # Heredoc con el delimitador entrecomillado: nada de lo que haya en el
            # token se expande ni se interpreta, venga como venga.
            "cat >> \"$T\" <<'FIN_TOKEN'",
            f"export DO_TOKEN={shq(valor)}",
            "FIN_TOKEN",
            'mv "$T" "$F"',
            'chmod 600 "$F"',
            'chown "$DEV_USER:$DEV_USER" "$F"',
            "",
            *bloque_cargar_secretos(),
            "",
            'echo "DO_TOKEN escrito en $F (modo 600, dueño $DEV_USER)."',
        ]
    )

    if run_remote_script(ip, port, script) != 0:
        die("Falló el envío del token. La salida de ssh está justo arriba.")
    log("\nListo. En esa máquina, para comprobarlo sin sacar el token a pantalla:")
    log("  bash -lc 'python3 scripts/do_droplet.py list'")


def cmd_authorize_key(args: argparse.Namespace) -> None:
    """DENTRO de una máquina: autoriza una clave pública para entrar por SSH.

    Es la mitad que falta para que un droplet pueda entrar en otra máquina. La
    privada no viaja nunca -se queda donde se generó-, aquí sólo se apunta la
    pública, que no es secreta.

    Se ejecuta donde se quiere entrar, y por eso vale desde Telegram: el bot ya
    corre comandos en la máquina de control, que es justo el destino habitual.
    """
    linea = " ".join(args.clave).strip()
    tipos = ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-", "sk-ssh-", "sk-ecdsa-")
    partes = linea.split()
    if not linea.startswith(tipos) or len(partes) < 2:
        die(
            "Eso no parece una clave pública.\n"
            "  Se espera la línea entera, tal cual sale del fichero .pub:\n"
            "    ssh-ed25519 AAAAC3Nza... comentario"
        )

    path = Path.home() / ".ssh" / "authorized_keys"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    existentes = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    # Se compara el material de la clave, no la línea entera: el comentario del
    # final cambia entre máquinas y no distingue una clave de otra.
    for existente in existentes:
        trozos = existente.split()
        if len(trozos) >= 2 and trozos[1] == partes[1]:
            log(f"Esa clave ya estaba autorizada en {path}. No se toca nada.")
            return

    with path.open("a", encoding="utf-8") as fh:
        if existentes and existentes[-1].strip():
            fh.write("\n")
        fh.write(linea + "\n")
    path.chmod(0o600)
    comentario = " ".join(partes[2:]) or "(sin comentario)"
    log(f"Clave autorizada en {path}: {partes[0]} … {comentario}")
    log(f"Ahora esa máquina puede entrar aquí como {Path.home().name}.")


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

    p = sub.add_parser(
        "sizes", help="catálogo de planes con su precio mensual, GPU incluidas"
    )
    p.add_argument("--region", help="sólo los de esta región (por defecto, la de .env)")
    p.add_argument(
        "--all-regions",
        action="store_true",
        help="de todas las regiones, diciendo en cuáles hay cada plan",
    )
    p.add_argument(
        "--gpu",
        action="store_true",
        help="sólo planes con GPU. Mira todas las regiones salvo que pidas una: "
        "las GPU no están en la mayoría, y filtrando por la del .env no sale ninguna",
    )
    p.add_argument("--filter", default="", help="planes cuyo slug contenga este texto")
    p.add_argument(
        "--max-price", type=float, default=0.0, metavar="USD", help="precio mensual máximo"
    )
    p.add_argument(
        "--min-memory",
        type=int,
        default=4096,
        metavar="MB",
        help="RAM mínima (por defecto 4096, para no listar los planes diminutos)",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="incluye los que existen pero no están disponibles para tu cuenta",
    )
    p.set_defaults(func=cmd_sizes)

    p = sub.add_parser(
        "types", help="tipos de máquina con nombre (types/), con su precio en vivo"
    )
    p.set_defaults(func=cmd_types)

    p = sub.add_parser("regions", help="regiones disponibles")
    p.set_defaults(func=cmd_regions)

    p = sub.add_parser("images", help="imágenes de arranque")
    p.add_argument("--filter", default="ubuntu")
    p.add_argument(
        "--kind",
        choices=["distribution", "application", "all"],
        default="distribution",
        help="qué tipo de imagen listar. Las de GPU con drivers "
        "(gpu-h100x1-base) no son distribuciones: hacen falta --kind all",
    )
    p.set_defaults(func=cmd_images)

    p = sub.add_parser("launch", help="crea el droplet y espera a que esté usable")
    p.add_argument("name", nargs="?")
    p.add_argument(
        "--type",
        help="tipo de máquina de types/: fija de una vez plan, imagen, región y "
        "plantilla de arranque. Míralos con `types`. Cualquier opción de aquí "
        "abajo pisa lo que diga el tipo",
    )
    p.add_argument("--region")
    p.add_argument("--size")
    p.add_argument("--image")
    p.add_argument(
        "--accept-cost",
        action="store_true",
        help="lanza aunque el plan pase de DO_MAX_PRICE_MONTHLY. Hace falta para "
        "las GPU, que cuestan de 565 a 3.281 dólares al mes",
    )
    p.add_argument(
        "--no-check",
        action="store_true",
        help="no validar el plan contra /v2/sizes antes de crear. Sólo para los "
        "planes por contrato, que no se publican ahí",
    )
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
        "--volume",
        help="volumen de bloques que conectar y montar en /mnt/<nombre>. Tiene que "
        "existir ya (volume create) y estar en la misma región",
    )
    p.add_argument(
        "--make-launcher",
        action="store_true",
        help="deja la máquina en condiciones de lanzar y usar otros droplets: "
        "implica --push-do-token, clona el repo del lanzador y le crea un par de "
        "claves SSH propio registrado en la cuenta",
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
        "--make-launcher",
        action="store_true",
        help="deja la máquina en condiciones de lanzar y usar otros droplets: "
        "implica --push-do-token, clona el repo del lanzador y le crea un par de "
        "claves SSH propio registrado en la cuenta",
    )
    p.add_argument(
        "--skip-wait",
        action="store_true",
        help="no esperar al testigo de instalación de cloud-init",
    )
    p.set_defaults(func=cmd_provision)

    p = sub.add_parser(
        "push-do-token",
        help="da a un droplet ya creado el token de DigitalOcean, sin tocar sus "
        "demás secretos (a diferencia de provision, que reescribe el fichero)",
    )
    p.add_argument("name", nargs="?")
    p.add_argument("--port", type=int)
    p.add_argument(
        "--from-env",
        default="DO_TOKEN",
        metavar="VAR",
        help="variable de ESTA máquina cuyo valor se envía como DO_TOKEN. Sirve "
        "para mandar un token de sólo lectura guardado aparte, p. ej. DO_TOKEN_RO",
    )
    p.set_defaults(func=cmd_push_do_token)

    p = sub.add_parser(
        "authorize-key",
        help="DENTRO de una máquina: autoriza una clave pública para entrar por SSH",
    )
    p.add_argument(
        "clave",
        nargs="+",
        help="la línea entera de la clave pública, tal cual sale del fichero .pub",
    )
    p.set_defaults(func=cmd_authorize_key)

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

    p = sub.add_parser(
        "volume",
        help="volúmenes de bloques: el almacenamiento que sobrevive al droplet",
    )
    p.add_argument(
        "action", choices=["list", "create", "attach", "detach", "destroy"]
    )
    p.add_argument("name", nargs="?", help="nombre del volumen (o DO_VOLUME de .env)")
    p.add_argument("--droplet", help="droplet al que conectarlo (por defecto DO_DROPLET_NAME)")
    p.add_argument("--port", type=int)
    p.add_argument("--region", help="sólo en create; por defecto DO_REGION")
    p.add_argument("--size-gb", type=int, help="sólo en create; por defecto DO_VOLUME_SIZE_GB")
    p.add_argument("--description", help="sólo en create")
    p.add_argument(
        "--no-mount",
        action="store_true",
        help="en attach: conectar sin montar. Deja un disco que no ve ningún programa",
    )
    p.add_argument("--yes", action="store_true", help="en destroy: no preguntar")
    p.set_defaults(func=cmd_volume)

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
